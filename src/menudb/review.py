"""Review of ingredient matches, through the bucket like extraction.

    review/queue.json         every review_queue row, written by `review export` (Actions)
    review/decisions/*.json   lists of Decision, written by a session with `review push`
    review/applied/*.json     decision files moved there by `review apply` (Actions)

Logs carry counts and statuses only.
"""

import json
import logging
import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from menudb import storage
from menudb.db.models import Component, Course, Ingredient, IngredientAlias, Menu, ReviewItem
from menudb.ingredients import _norm

log = logging.getLogger(__name__)

QUEUE_KEY, DECISIONS, APPLIED = "review/queue.json", "review/decisions/", "review/applied/"


class Decision(BaseModel):
    raw_term: str
    language: str = "und"
    # map: point the term at `canonical` (created if new); accept: confirm the current
    # match; ignore: not an ingredient, unlink it
    action: Literal["map", "accept", "ignore"]
    canonical: str | None = Field(None, max_length=120)
    parent: str | None = Field(None, max_length=120)  # e.g. "porcini" -> parent "mushroom"
    category: str | None = Field(None, max_length=40)  # vegetable, seafood, dairy, herb, ...
    is_composite: bool | None = None  # dashi, XO sauce, garum


Decisions = TypeAdapter(list[Decision])


def export_queue(session: Session) -> list[dict]:
    """review_queue rows with the ingredient each term currently resolves to."""
    rows = session.execute(select(ReviewItem).order_by(ReviewItem.status, ReviewItem.occurrences.desc(),
                                                       ReviewItem.raw_term)).scalars()
    out = []
    for item in rows:
        current = session.scalar(
            select(Ingredient.canonical_name).join(IngredientAlias)
            .where(IngredientAlias.alias == _norm(item.raw_term),
                   IngredientAlias.language.in_([item.language, "und"])).limit(1))
        out.append({
            "raw_term": item.raw_term, "language": item.language, "status": item.status,
            "suggested_name": item.suggested_name, "current": current or (
                item.suggested_name if item.status == "auto" else None),
            "occurrences": item.occurrences,
        })
    return out


def _ingredient(session: Session, name: str) -> Ingredient:
    name = _norm(name)[:120]
    ing = session.scalar(select(Ingredient).where(Ingredient.canonical_name == name))
    if ing is None:
        ing = Ingredient(canonical_name=name)
        session.add(ing)
        session.flush()
    return ing


def _components(term: str, language: str):
    """Components spelled `term`, in menus of `language` (any language for "und")."""
    q = (select(Component.id).join(Course).join(Menu)
         .where(func.lower(func.btrim(Component.raw_term)) == term))
    if language != "und":
        q = q.where(Menu.language == language)
    return q


def _drop_orphans(session: Session) -> int:
    """Delete auto-created ingredients nothing points at any more."""
    child = Ingredient.__table__.alias("child")
    orphans = session.scalars(select(Ingredient).where(
        ~exists().where(Component.ingredient_id == Ingredient.id),
        ~exists().where(IngredientAlias.ingredient_id == Ingredient.id),
        ~exists().where(child.c.parent_id == Ingredient.id),
        Ingredient.category.is_(None), Ingredient.parent_id.is_(None),
    )).all()
    for ing in orphans:
        session.delete(ing)
    session.flush()
    return len(orphans)


def apply(session: Session, d: Decision) -> str:
    """Apply one decision. Returns its outcome for the run summary."""
    term, lang = _norm(d.raw_term), d.language or "und"
    item = session.scalar(select(ReviewItem).where(
        func.lower(ReviewItem.raw_term) == d.raw_term.lower(), ReviewItem.language == lang))
    alias = session.scalar(select(IngredientAlias).where(
        IngredientAlias.alias == term, IngredientAlias.language == lang))

    if d.action == "ignore":
        if alias:
            session.delete(alias)
        session.execute(update(Component).where(Component.id.in_(_components(term, lang)))
                        .values(ingredient_id=None))
        if item:
            item.status = "ignored"
        return "ignored"

    if d.action == "accept":
        target = (session.get(Ingredient, alias.ingredient_id) if alias else None) or (
            _ingredient(session, item.suggested_name) if item and item.suggested_name else None)
        if target is None:
            return "skipped"  # nothing to accept: map it instead
    else:
        if not d.canonical:
            return "skipped"
        target = _ingredient(session, d.canonical)

    if d.parent:
        parent = _ingredient(session, d.parent)
        if parent.id != target.id:
            target.parent_id = parent.id
    if d.category:
        target.category = d.category
    if d.is_composite is not None:
        target.is_composite = d.is_composite

    if term != target.canonical_name or alias:
        if alias is None:
            alias = IngredientAlias(alias=term, language=lang, ingredient_id=target.id)
            session.add(alias)
        alias.ingredient_id = target.id
    session.execute(update(Component).where(Component.id.in_(_components(term, lang)))
                    .values(ingredient_id=target.id))
    if item:
        item.status = "mapped"
    session.flush()
    return "mapped" if d.action == "map" else "accepted"


def apply_all(session: Session, decisions: list[Decision]) -> Counter:
    counts: Counter = Counter(apply(session, d) for d in decisions)
    if n := _drop_orphans(session):
        counts["ingredients_removed"] = n
    return counts


def export_to_bucket(session: Session) -> Counter:
    rows = export_queue(session)
    storage.put_bytes(QUEUE_KEY, json.dumps(rows, ensure_ascii=False).encode(), "application/json")
    return Counter(r["status"] for r in rows)


def apply_from_bucket(session: Session) -> Counter:
    counts: Counter = Counter()
    for k in sorted(storage.list_keys(DECISIONS)):
        try:
            decisions = Decisions.validate_json(storage.get_raw(k))
        except Exception as e:
            log.warning("invalid %s: %s", k, type(e).__name__)
            counts["invalid_files"] += 1
            continue
        with session.begin_nested():
            counts.update(apply_all(session, decisions))
        session.commit()
        storage.move(k, APPLIED + k.removeprefix(DECISIONS))
        counts["files"] += 1
    return counts


# --- review page: auto-created ingredients and lexicon candidates ---------------
#
# `review build` (bucket only) turns review/queue.json and review/lexicon_candidates.json
# into one list of items for a person to approve, merge or reject (a private artifact
# page or a JSON file). `review decide` applies their choices: lexicon edits to
# data/lexicon/ingredients.txt (committed by the session), alias decisions to
# review/decisions/ (applied by check.yml), rejected phrases to review/lexicon_rejected.json
# so they aren't offered again. Then `extract-auto --rerun` uses the new lexicon.

REJECTED_KEY = "review/lexicon_rejected.json"
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
CANDIDATES_KEY = "review/lexicon_candidates.json"


class Choice(BaseModel):
    """One reviewed item. approve: a real ingredient, under `canonical` (new or existing);
    merge: another name for the existing ingredient `canonical`; reject: not an ingredient."""
    kind: Literal["ingredient", "candidate"]
    term: str = Field(max_length=200)
    language: str | None = "und"
    action: Literal["approve", "merge", "reject"]
    canonical: str | None = Field(None, max_length=120)
    category: str | None = Field(None, max_length=40)
    parent: str | None = Field(None, max_length=120)


Choices = TypeAdapter(list[Choice])


def item_id(kind: str, term: str, language: str | None) -> str:
    from menudb.extract.auto import fold

    return f"{kind[0]}:{language or 'und'}:{fold(term.strip())}"


def _lang(language: str | None) -> str:
    """Lexicon language for a review language ("und" and unknowns count as English)."""
    return language if language and len(language) == 2 and language.isalpha() else "en"


def _suggest(kind: str, term: str, current: str | None, names: dict[str, str],
             terms: dict[str, str]) -> dict:
    """A cheap, rule-based starting point; the person decides."""
    import difflib

    from menudb.extract.auto import fold

    words = term.split()
    if kind == "candidate" and (len(words) > 3 or any(ch.isdigit() for ch in term)
                                or (len(words) > 1 and all(w[:1].isupper() for w in words))):
        return {"action": "reject"}
    f = fold(current or term)
    if f in names:
        return {"action": "merge", "canonical": names[f]}
    close = difflib.get_close_matches(f, list(terms), n=1, cutoff=0.88)
    if close:
        return {"action": "merge", "canonical": terms[close[0]]}
    if kind == "ingredient" and current:
        return {"action": "approve", "canonical": current}
    return {}


def build_items(queue: list[dict], candidates: list[dict], lexicon_path, *,
                rejected: list[dict] | None = None, min_restaurants: int = 2) -> list[dict]:
    """Items still needing a decision, most widely used first.

    Ingredients: review_queue rows whose current match isn't a lexicon ingredient (the
    lexicon is already curated). Candidates: unmatched phrases seen at `min_restaurants`
    or more restaurants that the lexicon doesn't know yet and nobody rejected."""
    from menudb.extract.auto import fold
    from menudb.lexicon_edit import LexiconFile

    lex = LexiconFile.read(lexicon_path)
    canon = lex.canonicals()
    names = {fold(n): n for n in canon}
    terms = dict(names)  # every known term (stemmed tokens) -> its canonical
    for key, cands in lex.parsed().words.items():
        for _, e in cands:
            terms.setdefault(" ".join(key), e.name)
    gone = {item_id("candidate", r["phrase"], r.get("language")) for r in rejected or []}

    items = []
    for r in queue:
        if r["status"] not in ("auto", "open"):
            continue
        current = r.get("current") or r.get("suggested_name")
        if (current and current in canon) or lex.lookup(r["raw_term"], _lang(r["language"])):
            continue  # curated already, or the lexicon decides this term on re-extraction
        items.append({"id": item_id("ingredient", r["raw_term"], r["language"]), "kind": "ingredient",
                      "term": r["raw_term"], "language": r["language"], "current": current,
                      "count": r.get("occurrences") or 0,
                      "suggest": _suggest("ingredient", r["raw_term"], current, names, terms)})
    for r in candidates:
        if r.get("restaurants", 0) < min_restaurants:
            continue
        iid = item_id("candidate", r["phrase"], r.get("language"))
        if iid in gone or lex.lookup(r["phrase"], _lang(r.get("language"))):
            continue
        items.append({"id": iid, "kind": "candidate", "term": r["phrase"],
                      "language": r.get("language"), "current": None, "count": r["restaurants"],
                      "suggest": _suggest("candidate", r["phrase"], None, names, terms)})
    import hashlib

    seen, out = set(), []
    for it in sorted(items, key=lambda i: (i["kind"] != "ingredient", -i["count"], i["term"].lower())):
        if it["id"] not in seen:
            seen.add(it["id"])
            it["key"] = hashlib.sha1(it["id"].encode()).hexdigest()[:16]  # a path-safe id for the page
            out.append(it)
    return out


def decide(choices: list[Choice], lexicon_path) -> tuple[Counter, list[Decision], list[dict]]:
    """Apply choices to the lexicon file. Returns counts, the alias decisions for
    review/decisions/, and the newly rejected candidate phrases."""
    from menudb.lexicon_edit import CATEGORIES, LexiconFile, valid_name

    lex = LexiconFile.read(lexicon_path)
    counts: Counter = Counter()
    decisions: list[Decision] = []
    rejected: list[dict] = []
    for c in choices:
        lang = _lang(c.language)
        if c.action == "reject":
            if c.kind == "ingredient":
                decisions.append(Decision(raw_term=c.term, language=c.language or "und", action="ignore"))
            else:
                rejected.append({"phrase": c.term, "language": c.language})
            counts["rejected"] += 1
            continue
        canonical = _norm(c.canonical or c.term)
        if not (valid_name(canonical) and valid_name(c.term)) or (c.parent and not valid_name(c.parent)):
            counts["skipped_bad_name"] += 1
            continue
        canon = lex.canonicals()
        if canonical not in canon and len(alias_of := lex.lookup(canonical, "en")) == 1:
            canonical = alias_of.pop()  # "rum" is already a name for spirits: use that
            counts["redirected_to_existing"] += 1
        term = c.term.strip().lower()
        known = lex.lookup(term, lang)
        if c.kind == "ingredient":
            decisions.append(Decision(raw_term=c.term, language=c.language or "und", action="map",
                                      canonical=canonical, parent=c.parent,
                                      category=c.category if c.category in CATEGORIES else None))
        counts["approved" if c.action == "approve" else "merged"] += 1
        if canonical in known:
            counts["lexicon_already"] += 1
            continue
        if known:
            counts["lexicon_conflict"] += 1  # the term already means something else; DB only
            continue
        if len(term.replace(" ", "")) < 3 and not _CJK_RE.search(term):
            counts["lexicon_too_short"] += 1  # "mu" would match everywhere; DB only
            continue
        if canonical not in canon:
            if c.action == "merge":
                counts["merge_target_added"] += 1
            category = c.category if c.category in CATEGORIES else "other"
            parent = _norm(c.parent) if c.parent and _norm(c.parent) in canon else None
            lex.add_entry(canonical, category, lang, term, parent=parent)
            counts["lexicon_new_ingredient"] += 1
        elif lex.add_term(canonical, lang, term):
            counts["lexicon_new_term"] += 1
    lex.write()
    return counts, decisions, rejected

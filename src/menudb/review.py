"""Review of ingredient matches, through the bucket like extraction.

    review/queue.json         every review_queue row, written by `review export` (Actions)
    review/decisions/*.json   lists of Decision, written by a session with `review push`
    review/applied/*.json     decision files moved there by `review apply` (Actions)

Logs carry counts and statuses only.
"""

import json
import logging
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

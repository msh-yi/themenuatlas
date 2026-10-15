"""Bucket handoff between Actions (has Postgres) and cloud sessions (don't).

    pending/<slug>/<normalized_hash>.json    PendingItem, written by `pending`
    extracted/<slug>/<normalized_hash>.json  ExtractedMenu, written by `extract-io put`
    imported/<slug>/<normalized_hash>.json   moved there by `import --from-bucket`
    processed/<slug>/<normalized_hash>.json  pending items `extract-auto` has done, kept so
                                             it can re-run when the lexicon improves
    review/candidates/<slug>/<hash>.json     phrases `extract-auto` couldn't match, per document;
    review/lexicon_candidates.json           the same, totalled, for growing the lexicon

Keys use (slug, normalized_hash), the same identity as a menu row, so a document
can be queued and extracted before `check` has created its menu; import waits
until the row exists. Logs carry slugs, ids and hashes only, never menu text.
"""

import json
import logging
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from menudb import ingredients, storage
from menudb.db.models import Component, Course, Menu, Restaurant, Snapshot, Source
from menudb.extract.schema import ExtractedMenu, PendingItem
from menudb.extract.text import document_kind, document_text
from menudb.normalize import content_hash, normalized_hash

log = logging.getLogger(__name__)

PENDING, EXTRACTED, IMPORTED, PROCESSED = "pending/", "extracted/", "imported/", "processed/"
CANDIDATES, CANDIDATES_SUMMARY = "review/candidates/", "review/lexicon_candidates.json"


def key(prefix: str, slug: str, nhash: str) -> str:
    return f"{prefix}{slug}/{nhash}.json"


def put_json(k: str, model) -> None:
    storage.put_bytes(k, model.model_dump_json().encode(), "application/json")


def get_json(k: str) -> dict:
    return json.loads(storage.get_raw(k))


def _known(prefixes: Iterable[str]) -> set[tuple[str, str]]:
    """(slug, hash) pairs that already have an object under any of `prefixes`."""
    out = set()
    for prefix in prefixes:
        for k in storage.list_keys(prefix):
            slug, _, name = k.removeprefix(prefix).rpartition("/")
            out.add((slug, name.removesuffix(".json")))
    return out


# --- pending ------------------------------------------------------------------

def item_from_bytes(slug: str, label: str, url: str, data: bytes, ctype: str,
                    menu_id: int | None = None, store: bool = True) -> PendingItem:
    """Build a pending item with the same identity `check` gives the document."""
    from menudb.pipeline import _MIME

    kind = document_kind(ctype, url, data)
    text = document_text(kind, data)
    raw_hash = content_hash(data)
    nhash = normalized_hash(text) if text else raw_hash
    k = storage.put_raw(raw_hash, data, _MIME[kind]) if store else storage.key_for(raw_hash)
    return PendingItem(slug=slug, source_label=label, normalized_hash=nhash, menu_id=menu_id,
                       kind=kind, url=url, storage_key=k, text=text)


def pending_from_db(session: Session, limit: int | None = None) -> Counter:
    """Queue every menu that has no extraction yet and isn't already in the bucket."""
    known = _known((PENDING, EXTRACTED, IMPORTED, PROCESSED))
    rows = session.execute(
        select(Menu.id, Restaurant.slug, Source.label, Snapshot)
        .join(Restaurant, Menu.restaurant_id == Restaurant.id)
        .join(Snapshot, Menu.first_snapshot_id == Snapshot.id)
        .join(Source, Snapshot.source_id == Source.id)
        .where(Menu.extraction_version.is_(None))
        .order_by(Menu.id)
    ).all()
    counts: Counter = Counter()
    for menu_id, slug, label, snap in rows:
        if (slug, snap.normalized_hash) in known:
            counts["already_queued"] += 1
            continue
        if limit is not None and counts["queued"] >= limit:
            break
        data = storage.get_raw(snap.storage_key)
        text = document_text(snap.content_type, data)
        item = PendingItem(slug=slug, source_label=label, normalized_hash=snap.normalized_hash,
                           menu_id=menu_id, kind=snap.content_type, url=snap.url,
                           storage_key=snap.storage_key, text=text)
        put_json(key(PENDING, slug, item.normalized_hash), item)
        log.info("queued menu=%s slug=%s hash=%s", menu_id, slug, item.normalized_hash[:12])
        counts["queued"] += 1
    return counts


def queue_items(items: Iterable[PendingItem]) -> Counter:
    """Write items to pending/ unless already pending, extracted or imported."""
    known = _known((PENDING, EXTRACTED, IMPORTED, PROCESSED))
    counts: Counter = Counter()
    for item in items:
        if (item.slug, item.normalized_hash) in known:
            counts["already_queued"] += 1
            continue
        known.add((item.slug, item.normalized_hash))
        put_json(key(PENDING, item.slug, item.normalized_hash), item)
        counts["queued"] += 1
    return counts


# --- rule-based extraction ----------------------------------------------------

def extract_auto(slugs: set[str] | None = None, countries: dict[str, str] | None = None,
                 rerun: bool = False, limit: int | None = None, dry_run: bool = False) -> Counter:
    """Run the lexicon extractor over pending/ (and processed/ with `rerun`).

    Writes extracted/ and moves each pending item to processed/. A session's
    extraction is never overwritten: rerun only replaces earlier lexicon output.
    Items with no text layer (scans, images) stay pending for a session.
    """
    from menudb.extract import auto

    countries = countries or {}
    keys = storage.list_keys(PENDING) + (storage.list_keys(PROCESSED) if rerun else [])
    counts: Counter = Counter()
    for k in sorted(keys):
        slug = k.split("/")[1]
        if slugs is not None and slug not in slugs:
            continue
        if limit is not None and counts["extracted"] >= limit:
            break
        item = PendingItem.model_validate(get_json(k))
        if not item.text:
            counts["no_text_layer"] += 1
            continue
        out = key(EXTRACTED, item.slug, item.normalized_hash)
        if k.startswith(PROCESSED):
            done = next((p + out.removeprefix(EXTRACTED) for p in (EXTRACTED, IMPORTED)
                         if storage.exists(p + out.removeprefix(EXTRACTED))), None)
            if done and not get_json(done).get("extractor", "").startswith("lexicon"):
                counts["kept_session_extraction"] += 1
                continue
        found: list[str] = []
        ex = auto.extract(item, country=countries.get(item.slug), candidates=found)
        counts["extracted"] += 1
        counts["menus" if ex.is_menu else "not_a_menu"] += 1
        counts["courses"] += len(ex.courses)
        comps = [c for co in ex.courses for c in co.components]
        counts["components"] += len(comps)
        counts["candidates"] += len(found)
        log.info("extracted slug=%s hash=%s is_menu=%s courses=%d",
                 item.slug, item.normalized_hash[:12], ex.is_menu, len(ex.courses))
        if dry_run:
            continue
        put_json(out, ex)
        storage.put_bytes(key(CANDIDATES, item.slug, item.normalized_hash),
                          json.dumps({"language": ex.language, "phrases": found}).encode(),
                          "application/json")
        if k.startswith(PENDING):
            storage.move(k, PROCESSED + k.removeprefix(PENDING))
    if counts["extracted"] and not dry_run:
        summarize_candidates()
    return counts


def summarize_candidates() -> int:
    """Total the per-document candidates into review/lexicon_candidates.json:
    phrase, language, how many restaurants use it. Returns the number of phrases."""
    from menudb.extract.auto import fold

    totals: dict[tuple[str, str], dict] = {}
    for k in storage.list_keys(CANDIDATES):
        slug = k.removeprefix(CANDIDATES).split("/")[0]
        doc = get_json(k)
        for phrase in set(doc.get("phrases", [])):
            row = totals.setdefault((fold(phrase), doc.get("language") or "und"),
                                    {"phrase": phrase, "language": doc.get("language"), "slugs": set()})
            row["slugs"].add(slug)
    rows = sorted(({"phrase": r["phrase"], "language": r["language"], "restaurants": len(r["slugs"])}
                   for r in totals.values()), key=lambda r: (-r["restaurants"], r["phrase"]))
    storage.put_bytes(CANDIDATES_SUMMARY, json.dumps(rows, ensure_ascii=False).encode(),
                      "application/json")
    return len(rows)


# --- import -------------------------------------------------------------------

def import_menu(session: Session, ex: ExtractedMenu, now: datetime | None = None) -> str:
    """Write one extraction into Postgres. Returns imported | waiting | unknown_restaurant.

    Re-importing replaces the menu's courses, so it is safe to repeat.
    """
    now = now or datetime.now(timezone.utc)
    restaurant_id = session.scalar(select(Restaurant.id).where(Restaurant.slug == ex.slug))
    if restaurant_id is None:
        return "unknown_restaurant"
    menu = session.get(Menu, ex.menu_id) if ex.menu_id else None
    if menu is None or menu.restaurant_id != restaurant_id:
        menu = session.scalar(select(Menu).where(
            Menu.restaurant_id == restaurant_id, Menu.normalized_hash == ex.normalized_hash))
    if menu is None:
        return "waiting"  # `check` hasn't recorded this document yet

    session.execute(delete(Course).where(Course.menu_id == menu.id))
    menu.menu_type = ex.menu_type if ex.is_menu else "not_a_menu"
    menu.language, menu.price, menu.currency = ex.language, ex.price, ex.currency
    menu.extraction_version, menu.extraction_model, menu.extracted_at = (
        ex.extraction_version, ex.extractor, now)
    for c in ex.courses if ex.is_menu else []:
        course = Course(menu_id=menu.id, position=c.position, raw_text=c.raw_text, kind=c.kind)
        session.add(course)
        session.flush()
        for comp in c.components:
            session.add(Component(
                course_id=course.id, raw_term=comp.raw_term, suggested_name=comp.name,
                role=comp.role, preparations=comp.preparations,
                ingredient_id=ingredients.match(session, comp.raw_term, comp.name, ex.language,
                                                context=f"menu {menu.id} course {c.position}"),
            ))
    session.flush()
    log.info("imported menu=%s slug=%s courses=%d", menu.id, ex.slug, len(ex.courses))
    return "imported"


def import_from_bucket(session: Session) -> Counter:
    counts: Counter = Counter()
    for k in storage.list_keys(EXTRACTED):
        try:
            ex = ExtractedMenu.model_validate(get_json(k))
        except Exception as e:  # don't log the body; the error type is enough
            log.warning("invalid %s: %s", k, type(e).__name__)
            counts["invalid"] += 1
            continue
        try:
            with session.begin_nested():
                outcome = import_menu(session, ex)
        except Exception as e:
            log.warning("failed %s: %s", k, type(e).__name__)
            counts["failed"] += 1
            continue
        if outcome == "imported":
            session.commit()
            storage.move(k, IMPORTED + k.removeprefix(EXTRACTED))
        elif outcome == "unknown_restaurant":
            log.warning("unknown restaurant for %s", k)
        counts[outcome] += 1
    return counts

"""`menudb check`: tier 0 (unchanged → stop) and tier 1 (new menu → snapshot).

Logs carry IDs, hashes and statuses only, never menu text: Actions logs are public.
"""

import logging
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from menudb import storage
from menudb.db.models import Menu, Snapshot, Source
from menudb.extract.text import document_kind, document_text
from menudb.fetch.http import Fetcher, RobotsDisallowed
from menudb.normalize import content_hash, normalized_hash
from menudb.recipes import Recipe, resolve

log = logging.getLogger(__name__)

MIN_INTERVAL = timedelta(days=3)
MAX_INTERVAL = timedelta(days=30)
_MIME = {"html": "text/html", "pdf": "application/pdf", "image": "application/octet-stream"}


def due_sources(session: Session, now: datetime, limit: int | None = None) -> list[Source]:
    q = (
        select(Source)
        .where(
            Source.mode == "check",
            or_(
                Source.last_checked.is_(None),
                Source.last_checked + Source.check_interval <= now,
            )
        )
        .order_by(Source.last_checked.asc().nulls_first())
        .limit(limit)
    )
    return list(session.scalars(q))


def _record_document(
    session: Session, source: Source, url: str, data: bytes, ctype: str, now: datetime,
    origin: str = "live", wayback_ts: str | None = None,
) -> bool:
    """Store a fetched document if it is a menu we haven't seen. Returns True if new."""
    kind = document_kind(ctype, url, data)
    text = document_text(kind, data)
    raw_hash = content_hash(data)
    # no text layer (image-only): fall back to the raw bytes as identity
    nhash = normalized_hash(text) if text else raw_hash

    menu = session.scalar(
        select(Menu).where(Menu.restaurant_id == source.restaurant_id, Menu.normalized_hash == nhash)
    )
    if menu:
        menu.first_seen = min(menu.first_seen, now)
        menu.last_seen = max(menu.last_seen, now)
        return False

    key = storage.put_raw(raw_hash, data, _MIME[kind])
    snap = Snapshot(
        source_id=source.id, fetched_at=now, origin=origin, wayback_ts=wayback_ts, url=url,
        content_type=kind, content_hash=raw_hash, normalized_hash=nhash, storage_key=key,
    )
    session.add(snap)
    session.flush()
    session.add(Menu(
        restaurant_id=source.restaurant_id, normalized_hash=nhash, first_snapshot_id=snap.id,
        first_seen=now, last_seen=now,
    ))
    log.info("source=%s new menu hash=%s kind=%s", source.id, nhash[:12], kind)
    return True


@dataclass
class Document:
    url: str
    data: bytes
    content_type: str
    tdm_reserved: bool


def html_getter(recipe: Recipe, fetcher: Fetcher, renderer=None):
    """Navigation-page loader for a recipe: plain HTTP, or headless Chromium for render: js."""
    def get_html(url: str) -> tuple[str, str]:
        if recipe.render == "js":
            if not fetcher.allowed(url):
                raise RobotsDisallowed(url)
            return renderer.render(url)
        return fetcher.get_html(url)
    return get_html


def fetch_documents(recipe: Recipe, fetcher: Fetcher, get_html, validators: dict) -> Iterator[Document]:
    """Walk the recipe and fetch each menu document. Skips 304s; updates `validators` in place."""
    for url in resolve(recipe, get_html):
        if recipe.render == "js" and recipe.content == "html_main":
            _, html = get_html(url)
            yield Document(url, html.encode(), "text/html", False)
            continue
        v = validators.get(url, {})
        res = fetcher.get(url, etag=v.get("etag"), last_modified=v.get("last_modified"))
        if res.not_modified:
            continue
        validators[url] = {"etag": res.etag, "last_modified": res.last_modified}
        yield Document(url, res.content, res.content_type, res.tdm_reserved)


def check_source(session: Session, source: Source, fetcher: Fetcher, renderer=None,
                 now: datetime | None = None) -> str:
    """Returns one of: unchanged | changed | failed | opted_out."""
    now = now or datetime.now(timezone.utc)
    recipe = Recipe.model_validate(source.recipe)
    source.last_checked = now

    get_html = html_getter(recipe, fetcher, renderer)

    try:
        # savepoint: a failure part-way through leaves no half-recorded menus
        with session.begin_nested():
            changed, validators, opted_out = _walk(session, source, recipe, fetcher, get_html, now)
    except RobotsDisallowed:
        source.robots_ok = False
        return _fail(source, "robots.txt disallows")
    except Exception as e:  # recipe drift, HTTP errors, parse failures
        return _fail(source, f"{type(e).__name__}: {e}"[:500])

    if opted_out:
        source.tdm_reserved = True
        source.last_error = "tdm-reservation"
        log.info("source=%s opted out of TDM; skipping", source.id)
        return "opted_out"

    source.validators = validators
    source.robots_ok = True
    source.tdm_reserved = False
    source.consecutive_failures = 0
    source.last_error = None
    source.last_verified = now
    # adapt cadence: menus that change get checked more often
    interval = source.check_interval / 2 if changed else source.check_interval * 1.5
    source.check_interval = min(max(interval, MIN_INTERVAL), MAX_INTERVAL)
    return "changed" if changed else "unchanged"


def _walk(session, source, recipe, fetcher, get_html, now) -> tuple[bool, dict, bool]:
    changed = False
    validators = dict(source.validators or {})
    for doc in fetch_documents(recipe, fetcher, get_html, validators):
        if doc.tdm_reserved:
            return False, validators, True
        changed |= _record_document(session, source, doc.url, doc.data, doc.content_type, now)
    return changed, validators, False


def _fail(source: Source, error: str) -> str:
    source.consecutive_failures += 1
    source.last_error = error
    log.warning("source=%s failed (%dx): %s", source.id, source.consecutive_failures, error)
    return "failed"


def run_check(session: Session, fetcher: Fetcher, renderer=None, limit: int | None = None) -> Counter:
    now = datetime.now(timezone.utc)
    counts: Counter = Counter()
    for source in due_sources(session, now, limit):
        outcome = check_source(session, source, fetcher, renderer, now)
        counts[outcome] += 1
        session.commit()
    return counts

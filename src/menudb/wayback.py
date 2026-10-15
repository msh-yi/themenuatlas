"""`menudb backfill`: menu history from the Wayback Machine.

For each source, list distinct captures of the recipe's start page with the CDX API
(collapse=digest, at most one per month), then replay the recipe against
web.archive.org/web/<ts>id_/<url> and record what it finds as origin=wayback
snapshots. `id_` returns the archived bytes without Wayback's rewriting, so links
stay original and the recipe's selectors work unchanged.

Politeness: about one request per second to web.archive.org, and the restaurant's
current robots.txt and TDM signals still apply to the archived copy.
web.archive.org is unreachable from cloud sessions, so this runs in Actions or locally.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from menudb.db.models import Source
from menudb.fetch.http import FetchError, Fetcher, RobotsDisallowed
from menudb.normalize import content_hash
from menudb.pipeline import _record_document
from menudb.recipes import Recipe, resolve

log = logging.getLogger(__name__)

WAYBACK = "https://web.archive.org"
# Seconds between requests to web.archive.org. At 1s the archive refused connections
# after about 70 requests (first Actions run, 2026-10-15); 5s keeps to ~12 a minute.
WAYBACK_INTERVAL = 5.0
_REPLAY = re.compile(r"^https?://web\.archive\.org/web/(\d{14})(?:[a-z]{2}_)?/(.+)$")


class Skipped(Exception):
    pass


class WaybackUnavailable(Exception):
    """The archive is refusing us (429 or refused connections): stop the whole run."""


def _refused(e: Exception) -> bool:
    msg = str(e)
    return isinstance(e, FetchError) and any(
        s in msg for s in ("HTTP 429", "ConnectError", "ConnectTimeout"))


@dataclass
class Capture:
    timestamp: str  # YYYYMMDDhhmmss
    original: str
    digest: str


@dataclass
class ArchivedDocument:
    url: str  # original URL
    timestamp: str  # capture actually served (Wayback picks the nearest)
    data: bytes
    content_type: str
    tdm_reserved: bool


def replay_url(url: str, ts: str) -> str:
    return f"{WAYBACK}/web/{ts}id_/{url}"


def unwrap(url: str) -> tuple[str, str | None]:
    """web.archive.org/web/<ts>id_/<original> -> (original, ts); other URLs pass through."""
    m = _REPLAY.match(url)
    return (m.group(2), m.group(1)) if m else (url, None)


def ts_datetime(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def captures(wb: Fetcher, url: str, since: str | None = None, until: str | None = None,
             per_month: bool = True) -> list[Capture]:
    """Distinct successful captures of `url`, oldest first."""
    params = {"url": url, "output": "json", "fl": "timestamp,original,digest",
              "filter": "statuscode:200", "collapse": "digest"}
    if since:
        params["from"] = since
    if until:
        params["to"] = until
    res = wb.get(f"{WAYBACK}/cdx/search/cdx?{urlencode(params)}")
    rows = json.loads(res.content or b"[]")
    caps = [Capture(*row) for row in rows[1:]]  # first row is the header
    if per_month:
        by_month: dict[str, Capture] = {}
        for c in caps:
            by_month.setdefault(c.timestamp[:6], c)
        caps = list(by_month.values())
    return caps


def _check_live_signals(live: Fetcher, url: str) -> None:
    """The restaurant's current robots.txt and tdmrep.json apply to archived copies too."""
    if not live.allowed(url):
        raise RobotsDisallowed(url)
    _, host = live._host(url)
    if live._tdm_from_wellknown(url, host):
        raise Skipped("tdm-reservation")


def replay(recipe: Recipe, ts: str, wb: Fetcher, live: Fetcher) -> list[ArchivedDocument]:
    """Walk the recipe as the site looked at `ts`."""
    if recipe.render == "js":
        raise Skipped("render: js pages don't replay from the archive")

    def get_html(url: str) -> tuple[str, str]:
        _check_live_signals(live, url)
        res = wb.get(replay_url(url, ts))
        return unwrap(res.final_url)[0], res.content.decode(errors="replace")

    docs = []
    for url in resolve(recipe, get_html):
        _check_live_signals(live, url)
        res = wb.get(replay_url(url, ts))
        original, served_ts = unwrap(res.final_url)
        docs.append(ArchivedDocument(original, served_ts or ts, res.content, res.content_type,
                                     res.tdm_reserved))
    return docs


def backfill_source(session: Session, source: Source, wb: Fetcher, live: Fetcher,
                    since: str | None = None, max_captures: int = 24) -> Counter:
    """Record archived menus for one source. Counts: captures, new_menus, seen, failed, skipped."""
    counts: Counter = Counter()
    recipe = Recipe.model_validate(source.recipe)
    if recipe.render == "js":
        counts["skipped"] += 1
        log.info("source=%s skipped: render js", source.id)
        return counts
    try:
        _check_live_signals(live, recipe.start)
    except (RobotsDisallowed, Skipped) as e:
        counts["skipped"] += 1
        log.info("source=%s skipped: %s", source.id, type(e).__name__)
        return counts

    try:
        caps = captures(wb, recipe.start, since=since)[-max_captures:]
    except Exception as e:
        # RobotsDisallowed here is web.archive.org's own robots.txt (or its being
        # unreachable, which the Fetcher treats as disallow-all): nothing else will work.
        if _refused(e) or isinstance(e, RobotsDisallowed):
            raise WaybackUnavailable(type(e).__name__) from e
        counts["failed"] += 1
        log.info("source=%s cdx failed: %s", source.id, type(e).__name__)
        return counts
    seen_raw: set[str] = set()
    for cap in caps:
        counts["captures"] += 1
        try:
            with session.begin_nested():
                for doc in replay(recipe, cap.timestamp, wb, live):
                    if doc.tdm_reserved:
                        raise Skipped("tdm-reservation")
                    raw = content_hash(doc.data)
                    if raw in seen_raw:  # same bytes served for a neighbouring capture
                        continue
                    seen_raw.add(raw)
                    new = _record_document(session, source, doc.url, doc.data, doc.content_type,
                                           ts_datetime(doc.timestamp), origin="wayback",
                                           wayback_ts=doc.timestamp)
                    counts["new_menus" if new else "seen"] += 1
        except Skipped:
            counts["skipped"] += 1
            log.info("source=%s ts=%s skipped", source.id, cap.timestamp)
        except Exception as e:  # archived page missing, recipe didn't match back then, ...
            if _refused(e):
                session.commit()
                raise WaybackUnavailable(type(e).__name__) from e
            counts["failed"] += 1
            log.info("source=%s ts=%s failed: %s", source.id, cap.timestamp, type(e).__name__)
        session.commit()
    log.info("source=%s backfill %s", source.id, dict(counts))
    return counts

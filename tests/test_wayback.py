"""Wayback backfill against a mock archive and a local Postgres."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from menudb import pipeline, wayback
from menudb.db import make_engine
from menudb.db.models import Menu, Restaurant, Snapshot, Source
from menudb.fetch.http import Fetcher

CDX_ROWS = [
    ["timestamp", "original", "digest"],
    ["20260105120000", "https://r.example/menu", "AAA"],
    ["20260120120000", "https://r.example/menu", "BBB"],  # same month: collapsed
    ["20260310120000", "https://r.example/menu", "CCC"],
]


def mock_web(fixture_text, robots="User-agent: *\nAllow: /", calls=None):
    pages = {"202601": fixture_text("menu_v1.html"), "202603": fixture_text("menu_v2.html")}

    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "r.example":
            if path == "/robots.txt":
                return httpx.Response(200, text=robots)
            return httpx.Response(404)
        if host == "web.archive.org":
            if path == "/cdx/search/cdx":
                return httpx.Response(200, text=json.dumps(CDX_ROWS),
                                      headers={"content-type": "application/json"})
            original, ts = wayback.unwrap(str(request.url))
            if ts and original == "https://r.example/menu":
                return httpx.Response(200, text=pages[ts[:6]], headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    make = lambda: Fetcher("t", min_interval=0, transport=transport, sleep=lambda s: None)
    return make(), make()


def test_unwrap():
    assert wayback.unwrap("https://web.archive.org/web/20260105120000id_/https://r.example/a?b=1") == (
        "https://r.example/a?b=1", "20260105120000")
    assert wayback.unwrap("https://r.example/a") == ("https://r.example/a", None)


def test_captures_one_per_month(fixture_text):
    wb, _ = mock_web(fixture_text)
    caps = wayback.captures(wb, "https://r.example/menu")
    assert [c.timestamp for c in caps] == ["20260105120000", "20260310120000"]


@pytest.fixture
def session(pg_url, monkeypatch):
    monkeypatch.setattr(pipeline.storage, "put_raw", lambda h, data, ct: f"raw/{h}")
    engine = make_engine(pg_url)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        r = Restaurant(slug="wb-test", name="Test")
        s.add(r)
        s.flush()
        src = Source(restaurant_id=r.id, recipe={"start": "https://r.example/menu"}, validators={})
        s.add(src)
        s.commit()
        yield s, src
        s.rollback()
        for model in (Menu, Snapshot, Source, Restaurant):
            s.query(model).delete()
        s.commit()
    engine.dispose()


@pytest.mark.db
def test_backfill_records_history(session, fixture_text):
    s, src = session
    wb, live = mock_web(fixture_text)
    counts = wayback.backfill_source(s, src, wb, live)
    assert counts == {"captures": 2, "new_menus": 2}
    snaps = s.scalars(select(Snapshot).order_by(Snapshot.fetched_at)).all()
    assert [(x.origin, x.wayback_ts) for x in snaps] == [
        ("wayback", "20260105120000"), ("wayback", "20260310120000")]
    first = s.scalar(select(Menu).order_by(Menu.first_seen))
    assert first.first_seen == datetime(2026, 1, 5, 12, tzinfo=timezone.utc)

    # a rerun finds the same menus and adds nothing
    assert wayback.backfill_source(s, src, wb, live) == {"captures": 2, "seen": 2}


@pytest.mark.db
def test_backfill_respects_live_robots(session, fixture_text):
    s, src = session
    calls = []
    wb, live = mock_web(fixture_text, robots="User-agent: *\nDisallow: /", calls=calls)
    assert wayback.backfill_source(s, src, wb, live) == {"skipped": 1}
    assert not any("web.archive.org" in c for c in calls)

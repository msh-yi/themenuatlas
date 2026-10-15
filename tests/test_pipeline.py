"""check_source end to end against a local Postgres, a mock website and in-memory storage."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from menudb import pipeline
from menudb.db import make_engine
from menudb.db.models import Menu, Restaurant, Snapshot, Source
from menudb.fetch.http import Fetcher

pytestmark = pytest.mark.db


@pytest.fixture
def session(pg_url, monkeypatch):
    blobs = {}
    monkeypatch.setattr(pipeline.storage, "put_raw",
                        lambda h, data, ct: blobs.setdefault(f"raw/{h}", data) and f"raw/{h}")
    engine = make_engine(pg_url)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
        s.rollback()
        for model in (Menu, Snapshot, Source, Restaurant):
            s.query(model).delete()
        s.commit()
    engine.dispose()


@pytest.fixture
def site(fixture_text):
    """Mutable mock website: tests swap pages to simulate menu updates."""
    pages = {"/menu": fixture_text("menu_v1.html")}

    def handler(request):
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    fetcher = Fetcher("t", min_interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return pages, fetcher


def make_source(session, recipe):
    r = Restaurant(slug="noma-test", name="Test", lat=55.68, lon=12.61)
    session.add(r)
    session.flush()
    src = Source(restaurant_id=r.id, recipe=recipe, validators={})
    session.add(src)
    session.commit()
    return src


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_new_then_unchanged_then_changed(session, site, fixture_text):
    pages, fetcher = site
    src = make_source(session, {"start": "https://r.example/menu"})
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)

    assert pipeline.check_source(session, src, fetcher, now=t0) == "changed"
    assert count(session, Menu) == 1 and count(session, Snapshot) == 1

    # cosmetic changes only: same menu, last_seen moves forward, no new snapshot
    pages["/menu"] = fixture_text("menu_v1_noise.html")
    t1 = t0 + timedelta(days=7)
    assert pipeline.check_source(session, src, fetcher, now=t1) == "unchanged"
    assert count(session, Menu) == 1 and count(session, Snapshot) == 1
    assert session.scalar(select(Menu.last_seen)) == t1

    # a dish changes: new menu
    pages["/menu"] = fixture_text("menu_v2.html")
    assert pipeline.check_source(session, src, fetcher, now=t1 + timedelta(days=7)) == "changed"
    assert count(session, Menu) == 2 and count(session, Snapshot) == 2


def test_recipe_drift_is_a_failure_and_rolls_back(session, site):
    _, fetcher = site
    src = make_source(session, {
        "start": "https://r.example/menu",
        "steps": [{"select": {"css": "a.no-such-link"}}],
    })
    assert pipeline.check_source(session, src, fetcher) == "failed"
    assert src.consecutive_failures == 1 and "matched nothing" in src.last_error
    assert count(session, Menu) == 0


def test_tdm_opt_out_stores_nothing(session, site):
    pages, fetcher = site
    pages["/menu"] = '<html><head><meta name="tdm-reservation" content="1"></head><body>x</body></html>'
    src = make_source(session, {"start": "https://r.example/menu"})
    assert pipeline.check_source(session, src, fetcher) == "opted_out"
    assert src.tdm_reserved and count(session, Snapshot) == 0


def test_due_sources_respects_interval(session, site):
    src = make_source(session, {"start": "https://r.example/menu"})
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert pipeline.due_sources(session, now) == [src]
    src.last_checked, src.check_interval = now - timedelta(days=2), timedelta(days=7)
    session.commit()
    assert pipeline.due_sources(session, now) == []

"""Data-quality checks in the stats summary."""

from datetime import datetime, timezone

import pytest

from menudb import handoff, quality
from menudb.db.models import Menu, Restaurant, Snapshot, Source
from menudb.domains import not_official
from menudb.extract.schema import PendingItem
from test_handoff import bucket, extraction, make_menu, session  # noqa: F401 (fixtures)


def test_not_official_hosts():
    assert not_official("www.timeout.com") and not_official("foo.wordpress.com")
    assert not not_official("noma.dk") and not not_official("")


def by_check(session):
    return {r["check"]: r for r in quality.checks(session)}


@pytest.mark.db
def test_quality_flags_shared_documents_hosts_and_empty_menus(session, bucket):  # noqa: F811
    menu = make_menu(session, bucket)
    item = PendingItem(slug="r", normalized_hash=menu.normalized_hash, kind="html", url="u",
                       storage_key="k")
    handoff.import_menu(session, extraction(item))
    session.get(Restaurant, menu.restaurant_id).website = "https://www.timeout.com/x/r"

    # the same document and URL under a second restaurant, whose website shares the host
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    r2 = Restaurant(slug="r2", name="R2", website="https://timeout.com/y")
    session.add(r2)
    session.flush()
    src = Source(restaurant_id=r2.id, label="main", recipe={"start": "https://r.example/menu"}, validators={})
    session.add(src)
    session.flush()
    snap = Snapshot(source_id=src.id, fetched_at=now, origin="live", url="https://r.example/menu",
                    content_type="html", content_hash="c", normalized_hash=menu.normalized_hash,
                    storage_key="raw/c")
    session.add(snap)
    session.flush()
    m2 = Menu(restaurant_id=r2.id, normalized_hash=menu.normalized_hash, first_snapshot_id=snap.id,
              first_seen=now, last_seen=now, extraction_version=1)
    session.add(m2)
    session.commit()

    c = by_check(session)
    assert c["documents_shared_by_restaurants"]["count"] == 1
    assert c["urls_shared_by_restaurants"]["count"] == 1
    assert c["urls_shared_by_restaurants"]["examples"] == "r.example (2)"
    assert c["websites_shared_by_restaurants"]["examples"] == "timeout.com (2)"
    assert c["websites_on_aggregator_hosts"]["count"] == 2
    assert c["menus_with_no_ingredients"]["count"] == 1  # m2: extracted, no dishes
    assert c["menus_with_no_ingredients"]["examples"] == "r2 (1)"
    assert c["duplicate_menus"]["count"] == 0
    assert "|---|" in quality.markdown(list(c.values()))

    m2.excluded_reason = "search_rejected"
    session.commit()
    assert by_check(session)["documents_shared_by_restaurants"]["count"] == 0

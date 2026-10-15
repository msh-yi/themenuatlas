"""Bucket handoff: pending → extracted → import, against a fake bucket and local Postgres."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from menudb import handoff
from menudb.db import make_engine
from menudb.db.models import (
    Component, Course, Ingredient, IngredientAlias, Menu, Restaurant, ReviewItem, Snapshot, Source,
)
from menudb.extract.schema import ExtractedMenu, PendingItem
from menudb.normalize import content_hash


@pytest.fixture
def bucket(monkeypatch):
    blobs: dict[str, bytes] = {}
    s = handoff.storage
    monkeypatch.setattr(s, "put_bytes", lambda k, data, ct: blobs.__setitem__(k, data))
    monkeypatch.setattr(s, "get_raw", lambda k: blobs[k])
    monkeypatch.setattr(s, "exists", lambda k, bucket=None: k in blobs)
    monkeypatch.setattr(s, "delete", lambda k: blobs.pop(k, None))
    monkeypatch.setattr(s, "list_keys", lambda p: sorted(k for k in blobs if k.startswith(p)))
    monkeypatch.setattr(s, "move", lambda a, b: blobs.__setitem__(b, blobs.pop(a)))
    monkeypatch.setattr(s, "last_modified", lambda k: datetime(2026, 9, 1, tzinfo=timezone.utc))

    def put_raw(h, data, ct):
        blobs.setdefault(s.key_for(h), data)
        return s.key_for(h)
    monkeypatch.setattr(s, "put_raw", put_raw)
    return blobs


MENU_HTML = b"<html><body><main><h1>Autumn</h1><p>Langoustine, brown butter</p></main></body></html>"


def extraction(item: PendingItem, **kw) -> ExtractedMenu:
    return ExtractedMenu.model_validate({
        "slug": item.slug, "normalized_hash": item.normalized_hash, "menu_id": item.menu_id,
        "extractor": "test", "language": "en", "menu_type": "tasting",
        "courses": [{"position": 1, "raw_text": "Langoustine, brown butter", "kind": "course",
                     "components": [
                         {"raw_term": "Langoustine", "name": "Langoustine", "role": "main"},
                         {"raw_term": "brown butter", "name": "butter", "role": "sauce",
                          "preparations": ["Browned"]},
                     ]}],
        **kw,
    })


def test_item_from_bytes_matches_check_identity(bucket):
    from menudb.extract.text import document_text
    from menudb.normalize import normalized_hash

    item = handoff.item_from_bytes("r", "main", "https://r.example/menu", MENU_HTML, "text/html")
    assert item.kind == "html" and item.text
    assert item.normalized_hash == normalized_hash(document_text("html", MENU_HTML))
    assert bucket[item.storage_key] == MENU_HTML


def test_queue_items_skips_known(bucket):
    item = handoff.item_from_bytes("r", "main", "https://r.example/menu", MENU_HTML, "text/html")
    assert handoff.queue_items([item])["queued"] == 1
    assert handoff.queue_items([item]) == {"already_queued": 1}
    k = handoff.key(handoff.PENDING, "r", item.normalized_hash)
    handoff.storage.move(k, k.replace("pending/", "imported/"))
    assert handoff.queue_items([item]) == {"already_queued": 1}


def test_schema_normalizes_names_and_preparations():
    item = PendingItem(slug="r", normalized_hash="h", kind="html", url="u", storage_key="k")
    comp = extraction(item).courses[0].components[1]
    assert comp.name == "butter" and comp.preparations == ["browned"]


# --- with Postgres ---------------------------------------------------------------

@pytest.fixture
def session(pg_url):
    engine = make_engine(pg_url)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
        s.rollback()
        for model in (ReviewItem, Component, Course, IngredientAlias, Ingredient, Menu, Snapshot,
                      Source, Restaurant):
            s.query(model).delete()
        s.commit()
    engine.dispose()


def make_menu(session, bucket, data=MENU_HTML):
    from menudb.extract.text import document_text
    from menudb.normalize import normalized_hash

    r = Restaurant(slug="r", name="R")
    session.add(r)
    session.flush()
    src = Source(restaurant_id=r.id, label="main", recipe={"start": "https://r.example/menu"},
                 validators={})
    session.add(src)
    session.flush()
    h = content_hash(data)
    key = handoff.storage.put_raw(h, data, "text/html")
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    snap = Snapshot(source_id=src.id, fetched_at=now, origin="live", url="https://r.example/menu",
                    content_type="html", content_hash=h,
                    normalized_hash=normalized_hash(document_text("html", data)), storage_key=key)
    session.add(snap)
    session.flush()
    menu = Menu(restaurant_id=r.id, normalized_hash=snap.normalized_hash,
                first_snapshot_id=snap.id, first_seen=now, last_seen=now)
    session.add(menu)
    session.commit()
    return menu


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.db
def test_round_trip(session, bucket):
    menu = make_menu(session, bucket)

    assert handoff.pending_from_db(session) == {"queued": 1}
    assert handoff.pending_from_db(session) == {"already_queued": 1}
    [pk] = handoff.storage.list_keys(handoff.PENDING)
    item = PendingItem.model_validate(handoff.get_json(pk))
    assert item.menu_id == menu.id and item.text

    # the session's side: write extracted/, drop pending/
    handoff.put_json(handoff.key(handoff.EXTRACTED, item.slug, item.normalized_hash),
                     extraction(item))
    handoff.storage.delete(pk)

    assert handoff.import_from_bucket(session) == {"imported": 1}
    assert handoff.storage.list_keys(handoff.EXTRACTED) == []
    assert len(handoff.storage.list_keys(handoff.IMPORTED)) == 1
    session.refresh(menu)
    assert menu.extraction_version == 1 and menu.menu_type == "tasting"
    assert count(session, Course) == 1 and count(session, Component) == 2
    assert session.scalars(select(Ingredient.canonical_name).order_by(Ingredient.canonical_name)
                           ).all() == ["butter", "langoustine"]
    # auto-accepted terms are queued for audit
    assert set(session.scalars(select(ReviewItem.status))) == {"auto"}
    # extracted menus are no longer pending
    assert handoff.pending_from_db(session) == {}


@pytest.mark.db
def test_reimport_replaces_and_reuses_aliases(session, bucket):
    menu = make_menu(session, bucket)
    item = PendingItem(slug="r", normalized_hash=menu.normalized_hash, kind="html", url="u",
                       storage_key="k")
    assert handoff.import_menu(session, extraction(item)) == "imported"
    session.commit()
    assert handoff.import_menu(session, extraction(item)) == "imported"
    session.commit()
    assert count(session, Course) == 1 and count(session, Component) == 2
    assert count(session, Ingredient) == 2
    # "brown butter" now resolves through its alias
    alias = session.scalar(select(IngredientAlias).where(IngredientAlias.alias == "brown butter"))
    assert alias is not None
    assert None not in session.scalars(select(Component.ingredient_id)).all()


@pytest.mark.db
def test_import_waits_for_check(session, bucket):
    item = handoff.item_from_bytes("r", "main", "https://r.example/menu", MENU_HTML, "text/html")
    session.add(Restaurant(slug="r", name="R"))
    session.commit()
    handoff.put_json(handoff.key(handoff.EXTRACTED, "r", item.normalized_hash), extraction(item))
    assert handoff.import_from_bucket(session) == {"waiting": 1}
    assert len(handoff.storage.list_keys(handoff.EXTRACTED)) == 1


@pytest.mark.db
def test_invalid_extraction_is_skipped(session, bucket):
    bucket["extracted/r/x.json"] = b'{"slug": "r"}'
    assert handoff.import_from_bucket(session) == {"invalid": 1}


@pytest.mark.db
def test_unmatched_term_without_suggestion_goes_to_review(session, bucket):
    menu = make_menu(session, bucket)
    item = PendingItem(slug="r", normalized_hash=menu.normalized_hash, kind="html", url="u",
                       storage_key="k")
    ex = extraction(item)
    ex.courses[0].components[0].name = None
    handoff.import_menu(session, ex)
    session.commit()
    review = session.scalar(select(ReviewItem).where(ReviewItem.raw_term == "Langoustine"))
    assert review.status == "open"
    comp = session.scalar(select(Component).where(Component.raw_term == "Langoustine"))
    assert comp.ingredient_id is None


@pytest.mark.db
def test_import_records_bucket_only_menu(session, bucket):
    """recheck.yml/discover documents have no menu row: import creates one from processed/."""
    item = handoff.item_from_bytes("r", "main", "https://r.example/menu", MENU_HTML, "text/html")
    handoff.put_json(handoff.key(handoff.PROCESSED, "r", item.normalized_hash), item)
    session.add(Restaurant(slug="r", name="R"))
    session.commit()
    handoff.put_json(handoff.key(handoff.EXTRACTED, "r", item.normalized_hash), extraction(item))
    assert handoff.import_from_bucket(session) == {"imported": 1}
    src = session.scalar(select(Source))
    assert src.mode == "bucket" and src.label == "main"
    menu = session.scalar(select(Menu))
    assert menu.normalized_hash == item.normalized_hash and menu.menu_type == "tasting"
    snap = session.get(Snapshot, menu.first_snapshot_id)
    assert snap.storage_key == item.storage_key and snap.content_hash == content_hash(MENU_HTML)
    assert menu.first_seen == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert count(session, Component) == 2

    # `check` leaves bucket sources to recheck.yml
    from menudb.pipeline import due_sources
    assert list(due_sources(session, datetime.now(timezone.utc))) == []


@pytest.mark.db
def test_stats_counts(session, bucket):
    from menudb.stats import row_counts

    menu = make_menu(session, bucket)
    item = PendingItem(slug="r", normalized_hash=menu.normalized_hash, kind="html", url="u",
                       storage_key="k")
    handoff.import_menu(session, extraction(item))
    session.commit()
    c = row_counts(session)
    assert c["restaurants"] == c["menus"] == c["menus_extracted"] == c["sources_check"] == 1
    assert c["courses"] == 1 and c["components"] == 2 and c["ingredients"] == 2


@pytest.mark.db
def test_exclude_flags_quarantines_and_undoes(session, bucket):
    from menudb.exclude import exclude

    menu = make_menu(session, bucket)
    session.get(Restaurant, menu.restaurant_id).website = "https://wrong.example/"
    session.commit()
    handoff.pending_from_db(session)
    rejected = [{"slug": "r", "website": "https://wrong.example/"}]

    dry = exclude(session, rejected, "search_rejected")
    assert dry["menus"] == 1 and dry["bucket_pending"] == 1 and "applied" not in dry
    assert menu.excluded_reason is None

    exclude(session, rejected, "search_rejected", apply=True)
    session.refresh(menu)
    assert menu.excluded_reason == "search_rejected"
    assert session.get(Restaurant, menu.restaurant_id).website is None
    assert handoff.storage.list_keys(handoff.PENDING) == []
    assert len(handoff.storage.list_keys("quarantine/pending/r/")) == 1
    assert handoff.pending_from_db(session) == {}  # excluded menus are not re-queued

    exclude(session, rejected, "search_rejected", apply=True, undo=True)
    session.refresh(menu)
    assert menu.excluded_reason is None
    assert session.get(Restaurant, menu.restaurant_id).website == "https://wrong.example/"
    assert len(handoff.storage.list_keys(handoff.PENDING)) == 1

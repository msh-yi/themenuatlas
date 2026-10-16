"""menudb review: decisions change aliases, ingredients and components."""

import json

import pytest
from sqlalchemy import select

from menudb import handoff, review
from menudb.db.models import Component, Ingredient, IngredientAlias, ReviewItem
from menudb.extract.schema import PendingItem
from test_handoff import bucket, extraction, make_menu, session  # noqa: F401 (fixtures)

pytestmark = pytest.mark.db


@pytest.fixture
def imported(session, bucket):  # noqa: F811
    menu = make_menu(session, bucket)
    item = PendingItem(slug="r", normalized_hash=menu.normalized_hash, kind="html", url="u",
                       storage_key="k")
    ex = extraction(item)
    ex.courses[0].components[0].name = None  # "Langoustine" waits as open
    handoff.import_menu(session, ex)
    session.commit()
    return menu


def ingredient_of(session, term):
    return session.scalar(select(Ingredient.canonical_name).join(Component)
                          .where(Component.raw_term == term))


def test_export_lists_queue(session, imported):
    rows = {r["raw_term"]: r for r in review.export_queue(session)}
    assert rows["Langoustine"]["status"] == "open" and rows["Langoustine"]["current"] is None
    assert rows["brown butter"]["status"] == "auto" and rows["brown butter"]["current"] == "butter"


def test_map_creates_alias_relinks_and_drops_orphans(session, imported):
    counts = review.apply_all(session, [
        review.Decision(raw_term="Langoustine", language="en", action="map",
                        canonical="Langoustine", category="seafood"),
        review.Decision(raw_term="brown butter", language="en", action="map",
                        canonical="brown butter", parent="butter", category="dairy"),
    ])
    session.commit()
    assert counts["mapped"] == 2
    assert ingredient_of(session, "Langoustine") == "langoustine"
    assert ingredient_of(session, "brown butter") == "brown butter"
    bb = session.scalar(select(Ingredient).where(Ingredient.canonical_name == "brown butter"))
    assert session.get(Ingredient, bb.parent_id).canonical_name == "butter"  # kept: it's a parent
    assert set(session.scalars(select(ReviewItem.status))) == {"mapped"}


def test_map_away_from_auto_ingredient_removes_it(session, imported):
    counts = review.apply_all(session, [review.Decision(
        raw_term="brown butter", language="en", action="map", canonical="beurre noisette")])
    session.commit()
    assert counts["ingredients_removed"] == 1
    assert session.scalar(select(Ingredient).where(Ingredient.canonical_name == "butter")) is None


def test_ignore_unlinks(session, imported):
    review.apply_all(session, [review.Decision(raw_term="brown butter", language="en",
                                               action="ignore")])
    session.commit()
    assert ingredient_of(session, "brown butter") is None
    assert session.scalar(select(IngredientAlias).where(IngredientAlias.alias == "brown butter")) is None


def test_accept_confirms(session, imported):
    counts = review.apply_all(session, [
        review.Decision(raw_term="brown butter", language="en", action="accept"),
        review.Decision(raw_term="Langoustine", language="en", action="accept"),  # nothing to accept
    ])
    assert counts == {"accepted": 1, "skipped": 1}
    assert ingredient_of(session, "brown butter") == "butter"


def test_bucket_round_trip(session, imported, bucket):  # noqa: F811
    assert review.export_to_bucket(session) == {"open": 1, "auto": 1}
    assert {r["raw_term"] for r in json.loads(bucket[review.QUEUE_KEY])} == {"Langoustine", "brown butter"}
    bucket["review/decisions/a.json"] = json.dumps([
        {"raw_term": "Langoustine", "language": "en", "action": "map", "canonical": "langoustine"},
    ]).encode()
    bucket["review/decisions/b.json"] = b"not json"
    counts = review.apply_from_bucket(session)
    assert counts["mapped"] == 1 and counts["invalid_files"] == 1 and counts["files"] == 1
    assert "review/applied/a.json" in bucket and "review/decisions/b.json" in bucket
    assert ingredient_of(session, "Langoustine") == "langoustine"


def test_consolidate_merges_variant_ingredients(session, bucket):  # noqa: F811
    from menudb import ingredients
    from menudb.db.models import Course

    menu = make_menu(session, bucket)
    beef, ribeye, kept = (Ingredient(canonical_name=n) for n in ("beef", "beef ribeye", "beef tartare stuff"))
    nfd = Ingredient(canonical_name="lingot  bean")
    session.add_all([beef, ribeye, kept, nfd])
    session.flush()
    session.add_all([IngredientAlias(alias="ribeye", language="en", ingredient_id=ribeye.id),
                     IngredientAlias(alias="tartare", language="en", ingredient_id=kept.id),
                     ReviewItem(raw_term="tartare", language="en", status="mapped", occurrences=1)])
    course = Course(menu_id=menu.id, position=1, raw_text="x")
    course.components.append(Component(raw_term="ribeye", ingredient_id=ribeye.id))
    session.add(course)
    session.commit()

    assert ingredients.consolidate(session) == 1
    session.commit()
    names = set(session.scalars(select(Ingredient.canonical_name)))
    assert names == {"beef", "beef tartare stuff", "lingot bean"}  # human choice kept, spacing fixed
    assert ingredient_of(session, "ribeye") == "beef"
    alias = session.scalar(select(IngredientAlias).where(IngredientAlias.alias == "ribeye"))
    assert alias.ingredient_id == beef.id

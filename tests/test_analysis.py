"""Analysis statistics on hand-made rows, the synthetic fixture and a local Postgres."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from menudb import analysis as a
from menudb.db import make_engine
from menudb.db.models import Component, Course, Ingredient, Menu, Restaurant, Snapshot, Source

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_menus.json"


def row(slug, menu, course, ingredient, lat=50.0, seen=date(2026, 4, 1)):
    return a.Row(slug, "X", lat, menu, seen, course, ingredient)


def test_each_restaurant_weighs_the_same():
    # "big" publishes three menus with uni; "small" one menu without it
    rows = [row("big", f"m{i}", 1, "uni") for i in range(3)] + [row("small", "s", 1, "kelp")]
    shares = {d["ingredient"]: d["share"] for d in a.ingredient_frequency(rows)}
    assert shares == {"uni": 0.5, "kelp": 0.5}


def test_pairs_need_several_restaurants():
    rows = [row("r1", "m", 1, "beet"), row("r1", "m", 1, "dill"),
            row("r2", "m", 1, "beet"), row("r2", "m", 1, "dill"),
            row("r3", "m", 1, "beet"), row("r3", "m", 1, "kelp")]
    pairs = a.pair_stats(rows, min_restaurants=2)
    assert [(p["a"], p["b"], p["restaurants"]) for p in pairs] == [("beet", "dill", 2)]
    # P(beet,dill)=2/3, P(beet)=1, P(dill)=2/3 -> lift 1
    assert pairs[0]["lift"] == pytest.approx(1.0)


def test_score_prefers_common_pairs_over_rare_extreme_lift():
    rows = []
    # eel+mitsuba: two restaurants, once each, among many other courses -> huge raw lift
    for r in ("r1", "r2"):
        rows += [row(r, "m", 1, "eel"), row(r, "m", 1, "mitsuba")]
    # tomato+basil: ten restaurants, every time
    for i in range(10):
        rows += [row(f"t{i}", "m", 1, "tomato"), row(f"t{i}", "m", 1, "basil")]
    for r in [f"t{i}" for i in range(10)] + ["r1", "r2"]:
        rows += [row(r, "m", c, f"filler{c}") for c in range(2, 12)]
    pairs = {(p["a"], p["b"]): p for p in a.pair_stats(rows)}
    rare, common = pairs[("eel", "mitsuba")], pairs[("basil", "tomato")]
    assert rare["lift"] > common["lift"]
    assert common["score"] > rare["score"] > 1
    assert -1 <= rare["npmi"] <= 1 and common["npmi"] > 0


def test_synthetic_planted_patterns():
    rows, restaurants = a.load_synthetic(FIXTURE)
    top = {(p["a"], p["b"]) for p in a.pair_stats(rows)[:3]}
    assert top == {("rice", "uni"), ("brown butter", "scallop"), ("beetroot", "horseradish")}

    spring = {d["ingredient"]: d["share"] for d in a.ingredient_frequency(rows, by="season")
              if d["group"] == "spring"}
    assert spring["asparagus"] == 1.0  # April north and October south are both spring

    cov = {c["region"]: c for c in a.coverage(restaurants, rows)}
    assert cov["US"] == {"region": "US", "restaurants": 1, "publishes_menu": 0, "with_data": 0}


@pytest.mark.db
def test_load_db(pg_url):
    engine = make_engine(pg_url)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with sessionmaker(engine)() as s:
        try:
            r = Restaurant(slug="an-test", name="T", country="AU", lat=-37.8, publishes_menu="yes")
            s.add(r)
            s.flush()
            src = Source(restaurant_id=r.id, recipe={"start": "https://r.example/"}, validators={})
            s.add(src)
            s.flush()
            snap = Snapshot(source_id=src.id, fetched_at=now, origin="live", url="u",
                            content_type="html", content_hash="h", normalized_hash="n",
                            storage_key="k")
            s.add(snap)
            s.flush()
            menu = Menu(restaurant_id=r.id, normalized_hash="n", first_snapshot_id=snap.id,
                        first_seen=now, last_seen=now, menu_type="tasting")
            s.add(menu)
            s.flush()
            ing = Ingredient(canonical_name="marron")
            s.add(ing)
            s.flush()
            c = Course(menu_id=menu.id, position=1, raw_text="private")
            s.add(c)
            s.flush()
            s.add_all([Component(course_id=c.id, raw_term="Marron", ingredient_id=ing.id,
                                     preparations=["grilled"]),
                       Component(course_id=c.id, raw_term="Finger Lime", suggested_name=None)])
            s.flush()
            rows, restaurants = a.load_db(s)
            mine = [x for x in rows if x.restaurant == "an-test"]
            assert {x.ingredient for x in mine} == {"marron", "finger lime"}
            assert mine[0].region == "Australia & NZ" and mine[0].season == "spring"
            assert restaurants["an-test"].publishes_menu == "yes"
            from menudb import site  # the site export reads preparations the same way
            assert site.preparations_from_db(s)["marron"]["grilled"] >= 1
        finally:
            s.rollback()
    engine.dispose()

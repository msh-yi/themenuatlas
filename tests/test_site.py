"""menudb export-site: aggregated JSON for the static site, built from the synthetic fixture."""

import json
from datetime import datetime, timezone
from pathlib import Path

from menudb import site

ROOT = Path(__file__).parents[1]


def build():
    rows, restaurants, details, preps = site.load("synthetic", ROOT)
    return site.build(rows, restaurants, source="synthetic", details=details, preparations=preps,
                      now=datetime(2026, 10, 15, tzinfo=timezone.utc))


def test_bundle_shape_and_totals():
    d = build()
    assert d["schema"] == site.SCHEMA and d["source"] == "synthetic"
    assert d["totals"]["restaurants"] == 9 and d["totals"]["restaurants_with_data"] == 8
    assert d["totals"]["menus"] == 16
    assert {"Japan", "Nordic", "US"} <= set(d["groups"]["region"])
    names = {i["name"] for i in d["ingredients"]}
    assert all(p["a"] in names and p["b"] in names for p in d["pairs"])
    assert all(p["restaurants"] >= d["min_restaurants"] for p in d["pairs"])


def test_planted_patterns_survive():
    d = build()
    ing = {i["name"]: i for i in d["ingredients"]}
    assert set(ing["uni"]["by_region"]) == {"Japan"}
    assert ing["uni"]["restaurants"] == ["syn-japan-a", "syn-japan-b"]
    assert list(ing["beetroot"]["preparations"][0]) == ["roasted", 7]
    top = {(p["a"], p["b"]) for p in d["pairs"][:3]}
    assert top == {("rice", "uni"), ("brown butter", "scallop"), ("beetroot", "horseradish")}
    r = {x["slug"]: x for x in d["restaurants"]}
    assert r["syn-no-menu"]["menus"] == 0 and r["syn-japan-a"]["name"] == "Synthetic Japan A"


def test_no_course_text_or_course_grouping(tmp_path):
    d = build()
    path = site.write(d, tmp_path)
    text = path.read_text()
    # the fixture's raw_text is "synthetic"; course text and positions never leave
    assert "raw_text" not in text and '"position"' not in text and '"courses":[' not in text
    assert json.loads(text)["totals"] == d["totals"]


def test_city_level_coordinates_are_flagged(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("slug,name,city,country,lat,lon,website,publishes_menu,lists,coord_precision\n"
                    "a,A,Oslo,Norway,59.9,10.7,https://a.example/,true,,city\n"
                    "b,B,Oslo,Norway,59.9,10.7,https://b.example/,true,,\n")
    details = site.restaurant_details(path)
    assert details["a"]["approx"] is True and details["b"]["approx"] is False
    assert details["a"]["lon"] == 10.7


def test_full_list_joins_the_map_without_menus(tmp_path):
    from menudb import analysis as a

    full = tmp_path / "full.csv"
    full.write_text("slug,name,city,country,lat,lon,website,publishes_menu,lists,coord_precision\n"
                    "syn-uk,Synthetic UK,London,GB,51.5,-0.13,,true,,\n"
                    "new-one,New One,Tbilisi,Georgia,41.7,44.8,,,,city\n")
    rows, restaurants, details, preps = site.load("synthetic", ROOT)
    site.add_full_list(restaurants, details, full)
    assert restaurants["new-one"] == a.Restaurant("new-one", "Other", 41.7, "unknown")
    d = site.build(rows, restaurants, source="synthetic", details=details, preparations=preps)
    r = {x["slug"]: x for x in d["restaurants"]}
    assert r["new-one"]["menus"] == 0 and r["new-one"]["approx"] is True and r["new-one"]["lon"] == 44.8
    assert r["syn-uk"]["menus"] == 2  # already in the analysis set: unchanged
    assert d["totals"]["restaurants"] == 10

"""menudb export-site: aggregated JSON for the static site, built from the synthetic fixture."""

import json
from datetime import datetime, timezone
from pathlib import Path

from menudb import site

ROOT = Path(__file__).parents[1]


def build():
    rows, restaurants, details, preps, _ = site.load("synthetic", ROOT)
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
    paths = site.write(d, tmp_path)
    text = "".join(p.read_text() for p in paths)
    # the fixture's raw_text is "synthetic"; course text and positions never leave
    assert "raw_text" not in text and '"position"' not in text and '"courses":[' not in text
    assert json.loads(paths[0].read_text())["totals"] == d["totals"]


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
    rows, restaurants, details, preps, _ = site.load("synthetic", ROOT)
    site.add_full_list(restaurants, details, full)
    assert restaurants["new-one"] == a.Restaurant("new-one", "Other", 41.7, "unknown")
    d = site.build(rows, restaurants, source="synthetic", details=details, preparations=preps)
    r = {x["slug"]: x for x in d["restaurants"]}
    assert r["new-one"]["menus"] == 0 and r["new-one"]["approx"] is True and r["new-one"]["lon"] == 44.8
    assert r["syn-uk"]["menus"] == 2  # already in the analysis set: unchanged
    assert d["totals"]["restaurants"] == 10


def test_discovery_fills_in_publishing_and_blocking(tmp_path):
    from menudb import analysis as a

    disc = tmp_path / "discovery.csv"
    disc.write_text("slug,status,score,kind,menu_url,stored,note,checked\n"
                    "x,menu_found,9,html,,,,\ny,robots,0,,,,,\nz,menu_maybe,3,,,,,\n")
    restaurants = {s: a.Restaurant(s, "Other", 1.0, "unknown") for s in "xyz"}
    details: dict = {}
    site.apply_discovery(restaurants, details, disc)
    assert restaurants["x"].publishes_menu == "yes"
    assert restaurants["z"].publishes_menu == "unknown"  # unclear stays unknown
    assert details == {"y": {"blocked": True}}


def test_lexicon_info_gives_category_and_other_names(tmp_path):
    lex = tmp_path / "ingredients.txt"
    lex.write_text("# comment\n## vegetable\naubergine = en: eggplant | es: berenjena | ja: ナス\n"
                   "white asparagus > asparagus = fr: asperge blanche\n## herb\nbasil\n")
    info = site.lexicon_info(lex)
    assert info["aubergine"] == {"category": "vegetable", "parent": None, "aka": ["eggplant", "berenjena", "ナス"]}
    assert info["white asparagus"]["parent"] == "asparagus"
    assert info["basil"] == {"category": "herb", "parent": None, "aka": []}


def test_accolades_parse_latest_year():
    a = site.accolades("michelin:2026:3;michelin:2020:2;michelin:2026:1;w50b:2023:40;w50b:2025:83;"
                       "nyt:2026:america50;nytnyc:2024:14;nytnyc:2026:48")
    assert a == {"stars": 3, "stars_year": 2026, "w50b": 83, "w50b_year": 2025, "nyt50": 2026,
                 "nytnyc": 48, "nytnyc_year": 2026}
    assert site.accolades("michelin:2017:1;michelin:2026:2")["stars"] == 2
    assert site.accolades("") == {} and site.accolade_groups({}) == []
    assert site.accolade_groups(a) == ["3 stars", "World's 50 Best", "NYT America 50", "NYT NYC 100"]


def test_accolade_groups_need_enough_restaurants():
    rows, restaurants, details, preps, _ = site.load("synthetic", ROOT)
    d = site.build(rows, restaurants, source="synthetic", details=details, min_group=3)
    assert d["groups"]["accolade_with_data"] == {"3 stars": 3, "World's 50 Best": 4}
    r = {x["slug"]: x for x in d["restaurants"]}
    assert r["syn-nordic-a"]["tiers"] == ["3 stars", "World's 50 Best"]
    assert r["syn-no-menu"]["tiers"] == ["NYT NYC 100"]  # listed, but no menus: no group stats
    uni = next(i for i in d["ingredients"] if i["name"] == "uni")
    assert set(uni["by_accolade"]) <= {"3 stars", "World's 50 Best"}
    assert set(d["pairs_by_accolade"]) == {"3 stars", "World's 50 Best"}
    assert not build()["groups"]["accolade_with_data"]  # the default threshold is 10


def test_menus_only_when_asked():
    rows, restaurants, details, preps, manifests = site.load("synthetic", ROOT)
    assert not any("menu_list" in r for r in build()["restaurants"]) and not build()["has_menus"]
    d = site.build(rows, restaurants, source="synthetic", details=details, manifests=manifests)
    r = {x["slug"]: x for x in d["restaurants"]}
    ms = r["syn-japan-a"]["menu_list"]
    assert d["has_menus"] and len(ms) == 2 and "syn-no-menu" not in manifests
    assert ms[0]["first_seen"] and ms[0]["courses"][0]["k"]
    names = {i["name"] for i in d["ingredients"]}
    for m in ms:
        for c in m["courses"]:
            for name, preps in c["i"]:
                assert name in names and isinstance(preps, list)
    assert "raw_text" not in json.dumps(d)


def test_split_files_round_trip(tmp_path):
    rows, restaurants, details, preps, manifests = site.load("synthetic", ROOT)
    d = site.build(rows, restaurants, source="synthetic", details=details, preparations=preps,
                   manifests=manifests, min_group=2)
    paths = site.write(d, tmp_path)
    assert paths[0].name == "data.json" and len(paths) == 2 + site.MENU_SHARDS
    core = json.loads(paths[0].read_text())
    assert "pairs" not in core and core["layout"] == "split"
    assert not any("menu_list" in r for r in core["restaurants"])
    slugs = [r["slug"] for r in core["restaurants"]]
    uni = next(i for i in core["ingredients"] if i["name"] == "uni")
    assert [slugs[i] for i in uni["restaurants"]] == ["syn-japan-a", "syn-japan-b"]
    no_menu = next(r for r in core["restaurants"] if r["slug"] == "syn-no-menu")
    assert "menus" not in no_menu and "approx" not in no_menu  # defaults dropped
    pairs = json.loads((tmp_path / "pairs.json").read_text())
    assert pairs["pairs"] == d["pairs"] and pairs["pairs_by_accolade"] == d["pairs_by_accolade"]
    shard = json.loads((tmp_path / f"menus/{site.shard('syn-japan-a')}.json").read_text())
    full = {r["slug"]: r for r in d["restaurants"]}
    assert shard["syn-japan-a"] == full["syn-japan-a"]["menu_list"]
    assert site.shard("syn-japan-a") == "62" and site.shard("jordnær") == "39"  # same as site/app.js


def test_restaurants_carry_first_and_last_seen():
    rows, restaurants, details, preps, manifests = site.load("synthetic", ROOT)
    d = site.build(rows, restaurants, source="synthetic", details=details)
    with_menus = [r for r in d["restaurants"] if r["menus"]]
    assert with_menus and all(r["first_seen"] <= r["last_seen"] for r in with_menus)
    assert all(r["first_seen"] is None for r in d["restaurants"] if not r["menus"])
    first = {m["first_seen"] for ms in manifests.values() for m in ms}
    assert min(r["first_seen"] for r in with_menus) == min(first)


def test_add_windows_and_bucket_seen(monkeypatch):
    from datetime import datetime, timezone

    details = {"a": {"name": "A"}}
    site.add_windows(details, [("a", "2026-03-01", "2026-05-01"), ("a", "2026-01-10", None),
                               ("b", "2026-02-02", "2026-02-02")])
    assert details["a"] == {"name": "A", "first_seen": "2026-01-10", "last_seen": "2026-05-01"}
    assert details["b"] == {"first_seen": "2026-02-02", "last_seen": "2026-02-02"}

    from menudb import storage
    t = lambda m: datetime(2026, m, 1, tzinfo=timezone.utc)  # noqa: E731
    objs = {"pending/": {"pending/x/h1.json": t(9)}, "processed/": {"processed/x/h1.json": t(3),
                                                                    "processed/y/h2.json": t(4)}}
    monkeypatch.setattr(storage, "list_modified", lambda p: objs[p])
    assert site.bucket_seen() == {"h1": ("2026-03-01", "2026-09-01"), "h2": ("2026-04-01", "2026-04-01")}


def test_optouts_leave_no_trace(tmp_path):
    opt = tmp_path / "optouts.csv"
    opt.write_text("slug,date,note\nsyn-uk,2026-10-16,asked by email\n\n")
    assert site.optouts(opt) == {"syn-uk"} and site.optouts(tmp_path / "missing.csv") == frozenset()
    rows, restaurants, details, preps, manifests = site.drop_restaurants(site.load("synthetic", ROOT), site.optouts(opt))
    assert all(r.restaurant != "syn-uk" for r in rows)
    assert "syn-uk" not in restaurants and "syn-uk" not in details and "syn-uk" not in manifests
    d = site.build(rows, restaurants, source="synthetic", details=details, preparations=preps, manifests=manifests)
    assert "syn-uk" not in json.dumps(d) and "Synthetic UK" not in json.dumps(d)
    assert d["totals"]["restaurants"] == 8


def test_lexicon_info_gives_one_chip_per_spelling(tmp_path):
    lex = tmp_path / "ingredients.txt"
    lex.write_text("## meat\nbeef = en: wagyu, wagyū, Wagyu, ribeye, rib eye, rib-eye | fr: bœuf\n",
                   encoding="utf-8")
    assert site.lexicon_info(lex)["beef"]["aka"] == ["wagyu", "ribeye", "bœuf"]

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from apply_website_suggestion import Rejected, apply, parse_form  # noqa: E402

FIELDS = ["slug", "name", "city", "country", "lat", "lon", "website", "publishes_menu", "lists",
          "list_sources", "website_source", "coord_precision"]


def body(restaurant: str, link: str) -> str:
    return (f"### Restaurant\n\n{restaurant}\n\n### Link\n\n{link}\n\n### You are\n\n_No response_\n\n"
            "### Anything else\n\n_No response_\n")


@pytest.fixture
def lists(tmp_path):
    full, curated = tmp_path / "full.csv", tmp_path / "curated.csv"
    with full.open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerow({"slug": "sushi-a", "name": "Sushi A", "city": "Tokyo", "website": "", "website_source": ""})
        w.writerow({"slug": "twin-1", "name": "Twin", "website": ""})
        w.writerow({"slug": "twin-2", "name": "Twin", "website": ""})
    curated.write_text("slug,name,city,country,lat,lon,website,publishes_menu,lists\nsushi-a,Sushi A,Tokyo,JP,,,,,\n")
    return [full, curated]


def test_parse_form():
    form = parse_form(body("sushi-a", "https://sushi-a.jp/"))
    assert form == {"restaurant": "sushi-a", "link": "https://sushi-a.jp/", "you are": "", "anything else": ""}


def test_apply_sets_the_website_in_both_lists(lists):
    assert apply(body("sushi-a", "sushi-a.jp/menu"), lists) == ("sushi-a", "https://sushi-a.jp/menu")
    full = {r["slug"]: r for r in csv.DictReader(lists[0].open())}
    assert full["sushi-a"]["website"] == "https://sushi-a.jp/menu"
    assert full["sushi-a"]["website_source"] == "suggested" and full["twin-1"]["website"] == ""
    assert next(csv.DictReader(lists[1].open()))["website"] == "https://sushi-a.jp/menu"


def test_apply_finds_the_restaurant_by_page_link_or_name(lists):
    assert apply(body("https://msh-yi.github.io/themenuatlas/#/restaurant/sushi-a", "https://a.jp"), lists)[0] == "sushi-a"
    assert apply(body("sushi a", "https://a.jp"), lists)[0] == "sushi-a"


@pytest.mark.parametrize("restaurant,link", [
    ("nobody", "https://a.jp"), ("Twin", "https://a.jp"),  # unknown, ambiguous
    ("sushi-a", "not a link"), ("sushi-a", "https://www.instagram.com/sushi_a"),
    ("sushi-a", "https://tabelog.com/tokyo/123"),
])
def test_apply_rejects(lists, restaurant, link):
    with pytest.raises(Rejected):
        apply(body(restaurant, link), lists)

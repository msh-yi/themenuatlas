"""Load data/seed/restaurants.csv and data/recipes/<slug>.yaml into the database."""

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from menudb.db.models import ListMembership, Restaurant, Source
from menudb.recipes import load_recipes

# restaurant.country is ISO 3166-1 alpha-2; the seed CSV uses names.
COUNTRY_CODES = {
    "Australia": "AU", "Denmark": "DK", "France": "FR", "Hong Kong": "HK", "Italy": "IT",
    "Japan": "JP", "Mexico": "MX", "Norway": "NO", "Peru": "PE", "Singapore": "SG",
    "South Korea": "KR", "Spain": "ES", "Sweden": "SE", "Thailand": "TH",
    "United Kingdom": "GB", "United States": "US",
}
_PUBLISHES = {"true": "yes", "false": "no", "yes": "yes", "no": "no", "": "unknown", "unknown": "unknown"}


def country_code(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if len(value) == 2 and value.isupper():
        return value
    try:
        return COUNTRY_CODES[value]
    except KeyError:
        raise ValueError(f"unknown country {value!r}: add it to COUNTRY_CODES") from None


def seed(session: Session, csv_path: Path, recipes_dir: Path) -> dict[str, int]:
    counts = {"restaurants": 0, "sources": 0}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            r = session.scalar(select(Restaurant).where(Restaurant.slug == row["slug"]))
            if r is None:
                r = Restaurant(slug=row["slug"])
                session.add(r)
            r.name, r.city = row["name"], row["city"] or None
            r.country = country_code(row["country"])
            r.lat = float(row["lat"]) if row["lat"] else None
            r.lon = float(row["lon"]) if row["lon"] else None
            r.website = row["website"] or None
            r.publishes_menu = _PUBLISHES[(row.get("publishes_menu") or "").strip().lower()]
            session.flush()
            counts["restaurants"] += 1

            for entry in filter(None, (row.get("lists") or "").split(";")):
                list_name, year, value = entry.split(":")
                m = session.scalar(select(ListMembership).where(
                    ListMembership.restaurant_id == r.id,
                    ListMembership.list_name == list_name, ListMembership.year == int(year)))
                if m is None:
                    session.add(ListMembership(restaurant_id=r.id, list_name=list_name,
                                               year=int(year), value=value))
                else:
                    m.value = value

            recipe_file = recipes_dir / f"{r.slug}.yaml"
            if recipe_file.exists():
                for label, parsed in load_recipes(recipe_file).items():
                    recipe = parsed.model_dump(mode="json")
                    src = session.scalar(select(Source).where(
                        Source.restaurant_id == r.id, Source.label == label))
                    if src is None:
                        session.add(Source(restaurant_id=r.id, label=label, recipe=recipe, validators={}))
                    elif src.recipe != recipe:
                        src.recipe, src.consecutive_failures, src.last_error = recipe, 0, None
                    counts["sources"] += 1
    return counts

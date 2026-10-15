"""Ingredient frequency and pairing statistics for notebooks/01_pairings.ipynb.

Works on flat `Row`s (one per ingredient per course), loaded from Postgres, from the
bucket's extracted/ objects (no database, so it runs in cloud sessions), or from the
synthetic fixture used by the tests. Only derived data comes out: ingredient names,
counts and scores, never course text.

Weighting follows docs/PLAN.md: each distinct menu counts once, and every restaurant
carries the same total weight, so a restaurant that publishes five menus doesn't
count five times as much as one that publishes one.
"""

import csv
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path

from menudb.seasons import season

# Regions by ISO country code. The first slice's regions, widened on 2026-10-15 for the
# full restaurant list (Germany, Benelux, Canada and others no longer fall into "Other").
REGIONS = {
    "DK": "Nordic", "NO": "Nordic", "SE": "Nordic", "FI": "Nordic", "IS": "Nordic",
    "FR": "France", "MC": "France", "ES": "Spain", "PT": "Portugal", "IT": "Italy",
    "GB": "UK & Ireland", "IE": "UK & Ireland",
    "DE": "Central Europe", "AT": "Central Europe", "CH": "Central Europe", "PL": "Central Europe",
    "CZ": "Central Europe", "HU": "Central Europe", "SI": "Central Europe", "SK": "Central Europe",
    "HR": "Central Europe", "BE": "Benelux", "NL": "Benelux", "LU": "Benelux",
    "US": "US", "CA": "Canada",
    "MX": "Latin America", "PE": "Latin America", "BR": "Latin America", "AR": "Latin America",
    "CL": "Latin America", "CO": "Latin America", "UY": "Latin America",
    "JP": "Japan",
    "KR": "East Asia", "HK": "East Asia", "MO": "East Asia", "CN": "East Asia", "TW": "East Asia",
    "SG": "Southeast Asia", "TH": "Southeast Asia", "VN": "Southeast Asia", "MY": "Southeast Asia",
    "ID": "Southeast Asia", "PH": "Southeast Asia",
    "AE": "Middle East", "QA": "Middle East", "SA": "Middle East", "LB": "Middle East",
    "IL": "Middle East", "TR": "Middle East",
    "AU": "Australia & NZ", "NZ": "Australia & NZ",
}


@dataclass(frozen=True)
class Row:
    restaurant: str  # slug
    region: str
    lat: float | None
    menu: str  # menu id or normalized hash
    seen: date | None  # first_seen; None when unknown (bucket source)
    course: int  # position within the menu
    ingredient: str

    @property
    def season(self) -> str:
        if self.lat is None or self.seen is None:
            return "unknown"
        return season(self.lat, self.seen)

    @property
    def hemisphere(self) -> str:
        if self.lat is None:
            return "unknown"
        return "south" if self.lat < 0 else "north"


@dataclass(frozen=True)
class Restaurant:
    slug: str
    region: str
    lat: float | None
    publishes_menu: str  # yes | no | unknown


# --- loading -------------------------------------------------------------------

def region_of(country: str | None) -> str:
    return REGIONS.get((country or "").upper(), "Other")


def restaurants_from_csv(path: Path) -> dict[str, Restaurant]:
    from menudb.seed import _PUBLISHES, country_code

    out = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            out[r["slug"]] = Restaurant(
                r["slug"], region_of(country_code(r["country"])),
                float(r["lat"]) if r["lat"] else None,
                _PUBLISHES[(r.get("publishes_menu") or "").strip().lower()])
    return out


def rows_from_extracted(menus: Iterable[dict], restaurants: dict[str, Restaurant],
                        seen: dict[str, date] | None = None) -> list[Row]:
    """Rows from ExtractedMenu-shaped dicts. `seen` maps normalized_hash to a date."""
    rows = []
    for m in menus:
        r = restaurants.get(m["slug"])
        if r is None or not m.get("is_menu", True):
            continue
        d = (seen or {}).get(m["normalized_hash"])
        for c in m.get("courses", []):
            names = {(comp.get("name") or comp["raw_term"]).strip().lower()
                     for comp in c.get("components", [])}
            rows += [Row(r.slug, r.region, r.lat, m["normalized_hash"], d, c["position"], n)
                     for n in sorted(names) if n]
    return rows


def load_synthetic(path: Path) -> tuple[list[Row], dict[str, Restaurant]]:
    data = json.loads(path.read_text())
    restaurants = {r["slug"]: Restaurant(r["slug"], region_of(r["country"]), r["lat"],
                                         r["publishes_menu"]) for r in data["restaurants"]}
    seen = {m["normalized_hash"]: date.fromisoformat(m["first_seen"]) for m in data["menus"]}
    return rows_from_extracted(data["menus"], restaurants, seen), restaurants


def load_bucket(csv_path: Path) -> tuple[list[Row], dict[str, Restaurant]]:
    """extracted/ and imported/ objects; no database. Dates are unknown here."""
    from menudb import handoff, storage

    restaurants = restaurants_from_csv(csv_path)
    menus = [handoff.get_json(k) for prefix in (handoff.EXTRACTED, handoff.IMPORTED)
             for k in storage.list_keys(prefix)]
    return rows_from_extracted(menus, restaurants), restaurants


def load_db(session) -> tuple[list[Row], dict[str, Restaurant]]:
    from sqlalchemy import func, or_, select

    from menudb.db.models import Component, Course, Ingredient, Menu
    from menudb.db.models import Restaurant as R

    restaurants = {r.slug: Restaurant(r.slug, region_of(r.country), r.lat, r.publishes_menu)
                   for r in session.scalars(select(R))}
    q = (
        select(R.slug, Menu.id, Menu.first_seen, Course.position,
               func.lower(func.coalesce(Ingredient.canonical_name, Component.suggested_name,
                                        Component.raw_term)))
        .join(Menu, Menu.restaurant_id == R.id)
        .join(Course, Course.menu_id == Menu.id)
        .join(Component, Component.course_id == Course.id)
        .outerjoin(Ingredient, Ingredient.id == Component.ingredient_id)
        .where(or_(Menu.menu_type.is_(None), Menu.menu_type != "not_a_menu"))
    )
    rows = {Row(slug, restaurants[slug].region, restaurants[slug].lat, str(menu_id),
                first_seen.date() if first_seen else None, pos, name.strip())
            for slug, menu_id, first_seen, pos, name in session.execute(q) if name}
    return sorted(rows, key=lambda r: (r.restaurant, r.menu, r.course, r.ingredient)), restaurants


# --- statistics ---------------------------------------------------------------

def _menu_sets(rows: Iterable[Row]) -> dict[str, dict[str, set[str]]]:
    """restaurant -> menu -> ingredients."""
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        out[r.restaurant][r.menu].add(r.ingredient)
    return out


def ingredient_frequency(rows: list[Row], by: str | None = None) -> list[dict]:
    """Share of restaurants' menus that use each ingredient, optionally per group.

    `by` is a Row attribute such as "region", "season" or "hemisphere". Within a group,
    each restaurant weighs 1, split evenly over its menus; `share` is the weighted
    fraction of menus containing the ingredient (0..1).
    """
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[getattr(r, by) if by else "all"].append(r)
    out = []
    for g, grows in sorted(groups.items()):
        sets = _menu_sets(grows)
        weight: dict[str, float] = defaultdict(float)
        rest: dict[str, set[str]] = defaultdict(set)
        for slug, menus in sets.items():
            for ingredients in menus.values():
                for i in ingredients:
                    weight[i] += 1 / len(menus)
                    rest[i].add(slug)
        n = len(sets)
        out += [{"group": g, "ingredient": i, "share": w / n, "restaurants": len(rest[i]),
                 "restaurants_in_group": n} for i, w in weight.items()]
    return sorted(out, key=lambda d: (d["group"], -d["share"], d["ingredient"]))


def pair_stats(rows: list[Row], min_restaurants: int = 2) -> list[dict]:
    """Course-level co-occurrence of ingredient pairs, scored by lift and PMI.

    Each course is a basket. Each restaurant weighs 1, split evenly over all its
    courses (across its distinct menus), so P(x) is the restaurant-weighted share of
    courses containing x. Pairs seen at fewer than `min_restaurants` restaurants are
    dropped: one chef's signature dish is not a pairing pattern.
    """
    courses: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for r in rows:
        courses[(r.restaurant, r.menu, r.course)].add(r.ingredient)
    per_restaurant: dict[str, int] = defaultdict(int)
    for slug, _, _ in courses:
        per_restaurant[slug] += 1
    total = len(per_restaurant)
    if not total:
        return []

    single: dict[str, float] = defaultdict(float)
    pair: dict[tuple[str, str], float] = defaultdict(float)
    pair_rest: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (slug, _, _), ingredients in courses.items():
        w = 1 / per_restaurant[slug]
        for i in ingredients:
            single[i] += w
        for a, b in combinations(sorted(ingredients), 2):
            pair[(a, b)] += w
            pair_rest[(a, b)].add(slug)

    out = []
    for (a, b), w in pair.items():
        if len(pair_rest[(a, b)]) < min_restaurants:
            continue
        p_ab, p_a, p_b = w / total, single[a] / total, single[b] / total
        lift = p_ab / (p_a * p_b)
        out.append({"a": a, "b": b, "support": p_ab, "lift": lift, "pmi": math.log2(lift),
                    "restaurants": len(pair_rest[(a, b)])})
    return sorted(out, key=lambda d: (-d["lift"], -d["support"], d["a"], d["b"]))


def coverage(restaurants: dict[str, Restaurant], rows: list[Row]) -> list[dict]:
    """Per region: restaurants in the seed, how many publish menus, how many have data."""
    with_data = {r.restaurant for r in rows}
    table: dict[str, dict] = {}
    for r in restaurants.values():
        t = table.setdefault(r.region, {"region": r.region, "restaurants": 0,
                                        "publishes_menu": 0, "with_data": 0})
        t["restaurants"] += 1
        t["publishes_menu"] += r.publishes_menu == "yes"
        t["with_data"] += r.slug in with_data
    return sorted(table.values(), key=lambda t: t["region"])

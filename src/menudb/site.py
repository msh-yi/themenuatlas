"""Aggregated JSON for the static site in site/ (`menudb export-site`).

The site is a static page that loads one JSON file and does search, filtering and
charts in the browser. Only derived data goes in: ingredient names, preparations,
shares, pair scores, coverage, and restaurant names with a link to their website.
Never course text, never which ingredients share a course at one restaurant (that
reconstructs dishes), and pairs only when several restaurants use them.

Real data is not published yet (see "Website" in docs/PLAN.md). The Actions workflow
builds the site from the synthetic fixture only.
"""

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from menudb import analysis as a

SCHEMA = 1
SEASONS = ["spring", "summer", "autumn", "winter", "tropical", "unknown"]


def preparations_from_extracted(menus: Iterable[dict]) -> dict[str, Counter]:
    """ingredient -> preparation -> number of restaurants using that preparation."""
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for m in menus:
        if not m.get("is_menu", True):
            continue
        for c in m.get("courses", []):
            for comp in c.get("components", []):
                name = (comp.get("name") or comp["raw_term"]).strip().lower()
                for p in comp.get("preparations") or []:
                    if name and p.strip():
                        seen[name][p.strip().lower()].add(m["slug"])
    return {i: Counter({p: len(s) for p, s in preps.items()}) for i, preps in seen.items()}


def preparations_from_db(session) -> dict[str, Counter]:
    from sqlalchemy import func, select

    from menudb.db.models import Component, Course, Ingredient, Menu, Restaurant

    q = (
        select(Restaurant.slug,
               func.lower(func.coalesce(Ingredient.canonical_name, Component.suggested_name,
                                        Component.raw_term)),
               Component.preparations)
        .join(Menu, Menu.restaurant_id == Restaurant.id)
        .join(Course, Course.menu_id == Menu.id)
        .join(Component, Component.course_id == Course.id)
        .outerjoin(Ingredient, Ingredient.id == Component.ingredient_id)
    )
    return preparations_from_extracted(
        {"slug": slug, "courses": [{"components": [{"raw_term": name, "preparations": preps}]}]}
        for slug, name, preps in session.execute(q) if name)


def restaurant_details(csv_path: Path | None) -> dict[str, dict]:
    """Public facts from the seed CSV: name, city, country, coordinates, website.

    An optional `coord_precision` column marks city-level coordinates (`city`) so the
    map can show them as approximate; blank or `exact` means a geocoded address.
    """
    if csv_path is None or not csv_path.exists():
        return {}
    with csv_path.open() as f:
        return {r["slug"]: {"name": r["name"], "city": r["city"], "country": r["country"],
                            "lon": float(r["lon"]) if r.get("lon") else None,
                            "approx": (r.get("coord_precision") or "").strip().lower() == "city",
                            "url": r["website"]} for r in csv.DictReader(f)}


def add_full_list(restaurants: dict[str, a.Restaurant], details: dict[str, dict],
                  full_csv: Path) -> None:
    """Add every restaurant in data/seed/restaurants_full.csv to the coverage map.

    The analysis set (restaurants.csv, or the database) is unchanged; restaurants only
    in the full list have no menus yet. Countries the seed code table doesn't know
    fall into region "Other" instead of failing the export.
    """
    from menudb.seed import _PUBLISHES, country_code

    if not full_csv.exists():
        return
    for slug, det in restaurant_details(full_csv).items():
        details.setdefault(slug, det)
    with full_csv.open() as f:
        for r in csv.DictReader(f):
            if r["slug"] in restaurants:
                continue
            try:
                code = country_code(r["country"])
            except ValueError:
                code = None
            restaurants[r["slug"]] = a.Restaurant(
                r["slug"], a.region_of(code), float(r["lat"]) if r.get("lat") else None,
                _PUBLISHES.get((r.get("publishes_menu") or "").strip().lower(), "unknown"))


# data/seed/discovery.csv status -> what it says about publishing a menu
DISCOVERY = {"menu_found": "yes", "hand_recipe": "yes", "no_menu_found": "no",
             "robots": "blocked", "opted_out": "blocked"}


def apply_discovery(restaurants: dict[str, a.Restaurant], details: dict[str, dict],
                    discovery_csv: Path) -> None:
    """Fill in publishes_menu from menu discovery where the seed lists don't say.

    `blocked` (robots.txt or a TDM opt-out) is kept apart from "doesn't publish": the
    menu may exist, but we don't fetch it. Unclear results (menu_maybe, failed) stay unknown.
    """
    from dataclasses import replace

    if not discovery_csv.exists():
        return
    with discovery_csv.open() as f:
        for row in csv.DictReader(f):
            r, found = restaurants.get(row["slug"]), DISCOVERY.get(row.get("status", ""))
            if r is None or found is None:
                continue
            if found == "blocked":
                details.setdefault(r.slug, {})["blocked"] = True
            elif r.publishes_menu == "unknown":
                restaurants[r.slug] = replace(r, publishes_menu=found)


def _share_by(rows: list[a.Row], by: str) -> dict[str, dict[str, float]]:
    """ingredient -> group -> share."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for d in a.ingredient_frequency(rows, by=by):
        out[d["ingredient"]][d["group"]] = round(d["share"], 4)
    return out


def build(rows: list[a.Row], restaurants: dict[str, a.Restaurant], *, source: str,
          details: dict[str, dict] | None = None, preparations: dict[str, Counter] | None = None,
          min_restaurants: int = 2, now: datetime | None = None,
          wikipedia: dict[str, str] | None = None) -> dict:
    """`wikipedia` maps ingredient name -> article URL (see wiki.py)."""
    details = details or {}
    wikipedia = wikipedia or {}
    preparations = preparations or {}
    overall = a.ingredient_frequency(rows)
    by_region, by_season = _share_by(rows, "region"), _share_by(rows, "season")
    users: dict[str, set[str]] = defaultdict(set)
    menus: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        users[r.ingredient].add(r.restaurant)
        menus[r.restaurant].add(r.menu)
    pairs = a.pair_stats(rows, min_restaurants=min_restaurants)

    ingredients = [{
        "name": d["ingredient"],
        "share": round(d["share"], 4),
        "restaurants": sorted(users[d["ingredient"]]),
        "by_region": by_region.get(d["ingredient"], {}),
        "by_season": by_season.get(d["ingredient"], {}),
        "preparations": preparations.get(d["ingredient"], Counter()).most_common(8),
        "wikipedia": wikipedia.get(d["ingredient"]),
    } for d in overall]

    restaurant_list = []
    for slug, r in sorted(restaurants.items()):
        det = details.get(slug, {})
        restaurant_list.append({
            "slug": slug, "name": det.get("name") or slug, "city": det.get("city"),
            "country": det.get("country"), "region": r.region, "lat": r.lat, "lon": det.get("lon"),
            "approx": bool(det.get("approx")),
            "blocked": bool(det.get("blocked")),
            "url": det.get("url"), "publishes_menu": r.publishes_menu,
            "menus": len(menus.get(slug, ())),
            "ingredients": sum(slug in u for u in users.values()),
        })

    groups = {
        "region": sorted({r.region for r in restaurants.values()}),
        "season": [s for s in SEASONS if any(r.season == s for r in rows)],
    }
    return {
        "schema": SCHEMA,
        "source": source,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "min_restaurants": min_restaurants,
        "totals": {
            "restaurants": len(restaurants),
            "restaurants_with_data": len(menus),
            "menus": sum(len(m) for m in menus.values()),
            "courses": len({(r.restaurant, r.menu, r.course) for r in rows}),
            "ingredients": len(ingredients),
            "pairs": len(pairs),
        },
        "groups": groups,
        "coverage": a.coverage(restaurants, rows),
        "restaurants": restaurant_list,
        "ingredients": ingredients,
        "pairs": [{"a": p["a"], "b": p["b"], "support": round(p["support"], 5),
                   "lift": round(p["lift"], 3), "pmi": round(p["pmi"], 3),
                   "restaurants": p["restaurants"]} for p in pairs],
    }


def load(source: str, root: Path) -> tuple[list[a.Row], dict[str, a.Restaurant], dict, dict]:
    """rows, restaurants, restaurant details and preparations for one source."""
    csv_path = root / "data/seed/restaurants.csv"
    full_csv = root / "data/seed/restaurants_full.csv"  # the map shows the whole list
    if source == "synthetic":
        path = root / "tests/fixtures/synthetic_menus.json"
        rows, restaurants = a.load_synthetic(path)
        data = json.loads(path.read_text())
        details = {r["slug"]: {"name": r.get("name"), "city": r.get("city"),
                               "country": r["country"], "lon": r.get("lon"), "url": None}
                   for r in data["restaurants"]}
        return rows, restaurants, details, preparations_from_extracted(data["menus"])
    if source == "bucket":
        from menudb import handoff, storage

        restaurants = a.restaurants_from_csv(csv_path)
        menus = [handoff.get_json(k) for prefix in (handoff.EXTRACTED, handoff.IMPORTED)
                 for k in storage.list_keys(prefix)]
        details = restaurant_details(csv_path)
        add_full_list(restaurants, details, full_csv)
        apply_discovery(restaurants, details, root / "data/seed/discovery.csv")
        return a.rows_from_extracted(menus, restaurants), restaurants, details, preparations_from_extracted(menus)
    if source == "db":
        from menudb.db import get_session

        with get_session() as session:
            rows, restaurants = a.load_db(session)
            preps = preparations_from_db(session)
        details = restaurant_details(csv_path)
        add_full_list(restaurants, details, full_csv)
        apply_discovery(restaurants, details, root / "data/seed/discovery.csv")
        return rows, restaurants, details, preps
    raise ValueError(f"unknown source {source!r}")


def write(data: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "data.json"
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return path

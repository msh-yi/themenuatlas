"""Aggregated JSON for the static site in site/ (`menudb export-site`).

The site is a static page that loads one JSON file and does search, filtering and
charts in the browser. Only derived data goes in: ingredient names, preparations,
shares, pair scores, coverage, and restaurant names with a link to their website.
Never course text, and pairs only when several restaurants use them.

Each restaurant's menus, as courses with their ingredients and preparations (a
manifest, no menu wording), go in only with `export-site --menus`. site.yml builds the
public site from Postgres with them (see "Website" in docs/PLAN.md).
"""

import csv
import json
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

from menudb import analysis as a
from menudb.extract.schema import MAX_DISH_INGREDIENTS

SCHEMA = 2  # 2: split into data.json, pairs.json and menus/NN.json (see write())
MENU_SHARDS = 64
SEASONS = ["spring", "summer", "autumn", "winter", "tropical", "unknown"]
# Accolade groups, most selective first. A restaurant can be in several.
ACCOLADES = ["3 stars", "2 stars", "1 star", "World's 50 Best", "NYT America 50", "NYT NYC 100"]
# Groups with fewer restaurants with menus get no ingredient shares or pairings: too few
# to say anything, and too close to describing single restaurants.
MIN_GROUP = 10
PAIRS_PER_GROUP = 3000


def accolades(lists: str | None) -> dict:
    """Public list facts from a `lists` cell such as
    "michelin:2026:3;michelin:2020:2;w50b:2025:83;nyt:2026:america50;nytnyc:2026:48".

    Michelin stars come from the most recent year listed (the highest, if a year repeats);
    ranks likewise from each list's most recent year.
    """
    best: dict[str, tuple[int, int]] = {}  # list -> (year, value)
    for entry in filter(None, (lists or "").split(";")):
        kind, _, rest = entry.strip().partition(":")
        year, _, value = rest.partition(":")
        if not year.isdigit():
            continue
        y = int(year)
        if kind == "michelin" and value.isdigit():
            v = int(value)
            if kind not in best or (y, v) > best[kind]:
                best[kind] = (y, v)
        elif kind in ("w50b", "nytnyc") and value.isdigit():
            if kind not in best or y > best[kind][0]:
                best[kind] = (y, int(value))
        elif kind == "nyt":
            best[kind] = (y, 0)
    out: dict = {}
    if "michelin" in best and best["michelin"][1]:
        out["stars"], out["stars_year"] = best["michelin"][1], best["michelin"][0]
    if "w50b" in best:
        out["w50b"], out["w50b_year"] = best["w50b"][1], best["w50b"][0]
    if "nyt" in best:
        out["nyt50"] = best["nyt"][0]
    if "nytnyc" in best:
        out["nytnyc"], out["nytnyc_year"] = best["nytnyc"][1], best["nytnyc"][0]
    return out


def accolade_groups(acc: dict) -> list[str]:
    tiers = []
    if acc.get("stars"):
        tiers.append({3: "3 stars", 2: "2 stars"}.get(acc["stars"], "1 star"))
    if "w50b" in acc:
        tiers.append("World's 50 Best")
    if "nyt50" in acc:
        tiers.append("NYT America 50")
    if "nytnyc" in acc:
        tiers.append("NYT NYC 100")
    return tiers


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


def manifests_from_extracted(menus: Iterable[dict],
                             seen: dict[str, tuple[str | None, str | None]] | None = None) -> dict[str, list[dict]]:
    """slug -> that restaurant's menus as manifests: courses in order, each a list of
    [ingredient, [preparations]] or [ingredient, [preparations], term], where term is the name
    the menu used when it differs from the canonical one. build() keeps a term only if the
    lexicon lists it as another name for that ingredient, so no other wording gets through.
    `seen` maps normalized_hash to (first_seen, last_seen) ISO dates when known."""
    out: dict[str, list[dict]] = defaultdict(list)
    done = set()
    for m in menus:
        key = (m["slug"], m["normalized_hash"])
        if not m.get("is_menu", True) or key in done:
            continue
        done.add(key)
        courses = []
        for c in sorted(m.get("courses", []), key=lambda c: c.get("position", 0)):
            items, names = [], set()
            for comp in c.get("components", []):
                name = (comp.get("name") or comp["raw_term"]).strip().lower()
                if not name or name in names:
                    continue
                names.add(name)
                item = [name, [p.strip().lower() for p in comp.get("preparations") or [] if p.strip()]]
                term = (comp.get("raw_term") or "").strip().lower()
                if term and _spelling(term) != _spelling(name):
                    item.append(term)
                items.append(item)
            if items:
                # an unsplit section: shown apart, kept out of course lists (and pairings)
                kind = "section" if len(items) > MAX_DISH_INGREDIENTS else c.get("kind") or "course"
                courses.append({"k": kind, "i": items})
        if not courses:
            continue
        first, last = (seen or {}).get(m["normalized_hash"], (None, None))
        out[m["slug"]].append({k: v for k, v in {
            "id": m["normalized_hash"][:12], "type": m.get("menu_type"), "lang": m.get("language"),
            "price": m.get("price"), "currency": m.get("currency"), "period": m.get("stated_period"),
            "first_seen": first, "last_seen": last, "courses": courses}.items() if v not in (None, "")})
    for ms in out.values():  # most recent first, then the longest
        ms.sort(key=lambda x: (x.get("last_seen") or "", sum(len(c["i"]) for c in x["courses"])), reverse=True)
    return dict(out)


def manifests_from_db(session) -> dict[str, list[dict]]:
    from sqlalchemy import func, select

    from menudb.db.models import Component, Course, Ingredient, Menu, Restaurant

    q = (
        select(Restaurant.slug, Menu.normalized_hash, Menu.menu_type, Menu.language, Menu.price,
               Menu.currency, Menu.first_seen, Menu.last_seen, Course.position, Course.kind,
               func.lower(func.coalesce(Ingredient.canonical_name, Component.suggested_name,
                                        Component.raw_term)),
               Component.preparations, Component.raw_term)
        .join(Menu, Menu.restaurant_id == Restaurant.id)
        .join(Course, Course.menu_id == Menu.id)
        .join(Component, Component.course_id == Course.id)
        .outerjoin(Ingredient, Ingredient.id == Component.ingredient_id)
        .where(Menu.excluded_reason.is_(None))
        .order_by(Restaurant.slug, Menu.id, Course.position, Component.id)
    )
    menus: dict[tuple[str, str], dict] = {}
    seen: dict[str, tuple[str | None, str | None]] = {}
    for slug, h, mtype, lang, price, cur, first, last, pos, kind, name, preps, term in session.execute(q):
        m = menus.setdefault((slug, h), {"slug": slug, "normalized_hash": h, "is_menu": mtype != "not_a_menu",
                                         "menu_type": mtype, "language": lang, "price": price,
                                         "currency": cur, "courses": {}})
        seen[h] = (first.date().isoformat() if first else None, last.date().isoformat() if last else None)
        c = m["courses"].setdefault(pos, {"position": pos, "kind": kind, "components": []})
        if name:
            c["components"].append({"name": name, "raw_term": term, "preparations": preps or []})
    for m in menus.values():
        m["courses"] = list(m["courses"].values())
    return manifests_from_extracted(menus.values(), seen)


def preparations_from_db(session, drop: frozenset[str] = frozenset()) -> dict[str, Counter]:
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
        .where(Menu.excluded_reason.is_(None))
    )
    return preparations_from_extracted(
        {"slug": slug, "courses": [{"components": [{"raw_term": name, "preparations": preps}]}]}
        for slug, name, preps in session.execute(q) if name and slug not in drop)


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
                            "accolades": accolades(r.get("lists")),
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
        known = details.setdefault(slug, det)
        if det["accolades"]:  # the full list is the more complete and recent source
            known["accolades"] = det["accolades"]
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


def _spelling(term: str) -> str:
    """Case, accents, spaces and punctuation removed: "Wagyū", "wagyu" and "rib-eye",
    "rib eye", "ribeye" each come out the same."""
    t = unicodedata.normalize("NFKD", term.lower())
    return "".join(c for c in t if c.isalnum() and not unicodedata.combining(c))


def _lexicon_terms(manifests: dict[str, list[dict]] | None,
                   lexicon: dict[str, dict]) -> tuple[dict[str, list[dict]] | None, dict[str, dict[str, int]]]:
    """Manifests with each item's term kept only when the lexicon lists it as another name
    for that ingredient (as the lexicon spells it), and ingredient -> term -> restaurants."""
    if not manifests:
        return manifests, {}
    known = {name: {_spelling(t): t for t in info.get("aka") or []} for name, info in lexicon.items()}
    users: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    out: dict[str, list[dict]] = {}
    for slug, ms in manifests.items():
        out[slug] = []
        for m in ms:
            courses = []
            for c in m["courses"]:
                items = []
                for item in c["i"]:
                    term = known.get(item[0], {}).get(_spelling(item[2])) if len(item) > 2 else None
                    items.append([item[0], item[1], term] if term else [item[0], item[1]])
                    if term:
                        users[item[0]][term].add(slug)
                courses.append({**c, "i": items})
            out[slug].append({**m, "courses": courses})
    return out, {i: {t: len(s) for t, s in ts.items()} for i, ts in users.items()}


def lexicon_info(path: Path) -> dict[str, dict]:
    """canonical name -> {category, parent, aka}, from data/lexicon/ingredients.txt.

    `aka` is the other names a menu may use (aubergine: eggplant, berenjena, ナス),
    so the site's search finds an ingredient under any of them.
    """
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    category = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            category = line[3:].strip()
            continue
        if not line or line.startswith("#"):
            continue
        head, _, body = line.partition("=")
        name, _, parent = head.partition(">")
        name = name.strip().lower()
        aka: list[str] = []
        seen = {_spelling(name)}  # one chip per spelling: not both "ribeye" and "rib eye"
        for part in body.split("|") if body else []:
            _, _, terms = part.partition(":")
            for t in terms.split(","):
                t = t.strip()
                if t and _spelling(t) not in seen:
                    seen.add(_spelling(t))
                    aka.append(t)
        out[name] = {"category": category, "parent": parent.strip().lower() or None, "aka": aka[:24]}
    return out


def _share_by(rows: list[a.Row], by: str) -> dict[str, dict[str, float]]:
    """ingredient -> group -> share."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for d in a.ingredient_frequency(rows, by=by):
        out[d["ingredient"]][d["group"]] = round(d["share"], 4)
    return out


def build(rows: list[a.Row], restaurants: dict[str, a.Restaurant], *, source: str,
          details: dict[str, dict] | None = None, preparations: dict[str, Counter] | None = None,
          min_restaurants: int = 2, min_group: int = MIN_GROUP, now: datetime | None = None,
          wikipedia: dict[str, str] | None = None, lexicon: dict[str, dict] | None = None,
          manifests: dict[str, list[dict]] | None = None) -> dict:
    """`wikipedia` maps ingredient name -> article URL (see wiki.py); `lexicon` comes
    from lexicon_info(); `manifests` (only with --menus) from manifests_from_extracted()."""
    details = details or {}
    wikipedia = wikipedia or {}
    lexicon = lexicon or {}
    manifests, aka_counts = _lexicon_terms(manifests, lexicon)
    preparations = preparations or {}
    overall = a.ingredient_frequency(rows)
    by_region, by_season = _share_by(rows, "region"), _share_by(rows, "season")
    users: dict[str, set[str]] = defaultdict(set)
    menus: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        users[r.ingredient].add(r.restaurant)
        menus[r.restaurant].add(r.menu)
    pairs = a.pair_stats(rows, min_restaurants=min_restaurants)

    # accolade groups: ingredient shares and pairings among the restaurants in each
    tiers = {slug: accolade_groups(details.get(slug, {}).get("accolades") or {}) for slug in restaurants}
    by_accolade: dict[str, dict[str, float]] = defaultdict(dict)
    pairs_by_accolade: dict[str, list[dict]] = {}
    accolade_with_data: dict[str, int] = {}
    for g in ACCOLADES:
        grows = [r for r in rows if g in tiers.get(r.restaurant, ())]
        n = len({r.restaurant for r in grows})
        if n < min_group:
            continue
        accolade_with_data[g] = n
        for d in a.ingredient_frequency(grows):
            by_accolade[d["ingredient"]][g] = round(d["share"], 4)
        gp = sorted(a.pair_stats(grows, min_restaurants=min_restaurants),
                    key=lambda p: (-p["score"], -p["restaurants"], p["a"], p["b"]))
        pairs_by_accolade[g] = [_pair(p) for p in gp[:PAIRS_PER_GROUP]]

    ingredients = [{
        "name": d["ingredient"],
        "share": round(d["share"], 4),
        "restaurants": sorted(users[d["ingredient"]]),
        "by_region": by_region.get(d["ingredient"], {}),
        "by_season": by_season.get(d["ingredient"], {}),
        "by_accolade": by_accolade.get(d["ingredient"], {}),
        "preparations": preparations.get(d["ingredient"], Counter()).most_common(8),
        "wikipedia": wikipedia.get(d["ingredient"]),
        **{k: v for k, v in lexicon.get(d["ingredient"], {}).items() if v},
        **({"aka_n": aka_counts[d["ingredient"]]} if aka_counts.get(d["ingredient"]) else {}),
    } for d in overall]

    restaurant_list = []
    for slug, r in sorted(restaurants.items()):
        det = details.get(slug, {})
        restaurant_list.append({
            "slug": slug, "name": det.get("name") or slug, "city": det.get("city"),
            "country": det.get("country"), "region": r.region, "lat": r.lat, "lon": det.get("lon"),
            "approx": bool(det.get("approx")),
            "accolades": det.get("accolades") or {}, "tiers": tiers.get(slug, []),
            "blocked": bool(det.get("blocked")),
            "url": det.get("url"),
            "publishes_menu": "yes" if menus.get(slug) else r.publishes_menu,
            "menus": len(menus.get(slug, ())),
            # when this restaurant's menus were first and last seen (dates only)
            "first_seen": det.get("first_seen"), "last_seen": det.get("last_seen"),
            "ingredients": sum(slug in u for u in users.values()),
        })
        if manifests and manifests.get(slug):
            restaurant_list[-1]["menu_list"] = manifests[slug]

    dates = sorted({r.seen for r in rows if r.seen})
    groups = {
        "region": sorted({r.region for r in restaurants.values()}),
        "season": [s for s in SEASONS if any(r.season == s for r in rows)],
        "category": sorted({i["category"] for i in ingredients if i.get("category")}),
        # every accolade some restaurant holds (restaurant filters), and those with
        # enough menus for ingredient shares and pairings
        "accolade": [g for g in ACCOLADES if any(g in t for t in tiers.values())],
        "accolade_with_data": accolade_with_data,
    }
    return {
        "schema": SCHEMA,
        "source": source,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "min_restaurants": min_restaurants,
        "has_menus": bool(manifests),
        # when menus were first seen: seasons mean little until this spans the year
        "seen_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
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
        "pairs": [_pair(p) for p in pairs],
        "pairs_by_accolade": pairs_by_accolade,
    }


def _pair(p: dict) -> dict:
    return {"a": p["a"], "b": p["b"], "support": round(p["support"], 5),
            "lift": round(p["lift"], 3), "pmi": round(p["pmi"], 3),
            "score": round(p["score"], 3), "restaurants": p["restaurants"]}


def optouts(path: Path) -> frozenset[str]:
    """Slugs of restaurants that asked to be left out (data/seed/optouts.csv: slug, date, note)."""
    if not path.exists():
        return frozenset()
    with path.open(newline="") as f:
        return frozenset(r["slug"].strip() for r in csv.DictReader(f) if r.get("slug", "").strip())


def drop_restaurants(loaded: tuple, drop: frozenset[str]) -> tuple:
    """load()'s result without these restaurants: no name, no menus, no part in any share or pair."""
    if not drop:
        return loaded
    rows, restaurants, details, preps, manifests = loaded
    return ([r for r in rows if r.restaurant not in drop],
            {k: v for k, v in restaurants.items() if k not in drop},
            {k: v for k, v in details.items() if k not in drop},
            preps, {k: v for k, v in manifests.items() if k not in drop})


def load(source: str, root: Path) -> tuple[list[a.Row], dict[str, a.Restaurant], dict, dict, dict]:
    """rows, restaurants, restaurant details, preparations and menu manifests for one source.

    Restaurants in data/seed/optouts.csv are left out of real builds entirely."""
    drop = optouts(root / "data/seed/optouts.csv")
    csv_path = root / "data/seed/restaurants.csv"
    full_csv = root / "data/seed/restaurants_full.csv"  # the map shows the whole list
    if source == "synthetic":
        path = root / "tests/fixtures/synthetic_menus.json"
        rows, restaurants = a.load_synthetic(path)
        data = json.loads(path.read_text())
        details = {r["slug"]: {"name": r.get("name"), "city": r.get("city"),
                               "country": r["country"], "lon": r.get("lon"), "url": None,
                               "accolades": accolades(r.get("lists"))}
                   for r in data["restaurants"]}
        seen = {m["normalized_hash"]: (m.get("first_seen"), m.get("last_seen")) for m in data["menus"]}
        add_windows(details, ((m["slug"], *seen[m["normalized_hash"]]) for m in data["menus"]
                              if m.get("is_menu", True)))
        return (rows, restaurants, details, preparations_from_extracted(data["menus"]),
                manifests_from_extracted(data["menus"], seen))
    if source == "bucket":
        from menudb import handoff, storage

        restaurants = a.restaurants_from_csv(csv_path)
        menus = [m for m in (handoff.get_json(k) for prefix in (handoff.EXTRACTED, handoff.IMPORTED)
                             for k in storage.list_keys(prefix)) if m["slug"] not in drop]
        details = restaurant_details(csv_path)
        add_full_list(restaurants, details, full_csv)
        apply_discovery(restaurants, details, root / "data/seed/discovery.csv")
        seen = bucket_seen()
        add_windows(details, ((m["slug"], *seen[m["normalized_hash"]]) for m in menus
                              if m.get("is_menu", True) and m["normalized_hash"] in seen))
        first = {h: date.fromisoformat(f) for h, (f, _) in seen.items()}
        return drop_restaurants((a.rows_from_extracted(menus, restaurants, first), restaurants, details,
                                 preparations_from_extracted(menus), manifests_from_extracted(menus, seen)), drop)
    if source == "db":
        from menudb.db import get_session

        with get_session() as session:
            rows, restaurants = a.load_db(session)
            preps = preparations_from_db(session, drop)
            manifests = manifests_from_db(session)
            windows = menu_windows_from_db(session)
        details = restaurant_details(csv_path)
        add_full_list(restaurants, details, full_csv)
        apply_discovery(restaurants, details, root / "data/seed/discovery.csv")
        add_windows(details, windows)
        return drop_restaurants((rows, restaurants, details, preps, manifests), drop)
    raise ValueError(f"unknown source {source!r}")


def add_windows(details: dict[str, dict], menus: Iterable[tuple[str, str | None, str | None]]) -> None:
    """Set each restaurant's first_seen / last_seen (ISO dates) from (slug, first, last) per menu."""
    for slug, first, last in menus:
        det = details.setdefault(slug, {})
        if first and (det.get("first_seen") is None or first < det["first_seen"]):
            det["first_seen"] = first
        last = last or first
        if last and (det.get("last_seen") is None or last > det["last_seen"]):
            det["last_seen"] = last


def menu_windows_from_db(session) -> list[tuple[str, str, str]]:
    """(slug, first_seen, last_seen) per menu that counts: not excluded, not not-a-menu."""
    from sqlalchemy import or_, select

    from menudb.db.models import Menu, Restaurant

    q = (select(Restaurant.slug, Menu.first_seen, Menu.last_seen)
         .join(Menu, Menu.restaurant_id == Restaurant.id)
         .where(Menu.excluded_reason.is_(None),
                or_(Menu.menu_type.is_(None), Menu.menu_type != "not_a_menu")))
    return [(slug, f.date().isoformat(), (l or f).date().isoformat()) for slug, f, l in session.execute(q)]


def bucket_seen() -> dict[str, tuple[str, str]]:
    """normalized_hash -> (first, last) dates for bucket-only builds, which have no menu rows:
    when each document was first and last queued (pending/ or processed/). Approximate."""
    from menudb import handoff, storage

    seen: dict[str, tuple[str, str]] = {}
    for prefix in (handoff.PENDING, handoff.PROCESSED):
        for k, when in storage.list_modified(prefix).items():
            h = k.rsplit("/", 1)[-1].removesuffix(".json")
            d = when.date().isoformat()
            f, l = seen.get(h, (d, d))
            seen[h] = (min(f, d), max(l, d))
    return seen


def shard(slug: str) -> str:
    """Menu shard for a restaurant: FNV-1a 32-bit of the slug's UTF-8 bytes, mod 64.
    site/app.js computes the same to find a restaurant's file."""
    h = 0x811C9DC5
    for b in slug.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h % MENU_SHARDS:02d}"


# Restaurant fields left out when they hold their default; the site fills them back in.
_DEFAULTS = {"approx": False, "blocked": False, "menus": 0, "ingredients": 0, "accolades": {},
             "tiers": [], "publishes_menu": "unknown", "url": None, "city": None, "country": None,
             "lat": None, "lon": None, "first_seen": None, "last_seen": None}


def split(data: dict) -> dict[str, object]:
    """The bundle as files, relative path -> JSON value, so a first page load stays light:

    data.json        everything the list views, map and charts need, compacted: restaurant
                     fields at their default are dropped and each ingredient's restaurants
                     are indexes into `restaurants`
    pairs.json       pairs and pairs_by_accolade, loaded by the pairing and ingredient views
    menus/NN.json    {slug: menu_list} for restaurants in shard NN (only with --menus),
                     loaded when a restaurant page opens
    """
    core = {k: v for k, v in data.items() if k not in ("pairs", "pairs_by_accolade")}
    index = {r["slug"]: i for i, r in enumerate(data["restaurants"])}
    shards: dict[str, dict] = {f"{i:02d}": {} for i in range(MENU_SHARDS)} if data.get("has_menus") else {}
    restaurants = []
    for r in data["restaurants"]:
        r = dict(r)
        menus = r.pop("menu_list", None)
        if menus:
            shards[shard(r["slug"])][r["slug"]] = menus
        restaurants.append({k: v for k, v in r.items()
                            if k not in _DEFAULTS or (v != _DEFAULTS[k] and v != "")})
    core["restaurants"] = restaurants
    core["ingredients"] = [{**i, "restaurants": [index[s] for s in i["restaurants"] if s in index]}
                           for i in data["ingredients"]]
    files: dict[str, object] = {"data.json": core,
                                "pairs.json": {"pairs": data["pairs"],
                                               "pairs_by_accolade": data.get("pairs_by_accolade", {})}}
    files |= {f"menus/{k}.json": v for k, v in shards.items()}
    return files


def write(data: dict, out_dir: Path) -> list[Path]:
    """Write split(data) under out_dir; returns the paths, data.json first."""
    paths = []
    for rel, value in split(data).items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        paths.append(path)
    return paths

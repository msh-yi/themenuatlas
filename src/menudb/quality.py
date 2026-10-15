"""Data-quality checks for the Actions step summary (`menudb stats`).

Counts plus a few slugs, short hashes and hosts as examples, never menu text. Excluded
menus (`menudb exclude`) are left out, so a check stays at zero once its cause is fixed.
"""

import hashlib
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from menudb.db.models import Component, Course, Menu, Restaurant, Snapshot, Source
from menudb.domains import not_official

EXAMPLES = 5


def _host(url: str | None) -> str:
    return urlsplit(url or "").netloc.lower().split(":")[0].removeprefix("www.")


def _top(counter: Counter, n: int = EXAMPLES) -> str:
    return ", ".join(f"{k} ({v})" for k, v in counter.most_common(n))


def checks(session: Session) -> list[dict]:
    """[{check, count, examples, note}], one row per check."""
    out = []

    def add(check: str, count: int, examples: str, note: str):
        out.append({"check": check, "count": count, "examples": examples, "note": note})

    live = Menu.excluded_reason.is_(None)
    slugs = dict(session.execute(select(Restaurant.id, Restaurant.slug)).all())

    # One document under several restaurants: a search result or aggregator page matched
    # to the wrong places. Same normalized hash, or the same fetched URL.
    by_hash: dict[str, set[int]] = defaultdict(set)
    for rid, h in session.execute(select(Menu.restaurant_id, Menu.normalized_hash).where(live)):
        by_hash[h].add(rid)
    shared = {h: r for h, r in by_hash.items() if len(r) > 1}
    add("documents_shared_by_restaurants", len(shared),
        _top(Counter({h[:12]: len(r) for h, r in shared.items()})),
        f"{len(set().union(*shared.values())) if shared else 0} restaurants involved")

    by_url: dict[str, set[int]] = defaultdict(set)
    q = (select(Source.restaurant_id, Snapshot.url).join(Snapshot, Snapshot.source_id == Source.id)
         .join(Menu, Menu.first_snapshot_id == Snapshot.id).where(live))
    for rid, url in session.execute(q):
        by_url[url].add(rid)
    shared_urls = {u: r for u, r in by_url.items() if len(r) > 1}
    add("urls_shared_by_restaurants", len(shared_urls),
        _top(Counter({_host(u): len(r) for u, r in shared_urls.items()})),
        "menu URLs fetched for more than one restaurant")

    # Websites: a host several restaurants share, or a news/guide/directory host.
    sites = session.execute(select(Restaurant.slug, Restaurant.website)
                            .where(Restaurant.website.is_not(None), Restaurant.website != "")).all()
    by_host: dict[str, list[str]] = defaultdict(list)
    for slug, url in sites:
        by_host[_host(url)].append(slug)
    shared_hosts = {h: s for h, s in by_host.items() if h and len(s) > 1}
    add("websites_shared_by_restaurants", len(shared_hosts),
        _top(Counter({h: len(s) for h, s in shared_hosts.items()})),
        f"{sum(len(s) for s in shared_hosts.values())} restaurants; group sites can be legitimate")
    bad = sorted(slug for slug, url in sites if not_official(_host(url)))
    add("websites_on_aggregator_hosts", len(bad), ", ".join(bad[:EXAMPLES]),
        "news, guide, directory, booking or social hosts")

    # Extraction: menus with no matched ingredient, restaurants whose courses mostly
    # match nothing, and the same dishes stored as two menus.
    matched = (select(Course.menu_id, Course.id.label("course_id"),
                      func.count(Component.ingredient_id).label("n"))
               .outerjoin(Component, Component.course_id == Course.id)
               .group_by(Course.menu_id, Course.id).subquery())
    rows = session.execute(
        select(Menu.id, Menu.restaurant_id, matched.c.n).outerjoin(matched, matched.c.menu_id == Menu.id)
        .where(live, Menu.extraction_version.is_not(None),
               func.coalesce(Menu.menu_type, "") != "not_a_menu")).all()
    per_menu: dict[int, list[int]] = defaultdict(list)
    menu_owner = {}
    for mid, rid, n in rows:
        menu_owner[mid] = rid
        per_menu[mid] += [] if n is None else [n]  # no courses at all: still a menu to flag
    empty = [m for m, ns in per_menu.items() if not any(ns)]
    add("menus_with_no_ingredients", len(empty),
        _top(Counter(slugs[menu_owner[m]] for m in empty)), "extracted menus, not marked not-a-menu")

    per_rest: dict[int, list[int]] = defaultdict(list)
    for m, ns in per_menu.items():
        per_rest[menu_owner[m]] += ns
    mostly = {slugs[r]: round(sum(1 for n in ns if not n) / len(ns), 2)
              for r, ns in per_rest.items() if len(ns) >= 5 and sum(1 for n in ns if not n) > len(ns) / 2}
    add("restaurants_mostly_unmatched_courses", len(mostly), _top(Counter(mostly)),
        "5+ courses, over half with no matched ingredient (share shown)")

    sig: dict[int, list[tuple]] = defaultdict(list)
    comp = (select(Course.menu_id, Course.position, Component.ingredient_id)
            .join(Component, Component.course_id == Course.id).join(Menu, Menu.id == Course.menu_id)
            .where(live, Component.ingredient_id.is_not(None)).order_by(Course.menu_id, Course.position))
    courses: dict[tuple[int, int], set[int]] = defaultdict(set)
    for mid, pos, ing in session.execute(comp):
        courses[(mid, pos)].add(ing)
    for (mid, pos), ings in sorted(courses.items()):
        sig[mid].append(tuple(sorted(ings)))
    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    for mid, s in sig.items():
        if mid in menu_owner and len(s) >= 3:
            digest = hashlib.sha1(repr(sorted(s)).encode()).hexdigest()
            groups[(menu_owner[mid], digest)].append(mid)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    add("duplicate_menus", sum(len(v) - 1 for v in dups.values()),
        _top(Counter(slugs[r] for r, _ in dups)),
        "same restaurant, same dishes (by ingredients) under different hashes")

    failing = session.execute(select(Restaurant.slug, Source.consecutive_failures)
                              .join(Source, Source.restaurant_id == Restaurant.id)
                              .where(Source.consecutive_failures >= 3)).all()
    add("sources_failing_3_plus_times", len(failing),
        _top(Counter({s: n for s, n in failing})), "consecutive failed checks")
    return out


def markdown(rows: list[dict]) -> str:
    lines = ["| check | count | examples | note |", "|---|---:|---|---|"]
    lines += [f"| {r['check']} | {r['count']} | {r['examples'] or '-'} | {r['note']} |" for r in rows]
    return "\n".join(lines)

"""Flag menus that don't belong to their restaurant, without deleting them.

Two lists feed it:
- data/seed/rejected_websites.csv (written by scripts/find_websites.py): search-matched
  websites that were wrong. Every menu of the restaurant is flagged "search_rejected" and its
  website cleared.
- data/seed/exclusions.csv (by hand): slug, normalized_hash, reason, website, note. A row
  with a normalized_hash flags only that menu (e.g. reason "shared_document", one restaurant's
  copy of a page another restaurant owns); without one, all the restaurant's menus. A row with
  a website clears that (wrong) website.

`menudb exclude` prints what would change; `--apply` sets Menu.excluded_reason, clears the
websites and moves the matching bucket items (pending/, processed/, extracted/, imported/)
under quarantine/; `--undo` reverses all three. Re-applying is harmless. Raw files under raw/
are left alone. Counts only, never menu text.
"""

from collections import Counter

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from menudb import storage
from menudb.db.models import Course, Menu, Restaurant

QUARANTINE = "quarantine/"
PREFIXES = ("pending/", "processed/", "extracted/", "imported/")


def items_from(rejected: list[dict], exclusions: list[dict]) -> list[dict]:
    """Both lists as [{slug, reason, hash, website}]; website is the one to clear."""
    return ([{"slug": r["slug"], "reason": "search_rejected", "hash": None, "website": r["website"]}
             for r in rejected]
            + [{"slug": r["slug"], "reason": r["reason"], "hash": r.get("normalized_hash") or None,
                "website": r.get("website") or None} for r in exclusions])


def bucket_keys(slug: str, nhash: str | None, undo: bool = False) -> list[str]:
    """Bucket keys for a restaurant (one menu with nhash): live ones, or quarantined with undo."""
    root = QUARANTINE if undo else ""
    keys = [k for p in PREFIXES for k in storage.list_keys(f"{root}{p}{slug}/")]
    return [k for k in keys if nhash is None or k.rsplit("/", 1)[-1].startswith(nhash)]


def exclude(session: Session, items: list[dict], apply: bool = False, undo: bool = False) -> Counter:
    """items: from items_from(). Prints nothing; returns counts."""
    ids = dict(session.execute(select(Restaurant.slug, Restaurant.id)
                               .where(Restaurant.slug.in_({i["slug"] for i in items}))).all())
    counts: Counter = Counter(items=len(items), restaurants_in_db=len(ids))
    menu_ids: set[int] = set()
    moves: list[str] = []
    for i in items:
        if i["slug"] not in ids:
            continue
        q = select(Menu.id).where(Menu.restaurant_id == ids[i["slug"]],
                                  Menu.excluded_reason == i["reason"] if undo
                                  else Menu.excluded_reason.is_(None))
        if i["hash"]:
            q = q.where(Menu.normalized_hash == i["hash"])
        found = set(session.scalars(q))
        menu_ids |= found
        counts[f"menus_{i['reason']}"] += len(found)
        keys = bucket_keys(i["slug"], i["hash"], undo)
        moves += keys
        counts.update(f"bucket_{k.removeprefix(QUARANTINE).split('/')[0]}" for k in keys)
        counts["websites_cleared"] += bool(i["website"])
    counts["menus"] = len(menu_ids)
    counts["restaurants_with_menus"] = session.scalar(
        select(func.count(func.distinct(Menu.restaurant_id))).where(Menu.id.in_(menu_ids))) or 0
    counts["courses"] = session.scalar(
        select(func.count()).select_from(Course).where(Course.menu_id.in_(menu_ids))) or 0
    if not apply:
        return counts
    for i in items:
        if i["slug"] not in ids:
            continue
        q = update(Menu).where(Menu.id.in_(menu_ids), Menu.restaurant_id == ids[i["slug"]])
        if i["hash"]:
            q = q.where(Menu.normalized_hash == i["hash"])
        session.execute(q.values(excluded_reason=None if undo else i["reason"]))
        if i["website"]:
            session.get(Restaurant, ids[i["slug"]]).website = i["website"] if undo else None
    session.commit()
    for k in moves:
        storage.move(k, k.removeprefix(QUARANTINE) if undo else QUARANTINE + k)
    counts["applied"] = 1
    return counts

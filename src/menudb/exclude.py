"""Flag menus from websites that were wrongly matched to a restaurant, without deleting them.

`menudb exclude --file data/seed/rejected_websites.csv` prints what would change; `--apply`
sets Menu.excluded_reason, clears Restaurant.website and moves the restaurants' bucket items
(pending/, processed/, extracted/, imported/) under quarantine/; `--undo` reverses all three.
Raw files under raw/ are left alone. Counts only, never menu text.
"""

from collections import Counter

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from menudb import storage
from menudb.db.models import Course, Menu, Restaurant

QUARANTINE = "quarantine/"
PREFIXES = ("pending/", "processed/", "extracted/", "imported/")


def bucket_keys(slugs: list[str], undo: bool = False) -> list[str]:
    """Bucket keys for these restaurants: live ones, or quarantined ones with undo."""
    root = QUARANTINE if undo else ""
    return [k for slug in slugs for p in PREFIXES for k in storage.list_keys(f"{root}{p}{slug}/")]


def exclude(session: Session, rejected: list[dict], reason: str, apply: bool = False,
            undo: bool = False) -> Counter:
    """rejected: rows of rejected_websites.csv (slug, website, ...)."""
    websites = {r["slug"]: r["website"] for r in rejected}
    ids = dict(session.execute(select(Restaurant.slug, Restaurant.id)
                               .where(Restaurant.slug.in_(list(websites)))).all())
    flagged = Menu.excluded_reason == reason if undo else Menu.excluded_reason.is_(None)
    menus = select(Menu.id).where(Menu.restaurant_id.in_(list(ids.values())), flagged)
    counts = Counter(
        rejected=len(websites), restaurants_in_db=len(ids),
        restaurants_with_menus=session.scalar(
            select(func.count(func.distinct(Menu.restaurant_id))).where(Menu.id.in_(menus))) or 0,
        menus=session.scalar(select(func.count()).select_from(menus.subquery())) or 0,
        courses=session.scalar(select(func.count()).select_from(Course)
                               .where(Course.menu_id.in_(menus))) or 0)
    keys = bucket_keys(list(ids), undo)
    counts.update(f"bucket_{k.removeprefix(QUARANTINE).split('/')[0]}" for k in keys)
    if not apply:
        return counts
    session.execute(update(Menu).where(Menu.id.in_(menus))
                    .values(excluded_reason=None if undo else reason))
    for slug, rid in ids.items():
        session.get(Restaurant, rid).website = websites[slug] if undo else None
    session.commit()
    for k in keys:
        storage.move(k, k.removeprefix(QUARANTINE) if undo else QUARANTINE + k)
    counts["applied"] = 1
    return counts

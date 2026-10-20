"""Flag menus that don't belong to their restaurant, without deleting them.

Two lists feed it:
- data/seed/rejected_websites.csv (written by scripts/find_websites.py): search-matched
  websites that were wrong. The restaurant's menus from that website are flagged
  "search_rejected", and the website cleared if the restaurant still has it.
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
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from menudb import storage
from menudb.db.models import Course, Menu, Restaurant, Snapshot

QUARANTINE = "quarantine/"
PREFIXES = ("pending/", "processed/", "extracted/", "imported/")


def items_from(rejected: list[dict], exclusions: list[dict]) -> list[dict]:
    """Both lists as [{slug, reason, hash, website}]; website is the one to clear."""
    return ([{"slug": r["slug"], "reason": "search_rejected", "hash": None, "website": r["website"]}
             for r in rejected]
            + [{"slug": r["slug"], "reason": r["reason"], "hash": r.get("normalized_hash") or None,
                "website": r.get("website") or None} for r in exclusions])


def bucket_keys(slug: str, hashes: set[str], undo: bool = False) -> list[str]:
    """Bucket keys of these menus of a restaurant: live ones, or quarantined with undo."""
    root = QUARANTINE if undo else ""
    keys = [k for p in PREFIXES for k in storage.list_keys(f"{root}{p}{slug}/")]
    return [k for k in keys if k.rsplit("/", 1)[-1].split(".")[0] in hashes]


def _host(url: str | None) -> str:
    return urlsplit(url or "").netloc.lower().removeprefix("www.")


def _menus(session: Session, rid: int, item: dict) -> list[tuple[int, str, str | None, str]]:
    """(id, normalized_hash, excluded_reason, host of its first snapshot) of the item's menus."""
    q = (select(Menu.id, Menu.normalized_hash, Menu.excluded_reason, Snapshot.url)
         .join(Snapshot, Menu.first_snapshot_id == Snapshot.id).where(Menu.restaurant_id == rid))
    if item["hash"]:
        q = q.where(Menu.normalized_hash == item["hash"])
    return [(m, h, r, _host(u)) for m, h, r, u in session.execute(q)]


def exclude(session: Session, items: list[dict], apply: bool = False, undo: bool = False) -> Counter:
    """items: from items_from(). Prints nothing; returns counts.

    A rejected website flags only the menus that came from it: many of these restaurants have
    since got their real website, whose menus stay. Menus an earlier version flagged although
    they came from another site are restored ("menus_restored")."""
    ids = dict(session.execute(select(Restaurant.slug, Restaurant.id)
                               .where(Restaurant.slug.in_({i["slug"] for i in items}))).all())
    counts: Counter = Counter(items=len(items), restaurants_in_db=len(ids))
    flag: dict[int, str | None] = {}  # menu id -> new excluded_reason
    moves: dict[str, str] = {}  # bucket key -> where it goes
    clear: dict[int, str | None] = {}  # restaurant id -> website to set
    for i in items:
        if i["slug"] not in ids:
            continue
        rid = ids[i["slug"]]
        wrong = _host(i["website"]) if i["reason"] == "search_rejected" else ""
        hit, back = set(), set()
        for m, h, reason, host in _menus(session, rid, i):
            from_wrong = not wrong or host == wrong
            if undo:
                if reason == i["reason"] and from_wrong:
                    flag[m], _ = None, back.add(h)
            elif from_wrong:
                if reason is None:
                    flag[m], _ = i["reason"], hit.add(h)
            elif reason == i["reason"]:  # flagged by the old rule; its menu came from elsewhere
                flag[m], _ = None, back.add(h)
                counts["menus_restored"] += 1
        counts[f"menus_{i['reason']}"] += len(hit) if not undo else len(back)
        for k in bucket_keys(i["slug"], hit):
            moves[k] = QUARANTINE + k
        for k in bucket_keys(i["slug"], back, undo=True):
            moves[k] = k.removeprefix(QUARANTINE)
        if i["website"]:
            current = session.get(Restaurant, rid).website
            if undo and not current:
                clear[rid] = i["website"]
            elif not undo and _host(current) == _host(i["website"]):
                clear[rid] = None
    counts.update(f"bucket_{k.removeprefix(QUARANTINE).split('/')[0]}" for k in moves)
    counts["websites_cleared"] = sum(w is None for w in clear.values())
    menu_ids = {m for m, r in flag.items() if (r is None) == undo}
    counts["menus"] = len(menu_ids)
    counts["restaurants_with_menus"] = session.scalar(
        select(func.count(func.distinct(Menu.restaurant_id))).where(Menu.id.in_(menu_ids))) or 0
    counts["courses"] = session.scalar(
        select(func.count()).select_from(Course).where(Course.menu_id.in_(menu_ids))) or 0
    if not apply:
        return counts
    for m, reason in flag.items():
        session.execute(update(Menu).where(Menu.id == m).values(excluded_reason=reason))
    for rid, website in clear.items():
        session.get(Restaurant, rid).website = website
    session.commit()
    for src, dst in moves.items():
        if storage.exists(src):  # else moved already by an earlier, interrupted run
            storage.move(src, dst)
    counts["applied"] = 1
    return counts

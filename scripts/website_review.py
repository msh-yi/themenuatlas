"""Websites a person approves: pages on other sites that carry a restaurant's name, which the
browser search (scripts/find_websites_browser.py) doesn't take on its own because most are
directory listings (mishumap.com/ja/nara/r/<name>/), though some are a group's page for one of
its restaurants (romecavalieri.com/it/la-pergola-it/).

    uv run python scripts/website_review.py triage   # move such pages out of restaurants_full.csv
    uv run python scripts/website_review.py apply    # set the ones marked approve=y

data/seed/website_review.csv has one row per restaurant: slug, name, city, country, website,
approve. Mark approve with y for the restaurant's own page (or n to keep it out), then run
`apply`: approved websites go into restaurants_full.csv with website_source=reviewed, and the
decided rows leave the review file.
"""

import csv
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "data/seed/restaurants_full.csv"
REVIEW = ROOT / "data/seed/website_review.csv"
FIELDS = ["slug", "name", "city", "country", "website", "approve"]


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    ending = "\r\n" if path.exists() and b"\r\n" in path.read_bytes()[:4096] else "\n"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fields, lineterminator=ending)
        w.writeheader()
        w.writerows(rows)


def add_candidates(rows: list[dict], review: Path = REVIEW) -> int:
    """Add review rows for restaurants not in the file yet. Returns how many were added."""
    old = _read(review)[1] if review.exists() else []
    have = {r["slug"] for r in old}
    new = [{k: r.get(k, "") for k in FIELDS} for r in rows if r["slug"] not in have]
    if new:
        _write(review, FIELDS, old + new)
    return len(new)


def triage(full: Path = FULL, review: Path = REVIEW) -> tuple[int, int]:
    """Browser-search websites that are a page on a site other than a hotel's: clear them in the
    list (website_source=browser_review) and add them to the review file. Returns (moved, kept)."""
    from find_websites_browser import hotel_host

    fields, rows = _read(full)
    moved, kept = [], 0
    for r in rows:
        if r["website_source"] != "search_browser":
            continue
        parts = urlsplit(r["website"])
        if parts.path.strip("/") and not hotel_host(parts.netloc):
            moved.append({**r, "approve": ""})
            r["website"], r["website_source"] = "", "browser_review"
        else:
            kept += 1
    _write(full, fields, rows)
    add_candidates(moved, review)
    return len(moved), kept


def apply(full: Path = FULL, review: Path = REVIEW) -> dict:
    """Set approved websites; drop decided rows (y or n) from the review file."""
    fields, rows = _read(full)
    by_slug = {r["slug"]: r for r in rows}
    pending, counts = [], {"approved": 0, "rejected": 0, "pending": 0}
    for r in _read(review)[1]:
        mark = (r.get("approve") or "").strip().lower()
        if mark in ("y", "yes", "1", "x") and r["slug"] in by_slug:
            row = by_slug[r["slug"]]
            row["website"], row["website_source"] = r["website"].strip(), "reviewed"
            counts["approved"] += 1
        elif mark in ("n", "no", "0"):
            counts["rejected"] += 1
        else:
            pending.append(r)
            counts["pending"] += 1
    _write(full, fields, rows)
    _write(review, FIELDS, pending)
    return counts


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "triage":
        moved, kept = triage()
        print(f"{moved} pages on other sites moved to {REVIEW.name}; {kept} browser-search websites kept")
    elif cmd == "apply":
        print(apply())
    else:
        sys.exit(__doc__)

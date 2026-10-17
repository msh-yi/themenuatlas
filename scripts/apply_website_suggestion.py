"""Apply an approved website suggestion (.github/ISSUE_TEMPLATE/website.yml) to the restaurant
lists. Run by website-suggestion.yml when a maintainer labels the issue `website-approved`.

    ISSUE_BODY=... uv run python scripts/apply_website_suggestion.py

Reads the issue form from $ISSUE_BODY (never from the command line, so nothing in it reaches a
shell), finds the restaurant, checks the link, and sets it as the restaurant's website in
data/seed/restaurants_full.csv (and restaurants.csv, if listed there) with
website_source=suggested. Writes `slug` and `message` to $GITHUB_OUTPUT; exits 1 when the
suggestion can't be applied, with the reason as the message.
"""

import csv
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from menudb.domains import not_official

ROOT = Path(__file__).resolve().parents[1]
LISTS = [ROOT / "data/seed/restaurants_full.csv", ROOT / "data/seed/restaurants.csv"]


class Rejected(Exception):
    pass


def parse_form(body: str) -> dict[str, str]:
    """GitHub renders an issue form as "### Label" sections; returns label -> value."""
    out, label = {}, None
    for line in (body or "").splitlines():
        if line.startswith("### "):
            label = line[4:].strip().lower()
            out[label] = ""
        elif label is not None:
            out[label] += line + "\n"
    return {k: "" if v.strip() == "_No response_" else v.strip() for k, v in out.items()}


def find_restaurant(rows: list[dict], value: str) -> dict:
    """By slug (as the restaurant page fills it in, or in a pasted site link), else by name."""
    value = value.strip()
    m = re.search(r"#/restaurant/([^/?#\s]+)", value)
    key = m.group(1) if m else value
    by_slug = {r["slug"]: r for r in rows}
    if key in by_slug:
        return by_slug[key]
    named = [r for r in rows if r["name"].strip().lower() == value.lower()]
    if len(named) == 1:
        return named[0]
    raise Rejected(f"no single restaurant matches {value[:80]!r}" if not named
                   else f"{len(named)} restaurants are called {value[:80]!r}; use the link from its page")


def check_link(value: str) -> str:
    url = value.strip().split()[0] if value.strip() else ""
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        raise Rejected("the link isn't a web address")
    if not_official(parts.netloc.lower().removeprefix("www.")):
        raise Rejected(f"{parts.netloc} is a guide, booking, social or hosting site, not the restaurant's own")
    return url


def apply(body: str, lists: list[Path] = LISTS) -> tuple[str, str]:
    """Returns (slug, url); raises Rejected."""
    form = parse_form(body)
    full = list(csv.DictReader(lists[0].open(newline="")))
    r = find_restaurant(full, form.get("restaurant", ""))
    url = check_link(form.get("link", ""))
    for path in lists:
        if not path.exists():
            continue
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        hit = False
        for row in rows:
            if row["slug"] == r["slug"]:
                row["website"], hit = url, True
                if "website_source" in row:
                    row["website_source"] = "suggested"
        if hit:
            ending = "\r\n" if b"\r\n" in path.read_bytes()[:4096] else "\n"  # keep the file's own
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fields, lineterminator=ending)
                w.writeheader()
                w.writerows(rows)
    return r["slug"], url


def main() -> int:
    out = os.environ.get("GITHUB_OUTPUT")
    try:
        slug, url = apply(os.environ.get("ISSUE_BODY", ""))
        message, code = f"Set `{slug}`'s website to {url}.", 0
    except Rejected as e:
        slug, message, code = "", f"Not applied: {e}.", 1
    print(message)
    if out:
        with open(out, "a") as f:
            f.write(f"slug={slug}\nmessage={' '.join(message.split())}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())

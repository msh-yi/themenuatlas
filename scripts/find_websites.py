"""Find official websites for restaurants in data/seed/restaurants_full.csv that have none,
using the Brave Search API (one query per restaurant, about $5 per 1,000 queries).

    BRAVE_SEARCH_API_KEY=... uv run python scripts/find_websites.py --cache <dir> [--limit N]

Picks the first result that isn't a directory, review, booking or social site and whose
domain or title shares a word with the restaurant's name. Results are cached under --cache
(outside the repo), so a rerun costs nothing for restaurants already searched.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from build_full_list import norm_name, slugify  # noqa: E402

API = "https://api.search.brave.com/res/v1/web/search"
NOT_OFFICIAL = re.compile(
    r"(?i)(tripadvisor|yelp|google\.|facebook|instagram|tiktok|twitter|x\.com|youtube|linkedin|"
    r"michelin|theworlds50best|the50\.com|opentable|resy|exploretock|sevenrooms|thefork|quandoo|"
    r"tablecheck|tabelog|gurunavi|retty|dianping|meituan|openrice|zomato|timeout|eater\.com|"
    r"infatuation|nytimes|wikipedia|wikidata|foursquare|ubereats|deliveroo|doordash|grubhub|"
    r"booking\.com|expedia|hotels\.com|agoda|trip\.com|gaultmillau|falstaff|restaurantguru|"
    r"menupix|sluurpy|wanderlog|lafourchette|cntraveler|forbes|condenast|guide\.|reddit|pinterest)"
)


def pick(name: str, results: list[dict]) -> str:
    words = {w for w in norm_name(name).split("-") if len(w) >= 3}
    for res in results:
        url = res.get("url", "")
        host = urlsplit(url).netloc.lower()
        if not host or NOT_OFFICIAL.search(host):
            continue
        hay = slugify(host + " " + res.get("title", ""))
        if not words or any(w in hay for w in words):
            return f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--csv", type=Path, default=Path("data/seed/restaurants_full.csv"))
    ap.add_argument("--limit", type=int, default=None, help="max new queries this run (cost control)")
    args = ap.parse_args()
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        sys.exit("set BRAVE_SEARCH_API_KEY")

    rows = list(csv.DictReader(args.csv.open()))
    fields = list(rows[0].keys())
    client = httpx.Client(headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30)
    (args.cache / "brave").mkdir(parents=True, exist_ok=True)
    queried = found = 0
    for r in rows:
        if r["website"]:
            continue
        path = args.cache / "brave" / f"{r['slug']}.json"
        if path.exists():
            results = json.loads(path.read_text())
        else:
            if args.limit is not None and queried >= args.limit:
                continue
            q = " ".join(x for x in (r["name"], r["city"], "restaurant") if x)
            resp = client.get(API, params={"q": q, "count": 10})
            if resp.status_code == 429:
                time.sleep(2)
                resp = client.get(API, params={"q": q, "count": 10})
            resp.raise_for_status()
            results = resp.json().get("web", {}).get("results", [])
            path.write_text(json.dumps(results))
            queried += 1
            time.sleep(1.1)  # free and base plans allow 1 query per second
        if site := pick(r["name"], results):
            r["website"], r["website_source"] = site, "search"
            found += 1
    with args.csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)
    print(f"{queried} new queries, {found} websites found", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Find official websites for restaurants in data/seed/restaurants_full.csv that have none,
with one web search per restaurant. Uses the Brave Search API when BRAVE_SEARCH_API_KEY is set
(about $5 per 1,000 queries), otherwise LangSearch (LANGSEARCH_API_KEY, free 1,000 a day;
LANGSEARCH_API_KEY_2 from a second account adds another 1,000).

    BRAVE_SEARCH_API_KEY=... uv run python scripts/find_websites.py --cache <dir> [--limit N]

A result counts only if its domain itself carries the restaurant's name (see official()).
Results are cached under --cache (outside the repo), so a rerun costs nothing for
restaurants already searched.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from build_full_list import norm_name  # noqa: E402

from menudb.domains import NOT_OFFICIAL, PLATFORM  # noqa: E402

BRAVE = "https://api.search.brave.com/res/v1/web/search"
LANGSEARCH = "https://api.langsearch.com/v1/web-search"


# Words too common in restaurant names to show a domain is this restaurant's own.
GENERIC = set("""restaurant restaurants ristorante restaurante trattoria osteria brasserie bistro
bistrot taverna cafe caffe kitchen table house room bar grill dining food foods sushi ramen club
chez maison casa hotel wine wines and the les des del della dal tea garden steak steakhouse
pizza pizzeria bakery noodle noodles cuisine cucina wok fish market""".split())


def domain_label(host: str) -> str:
    """The registrable name of a host, letters and digits only: www.le-bernardin.com -> lebernardin,
    noma.dk -> noma, foo.co.uk -> foo."""
    parts = host.lower().split(":")[0].removeprefix("www.").split(".")
    if len(parts) >= 3 and len(parts[-2]) <= 3:  # co.uk, com.au, ne.jp
        parts = parts[:-1]
    return re.sub(r"[^a-z0-9]", "", parts[-2] if len(parts) >= 2 else parts[0])


def official(name: str, city: str, url: str) -> bool:
    """Whether url plausibly is the restaurant's own site: not a directory, news, guide or
    hosting domain, and the domain itself carries the restaurant's name. A page title is not
    enough, since articles and directories name the restaurant in theirs."""
    host = urlsplit(url).netloc.lower()
    if not host or NOT_OFFICIAL.search(host) or PLATFORM.search(host):
        return False
    label = domain_label(host)
    city_words = {w for w in norm_name(city).split("-") if len(w) >= 3}
    words = [w for w in norm_name(name).split("-") if w and w not in GENERIC and w not in city_words]
    compact = "".join(words)
    core = label  # the domain without generic and city words: alinearestaurant -> alinea
    for w in sorted(GENERIC | city_words, key=len, reverse=True):
        core = core.replace(w, "")
    if len(core) < 3 or not compact:
        return False
    # The name makes up most of the domain, or the domain is part of the name, or one
    # distinctive name word makes up most of the domain (tinyurbankitchen is not Urban
    # Gastronomy's, jimbochoden is not checked as Den's).
    def most(part: str) -> bool:
        return len(part) >= 4 and part in core and 3 * len(part) >= 2 * len(core)
    return most(compact) or (len(core) >= 5 and core in compact) or any(most(w) for w in words)


def pick(name: str, city: str, results: list[dict]) -> str:
    """The first result whose domain is the restaurant's own (see official) and is a homepage
    or a shallow page of it, as a root URL."""
    for res in results:
        url = res.get("url", "")
        if official(name, city, url) and urlsplit(url).path.strip("/").count("/") <= 1:
            return f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
    return ""


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def revalidate(rows: list[dict], rejected_path: Path, status_path: Path, recipes_dir: Path) -> list[str]:
    """Drop search-sourced websites that fail `official` or share a host with another
    restaurant. Each one is recorded in rejected_path (so `menudb exclude` can flag its menus),
    its website cleared with website_source=search_rejected, and its discovery status and
    auto recipe removed. Returns the rejected slugs."""
    hosts = Counter(host_of(r["website"]) for r in rows if r["website"])
    rejected = []
    for r in rows:
        if r["website_source"] != "search":
            continue
        reason = ("not_official" if not official(r["name"], r["city"], r["website"])
                  else "shared_host" if hosts[host_of(r["website"])] > 1 else "")
        if reason:
            rejected.append({"slug": r["slug"], "website": r["website"], "reason": reason,
                             "rejected": date.today().isoformat()})
            r["website"], r["website_source"] = "", "search_rejected"
    if not rejected:
        return []
    old = list(csv.DictReader(rejected_path.open())) if rejected_path.exists() else []
    with rejected_path.open("w", newline="") as f:
        w = csv.DictWriter(f, ["slug", "website", "reason", "rejected"])
        w.writeheader()
        w.writerows(old + rejected)
    slugs = {x["slug"] for x in rejected}
    if status_path.exists():
        status = list(csv.DictReader(status_path.open()))
        with status_path.open("w", newline="") as f:
            w = csv.DictWriter(f, list(status[0].keys()))
            w.writeheader()
            w.writerows(x for x in status if x["slug"] not in slugs)
    for slug in slugs:
        (recipes_dir / f"{slug}.yaml").unlink(missing_ok=True)
    return sorted(slugs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--csv", type=Path, default=Path("data/seed/restaurants_full.csv"))
    ap.add_argument("--limit", type=int, default=None, help="max new queries this run (cost control)")
    ap.add_argument("--rejected", type=Path, default=Path("data/seed/rejected_websites.csv"))
    ap.add_argument("--status", type=Path, default=Path("data/seed/discovery.csv"))
    ap.add_argument("--recipes-dir", type=Path, default=Path("data/recipes_auto"))
    ap.add_argument("--revalidate-only", action="store_true",
                    help="only re-check existing search-sourced websites; no searches, no key needed")
    args = ap.parse_args()
    rows = list(csv.DictReader(args.csv.open()))
    fields = list(rows[0].keys())

    def write_rows():
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(rows)

    dropped = revalidate(rows, args.rejected, args.status, args.recipes_dir)
    print(f"{len(dropped)} search-sourced websites rejected", file=sys.stderr)
    if args.revalidate_only:
        write_rows()
        return
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    # Brave when its key is set: LangSearch rarely returns a restaurant's own site.
    lang_keys = [] if brave_key else [
        k for k in (os.environ.get("LANGSEARCH_API_KEY"), os.environ.get("LANGSEARCH_API_KEY_2")) if k]
    if not (lang_keys or brave_key):
        sys.exit("set LANGSEARCH_API_KEY or BRAVE_SEARCH_API_KEY")
    per_key = 950  # stay under LangSearch's free 1,000 queries a day per account
    if lang_keys and args.limit is None:
        args.limit = per_key * len(lang_keys)
    lang_key = lang_keys[0] if lang_keys else None

    def search(q: str) -> list[dict]:
        """Results as [{url, title}] from whichever provider is configured. With two LangSearch
        keys, the second takes over after per_key queries or when the first is out of quota."""
        nonlocal lang_key
        if lang_key and queried > per_key and lang_key == lang_keys[0] and len(lang_keys) > 1:
            lang_key = lang_keys[1]
        for attempt in range(3):
            if lang_key:
                resp = client.post(LANGSEARCH, json={"query": q, "count": 10},
                                   headers={"Authorization": f"Bearer {lang_key}"})
            else:
                resp = client.get(BRAVE, params={"q": q, "count": 10},
                                  headers={"X-Subscription-Token": brave_key, "Accept": "application/json"})
            if resp.status_code == 429 or resp.status_code >= 500:
                if resp.status_code == 429 and attempt == 2 and lang_key and lang_key != lang_keys[-1]:
                    lang_key = lang_keys[lang_keys.index(lang_key) + 1]
                    return search(q)
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            if lang_key:
                pages = (resp.json().get("data") or {}).get("webPages", {}).get("value", [])
                return [{"url": p.get("url", ""), "title": p.get("name", "")} for p in pages]
            return [{"url": r.get("url", ""), "title": r.get("title", "")}
                    for r in resp.json().get("web", {}).get("results", [])]
        raise RuntimeError(f"search provider: HTTP {resp.status_code}")

    client = httpx.Client(timeout=30)
    taken = {host_of(r["website"]) for r in rows if r["website"]}
    # One cache per provider, so a Brave run re-searches what LangSearch got wrong.
    cache_dir = args.cache / ("search" if lang_key else "brave")
    cache_dir.mkdir(parents=True, exist_ok=True)
    queried = found = errors = 0
    for r in rows:
        if r["website"]:
            continue
        path = cache_dir / f"{r['slug']}.json"
        if lang_key and r["website_source"] == "search_rejected" and not path.exists():
            continue  # searched with LangSearch before; its results are gone, don't pay twice
        if path.exists():
            results = json.loads(path.read_text())
        else:
            if args.limit is not None and queried >= args.limit:
                continue
            q = " ".join(x for x in (r["name"], r["city"], "restaurant") if x)
            queried += 1
            try:
                results = search(q)
            except (RuntimeError, httpx.HTTPError) as e:
                errors += 1
                print(f"{r['slug']}: {e}", file=sys.stderr)
                if errors >= 10:
                    print("10 search errors, stopping early", file=sys.stderr)
                    break
                continue
            path.write_text(json.dumps(results))
            if queried % 100 == 0:
                print(f"{queried} queries, {found} websites found so far", file=sys.stderr, flush=True)
            time.sleep(1.1)  # both free plans allow 1 query per second
        if (site := pick(r["name"], r["city"], results)) and host_of(site) not in taken:
            r["website"], r["website_source"] = site, "search"
            taken.add(host_of(site))
            found += 1
    write_rows()
    print(f"{queried} new queries ({errors} errors), {found} websites found", file=sys.stderr)
    if queried and errors == queried:
        sys.exit("every search failed")


if __name__ == "__main__":
    main()

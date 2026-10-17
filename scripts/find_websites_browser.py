"""Search Google (or DuckDuckGo) in your own Chrome for the websites of restaurants that still
have none. Run it on your own machine (it opens a visible Chrome window through DrissionPage):

    uv run --with DrissionPage python scripts/find_websites_browser.py --cache ~/.cache/menudb-lists [--limit 100] [--country JP] [--engine ddg]

It reads the results page one query at a time, with a pause of 10 to 20 seconds (Google) or 6
to 12 (DuckDuckGo's plain HTML page) between queries, in the engine's region for the
restaurant's country. If Google asks for cookie consent, accept it once in the window.

A result counts (pick_loose) when the restaurant's name, or a distinctive word of it, appears
anywhere in the domain, or in the path of a page up to four levels deep, so a restaurant's page
on a hotel or group site counts too (fourseasons.com/tokyo/dining/restaurants/den/). Guide,
news, booking and social hosts and article-like paths (/2026/, /news/, /best-...) never count,
and Japanese names are compared across romanisation spellings. This is looser than
scripts/find_websites.py, which runs unreviewed in Actions, so review the matches. A homepage
whose title carries the name in its own script (from Wikidata) also counts. It never opens a
restaurant's site; run `menudb discover --js --to-bucket` afterwards for the menus.

If the engine shows its bot check (Google's "unusual traffic" page, DuckDuckGo's challenge),
the script saves what it has and stops. It doesn't try to get past it: wait a few hours (or
until tomorrow) and run it again. Results are cached per restaurant under --cache/<engine>/, so
a rerun picks up where it stopped. Matches get website_source=search_browser and are listed in
<cache>/browser_matches.csv for review.
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

sys.path.insert(0, str(Path(__file__).parent))
from build_full_list import UA, norm_name  # noqa: E402
from find_websites import (  # noqa: E402
    GENERIC, NOT_OFFICIAL, PLATFORM, domain_label, native_names, pick_native, romaji_fold)

DDG = "https://html.duckduckgo.com/html/"
GOOGLE = "https://www.google.com/search"
# DuckDuckGo's region codes (kl) for each country.
REGION = {"JP": "jp-jp", "CN": "cn-zh", "HK": "hk-tzh", "MO": "hk-tzh", "TW": "tw-tzh", "KR": "kr-kr",
          "TH": "th-th", "SG": "sg-en", "FR": "fr-fr", "MC": "fr-fr", "IT": "it-it", "DE": "de-de",
          "AT": "at-de", "CH": "ch-de", "ES": "es-es", "PT": "pt-pt", "BR": "br-pt", "MX": "mx-es",
          "AR": "ar-es", "CL": "cl-es", "NL": "nl-nl", "BE": "be-fr", "DK": "dk-da", "SE": "se-sv",
          "NO": "no-no", "FI": "fi-fi", "PL": "pl-pl", "US": "us-en", "GB": "uk-en", "IE": "ie-en",
          "CA": "ca-en", "AU": "au-en", "NZ": "nz-en"}
# A bot check rather than results: DuckDuckGo's challenge, Google's "unusual traffic" page.
_CHALLENGE = ("anomaly-modal", "bots use DuckDuckGo too", "challenge-form", "Unfortunately, bots",
              'id="captcha-form"', "g-recaptcha", "/sorry/index", "unusual traffic from your computer")


class BotCheck(Exception):
    pass


def search_url(query: str, country: str, engine: str = "ddg") -> str:
    if engine == "google":
        # gl: results as seen from that country; 20 results, so an official site below the
        # directories still shows
        return f"{GOOGLE}?q={quote_plus(query)}&num=20" + (f"&gl={country.lower()}" if country else "")
    kl = REGION.get(country)
    return f"{DDG}?q={quote_plus(query)}" + (f"&kl={kl}" if kl else "")


def parse_google(html: str) -> list[dict]:
    """[{url, title}] from a Google results page: each organic result is a link around an h3."""
    if any(marker in html for marker in _CHALLENGE):
        raise BotCheck()
    out, seen = [], set()
    for h3 in LexborHTMLParser(html).css("h3"):
        a = h3.parent
        while a is not None and a.tag != "a":
            a = a.parent
        href = (a.attributes.get("href") or "") if a is not None else ""
        if href.startswith("/url?"):  # older markup: /url?q=<real url>
            href = parse_qs(urlsplit(href).query).get("q", [""])[0]
        host = urlsplit(href).netloc
        if href.startswith("http") and "google." not in host and href not in seen:
            seen.add(href)
            out.append({"url": href, "title": h3.text(strip=True)})
    return out


def parse_results(html: str) -> list[dict]:
    """[{url, title}] from a DuckDuckGo HTML results page. Raises BotCheck on the bot check."""
    if any(marker in html for marker in _CHALLENGE):
        raise BotCheck()
    out = []
    for a in LexborHTMLParser(html).css("a.result__a"):
        href = a.attributes.get("href") or ""
        if "duckduckgo.com/l/" in href:  # a redirect link carrying the real URL in uddg
            href = parse_qs(urlsplit(href).query).get("uddg", [""])[0]
        if href.startswith("http") and "duckduckgo.com/y.js" not in href:  # skip ads
            out.append({"url": href, "title": a.text(strip=True)})
    return out


# Paths that are articles, lists or reviews about restaurants, not a restaurant's own page.
_ARTICLE = re.compile(r"(?i)(^|[/_-])(19|20)\d\d([/_-]|$)|(^|/)(news|blogs?|articles?|stor(y|ies)|magazine|"
                      r"best|top|guides?|lists?|reviews?|press|awards?|tags?|category|search|jobs?|careers?)([/_-]|$)")


# News and food-media hosts the shared blocklist doesn't name: fine as a domain match, but their
# pages about a restaurant are articles, so a name in their path doesn't count.
_MEDIA = re.compile(r"(?i)(tribune|herald|gazette|chronicle|observer|daily|insider|thrillist|eater|forbes|"
                    r"vogue|esquire|tatler|travel+er|gourmet|foodie|zagat|bloomberg|cnn|bbc|nymag|newyorker|"
                    r"latimes|wsj|scmp|asahi|nikkei|mainichi|yomiuri|hotpepper|gnavi|ozmall|ikyu|savor|"
                    r"tasteatlas|lemonde|lefigaro|repubblica|corriere|elpais|spiegel|zeit)")


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_keys(name: str, city: str, romaji: bool = False) -> tuple[list[str], str]:
    """The restaurant's distinctive name words (common words and the city's name left out,
    unless that leaves nothing) and those words joined, folded like the URLs they're matched to."""
    fold = romaji_fold if romaji else (lambda t: t)
    city_words = {fold(w) for w in norm_name(city).split("-") if len(w) >= 3}
    generic = {fold(w) for w in GENERIC}
    every = [fold(w) for w in norm_name(name).split("-") if w]
    words = [w for w in every if w not in generic and w not in city_words] or every
    return words, "".join(words)


def pick_loose(name: str, city: str, results: list[dict], romaji: bool = False) -> str:
    """The first result whose domain, or whose path up to four levels deep, carries the name.

    Domain: the joined name anywhere in it (4+ letters: alinearestaurant, sushisaito-ginza),
    the domain's own name inside the restaurant's (4+), or one distinctive name word of 5+
    letters (gordonramsayrestaurants for Restaurant Gordon Ramsay). Returns the homepage.
    Path: the joined name (5+) anywhere in it, a path segment that is the name, or one name word
    of 6+ letters, on a page that isn't an article or list. Returns that page, so a restaurant
    inside a hotel or group site gets its own page as its website."""
    fold = romaji_fold if romaji else (lambda t: t)
    words, compact = name_keys(name, city, romaji)
    if not compact:
        return ""
    for res in results:
        parts = urlsplit(res.get("url", ""))
        host = parts.netloc.lower()
        if not host or NOT_OFFICIAL.search(host) or PLATFORM.search(host):
            continue
        label = fold(domain_label(host))
        if ((len(compact) >= 4 and compact in label) or (len(label) >= 4 and label in compact)
                or any(len(w) >= 5 and w in label for w in words)):
            return f"{parts.scheme}://{parts.netloc}/"
        segs = [s for s in parts.path.split("/") if s]
        if not segs or len(segs) > 4 or _ARTICLE.search(parts.path) or _MEDIA.search(host):
            continue
        path = fold(_alnum(parts.path))
        if ((len(compact) >= 5 and compact in path) or compact in {fold(_alnum(s)) for s in segs}
                or any(len(w) >= 6 and w in path for w in words)):
            return f"{parts.scheme}://{parts.netloc}{parts.path}"
    return ""


def run(rows: list[dict], fetch_html: Callable[[str], str], cache: Path, natives: dict,
        limit: int | None = None, countries: set[str] | None = None, engine: str = "ddg",
        pause: Callable[[], None] | None = None) -> tuple[list[dict], str]:
    """Search for every row without a website; set the ones found. Returns (matches, why it
    stopped: "done", "limit" or "bot check")."""
    low, high = (10, 20) if engine == "google" else (6, 12)
    pause = pause or (lambda: time.sleep(random.uniform(low, high)))
    parse = parse_google if engine == "google" else parse_results
    cache_dir = cache / engine
    cache_dir.mkdir(parents=True, exist_ok=True)
    # websites in use: a group or hotel host may serve several restaurants, each its own page
    taken = {r["website"].rstrip("/").lower() for r in rows if r["website"]}
    found: list[dict] = []
    queried = 0

    def results(slug: str, tag: str, query: str, country: str) -> list[dict] | None:
        nonlocal queried
        path = cache_dir / f"{slug}.{tag}.json"
        if path.exists():
            return json.loads(path.read_text())
        if limit is not None and queried >= limit:
            return None
        if queried:
            pause()
        queried += 1
        res = parse(fetch_html(search_url(query, country, engine)))
        path.write_text(json.dumps(res))
        return res

    stopped = "done"
    for r in rows:
        if r["website"] or (countries and r["country"] not in countries):
            continue
        cc = r["country"]
        native = natives.get((norm_name(r["name"]), cc), "")
        site, how = "", ""
        try:
            res = results(r["slug"], "name", " ".join(x for x in (r["name"], r["city"]) if x), cc)
            if res is None:
                stopped = "limit"
                break
            if site := pick_loose(r["name"], r["city"], res, romaji=cc == "JP"):
                how = "name"
            elif native:
                res = results(r["slug"], "native", native, cc) or []
                if site := pick_loose(r["name"], r["city"], res, romaji=cc == "JP"):
                    how = "native name"
                elif site := pick_native(native, res):
                    how = "native name in homepage title"
        except BotCheck:
            stopped = "bot check"
            break
        if site and site.rstrip("/").lower() not in taken:
            r["website"], r["website_source"] = site, "search_browser"
            taken.add(site.rstrip("/").lower())
            found.append({"slug": r["slug"], "name": r["name"], "city": r["city"], "country": cc,
                          "native": native, "website": site, "how": how})
            print(f"{r['slug']}: {site} ({how})", flush=True)
    print(f"{queried} searches, {len(found)} websites found; stopped: {stopped}", file=sys.stderr)
    return found, stopped


def why(slug: str, csv_path: Path, cache: Path, engine: str) -> str:
    """Each cached result for one restaurant, and whether pick_loose takes it."""
    row = next((r for r in csv.DictReader(csv_path.open(newline="")) if r["slug"] == slug), None)
    if row is None:
        return f"no restaurant {slug!r} in {csv_path}"
    print(f"{row['name']} ({row['city']}, {row['country']}), website now: {row['website'] or '-'}")
    print(f"name words compared: {name_keys(row['name'], row['city'], row['country'] == 'JP')[0]}")
    files = sorted((cache / engine).glob(f"{slug}.*.json"))
    if not files:
        return f"no cached {engine} results for {slug}: run a search first"
    for f in files:
        print(f"-- {f.name}")
        for res in json.loads(f.read_text()):
            got = pick_loose(row["name"], row["city"], [res], row["country"] == "JP")
            print(f"  {'MATCH ' if got else '      '}{res['url']}")
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", type=Path, required=True, help="outside the repo, e.g. ~/.cache/menudb-lists")
    ap.add_argument("--csv", type=Path, default=Path("data/seed/restaurants_full.csv"))
    ap.add_argument("--limit", type=int, default=None, help="max searches this run")
    ap.add_argument("--country", action="append", help="only these countries (repeatable), e.g. JP")
    ap.add_argument("--engine", choices=["google", "ddg"], default="google")
    ap.add_argument("--why", metavar="SLUG", help="show a restaurant's cached results and which would match; no search")
    args = ap.parse_args()
    if args.why:
        sys.exit(why(args.why, args.csv, args.cache, args.engine))

    from DrissionPage import ChromiumPage  # only needed here: tests run without it

    with args.csv.open(newline="") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    args.cache.mkdir(parents=True, exist_ok=True)
    natives = native_names(args.cache, httpx.Client(headers={"User-Agent": UA}))
    page = ChromiumPage()

    def fetch_html(url: str) -> str:
        page.get(url)
        if "consent.google." in page.url:  # Google's cookie consent, shown once in Europe
            input("Accept Google's cookie dialog in the Chrome window, then press Enter here: ")
            page.get(url)
        return page.html

    try:
        found, stopped = run(rows, fetch_html, args.cache, natives, args.limit,
                             set(args.country) if args.country else None, args.engine)
    finally:
        page.quit()
    with args.csv.open("w", newline="") as f:  # csv's default \r\n, as the file has
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)
    review = args.cache / "browser_matches.csv"
    old = list(csv.DictReader(review.open())) if review.exists() else []
    with review.open("w", newline="") as f:
        w = csv.DictWriter(f, ["slug", "name", "city", "country", "native", "website", "how"])
        w.writeheader()
        w.writerows(old + found)
    print(f"review the matches in {review}", file=sys.stderr)
    if stopped == "bot check":
        sys.exit(f"{args.engine} showed its bot check: progress is saved; try again in a few hours")


if __name__ == "__main__":
    main()

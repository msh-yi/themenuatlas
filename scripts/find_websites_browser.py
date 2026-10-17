"""Search Google (or DuckDuckGo) in your own Chrome for the websites of restaurants that still
have none. Run it on your own machine (it opens a visible Chrome window through DrissionPage):

    uv run --with DrissionPage python scripts/find_websites_browser.py --cache ~/.cache/menudb-lists [--limit 100] [--country JP] [--engine ddg]

It reads the results page one query at a time, with a pause of 10 to 20 seconds (Google) or 6
to 12 (DuckDuckGo's plain HTML page) between queries, in the engine's region for the
restaurant's country. If Google asks for cookie consent, accept it once in the window. A result counts only under the same rules as scripts/find_websites.py: the domain
carries the restaurant's name (across romanisation spellings for Japan), or a homepage's title
carries its name in its own script (from Wikidata). It never opens a restaurant's site; run
`menudb discover --js --to-bucket` afterwards for the menus.

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
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

sys.path.insert(0, str(Path(__file__).parent))
from build_full_list import UA, norm_name  # noqa: E402
from find_websites import host_of, native_names, pick, pick_native  # noqa: E402

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
    taken = {host_of(r["website"]) for r in rows if r["website"]}
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
            if site := pick(r["name"], r["city"], res, romaji=cc == "JP"):
                how = "name"
            elif native:
                res = results(r["slug"], "native", native, cc) or []
                if site := pick(r["name"], r["city"], res, romaji=cc == "JP"):
                    how = "native name"
                elif site := pick_native(native, res):
                    how = "native name in homepage title"
        except BotCheck:
            stopped = "bot check"
            break
        if site and host_of(site) not in taken:
            r["website"], r["website_source"] = site, "search_browser"
            taken.add(host_of(site))
            found.append({"slug": r["slug"], "name": r["name"], "city": r["city"], "country": cc,
                          "native": native, "website": site, "how": how})
            print(f"{r['slug']}: {site} ({how})", flush=True)
    print(f"{queried} searches, {len(found)} websites found; stopped: {stopped}", file=sys.stderr)
    return found, stopped


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", type=Path, required=True, help="outside the repo, e.g. ~/.cache/menudb-lists")
    ap.add_argument("--csv", type=Path, default=Path("data/seed/restaurants_full.csv"))
    ap.add_argument("--limit", type=int, default=None, help="max searches this run")
    ap.add_argument("--country", action="append", help="only these countries (repeatable), e.g. JP")
    ap.add_argument("--engine", choices=["google", "ddg"], default="google")
    args = ap.parse_args()

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

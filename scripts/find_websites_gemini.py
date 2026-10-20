"""Ask Gemini, grounded with Google Search, for the official websites of restaurants that still
have none, then check each answer by opening the page. Needs GEMINI_API_KEY (a Google AI Studio
key; grounded queries are free up to the tier's monthly quota):

    uv run python scripts/find_websites_gemini.py --cache <dir> [--from 300] [--limit 50] [--slug x]

Gemini is used only as a search engine here: one query per restaurant, in the restaurant's own
language, asking for the URL of its own site (or its page on its hotel's, resort's or
restaurant group's site) and whether it has closed. Never for menus or extraction.

Each answer is checked before it counts: the page is fetched with the project's Fetcher
(honest User-Agent, robots.txt, 1 s per host); directory, guide, booking, social and news hosts
(menudb.domains), parked or for-sale domains, redirects to another site and anything in
data/seed/rejected_websites.csv are dropped; then the page must carry the restaurant's name
(a distinctive word of it, or the name in its own script) and its city. Pages that pass go to
data/seed/website_review.csv with approve=y; pages that carry the name but not the city go
there with approve blank, for a person to decide. Then run `scripts/website_review.py apply`.

--from/--limit select rows of the no-website list in restaurants_full.csv order (the same
numbering as the hand-search batches). Answers are cached per restaurant under --cache, so a
rerun picks up where it stopped. Prints slugs, statuses and hosts only.
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
from build_full_list import norm_name  # noqa: E402
from find_websites import GENERIC  # noqa: E402
from website_review import FIELDS, REVIEW, _read, _write  # noqa: E402

from menudb.domains import NOT_OFFICIAL, PLATFORM  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "data/seed/restaurants_full.csv"
REJECTED = ROOT / "data/seed/rejected_websites.csv"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LANGUAGE = {"JP": "Japanese", "CN": "Simplified Chinese", "HK": "Traditional Chinese", "MO": "Traditional Chinese",
            "TW": "Traditional Chinese", "KR": "Korean", "TH": "Thai", "FR": "French", "DE": "German",
            "AT": "German", "IT": "Italian", "ES": "Spanish", "PT": "Portuguese", "BR": "Portuguese",
            "NL": "Dutch", "BE": "French or Dutch", "DK": "Danish", "SE": "Swedish", "NO": "Norwegian",
            "PL": "Polish", "GR": "Greek", "TR": "Turkish", "VN": "Vietnamese", "MX": "Spanish",
            "AR": "Spanish", "PE": "Spanish", "CO": "Spanish", "HR": "Croatian", "HU": "Hungarian",
            "EE": "Estonian", "CZ": "Czech", "FI": "Finnish"}
PARKED = re.compile(r"(?i)(domain (name )?is for sale|buy this domain|this domain may be for sale|"
                    r"parked free|domain parking|hugedomains|sedo\.com|dan\.com|afternic|"
                    r"forsale\.godaddy|godaddy\.com/domainsearch|recursive dns)")
_NATIVE = re.compile(r"[฀-๿぀-ヿ㐀-鿿가-힯]+")
_JSON = re.compile(r"\{[^{}]*\}")


def prompt(r: dict) -> str:
    # A prose answer ending in one JSON line: asked for JSON only, the model answers from memory
    # without searching (no groundingMetadata), and its "closed" guesses are often wrong.
    lang = LANGUAGE.get(r["country"], "")
    local = f" Search in {lang} as well as English, using the restaurant's name in its own script." if lang else ""
    return (
        f"What is the official website of the restaurant \"{r['name']}\" in {r['city']}, {r['country']} "
        f"(a Michelin-starred or similarly listed restaurant)? Is it still open?{local}\n"
        "Accept only the restaurant's own website, or the page for this restaurant on the website of "
        "its own hotel, resort or restaurant group. Never give a directory, guide, review, booking, "
        "social-media, news, blog, tourism or Wikipedia page. Say it has closed only if a source says so.\n"
        'End your answer with one line of JSON: {"website": "<url or empty>", "closed": true|false, '
        '"native_name": "<the name in its own script, or empty>"}'
    )


def ask(client: httpx.Client, key: str, model: str, text: str, sleep=time.sleep) -> dict:
    """One grounded query. Returns {website, closed, native_name, grounded, sources}: grounded says
    whether the model searched at all; sources are the grounding pages' titles (domains)."""
    for attempt in range(3):  # grounded answers sometimes take minutes, or fail with a 5xx
        try:
            res = client.post(API.format(model=model), headers={"x-goog-api-key": key},
                              json={"contents": [{"parts": [{"text": text}]}], "tools": [{"google_search": {}}]})
        except httpx.TimeoutException:
            if attempt == 2:
                raise
            continue
        if res.status_code < 500 or attempt == 2:
            break
        sleep(10)
    if res.status_code == 429:
        raise RuntimeError("quota")
    res.raise_for_status()
    cand = (res.json().get("candidates") or [{}])[0]
    answer = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    got = {}
    for m in reversed(_JSON.findall(answer)):  # the last JSON object in the answer
        try:
            got = json.loads(m)
            break
        except json.JSONDecodeError:
            continue
    meta = cand.get("groundingMetadata", {})
    chunks = meta.get("groundingChunks", [])
    return {"website": str(got.get("website") or "").strip(), "closed": bool(got.get("closed")),
            "native_name": str(got.get("native_name") or "").strip(),
            "grounded": bool(meta.get("webSearchQueries") or chunks),
            "sources": [c.get("web", {}).get("title", "") for c in chunks][:10]}


def name_words(name: str) -> list[str]:
    words = [w for w in norm_name(name).split("-") if len(w) >= 3]
    return [w for w in words if w not in GENERIC] or words


def check(r: dict, url: str, native: str, fetcher, rejected: set[tuple[str, str]]) -> tuple[str, str]:
    """(verdict, final url): verdict is ok (name and city on the page), name_only, or why not."""
    host = urlsplit(url).netloc.lower()
    if not url.startswith("http") or not host:
        return "no_url", ""
    if NOT_OFFICIAL.search(host) or PLATFORM.search(host):
        return "not_official_host", url
    if (r["slug"], url.rstrip("/")) in rejected:
        return "rejected_before", url
    try:
        res = fetcher.get(url, retries=0)
    except Exception as e:
        return f"fetch_{type(e).__name__}", url
    final = res.final_url
    fhost = urlsplit(final).netloc.lower()
    if fhost.removeprefix("www.") != host.removeprefix("www."):
        if NOT_OFFICIAL.search(fhost) or PLATFORM.search(fhost) or PARKED.search(fhost):
            return "redirected_away", final
    html = res.content.decode(errors="replace")
    if PARKED.search(html[:50000]) or PARKED.search(final):
        return "parked", final
    text = html.lower()
    compact = re.sub(r"[^a-z0-9]", "", text)
    has_name = any(w.replace("-", "") in compact for w in name_words(r["name"]))
    natives = [n for n in [native, *_NATIVE.findall(r["name"])] if n]
    has_name = has_name or any(n in html for n in natives)
    if not has_name:
        return "name_missing", final
    city = [w for w in norm_name(r["city"]).split("-") if len(w) >= 4]
    has_city = any(w in compact for w in city) or any(c in html for c in _NATIVE.findall(r["city"]))
    return ("ok" if has_city else "name_only"), final


def todo(full: Path, start: int, limit: int | None, slugs: list[str] | None) -> list[dict]:
    rows = [r for r in csv.DictReader(full.open(newline="")) if not r["website"]]
    rows = rows[start:]
    if slugs:
        rows = [r for r in rows if r["slug"] in slugs]
    return rows[:limit] if limit else rows


def run(rows: list[dict], ask_one, fetcher, cache: Path, review: Path = REVIEW,
        rejected_path: Path = REJECTED, sleep=time.sleep, interval: float = 6.0) -> dict:
    """Query, check and record each restaurant; returns counts by verdict."""
    cache.mkdir(parents=True, exist_ok=True)
    rejected = {(x["slug"], x["website"].rstrip("/")) for x in csv.DictReader(rejected_path.open(newline=""))}
    counts: dict[str, int] = {}
    for n, r in enumerate(rows, 1):
        path = cache / f"{r['slug']}.json"
        if path.exists():
            got = json.loads(path.read_text())
        else:
            got = ask_one(prompt(r))
            if not got.get("grounded", True):  # answered without searching: ask once more
                sleep(interval)
                got = ask_one(prompt(r))
            path.write_text(json.dumps(got, ensure_ascii=False))
            sleep(interval)
        if got.get("closed") and got.get("grounded", True):
            verdict, final = "closed", ""
        elif not got.get("website") and not got.get("grounded", True):
            verdict, final = "not_searched", ""
        else:
            verdict, final = check(r, got.get("website", ""), got.get("native_name", ""), fetcher, rejected)
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict in ("ok", "name_only"):
            record(review, {"slug": r["slug"], "name": r["name"], "city": r["city"], "country": r["country"],
                            "website": final, "approve": "y" if verdict == "ok" else ""})
        print(f"[{n}/{len(rows)}] {r['slug']}: {verdict} {urlsplit(final).netloc}", flush=True)
    return counts


def record(review: Path, row: dict) -> None:
    """Add or replace one restaurant's row in the review file, saved at once so a stop loses nothing."""
    old = _read(review)[1] if review.exists() else []
    _write(review, FIELDS, [x for x in old if x["slug"] != row["slug"]] + [row])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", type=Path, required=True, help="per-restaurant answers, for resuming")
    ap.add_argument("--from", dest="start", type=int, default=0, help="first row of the no-website list")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--slug", action="append")
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--interval", type=float, default=6.0, help="seconds between queries")
    args = ap.parse_args()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set")
    os.chdir(ROOT)
    from menudb.config import get_settings
    from menudb.fetch.http import Fetcher

    rows = todo(FULL, args.start, args.limit, args.slug)
    print(f"{len(rows)} restaurants", file=sys.stderr)
    with httpx.Client(timeout=180) as client, Fetcher(get_settings().user_agent) as fetcher:
        try:
            counts = run(rows, lambda t: ask(client, key, args.model, t), fetcher, args.cache,
                         interval=args.interval)
        except RuntimeError as e:
            sys.exit(f"stopped: {e} (rerun later; answers so far are cached)")
    print(" ".join(f"{k}: {v}" for k, v in sorted(counts.items())), file=sys.stderr)


if __name__ == "__main__":
    main()

"""Find official websites for restaurants in data/seed/restaurants_full.csv that have none,
with one web search per restaurant. Uses the Brave Search API when BRAVE_SEARCH_API_KEY is set
(about $5 per 1,000 queries), otherwise LangSearch (LANGSEARCH_API_KEY, free 1,000 a day;
LANGSEARCH_API_KEY_2 from a second account adds another 1,000).

    BRAVE_SEARCH_API_KEY=... uv run python scripts/find_websites.py --cache <dir> [--limit N]

A result counts only if its domain itself carries the restaurant's name (see official()).

--local is a second pass for the restaurants the first missed (Brave only): each search is
limited to the restaurant's country and language, Japanese names are compared with the domain
across romanisation spellings, and where Wikidata knows the restaurant's name in its own script
(Japanese, Chinese, Korean, Thai) a second search uses that name, and a homepage whose title
carries it also counts. Its matches get website_source=search_local and are listed in the run
summary for review.
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
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from build_full_list import UA, norm_name  # noqa: E402

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


# Hepburn and Kunrei spellings, long vowels and n/m before b, m, p, folded to one form, so
# "Shōfukurō" (shofukuro after accents go) and the domain syofukurou.jp compare equal.
_ROMAJI = [("tsu", "tu"), ("sha", "sya"), ("shu", "syu"), ("sho", "syo"), ("shi", "si"),
           ("cha", "tya"), ("chu", "tyu"), ("cho", "tyo"), ("chi", "ti"), ("ja", "zya"), ("ju", "zyu"),
           ("jo", "zyo"), ("ji", "zi"), ("fu", "hu"), ("mb", "nb"), ("mp", "np"), ("mm", "nm"),
           ("nn", "n"), ("ou", "o"), ("oh", "o"), ("oo", "o"), ("uu", "u"), ("aa", "a"), ("ii", "i"), ("ee", "e")]


def romaji_fold(text: str) -> str:
    for a, b in _ROMAJI:
        text = text.replace(a, b)
    return text


def official(name: str, city: str, url: str, romaji: bool = False) -> bool:
    """Whether url plausibly is the restaurant's own site: not a directory, news, guide or
    hosting domain, and the domain itself carries the restaurant's name. A page title is not
    enough, since articles and directories name the restaurant in theirs. With `romaji`
    (Japanese restaurants), name and domain are compared across romanisation spellings."""
    host = urlsplit(url).netloc.lower()
    if not host or NOT_OFFICIAL.search(host) or PLATFORM.search(host):
        return False
    fold = romaji_fold if romaji else (lambda t: t)
    label = fold(domain_label(host))
    city_words = {fold(w) for w in norm_name(city).split("-") if len(w) >= 3}
    generic = {fold(w) for w in GENERIC}
    words = [fold(w) for w in norm_name(name).split("-") if w]
    words = [w for w in words if w not in generic and w not in city_words]
    compact = "".join(words)
    core = label  # the domain without generic and city words: alinearestaurant -> alinea
    for w in sorted(generic | city_words, key=len, reverse=True):
        core = core.replace(w, "")
    if len(core) < 3 or not compact:
        return False
    # The name makes up most of the domain, or the domain is part of the name, or one
    # distinctive name word makes up most of the domain (tinyurbankitchen is not Urban
    # Gastronomy's, jimbochoden is not checked as Den's).
    def most(part: str) -> bool:
        return len(part) >= 4 and part in core and 3 * len(part) >= 2 * len(core)
    return most(compact) or (len(core) >= 5 and core in compact) or any(most(w) for w in words)


def pick(name: str, city: str, results: list[dict], romaji: bool = False) -> str:
    """The first result whose domain is the restaurant's own (see official) and is a homepage
    or a shallow page of it, as a root URL."""
    for res in results:
        url = res.get("url", "")
        if official(name, city, url, romaji) and urlsplit(url).path.strip("/").count("/") <= 1:
            return f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
    return ""


_NATIVE_SCRIPT = re.compile(r"[\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def pick_native(native: str, results: list[dict]) -> str:
    """A homepage (not a deeper page) whose title carries the restaurant's name in its own
    script, on a host that isn't a guide, directory or platform. Articles and listings are
    deeper pages, so a title match on a site's front page is a much stronger signal than on
    any page."""
    want = "".join(unicodedata.normalize("NFKC", native).split())
    if len(want) < 2 or not _NATIVE_SCRIPT.search(want):
        return ""
    for res in results:
        parts = urlsplit(res.get("url", ""))
        host = parts.netloc.lower()
        if not host or NOT_OFFICIAL.search(host) or PLATFORM.search(host):
            continue
        if parts.path.strip("/").lower() not in ("", "index.html", "index.php", "ja", "jp", "top"):
            continue
        if want in "".join(unicodedata.normalize("NFKC", res.get("title", "")).split()):
            return f"{parts.scheme}://{parts.netloc}/"
    return ""


# Brave's search_lang for each country's own language, and the countries it can limit a search
# to (https://api.search.brave.com/app/documentation/web-search/codes).
LOCAL_LANG = {"JP": "jp", "CN": "zh-hans", "HK": "zh-hant", "TW": "zh-hant", "MO": "zh-hant",
              "KR": "ko", "TH": "th", "FR": "fr", "MC": "fr", "DE": "de", "AT": "de", "CH": "de",
              "IT": "it", "ES": "es", "MX": "es", "AR": "es", "CL": "es", "CO": "es", "PE": "es",
              "UY": "es", "PT": "pt-pt", "BR": "pt-br", "NL": "nl", "DK": "da", "SE": "sv", "NO": "nb",
              "FI": "fi", "PL": "pl", "CZ": "cs", "HU": "hu", "TR": "tr", "SI": "sl", "HR": "hr",
              "SK": "sk"}
BRAVE_COUNTRIES = set("AR AU AT BE BR CA CL DK FI FR DE HK IN ID IT JP KR MY MX NL NZ NO CN PL PT "
                      "PH RU SA ZA ES SE CH TW TR GB US".split())


def local_params(country: str) -> dict:
    out = {}
    if country in BRAVE_COUNTRIES:
        out["country"] = country
    if country in LOCAL_LANG:
        out["search_lang"] = LOCAL_LANG[country]
    return out


WDQS = "https://query.wikidata.org/sparql"
# Restaurants in countries with their own script, with their English and local names.
NATIVE_QUERY = """SELECT ?en ?native ?cc WHERE {
  VALUES ?cc { "JP" "CN" "HK" "TW" "MO" "KR" "TH" }
  ?c wdt:P297 ?cc . ?r wdt:P17 ?c ; wdt:P31/wdt:P279* wd:Q11707 .
  ?r rdfs:label ?en FILTER(LANG(?en) = "en")
  ?r rdfs:label ?native FILTER(LANG(?native) IN ("ja", "zh", "zh-hant", "zh-hk", "zh-tw", "ko", "th"))
}"""


def native_names(cache: Path, client: httpx.Client) -> dict[tuple[str, str], str]:
    """(normalized English name, country) -> the restaurant's name in its own script, from one
    Wikidata query (cached). Empty when Wikidata can't be reached; the run goes on without."""
    path = cache / "wikidata_native.json"
    if not path.exists():
        try:
            r = client.get(WDQS, params={"query": NATIVE_QUERY, "format": "json"}, timeout=120)
            r.raise_for_status()
            path.write_text(json.dumps(r.json()["results"]["bindings"]))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            print(f"wikidata: {type(e).__name__}; searching without native names", file=sys.stderr)
            return {}
    out: dict[tuple[str, str], str] = {}
    for b in json.loads(path.read_text()):
        key = (norm_name(b["en"]["value"]), b["cc"]["value"])
        if _NATIVE_SCRIPT.search(b["native"]["value"]):
            out.setdefault(key, b["native"]["value"])
    return out


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
    ap.add_argument("--local", action="store_true",
                    help="second pass for restaurants still without a website: local language, "
                         "romanisation, native names (Brave only)")
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
    if args.local and not brave_key:
        sys.exit("--local needs BRAVE_SEARCH_API_KEY")
    per_key = 950  # stay under LangSearch's free 1,000 queries a day per account
    if lang_keys and args.limit is None:
        args.limit = per_key * len(lang_keys)
    lang_key = lang_keys[0] if lang_keys else None

    def search(q: str, extra: dict | None = None) -> list[dict]:
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
                resp = client.get(BRAVE, params={"q": q, "count": 10, **(extra or {})},
                                  headers={"X-Subscription-Token": brave_key, "Accept": "application/json"})
                if resp.status_code == 422 and extra:  # a country or language Brave doesn't take
                    return search(q)
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

    client = httpx.Client(timeout=30, headers={"User-Agent": UA})
    taken = {host_of(r["website"]) for r in rows if r["website"]}
    if args.local:
        local_pass(rows, args, search, client, taken)
        write_rows()
        return
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


def local_pass(rows: list[dict], args, search, client: httpx.Client, taken: set[str]) -> None:
    """See --local in the module docstring. Up to two Brave queries per restaurant (its name in
    English, and in its own script when Wikidata has it), cached under <cache>/brave-local/."""
    natives = native_names(args.cache, client)
    cache_dir = args.cache / "brave-local"
    cache_dir.mkdir(parents=True, exist_ok=True)
    queried = errors = 0
    found: list[dict] = []

    def results_for(slug: str, tag: str, q: str, extra: dict) -> list[dict] | None:
        nonlocal queried, errors
        path = cache_dir / f"{slug}.{tag}.json"
        if path.exists():
            return json.loads(path.read_text())
        if args.limit is not None and queried >= args.limit:
            return None
        queried += 1
        try:
            res = search(q, extra)
        except (RuntimeError, httpx.HTTPError) as e:
            errors += 1
            print(f"{slug}: {e}", file=sys.stderr)
            return None
        path.write_text(json.dumps(res))
        if queried % 100 == 0:
            print(f"{queried} queries, {len(found)} websites found so far", file=sys.stderr, flush=True)
        time.sleep(1.1)
        return res

    for r in rows:
        if r["website"] or errors >= 10:
            continue
        cc, extra = r["country"], local_params(r["country"])
        native = natives.get((norm_name(r["name"]), cc), "")
        site, how = "", ""
        res = results_for(r["slug"], "name", " ".join(x for x in (r["name"], r["city"]) if x), extra)
        if res and (site := pick(r["name"], r["city"], res, romaji=cc == "JP")):
            how = "name, local search"
        if not site and native:
            res = results_for(r["slug"], "native", native, extra)
            if res and (site := pick(r["name"], r["city"], res, romaji=cc == "JP")):
                how = "native name"
            elif res and (site := pick_native(native, res)):
                how = "native name in homepage title"
        if site and host_of(site) not in taken:
            r["website"], r["website_source"] = site, "search_local"
            taken.add(host_of(site))
            found.append({"slug": r["slug"], "name": r["name"], "country": cc, "native": native,
                          "website": site, "how": how})
    print(f"{queried} new queries ({errors} errors), {len(found)} websites found, "
          f"{len(natives)} native names", file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"### find_websites --local\n{queried} queries, {len(found)} websites found\n\n"
                    "| restaurant | country | native name | website | matched by |\n|---|---|---|---|---|\n")
            f.writelines(f"| {x['name']} | {x['country']} | {x['native']} | {x['website']} | {x['how']} |\n"
                         for x in found)
    if queried and errors == queried:
        sys.exit("every search failed")


if __name__ == "__main__":
    main()

"""Build data/seed/restaurants_full.csv: Michelin-starred restaurants (from Wikipedia's lists,
since guide.michelin.com serves a bot challenge) plus The World's 50 Best 1-100, with a
website and coordinates for each where one can be found.

    uv run python scripts/build_full_list.py --cache ~/.cache/menudb-lists [--fetch]

Sources, all fetched slowly with the project User-Agent and cached under --cache:
  - en.wikipedia.org "List of Michelin-starred restaurants in ..." pages (action=parse)
  - theworlds50best.com list pages and restaurant pages (their robots.txt allows it)
  - Wikidata (official website P856, coordinates P625) for restaurants with an article
  - Nominatim (OpenStreetMap) search, at most one request per second, for the rest

Wikipedia's lists are community-maintained and lag the guide in places, so each row
records the list year it came from. Verify before publishing anything about star counts.
"""

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

UA = "themenuproject/0.1 (+https://github.com/msh-yi/themenuproject)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
W50B_LISTS = ["https://www.theworlds50best.com/list/1-50", "https://www.theworlds50best.com/list/51-100"]
W50B_YEAR = 2025

# Wikipedia list page suffix ("List of Michelin-starred restaurants in X") -> ISO 3166-1 alpha-2.
PAGE_COUNTRY = {
    "Abu Dhabi": "AE", "Dubai": "AE", "American Northeast Cities": "US", "the American South": "US",
    "the American Southwest": "US", "California": "US", "Chicago": "US", "Colorado": "US",
    "Florida": "US", "New York City": "US", "Texas": "US", "Washington, D.C.": "US",
    "Argentina": "AR", "Austria": "AT", "Beijing": "CN", "Chengdu": "CN", "Guangzhou": "CN",
    "Hangzhou": "CN", "Shanghai": "CN", "Belgium and Luxembourg": "BE", "Berlin": "DE",
    "Germany": "DE", "Brazil": "BR", "Croatia": "HR", "Denmark": "DK", "Doha": "QA",
    "England": "GB", "Greater London": "GB", "Northern Ireland": "GB", "Scotland": "GB",
    "Wales": "GB", "Estonia": "EE", "Finland": "FI", "France": "FR", "Paris": "FR",
    "Greece": "GR", "Hokkaido": "JP", "Japan": "JP", "Kyoto and Osaka": "JP", "Nara": "JP",
    "Tokyo": "JP", "Hong Kong and Macau": "HK", "Hungary": "HU", "Iceland": "IS",
    "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Lithuania": "LT", "Malaysia": "MY",
    "Malta": "MT", "Mexico": "MX", "Monaco": "MC", "Moscow": "RU", "New Zealand": "NZ",
    "Norway": "NO", "Poland": "PL", "Portugal": "PT", "Quebec": "CA", "Toronto": "CA",
    "Vancouver": "CA", "Saudi Arabia": "SA", "Serbia": "RS", "Singapore": "SG",
    "Slovenia": "SI", "South Australia": "AU", "South Korea": "KR", "Spain": "ES",
    "Sweden": "SE", "Switzerland": "CH", "Taiwan": "TW", "Thailand": "TH", "Turkey": "TR",
    "Vietnam": "VN", "the Czech Republic": "CZ", "the Netherlands": "NL",
    "the Philippines": "PH",
}
# Pages whose rows another page already covers in more detail (the 3-star summaries).
SUMMARY_PAGES = ("List of Michelin 3-star restaurants",)

_STAR = re.compile(r"(\d)\s+Michelin star", re.I)


class Http:
    def __init__(self, cache: Path, fetch: bool):
        self.cache, self.fetch = cache, fetch
        self.client = httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
        self.last: dict[str, float] = {}

    def get(self, url: str, params: dict | None = None, interval: float = 1.5) -> httpx.Response:
        host = httpx.URL(url).host
        for attempt in range(8):
            wait = self.last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            r = self.client.get(url, params=params)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("retry-after", "5")) + 2 * attempt)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"{host}: still rate-limited")

    def cached_json(self, name: str, fn):
        path = self.cache / name
        if path.exists():
            return json.loads(path.read_text())
        if not self.fetch:
            return None
        value = fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return value


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "restaurant"


def norm_name(text: str) -> str:
    text = re.sub(r"\(.*?\)", "", text)
    text = re.sub(r"(?i)\b(restaurant|ristorante|restaurante|the|le|la|l'|by)\b", " ", text)
    return slugify(text)


# -- Wikipedia --------------------------------------------------------------------------

def wiki_titles(http: Http) -> list[str]:
    data = http.cached_json("wiki/_index.json", lambda: http.get(WIKI_API, dict(
        action="query", prop="links", titles="Lists of Michelin-starred restaurants",
        pllimit=500, format="json")).json())
    titles = [link["title"] for page in data["query"]["pages"].values() for link in page["links"]]
    return sorted(t for t in titles if "&" not in t and (
        t.startswith("List of Michelin-starred restaurants in ") or t.startswith("List of Michelin 3-star")))


def wiki_page(http: Http, title: str) -> dict | None:
    def fetch():
        j = http.get(WIKI_API, dict(action="parse", page=title, prop="text|revid", redirects=1,
                                    format="json", formatversion=2)).json()
        return {"title": j["parse"]["title"], "revid": j["parse"]["revid"], "html": j["parse"]["text"]}
    return http.cached_json(f"wiki/{slugify(title)}.json", fetch)


def _grid(table) -> list[list]:
    """Table rows with rowspan/colspan expanded, so column i means the same thing in every row."""
    grid, spans = [], {}
    for r_i, tr in enumerate(table.css("tr")):
        row, col, ci = [], 0, 0
        cells = [c for c in tr.iter() if c.tag in ("td", "th")]
        while ci < len(cells) or (r_i, col) in spans:
            if (r_i, col) in spans:
                row.append(spans.pop((r_i, col)))
                col += 1
                continue
            c = cells[ci]
            ci += 1
            rs = int(re.sub(r"\D", "", c.attributes.get("rowspan") or "1") or 1)
            cs = int(re.sub(r"\D", "", c.attributes.get("colspan") or "1") or 1)
            for _ in range(cs):
                row.append(c)
                for j in range(1, rs):
                    spans[(r_i + j, col)] = c
                col += 1
        grid.append(row)
    return grid


def _stars(cell) -> int | None:
    for img in cell.css("img"):
        if m := _STAR.search(img.attributes.get("alt") or ""):
            return int(m.group(1))
    m = _STAR.search(cell.text())
    return int(m.group(1)) if m else None


def parse_wiki(html: str) -> list[dict]:
    """Rows of every star table on a list page: name, article, location, latest year and stars."""
    out = []
    for table in HTMLParser(html).css("table.wikitable"):
        grid = _grid(table)
        if len(grid) < 2:
            continue
        header = [c.text(strip=True) for c in grid[0]]
        years = [(i, int(h[:4])) for i, h in enumerate(header) if re.fullmatch(r"(19|20)\d\d\S*", h)]
        if not years:
            continue

        def column(rx):
            return next((i for i, h in enumerate(header) if re.search(rx, h, re.I)), None)

        name_col = column(r"^(name|restaurant)") or 0
        loc_col = column(r"location|city|region|area|prefecture|district|neighbo|town|municipal|borough")
        rows = []
        for r in grid[1:]:
            if len(r) <= max(i for i, _ in years) or r[name_col] is grid[0][name_col]:
                continue
            cell = r[name_col]
            for junk in cell.css("style, .sr-only, sup.reference"):
                junk.decompose()
            name = re.sub(r"\[.*?\]", "", cell.text(strip=True)).strip()
            if not name:
                continue
            a = cell.css_first('a[rel="mw:WikiLink"]')
            article = None
            if a is not None and "/wiki/" in (a.attributes.get("href") or ""):
                article = unquote(a.attributes["href"].split("/wiki/")[-1]).replace("_", " ")
            rows.append({
                "name": name, "article": article,
                "location": r[loc_col].text(strip=True) if loc_col is not None else "",
                "stars": {y: _stars(r[i]) for i, y in years},
            })
        filled = [y for _, y in years if any(x["stars"].get(y) for x in rows)]
        if not filled:
            continue
        latest = max(filled)
        for x in rows:
            x["year"], x["current"] = latest, x["stars"].get(latest)
            out.append(x)
    return out


def michelin(http: Http) -> list[dict]:
    """Restaurants holding stars in their page's latest guide year, one row each."""
    by_key: dict[str, dict] = {}
    for title in wiki_titles(http):
        page = wiki_page(http, title)
        if page is None:
            continue
        suffix = title.removeprefix("List of Michelin-starred restaurants in ")
        country = PAGE_COUNTRY.get(suffix, "")
        for row in parse_wiki(page["html"]):
            if not row["current"]:
                continue
            key = slugify(row["article"]) if row["article"] else f"{norm_name(row['name'])}|{country}"
            entry = {
                "name": row["name"], "article": row["article"], "location": row["location"],
                "country": country, "stars": row["current"], "year": row["year"],
                "page": page["title"], "revid": page["revid"],
            }
            old = by_key.get(key)
            # a country/city page beats a 3-star summary; otherwise the newer guide year wins
            if old is None or (old["page"].startswith(SUMMARY_PAGES) and not title.startswith(SUMMARY_PAGES)) or (
                    entry["year"] > old["year"] and not title.startswith(SUMMARY_PAGES)):
                if old and not entry["country"]:
                    entry["country"] = old["country"]
                by_key[key] = entry
    return list(by_key.values())


# -- World's 50 Best ---------------------------------------------------------------------

def w50b(http: Http) -> list[dict]:
    def fetch():
        out = []
        for url in W50B_LISTS:
            page = HTMLParser(http.get(url, interval=3).text)
            for item in page.css("div.list-item"):
                rank = item.css_first("p.rank")
                if rank is None or not rank.text(strip=True).isdigit():
                    continue
                if int(rank.text(strip=True)) in {o["rank"] for o in out}:
                    continue
                link = item.css_first(".item-bottom a")
                out.append({
                    "rank": int(rank.text(strip=True)),
                    "name": item.css_first(".item-bottom h2").text(strip=True),
                    "location": item.css_first(".item-bottom p").text(strip=True),
                    "page": "https://www.the50.com" + link.attributes["href"] if link else "",
                })
        for row in out:
            row["website"] = ""
            if row["page"]:
                page = HTMLParser(http.get(row["page"], interval=3).text)
                links = [a.attributes.get("href") or "" for a in page.css("a[href^=http]")]
                links = [h for h in links if not re.search(
                    r"the50\.com|instagram|facebook|twitter|x\.com|youtube|linkedin|tiktok|apple\.com|"
                    r"google\.|sanpellegrino|estrella|visa\.|sevenrooms|doordash|resy|opentable|tock|"
                    r"onetrust|webpuzzle", h, re.I)]
                row["website"] = links[0] if links else ""
        return out
    return http.cached_json("w50b.json", fetch) or []


# -- websites and coordinates --------------------------------------------------------------

def wikidata(http: Http, articles: list[str]) -> dict[str, dict]:
    """article title -> {website, lat, lon} from Wikidata."""
    out: dict[str, dict] = {}
    for i in range(0, len(articles), 50):
        batch = articles[i:i + 50]

        def fetch():
            q = http.get(WIKI_API, dict(action="query", prop="pageprops", ppprop="wikibase_item",
                                        titles="|".join(batch), redirects=1, format="json")).json()["query"]
            alias = {n["to"]: n["from"] for n in q.get("normalized", []) + q.get("redirects", [])}
            qids = {}
            for p in q["pages"].values():
                if qid := p.get("pageprops", {}).get("wikibase_item"):
                    title = p["title"]
                    while title in alias:
                        title = alias[title]
                    qids[qid] = title
            result = {}
            if qids:
                ents = http.get(WIKIDATA_API, dict(action="wbgetentities", ids="|".join(qids), props="claims",
                                                   format="json")).json()["entities"]
                for qid, ent in ents.items():
                    claims = ent.get("claims", {})
                    site = next((c["mainsnak"].get("datavalue", {}).get("value") for c in claims.get("P856", [])
                                 if c.get("rank") != "deprecated"), None)
                    coord = next((c["mainsnak"].get("datavalue", {}).get("value") for c in claims.get("P625", [])),
                                 None)
                    result[qids[qid]] = {"qid": qid, "website": site or "",
                                         "lat": coord["latitude"] if coord else None,
                                         "lon": coord["longitude"] if coord else None}
            return result
        out.update(http.cached_json(f"wikidata/{slugify(batch[0])}-{i}.json", fetch) or {})
    return out


WDQS = "https://query.wikidata.org/sparql"
WDQS_QUERY = """SELECT ?r ?label ?site ?coord ?cc WHERE {
  ?r wdt:P31/wdt:P279* wd:Q11707 .
  ?r rdfs:label ?label .
  OPTIONAL { ?r wdt:P856 ?site }
  OPTIONAL { ?r wdt:P625 ?coord }
  OPTIONAL { ?r wdt:P17 ?c . ?c wdt:P297 ?cc }
  FILTER(BOUND(?site) || BOUND(?coord))
}"""


def wikidata_restaurants(http: Http) -> dict[tuple[str, str], dict[str, dict]]:
    """Every Wikidata restaurant with a website or coordinates, in one query:
    (normalized name, country) -> {item: {website, lat, lon}}."""
    data = http.cached_json("wdqs_restaurants.json", lambda: http.get(
        WDQS, {"query": WDQS_QUERY, "format": "json"}, interval=5).json())
    index: dict[tuple[str, str], dict[str, dict]] = {}
    for b in (data or {}).get("results", {}).get("bindings", []):
        key = (norm_name(b["label"]["value"]), b.get("cc", {}).get("value", ""))
        entry = index.setdefault(key, {}).setdefault(b["r"]["value"], {})
        if "site" in b:
            entry["website"] = b["site"]["value"]
        if m := re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", b.get("coord", {}).get("value", "")):
            entry["lon"], entry["lat"] = float(m.group(1)), float(m.group(2))
    return index


def nominatim(http: Http, name: str, location: str, country: str, unique: bool = False) -> dict | None:
    """Best OpenStreetMap match for a restaurant: website and coordinates."""
    query = ", ".join(x for x in (name, location) if x)

    def fetch():
        params = dict(q=query, format="jsonv2", extratags=1, addressdetails=1, limit=5)
        if country and country not in ("HK",):
            params["countrycodes"] = country.lower()
        return http.get(NOMINATIM, params, interval=1.1).json()
    hits = http.cached_json(f"nominatim/{slugify(query)[:80]}-{country}.json", fetch)
    if not hits:
        return None
    wanted = norm_name(name)
    matches = []
    for h in hits:
        if h.get("category") not in ("amenity", "tourism") and h.get("type") not in ("restaurant", "hotel"):
            continue
        if wanted and wanted not in norm_name(h.get("name") or "") and norm_name(h.get("name") or "") not in wanted:
            continue
        tags = h.get("extratags") or {}
        site = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
        address = h.get("address") or {}
        matches.append({"website": site, "lat": float(h["lat"]), "lon": float(h["lon"]),
                        "country": address.get("country_code", "").upper(),
                        "city": address.get("city") or address.get("town") or address.get("village") or ""})
    if not matches or (unique and len(matches) > 1):
        return None
    return matches[0]


def city_center(http: Http, city: str, country: str) -> tuple[float, float] | None:
    """Coordinates of a city or district, for restaurants OpenStreetMap doesn't know by name."""
    city = re.split(r"[–—(]", city)[0].strip(" ,")

    def fetch():
        params = dict(q=city, format="jsonv2", limit=1)
        if country:
            params["countrycodes"] = country.lower()
        return http.get(NOMINATIM, params, interval=1.1).json()
    hits = http.cached_json(f"nominatim-city/{slugify(city)[:80]}-{country}.json", fetch)
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def country_center(http: Http, country: str) -> tuple[float, float] | None:
    def fetch():
        # Nominatim's structured country search misses territories such as Hong Kong.
        name = {"HK": "Hong Kong", "MO": "Macau"}.get(country)
        params = dict(q=name) if name else dict(country=country)
        return http.get(NOMINATIM, {**params, "format": "jsonv2", "limit": 1}, interval=1.1).json()
    hits = http.cached_json(f"nominatim-country/{country}.json", fetch)
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def clean_site(url: str) -> str:
    url = (url or "").strip().split()[0] if url and url.strip() else ""
    if url and not url.startswith("http"):
        url = "https://" + url
    if re.search(r"instagram|facebook|linktr\.ee|tripadvisor|guide\.michelin", url, re.I):
        return ""
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True, help="directory for downloaded pages (outside the repo)")
    ap.add_argument("--fetch", action="store_true", help="download what isn't cached yet")
    ap.add_argument("--geocode", action="store_true", help="look up missing websites in OpenStreetMap")
    ap.add_argument("--min-stars", type=int, default=1, help="geocode only restaurants with this many stars")
    ap.add_argument("--nyt", type=Path, default=Path("data/seed/lists/nyt_america_2026.txt"))
    ap.add_argument("--nyt-nyc", type=Path, default=Path("data/seed/lists/nyt_nyc_100.txt"))
    ap.add_argument("--seed", type=Path, default=Path("data/seed/restaurants.csv"))
    ap.add_argument("--out", type=Path, default=Path("data/seed/restaurants_full.csv"))
    args = ap.parse_args()
    http = Http(args.cache, args.fetch)

    known = {r["slug"]: r for r in csv.DictReader(args.seed.open())}
    known_by_name = {norm_name(r["name"]): r for r in known.values()}

    rows: dict[str, dict] = {}
    by_name: dict[str, list[str]] = {}

    def same_place(name, location, country) -> str | None:
        """An existing row for this restaurant: same name, and same country or city."""
        slugs = by_name.get(norm_name(name), [])
        if country:
            slugs = [s for s in slugs if rows[s]["country"] in ("", country)]
        loc = slugify(location)
        near = [s for s in slugs if loc and (loc in slugify(rows[s]["city"]) or slugify(rows[s]["city"]) in loc)]
        if len(near) == 1:
            return near[0]
        return slugs[0] if len(slugs) == 1 else None

    def add(name, location, country, lists, sources, article=None):
        hit = known_by_name.get(norm_name(name))
        existing = same_place(name, location, country)
        if existing:
            slug = existing
        elif hit and (not country or not hit["country"] or len(hit["country"]) != 2 or hit["country"] == country):
            slug = hit["slug"]
        else:
            slug = slugify(name)
            base, n = slug, 2
            while slug in rows and (rows[slug]["country"] != country or norm_name(rows[slug]["name"]) != norm_name(name)):
                slug = f"{base}-{slugify(location)[:20] or n}" if n == 2 else f"{base}-{n}"
                n += 1
        row = rows.setdefault(slug, {"slug": slug, "name": name, "city": location, "country": country,
                                     "lat": "", "lon": "", "website": "", "publishes_menu": "",
                                     "lists": [], "list_sources": [], "website_source": "",
                                     "article": article, "stars": 0})
        if slug not in by_name.setdefault(norm_name(name), []):
            by_name[norm_name(name)].append(slug)
        row["lists"] += [x for x in lists if x not in row["lists"]]
        row["list_sources"] += [x for x in sources if x not in row["list_sources"]]
        row["article"] = row["article"] or article
        return row

    for m in michelin(http):
        r = add(m["name"], m["location"], m["country"], [f"michelin:{m['year']}:{m['stars']}"],
                [f"wikipedia:{m['page']}@{m['revid']}"], m["article"])
        r["stars"] = max(r["stars"], m["stars"])
    for w in w50b(http):
        r = add(w["name"], w["location"], "", [f"w50b:{W50B_YEAR}:{w['rank']}"], ["theworlds50best.com"])
        if w["website"] and not r["website"]:
            r["website"], r["website_source"] = clean_site(w["website"]), "w50b"
        r["stars"] = max(r["stars"], 3)  # geocode these first

    # NYT America 50 has names only; with no city, a lookup must match exactly one place.
    nyt_names = [n for n in args.nyt.read_text().splitlines() if n and not n.startswith("#")]
    for name in nyt_names:
        r = add(name, "", "US", ["nyt:2026:america50"], ["nyt-google-maps-list"])
        r["stars"] = max(r["stars"], 2)
        r["nyt"] = True

    # NYT NYC 100: "name | year:rank;..." lines; restaurants without a rank are left out.
    for line in args.nyt_nyc.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, ranks = (x.strip() for x in line.partition("|"))
        lists = [f"nytnyc:{yr}" for yr in ranks.split(";") if yr]
        if lists:
            r = add(name, "New York", "US", lists, ["nyt-nyc-google-maps-list"])
            r["stars"] = max(r["stars"], 2)

    for r in rows.values():
        if r["slug"] in known:
            k = known[r["slug"]]
            for col in ("city", "lat", "lon", "website", "publishes_menu"):
                r[col] = k[col] or r[col]
            if k["country"] and len(k["country"]) == 2:
                r["country"] = k["country"]
            r["website_source"] = "seed"

    # Keep websites found by search (scripts/find_websites.py) across rebuilds.
    if args.out.exists():
        for old in csv.DictReader(args.out.open()):
            r = rows.get(old["slug"])
            if r and not r["website"] and old.get("website_source") == "search":
                r["website"], r["website_source"] = old["website"], "search"

    wd = wikidata(http, sorted({r["article"] for r in rows.values() if r["article"]}))
    for r in rows.values():
        d = wd.get(r["article"] or "")
        if not d:
            continue
        if not r["website"] and clean_site(d["website"]):
            r["website"], r["website_source"] = clean_site(d["website"]), "wikidata"
        if not r["lat"] and d["lat"] is not None:
            r["lat"], r["lon"] = round(d["lat"], 4), round(d["lon"], 4)

    # Restaurants without an article: match Wikidata by name within the country, only when
    # exactly one item matches.
    wdqs = wikidata_restaurants(http)
    for r in rows.values():
        if r["website"] and r["lat"]:
            continue
        items = wdqs.get((norm_name(r["name"]), r["country"]), {})
        if len(items) != 1:
            continue
        d = next(iter(items.values()))
        if not r["website"] and clean_site(d.get("website", "")):
            r["website"], r["website_source"] = clean_site(d["website"]), "wikidata-name"
        if not r["lat"] and "lat" in d:
            r["lat"], r["lon"] = round(d["lat"], 4), round(d["lon"], 4)

    if args.geocode:
        todo = [r for r in rows.values() if (not r["website"] or not r["lat"]) and r["stars"] >= args.min_stars]
        todo.sort(key=lambda r: -r["stars"])
        for n, r in enumerate(todo, 1):
            hit = nominatim(http, r["name"], r["city"], r["country"], unique=r.get("nyt", False) and not r["city"])
            if hit:
                if not r["website"] and clean_site(hit["website"]):
                    r["website"], r["website_source"] = clean_site(hit["website"]), "osm"
                if not r["lat"]:
                    r["lat"], r["lon"] = round(hit["lat"], 4), round(hit["lon"], 4)
                if not r["country"] and hit["country"]:
                    r["country"] = hit["country"]
                if not r["city"] and hit["city"]:
                    r["city"] = hit["city"]
            if n % 100 == 0:
                print(f"geocoded {n}/{len(todo)}", file=sys.stderr)

        # No exact place found: put the pin on the city so every row can be mapped.
        for r in rows.values():
            if r["lat"] or not r["city"]:
                continue
            hit = city_center(http, r["city"], r["country"])
            if hit:
                r["lat"], r["lon"], r["coord_precision"] = round(hit[0], 4), round(hit[1], 4), "city"
        # Last resort for city-states and rows with no usable city: the country itself.
        for r in rows.values():
            if r["lat"] or not r["country"]:
                continue
            hit = country_center(http, r["country"])
            if hit:
                r["lat"], r["lon"], r["coord_precision"] = round(hit[0], 4), round(hit[1], 4), "country"

    for r in rows.values():
        if r["lat"] and not r.get("coord_precision"):
            r["coord_precision"] = "exact"

    cols = ["slug", "name", "city", "country", "lat", "lon", "website", "publishes_menu", "lists",
            "list_sources", "website_source", "coord_precision"]
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (-r["stars"], r["slug"])):
            w.writerow({**r, "lists": ";".join(r["lists"]), "list_sources": ";".join(r["list_sources"])})
    with_site = sum(1 for r in rows.values() if r["website"])
    print(f"{len(rows)} restaurants, {with_site} with a website -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

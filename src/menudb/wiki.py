"""Wikipedia links for canonical ingredients (`menudb wiki-links`).

Resolves each ingredient name through the English Wikipedia API (redirects followed,
short description and Wikidata item included) and keeps the result in
data/ingredients/wikipedia.csv, which is committed and reviewed by hand:

    ok         resolved to an article whose short description reads as food or an organism
    check      resolved, but the description doesn't say food: confirm or fix by hand
    ambiguous  the name is a disambiguation page; `candidates` lists any qualified
               titles found, such as "Sole (fish)". Pick one by hand.
    missing    no article under that name
    manual     set by hand: never touched again
    none       no link wanted (set by hand): never touched again

Only `ok` and `manual` rows are linked on the site. Reruns only look up names that
aren't in the file yet (`--retry` also redoes check, ambiguous and missing).
The API takes 50 titles per request; requests are spaced out and 429/503 responses
wait for Retry-After, since the sandbox shares its IP with other users.
"""

import csv
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"
CSV_PATH = Path(__file__).resolve().parents[2] / "data/ingredients/wikipedia.csv"
KEEP = {"manual", "none"}  # hand-set rows
RETRY = {"check", "ambiguous", "missing"}
LINKED = {"ok", "manual"}
# Qualifiers tried when a name is a disambiguation page, in order.
QUALIFIERS = ["food", "fish", "fruit", "vegetable", "plant", "herb", "spice", "mushroom",
              "seaweed", "cheese", "meat", "shellfish", "dish", "sauce", "ingredient"]
FOODLIKE = re.compile(
    r"\b(food|foods|dish|dishes|cuisine|culinary|cooking|edible|ingredient|species|genus|"
    r"subspecies|cultivar|variety|plant|plants|tree|shrub|herb|spice|fruit|berry|berries|"
    r"vegetable|nut|seed|grain|cereal|legume|bean|root|tuber|flower|mushroom|fungus|fungi|"
    r"seaweed|alga|algae|kelp|fish|fishes|flatfish|seafood|shellfish|mollusc|mollusk|crustacean|bivalve|bird|mammal|"
    r"meat|offal|cut|egg|eggs|milk|dairy|cheese|butter|cream|yogurt|oil|vinegar|sauce|"
    r"condiment|paste|broth|stock|sugar|syrup|sweetener|honey|chocolate|cocoa|coffee|tea|"
    r"wine|beverage|drink|liqueur|spirit|bread|pastry|dessert|confection|noodle|pasta|rice|"
    r"citrus|pepper|chili|onion|garlic)\b", re.I)


@dataclass
class Link:
    name: str
    status: str
    title: str = ""
    wikidata: str = ""
    description: str = ""
    candidates: str = ""  # "; "-separated titles

    @property
    def url(self) -> str | None:
        if self.status not in LINKED or not self.title:
            return None
        return "https://en.wikipedia.org/wiki/" + quote(self.title.replace(" ", "_"), safe="()_,'-")


COLUMNS = [f.name for f in fields(Link)]


def load(path: Path = CSV_PATH) -> dict[str, Link]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["name"]: Link(**{c: r.get(c) or "" for c in COLUMNS}) for r in csv.DictReader(f)}


def save(links: dict[str, Link], path: Path = CSV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for name in sorted(links):
            w.writerow(asdict(links[name]))


def urls(path: Path = CSV_PATH) -> dict[str, str]:
    """name -> Wikipedia URL, for the linked rows only."""
    return {n: link.url for n, link in load(path).items() if link.url}


class Client:
    def __init__(self, user_agent: str, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, min_interval: float = 1.0,
                 max_tries: int = 6):
        self.http = httpx.Client(transport=transport, timeout=30, headers={"User-Agent": user_agent})
        self.sleep, self.min_interval, self.max_tries = sleep, min_interval, max_tries
        self.requests = 0

    def pages(self, titles: list[str]) -> dict[str, dict]:
        """Requested title -> page dict ({} when missing), following normalization and redirects."""
        out: dict[str, dict] = {}
        for i in range(0, len(titles), 50):
            out |= self._batch(titles[i:i + 50])
        return out

    def _batch(self, titles: list[str]) -> dict[str, dict]:
        params = {"action": "query", "format": "json", "formatversion": "2", "redirects": "1",
                  "prop": "pageprops|description", "ppprop": "disambiguation|wikibase_item",
                  "titles": "|".join(titles), "maxlag": "5"}
        for attempt in range(self.max_tries):
            if self.requests:
                self.sleep(self.min_interval)
            self.requests += 1
            r = self.http.get(API, params=params)
            if r.status_code in (429, 503) or (r.status_code == 200 and "maxlag" in r.text[:200]):
                wait = float(r.headers.get("retry-after") or 0) or 2 ** attempt
                log.info("wikipedia busy (%s), waiting %.0fs", r.status_code, wait)
                self.sleep(min(wait, 120))
                continue
            r.raise_for_status()
            q = r.json()["query"]
            hop = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redirect = {n["from"]: n["to"] for n in q.get("redirects", [])}
            by_title = {p["title"]: p for p in q["pages"] if not p.get("missing") and not p.get("invalid")}
            out = {}
            for t in titles:
                cur = hop.get(t, t)
                for _ in range(5):  # redirect chains are short
                    if cur not in redirect:
                        break
                    cur = redirect[cur]
                out[t] = by_title.get(cur, {})
            return out
        raise RuntimeError(f"wikipedia still busy after {self.max_tries} tries")


def _foodlike(description: str) -> bool:
    return bool(FOODLIKE.search(description or ""))


def resolve(names: Iterable[str], client: Client) -> dict[str, Link]:
    names = sorted({n.strip() for n in names if n and n.strip()})
    pages = client.pages(names)
    out: dict[str, Link] = {}
    ambiguous = []
    for n in names:
        p = pages.get(n) or {}
        if not p:
            out[n] = Link(n, "missing")
        elif "disambiguation" in p.get("pageprops", {}):
            ambiguous.append(n)
        else:
            desc = p.get("description", "")
            out[n] = Link(n, "ok" if _foodlike(desc) else "check", p["title"],
                          p.get("pageprops", {}).get("wikibase_item", ""), desc)
    if ambiguous:
        variants = {f"{n} ({q})": n for n in ambiguous for q in QUALIFIERS}
        found = client.pages(list(variants))
        for n in ambiguous:
            hits = []
            for v, owner in variants.items():
                p = found.get(v) or {}
                if owner == n and p and "disambiguation" not in p.get("pageprops", {}) \
                        and _foodlike(p.get("description", "")) and p["title"] not in [h["title"] for h in hits]:
                    hits.append(p)
            if len(hits) == 1:  # "Sole (fish)" and nothing else: a qualified title, not a guess
                p = hits[0]
                out[n] = Link(n, "ok", p["title"], p.get("pageprops", {}).get("wikibase_item", ""),
                              p.get("description", ""))
            else:
                out[n] = Link(n, "ambiguous", candidates="; ".join(h["title"] for h in hits))
    return out


def update(names: Iterable[str], client: Client, path: Path = CSV_PATH, retry: bool = False) -> dict:
    links = load(path)
    todo = [n for n in {n.strip() for n in names if n and n.strip()}
            if n not in links or (retry and links[n].status in RETRY)]
    if todo:
        links |= resolve(todo, client)
        save(links, path)
    counts: dict[str, int] = {"looked_up": len(todo), "requests": client.requests}
    for link in links.values():
        counts[link.status] = counts.get(link.status, 0) + 1
    return counts

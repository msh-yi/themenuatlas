"""Find menus in your own Chrome for restaurants whose menu discovery came up empty.
Run it on your own machine (it opens a visible Chrome window through DrissionPage):

    uv run --with DrissionPage python scripts/find_menus_browser.py --limit 50 [--to-bucket] [--status new]

For each restaurant with a website whose status in data/seed/discovery.csv is no_menu_found,
failed or needs_js (add `--status new` for websites not checked yet), it runs the same menu
discovery as `menudb discover` (src/menudb/discover.py), with every page loaded in Chrome so
menus built by JavaScript appear, following up to 8 menu-like links instead of 4. When that
finds no menu, it looks further, cheapest first, up to 15 more pages or PDFs per site:
  1. PDFs anywhere in the home page's source (scripts, page data, embedded viewers), not only
     in links;
  2. the site's sitemap (sitemap.xml and the ones robots.txt lists), pages ranked by menu
     words in their address;
  3. up to 8 of the site's own pages the home page links to, whatever they're called ("The
     Experience", "Our Journey"), each judged by the food words it contains;
  4. the usual addresses (/menu, /menus, /carte, /en/menu).

Politeness is the crawler's own: Chrome sends the project's User-Agent, robots.txt is checked
before every page (RESPECT_ROBOTS=false skips that, as in the monthly workflows), a site gets
one page every 5 seconds, pages that reserve text and data mining (TDM) are skipped, and a
bot-protection page is recorded as bot_blocked, never got past.

Found menus are written like `menudb discover` writes them: a recipe in data/recipes_auto/
(render: js) and the status in data/seed/discovery.csv, saved after every restaurant so an
interrupted run resumes. With --to-bucket the menus are also stored and queued in the bucket's
pending/ for check.yml to extract (needs the Neon values in .env.local; the run stops at once
if the bucket can't be reached). Restaurants this script has already tried are skipped unless
--redo. Prints slugs, statuses and scores only.

Menus found in a run without --to-bucket (or whose storing failed) are stored later, without
searching again, from the recipes already written:

    uv run --with DrissionPage python scripts/find_menus_browser.py --store-only
"""

import argparse
import csv
import datetime as dt
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

import re

from selectolax.lexbor import LexborHTMLParser

from menudb.discover import (
    _AVOID_RE, _FILE_RE, _MENU_HREF_RE, _MENU_RE, _SOCIAL_HOSTS, MAYBE_MIN, MENU_MIN, Discovery,
    _candidate, _site, discover, food_score)
from menudb.extract.text import document_kind, document_text
from menudb.fetch.http import _TDM_META, Fetcher, RobotsDisallowed, absolute
from menudb.normalize import html_to_text

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["slug", "status", "score", "kind", "menu_url", "stored", "note", "checked"]
RETRY = {"no_menu_found", "failed", "needs_js"}
COMMON_PATHS = ["/menu", "/menus", "/carte", "/en/menu"]
EXTRA_BUDGET = 15  # pages or PDFs looked at per site after discover() finds nothing
NAV_PAGES = 8
SITEMAPS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"]
# a .pdf address anywhere in a page's source: links, scripts, JSON, data attributes
_PDF_IN_SOURCE = re.compile(r"""(?i)(https?:\\?/\\?/[^"'\s<>()]+?\.pdf|/[^"'\s<>()]+?\.pdf)(?=[?#"'\s<>()]|$)""")
_LOC = re.compile(r"(?is)<loc>\s*(.*?)\s*</loc>")
NOTE = "chrome"  # marks restaurants this script has tried


class TdmReserved(Exception):
    pass


class ChromeRenderer:
    """Pages loaded in a visible Chrome, with the project's User-Agent, one page per site every
    `interval` seconds."""

    def __init__(self, user_agent: str, interval: float = 5.0, settle: float = 2.0):
        from DrissionPage import ChromiumOptions, ChromiumPage

        opts = ChromiumOptions().set_user_agent(user_agent).auto_port()
        self.page = ChromiumPage(opts)
        self.interval, self.settle = interval, settle
        self.last: dict[str, float] = {}

    def render(self, url: str) -> tuple[str, str]:
        host = urlsplit(url).netloc
        wait = self.last.get(host, 0) + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last[host] = time.monotonic()
        self.page.get(url, timeout=30)
        time.sleep(self.settle)  # scripts fill the page in after load
        return self.page.url, self.page.html

    def close(self) -> None:
        self.page.quit()


def page_loader(fetcher: Fetcher, renderer):
    """get_html for discover(): robots.txt and TDM checked, the page loaded in Chrome. Pages
    are kept for the rest of the site's search, so none is loaded twice."""
    loaded: dict[str, tuple[str, str]] = {}

    def get_html(url: str) -> tuple[str, str]:
        if url in loaded:
            return loaded[url]
        if len(loaded) > 200:
            loaded.clear()
        if not fetcher.allowed(url):
            raise RobotsDisallowed(url)
        _, host = fetcher._host(url)
        if fetcher._tdm_from_wellknown(url, host):
            raise TdmReserved(url)
        final, html = renderer.render(url)
        if _TDM_META.search(html[:20000]):
            raise TdmReserved(url)
        loaded[url] = (final, html)
        return final, html
    return get_html


def find_menu(website: str, fetcher: Fetcher, get_html) -> Discovery:
    """discover() in the browser with more links followed; then, when that finds no menu, the
    places discover() doesn't look (see the module docstring), best candidate first."""
    d = discover(website, fetcher, get_html, max_pages=8)
    if "TdmReserved" in d.note:
        return Discovery("opted_out", note="tdm-reservation")
    if d.status in ("menu_found", "robots", "bot_blocked", "opted_out"):
        return d
    best = d if d.status == "menu_maybe" else None
    try:
        home_url, home = get_html(website)
    except Exception:
        home_url, home = website, ""
    tried = 0
    for url, how in candidates(home_url, home, fetcher):
        if tried >= EXTRA_BUDGET:
            break
        tried += 1
        got = _try(url, fetcher, get_html)
        if got.status.startswith("menu_") and (best is None or got.score > best.score):
            got.note = f"found via {how}"
            best = got
        if best is not None and best.status == "menu_found":
            break
    if best is None:
        return Discovery(d.status if d.status != "needs_js" else "no_menu_found",
                         score=d.score, menu_url=d.menu_url, note=f"{d.note}; {tried} more tried")
    return best


def candidates(home_url: str, html: str, fetcher: Fetcher):
    """(url, how found) to look at, cheapest and likeliest first, each once: PDFs in the home
    page's source, menu-like addresses from the sitemap, the site's own pages linked from the
    home page, then the usual menu addresses."""
    seen = {home_url.rstrip("/")}

    def new(url: str) -> bool:
        key = url.split("#")[0].rstrip("/")
        if key in seen or not url.startswith("http") or _SOCIAL_HOSTS.search(urlsplit(url).netloc):
            return False
        seen.add(key)
        return True

    pdfs = [absolute(home_url, m.replace("\\/", "/")) for m in _PDF_IN_SOURCE.findall(html)]
    for url in sorted(dict.fromkeys(pdfs), key=lambda u: -_menu_rank(u)):
        if not _AVOID_RE.search(urlsplit(url).path) and new(url):
            yield url, "a PDF in the page source"
    for url in sorted(sitemap_urls(home_url, fetcher), key=lambda u: -_menu_rank(u)):
        if _menu_rank(url) > 0 and new(url):
            yield url, "the sitemap"
    for url in nav_links(home_url, html)[:NAV_PAGES]:
        if new(url):
            yield url, "the site's navigation"
    root = f"{urlsplit(home_url).scheme}://{urlsplit(home_url).netloc}"
    for path in COMMON_PATHS:
        url = urljoin(root, path)
        if new(url):
            yield url, "a usual menu address"


def _menu_rank(url: str) -> int:
    """How menu-like an address is: menu words in its path or file name, PDFs a little more."""
    path = urlsplit(url).path
    words = re.sub(r"[/_.\-]+", " ", path)
    score = 2 * bool(_MENU_RE.search(words)) + bool(_MENU_HREF_RE.search(path))
    if _AVOID_RE.search(words):
        score -= 3
    return score + (score > 0 and path.lower().endswith(".pdf"))


def sitemap_urls(home_url: str, fetcher: Fetcher, max_files: int = 5) -> list[str]:
    """Same-site page and PDF addresses from the sitemaps robots.txt names, or the usual sitemap
    addresses; one level of sitemap index followed. Empty when there is none."""
    root = f"{urlsplit(home_url).scheme}://{urlsplit(home_url).netloc}"
    _, host = fetcher._host(home_url)
    queue = list((host.robots.site_maps() or []) if host.robots else []) or [root + p for p in SITEMAPS]
    urls, files = [], 0
    while queue and files < max_files:
        sm = queue.pop(0)
        try:
            res = fetcher.get(sm, retries=0)
        except Exception:
            continue
        files += 1
        locs = _LOC.findall(res.content.decode(errors="replace"))
        if b"<sitemapindex" in res.content[:2000]:
            queue = [u for u in locs if _site(u) == _site(home_url)] + queue
            continue
        urls += [u for u in locs if _site(u) == _site(home_url)]
        if urls:
            break
    return urls


def nav_links(home_url: str, html: str) -> list[str]:
    """The site's own pages the home page links to, in page order: shallow, not files, not
    booking, gift card or job pages."""
    out = []
    for a in LexborHTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = absolute(home_url, href).split("#")[0]
        path = urlsplit(url).path
        if (_site(url) != _site(home_url) or _FILE_RE.search(path.lower()) or path.strip("/").count("/") > 2
                or _AVOID_RE.search(path) or _AVOID_RE.search(a.text(strip=True))):
            continue
        if url.rstrip("/") != home_url.rstrip("/") and url not in out:
            out.append(url)
    return out


def _try(url: str, fetcher: Fetcher, get_html) -> Discovery:
    """One candidate judged on its food words: a PDF fetched over HTTP, a page loaded in Chrome."""
    if urlsplit(url).path.lower().endswith(".pdf"):
        try:
            res = fetcher.get(url)
        except Exception as e:
            return Discovery("failed", note=type(e).__name__)
        if res.tdm_reserved:
            return Discovery("opted_out", note="tdm-reservation")
        kind = document_kind(res.content_type, res.final_url, res.content)
        score = food_score(document_text(kind, res.content) if kind != "image" else None)
        if score < MAYBE_MIN:
            return Discovery("no_menu_found", score=score)
        d = _candidate(url, "", kind, score)
        d.status = "menu_found" if score >= MENU_MIN else "menu_maybe"
        return d
    return _scored(url, get_html)


def _scored(url: str, get_html) -> Discovery:
    """One page judged on its own food words."""
    try:
        final, html = get_html(url)
    except Exception as e:
        return Discovery("failed", note=type(e).__name__)
    score = food_score(html_to_text(html))
    if score < MAYBE_MIN:
        return Discovery("no_menu_found", score=score)
    d = _candidate(final, "", "html", score)
    d.status = "menu_found" if score >= MENU_MIN else "menu_maybe"
    return d


def todo(full_csv: Path, status_csv: Path, statuses: set[str], redo: bool) -> tuple[list[dict], dict]:
    rows = [r for r in csv.DictReader(full_csv.open(newline="")) if r.get("website")]
    done = {r["slug"]: r for r in csv.DictReader(status_csv.open(newline=""))} if status_csv.exists() else {}
    picked = []
    for r in rows:
        prev = done.get(r["slug"])
        status = prev["status"] if prev else "new"
        if status not in statuses:
            continue
        if prev and prev.get("note", "").startswith(NOTE) and not redo:
            continue  # tried in Chrome already
        if (ROOT / "data/recipes" / f"{r['slug']}.yaml").exists():
            continue  # a hand recipe
        picked.append(r)
    return picked, done


def save(status_csv: Path, done: dict) -> None:
    with status_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(done.values(), key=lambda r: r["slug"]))


def run(rows: list[dict], done: dict, fetcher: Fetcher, get_html, status_csv: Path, recipes_dir: Path,
        store=None) -> Counter:
    """Try each restaurant; record its recipe and status as it goes. `store(slug, recipe)` stores
    and queues the documents (--to-bucket) and returns how many, or -1 for a TDM opt-out."""
    counts: Counter = Counter()
    recipes_dir.mkdir(parents=True, exist_ok=True)
    for n, r in enumerate(rows, 1):
        slug = r["slug"]
        try:
            d = find_menu(r["website"], fetcher, get_html)
        except Exception as e:  # one odd site must not stop the run
            d = Discovery("failed", note=f"{type(e).__name__}: {e}"[:150])
        stored: int | str = ""
        if d.recipe and d.status.startswith("menu_"):
            d.recipe["render"] = "js"
            if store is not None:
                try:
                    stored = store(slug, d.recipe)
                except Exception as e:
                    d.note = f"store failed: {type(e).__name__}"
            if stored == -1:
                d.status, d.note, stored = "opted_out", "tdm-reservation", ""
        if d.recipe and d.status.startswith("menu_"):
            (recipes_dir / f"{slug}.yaml").write_text(yaml.safe_dump(d.recipe, sort_keys=False, allow_unicode=True))
        done[slug] = {"slug": slug, "status": d.status, "score": d.score, "kind": d.kind, "menu_url": d.menu_url,
                      "stored": stored, "note": f"{NOTE}: {' '.join(d.note.split())}"[:200],
                      "checked": dt.date.today().isoformat()}
        save(status_csv, done)
        counts[d.status] += 1
        print(f"[{n}/{len(rows)}] {slug}: {d.status} score={d.score} {d.kind} stored={stored}", flush=True)
    return counts


def unstored(done: dict, recipes_dir: Path) -> list[str]:
    """Restaurants this script found a menu for whose documents aren't in the bucket yet."""
    return [s for s, r in sorted(done.items())
            if r.get("note", "").startswith(NOTE) and r["status"].startswith("menu_")
            and not r.get("stored") and (recipes_dir / f"{s}.yaml").exists()]


def store_found(slugs: list[str], done: dict, store, status_csv: Path, recipes_dir: Path) -> Counter:
    """Store the documents of menus already found, from their recipes; no searching."""
    counts: Counter = Counter()
    for n, slug in enumerate(slugs, 1):
        row = done[slug]
        try:
            stored = store(slug, yaml.safe_load((recipes_dir / f"{slug}.yaml").read_text()))
        except Exception as e:  # one odd site must not stop the run
            stored, result = "", f"failed ({type(e).__name__})"
        else:
            result = "opted_out" if stored == -1 else "stored"
        if stored == -1:
            row.update(status="opted_out", note=f"{NOTE}: tdm-reservation")
            (recipes_dir / f"{slug}.yaml").unlink()
            stored = ""
        elif "store failed" in row.get("note", "") and stored != "":
            row["note"] = f"{NOTE}: stored later"
        row["stored"] = stored
        save(status_csv, done)
        counts[result] += 1
        print(f"[{n}/{len(slugs)}] {slug}: {result} stored={stored}", flush=True)
    return counts


def check_bucket() -> None:
    """Stop before any searching when the bucket can't be reached (e.g. no .env.local)."""
    from menudb import storage

    try:
        storage.exists("raw/00/ping")
    except Exception as e:
        sys.exit(f"--to-bucket: the bucket can't be reached ({type(e).__name__}). Run from the repo "
                 "with the Neon values (AWS_*) in .env.local, from `neon env pull`.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None, help="max restaurants this run")
    ap.add_argument("--status", action="append",
                    help="statuses to try (repeatable): no_menu_found, failed, needs_js, menu_maybe, new "
                         "(not checked yet); default: the first three")
    ap.add_argument("--slug", action="append", help="only these restaurants")
    ap.add_argument("--redo", action="store_true", help="also restaurants this script tried before")
    ap.add_argument("--to-bucket", action="store_true", help="store found menus and queue them in pending/")
    ap.add_argument("--store-only", action="store_true",
                    help="no searching: store the menus found earlier that aren't in the bucket yet")
    ap.add_argument("--csv", type=Path, default=ROOT / "data/seed/restaurants_full.csv")
    ap.add_argument("--status-csv", type=Path, default=ROOT / "data/seed/discovery.csv")
    ap.add_argument("--recipes-dir", type=Path, default=ROOT / "data/recipes_auto")
    args = ap.parse_args()
    args.to_bucket = args.to_bucket or args.store_only
    os.chdir(ROOT)  # the settings read .env.local from the current directory

    from menudb.config import get_settings

    if args.store_only:
        done = {r["slug"]: r for r in csv.DictReader(args.status_csv.open(newline=""))}
        rows = unstored(done, args.recipes_dir)
        if args.slug:
            rows = [s for s in rows if s in args.slug]
        rows = rows[:args.limit] if args.limit else rows
        print(f"{len(rows)} found menus to store", file=sys.stderr)
    else:
        statuses = set(args.status) if args.status else RETRY
        rows, done = todo(args.csv, args.status_csv, statuses, args.redo)
        if args.slug:
            rows = [r for r in rows if r["slug"] in args.slug]
        rows = rows[:args.limit] if args.limit else rows
        print(f"{len(rows)} restaurants to try", file=sys.stderr)
    if not rows:
        return
    if args.to_bucket:
        check_bucket()
    ua = get_settings().user_agent
    renderer = ChromeRenderer(ua)
    with Fetcher(ua) as fetcher:
        get_html = page_loader(fetcher, renderer)
        store = None
        if args.to_bucket:
            from menudb import handoff
            from menudb.pipeline import fetch_documents, html_getter
            from menudb.recipes import Recipe

            def store(slug: str, recipe: dict) -> int:
                rec = Recipe.model_validate(recipe)
                docs = list(fetch_documents(rec, fetcher, html_getter(rec, fetcher, renderer), {}))
                if any(d.tdm_reserved for d in docs):
                    return -1
                handoff.queue_items(handoff.item_from_bytes(slug, "main", d.url, d.data, d.content_type)
                                    for d in docs)
                return len(docs)
        try:
            if args.store_only:
                counts = store_found(rows, done, store, args.status_csv, args.recipes_dir)
            else:
                counts = run(rows, done, fetcher, get_html, args.status_csv, args.recipes_dir, store)
        finally:
            renderer.close()
    print(" ".join(f"{k}: {v}" for k, v in sorted(counts.items())), file=sys.stderr)


if __name__ == "__main__":
    main()

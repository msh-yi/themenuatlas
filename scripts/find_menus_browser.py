"""Find menus in your own Chrome for restaurants whose menu discovery came up empty.
Run it on your own machine (it opens a visible Chrome window through DrissionPage):

    uv run --with DrissionPage python scripts/find_menus_browser.py --limit 50 [--to-bucket] [--status new]

For each restaurant with a website whose status in data/seed/discovery.csv is no_menu_found,
failed or needs_js (add `--status new` for websites not checked yet), it runs the same menu
discovery as `menudb discover` (src/menudb/discover.py), with every page loaded in Chrome so
menus built by JavaScript appear. It follows up to 8 menu-like links instead of 4, and when
the home page links to no menu it tries the usual addresses (/menu, /menus, /carte, /en/menu).

Politeness is the crawler's own: Chrome sends the project's User-Agent, robots.txt is checked
before every page (RESPECT_ROBOTS=false skips that, as in the monthly workflows), a site gets
one page every 5 seconds, pages that reserve text and data mining (TDM) are skipped, and a
bot-protection page is recorded as bot_blocked, never got past.

Found menus are written like `menudb discover` writes them: a recipe in data/recipes_auto/
(render: js) and the status in data/seed/discovery.csv, saved after every restaurant so an
interrupted run resumes. With --to-bucket the menus are also stored and queued in the bucket's
pending/ for check.yml to extract (needs the Neon values in .env.local). Restaurants this
script has already tried are skipped unless --redo. Prints slugs, statuses and scores only.
"""

import argparse
import csv
import datetime as dt
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

from menudb.discover import MAYBE_MIN, MENU_MIN, Discovery, _candidate, discover, food_score
from menudb.fetch.http import _TDM_META, Fetcher, RobotsDisallowed
from menudb.normalize import html_to_text

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["slug", "status", "score", "kind", "menu_url", "stored", "note", "checked"]
RETRY = {"no_menu_found", "failed", "needs_js"}
COMMON_PATHS = ["/menu", "/menus", "/carte", "/en/menu"]
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
    """get_html for discover(): robots.txt and TDM checked, the page loaded in Chrome."""
    def get_html(url: str) -> tuple[str, str]:
        if not fetcher.allowed(url):
            raise RobotsDisallowed(url)
        _, host = fetcher._host(url)
        if fetcher._tdm_from_wellknown(url, host):
            raise TdmReserved(url)
        final, html = renderer.render(url)
        if _TDM_META.search(html[:20000]):
            raise TdmReserved(url)
        return final, html
    return get_html


def find_menu(website: str, fetcher: Fetcher, get_html) -> Discovery:
    """discover() in the browser with more links followed; then the usual menu addresses when
    that finds no menu."""
    d = discover(website, fetcher, get_html, max_pages=8)
    if "TdmReserved" in d.note:
        return Discovery("opted_out", note="tdm-reservation")
    if d.status in ("menu_found", "robots", "bot_blocked", "opted_out"):
        return d
    best = d if d.status == "menu_maybe" else None
    root = f"{urlsplit(website).scheme}://{urlsplit(website).netloc}"
    for path in COMMON_PATHS:
        url = urljoin(root, path)
        if not fetcher.allowed(url):
            continue
        tried = discover(url, fetcher, get_html, max_pages=2)
        if tried.status == "needs_js":  # a short page; Chrome has already run its scripts
            tried = _scored(url, get_html)
        if tried.status.startswith("menu_") and (best is None or tried.score > best.score):
            best = tried
        if best is not None and best.status == "menu_found":
            break
    if best is None:
        return Discovery(d.status if d.status != "needs_js" else "no_menu_found",
                         score=d.score, menu_url=d.menu_url, note=d.note)
    return best


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None, help="max restaurants this run")
    ap.add_argument("--status", action="append",
                    help="statuses to try (repeatable): no_menu_found, failed, needs_js, menu_maybe, new "
                         "(not checked yet); default: the first three")
    ap.add_argument("--slug", action="append", help="only these restaurants")
    ap.add_argument("--redo", action="store_true", help="also restaurants this script tried before")
    ap.add_argument("--to-bucket", action="store_true", help="store found menus and queue them in pending/")
    ap.add_argument("--csv", type=Path, default=ROOT / "data/seed/restaurants_full.csv")
    ap.add_argument("--status-csv", type=Path, default=ROOT / "data/seed/discovery.csv")
    ap.add_argument("--recipes-dir", type=Path, default=ROOT / "data/recipes_auto")
    args = ap.parse_args()

    from menudb.config import get_settings

    statuses = set(args.status) if args.status else RETRY
    rows, done = todo(args.csv, args.status_csv, statuses, args.redo)
    if args.slug:
        rows = [r for r in rows if r["slug"] in args.slug]
    rows = rows[:args.limit] if args.limit else rows
    print(f"{len(rows)} restaurants to try", file=sys.stderr)
    if not rows:
        return
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
            counts = run(rows, done, fetcher, get_html, args.status_csv, args.recipes_dir, store)
        finally:
            renderer.close()
    print(" ".join(f"{k}: {v}" for k, v in sorted(counts.items())), file=sys.stderr)


if __name__ == "__main__":
    main()

import csv
import sys
from pathlib import Path

import httpx
import yaml

from menudb.fetch.http import Fetcher

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import find_menus_browser as fm  # noqa: E402

DISHES = ("<main><h1>Autumn tasting menu</h1><ul>"
          "<li>Scallop, yuzu, brown butter</li><li>Turbot, celeriac, truffle</li>"
          "<li>Lamb, garlic, rosemary, potato</li><li>Duck, cherry, beetroot</li>"
          "<li>Chocolate, hazelnut, sorrel, cream</li><li>Oyster, cucumber, dill</li></ul></main>")
HOME = "<p>" + "Welcome to our restaurant by the sea. " * 20 + "</p><a href='/about'>About</a>"


def fetcher(robots: str = "") -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and robots:
            return httpx.Response(200, text=robots)
        return httpx.Response(404)
    return Fetcher("test", transport=httpx.MockTransport(handler), sleep=lambda s: None)


class FakeChrome:
    """What Chrome would show: pages only a browser sees (built by JavaScript)."""
    def __init__(self, pages):
        self.pages, self.loaded = pages, []

    def render(self, url):
        self.loaded.append(url)
        return url, self.pages.get(url.rstrip("/"), "<html><body>Not found</body></html>")


def test_finds_a_menu_at_a_usual_address_the_home_page_doesnt_link():
    chrome = FakeChrome({"https://r.example": HOME, "https://r.example/menu": DISHES})
    f = fetcher()
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, chrome))
    assert d.status == "menu_found" and d.recipe["start"] == "https://r.example/menu"


def test_respects_robots_and_tdm():
    f = fetcher(robots="User-agent: *\nDisallow: /\n")
    chrome = FakeChrome({"https://r.example": HOME})
    assert fm.find_menu("https://r.example/", f, fm.page_loader(f, chrome)).status == "robots"
    assert chrome.loaded == []  # nothing loaded in Chrome
    tdm = '<meta name="tdm-reservation" content="1">' + DISHES
    f = fetcher()
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, FakeChrome({"https://r.example": tdm})))
    assert d.status == "opted_out"


def test_run_records_recipes_and_statuses_and_skips_tried(tmp_path):
    full, status = tmp_path / "full.csv", tmp_path / "discovery.csv"
    with full.open("w", newline="") as fh:
        w = csv.DictWriter(fh, ["slug", "name", "website"])
        w.writeheader()
        w.writerows([{"slug": "a", "name": "A", "website": "https://a.example/"},
                     {"slug": "b", "name": "B", "website": "https://b.example/"},
                     {"slug": "c", "name": "C", "website": "https://c.example/"},
                     {"slug": "d", "name": "D", "website": ""}])
    with status.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fm.FIELDS)
        w.writeheader()
        w.writerows([{"slug": "a", "status": "no_menu_found"}, {"slug": "b", "status": "menu_found"},
                     {"slug": "c", "status": "bot_blocked"}])
    rows, done = fm.todo(full, status, fm.RETRY, redo=False)
    assert [r["slug"] for r in rows] == ["a"]
    chrome = FakeChrome({"https://a.example": HOME, "https://a.example/menus": DISHES})
    f = fetcher()
    counts = fm.run(rows, done, f, fm.page_loader(f, chrome), status, tmp_path / "recipes")
    assert counts == {"menu_found": 1}
    assert yaml.safe_load((tmp_path / "recipes/a.yaml").read_text())["render"] == "js"
    saved = {r["slug"]: r for r in csv.DictReader(status.open())}
    assert saved["a"]["status"] == "menu_found" and saved["a"]["note"].startswith("chrome")
    assert saved["b"]["status"] == "menu_found"  # untouched
    assert fm.todo(full, status, fm.RETRY, redo=False)[0] == []  # a is done


def menu_pdf() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in ("Autumn tasting menu", "Scallop, yuzu, brown butter", "Turbot, celeriac, truffle",
                 "Lamb, garlic, rosemary, potato", "Duck, cherry, beetroot",
                 "Chocolate, hazelnut, sorrel, cream", "Oyster, cucumber, dill"):
        page.insert_text((72, y), line)
        y += 20
    return doc.tobytes()


def site_fetcher(files: dict) -> Fetcher:
    """HTTP for robots.txt, sitemaps and PDFs (pages come from FakeChrome)."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        ctype = "application/pdf" if str(request.url).endswith(".pdf") else "text/plain"
        return httpx.Response(200, content=body if isinstance(body, bytes) else body.encode(),
                              headers={"content-type": ctype})
    return Fetcher("test", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_finds_a_pdf_only_mentioned_in_a_script():
    home = HOME + '<script>window.__DATA__={"menu":"https:\\/\\/cdn.example.net\\/f\\/carte-automne.pdf"}</script>'
    f = site_fetcher({"https://cdn.example.net/f/carte-automne.pdf": menu_pdf()})
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, FakeChrome({"https://r.example": home})))
    assert d.status == "menu_found" and d.kind == "pdf"
    assert d.recipe == {"start": "https://cdn.example.net/f/carte-automne.pdf", "content": "pdf"}
    assert "page source" in d.note


def test_finds_a_menu_page_through_the_sitemap():
    f = site_fetcher({"https://r.example/robots.txt": "User-agent: *\nAllow: /\nSitemap: https://r.example/sm.xml\n",
                      "https://r.example/sm.xml": "<urlset><url><loc>https://r.example/about</loc></url>"
                                                  "<url><loc>https://r.example/fr/la-carte-du-moment</loc></url></urlset>"})
    chrome = FakeChrome({"https://r.example": HOME, "https://r.example/fr/la-carte-du-moment": DISHES})
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, chrome))
    assert d.status == "menu_found" and d.menu_url == "https://r.example/fr/la-carte-du-moment"
    assert "sitemap" in d.note


def test_finds_a_menu_named_like_anything_through_the_navigation():
    home = HOME + '<nav><a href="/story">Our story</a><a href="/the-journey">The Journey</a><a href="/gift-vouchers">Gifts</a></nav>'
    chrome = FakeChrome({"https://r.example": home, "https://r.example/story": HOME, "https://r.example/the-journey": DISHES})
    f = fetcher()
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, chrome))
    assert d.status == "menu_found" and d.menu_url == "https://r.example/the-journey"
    assert "navigation" in d.note
    assert "https://r.example/gift-vouchers" not in chrome.loaded


def test_each_page_is_loaded_once_and_the_budget_holds():
    links = "".join(f'<a href="/p{i}">Page {i}</a>' for i in range(30))
    chrome = FakeChrome({"https://r.example": HOME + links})
    f = fetcher()
    d = fm.find_menu("https://r.example/", f, fm.page_loader(f, chrome))
    assert d.status == "no_menu_found"
    assert len(chrome.loaded) == len(set(chrome.loaded))
    assert len(chrome.loaded) <= 1 + 8 + fm.EXTRA_BUDGET + 2 * len(fm.COMMON_PATHS)


def test_store_only_stores_found_menus_without_searching(tmp_path):
    status, recipes = tmp_path / "discovery.csv", tmp_path / "recipes"
    recipes.mkdir()
    for s in ("a", "b", "c"):
        (recipes / f"{s}.yaml").write_text("start: https://x.example/menu\nrender: js\n")
    done = {"a": {"slug": "a", "status": "menu_found", "stored": "", "note": "chrome: store failed: ValueError"},
            "b": {"slug": "b", "status": "menu_maybe", "stored": "2", "note": "chrome: ok"},
            "c": {"slug": "c", "status": "menu_maybe", "stored": "", "note": "chrome: ok"},
            "d": {"slug": "d", "status": "menu_found", "stored": "", "note": "discover"}}
    slugs = fm.unstored(done, recipes)
    assert slugs == ["a", "c"]  # b is stored, d wasn't found by this script
    counts = fm.store_found(slugs, done, lambda slug, recipe: 1 if slug == "a" else -1, status, recipes)
    assert counts == {"stored": 1, "opted_out": 1}
    saved = {r["slug"]: r for r in csv.DictReader(status.open())}
    assert saved["a"]["stored"] == "1" and "failed" not in saved["a"]["note"]
    assert saved["c"]["status"] == "opted_out" and not (recipes / "c.yaml").exists()
    assert fm.unstored(saved, recipes) == []

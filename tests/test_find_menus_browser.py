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

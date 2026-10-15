"""Recipe files and fetch_documents, offline."""

import httpx

from menudb.fetch.http import Fetcher
from menudb.pipeline import fetch_documents, html_getter
from menudb.recipes import Recipe, load_recipes


def test_load_single_and_labeled(tmp_path):
    (tmp_path / "a.yaml").write_text("start: https://r.example/menu\n")
    (tmp_path / "b.yaml").write_text(
        "lunch:\n  start: https://r.example/lunch\ndinner:\n  start: https://r.example/dinner\n  render: js\n")
    assert list(load_recipes(tmp_path / "a.yaml")) == ["main"]
    b = load_recipes(tmp_path / "b.yaml")
    assert list(b) == ["lunch", "dinner"] and b["dinner"].render == "js"


def test_fetch_documents_skips_not_modified(fixture_text):
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text=fixture_text("index.html"), headers={"content-type": "text/html"})
        if request.url.path == "/food/":
            return httpx.Response(200, text=fixture_text("menus_page.html"), headers={"content-type": "text/html"})
        if request.url.path.endswith(".pdf"):
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf", "etag": '"v1"'})
        return httpx.Response(404)

    fetcher = Fetcher("t", min_interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    recipe = Recipe.model_validate({
        "start": "https://r.example/",
        "steps": [{"follow": {"text_matches": "(?i)menu"}},
                  {"select": {"css": "#menus a[href$='.pdf']", "pick": "all"}}],
        "content": "pdf",
    })
    validators: dict = {}
    docs = list(fetch_documents(recipe, fetcher, html_getter(recipe, fetcher), validators))
    assert [d.url.rsplit("/", 1)[1] for d in docs] == ["autumn-tasting.pdf", "autumn-vegetarian.pdf"]
    assert all(v["etag"] == '"v1"' for v in validators.values())
    assert list(fetch_documents(recipe, fetcher, html_getter(recipe, fetcher), validators)) == []

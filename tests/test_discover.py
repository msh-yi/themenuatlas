import httpx

from menudb.discover import discover, food_score, menu_links
from menudb.fetch.http import Fetcher

DISHES = (
    "<main><h1>Autumn tasting menu</h1><ul>"
    "<li>Scallop, yuzu, brown butter</li><li>Turbot, celeriac, truffle</li>"
    "<li>Lamb, garlic, rosemary, potato</li><li>Duck, cherry, beetroot</li>"
    "<li>Chocolate, hazelnut, sorrel, cream</li><li>Oyster, cucumber, dill</li></ul></main>"
)


def site(pages: dict[str, tuple[str, bytes]]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" or request.url.path.startswith("/.well-known"):
            return httpx.Response(404)
        ctype, body = pages.get(str(request.url), ("text/html", b""))
        if not body:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": ctype})
    return Fetcher("test", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_food_score_counts_distinct_words():
    assert food_score("Scallop, scallops, yuzu and brown butter") == 3
    assert food_score("先付 鱧 松茸") == 3
    assert food_score(None) == 0


def test_menu_links_ranks_menu_and_skips_gift_cards_and_other_sites():
    html = """<a href="/menu/">Menu</a><a href="/gift-vouchers">Gift menu</a>
              <a href="https://instagram.com/x">Menu</a><a href="/about">About</a>
              <a href="/files/tasting-menu.pdf">Download</a><a href="/img/menu.jpg">Menu</a>"""
    links = menu_links("https://r.example/", html)
    assert [link.url for link in links] == ["https://r.example/menu/", "https://r.example/files/tasting-menu.pdf"]


def test_finds_html_menu_page():
    fetcher = site({
        "https://r.example/": ("text/html", b'<p>Welcome to our restaurant in town, a place to eat.</p>'
                               b'<a href="/menus">Menus</a><a href="/book">Book a table</a>' + b"x" * 400),
        "https://r.example/menus": ("text/html", DISHES.encode()),
    })
    d = discover("https://r.example/", fetcher)
    assert d.status == "menu_found"
    assert d.recipe == {"start": "https://r.example/menus", "content": "html_main"}


def test_menu_pdf_recipe_follows_the_link_by_name(monkeypatch):
    monkeypatch.setattr("menudb.discover.document_text", lambda kind, data: DISHES if kind == "pdf" else "")
    fetcher = site({
        "https://r.example/": ("text/html", b'<a href="/food">Food</a>' + b"<p>" + b"words " * 100 + b"</p>"),
        "https://r.example/food": ("text/html", b'<a href="/f/autumn-menu.pdf">Tasting menu (PDF)</a>'),
        "https://r.example/f/autumn-menu.pdf": ("application/pdf", b"%PDF-1.4 stub"),
    })
    d = discover("https://r.example/", fetcher)
    assert d.status == "menu_found" and d.kind == "pdf"
    assert d.recipe == {"start": "https://r.example/food",
                        "steps": [{"follow": {"href_matches": r"autumn\-menu\.pdf"}}], "content": "pdf"}


def test_empty_shell_needs_js():
    fetcher = site({"https://r.example/": ("text/html", b"<div id=root></div><script>app()</script>")})
    assert discover("https://r.example/", fetcher).status == "needs_js"


def test_no_menu():
    fetcher = site({"https://r.example/": ("text/html", b"<p>" + b"We are open Tuesday to Saturday. " * 20 + b"</p>")})
    assert discover("https://r.example/", fetcher).status == "no_menu_found"


def test_cloudflare_refusal_is_bot_blocked():
    from menudb.fetch.http import bot_wall

    assert bot_wall(403, {"server": "cloudflare", "cf-ray": "x"}, "") == "cloudflare"
    assert bot_wall(403, {}, "<title>Just a moment...</title>") == "cloudflare"
    assert bot_wall(404, {"server": "cloudflare"}, "") is None
    assert bot_wall(403, {"server": "nginx"}, "Forbidden") is None


def test_challenge_page_with_200_is_bot_blocked():
    fetcher = site({"https://r.example/": ("text/html", b"<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>")})
    assert discover("https://r.example/", fetcher).status == "bot_blocked"

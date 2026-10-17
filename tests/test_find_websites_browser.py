import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from find_websites_browser import BotCheck, parse_results, run, search_url  # noqa: E402


def page(*results: tuple[str, str]) -> str:
    links = "".join(f'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg={quote(u, safe="")}&rut=x">{t}</a></div>'
                    for u, t in results)
    return f"<html><body>{links}</body></html>"


def test_parse_results_unwraps_redirects_and_skips_ads():
    html = page(("https://kikunoi.jp/", "菊乃井"), ("https://duckduckgo.com/y.js?ad=1", "Ad"))
    assert parse_results(html) == [{"url": "https://kikunoi.jp/", "title": "菊乃井"}]


def test_parse_results_stops_at_the_bot_check():
    with pytest.raises(BotCheck):
        parse_results('<div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>')


def test_search_url_uses_the_country_region():
    assert search_url("Kikunoi Kyoto", "JP").endswith("?q=Kikunoi+Kyoto&kl=jp-jp")
    assert "kl=" not in search_url("x", "")


def rows():
    return [
        {"slug": "shofukuro", "name": "Shōfukurō", "city": "Kyoto", "country": "JP", "website": "", "website_source": ""},
        {"slug": "kikunoi", "name": "Kikunoi Honten", "city": "Kyoto", "country": "JP", "website": "", "website_source": ""},
        {"slug": "chez-x", "name": "Chez X", "city": "Lyon", "country": "FR", "website": "", "website_source": ""},
        {"slug": "done", "name": "Done", "city": "Paris", "country": "FR", "website": "https://done.fr/", "website_source": ""},
    ]


def fake_ddg(calls):
    pages = {"Shōfukurō Kyoto": page(("https://www.tripadvisor.jp/x", "Shofukuro"), ("https://syofukuro.jp/", "")),
             "菊乃井 本店": page(("https://kikunoi.jp/", "菊乃井 本店 | 京料理"))}

    def fetch(url):
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)["q"][0]
        calls.append(q)
        if q == "Chez X Lyon":
            return '<div class="anomaly-modal"></div>'
        return pages.get(q, "<html></html>")
    return fetch


def test_run_finds_matches_caches_and_stops_at_bot_check(tmp_path):
    rs, calls = rows(), []
    natives = {("kikunoi-honten", "JP"): "菊乃井 本店"}
    found, stopped = run(rs, fake_ddg(calls), tmp_path, natives, pause=lambda: None)
    assert stopped == "bot check"
    got = {r["slug"]: (r["website"], r["website_source"]) for r in rs}
    assert got["shofukuro"] == ("https://syofukuro.jp/", "search_browser")
    assert got["kikunoi"] == ("https://kikunoi.jp/", "search_browser")
    assert got["chez-x"] == ("", "")
    assert [f["how"] for f in found] == ["name", "native name"]  # kikunoi.jp carries the name itself
    assert calls == ["Shōfukurō Kyoto", "Kikunoi Honten Kyoto", "菊乃井 本店", "Chez X Lyon"]
    # a rerun reuses the cache and only searches what is left
    rs2, calls2 = rows(), []
    run(rs2, fake_ddg(calls2), tmp_path, natives, countries={"JP"}, pause=lambda: None)
    assert calls2 == []


def test_run_respects_the_limit(tmp_path):
    calls = []
    _, stopped = run(rows(), fake_ddg(calls), tmp_path, {}, limit=1, pause=lambda: None)
    assert stopped == "limit" and calls == ["Shōfukurō Kyoto"]


def test_google_results_and_bot_check(tmp_path):
    from find_websites_browser import parse_google
    html = ('<div id="search"><a href="https://www.tripadvisor.jp/x"><br><h3>Kikunoi - Tripadvisor</h3></a>'
            '<a href="/url?q=https://kikunoi.jp/&sa=U"><h3>菊乃井 本店 | 京料理</h3></a>'
            '<a href="https://maps.google.com/x"><h3>Map</h3></a><h3>People also ask</h3></div>')
    assert parse_google(html) == [{"url": "https://www.tripadvisor.jp/x", "title": "Kikunoi - Tripadvisor"},
                                  {"url": "https://kikunoi.jp/", "title": "菊乃井 本店 | 京料理"}]
    with pytest.raises(BotCheck):
        parse_google('<form id="captcha-form" action="/sorry/index">')
    assert search_url("Kikunoi Kyoto", "JP", "google").endswith("?q=Kikunoi+Kyoto&num=20&gl=jp")
    calls = []
    found, _ = run(rows()[:1], lambda url: calls.append(url) or html.replace("kikunoi.jp", "syofukuro.jp"),
                   tmp_path, {}, engine="google", pause=lambda: None)
    assert found[0]["website"] == "https://syofukuro.jp/" and (tmp_path / "google").is_dir()

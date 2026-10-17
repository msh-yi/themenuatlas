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

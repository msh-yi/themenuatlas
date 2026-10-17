import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import find_websites  # noqa: E402
from find_websites import local_params, official, pick, pick_native  # noqa: E402


def test_official_accepts_own_domains():
    assert official("Alinea", "Chicago", "https://www.alinearestaurant.com/")
    assert official("Le Bernardin", "New York", "https://www.le-bernardin.com/")
    assert official("Sushi Saito", "Tokyo", "https://sushisaito.com/")
    assert official("Osteria Francescana", "Modena", "https://osteriafrancescana.it/")


def test_official_rejects_articles_directories_and_partial_matches():
    assert not official("Plaza Athénée", "Paris", "https://elitetraveler.com/")
    assert not official("Korai Kitchen", "Houston", "https://usarestaurants.info/")
    assert not official("Asperges", "Paris", "https://rolandlim.wordpress.com/")
    assert not official("CTC Urban Gastronomy", "Athens", "https://tinyurbankitchen.com/")
    assert not official("Tacos Wuey Detroit", "Detroit", "https://www.clickondetroit.com/")


def test_pick_skips_deep_pages():
    results = [{"url": "https://alinearestaurant.com/a/b/c", "title": ""},
               {"url": "https://www.alinearestaurant.com/menu", "title": ""}]
    assert pick("Alinea", "Chicago", results) == "https://www.alinearestaurant.com/"


def test_romaji_spellings_match_only_with_romaji():
    assert not official("Shōfukurō", "Kyoto", "https://syofukuro.jp/")
    assert official("Shōfukurō", "Kyoto", "https://syofukuro.jp/", romaji=True)
    assert official("Tsukiji Shinbashi", "Tokyo", "https://tukijisimbasi.jp/", romaji=True)
    assert not official("Sushi Saito", "Tokyo", "https://tabelog.com/", romaji=True)
    assert not official("Kitcho", "Kyoto", "https://kyoto-guide.jp/", romaji=True)


def test_pick_native_needs_the_name_on_a_homepage():
    results = [{"url": "https://www.tripadvisor.jp/x", "title": "菊乃井 本店"},
               {"url": "https://blog.example.jp/2026/kikunoi", "title": "菊乃井 本店に行った"},
               {"url": "https://kikunoi.jp/", "title": "菊乃井 | 京料理"}]
    assert pick_native("菊乃井", results) == "https://kikunoi.jp/"
    assert pick_native("菊乃井", results[:2]) == ""
    assert pick_native("Kikunoi", results) == ""  # only names in their own script


def test_local_params():
    assert local_params("JP") == {"country": "JP", "search_lang": "jp"}
    assert local_params("MO") == {"search_lang": "zh-hant"}  # Brave can't limit to Macau
    assert local_params("") == {}


def test_local_pass_end_to_end(tmp_path, monkeypatch):
    rows = [
        {"slug": "shofukuro", "name": "Shōfukurō", "city": "Kyoto", "country": "JP", "website": "", "website_source": ""},
        {"slug": "kikunoi", "name": "Kikunoi Honten", "city": "Kyoto", "country": "JP", "website": "", "website_source": ""},
        {"slug": "has-site", "name": "Has Site", "city": "Paris", "country": "FR", "website": "https://a.fr/", "website_source": ""},
        {"slug": "nothing", "name": "Nothing", "city": "Seoul", "country": "KR", "website": "", "website_source": ""},
    ]
    monkeypatch.setattr(find_websites, "native_names", lambda cache, client: {("kikunoi-honten", "JP"): "菊乃井 本店"})
    calls = []

    def search(q, extra=None):
        calls.append((q, extra))
        return {"Shōfukurō Kyoto": [{"url": "https://syofukuro.jp/menu", "title": ""}],
                "菊乃井 本店": [{"url": "https://kikunoi.jp/", "title": "菊乃井 本店 | 京料理"}]}.get(q, [])

    class Args:
        cache, limit = tmp_path, None
    monkeypatch.setattr(find_websites.time, "sleep", lambda s: None)
    find_websites.local_pass(rows, Args, search, None, {"a.fr"})
    got = {r["slug"]: (r["website"], r["website_source"]) for r in rows}
    assert got["shofukuro"] == ("https://syofukuro.jp/", "search_local")
    assert got["kikunoi"] == ("https://kikunoi.jp/", "search_local")
    assert got["nothing"] == ("", "")
    assert ("Shōfukurō Kyoto", {"country": "JP", "search_lang": "jp"}) in calls
    assert len(calls) == 4  # has-site is skipped; kikunoi needs its native-name search
    find_websites.local_pass(rows[3:], Args, lambda q, e=None: calls.append(q) or [], None, set())
    assert len(calls) == 4  # cached

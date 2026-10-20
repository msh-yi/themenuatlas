import csv
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from find_websites_gemini import ask, check, prompt, run  # noqa: E402

from menudb.fetch.http import Fetcher

ROW = {"slug": "kikunoi", "name": "Kikunoi Honten", "city": "Kyoto", "country": "JP"}
PAGES = {
    "https://kikunoi.jp/": "<title>菊乃井</title><p>Kikunoi, Higashiyama, Kyoto</p>",
    "https://other.jp/": "<title>Kikunoi</title><p>Tokyo branch</p>",
    "https://parked.com/": "<p>This domain is for sale</p>",
    "https://unrelated.com/": "<p>Hello</p>",
}


def fetcher():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        body = PAGES.get(str(req.url))
        return httpx.Response(200, text=body, headers={"content-type": "text/html"}) if body else httpx.Response(404)
    return Fetcher("test", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_prompt_asks_in_the_local_language():
    assert "Japanese" in prompt(ROW) and "Kikunoi Honten" in prompt(ROW)


def test_ask_parses_the_json_answer_and_sources():
    def handler(req):
        assert req.headers["x-goog-api-key"] == "k" and json.loads(req.content)["tools"] == [{"google_search": {}}]
        return httpx.Response(200, json={"candidates": [{
            "content": {"parts": [{"text": 'Its site ({"x": 1} aside) is kikunoi.jp. ```json\n{"website": "https://kikunoi.jp/", "closed": false, "native_name": "菊乃井"}\n```'}]},
            "groundingMetadata": {"webSearchQueries": ["kikunoi"], "groundingChunks": [{"web": {"title": "kikunoi.jp"}}]}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = ask(c, "k", "m", "q")
    assert got == {"website": "https://kikunoi.jp/", "closed": False, "native_name": "菊乃井", "grounded": True,
                   "sources": ["kikunoi.jp"]}


def test_check_needs_name_and_city_and_drops_bad_hosts():
    with fetcher() as f:
        assert check(ROW, "https://kikunoi.jp/", "", f, set())[0] == "ok"
        assert check(ROW, "https://other.jp/", "", f, set())[0] == "name_only"
        assert check(ROW, "https://parked.com/", "", f, set())[0] == "parked"
        assert check(ROW, "https://unrelated.com/", "", f, set())[0] == "name_missing"
        assert check(ROW, "https://tabelog.com/kyoto/1", "", f, set())[0] == "not_official_host"
        assert check(ROW, "https://kikunoi.jp/", "", f, {("kikunoi", "https://kikunoi.jp")})[0] == "rejected_before"


def test_run_records_matches_and_caches_answers(tmp_path):
    review, rejected = tmp_path / "review.csv", tmp_path / "rejected.csv"
    rejected.write_text("slug,website,reason,rejected\n")
    rows = [ROW, {**ROW, "slug": "closed-one"}, {**ROW, "slug": "maybe"}, {**ROW, "slug": "guess"}]
    answers = {"kikunoi": {"website": "https://kikunoi.jp/"}, "closed-one": {"closed": True, "grounded": True},
               "maybe": {"website": "https://other.jp/"}, "guess": {"closed": True, "grounded": False}}
    asked = []

    def ask_one(text):
        slug = next(s for s in answers if s not in asked)
        asked.append(slug)
        return answers[slug]
    with fetcher() as f:
        counts = run(rows, ask_one, f, tmp_path / "cache", review, rejected, sleep=lambda s: None)
        assert counts == {"ok": 1, "closed": 1, "name_only": 1, "not_searched": 1}
        out = {r["slug"]: r["approve"] for r in csv.DictReader(review.open())}
        assert out == {"kikunoi": "y", "maybe": ""}
        run(rows, lambda t: (_ for _ in ()).throw(AssertionError("asked again")), f, tmp_path / "cache",
            review, rejected, sleep=lambda s: None)

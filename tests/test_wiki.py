"""wiki-links: Wikipedia lookups for ingredients, offline through httpx.MockTransport."""

import httpx

from menudb import wiki

PAGES = {
    "Rye": {"title": "Rye", "description": "Species of grain", "pageprops": {"wikibase_item": "Q12099"}},
    "Beurre noisette": {"title": "Beurre noisette", "description": "Sauce used in French cuisine",
                        "pageprops": {"wikibase_item": "Q1364507"}},
    "Sole": {"title": "Sole", "description": "Topics referred to by the same term",
             "pageprops": {"disambiguation": "", "wikibase_item": "Q1"}},
    "Sole (fish)": {"title": "Sole (fish)", "description": "Common name for several groups of flatfish",
                    "pageprops": {"wikibase_item": "Q2"}},
    "Lamb": {"title": "Lamb", "description": "Topics referred to by the same term", "pageprops": {"disambiguation": ""}},
    "Tasting": {"title": "Tasting", "description": "Sampling something", "pageprops": {}},
}
REDIRECTS = {"Brown butter": "Beurre noisette"}


def transport(calls, busy=0):
    def handler(request):
        calls.append(request)
        if len(calls) <= busy:
            return httpx.Response(429, headers={"retry-after": "3"})
        titles = request.url.params["titles"].split("|")
        normalized = [{"from": t, "to": t[0].upper() + t[1:]} for t in titles if t[0].islower()]
        norm = {n["from"]: n["to"] for n in normalized}
        redirects, pages = [], []
        for t in titles:
            cur = norm.get(t, t)
            if cur in REDIRECTS:
                redirects.append({"from": cur, "to": REDIRECTS[cur]})
                cur = REDIRECTS[cur]
            pages.append(PAGES.get(cur, {"title": cur, "missing": True}))
        return httpx.Response(200, json={"query": {"normalized": normalized, "redirects": redirects, "pages": pages}})
    return httpx.MockTransport(handler)


def client(calls, busy=0, slept=None):
    return wiki.Client("test", transport=transport(calls, busy),
                       sleep=(slept.append if slept is not None else lambda s: None))


def test_resolve_statuses():
    calls = []
    links = wiki.resolve(["rye", "brown butter", "sole", "lamb", "tasting", "zzz"], client(calls))
    assert links["rye"].status == "ok" and links["rye"].wikidata == "Q12099"
    assert links["brown butter"].title == "Beurre noisette"  # followed the redirect
    assert links["sole"].status == "ok" and links["sole"].title == "Sole (fish)"  # one qualified hit
    assert links["lamb"].status == "ambiguous" and links["lamb"].url is None  # left for a person
    assert links["tasting"].status == "check" and links["tasting"].url is None
    assert links["zzz"].status == "missing"
    assert links["sole"].url == "https://en.wikipedia.org/wiki/Sole_(fish)"


def test_waits_out_rate_limits():
    calls, slept = [], []
    links = wiki.resolve(["rye"], client(calls, busy=2, slept=slept))
    assert links["rye"].status == "ok"
    assert len(calls) == 3 and slept.count(3.0) == 2  # Retry-After honoured twice


def test_update_is_incremental_and_keeps_hand_edits(tmp_path):
    path = tmp_path / "wikipedia.csv"
    calls = []
    wiki.update(["rye", "lamb"], client(calls), path)
    links = wiki.load(path)
    links["lamb"] = wiki.Link("lamb", "manual", "Lamb and mutton")
    wiki.save(links, path)

    calls.clear()
    counts = wiki.update(["rye", "lamb", "sole"], client(calls), path, retry=True)
    assert counts["looked_up"] == 1  # only the new name; the manual row is never redone
    assert wiki.load(path)["lamb"].title == "Lamb and mutton"
    assert wiki.urls(path) == {
        "lamb": "https://en.wikipedia.org/wiki/Lamb_and_mutton",
        "rye": "https://en.wikipedia.org/wiki/Rye",
        "sole": "https://en.wikipedia.org/wiki/Sole_(fish)",
    }

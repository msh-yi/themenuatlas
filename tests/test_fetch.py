import httpx
import pytest

from menudb.fetch.http import Fetcher, FetchError, RobotsDisallowed

UA = "themenuproject-test/0.1"


def make_fetcher(routes, calls=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(request)
        fn = routes.get(request.url.path)
        return fn(request) if fn else httpx.Response(404)

    sleeps = []
    f = Fetcher(UA, min_interval=5, transport=httpx.MockTransport(handler),
                sleep=sleeps.append, clock=lambda: 0.0)
    return f, sleeps


def test_robots_disallow():
    f, _ = make_fetcher({
        "/robots.txt": lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /private/"),
        "/menu": lambda r: httpx.Response(200, text="ok"),
    })
    assert f.get("https://r.example/menu").status == 200
    with pytest.raises(RobotsDisallowed):
        f.get("https://r.example/private/menu")


def test_robots_5xx_means_disallow_all():
    f, _ = make_fetcher({"/robots.txt": lambda r: httpx.Response(503)})
    assert not f.allowed("https://r.example/menu")


def test_conditional_request_and_304():
    calls = []
    f, _ = make_fetcher({
        "/menu": lambda r: httpx.Response(304) if r.headers.get("if-none-match") == '"v1"'
        else httpx.Response(200, text="menu", headers={"etag": '"v1"'}),
    }, calls)
    first = f.get("https://r.example/menu")
    assert first.etag == '"v1"' and not first.not_modified
    assert f.get("https://r.example/menu", etag=first.etag).not_modified


def test_tdm_signals():
    f, _ = make_fetcher({
        "/.well-known/tdmrep.json": lambda r: httpx.Response(
            200, json=[{"location": "/reserved/*", "tdm-reservation": 1}]),
        "/reserved/menu": lambda r: httpx.Response(200, text="x"),
        "/header": lambda r: httpx.Response(200, text="x", headers={"tdm-reservation": "1"}),
        "/meta": lambda r: httpx.Response(
            200, text='<meta name="tdm-reservation" content="1">', headers={"content-type": "text/html"}),
        "/open": lambda r: httpx.Response(200, text="<p>menu</p>", headers={"content-type": "text/html"}),
    })
    assert f.get("https://r.example/reserved/menu").tdm_reserved
    assert f.get("https://r.example/header").tdm_reserved
    assert f.get("https://r.example/meta").tdm_reserved
    assert not f.get("https://r.example/open").tdm_reserved


def test_rate_limit_and_retry():
    attempts = {"n": 0}

    def flaky(r):
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] < 3 else httpx.Response(200, text="ok")

    f, sleeps = make_fetcher({"/menu": flaky})
    assert f.get("https://r.example/menu").status == 200
    assert attempts["n"] == 3
    assert sleeps  # waited between same-host requests and backed off

    f, _ = make_fetcher({"/gone": lambda r: httpx.Response(410)})
    with pytest.raises(FetchError):
        f.get("https://r.example/gone")

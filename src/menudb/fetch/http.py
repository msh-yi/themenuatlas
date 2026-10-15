"""Polite HTTP fetching: robots.txt, TDM opt-out signals, per-host rate limits,
conditional requests, and a small retry budget."""

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

_TDM_META = re.compile(
    r"""<meta[^>]+name=["']tdm-reservation["'][^>]+content=["']1["']""", re.I
)


class FetchError(Exception):
    def __init__(self, message: str, bot_wall: str | None = None):
        super().__init__(message)
        self.bot_wall = bot_wall  # vendor name when the refusal looks like bot protection


class RobotsDisallowed(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str
    etag: str | None
    last_modified: str | None
    tdm_reserved: bool

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass
class _Host:
    robots: RobotFileParser | None = None
    tdm_rules: list[dict] = field(default_factory=list)
    last_request: float = 0.0


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 5.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._hosts: dict[str, _Host] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- per-host policy -------------------------------------------------

    def _host(self, url: str) -> tuple[str, _Host]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._hosts:
            host = self._hosts[origin] = _Host()
            host.robots = self._load_robots(origin, host)
            host.tdm_rules = self._load_tdmrep(origin, host)
        return origin, self._hosts[origin]

    def _wait_turn(self, host: _Host) -> None:
        wait = host.last_request + self.min_interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        host.last_request = self._clock()

    def _raw_get(self, url: str, host: _Host, headers: dict | None = None) -> httpx.Response:
        self._wait_turn(host)
        return self._client.get(url, headers=headers)

    def _load_robots(self, origin: str, host: _Host) -> RobotFileParser | None:
        """RFC 9309: a 4xx robots.txt means no restrictions; 5xx or network failure
        means assume everything is disallowed."""
        rp = RobotFileParser()
        try:
            r = self._raw_get(origin + "/robots.txt", host)
        except httpx.HTTPError:
            rp.disallow_all = True
            return rp
        if 400 <= r.status_code < 500:
            return None
        if r.status_code >= 500:
            rp.disallow_all = True
            return rp
        rp.parse(r.text.splitlines())
        return rp

    def _load_tdmrep(self, origin: str, host: _Host) -> list[dict]:
        """W3C TDMRep well-known file: [{"location": "/path*", "tdm-reservation": 1}, ...]."""
        try:
            r = self._raw_get(origin + "/.well-known/tdmrep.json", host)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
        except (httpx.HTTPError, ValueError):
            pass
        return []

    def allowed(self, url: str) -> bool:
        _, host = self._host(url)
        return host.robots is None or host.robots.can_fetch(self.user_agent, url)

    def _tdm_from_wellknown(self, url: str, host: _Host) -> bool:
        path = urlsplit(url).path or "/"
        for rule in host.tdm_rules:
            if fnmatch.fnmatch(path, str(rule.get("location", ""))):
                return str(rule.get("tdm-reservation")) == "1"
        return False

    # -- public ------------------------------------------------------------

    def get(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        retries: int = 2,
    ) -> FetchResult:
        origin, host = self._host(url)
        if not self.allowed(url):
            raise RobotsDisallowed(url)

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        for attempt in range(retries + 1):
            try:
                r = self._raw_get(url, host, headers)
            except httpx.TransportError as e:
                if attempt == retries:
                    raise FetchError(f"{url}: {type(e).__name__}") from e
                self._sleep(2**attempt * self.min_interval)
                continue
            if r.status_code >= 500 and attempt < retries:
                self._sleep(2**attempt * self.min_interval)
                continue
            break

        if r.status_code >= 400:
            raise FetchError(f"{url}: HTTP {r.status_code}", bot_wall(r.status_code, r.headers, r.text[:20000]))

        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        tdm = (
            r.headers.get("tdm-reservation", "").strip() == "1"
            # Wayback replays carry the original response headers with this prefix
            or r.headers.get("x-archive-orig-tdm-reservation", "").strip() == "1"
            or self._tdm_from_wellknown(str(r.url), host)
            or ("html" in ctype and bool(_TDM_META.search(r.text[:20000])))
        )
        return FetchResult(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            content=r.content,
            content_type=ctype,
            etag=r.headers.get("etag"),
            last_modified=r.headers.get("last-modified"),
            tdm_reserved=tdm,
        )

    def get_html(self, url: str) -> tuple[str, str]:
        """Fetch a navigation page; returns (final_url, html)."""
        res = self.get(url)
        return res.final_url, res.content.decode(errors="replace")


_WALLS = [
    ("cloudflare", re.compile(r"(?i)just a moment\.\.\.|cf-chl|challenge-platform|cf_chl_opt|attention required! \| cloudflare")),
    ("datadome", re.compile(r"(?i)captcha-delivery\.com|datadome")),
    ("perimeterx", re.compile(r"(?i)px-captcha|perimeterx|_pxhd")),
    ("incapsula", re.compile(r"(?i)incapsula|_incapsula_resource|imperva")),
    ("akamai", re.compile(r"(?i)akamai bot manager|_abck|ak_bmsc|access denied.{0,200}reference #")),
    ("sucuri", re.compile(r"(?i)sucuri website firewall|sucuri cloudproxy")),
    ("captcha", re.compile(r"(?i)g-recaptcha|hcaptcha\.com|cf-turnstile|captcha")),
]


def bot_wall(status: int, headers, body: str) -> str | None:
    """Name the bot protection behind a refusal, from headers and the challenge page.

    Only labels what happened; nothing here tries to get past it."""
    if status not in (401, 403, 405, 429, 503):
        return None
    server = (headers.get("server") or "").lower()
    if "cf-mitigated" in headers or ("cloudflare" in server and status in (403, 429, 503)):
        return "cloudflare"
    if "x-datadome" in headers or "x-dd-b" in headers:
        return "datadome"
    if "x-iinfo" in headers or "x-cdn" in headers and "incapsula" in headers.get("x-cdn", "").lower():
        return "incapsula"
    if "x-sucuri-id" in headers:
        return "sucuri"
    for name, rx in _WALLS:
        if rx.search(body):
            return name
    return None


def absolute(base: str, href: str) -> str:
    return urljoin(base, href)

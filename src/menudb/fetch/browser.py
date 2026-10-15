"""Headless Chromium for JS-rendered sites. Used only when a recipe says render: js.

Robots and rate limits are enforced by the caller through Fetcher.allowed().
"""

from playwright.sync_api import Browser, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


class BrowserRenderer:
    def __init__(self, user_agent: str, executable_path: str | None = None, timeout_ms: int = 30_000):
        self.user_agent = user_agent
        self.executable_path = executable_path
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser: Browser | None = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(executable_path=self.executable_path)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def render(self, url: str) -> tuple[str, str]:
        """Returns (final_url, rendered_html)."""
        assert self._browser, "use BrowserRenderer as a context manager"
        page = self._browser.new_page(user_agent=self.user_agent)
        try:
            page.goto(url, wait_until="load", timeout=self.timeout_ms)
            try:
                # many sites never go network-idle (analytics, chat widgets); take what loaded
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeout:
                pass
            return page.url, page.content()
        finally:
            page.close()

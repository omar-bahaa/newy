from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig


class BrowserUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class RenderedPage:
    url: str
    html: str
    title: str = ""


class BrowserSession:
    def __init__(self, config: AppConfig):
        self.config = config
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    def __enter__(self) -> BrowserSession:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise BrowserUnavailableError("playwright is not installed") from exc
        self._playwright = sync_playwright().start()
        browser_type = getattr(self._playwright, self.config.browser.engine, None)
        if browser_type is None:
            raise BrowserUnavailableError(f"unsupported browser engine: {self.config.browser.engine}")
        self._browser = browser_type.launch(headless=self.config.browser.headless)
        self._page = self._browser.new_page(user_agent=self.config.user_agent)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._page = None
        self._playwright = None

    def open(self, url: str) -> RenderedPage:
        if self._page is None:
            raise BrowserUnavailableError("browser session not started")
        self._page.goto(url, wait_until=self.config.browser.wait_until, timeout=self.config.browser.timeout_seconds * 1000)
        return RenderedPage(url=self._page.url, html=self._page.content(), title=self._page.title())


@dataclass(slots=True)
class BrowserPageRenderer:
    config: AppConfig

    def available(self) -> bool:
        if not self.config.browser.enabled:
            return False
        try:
            import playwright.sync_api  # type: ignore  # noqa: F401
        except ImportError:
            return False
        return True

    def session(self) -> BrowserSession | None:
        if not self.available():
            return _NullBrowserSession()
        return BrowserSession(self.config)


class _NullBrowserSession:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

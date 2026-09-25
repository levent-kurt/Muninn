"""Persistent Playwright-stealth Chromium driver.

A single browser + single BrowserContext is started once (app lifespan) and
reused for every engine request. Each search opens a fresh ``Page`` in that
context, scrapes it, and closes it - so no browser process is ever launched or
torn down per request. Stealth evasions are injected automatically for every
page via ``playwright_stealth.Stealth.use_async``.
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright_stealth import Stealth

from app.config import Settings

logger = logging.getLogger(__name__)


class BrowserDriverError(Exception):
    """Raised when the underlying browser cannot fulfil a request."""


class BrowserDriver:
    """Owns one persistent Chromium browser and BrowserContext with stealth."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stealth_cm: Stealth | None = None
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._started = False
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Launch the persistent browser/context and apply stealth hooks."""
        if self._started:
            return
        stealth = Stealth()
        self._stealth_cm = stealth.use_async(async_playwright())
        self._pw = await self._stealth_cm.__aenter__()

        launch_args = list(self._settings.browser_args) + ["--no-sandbox"]
        self._browser = await self._pw.chromium.launch(
            headless=self._settings.headless,
            args=launch_args,
        )
        self._context = await self._browser.new_context(
            user_agent=self._settings.user_agent,
            locale=self._settings.locale,
            viewport={"width": 1280, "height": 900},
        )
        self._started = True
        logger.info("Persistent Chromium context started (stealth applied)")

    async def stop(self) -> None:
        """Tear down the browser and playwright session."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.debug("error closing context", exc_info=True)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # pragma: no cover
                logger.debug("error closing browser", exc_info=True)
        if self._stealth_cm is not None and self._pw is not None:
            try:
                await self._stealth_cm.__aexit__(None, None, None)
            except Exception:  # pragma: no cover
                logger.debug("error closing playwright", exc_info=True)
        self._started = False
        logger.info("Persistent Chromium context stopped")

    # -- request helpers ----------------------------------------------------

    async def fetch_html(self, url: str) -> tuple[str | None, int | None]:
        """Navigate to ``url`` in a fresh page and return ``(html, http_status)``.

        The page is always closed before returning; the browser and context are
        preserved for the next request. A hard per-page time budget prevents a
        misbehaving engine page from hanging the queue forever.
        """
        if not self._started or self._context is None:
            raise BrowserDriverError("browser driver is not started")
        context = self._context
        budget = max(30.0, self._settings.navigation_timeout_ms / 1000 + 15)

        async def _fetch() -> tuple[str | None, int | None]:
            async with self._lock:
                page: Page = await context.new_page()
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self._settings.navigation_timeout_ms,
                    )
                    status = response.status if response is not None else None
                    if status is None or status < 400:
                        # Give client-side rendered engines a beat to paint results.
                        await page.wait_for_timeout(1500)
                    html: str = await page.content()
                    return html, status
                except PWTimeoutError:
                    # Capture whatever DOM we have; callers run block detection on it.
                    logger.warning("navigation timeout for %s", url)
                    html = await page.content()
                    return html, None
                finally:
                    await page.close()

        try:
            return await asyncio.wait_for(_fetch(), timeout=budget)
        except asyncio.TimeoutError as exc:
            raise BrowserDriverError(f"page fetch exceeded budget ({budget:.0f}s): {url}") from exc
        except Exception as exc:  # pragma: no cover
            logger.error("fetch failed for %s: %s", url, exc)
            raise BrowserDriverError(f"fetch failed: {exc}") from exc
"""Standalone stealth-browser render worker (TODO2 Phase 3).

Runs in its OWN process, fully decoupled from the API gateway. The only
interface is a small internal HTTP API:

    GET  /ping    -> liveness + browser state
    POST /render  -> {"url": ..., "goto_timeout_ms": ...}
                  -> {"html", "final_url", "status", "elapsed_ms", "error"}

Browser lifecycle (all inside this process, all lazy):
  * Chromium is launched only on the FIRST /render request.
  * Every job opens a FRESH BrowserContext + Page and destroys the context
    immediately after extraction - nothing is reused between sites, so no
    cookies/DOM state leaks and no memory accumulates per site.
  * At most ``BROWSER_MAX_CONTEXTS`` jobs run concurrently (default 1).
  * After ``BROWSER_IDLE_TIMEOUT`` seconds without a job, the browser is shut
    down automatically.

Run it:  python -m browser_pool.worker [--host ..] [--port ..]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PWTimeoutError,
    async_playwright,
)
from playwright_stealth import Stealth
from pydantic import BaseModel, Field

from app.config import Settings, get_settings

logger = logging.getLogger("scrape-worker")


class RenderRequest(BaseModel):
    url: str
    goto_timeout_ms: int = Field(default=60_000, ge=1_000, le=300_000)


class BrowserController:
    """Owns the lazy Chromium lifecycle inside the worker process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stealth_cm: Stealth | None = None
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._sem = asyncio.Semaphore(settings.browser_max_contexts)
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._last_job_at: float = time.monotonic()
        self._jobs: int = 0
        self._active: int = 0
        self._started: bool = False

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._started = True
        self._idle_task = asyncio.create_task(self._idle_watcher(), name="browser-idle-watcher")

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        await self._shutdown_browser()

    # -- state ------------------------------------------------------------

    @property
    def browser_started(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_job_at)

    def state(self) -> dict:
        return {
            "browser_started": self.browser_started,
            "active_contexts": self._active,
            "max_contexts": self._settings.browser_max_contexts,
            "jobs": self._jobs,
            "idle_seconds": round(self.idle_seconds, 1),
        }

    # -- render -----------------------------------------------------------

    async def render(self, url: str, goto_timeout_ms: int) -> dict:
        async with self._sem:
            self._active += 1
            started = time.perf_counter()
            try:
                await self._ensure_browser()
                html, final_url, status = await self._render_page(url, goto_timeout_ms)
                self._jobs += 1
                return {
                    "html": html[: self._settings.scrape_max_body_bytes],
                    "final_url": final_url,
                    "status": status,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": None,
                }
            finally:
                self._active -= 1
                self._last_job_at = time.monotonic()

    async def _ensure_browser(self) -> None:
        """Launch Chromium once (stealth-hooked); relaunch if it died."""
        async with self._lock:
            if self.browser_started:
                return
            if self._browser is not None:
                await self._shutdown_browser()
            stealth = Stealth()
            self._stealth_cm = stealth.use_async(async_playwright())
            self._pw = await self._stealth_cm.__aenter__()
            self._browser = await self._pw.chromium.launch(
                headless=self._settings.headless,
                args=list(self._settings.browser_args) + ["--no-sandbox"],
            )
            logger.info("stealth Chromium launched (max contexts=%d)",
                        self._settings.browser_max_contexts)

    async def _render_page(self, url: str, goto_timeout_ms: int) -> tuple[str, str, int]:
        """Fresh context+page per job; destroy the context afterwards."""
        context: BrowserContext = await self._browser.new_context(
            user_agent=self._settings.user_agent,
            locale=self._settings.locale,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        status: int = 200
        try:
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=goto_timeout_ms,
                )
                if response is not None:
                    status = response.status
                if status < 400:
                    await page.wait_for_timeout(800)
            except PWTimeoutError:
                logger.warning("render navigation timeout for %s", url)
            final_url = page.url
            html = await page.content()
            return html, final_url, status
        finally:
            await context.close()

    # -- idle shutdown ----------------------------------------------------

    async def _idle_watcher(self) -> None:
        poll = max(1.0, min(10.0, self._settings.browser_idle_timeout / 4))
        while True:
            await asyncio.sleep(poll)
            if (
                self.browser_started
                and self._active == 0
                and self.idle_seconds > self._settings.browser_idle_timeout
            ):
                logger.info("browser idle for %.0fs -> shutting down",
                            self.idle_seconds)
                await self._shutdown_browser()

    async def _shutdown_browser(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug("error closing browser", exc_info=True)
            if self._stealth_cm is not None and self._pw is not None:
                try:
                    await self._stealth_cm.__aexit__(None, None, None)
                except Exception:  # pragma: no cover
                    logger.debug("error closing playwright", exc_info=True)
            self._browser = None
            self._pw = None
            self._stealth_cm = None
            logger.info("stealth Chromium shut down")


def create_app(
    settings: Settings | None = None,
    controller_factory=None,
) -> FastAPI:
    """Application factory; tests inject a fake ``controller_factory``."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        controller = (controller_factory(settings) if controller_factory
                      else BrowserController(settings))
        await controller.start()
        app.state.controller = controller
        yield
        await controller.stop()

    app = FastAPI(title="StealthSearch scrape-worker", version="0.1.0", lifespan=lifespan)

    @app.get("/ping")
    async def ping(request: Request) -> dict:
        controller: BrowserController = request.app.state.controller
        return {"ok": True, **controller.state()}

    @app.post("/render")
    async def render(request: Request, payload: RenderRequest) -> JSONResponse:
        controller: BrowserController = request.app.state.controller
        try:
            result = await asyncio.wait_for(
                controller.render(payload.url, payload.goto_timeout_ms),
                timeout=settings.scrape_render_timeout,
            )
            return JSONResponse(result)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=504, content={"error": "render timed out"})
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("render failed for %s", payload.url)
            return JSONResponse(status_code=502, content={"error": str(exc)})

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="StealthSearch scrape worker")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    settings = get_settings()
    host = args.host or settings.scrape_worker_host
    port = args.port or settings.scrape_worker_port
    uvicorn.run(create_app(), host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
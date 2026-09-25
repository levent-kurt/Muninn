"""Standalone stealth-browser render worker.

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
import os
import re
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PWTimeoutError,
)
from playwright_stealth import Stealth
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from ops.netguard import TargetNotAllowedError, validate_target_url

logger = logging.getLogger("scrape-worker")

# Grace period between the worker's own exit and the reaper's SIGKILL sweep:
# long enough for the graceful browser close above to have finished.
REAPER_GRACE_SECONDS = 1.0


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
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        await self._shutdown_browser()

    # -- state ------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        """Whether the controller is running (used by /health reporting)."""
        return self._started

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
        if self._browser is None:  # pragma: no cover - guarded by _ensure_browser
            raise RuntimeError("browser is not started")
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
                # In subprocess mode this may exit the whole process (its own
                # deterministic idle-shutdown, see _shutdown_browser).
                await self._shutdown_browser()

    async def _shutdown_browser(self) -> None:
        # In subprocess mode we snapshot our whole process tree (node driver +
        # every Chromium process, incl. renderers) BEFORE teardown: Chromium
        # detaches its browser into its own process group and graceful close
        # can orphan stray renderers, so neither killpg nor a post-close pid
        # walk can reach them. The snapshot records pid, process group and the
        # browser's --user-data-dir, so the reaper can also kill processes born
        # DURING/AFTER teardown (they inherit the browser's group/profile) that
        # would otherwise outlive the worker.
        tree, profiles = (
            _tree_snapshot(os.getpid())
            if (
                self._settings.scrape_worker_mode == "subprocess"
                and os.getpgid(os.getpid()) == os.getpid()  # we are our group leader
            )
            else ([], set())
        )
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    logger.warning("error closing browser", exc_info=True)
            if self._stealth_cm is not None and self._pw is not None:
                try:
                    await self._stealth_cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning("error closing playwright", exc_info=True)
            self._browser = None
            self._pw = None
            self._stealth_cm = None
            logger.info("stealth Chromium shut down")

        # Deterministic idle exit for subprocess mode. uvicorn's SIGTERM handler
        # is NOT reliably installed when this worker is spawned by the API via
        # asyncio.create_subprocess_exec (observed: the worker dies with the
        # default disposition, returncode -15), so self-signals can kill us
        # mid-teardown and orphan the playwright node driver / Chromium.
        # Instead: best-effort graceful close (above), then:
        #   1. SIGTERM the whole process group (the worker is its group leader
        #      thanks to main(): setpgid(0, 0)) so the node driver closes
        #      cleanly;
        #   2. fork a detached "reaper" that leaves the group (setsid) and ~1s
        #      later SIGKILLs: every pid captured in the snapshot, every process
        #      group captured in the snapshot (catches anything spawned into the
        #      browser's group during teardown), and every process still using
        #      the browser's --user-data-dir (catches anything born late, even
        #      in its own new group);
        #   3. exit cleanly with code 0 (the manager treats that as "lazy").
        # The reaper is a fresh process that never touches the event loop, so
        # forking from the single-threaded loop here is safe.
        if (
            self._settings.scrape_worker_mode == "subprocess"
            and os.getpgid(os.getpid()) == os.getpid()
        ):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # survive the group signal
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpid(), signal.SIGTERM)
            try:
                reaper = os.fork()
            except OSError:  # pragma: no cover - fork unavailable
                reaper = -1
            if reaper == 0:  # child: detached reaper
                with suppress(OSError):  # leave the worker's group so our killpg is safe
                    os.setsid()
                time.sleep(REAPER_GRACE_SECONDS)
                _reap(tree, profiles)
                os._exit(0)
            logger.info("subprocess worker idle -> exiting (code 0)")
            os._exit(0)


def _reap(tree: list[tuple[int, int]], profiles: set[str]) -> None:
    """SIGKILL everything the pre-teardown snapshot captured.

    Runs inside the detached reaper child and never returns to the event loop.
    Three passes, so nothing escapes:

    1. every pid in the snapshot, in case its process group is already gone;
    2. every process group in the snapshot, which catches anything Chromium
       spawned *into the browser's group* while we were tearing down;
    3. every process still using the browser's ``--user-data-dir``, which
       catches anything born late enough to have its own new group.

    Extracted out of the fork so it can be tested directly.
    """
    for pid, _pgid in tree:
        if pid > 0:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    for _pid, pgid in tree:
        if pgid > 0:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
    for profile in profiles:
        try:
            out = subprocess.run(
                ["pgrep", "-f", profile],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError, TimeoutError):  # pragma: no cover
            continue
        for token in out.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid > 0:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def _tree_snapshot(root: int) -> tuple[list[tuple[int, int]], set[str]]:
    """(pid, pgid) pairs + ``--user-data-dir`` values for every descendant.

    Walks by PARENTAGE (recursive ``pgrep -P``) so it works even though
    Chromium detaches its browser into its own process group. Must run before
    teardown: graceful close dismantles the parent/child links.
    """
    pairs: list[tuple[int, int]] = []
    profiles: set[str] = set()
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(root)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):  # pragma: no cover
        return pairs, profiles
    for token in out.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        try:
            pairs.append((pid, os.getpgid(pid)))
        except ProcessLookupError:
            pairs.append((pid, 0))
        try:
            cmd = subprocess.run(
                ["ps", "-ww", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError, TimeoutError):  # pragma: no cover
            cmd = ""
        match = re.search(r"--user-data-dir=(\S+)", cmd)
        if match:
            profiles.add(match.group(1))
        children, child_profiles = _tree_snapshot(pid)
        pairs.extend(children)
        profiles.update(child_profiles)
    return pairs, profiles


def create_app(
    settings: Settings | None = None,
    controller_factory: Callable[[Settings], BrowserController] | None = None,
) -> FastAPI:
    """Application factory; tests inject a fake ``controller_factory``."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        controller = (controller_factory(settings) if controller_factory
                      else BrowserController(settings))
        await controller.start()
        app.state.controller = controller
        yield
        await controller.stop()

    app = FastAPI(title="Muninn scrape worker", version="0.1.0", lifespan=lifespan)

    @app.get("/ping")
    async def ping(request: Request) -> dict:
        controller: BrowserController = request.app.state.controller
        return {"ok": True, **controller.state()}

    @app.post("/render")
    async def render(request: Request, payload: RenderRequest) -> JSONResponse:
        controller: BrowserController = request.app.state.controller
        # Defence in depth: the worker is its own HTTP service, so it re-applies
        # the same outbound guard the API does rather than trusting the caller.
        try:
            url = await validate_target_url(
                payload.url,
                allow_private=settings.scrape_allow_private_targets,
                allowed_hosts=settings.scrape_allowed_hosts,
            )
        except TargetNotAllowedError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        try:
            result = await asyncio.wait_for(
                controller.render(url, payload.goto_timeout_ms),
                timeout=settings.scrape_render_timeout,
            )
            return JSONResponse(result)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=504, content={"error": "render timed out"})
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("render failed for %s", payload.url)
            return JSONResponse(status_code=502, content={"error": str(exc)})

    return app


def _configure_logging(log_level: str, log_file: str) -> None:
    """Persist the worker's own logs to a file.

    ``uvicorn.run`` configures logging for its own loggers; the ``scrape-worker``
    logger otherwise has no visible sink (its output is invisible - or, when the
    manager pipes stderr, unread). Logging to a file makes the worker's idle
    shutdown / teardown sequence diagnosable without touching uvicorn's setup.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.setLevel(level)
    logger.propagate = False  # avoid duplicating through uvicorn's root config
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_file)
        logger.info("worker log -> %s", log_file)
    except OSError:
        handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Muninn scrape worker")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.scrape_worker_mode == "subprocess":
        # Become our own process-group leader so the idle shutdown can signal
        # the whole tree (node driver + Chromium) in one killpg instead of
        # racing per-process teardown. No-op if already a group/session leader.
        with suppress(OSError):
            os.setpgid(0, 0)
    _configure_logging(args.log_level, settings.scrape_worker_log_file)
    host = args.host or settings.scrape_worker_host
    port = args.port or settings.scrape_worker_port
    uvicorn.run(create_app(), host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
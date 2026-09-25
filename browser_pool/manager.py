"""Supervises the standalone scrape-worker process (TODO2 Phase 3).

* **Lazy start** - the worker subprocess is spawned only on the first
  ``render`` call (the worker itself also lazily launches Chromium).
* **Auto-shutdown** - after ``BROWSER_IDLE_TIMEOUT`` with no render activity,
  the manager terminates the (subprocess mode) worker; in external mode the
  supervising service owns that life-cycle and only the worker's in-process
  browser idle-shutdown applies.
* **Respawn on crash** - a worker that dies mid-job is restarted once and the
  job retried; repeated failures raise :class:`BrowserPoolUnavailable`.
* **Concurrency** - the worker enforces ``BROWSER_MAX_CONTEXTS``; the manager
  only serializes its own supervision actions, never the render load.

Two deployment modes (``SCRAPE_WORKER_MODE``):
  * ``subprocess`` (default): the manager spawns/supervises
    ``python -m browser_pool.worker`` on ``SCRAPE_WORKER_PORT``.
  * ``external``: the worker runs as its own supervised service (e.g. a
    ``scrape-worker`` container / systemd unit with ``Restart=always``) at
    ``SCRAPE_WORKER_URL``; the manager just probes and calls it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class BrowserPoolUnavailable(Exception):
    """The stealth browser pool cannot fulfil a render request."""


@dataclass
class RenderOutcome:
    html: str
    final_url: str
    status: int
    elapsed_ms: int
    error: str | None = None


@dataclass
class PoolStatus:
    ok: bool = False
    browser_started: bool = False
    active_contexts: int = 0
    max_contexts: int = 0
    jobs: int = 0
    idle_seconds: float = 0.0
    mode: str = ""
    detail: str = ""


class BrowserPoolManager:
    """Owns the worker lifecycle and the render client on the API side."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client_factory: Callable[[str], httpx.AsyncClient] | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = http_client_factory
        self._poll_interval = poll_interval or max(
            1.0, min(10.0, settings.browser_idle_timeout / 4)
        )
        self._process: asyncio.subprocess.Process | None = None
        self._last_use: float = time.monotonic()
        self._active_jobs = 0
        self._watchdog: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.started_at: float | None = None

    # -- basics --------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._settings.scrape_worker_mode

    @property
    def worker_url(self) -> str:
        if self._settings.scrape_worker_url:
            return self._settings.scrape_worker_url.rstrip("/")
        return (
            f"http://{self._settings.scrape_worker_host}:"
            f"{self._settings.scrape_worker_port}"
        )

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(self.worker_url)
        return httpx.AsyncClient(
            base_url=self.worker_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=self._settings.scrape_render_timeout + 10,
                write=10.0,
                pool=10.0,
            ),
        )

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.started_at = time.monotonic()
        self._watchdog = asyncio.create_task(self._idle_watchdog(), name="pool-watchdog")

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        if self.mode == "subprocess" and self._process is not None:
            await self._terminate_worker()

    # -- worker supervision -----------------------------------------------------

    def _process_alive(self) -> bool:
        if self.mode != "subprocess":
            return True  # external supervisor owns the worker
        return self._process is not None and self._process.returncode is None

    async def _ensure_worker(self) -> None:
        """Spawn (subprocess mode) or probe (external mode) the worker."""
        if self._process_alive():
            return
        if self.mode == "subprocess":
            if self._process is not None and self._process.returncode is not None:
                logger.warning("worker died (exit %s) - respawning", self._process.returncode)
                self._process = None
            await self._spawn_worker()
        await self._wait_until_ready()

    async def _spawn_worker(self) -> None:
        port = self._settings.scrape_worker_port
        cmd = [
            sys.executable,
            "-m",
            "browser_pool.worker",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("scrape worker spawned (pid=%s) on %s", self._process.pid, self.worker_url)

    async def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self._settings.scrape_worker_startup_timeout
        backoff = 0.2
        async with self._client() as client:
            while time.monotonic() < deadline:
                if self.mode == "subprocess" and not self._process_alive():
                    raise BrowserPoolUnavailable("scrape worker exited during startup")
                try:
                    resp = await client.get("/ping")
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 1.5)
        raise BrowserPoolUnavailable(f"scrape worker not ready at {self.worker_url}")

    async def _terminate_worker(self) -> None:
        proc, self._process = self._process, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    # -- render ---------------------------------------------------------------

    async def render(self, url: str, goto_timeout_ms: int = 60_000) -> RenderOutcome:
        async with self._lock:
            self._active_jobs += 1
            try:
                payload = await self._render_once(url, goto_timeout_ms)
            finally:
                self._active_jobs -= 1
                self._last_use = time.monotonic()
        return payload

    async def _render_once(self, url: str, goto_timeout_ms: int) -> RenderOutcome:
        await self._ensure_worker()
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/render",
                    json={"url": url, "goto_timeout_ms": goto_timeout_ms},
                )
                if resp.status_code != 200:
                    if self.mode == "subprocess" and not self._process_alive():
                        return await self._retry_after_respawn(url, goto_timeout_ms)
                    raise BrowserPoolUnavailable(
                        f"worker returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                data = resp.json()
                return RenderOutcome(
                    html=data.get("html") or "",
                    final_url=data.get("final_url") or url,
                    status=data.get("status") or 200,
                    elapsed_ms=data.get("elapsed_ms") or 0,
                    error=data.get("error"),
                )
        except httpx.HTTPError as exc:
            if self.mode == "subprocess" and not self._process_alive():
                return await self._retry_after_respawn(url, goto_timeout_ms)
            raise BrowserPoolUnavailable(f"worker unreachable: {exc}") from exc

    async def _retry_after_respawn(self, url: str, goto_timeout_ms: int) -> RenderOutcome:
        """One supervised retry: restart the worker and replay the job."""
        logger.warning("worker died mid-render; respawning and retrying once")
        await self._terminate_worker()
        await self._spawn_worker()
        await self._wait_until_ready()
        async with self._client() as client:
            resp = await client.post(
                "/render", json={"url": url, "goto_timeout_ms": goto_timeout_ms}
            )
            if resp.status_code != 200:
                raise BrowserPoolUnavailable(
                    f"worker retry returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            return RenderOutcome(
                html=data.get("html") or "",
                final_url=data.get("final_url") or url,
                status=data.get("status") or 200,
                elapsed_ms=data.get("elapsed_ms") or 0,
                error=data.get("error"),
            )

    # -- health / idle ---------------------------------------------------------

    async def status(self) -> PoolStatus:
        if not self._process_alive() and self.mode == "subprocess":
            return PoolStatus(ok=False, mode=self.mode, detail="worker not started")
        try:
            async with self._client() as client:
                resp = await client.get("/ping")
                if resp.status_code != 200:
                    return PoolStatus(ok=False, mode=self.mode, detail=f"HTTP {resp.status_code}")
                data = resp.json()
                return PoolStatus(
                    ok=True,
                    browser_started=bool(data.get("browser_started")),
                    active_contexts=int(data.get("active_contexts") or 0),
                    max_contexts=int(data.get("max_contexts") or 0),
                    jobs=int(data.get("jobs") or 0),
                    idle_seconds=float(data.get("idle_seconds") or 0.0),
                    mode=self.mode,
                )
        except httpx.HTTPError as exc:
            return PoolStatus(ok=False, mode=self.mode, detail=f"unreachable: {exc}")

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            if self.mode != "subprocess" or not self._process_alive():
                continue
            if (
                self._active_jobs == 0
                and time.monotonic() - self._last_use > self._settings.browser_idle_timeout
            ):
                logger.info("pool idle for %.0fs -> stopping worker",
                            self._settings.browser_idle_timeout)
                await self._terminate_worker()
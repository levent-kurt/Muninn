"""Async search pipeline: FIFO queue, randomized throttler, and orchestrator.

Design:

* Every non-cached request becomes a :class:`SearchJob` with an ``asyncio.Future``
  that the API endpoint awaits.
* A single worker drains the FIFO queue, enforcing a randomized 15-30 second
  delay between consecutive outbound requests (``random.uniform``), which keeps
  a home residential IP safely below quota.
* Each job is executed against engines resolved by the :class:`EngineManager`
  (Round-Robin across active engines). 429/CAPTCHA blocks quarantine an engine
  and the job retries on the next active engine; if none remain the future
  resolves with :class:`AllEnginesQuarantinedError` (surfaced as HTTP 503).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass, field
from time import perf_counter

from app.cache import SearchCache
from app.config import Settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.models import SearchResponse
from drivers.browser_driver import BrowserDriver
from drivers.parsers import EngineBlockedError, get_parser
from ops.metrics import Registry

logger = logging.getLogger(__name__)


@dataclass
class SearchJob:
    query: str
    max_results: int
    requested_engine: str | None
    force_refresh: bool
    future: asyncio.Future = field(default_factory=asyncio.Future)
    enqueued_at: float | None = None


@dataclass
class Metrics:
    """Lightweight counters surfaced by the /status endpoint."""

    searches_served: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    circuit_breaker_trips: int = 0
    requests_enqueued: int = 0
    queue_rejections: int = 0
    cache_write_skips: int = 0

    def to_dict(self) -> dict:
        return {
            "searches_served": self.searches_served,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "circuit_breaker_trips": self.circuit_breaker_trips,
            "requests_enqueued": self.requests_enqueued,
            "queue_rejections": self.queue_rejections,
            "cache_write_skips": self.cache_write_skips,
        }


class SearchQueueFullError(Exception):
    """The search queue is at capacity; the caller should retry later.

    Throughput is capped at roughly 2-4 requests/minute by the throttle, so an
    unbounded queue only ever accumulates work that will time out. Refusing is
    honest and keeps one client's burst from consuming the whole backlog.
    """


class SearchService:
    """Owns the request queue, throttler, and engine-orchestrated execution."""

    def __init__(
        self,
        settings: Settings,
        driver: BrowserDriver,
        cache: SearchCache,
        engines: EngineManager,
        registry: Registry | None = None,
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._cache = cache
        self._engines = engines
        self._registry = registry

        # Bounded: see SearchQueueFullError.
        self._queue: asyncio.Queue[SearchJob] = asyncio.Queue(
            maxsize=max(1, settings.max_search_queue)
        )
        self._worker_task: asyncio.Task | None = None
        self._last_fetch_started: float | None = None
        self.metrics = Metrics()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker(), name="search-worker")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # -- public API ---------------------------------------------------------

    async def submit(
        self,
        query: str,
        max_results: int,
        requested_engine: str | None,
        force_refresh: bool,
    ) -> SearchResponse:
        """Serve ``query`` from cache or enqueue it for live execution.

        Returns a fully-formed :class:`SearchResponse`. Raises
        :class:`AllEnginesQuarantinedError` when no engine is available.
        """
        started = time.perf_counter()

        if not force_refresh:
            entry = await self._cache.get(query)
            if entry is not None:
                self.metrics.cache_hits += 1
                self.metrics.searches_served += 1
                return SearchResponse(
                    query=query,
                    engine_used=entry.engine_used,
                    cached=True,
                    execution_time_ms=int((time.perf_counter() - started) * 1000),
                    results_count=len(entry.results),
                    results=entry.results[:max_results],
                )

        self.metrics.cache_misses += 1

        # Fast-fail when the entire pool is quarantined (avoids queue backlog).
        if not self._engines.active_engines():
            raise AllEnginesQuarantinedError()

        job = SearchJob(
            query=query,
            max_results=max_results,
            requested_engine=requested_engine,
            force_refresh=force_refresh,
            enqueued_at=perf_counter(),
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            self.metrics.queue_rejections += 1
            if self._registry is not None:
                self._registry.increment("muninn_search_queue_rejections_total")
            raise SearchQueueFullError(
                f"search queue is full ({self._queue.maxsize} requests); retry shortly"
            ) from exc
        self.metrics.requests_enqueued += 1

        try:
            response = await asyncio.wait_for(
                job.future, timeout=self._settings.request_timeout_seconds
            )
        except AllEnginesQuarantinedError:
            raise
        except asyncio.TimeoutError as exc:
            raise TimeoutError("search timed out in the queue") from exc
        self.metrics.searches_served += 1
        response.execution_time_ms = int((time.perf_counter() - started) * 1000)
        return response

    # -- worker -------------------------------------------------------------

    async def _worker(self) -> None:
        """Single consumer: serialize execution and enforce inter-request delay."""
        while True:
            job = await self._queue.get()
            try:
                await self._throttle()
                response = await self._execute(job)
                job.future.set_result(response)
            except AllEnginesQuarantinedError:
                self.metrics.circuit_breaker_trips += 1
                job.future.set_exception(AllEnginesQuarantinedError())
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("job failed for query=%r", job.query)
                job.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _throttle(self) -> None:
        """Wait a randomized 15-30s since the previous outbound fetch."""
        delay = random.uniform(self._settings.throttle_min_delay, self._settings.throttle_max_delay)
        now = time.monotonic()
        if self._last_fetch_started is not None:
            elapsed = now - self._last_fetch_started
            remaining = delay - elapsed
            if remaining > 0:
                logger.debug("throttling %.1fs until next outbound request", remaining)
                await asyncio.sleep(remaining)
        self._last_fetch_started = time.monotonic()

    async def _execute(self, job: SearchJob) -> SearchResponse:
        """Run the job against active engines until one succeeds."""
        engine_pool_size = len(self._engines.engines)
        for _ in range(engine_pool_size):
            engine = await self._engines.resolve_engine(job.requested_engine)
            parser = get_parser(engine)
            url = parser.search_url(job.query, job.max_results)
            try:
                started = perf_counter()
                html, status = await self._driver.fetch_html(url, engine)
                if self._registry is not None:
                    self._registry.observe(
                        "muninn_search_engine_seconds",
                        perf_counter() - started,
                        {"engine": engine},
                    )
                if html is None:
                    # Navigation failed with nothing to parse - treat it exactly
                    # like a block so the engine is quarantined and we rotate.
                    raise EngineBlockedError(engine, "empty-response")
                block_reason = parser.detect_block(html, status)
                if block_reason is not None:
                    raise EngineBlockedError(engine, block_reason)

                results = parser.parse(html, job.max_results)
                await self._engines.report_success(engine)
                if job.force_refresh:
                    # force_refresh means "do not touch the cache": read through
                    # the engines and do not store the result either. Storing it
                    # would make the flag name a lie and would let a
                    # refresh-seeded entry outlive the caller's intent.
                    self.metrics.cache_write_skips += 1
                    logger.info("query=%r force_refresh: result not cached", job.query)
                else:
                    await self._cache.set(job.query, results, engine)

                logger.info(
                    "query=%r engine=%s results=%d served",
                    job.query, engine, len(results),
                )
                return SearchResponse(
                    query=job.query,
                    engine_used=engine,
                    cached=False,
                    execution_time_ms=0,
                    results_count=len(results),
                    results=results,
                )
            except EngineBlockedError as exc:
                await self._engines.report_failure(engine, exc.reason)
                # Job.requested_engine is quarantined now; the next iteration
                # resolves a fresh engine via Round-Robin.
                continue
            except AllEnginesQuarantinedError:
                raise

        raise AllEnginesQuarantinedError()
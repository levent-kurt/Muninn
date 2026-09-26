"""Muninn FastAPI gateway - /search, /scrape, /health, /status.

The application is long-lived: the stealth Chromium driver, SQLite cache,
engine manager, queue worker, scrape fast-path fetcher, and the supervising
browser-pool manager are all started once in the lifespan and shut down
cleanly on exit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings, get_settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.engine_state_store import EngineStateStore
from app.models import SearchResponse
from app.search_service import SearchService
from browser_pool.manager import BrowserPoolManager
from drivers.browser_driver import BrowserDriver, BrowserDriverError
from fetchers.fast_path import FastPathFetcher
from ops.cache import ScrapeCache
from ops.politeness import HostPoliteness
from ops.ratelimit import RateLimiter
from routers.health import router as health_router
from routers.scrape import router as scrape_router
from schemas.common import ErrorResponse
from services.scrape_service import ScrapeService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("muninn")


def create_app(
    settings: Settings | None = None,
    driver_factory: Callable[[Settings], BrowserDriver | None] = lambda s: BrowserDriver(s),
    scrape_fetcher_factory: Callable[[Settings], FastPathFetcher] = FastPathFetcher,
    scrape_pool_factory: Callable[[Settings], BrowserPoolManager] = BrowserPoolManager,
) -> FastAPI:
    """Application factory.

    Tests inject a ``driver_factory`` (canned search HTML) plus, for the scrape
    module, ``scrape_fetcher_factory`` (mocked httpx transport) and/or
    ``scrape_pool_factory`` (fake browser pool) to stay offline and fast.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        driver = driver_factory(settings)
        if driver is None:
            raise RuntimeError(
                "driver_factory returned no browser driver; the search API cannot start"
            )
        cache = SearchCache(settings.cache_db_path, settings.cache_ttl_seconds)
        fetch: FastPathFetcher = scrape_fetcher_factory(settings)
        pool: BrowserPoolManager = scrape_pool_factory(settings)

        try:
            await driver.start()
            await cache.connect()
            await fetch.start()
        except BrowserDriverError as exc:
            logger.error("browser failed to start: %s", exc)
            raise

        # The engine manager needs the open cache connection to make its
        # circuit-breaker state durable, and is restored before any traffic is
        # served so a restart does not re-hammer an engine that just blocked us.
        engines = EngineManager(settings, store=EngineStateStore(cache.connection))
        await engines.restore()
        service = SearchService(settings, driver, cache, engines)

        # --- scrape module -------------------------------------------------
        politeness = HostPoliteness(
            settings.per_host_delay_seconds,
            settings.politeness_idle_evict_seconds,
        )
        scrape_cache = ScrapeCache(settings.scrape_cache_ttl, settings.scrape_cache_max_entries)
        limiter = RateLimiter(settings.scrape_rate_limit_per_minute)
        scrape_service = ScrapeService(
            settings,
            fetcher=fetch,
            politeness=politeness,
            cache=scrape_cache,
            browser_pool=pool,
        )

        await service.start()
        await pool.start()
        maintenance = await cache.start_maintenance()

        app.state.settings = settings
        app.state.driver = driver
        app.state.cache = cache
        app.state.engines = engines
        app.state.service = service
        app.state.scrape_service = scrape_service
        app.state.scrape_pool = pool
        app.state.scrape_cache = scrape_cache
        app.state.scrape_limiter = limiter
        app.state.started_at = time.time()

        yield

        await service.stop()
        await pool.stop()
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await driver.stop()
        await fetch.close()
        await cache.close()

    app = FastAPI(
        title="Muninn API Gateway",
        summary="Self-hosted web search and page scraping over a stealth browser.",
        description=(
            "Muninn exposes a small REST API in front of a persistent, "
            "anti-bot-hardened Chromium instance.\n\n"
            "* **`/search`** runs a query through a rotating pool of search "
            "engines, behind a throttling queue and a circuit breaker that "
            "quarantines engines which block us.\n"
            "* **`/scrape`** fetches an arbitrary public URL, escalating to an "
            "isolated stealth-browser process when the site challenges the "
            "request, and returns clean text, metadata and links.\n\n"
            "**This service is unauthenticated and single-user.** It binds to "
            "`127.0.0.1` by default; see the Security and Legal sections of the "
            "README before exposing it anywhere else."
        ),
        version="0.1.0",
        lifespan=lifespan,
        contact={"name": "Levent Kurt", "url": "https://github.com/levent-kurt/Muninn"},
        license_info={
            "name": "MIT",
            "url": "https://github.com/levent-kurt/Muninn/blob/main/LICENSE",
        },
        openapi_tags=[
            {
                "name": "search",
                "description": "Execute searches across Google, Bing, DuckDuckGo "
                "and Mojeek through a stealth browser.",
            },
            {
                "name": "scrape",
                "description": "Fetch and extract a page, escalating to a stealth "
                "browser when the site blocks the request.",
            },
            {
                "name": "health",
                "description": "Liveness, readiness and the deep health report.",
            },
            {
                "name": "status",
                "description": "Queue, cache and per-engine metrics.",
            },
        ],
        # Swagger UI is served by default. The service has no authentication, so
        # DOCS_ENABLED=0 is the switch to turn the schema off where the machine
        # is reachable by anyone else.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # ------------------------------------------------------------------ routes

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "Muninn API Gateway",
            "version": "0.1.0",
            "endpoints": {
                "/search": "GET q, max_results, engine, force_refresh",
                "/scrape": "GET url, render, max_text",
                "/health": "GET service health (fetcher + browser pool)",
                "/health/live": "GET cheap liveness probe",
                "/health/ready": "GET readiness probe",
                "/status": "GET engine + queue metrics",
                "/docs": "GET Swagger UI (DOCS_ENABLED, default on)",
                "/redoc": "GET ReDoc reference view",
                "/openapi.json": "GET the OpenAPI schema",
            },
            "engines": list(SUPPORTED_ENGINES),
        }

    @app.get(
        "/search",
        tags=["search"],
        summary="Execute a search",
        description=(
            "Runs `q` against a search engine through the stealth browser.\n\n"
            "Uncached queries join a FIFO queue drained by a single worker that "
            "enforces a randomized delay between outbound requests, which keeps "
            "a residential IP well below quota. Engines rotate round-robin; an "
            "engine that answers 429 or a CAPTCHA is quarantined (30 minutes, "
            "then 12 hours for consecutive failures) and the query is retried on "
            "the next active engine.\n\n"
            "Successful queries are cached in SQLite for `CACHE_TTL_SECONDS` and "
            "replayed from cache on a repeat, which is why `force_refresh` still "
            "returns and re-stores the result but reads through the engine."
        ),
        response_model=SearchResponse,
        responses={
            200: {
                "description": "Search executed (or served from cache).",
                "content": {
                    "application/json": {
                        "example": {
                            "query": "python web scraping",
                            "engine_used": "google",
                            "cached": False,
                            "execution_time_ms": 22140,
                            "results_count": 2,
                            "results": [
                                {
                                    "title": "Web Scraping - Real Python",
                                    "url": "https://realpython.com/scraping/",
                                    "snippet": "Learn how to scrape the web with Python.",
                                },
                                {
                                    "title": "Scrapy | A Fast and Powerful",
                                    "url": "https://scrapy.org/",
                                    "snippet": "An open source and collaborative framework.",
                                },
                            ],
                        }
                    }
                },
            },
            422: {"model": ErrorResponse, "description": "Invalid query parameters."},
            503: {
                "model": ErrorResponse,
                "description": "Every search engine is currently quarantined.",
                "content": {
                    "application/json": {
                        "example": {
                            "error": "all_engines_quarantined",
                            "detail": "every search engine is under quarantine; try again later",
                        }
                    }
                },
            },
            504: {"model": ErrorResponse, "description": "The search timed out in the queue."},
        },
    )
    async def search(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500, description="Search query"),
        max_results: int = Query(10, ge=1, le=50, description="Max organic results to yield"),
        engine: str | None = Query(
            None,
            description="Preferred engine. Ignored (and another engine used) if the "
            "requested one is quarantined.",
            examples=["google"],
        ),
        force_refresh: bool = Query(False, description="Bypass the cache for this read"),
    ) -> SearchResponse | JSONResponse:
        if engine is not None and engine not in SUPPORTED_ENGINES:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported engine {engine!r}; choose from {list(SUPPORTED_ENGINES)}",
            )
        if engine is not None:
            engine = engine.lower()

        service: SearchService = request.app.state.service
        try:
            response = await service.submit(
                query=q,
                max_results=max_results,
                requested_engine=engine,
                force_refresh=force_refresh,
            )
        except AllEnginesQuarantinedError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "all_engines_quarantined",
                    "detail": "every search engine is under quarantine; try again later",
                },
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="search timed out in the queue"
            ) from exc
        return response

    @app.get(
        "/status",
        tags=["status"],
        summary="Engine, queue and cache metrics",
        description=(
            "Operational counters: how deep the search queue is, how many queries "
            "are cached, the per-engine circuit-breaker state (failure counts, "
            "quarantine level and remaining cooldown) and lifetime request "
            "counters."
        ),
        responses={
            200: {
                "description": "Current metrics snapshot.",
                "content": {
                    "application/json": {
                        "example": {
                            "queue_depth": 0,
                            "cached_queries_count": 42,
                            "metrics": {
                                "searches_served": 128,
                                "cache_hits": 96,
                                "cache_misses": 32,
                                "circuit_breaker_trips": 2,
                                "requests_enqueued": 32,
                            },
                            "engines": {
                                "google": {
                                    "status": "active",
                                    "fail_count": 0,
                                    "success_count": 12,
                                    "total_requests": 12,
                                    "quarantine_level": 0,
                                    "quarantined_until": None,
                                    "remaining_cooldown_seconds": 0,
                                }
                            },
                        }
                    }
                },
            }
        },
    )
    async def status(request: Request) -> dict:
        engine_mgr: EngineManager = request.app.state.engines
        service: SearchService = request.app.state.service
        cache = request.app.state.cache
        engines = await engine_mgr.status()
        return {
            "queue_depth": service.queue_depth,
            "cached_queries_count": await cache.count(),
            "metrics": service.metrics.to_dict(),
            "engines": engines,
        }

    app.include_router(scrape_router)
    app.include_router(health_router)

    return app


def run() -> None:
    """Serve the gateway on ``Settings.host``/``Settings.port``.

    ``uvicorn app.main:app`` ignores our settings and uses its own defaults, so
    ``HOST``/``PORT`` would silently do nothing. This entry point makes the
    configured values authoritative:

        python -m app.main              # honours HOST and PORT
    """
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


app = create_app()


if __name__ == "__main__":  # pragma: no cover - process entry point
    run()
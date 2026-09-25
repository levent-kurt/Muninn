"""Muninn FastAPI gateway - /search, /scrape, /health, /status.

The application is long-lived: the stealth Chromium driver, SQLite cache,
engine manager, queue worker, scrape fast-path fetcher, and the supervising
browser-pool manager are all started once in the lifespan and shut down
cleanly on exit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings, get_settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.search_service import SearchService
from browser_pool.manager import BrowserPoolManager
from drivers.browser_driver import BrowserDriver, BrowserDriverError
from fetchers.fast_path import FastPathFetcher
from ops.cache import ScrapeCache
from ops.politeness import HostPoliteness
from routers.health import router as health_router
from routers.scrape import router as scrape_router
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
        cache = SearchCache(settings.cache_db_path, settings.cache_ttl_seconds)
        engines = EngineManager(settings)
        if driver is None:
            raise RuntimeError(
                "driver_factory returned no browser driver; the search API cannot start"
            )
        service = SearchService(settings, driver, cache, engines)

        # --- scrape module -------------------------------------------------
        fetch: FastPathFetcher = scrape_fetcher_factory(settings)
        politeness = HostPoliteness(settings.per_host_delay_seconds)
        scrape_cache = ScrapeCache(settings.scrape_cache_ttl, settings.scrape_cache_max_entries)
        pool: BrowserPoolManager = scrape_pool_factory(settings)
        scrape_service = ScrapeService(
            settings,
            fetcher=fetch,
            politeness=politeness,
            cache=scrape_cache,
            browser_pool=pool,
        )

        try:
            await driver.start()
            await cache.connect()
        except BrowserDriverError as exc:
            logger.error("browser failed to start: %s", exc)
            raise
        await service.start()
        await pool.start()

        app.state.settings = settings
        app.state.driver = driver
        app.state.cache = cache
        app.state.engines = engines
        app.state.service = service
        app.state.scrape_service = scrape_service
        app.state.scrape_pool = pool
        app.state.scrape_cache = scrape_cache
        app.state.started_at = time.time()

        yield

        await service.stop()
        await pool.stop()
        await driver.stop()
        await cache.close()

    app = FastAPI(
        title="Muninn API Gateway",
        description="Unified REST API for stealth web search (/search) and page scraping (/scrape).",
        version="0.1.0",
        lifespan=lifespan,
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
                "/status": "GET engine + queue metrics",
            },
            "engines": list(SUPPORTED_ENGINES),
        }

    @app.get("/search")
    async def search(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500, description="Search query"),
        max_results: int = Query(10, ge=1, le=50, description="Max organic results to yield"),
        engine: str | None = Query(None, description="Preferred engine (google|bing|ddg|mojeek)"),
        force_refresh: bool = Query(False, description="Bypass the cache"),
    ) -> JSONResponse:
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
        return JSONResponse(content=response.to_dict())

    @app.get("/status")
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


app = create_app()
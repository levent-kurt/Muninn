"""StealthSearch FastAPI gateway - /search, /health, /status.

The application is long-lived: the stealth Chromium driver, SQLite cache,
engine manager, and queue worker are all started once in the lifespan and shut
down cleanly on exit.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.cache import SearchCache
from app.config import SUPPORTED_ENGINES, Settings, get_settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager
from app.search_service import SearchService
from drivers.browser_driver import BrowserDriver, BrowserDriverError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stealthsearch")


def create_app(
    settings: Settings | None = None,
    driver_factory: Callable[[Settings], BrowserDriver | None] = lambda s: BrowserDriver(s),
) -> FastAPI:
    """Application factory; tests inject a ``driver_factory`` returning a stub."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        driver = driver_factory(settings)
        cache = SearchCache(settings.cache_db_path, settings.cache_ttl_seconds)
        engines = EngineManager(settings)
        service = SearchService(settings, driver, cache, engines)

        try:
            if driver is not None:
                await driver.start()
            await cache.connect()
        except BrowserDriverError as exc:
            logger.error("browser failed to start: %s", exc)
            raise
        await service.start()

        app.state.settings = settings
        app.state.driver = driver
        app.state.cache = cache
        app.state.engines = engines
        app.state.service = service
        app.state.started_at = time.time()

        yield

        await service.stop()
        if driver is not None:
            await driver.stop()
        await cache.close()

    app = FastAPI(
        title="StealthSearch API Gateway",
        description="Unified REST API for web searches via stealth-driven Google/Bing/DDG/Mojeek scraping.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ routes

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "StealthSearch API Gateway",
            "version": "0.1.0",
            "endpoints": {"/search": "GET q, max_results, engine, force_refresh",
                          "/health": "GET service health",
                          "/status": "GET engine + queue metrics"},
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
        settings: Settings = request.app.state.settings
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
        except TimeoutError:
            raise HTTPException(status_code=504, detail="search timed out in the queue")
        return JSONResponse(content=response.to_dict())

    @app.get("/health")
    async def health(request: Request) -> dict:
        settings = request.app.state.settings
        engine_mgr: EngineManager = request.app.state.engines
        service: SearchService = request.app.state.service
        driver = request.app.state.driver
        cache = request.app.state.cache

        active = engine_mgr.active_engines()
        status = "ok" if active else "degraded"
        return {
            "status": status,
            "browser_ready": driver is not None and driver._started,
            "queue_depth": service.queue_depth,
            "cache_entries": await cache.count(),
            "active_engines": active,
            "quarantined_engines": [e for e in engine_mgr.engines if e not in active],
            "uptime_seconds": int(time.time() - request.app.state.started_at),
        }

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

    return app


app = create_app()
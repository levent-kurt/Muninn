"""``GET /health`` endpoint.

Monitors and reports status for BOTH sides of the gateway:

  * the search side: stealth search driver, engine rotation, queue, cache; and
  * the scrape side: the HTTP fast-path fetcher and the isolated Stealth
    Browser process pool (worker state, context concurrency, idle timers).

A lazy-starting subprocess worker that has simply not been used yet is a
healthy state; only an unreachable *external* worker degrades the endpoint.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    engine_mgr = request.app.state.engines
    service = request.app.state.service
    driver = request.app.state.driver
    cache = request.app.state.cache
    pool = request.app.state.scrape_pool

    active = engine_mgr.active_engines()
    pool_status = await pool.status()

    if not active or pool_status.mode == "external" and not pool_status.ok:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "browser_ready": driver is not None and driver._started,
        "queue_depth": service.queue_depth,
        "cache_entries": await cache.count(),
        "active_engines": active,
        "quarantined_engines": [e for e in engine_mgr.engines if e not in active],
        "uptime_seconds": int(time.time() - request.app.state.started_at),
        "fetcher": {
            "status": "ok",
            "engine": "httpx-fast-path",
            "max_body_bytes": settings.scrape_max_body_bytes,
            "per_host_delay": settings.per_host_delay_seconds,
        },
        "browser_pool": {
            "status": "ok" if pool_status.ok else "down",
            "mode": pool_status.mode,
            "detail": pool_status.detail,
            "browser_started": pool_status.browser_started,
            "active_contexts": pool_status.active_contexts,
            "max_contexts": pool_status.max_contexts,
            "jobs": pool_status.jobs,
            "idle_seconds": pool_status.idle_seconds,
        },
    }
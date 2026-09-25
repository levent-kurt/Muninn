"""``GET /health`` and ``GET /health/ready`` endpoints.

Monitors and reports status for BOTH sides of the gateway:

  * the search side: stealth search driver, engine rotation, queue, cache; and
  * the scrape side: the HTTP fast-path fetcher and the isolated Stealth
    Browser process pool (worker state, context concurrency, idle timers).

Two endpoints, deliberately:

``/health/live``
    Cheap process liveness. No database query, no call into the worker. This is
    what a container healthcheck should poll, because Docker probes it every few
    seconds and the deep checks below are not free.

``/health/ready``
    The full picture, including a live worker probe and a cache count. Suitable
    for a load balancer or for a human, not for a 5-second heartbeat.

A lazy-starting subprocess worker that has simply not been used yet is a healthy
state; only an unreachable *external* worker degrades readiness.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from browser_pool.manager import PoolStatus

router = APIRouter(tags=["health"])


def _verdict(active: list[str], pool_status: PoolStatus) -> str:
    if not active:
        return "degraded"
    if pool_status.mode == "external" and not pool_status.ok:
        return "degraded"
    return "ok"


@router.get("/health")
async def health(request: Request) -> dict:
    """Deep check: engine pool, worker probe, cache size, queue depth."""
    settings = request.app.state.settings
    engine_mgr = request.app.state.engines
    service = request.app.state.service
    driver = request.app.state.driver
    cache = request.app.state.cache
    pool = request.app.state.scrape_pool

    active = engine_mgr.active_engines()
    pool_status = await pool.status()
    scrape_cache = request.app.state.scrape_cache
    limiter = request.app.state.scrape_limiter

    return {
        "status": _verdict(active, pool_status),
        "browser_ready": driver is not None and driver.is_started,
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
        "scrape_cache": {
            "entries": scrape_cache.size(),
            "evictions": scrape_cache.evictions,
        },
        "rate_limiter": {
            "tracked_clients": limiter.tracked_clients,
            "per_minute": settings.scrape_rate_limit_per_minute,
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


@router.get("/health/live")
async def health_live(request: Request) -> dict:
    """Cheap liveness for container healthchecks: no I/O beyond the process."""
    service = request.app.state.service
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - request.app.state.started_at),
        "queue_depth": service.queue_depth,
    }


@router.get("/health/ready")
async def health_ready(request: Request) -> dict:
    """Readiness: everything /health checks, without the deep probes.

    Answers "should traffic be routed here" using only in-process state, so it
    stays fast enough for a load balancer while still failing when no search
    engine is usable.
    """
    engine_mgr = request.app.state.engines
    driver = request.app.state.driver
    active = engine_mgr.active_engines()
    ready = bool(active) and driver is not None and driver.is_started
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "active_engines": active,
    }

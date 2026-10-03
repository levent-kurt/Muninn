"""Refresh the application's gauges from live state.

Kept separate from :mod:`ops.metrics` (which knows nothing about the
application) and called at scrape time by ``/metrics``.

Gauges are computed when they are read rather than pushed from a background
task, so they cannot go stale: whatever Prometheus last scraped is, by
definition, current. ``/health`` deliberately does not call this - it already
probes the browser pool for its own report, and refreshing here too would
double that round trip.
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.search_breaker import GAUGE_VALUES
from app.search_service import WORKER_GAUGE_VALUES
from ops.metrics import Registry

logger = logging.getLogger(__name__)


async def refresh_gauges(request: Request) -> None:
    """Set every gauge from current state. Never raises."""
    app = request.app
    registry: Registry | None = getattr(app.state, "metrics", None)
    if registry is None:  # pragma: no cover - only in a misconfigured app
        return

    try:
        # In-process values: free.
        service = app.state.service
        registry.set_gauge("muninn_search_queue_depth", service.queue_depth)
        # Worker liveness and the breaker. The wedge this describes looked
        # healthy from every other angle - /health answered in 80ms while the
        # queue never drained - so these are the numbers that would have shown
        # it: a worker stuck on one job, and a breaker that had given up.
        registry.set_gauge(
            "muninn_search_worker_state", WORKER_GAUGE_VALUES[service.worker_state()]
        )
        registry.set_gauge("muninn_search_workers", service.worker_status()["pool_size"])
        breaker = service.breaker_status()
        registry.set_gauge(
            "muninn_search_breaker_state", GAUGE_VALUES[str(breaker["state"])]
        )
        registry.set_gauge("muninn_search_queue_stuck", 1 if service.queue_stuck else 0)
        registry.set_gauge(
            "muninn_scrape_cache_entries", app.state.scrape_cache.size()
        )
        registry.set_gauge("muninn_cache_entries", await app.state.cache.count())

        # The browser pool is a separate process, so this is the one gauge that
        # costs a round trip. It degrades to "down" rather than failing the
        # scrape: a metrics endpoint that 500s when a dependency is sick is
        # exactly when you most need it.
        status = await app.state.scrape_pool.status()
        registry.set_gauge("muninn_browser_pool_up", 1 if status.ok else 0)
        registry.set_gauge("muninn_browser_pool_jobs", status.jobs)
    except Exception:  # pragma: no cover - metrics must never break a scrape
        logger.warning("could not refresh metrics gauges", exc_info=True)

"""``GET /metrics`` — Prometheus text exposition.

Dependency-free by design (see :mod:`ops.metrics`): Muninn is a single-user
service, so a scrapeable text format matters more than a client library.

The endpoint is unauthenticated like the rest of the API, which means it
publishes traffic volumes and latency shape. It exposes no request bodies, no
queries and no scraped content — only counters, gauges and latency
distributions. If that is still more than you want published, set
``DOCS_ENABLED=false`` and leave this to a private network, or comment the route
out.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ops.gauges import refresh_gauges
from ops.metrics import Registry

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    description=(
        "Counters, gauges and latency histograms in the Prometheus text format. "
        "Covers HTTP requests by endpoint and status, the search path (queue wait, "
        "engine execution, cache hits) and the scrape path (fast path vs. browser "
        "render, per outcome), plus gauges for cache occupancy, queue depth and "
        "browser-pool state. Gauges are computed at scrape time, so they are "
        "always current."
    ),
    response_class=PlainTextResponse,
)
async def metrics(request: Request) -> PlainTextResponse:
    registry: Registry = request.app.state.metrics
    await refresh_gauges(request)
    return PlainTextResponse(
        registry.render_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

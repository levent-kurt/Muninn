"""``GET /scrape`` endpoint.

Parameters:
  * ``url``      (required) - target URL as a valid ``http(s)`` address.
  * ``render``   (optional, 0|1) - ``1`` forces the stealth browser pool even
    when the fast-path fetch looks clean.
  * ``max_text`` (optional, 1..32000) - cap on extracted body-text characters.

Upstream failures map to explicit HTTP errors:
  * target rejected by the SSRF guard -> 400
  * target disallowed by robots.txt   -> 403
  * client rate limit exhausted       -> 429
  * fast-path network failure         -> 502
  * stealth browser pool down         -> 503
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import HttpUrl

from browser_pool.manager import BrowserPoolUnavailable
from fetchers.fast_path import FastPathError
from ops.netguard import TargetNotAllowedError
from ops.ratelimit import RateLimitExceeded
from ops.robots import RobotsDeniedError

router = APIRouter(tags=["scrape"])


@router.get("/scrape")
async def scrape(
    request: Request,
    url: HttpUrl = Query(..., description="Target URL to scrape (http/https)"),
    render: int = Query(
        0, ge=0, le=1,
        description="1 = force stealth-browser rendering, 0 = fast-path only (unless blocked)",
    ),
    max_text: int = Query(
        32_000, ge=1, le=200_000,
        description="Maximum number of body-text characters to return (server cap: MAX_TEXT_CAP)",
    ),
) -> JSONResponse:
    limiter = getattr(request.app.state, "scrape_limiter", None)
    if limiter is not None:
        # Abuse protection, not authentication. Muninn is single-user; if you
        # need real identity, put a gateway in front of this service.
        client = request.client.host if request.client else "unknown"
        try:
            await limiter.acquire(client)
        except RateLimitExceeded as exc:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
                content={"error": "rate_limited", "detail": str(exc), "retry_after": exc.retry_after},
            )

    service = request.app.state.scrape_service
    try:
        response = await service.scrape(str(url), render=render, max_text=max_text)
    except TargetNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RobotsDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FastPathError as exc:
        raise HTTPException(status_code=502, detail=f"fast-path fetch failed: {exc}") from exc
    except BrowserPoolUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(content=response.model_dump())

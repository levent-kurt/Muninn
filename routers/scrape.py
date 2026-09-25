"""``GET /scrape`` endpoint (TODO2 Phase 5).

Parameters:
  * ``url``      (required) - target URL as a valid ``http(s)`` address.
  * ``render``   (optional, 0|1) - ``1`` forces the stealth browser pool even
    when the fast-path fetch looks clean.
  * ``max_text`` (optional, 1..32000) - cap on extracted body-text characters.

Upstream failures map to explicit HTTP errors:
  * fast-path network failure  -> 502
  * stealth browser pool down  -> 503
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import HttpUrl

from browser_pool.manager import BrowserPoolUnavailable
from fetchers.fast_path import FastPathError

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
        32_000, ge=1, le=32_000,
        description="Maximum number of body-text characters to return",
    ),
) -> JSONResponse:
    service = request.app.state.scrape_service
    try:
        response = await service.scrape(str(url), render=render, max_text=max_text)
    except FastPathError as exc:
        raise HTTPException(status_code=502, detail=f"fast-path fetch failed: {exc}") from exc
    except BrowserPoolUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(content=response.model_dump())
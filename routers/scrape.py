"""``GET /scrape`` endpoint.

Fetches a page over plain HTTP and returns clean text, metadata and links,
escalating to the isolated stealth-browser pool when the site blocks the
request, when the page looks like a challenge page, or when ``render=1`` is
asked for explicitly.

Parameters:
  * ``url``      (required) - target URL as a valid ``http(s)`` address.
  * ``render``   (optional, 0|1) - ``1`` forces the stealth browser pool even
    when the fast-path fetch looks clean.
  * ``max_text`` (optional, 1..200000) - cap on extracted body-text characters.

Upstream failures map to explicit HTTP errors:
  * target rejected by the SSRF guard -> 400
  * target disallowed by robots.txt   -> 403
  * client rate limit exhausted       -> 429
  * fast-path network failure         -> 502
  * stealth browser pool down         -> 503
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import HttpUrl

from browser_pool.manager import BrowserPoolUnavailable
from fetchers.fast_path import FastPathError
from ops.netguard import TargetNotAllowedError
from ops.ratelimit import RateLimitExceeded
from ops.robots import RobotsDeniedError
from schemas.common import ErrorResponse
from schemas.scrape import ScrapeResponse

router = APIRouter(tags=["scrape"])

UrlQuery = Annotated[
    HttpUrl,
    Query(
        description="Target URL to scrape. Must be `http`/`https` and resolve to a "
        "public address; loopback, private and link-local targets are refused.",
        examples=["https://example.com"],
    ),
]
RenderQuery = Annotated[
    int,
    Query(
        ge=0,
        le=1,
        description="`1` forces rendering in the stealth browser pool. `0` uses the "
        "fast HTTP path unless the site blocks or challenges us.",
        examples=[0],
    ),
]
MaxTextQuery = Annotated[
    int,
    Query(
        ge=1,
        le=200_000,
        description="Maximum number of body-text characters to return. The server "
        "applies `MAX_TEXT_CAP` as a hard ceiling.",
        examples=[32_000],
    ),
]


@router.get(
    "/scrape",
    summary="Fetch and extract a web page",
    description=(
        "Fetches `url` over plain HTTP and extracts its main text, title, meta "
        "description and resolved links.\n\n"
        "If the site answers with a block or a challenge page, or the extracted "
        "text is suspiciously thin, the request is escalated to an isolated "
        "stealth-browser process and the extraction is repeated on the rendered "
        "DOM; the response then reports `rendered: true`.\n\n"
        "Targets are validated before anything is fetched: non-HTTP schemes and "
        "any host resolving to a loopback, private, link-local or otherwise "
        "non-public address are refused, and `robots.txt` is honoured by default."
    ),
    response_model=ScrapeResponse,
    responses={
        200: {
            "description": "Page fetched (possibly after a browser render).",
            "content": {
                "application/json": {
                    "example": {
                        "url": "https://example.com/",
                        "final_url": "https://example.com/",
                        "status": 200,
                        "title": "Example Domain",
                        "meta_description": "Illustrative examples for a domain.",
                        "text": "Example Domain This domain is for use in documentation.",
                        "links": [
                            {
                                "url": "https://example.com/more",
                                "anchor_text": "More information...",
                                "same_domain": True,
                            }
                        ],
                        "rendered": False,
                        "block_suspected": False,
                        "cached": False,
                        "age_seconds": 0,
                        "content_type": "text/html",
                    }
                }
            },
        },
        400: {"model": ErrorResponse, "description": "Target refused by the SSRF guard."},
        403: {"model": ErrorResponse, "description": "The target's robots.txt disallows this path."},
        422: {"model": ErrorResponse, "description": "Invalid query parameters."},
        429: {
            "model": ErrorResponse,
            "description": "Rate limit exceeded (abuse protection, not authentication). "
            "Retry after the `Retry-After` seconds.",
        },
        502: {"model": ErrorResponse, "description": "The upstream fetch failed (DNS, TLS, timeout)."},
        503: {"model": ErrorResponse, "description": "The stealth browser pool is unavailable."},
    },
)
async def scrape(
    request: Request,
    url: UrlQuery,
    render: RenderQuery = 0,
    max_text: MaxTextQuery = 32_000,
) -> ScrapeResponse:
    """Serve one scrape request end to end."""
    limiter = getattr(request.app.state, "scrape_limiter", None)
    if limiter is not None:
        # Abuse protection, not authentication. Muninn is single-user; if you
        # need real identity, put a gateway in front of this service.
        client = request.client.host if request.client else "unknown"
        try:
            await limiter.acquire(client)
        except RateLimitExceeded as exc:
            return JSONResponse(  # type: ignore[return-value]
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
                content={
                    "error": "rate_limited",
                    "detail": str(exc),
                    "retry_after": exc.retry_after,
                },
            )

    service = request.app.state.scrape_service
    try:
        return await service.scrape(str(url), render=render, max_text=max_text)
    except TargetNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RobotsDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FastPathError as exc:
        raise HTTPException(status_code=502, detail=f"fast-path fetch failed: {exc}") from exc
    except BrowserPoolUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

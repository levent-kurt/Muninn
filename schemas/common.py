"""Shared response models for the HTTP layer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """The body every 4xx/5xx from this API returns.

    FastAPI's ``HTTPException`` serialises to ``{"detail": ...}``; declaring
    this model in each endpoint's ``responses`` makes that contract visible in
    the OpenAPI schema instead of leaving it implicit.
    """

    detail: str = Field(
        ...,
        description="Human-readable explanation of what went wrong.",
        examples=["fast-path fetch failed for https://example.com: ConnectError"],
    )

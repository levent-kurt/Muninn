"""Request/response schemas for the ``/scrape`` endpoint (TODO2 Phase 1)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LinkItem(BaseModel):
    """A single hyperlink found on a scraped page."""

    url: str
    anchor_text: str
    same_domain: bool


class ScrapeResponse(BaseModel):
    """The JSON payload returned by ``GET /scrape``."""

    url: str
    final_url: str
    status: int
    title: str | None = None
    meta_description: str | None = None
    text: str = ""
    links: list[LinkItem] = Field(default_factory=list)
    rendered: bool = False
    block_suspected: bool = False
    cached: bool = False
    age_seconds: int = 0
    content_type: str = ""
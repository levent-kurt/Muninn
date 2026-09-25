"""Pydantic schemas for the HTTP layer."""

from schemas.common import ErrorResponse
from schemas.scrape import LinkItem, ScrapeResponse

__all__ = ["ErrorResponse", "LinkItem", "ScrapeResponse"]

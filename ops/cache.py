"""TTL cache for completed :class:`~schemas.scrape.ScrapeResponse` objects.

Results are keyed by the *requested* URL (fragment-stripped) plus the render
flag, so an explicit ``render=1`` call is never served from a fast-path cache
entry and vice-versa. On a hit the stored response is deep-copied so callers
can never mutate the cached entry, with ``cached=True`` and an accurate
``age_seconds`` populated. Expired entries are dropped lazily on access;
``size()`` reports surviving entries.
"""

from __future__ import annotations

import asyncio
import time
from typing import cast
from urllib.parse import urldefrag

from schemas.scrape import ScrapeResponse


class ScrapeCache:
    """In-memory, async-safe TTL cache keyed by URL + render flag."""

    def __init__(self, ttl_seconds: int = 3_600) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, ScrapeResponse]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(url: str, render: int = 0) -> str:
        return f"render={1 if render else 0}:{urldefrag(url.strip()).url}"

    async def get(self, url: str, render: int = 0) -> ScrapeResponse | None:
        key = self._key(url, render)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, response = entry
            age = time.monotonic() - stored_at
            if age > self._ttl:
                del self._entries[key]
                return None
            fresh = cast(ScrapeResponse, response.model_copy(deep=True))
            fresh.cached = True
            fresh.age_seconds = int(age)
            return fresh

    async def set(self, response: ScrapeResponse, render: int = 0) -> None:
        key = self._key(response.url, render)
        async with self._lock:
            self._entries[key] = (time.monotonic(), response)

    def size(self) -> int:
        return len(self._entries)
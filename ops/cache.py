"""Bounded TTL cache for completed :class:`~schemas.scrape.ScrapeResponse`.

Results are keyed by the *requested* URL (fragment-stripped) plus the render
flag, so an explicit ``render=1`` call is never served from a fast-path cache
entry and vice-versa. On a hit the stored response is deep-copied so callers
can never mutate the cached entry, with ``cached=True`` and an accurate
``age_seconds`` populated. Expired entries are dropped lazily on access.

The cache is bounded: at most ``max_entries`` live responses are kept, and the
least-recently-stored one is evicted when that is exceeded. Without a bound a
caller passing unique URLs (or any long-running scraper) would grow this dict
forever.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from urllib.parse import urldefrag

from schemas.scrape import ScrapeResponse

# Sensible ceiling: a scrape cache holding far more than this is a sign the
# operator wants a durable store, not an in-process one.
DEFAULT_MAX_ENTRIES = 2_000


class ScrapeCache:
    """In-memory, async-safe, size-bounded TTL cache keyed by URL + render flag."""

    def __init__(
        self,
        ttl_seconds: int = 3_600,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        # Ordered oldest -> newest; `move_to_end` on hit keeps it LRU-by-use.
        self._entries: OrderedDict[str, tuple[float, ScrapeResponse]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._evictions = 0

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
            self._entries.move_to_end(key)
            fresh = response.model_copy(deep=True)
            fresh.cached = True
            fresh.age_seconds = int(age)
            return fresh

    async def set(self, response: ScrapeResponse, render: int = 0) -> None:
        key = self._key(response.url, render)
        async with self._lock:
            self._entries[key] = (time.monotonic(), response)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def size(self) -> int:
        return len(self._entries)

    @property
    def evictions(self) -> int:
        """How many entries have been dropped to stay within the bound."""
        return self._evictions

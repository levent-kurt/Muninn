"""Coarse per-client rate limiting for the public scrape endpoint.

This is **abuse protection, not security**: a bounded token bucket per client
key (the peer IP by default) that keeps a single caller from monopolising the
stealth browser pool. Muninn is a single-user tool; if you need real
authentication, quotas or per-tenant accounting, put it in front of the service
or build it yourself.

State is bounded on purpose - a limiter that stores one entry per IP forever is
itself a denial-of-service vector, so idle clients are swept and the table is
capped.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict


class RateLimitExceeded(Exception):
    """The client used up its budget; carries a ``Retry-After`` hint."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(1, int(round(retry_after)))
        super().__init__(f"rate limit exceeded; retry in {self.retry_after}s")


class RateLimiter:
    """Token bucket per client key, with idle sweeping and a hard entry cap."""

    def __init__(
        self,
        per_minute: int,
        burst: int | None = None,
        max_clients: int = 10_000,
        idle_seconds: float = 300.0,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self._per_second = per_minute / 60.0
        self._burst = float(burst if burst is not None else per_minute)
        self._max_clients = max(1, max_clients)
        self._idle_seconds = idle_seconds
        # key -> (tokens, last_refill_monotonic)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._gate = asyncio.Lock()

    @property
    def tracked_clients(self) -> int:
        return len(self._buckets)

    async def acquire(self, key: str) -> None:
        """Consume one token for ``key``; raise when the bucket is empty."""
        now = time.monotonic()
        async with self._gate:
            self._sweep(now)
            entry = self._buckets.get(key)
            if entry is None:
                tokens, last = self._burst, now
            else:
                tokens, last = entry
                # Refill for the elapsed time, then LRU-touch.
                tokens = min(self._burst, tokens + (now - last) * self._per_second)
                self._buckets.move_to_end(key)

            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._buckets.move_to_end(key)
                self._trim()
                return

            self._buckets[key] = (tokens, now)
            self._trim()
            deficit = 1.0 - tokens
            retry_after = deficit / self._per_second if self._per_second else 60.0
            raise RateLimitExceeded(retry_after)

    def _sweep(self, now: float) -> None:
        """Drop clients idle beyond the threshold."""
        stale = [k for k, (_, last) in self._buckets.items() if now - last > self._idle_seconds]
        for key in stale:
            del self._buckets[key]

    def _trim(self) -> None:
        """Evict least-recently-used clients so the table stays bounded.

        Called *after* inserting, otherwise a table sitting exactly at the cap
        would briefly hold one entry too many.
        """
        while len(self._buckets) > self._max_clients:
            self._buckets.popitem(last=False)

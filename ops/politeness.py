"""Per-host politeness manager (TODO2 Phase 4).

An in-memory, host-keyed set of asyncio locks guarantees at most one in-flight
outbound request per hostname, and a mandatory minimum delay is enforced
between consecutive requests targeting the same hostname. Scrape requests to
*different* hosts run concurrently, untouched by this manager.

The orchestrator wraps its whole pipeline (fast-path fetch **and** any browser
render escalation, which hit the same host) in a single ``slot()`` so the 2 s
gap applies between distinct client scrape requests per host, not between the
internal legs of one request.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit


class HostPoliteness:
    """Host-level locking + minimum-gap scheduling for outbound traffic."""

    def __init__(self, delay_seconds: float = 2.0) -> None:
        self._delay = delay_seconds
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request_at: dict[str, float] = {}

    @staticmethod
    def host_of(url: str) -> str:
        """Normalise a URL to its hostname (lowercased, no port)."""
        return (urlsplit(url).hostname or "").lower()

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        """Serialise + space out requests to ``url``'s host.

        Waits for the remaining idle gap since the previous request to this
        host before yielding; the caller must hold the slot for the entire
        outbound interaction with that host.
        """
        host = self.host_of(url)
        async with self._locks[host]:
            now = time.monotonic()
            last = self._last_request_at.get(host, 0.0)
            wait = self._delay - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at[host] = time.monotonic()
            yield
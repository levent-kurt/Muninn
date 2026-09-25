"""Per-host politeness manager.

An in-memory, host-keyed set of asyncio locks guarantees at most one in-flight
outbound request per hostname, and a mandatory minimum delay is enforced
between consecutive requests targeting the same hostname. Scrape requests to
*different* hosts run concurrently, untouched by this manager.

The orchestrator wraps its whole pipeline (fast-path fetch **and** any browser
render escalation, which hit the same host) in a single ``slot()`` so the 2 s
gap applies between distinct client scrape requests per host, not between the
internal legs of one request.

Host state is bounded: every ``slot()`` entry sweeps hosts that have been idle
longer than ``idle_evict_seconds``. That is enough - state can only grow while
requests are arriving, and arrival is exactly when we sweep. (A background
reaper task would only add a lifecycle to manage for no extra safety.)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

# A host not seen for this long is forgotten (its lock + timestamp).
DEFAULT_IDLE_EVICT_SECONDS = 300.0


class HostPoliteness:
    """Host-level locking + minimum-gap scheduling for outbound traffic."""

    def __init__(
        self,
        delay_seconds: float = 2.0,
        idle_evict_seconds: float = DEFAULT_IDLE_EVICT_SECONDS,
    ) -> None:
        self._delay = delay_seconds
        self._idle_evict_seconds = idle_evict_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}
        # Guards the two host dicts. Separate from the per-host locks, which
        # serialise outbound traffic rather than bookkeeping.
        self._gate = asyncio.Lock()
        self._evicted_hosts = 0

    @staticmethod
    def host_of(url: str) -> str:
        """Normalise a URL to its hostname (lowercased, no port)."""
        return (urlsplit(url).hostname or "").lower()

    @property
    def tracked_hosts(self) -> int:
        """Number of hosts currently holding state (for metrics/tests)."""
        return len(self._locks)

    @property
    def evicted_hosts(self) -> int:
        """How many idle host entries have been swept over the process life."""
        return self._evicted_hosts

    def _evict_idle(self, now: float) -> None:
        """Drop hosts idle beyond the threshold. Caller must hold ``_gate``."""
        stale = [
            host
            for host, last in self._last_request_at.items()
            if now - last > self._idle_evict_seconds and not self._locks[host].locked()
        ]
        for host in stale:
            del self._locks[host]
            del self._last_request_at[host]
        self._evicted_hosts += len(stale)

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        """Serialise + space out requests to ``url``'s host.

        Waits for the remaining idle gap since the previous request to this
        host before yielding; the caller must hold the slot for the entire
        outbound interaction with that host.
        """
        host = self.host_of(url)

        async with self._gate:
            self._evict_idle(time.monotonic())
            lock = self._locks.get(host)
            if lock is None:
                lock = self._locks[host] = asyncio.Lock()
            last = self._last_request_at.get(host, 0.0)

        async with lock:
            wait = self._delay - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            async with self._gate:
                self._last_request_at[host] = time.monotonic()
            yield

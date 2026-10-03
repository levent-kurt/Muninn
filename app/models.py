"""Core data models shared across the gateway, cache, and API layers."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class SearchResult:
    """A single organic search result."""

    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class SearchResponse:
    """The JSON payload returned by ``GET /search``."""

    query: str
    engine_used: str
    cached: bool
    execution_time_ms: int
    results_count: int
    results: list[SearchResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "engine_used": self.engine_used,
            "cached": self.cached,
            "execution_time_ms": self.execution_time_ms,
            "results_count": self.results_count,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class EngineState:
    """Mutable runtime state for one search engine in the pool."""

    name: str
    fail_count: int = 0
    success_count: int = 0
    total_requests: int = 0
    # Epoch timestamp (seconds) until which the engine is quarantined, or None.
    quarantined_until: float | None = None
    # 1 = first failure, 2 = consecutive failures. The *duration* grows
    # exponentially up to Settings.quarantine_escalated_seconds; the level only
    # says whether this was the first failure or a repeat.
    quarantine_level: int = 0
    # Class of the most recent failure ("block", "timeout", "network", "parse").
    # Runtime-only: it explains the cooldown but is not worth persisting.
    last_failure_class: str | None = None

    @property
    def active(self) -> bool:
        return self.quarantined_until is None or self.quarantined_until <= time.time()

    def remaining_cooldown(self) -> int:
        """Seconds left on the current quarantine (0 when active)."""
        if self.quarantined_until is None:
            return 0
        return max(0, int(self.quarantined_until - time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "active" if self.active else "quarantined",
            "fail_count": self.fail_count,
            "success_count": self.success_count,
            "total_requests": self.total_requests,
            "quarantine_level": self.quarantine_level,
            "last_failure_class": self.last_failure_class,
            "quarantined_until": self.quarantined_until,
            "remaining_cooldown_seconds": self.remaining_cooldown(),
        }
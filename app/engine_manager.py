"""Engine pool manager and circuit breaker.

Owns the per-engine state, enforces the Round-Robin selection across *active*
(non-quarantined) engines, and implements the quarantine escalation policy:

* Fail #1 (429 / CAPTCHA)  -> 30 minutes (configurable).
* Fail #2+ (consecutive)   -> 12 hours (configurable).
* Any success              -> resets the consecutive failure counter.

Quarantined engines are dropped from rotation; if every engine is quarantined
the manager raises :class:`AllEnginesQuarantinedError` and the API returns 503.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import SUPPORTED_ENGINES, Settings
from app.models import EngineState

logger = logging.getLogger(__name__)


class AllEnginesQuarantinedError(Exception):
    """Every engine in the pool is currently under quarantine."""


class EngineManager:
    """Round-Robin engine rotator with quarantine/circuit-breaker logic."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._states: dict[str, EngineState] = {
            name: EngineState(name=name) for name in SUPPORTED_ENGINES
        }
        self._rr_index = 0
        self._lock = asyncio.Lock()

    # -- introspection ------------------------------------------------------

    @property
    def engines(self) -> tuple[str, ...]:
        return SUPPORTED_ENGINES

    def active_engines(self) -> list[str]:
        """Names of engines currently eligible for rotation."""
        return [name for name, st in self._states.items() if st.active]

    async def status(self) -> dict[str, dict]:
        """Full state snapshot for ``GET /status`` and ``GET /health``."""
        async with self._lock:
            return {name: st.to_dict() for name, st in self._states.items()}

    def queue_depth_hint(self) -> int:  # placeholder for symmetry with status()
        return 0

    # -- selection ----------------------------------------------------------

    async def resolve_engine(self, requested: str | None = None) -> str:
        """Pick the engine for the next request.

        * If ``requested`` names an active engine, use it directly.
        * Otherwise advance the Round-Robin pointer across active engines.
        * Raise :class:`AllEnginesQuarantinedError` when nothing is active.
        """
        async with self._lock:
            active = [name for name, st in self._states.items() if st.active]
            if not active:
                raise AllEnginesQuarantinedError()

            if requested is not None and requested in active:
                return requested

            engine = active[self._rr_index % len(active)]
            self._rr_index += 1
            return engine

    # -- outcomes -----------------------------------------------------------

    async def report_success(self, engine: str) -> None:
        """Record a successful search; resets the consecutive failure counter."""
        async with self._lock:
            st = self._states[engine]
            st.fail_count = 0
            st.quarantine_level = 0
            st.quarantined_until = None
            st.success_count += 1
            st.total_requests += 1
            logger.debug("engine=%s ok (successes=%d)", engine, st.success_count)

    async def report_failure(self, engine: str, reason: str) -> None:
        """Record a 429/CAPTCHA failure and quarantine the engine."""
        async with self._lock:
            st = self._states[engine]
            st.total_requests += 1
            st.fail_count += 1

            first = self._settings.quarantine_first_seconds
            esca = self._settings.quarantine_escalated_seconds
            if st.fail_count >= 2:
                st.quarantine_level = 2
                duration = esca
                logger.warning(
                    "engine=%s failed %d consecutive times -> escalated quarantine "
                    "(%ds / %dh)",
                    engine, st.fail_count, esca, esca // 3600,
                )
            else:
                st.quarantine_level = 1
                duration = first
                logger.warning(
                    "engine=%s failed (%s) -> first-level quarantine (%ds / %dm)",
                    engine, reason, first, first // 60,
                )
            st.quarantined_until = time.time() + duration
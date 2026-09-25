"""Engine pool manager and circuit breaker.

Owns the per-engine state, enforces the Round-Robin selection across *active*
(non-quarantined) engines, and implements the quarantine escalation policy:

* Fail #1 (429 / CAPTCHA)  -> 30 minutes (configurable).
* Fail #2+ (consecutive)   -> 12 hours (configurable).
* Any success              -> resets the consecutive failure counter.

Quarantined engines are dropped from rotation; if every engine is quarantined
the manager raises :class:`AllEnginesQuarantinedError` and the API returns 503.

State is durable. The manager takes an optional
:class:`~app.engine_state_store.EngineStateStore`; when present, counters and
quarantine deadlines are restored on :meth:`restore` and written on every
report. That matters because a restart is exactly when you do *not* want to
forget that an engine just blocked you.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import SUPPORTED_ENGINES, Settings
from app.engine_state_store import EngineStateStore
from app.models import EngineState

logger = logging.getLogger(__name__)


class AllEnginesQuarantinedError(Exception):
    """Every engine in the pool is currently under quarantine."""


class EngineManager:
    """Round-Robin engine rotator with quarantine/circuit-breaker logic."""

    def __init__(
        self,
        settings: Settings,
        store: EngineStateStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._states: dict[str, EngineState] = {
            name: EngineState(name=name) for name in SUPPORTED_ENGINES
        }
        self._rr_index = 0
        self._lock = asyncio.Lock()

    # -- persistence ---------------------------------------------------------

    async def restore(self) -> int:
        """Reload persisted counters/quarantines. Returns rows restored.

        Engines with no stored row keep their fresh defaults. A stored
        quarantine whose deadline has already passed is applied as a *completed*
        level so the next failure escalates immediately, matching the in-memory
        behaviour after a cooldown expires.
        """
        if self._store is None:
            return 0
        try:
            rows = await self._store.load()
        except Exception:  # pragma: no cover - never block startup on telemetry
            logger.warning("could not restore engine state", exc_info=True)
            return 0
        restored = 0
        for name, fails, successes, total, level, until in rows:
            state = self._states.get(name)
            if state is None:
                continue
            expired = until is not None and until <= time.time()
            state.fail_count = fails
            state.success_count = successes
            state.total_requests = total
            state.quarantine_level = level if (until is not None and not expired) else 0
            state.quarantined_until = None if (until is None or expired) else until
            restored += 1
        if restored:
            quarantined = [n for n, s in self._states.items() if not s.active]
            logger.info(
                "restored engine state for %d engine(s); still quarantined: %s",
                restored, quarantined or "none",
            )
        return restored

    async def _persist(self, state: EngineState) -> None:
        if self._store is None:
            return
        try:
            await self._store.save(
                state.name,
                state.fail_count,
                state.success_count,
                state.total_requests,
                state.quarantine_level,
                state.quarantined_until,
            )
        except Exception:  # pragma: no cover - telemetry must not fail a search
            logger.warning("could not persist engine state for %s", state.name, exc_info=True)

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
        await self._persist(self._states[engine])

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
        await self._persist(st)

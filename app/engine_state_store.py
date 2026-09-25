"""Durable store for search-engine circuit-breaker state.

The quarantine state is the whole point of the engine manager: it exists so a
restart does not immediately re-hammer an engine that just blocked us. Keeping
it in process memory defeats that, since a restart is exactly when you are most
likely to be deploying or recovering from a block. This table lives in the same
SQLite file as the response cache.
"""

from __future__ import annotations

import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_state (
    name              TEXT PRIMARY KEY,
    fail_count        INTEGER NOT NULL DEFAULT 0,
    success_count     INTEGER NOT NULL DEFAULT 0,
    total_requests    INTEGER NOT NULL DEFAULT 0,
    quarantine_level  INTEGER NOT NULL DEFAULT 0,
    quarantined_until REAL
)
"""


class EngineStateStore:
    """Async-safe persistence for :class:`~app.models.EngineState` rows."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def load(self) -> list[tuple[str, int, int, int, int, float | None]]:
        """Return every persisted row as
        ``(name, fail_count, success_count, total_requests, level, until)``."""
        cur = await self._db.execute(
            "SELECT name, fail_count, success_count, total_requests, "
            "quarantine_level, quarantined_until FROM engine_state"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            (
                r["name"],
                int(r["fail_count"]),
                int(r["success_count"]),
                int(r["total_requests"]),
                int(r["quarantine_level"]),
                None if r["quarantined_until"] is None else float(r["quarantined_until"]),
            )
            for r in rows
        ]

    async def save(
        self,
        name: str,
        fail_count: int,
        success_count: int,
        total_requests: int,
        quarantine_level: int,
        quarantined_until: float | None,
    ) -> None:
        """Upsert one engine's counters and quarantine deadline."""
        await self._db.execute(
            """
            INSERT INTO engine_state
                (name, fail_count, success_count, total_requests,
                 quarantine_level, quarantined_until)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                fail_count = excluded.fail_count,
                success_count = excluded.success_count,
                total_requests = excluded.total_requests,
                quarantine_level = excluded.quarantine_level,
                quarantined_until = excluded.quarantined_until
            """,
            (name, fail_count, success_count, total_requests, quarantine_level, quarantined_until),
        )
        await self._db.commit()
        logger.debug(
            "persisted engine state %s: fails=%d level=%d until=%s",
            name, fail_count, quarantine_level,
            "none" if quarantined_until is None else f"{quarantined_until - time.time():.0f}s",
        )

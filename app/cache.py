"""SQLite-backed response cache with TTL.

Keys are the SHA-256 of the *normalized* query (``q.strip().lower()``), so
identical queries (ignoring case/whitespace) reuse one cache row. Results are
stored as JSON and expire after ``cache_ttl_seconds``.

The same connection also carries the ``engine_state`` table, so search-engine
circuit-breaker state survives a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.engine_state_store import SCHEMA as ENGINE_STATE_SCHEMA
from app.models import SearchResult

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    results: list[SearchResult]
    engine_used: str
    stored_at: float
    expires_at: float


class SearchCache:
    """Thread-safe (single asyncio loop) TTL cache backed by SQLite."""

    def __init__(self, db_path: str, ttl_seconds: int) -> None:
        self._db_path = db_path
        self._ttl = ttl_seconds
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        if self._db_path != ":memory:":
            Path(self._db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                key             TEXT PRIMARY KEY,
                normalized_query TEXT NOT NULL,
                results         TEXT NOT NULL,
                engine_used     TEXT NOT NULL,
                stored_at       REAL NOT NULL,
                expires_at      REAL NOT NULL
            )
            """
        )
        # Circuit-breaker state shares this connection so a restart does not
        # forget that an engine is currently blocking us.
        await self._db.execute(ENGINE_STATE_SCHEMA)
        await self._require_db().commit()
        logger.info("cache connected at %s (ttl=%ss)", self._db_path, self._ttl)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        """The live connection, or a loud error if the cache is not connected.

        Every read/write goes through here so a lifecycle mistake surfaces as a
        clear error instead of an ``AttributeError`` on ``None``.
        """
        if self._db is None:
            raise RuntimeError("SearchCache is not connected; call connect() first")
        return self._db

    @property
    def connection(self) -> aiosqlite.Connection:
        """The live connection, for components that share this database."""
        return self._require_db()

    # -- key helpers --------------------------------------------------------

    @staticmethod
    def normalize_query(query: str) -> str:
        return query.strip().lower()

    @classmethod
    def key_for(cls, query: str) -> str:
        return hashlib.sha256(cls.normalize_query(query).encode("utf-8")).hexdigest()

    # -- read / write -------------------------------------------------------

    async def get(self, query: str) -> CacheEntry | None:
        """Return the cached entry for ``query`` if present and unexpired."""
        key = self.key_for(query)
        async with self._lock:
            cur = await self._require_db().execute(
                "SELECT results, engine_used, stored_at, expires_at "
                "FROM search_cache WHERE key = ?",
                (key,),
            )
            row = await cur.fetchone()
            await cur.close()

        if row is None:
            return None
        if row["expires_at"] <= time.time():
            await self.delete(key)
            return None

        payload = json.loads(row["results"])
        results = [
            SearchResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("snippet", ""))
            for r in payload
        ]
        return CacheEntry(
            results=results,
            engine_used=row["engine_used"],
            stored_at=row["stored_at"],
            expires_at=row["expires_at"],
        )

    async def set(self, query: str, results: list[SearchResult], engine_used: str) -> None:
        now = time.time()
        payload = json.dumps([r.to_dict() for r in results], ensure_ascii=False)
        async with self._lock:
            await self._require_db().execute(
                """
                INSERT OR REPLACE INTO search_cache
                    (key, normalized_query, results, engine_used, stored_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.key_for(query),
                    self.normalize_query(query),
                    payload,
                    engine_used,
                    now,
                    now + self._ttl,
                ),
            )
            await self._require_db().commit()

    async def delete(self, key: str) -> None:
        async with self._lock:
            await self._require_db().execute("DELETE FROM search_cache WHERE key = ?", (key,))
            await self._require_db().commit()

    async def count(self) -> int:
        """Number of live (unexpired) cache rows."""
        now = time.time()
        async with self._lock:
            cur = await self._require_db().execute(
                "SELECT COUNT(*) AS n FROM search_cache WHERE expires_at > ?", (now,)
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row["n"]) if row is not None else 0
"""Unit tests for the SQLite cache: normalization, TTL, and counts."""

from __future__ import annotations

import pytest

from app.cache import SearchCache
from app.models import SearchResult


@pytest.fixture
async def cache() -> SearchCache:
    c = SearchCache(db_path=":memory:", ttl_seconds=3_600)
    await c.connect()
    yield c
    await c.close()


async def test_connect_and_count_empty(cache: SearchCache) -> None:
    assert await cache.count() == 0


async def test_set_and_get_roundtrip(cache: SearchCache) -> None:
    results = [SearchResult(title="T", url="https://u", snippet="S")]
    await cache.set("Python Scraping", results, "bing")
    entry = await cache.get("python scraping")  # case-insensitive + trimmed
    assert entry is not None
    assert entry.engine_used == "bing"
    assert entry.results[0].url == "https://u"
    assert await cache.count() == 1


async def test_query_normalization_ignores_case_and_whitespace(cache: SearchCache) -> None:
    await cache.set("  My QUERY ", [SearchResult("t", "https://u", "s")], "ddg")
    assert await cache.get("my query") is not None
    assert SearchCache.key_for("MY Query") == SearchCache.key_for("my query")


async def test_expired_entry_is_miss(cache: SearchCache) -> None:
    await cache.set("old", [SearchResult("t", "https://u", "s")], "google")
    assert await cache.get("old") is not None
    # Force expiry by rewriting expires_at into the past.
    async with cache._lock:
        await cache._db.execute(
            "UPDATE search_cache SET expires_at = expires_at - 999999"
        )
        await cache._db.commit()
    assert await cache.get("old") is None
    assert await cache.count() == 0  # expired rows are not counted


async def test_replace_updates_entry(cache: SearchCache) -> None:
    await cache.set("q", [SearchResult("t1", "https://a", "s1")], "bing")
    await cache.set("q", [SearchResult("t2", "https://b", "s2")], "mojeek")
    entry = await cache.get("q")
    assert entry is not None
    assert entry.engine_used == "mojeek"
    assert entry.results[0].title == "t2"
    assert await cache.count() == 1
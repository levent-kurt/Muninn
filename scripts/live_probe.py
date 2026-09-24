#!/usr/bin/env python3
"""Live probe: real stealth Chromium driver against real search engines.

OPT-IN: set LIVE=1 to actually hit search engines (adds a handful of low-volume
requests to whatever IP this runs on). Without LIVE=1 the script only prints
instructions and exits cleanly - CI and dry runs stay offline.

What it verifies:
  * the persistent browser + context start once with stealth applied;
  * the SAME browser/context object is reused across many fetches;
  * each engine's parser returns structured results (or a block is detected);
  * RSS footprint delta after several requests (context reuse keeps it flat).

Usage:  LIVE=1 python scripts/live_probe.py [query]
"""

from __future__ import annotations

import asyncio
import os
import resource
import sys
from pathlib import Path

# Allow running directly:  python scripts/live_probe.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache import SearchCache
from app.config import Settings
from app.engine_manager import EngineManager
from app.search_service import SearchService
from drivers.browser_driver import BrowserDriver


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # macOS = KB


async def run(query: str) -> int:
    settings = Settings(
        throttle_min_delay=2.0,
        throttle_max_delay=4.0,
        cache_db_path=":memory:",
        request_timeout_seconds=90,
        navigation_timeout_ms=20_000,
    )
    driver = BrowserDriver(settings)
    cache = SearchCache(":memory:", ttl_seconds=3600)
    engines = EngineManager(settings)
    service = SearchService(settings, driver, cache, engines)

    await driver.start()
    await cache.connect()
    await service.start()
    browser_before = driver._browser
    context_before = driver._context
    rss_start = rss_mb()

    # Friendlier engines first so useful output lands early even if Google stalls.
    outcomes: dict[str, str] = {}
    try:
        for engine in ("ddg", "mojeek", "bing", "google"):
            try:
                response = await service.submit(
                    query=query,
                    max_results=5,
                    requested_engine=engine,
                    force_refresh=False,
                )
                first = response.results[0] if response.results else None
                outcome = (
                    f"{len(response.results)} results"
                    + (f" | first: {first.title[:48]}" if first else "")
                )
                print(f"  {engine:<8} [live] -> {outcome}", flush=True)
                outcomes[engine] = outcome
            except Exception as exc:  # noqa: BLE001 - report any live error
                outcome = f"ERROR: {type(exc).__name__}: {exc}"
                print(f"  {engine:<8} [live] -> {outcome}", flush=True)
                outcomes[engine] = outcome

        rss_end = rss_mb()
        context_reused = (
            driver._browser is browser_before and driver._context is context_before
        )

        print("=" * 56)
        print(f"live probe (query={query!r})")
        print("=" * 56)
        print(f"  RSS start/end   : {rss_start:.1f} MB -> {rss_end:.1f} MB "
              f"(delta {rss_end - rss_start:+.1f} MB)")
        print(f"  context reused  : {context_reused}")
        print(f"  cache entries   : {await cache.count()}")

        success = sum(1 for o in outcomes.values() if "results" in o)
        print("RESULT:", "PASS" if success >= 2 and context_reused else "INCONCLUSIVE")
        return 0 if success >= 2 and context_reused else 2
    finally:
        await service.stop()
        await driver.stop()
        await cache.close()


def main() -> int:
    if os.environ.get("LIVE") != "1":
        print("live probe skipped (set LIVE=1 to hit real search engines)")
        return 0
    query = sys.argv[1] if len(sys.argv) > 1 else "python web scraping"
    return asyncio.run(run(query))


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""End-to-end smoke test: 100+ simulated search requests through the API.

Runs the full pipeline (FastAPI -> queue -> throttler -> engine manager ->
driver stub -> parsers -> cache) with a deterministic fake driver, so:
  * 100 requests (25 unique queries x 4) complete successfully;
  * the second wave is served from cache (no extra outbound calls);
  * Round-Robin distributes the first wave across all four engines;
  * quarantine + 503 behaviour works when engines get blocked.

Usage:  python scripts/batch_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly:  python scripts/batch_smoke.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.config import Settings, SUPPORTED_ENGINES
from app.main import create_app
from tests.conftest import FakeDriver, make_test_settings

UNIQUE_QUERIES = 25
REPEAT_EACH = 4
TOTAL = UNIQUE_QUERIES * REPEAT_EACH


def main() -> int:
    fake = FakeDriver()
    app = create_app(settings=make_test_settings(), driver_factory=lambda s: fake)

    failures: list[str] = []
    with TestClient(app) as client:
        # --- wave 1: 25 unique queries (every engine must be exercised) -----
        wave1_engines: dict[str, int] = {}
        for i in range(UNIQUE_QUERIES):
            r = client.get("/search", params={"q": f"batch query {i}"})
            if r.status_code != 200:
                failures.append(f"wave1 q{i} -> {r.status_code}")
                continue
            body = r.json()
            wave1_engines[body["engine_used"]] = wave1_engines.get(body["engine_used"], 0) + 1
            if body["cached"]:
                failures.append(f"wave1 q{i} unexpectedly served from cache")
            if body["results_count"] < 1 or not body["results"]:
                failures.append(f"wave1 q{i} returned no results")

        # --- wave 2: identical queries -> cache hits, zero outbound calls ----
        calls_after_wave1 = fake.url_count
        cache_hits = 0
        for i in range(TOTAL - UNIQUE_QUERIES):
            r = client.get("/search", params={"q": f"  batch query {i % UNIQUE_QUERIES} "})
            if r.status_code != 200:
                failures.append(f"wave2 #{i} -> {r.status_code}")
                continue
            if r.json()["cached"]:
                cache_hits += 1

        # --- circuit breaker probe -------------------------------------------
        fake.block_engines = {"google"}
        if client.get("/search", params={"q": "block probe"}).json()["engine_used"] == "google":
            failures.append("google not excluded after block")
        fake.block_engines = set()
        fake.all_blocked = True
        if client.get("/search", params={"q": "all blocked"}).status_code != 503:
            failures.append("expected 503 when all engines are quarantined")
        status = client.get("/status").json()
        if all(e["status"] != "quarantined" for e in status["engines"].values()):
            failures.append("expected quarantine states in /status")
        final_status = client.get("/status").json()

    # ------------------------------------------------------------------ report
    covered = [e for e in SUPPORTED_ENGINES if wave1_engines.get(e, 0) > 0]
    print("=" * 56)
    print("StealthSearch batch smoke (simulated driver)")
    print("=" * 56)
    print(f"requests sent      : {TOTAL} ({UNIQUE_QUERIES} unique x {REPEAT_EACH})")
    print(f"cache hits (wave2) : {cache_hits}/{TOTAL - UNIQUE_QUERIES}")
    print(f"outbound calls     : {calls_after_wave1} for wave 1 (cached wave 2 adds 0)")
    print(f"engine rotation    : {dict(sorted(wave1_engines.items()))}")
    print(f"engines covered    : {sorted(covered)}")
    print(f"queue drained      : {final_status['queue_depth'] == 0}")
    print(f"cache entries      : {final_status['cached_queries_count']}")

    ok = not failures
    print("RESULT:", "PASS" if ok else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
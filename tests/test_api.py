"""API-level tests for /search, /health, /status using a fake driver.

The fake driver returns canned per-engine HTML instantly, so the throttler is
configured to a ~0s delay via ``make_test_settings`` - unit tests therefore
exercise the full pipeline (queue, round-robin, circuit breaker, cache)
without touching a real browser or the network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import FakeDriver, make_test_settings
from tests.html_fixtures import ENGINE_RESULTS_HTML


# --------------------------------------------------------------------------- /search


def test_search_returns_clean_results(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "python"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "python"
    assert body["cached"] is False
    assert body["engine_used"] in {"google", "bing", "ddg", "mojeek"}
    assert body["results_count"] >= 1
    assert body["results"][0]["title"]
    assert body["results"][0]["url"].startswith("http")
    assert "snippet" in body["results"][0]
    assert body["execution_time_ms"] >= 0


def test_search_missing_q_is_422(client: TestClient) -> None:
    assert client.get("/search").status_code == 422
    assert client.get("/search", params={"q": ""}).status_code == 422


def test_search_unknown_engine_is_422(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "x", "engine": "yahoo"})
    assert resp.status_code == 422


def test_search_honours_requested_active_engine(client: TestClient, fake_driver: FakeDriver) -> None:
    resp = client.get("/search", params={"q": "python", "engine": "bing"})
    assert resp.status_code == 200
    assert resp.json()["engine_used"] == "bing"


def test_cache_hit_serves_same_query_without_engine_call(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    first = client.get("/search", params={"q": "python web scraping"})
    assert first.json()["cached"] is False
    calls_after_first = fake_driver.url_count

    second = client.get("/search", params={"q": "  PYTHON Web Scraping  "})
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert second.json()["engine_used"] == first.json()["engine_used"]
    # identical normalized query => no additional outbound fetch
    assert fake_driver.url_count == calls_after_first


def test_force_refresh_bypasses_cache(client: TestClient, fake_driver: FakeDriver) -> None:
    first = client.get("/search", params={"q": "same q"})
    assert first.json()["cached"] is False
    calls_after_first = fake_driver.url_count

    second = client.get("/search", params={"q": "same q", "force_refresh": "true"})
    assert second.json()["cached"] is False
    assert fake_driver.url_count == calls_after_first + 1


def test_round_robin_across_engines(client: TestClient) -> None:
    engines = []
    for i in range(4):
        resp = client.get("/search", params={"q": f"query number {i}"})
        engines.append(resp.json()["engine_used"])
    assert engines == ["google", "bing", "ddg", "mojeek"]


# --------------------------------------------------------------------------- circuit breaker


def test_blocked_engine_is_quarantined_and_rotation_continues(
    client: TestClient, fake_driver: FakeDriver
) -> None:
    # google blocks -> rotation must fall through to bing for the next query
    fake_driver.block_engines = {"google"}
    blocked = client.get("/search", params={"q": "first"})
    assert blocked.json()["engine_used"] != "google"

    status = client.get("/status").json()["engines"]
    assert status["google"]["status"] == "quarantined"
    assert status["google"]["fail_count"] == 1


def test_all_engines_quarantined_returns_503(client: TestClient, fake_driver: FakeDriver) -> None:
    fake_driver.all_blocked = True
    resp = client.get("/search", params={"q": "doomed"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "all_engines_quarantined"


def test_quarantine_escalation_across_two_failures(client: TestClient, fake_driver: FakeDriver) -> None:
    fake_driver.block_engines = {"google", "bing", "ddg", "mojeek"}
    # first burst: every engine fails once
    for i in range(4):
        resp = client.get("/search", params={"q": f"burst {i}"})
        assert resp.status_code == 503

    status = client.get("/status").json()["engines"]
    assert all(v["fail_count"] == 1 for v in status.values())
    assert all(v["quarantine_level"] == 1 for v in status.values())


# --------------------------------------------------------------------------- health / status / root


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["browser_ready"] is True
    assert body["queue_depth"] == 0
    assert body["cache_entries"] == 0
    assert set(body["active_engines"]) == {"google", "bing", "ddg", "mojeek"}
    assert body["uptime_seconds"] >= 0


def test_status_endpoint_shape(client: TestClient) -> None:
    client.get("/search", params={"q": "state probe"})
    body = client.get("/status").json()
    assert set(body) == {"queue_depth", "cached_queries_count", "metrics", "engines"}
    assert set(body["engines"]) == {"google", "bing", "ddg", "mojeek"}
    assert body["metrics"]["searches_served"] >= 1
    assert body["cached_queries_count"] == 1


def test_root_metadata(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["service"] == "StealthSearch API Gateway"
    assert "/search" in body["endpoints"]
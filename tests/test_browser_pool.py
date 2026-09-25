"""Tests for the isolated stealth browser pool.

The worker's FastAPI app is tested in-process with a fake browser controller;
the manager's supervision logic (lazy spawn, respawn, idle shutdown, render
IPC) is tested with a fake subprocess and a mocked HTTP transport - no real
Chromium is launched in CI tests.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from browser_pool.manager import BrowserPoolManager, BrowserPoolUnavailable, RenderOutcome
from browser_pool.worker import create_app as create_worker_app


def _settings(**overrides) -> Settings:
    base = {
        "scrape_worker_mode": "external",
        "scrape_worker_host": "127.0.0.1",
        "scrape_worker_port": 8765,
        "browser_idle_timeout": 300,
        "browser_max_contexts": 1,
        "scrape_render_timeout": 45.0,
        "scrape_worker_startup_timeout": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://pool")


def _client_factory(handler) -> callable:
    def _factory(base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)
    return _factory


# --------------------------------------------------------------------------- worker app


class FakeController:
    """Stand-in for BrowserController with scriptable state/behaviour."""

    def __init__(self, state=None, result=None, fail=False) -> None:
        self.state_data = state or {
            "browser_started": False,
            "active_contexts": 0,
            "max_contexts": 1,
            "jobs": 3,
            "idle_seconds": 1.5,
        }
        self.result = result or {
            "html": "<html><body>rendered</body></html>",
            "final_url": "https://example.com/",
            "status": 200,
            "elapsed_ms": 42,
            "error": None,
        }
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def render(self, url: str, goto_timeout_ms: int) -> dict:
        self.calls.append((url, goto_timeout_ms))
        if self.fail:
            raise RuntimeError("chromium exploded")
        return dict(self.result)

    def state(self) -> dict:
        return self.state_data


@pytest.fixture
def fake_controller() -> FakeController:
    return FakeController()


@pytest.fixture
def worker_client(fake_controller: FakeController):
    app = create_worker_app(settings=_settings(), controller_factory=lambda s: fake_controller)
    with TestClient(app) as c:
        yield c


def test_worker_ping_reports_browser_state(worker_client: TestClient) -> None:
    body = worker_client.get("/ping").json()
    assert body["ok"] is True
    assert body["browser_started"] is False
    assert body["max_contexts"] == 1
    assert body["jobs"] == 3


def test_worker_render_returns_payload(worker_client: TestClient, fake_controller: FakeController) -> None:
    resp = worker_client.post("/render", json={"url": "https://example.com/", "goto_timeout_ms": 30_000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["html"].startswith("<html>")
    assert body["final_url"] == "https://example.com/"
    assert body["status"] == 200
    assert body["elapsed_ms"] == 42
    assert fake_controller.calls == [("https://example.com/", 30_000)]


def test_worker_render_failure_becomes_502(worker_client: TestClient, fake_controller: FakeController) -> None:
    fake_controller.fail = True
    resp = worker_client.post("/render", json={"url": "https://example.com/"})
    assert resp.status_code == 502
    assert "error" in resp.json()


# --------------------------------------------------------------------------- pool manager


def test_worker_url_subprocess_and_override() -> None:
    assert BrowserPoolManager(_settings()).worker_url == "http://127.0.0.1:8765"
    assert BrowserPoolManager(_settings(scrape_worker_url="http://scrape-worker:9000")).worker_url == "http://scrape-worker:9000"


async def test_render_posts_and_parses_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/render"
        return httpx.Response(200, json={"html": "H", "final_url": "https://x/", "status": 200, "elapsed_ms": 7, "error": None})

    manager = BrowserPoolManager(_settings(), http_client_factory=_client_factory(handler))
    outcome = await manager.render("https://x/", goto_timeout_ms=10_000)
    assert isinstance(outcome, RenderOutcome)
    assert outcome.html == "H"
    assert outcome.final_url == "https://x/"
    assert outcome.status == 200


async def test_render_worker_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="worker crashed")

    manager = BrowserPoolManager(_settings(), http_client_factory=_client_factory(handler))
    with pytest.raises(BrowserPoolUnavailable):
        await manager.render("https://x/")


async def test_status_ok_and_unreachable() -> None:
    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"browser_started": True, "active_contexts": 0, "max_contexts": 1, "jobs": 5, "idle_seconds": 2.0})

    manager = BrowserPoolManager(_settings(), http_client_factory=_client_factory(ok_handler))
    status = await manager.status()
    assert status.ok is True and status.browser_started is True and status.jobs == 5

    def dead_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no worker here")

    manager2 = BrowserPoolManager(_settings(), http_client_factory=_client_factory(dead_handler))
    status2 = await manager2.status()
    assert status2.ok is False
    assert "unreachable" in status2.detail


# --------------------------------------------------------------------------- supervision (subprocess mode)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.pid = 4242

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_process() -> FakeProcess:
    return FakeProcess()


async def test_idle_watchdog_terminates_worker(fake_process: FakeProcess, monkeypatch) -> None:
    manager = BrowserPoolManager(
        _settings(scrape_worker_mode="subprocess", browser_idle_timeout=0.15),
        poll_interval=0.05,
    )

    async def _fake_spawn():
        manager._process = fake_process

    monkeypatch.setattr(manager, "_spawn_worker", _fake_spawn)
    await manager._spawn_worker()
    manager._last_use = time.monotonic() - 60  # idle

    await manager.start()
    await asyncio.sleep(0.35)
    assert fake_process.terminated is True  # watchdog killed it during idle
    await manager.stop()


async def test_process_not_terminated_while_idle_within_timeout(fake_process: FakeProcess, monkeypatch) -> None:
    manager = BrowserPoolManager(
        _settings(scrape_worker_mode="subprocess", browser_idle_timeout=60),
        poll_interval=0.05,
    )

    async def _fake_spawn():
        manager._process = fake_process

    monkeypatch.setattr(manager, "_spawn_worker", _fake_spawn)
    await manager._spawn_worker()
    manager._last_use = time.monotonic()  # still active

    await manager.start()
    await asyncio.sleep(0.2)
    assert fake_process.terminated is False  # still within idle timeout
    await manager.stop()


async def test_retry_after_respawn(fake_process: FakeProcess, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"html": "retried", "final_url": "https://x/", "status": 200, "elapsed_ms": 1, "error": None})

    manager = BrowserPoolManager(
        _settings(scrape_worker_mode="subprocess"),
        http_client_factory=_client_factory(handler),
    )

    async def _fake_spawn():
        manager._process = fake_process

    async def _noop_ready():
        return None

    monkeypatch.setattr(manager, "_spawn_worker", _fake_spawn)
    monkeypatch.setattr(manager, "_wait_until_ready", _noop_ready)
    manager._process = fake_process

    outcome = await manager._retry_after_respawn("https://x/", 5_000)
    assert outcome.html == "retried"
    assert fake_process.terminated is True  # dead worker was torn down first

# --------------------------------------------------------------------------- concurrency


async def test_concurrent_renders_are_not_serialised_by_the_manager(
    monkeypatch,
) -> None:
    """A slow render must not block other callers.

    Regression test: the manager used to hold its supervision lock for the whole
    render, so one 45s page load blocked every other scrape request behind it.
    Two renders here must be in flight at the same time.
    """
    inflight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        if request.url.path == "/ping":
            return httpx.Response(200, json={"ok": True})
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.15)
            return httpx.Response(
                200,
                json={"html": "<html/>", "final_url": "https://x/", "status": 200,
                      "elapsed_ms": 150, "error": None},
            )
        finally:
            inflight -= 1

    manager = BrowserPoolManager(
        _settings(scrape_worker_mode="external"),
        http_client_factory=_client_factory(handler),
    )

    results = await asyncio.gather(
        manager.render("https://a.example/"),
        manager.render("https://b.example/"),
    )
    await manager.stop()

    assert len(results) == 2
    assert peak == 2, "renders were serialised; the supervision lock leaked into the render path"


async def test_shared_client_is_reused_across_renders(monkeypatch) -> None:
    """The default client path keeps one connection pool instead of one per call."""
    built: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200, json={"html": "<html/>", "final_url": "https://x/", "status": 200,
                       "elapsed_ms": 1, "error": None}
        )

    real_async_client = httpx.AsyncClient

    def counting_factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        client = real_async_client(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", counting_factory)

    manager = BrowserPoolManager(_settings(scrape_worker_mode="external"))
    await manager.render("https://a.example/")
    await manager.render("https://b.example/")
    await manager.stop()

    assert len(built) == 1, f"expected one shared client, built {len(built)}"

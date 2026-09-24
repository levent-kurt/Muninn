"""Shared pytest fixtures: fake driver, tiny-delay settings, and TestClient app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.html_fixtures import BLOCK_PAGES, ENGINE_RESULTS_HTML


class FakeDriver:
    """Stand-in for BrowserDriver that returns canned HTML per engine."""

    def __init__(self, block_engines: set[str] | None = None, all_blocked: bool = False) -> None:
        self._started = False
        self.urls: list[str] = []
        self.block_engines = block_engines or set()
        self.all_blocked = all_blocked

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def url_count(self) -> int:
        return len(self.urls)

    @staticmethod
    def engine_from_url(url: str) -> str:
        if "google.com" in url:
            return "google"
        if "bing.com" in url:
            return "bing"
        if "duckduckgo" in url:
            return "ddg"
        if "mojeek.com" in url:
            return "mojeek"
        return "unknown"

    async def fetch_html(self, url: str) -> tuple[str, int]:
        self.urls.append(url)
        engine = self.engine_from_url(url)
        if self.all_blocked or engine in self.block_engines:
            return BLOCK_PAGES[engine], 200
        return ENGINE_RESULTS_HTML[engine], 200


def make_test_settings(**overrides) -> Settings:
    base = {
        "throttle_min_delay": 0.0,
        "throttle_max_delay": 0.0,
        "cache_db_path": ":memory:",
        "request_timeout_seconds": 10,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake_driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def client(fake_driver: FakeDriver):
    app = create_app(
        settings=make_test_settings(),
        driver_factory=lambda s: fake_driver,
    )
    with TestClient(app) as test_client:
        yield test_client
"""API-level integration tests for /scrape and the enhanced /health.

The real FastPathFetcher runs against a mocked httpx transport (offline), the
browser pool is a stub, and the search side uses the existing FakeDriver - so
the full app wiring is exercised without any real network or Chromium.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from browser_pool.manager import BrowserPoolUnavailable, PoolStatus, RenderOutcome
from fetchers.fast_path import FastPathFetcher
from tests.conftest import FakeDriver, make_test_settings

CLEAN_URL = "https://example.com/page"
CLEAN_HTML = """<!DOCTYPE html>
<html><head><title>About Cats</title>
<meta name="description" content="All about cats."></head>
<body><p>All about cats and kittens: their care, feeding, and behaviour.</p>
<p>This second paragraph adds enough readable prose for the extractor.</p>
<a href="https://example.com/nav">Nav</a><a href="/other">Other</a></body></html>"""
CHALLENGE_URL = "https://example.com/challenge"
CHALLENGE_HTML = "<html><body><h1>Just a moment...</h1><p>enable js</p></body></html>"
RENDERED_HTML = """<html><head><title>Rendered Page</title></head><body>
<p>Full content rendered by the browser.</p><a href="https://example.com/a">A</a>
</body></html>"""
SITEMAP_URL = "https://example.com/sitemap.xml"


def _sitemap_xml(n: int) -> str:
    locs = "\n".join(
        f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(n)
    )
    return (
        '<?xml version="1.0"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}\n</urlset>"
    )


class StubPool:
    """Fake BrowserPoolManager used across app-level tests."""

    def __init__(self, outcome: RenderOutcome | None = None,
                 error: Exception | None = None) -> None:
        self.outcome = outcome or RenderOutcome(html=RENDERED_HTML, final_url=CLEAN_URL, status=200, elapsed_ms=7)
        self.error = error
        self.calls: list[str] = []
        self.status_data = PoolStatus(ok=True, mode="subprocess", detail="stub",
                                      max_contexts=1)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def render(self, url: str, goto_timeout_ms: int = 60_000) -> RenderOutcome:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        o = self.outcome
        return RenderOutcome(html=o.html, final_url=o.final_url or url,
                             status=o.status, elapsed_ms=o.elapsed_ms)

    async def status(self) -> PoolStatus:
        return self.status_data


def _handler(routes: dict[str, str]):
    """MockTransport handler serving canned HTML per URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in routes:
            ct = "text/xml" if url.endswith(".xml") else "text/html"
            return httpx.Response(200, text=routes[url], headers={"content-type": ct})
        raise httpx.ConnectError(f"no route for {url}")

    return handler


def _make_app(handler, pool: StubPool):
    settings = make_test_settings()
    fetcher = FastPathFetcher(
        settings,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )
    return create_app(
        settings=settings,
        driver_factory=lambda s: FakeDriver(),
        scrape_fetcher_factory=lambda s: fetcher,
        scrape_pool_factory=lambda s: pool,
    )


# --------------------------------------------------------------------------- /scrape


def test_scrape_fast_path_on_standard_html() -> None:
    pool = StubPool()
    app = _make_app(_handler({CLEAN_URL: CLEAN_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CLEAN_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == CLEAN_URL
    assert body["final_url"] == CLEAN_URL
    assert body["status"] == 200
    assert body["title"] == "About Cats"
    assert body["meta_description"] == "All about cats."
    assert "cats" in body["text"].lower()
    assert body["rendered"] is False
    assert body["block_suspected"] is False
    assert body["cached"] is False
    assert body["content_type"] == "text/html"
    assert pool.calls == []


def test_scrape_sitemap_extraction_and_link_capping() -> None:
    pool = StubPool()
    app = _make_app(_handler({SITEMAP_URL: _sitemap_xml(70)}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": SITEMAP_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["links"]) == 60  # MAX_LINKS_CAP
    assert body["links"][0]["url"].startswith("https://example.com/p")
    assert body["text"] == ""
    assert body["rendered"] is False
    assert pool.calls == []


def test_scrape_challenge_escalates_to_browser() -> None:
    pool = StubPool()
    app = _make_app(_handler({CHALLENGE_URL: CHALLENGE_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CHALLENGE_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered"] is True
    assert body["block_suspected"] is False
    assert "browser" in body["text"].lower()
    assert pool.calls == [CHALLENGE_URL]


def test_scrape_render_flag_forces_browser_on_clean_page() -> None:
    pool = StubPool()
    app = _make_app(_handler({CLEAN_URL: CLEAN_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CLEAN_URL, "render": 1})
    assert resp.status_code == 200
    assert resp.json()["rendered"] is True
    assert pool.calls == [CLEAN_URL]


def test_scrape_rendered_page_still_blocked() -> None:
    pool = StubPool(outcome=RenderOutcome(html=CHALLENGE_HTML, final_url=CHALLENGE_URL, status=200, elapsed_ms=1))
    app = _make_app(_handler({CHALLENGE_URL: CHALLENGE_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CHALLENGE_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered"] is True
    assert body["block_suspected"] is True


def test_scrape_honours_max_text() -> None:
    pool = StubPool()
    app = _make_app(_handler({CLEAN_URL: CLEAN_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CLEAN_URL, "max_text": 25})
    assert resp.status_code == 200
    assert len(resp.json()["text"]) <= 25


def test_scrape_second_call_is_cache_hit() -> None:
    pool = StubPool()
    app = _make_app(_handler({CLEAN_URL: CLEAN_HTML}), pool)
    with TestClient(app) as client:
        first = client.get("/scrape", params={"url": CLEAN_URL}).json()
        second = client.get("/scrape", params={"url": CLEAN_URL}).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["age_seconds"] >= 0


@pytest.mark.parametrize(
    "params",
    [
        {},  # url required
        {"url": "not-a-url"},
        {"url": CLEAN_URL, "render": 5},
        {"url": CLEAN_URL, "render": -1},
        {"url": CLEAN_URL, "max_text": 0},
        {"url": CLEAN_URL, "max_text": 999_999},
    ],
)
def test_scrape_validation_errors(params: dict) -> None:
    app = _make_app(_handler({CLEAN_URL: CLEAN_HTML}), StubPool())
    with TestClient(app) as client:
        assert client.get("/scrape", params=params).status_code == 422


def test_scrape_fast_path_failure_is_502() -> None:
    app = _make_app(_handler({}), StubPool())
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": "https://example.com/down"})
    assert resp.status_code == 502
    assert "fast-path" in resp.json()["detail"]


def test_scrape_pool_down_is_503() -> None:
    pool = StubPool(error=BrowserPoolUnavailable("worker unreachable"))
    app = _make_app(_handler({CHALLENGE_URL: CHALLENGE_HTML}), pool)
    with TestClient(app) as client:
        resp = client.get("/scrape", params={"url": CHALLENGE_URL})
    assert resp.status_code == 503
    assert "worker unreachable" in resp.json()["detail"]


# --------------------------------------------------------------------------- /health


def test_health_reports_fetcher_and_browser_pool() -> None:
    app = _make_app(_handler({}), StubPool())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["fetcher"]["engine"] == "httpx-fast-path"
    assert body["fetcher"]["status"] == "ok"
    pool_info = body["browser_pool"]
    assert pool_info["status"] == "ok"
    assert pool_info["mode"] == "subprocess"
    assert pool_info["max_contexts"] == 1
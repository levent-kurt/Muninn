"""Tests for per-host politeness, TTL cache, and the scrape orchestrator. All network legs are stubbed - no real HTTP or Chromium."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import Settings
from browser_pool.manager import BrowserPoolUnavailable, RenderOutcome
from fetchers.fast_path import FastPathError, FastPathResult
from ops.cache import ScrapeCache
from ops.politeness import HostPoliteness
from ops.robots import RobotsGate
from schemas.scrape import LinkItem, ScrapeResponse
from services.scrape_service import ScrapeService

CLEAN_PAGE_URL = "https://example.com/page"
CLEAN_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>About Cats</title>
<meta name="description" content="All about cats."></head>
<body><article>
<h1>Cats</h1>
<p>All about cats and kittens: their care, feeding, and behaviour.</p>
<p>This second paragraph adds enough readable prose for the extractor.</p>
<nav><a href="https://example.com/nav">Nav</a><a href="/other">Other</a></nav>
</article></body></html>"""
CHALLENGE_HTML = "<html><body><h1>Just a moment...</h1><p>enable js</p></body></html>"
RENDERED_HTML = """<html><head><title>Rendered Page</title></head><body>
<p>Full content rendered by the browser.</p><a href="https://example.com/a">A</a>
</body></html>"""
SITEMAP_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
<url><loc>https://example.com/b</loc></url>
</urlset>"""
BIG_THIN_HTML = "<html><body>" + (" " * 40_000) + "<p>tiny</p></body></html>"


def _settings(**overrides) -> Settings:
    base = {
        "default_max_text": 32_000,
        "max_links_cap": 60,
        "text_min_char_threshold": 200,
        "text_anomaly_html_bytes": 20_480,
        "scrape_cache_ttl": 3_600,
        "per_host_delay_seconds": 0.0,
        "scrape_fast_path_timeout": 5.0,
        "scrape_max_body_bytes": 1_000_000,
    }
    base.update(overrides)
    return Settings(**base)


def _fast(url: str, html: str, status: int = 200, content_type: str = "text/html",
          final_url: str | None = None) -> FastPathResult:
    return FastPathResult(
        url=url, final_url=final_url or url, status=status,
        content_type=content_type, html=html,
    )


class FakeFetcher:
    def __init__(self, responses: dict[str, FastPathResult], error: Exception | None = None) -> None:
        self.responses = responses
        self.error = error
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FastPathResult:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if url not in self.responses:
            raise FastPathError(f"no canned response for {url}")
        return self.responses[url]


class FakePool:
    def __init__(self, outcome: RenderOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome or RenderOutcome(html="", final_url="", status=200, elapsed_ms=0)
        self.error = error
        self.calls: list[str] = []

    async def render(self, url: str, goto_timeout_ms: int = 60_000) -> RenderOutcome:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        o = self.outcome
        return RenderOutcome(
            html=o.html, final_url=o.final_url or url, status=o.status, elapsed_ms=o.elapsed_ms,
        )


async def _allow_any_url(url: str) -> str:
    """Stand-in for the SSRF guard: no DNS, so tests stay offline."""
    return url


async def _no_robots(_url: str) -> str | None:
    """Stand-in for the robots.txt download: unreachable -> fail open."""
    return None


def _service(fetcher: FakeFetcher, pool: FakePool, *, render: int = 0, settings=None) -> ScrapeService:
    s = settings or _settings()
    return ScrapeService(
        s,
        fetcher=fetcher,
        politeness=HostPoliteness(s.per_host_delay_seconds),
        cache=ScrapeCache(s.scrape_cache_ttl),
        browser_pool=pool,
        validator=_allow_any_url,
        robots=RobotsGate("Muninn", fetch_text=_no_robots),
    )


# --------------------------------------------------------------------------- TTL cache


async def test_cache_hit_populates_cached_and_age() -> None:
    cache = ScrapeCache(ttl_seconds=3_600)
    await cache.set(ScrapeResponse(url=CLEAN_PAGE_URL, final_url=CLEAN_PAGE_URL, status=200, text="x"))
    await asyncio.sleep(0.01)
    hit = await cache.get(CLEAN_PAGE_URL)
    assert hit is not None
    assert hit.cached is True
    assert 0 <= hit.age_seconds <= 60


async def test_cache_returns_deep_copy() -> None:
    cache = ScrapeCache(ttl_seconds=3_600)
    original = ScrapeResponse(
        url=CLEAN_PAGE_URL, final_url=CLEAN_PAGE_URL, status=200, text="x",
        links=[LinkItem(url="https://example.com/a", anchor_text="A", same_domain=True)],
    )
    await cache.set(original)
    hit = await cache.get(CLEAN_PAGE_URL)
    assert hit is not None
    hit.links[0].url = "https://example.com/mutated"
    again = await cache.get(CLEAN_PAGE_URL)
    assert again is not None
    assert again.links[0].url == "https://example.com/a"


async def test_cache_expires_after_ttl() -> None:
    cache = ScrapeCache(ttl_seconds=0)
    await cache.set(ScrapeResponse(url=CLEAN_PAGE_URL, final_url=CLEAN_PAGE_URL, status=200, text="x"))
    assert await cache.get(CLEAN_PAGE_URL) is None


# --------------------------------------------------------------------------- politeness


async def test_politeness_spaces_same_host() -> None:
    politeness = HostPoliteness(delay_seconds=0.15)
    url = "https://example.com/a"
    t0 = time.perf_counter()
    async with politeness.slot(url):
        pass
    async with politeness.slot(url):
        pass
    elapsed = time.perf_counter() - t0
    assert elapsed >= 0.13  # second request waited out the gap


async def test_politeness_different_hosts_run_concurrently() -> None:
    politeness = HostPoliteness(delay_seconds=0.3)
    t0 = time.perf_counter()

    async def grab(url: str) -> None:
        async with politeness.slot(url):
            await asyncio.sleep(0.05)

    await asyncio.gather(
        grab("https://a.example.com/"), grab("https://b.example.com/"),
        grab("https://c.example.com/"),
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.3  # no cross-host serialisation


# --------------------------------------------------------------------------- orchestrator


async def test_scrape_clean_fast_path() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CLEAN_PAGE_HTML)})
    pool = FakePool()
    service = _service(fetcher, pool)

    resp = await service.scrape(CLEAN_PAGE_URL)
    assert resp.rendered is False and resp.block_suspected is False
    assert resp.status == 200
    assert resp.title == "About Cats"
    assert "cats" in resp.text.lower()
    assert resp.final_url == CLEAN_PAGE_URL
    assert pool.calls == []  # no browser needed


async def test_scrape_escalates_on_challenge_and_renders() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CHALLENGE_HTML)})
    pool = FakePool(outcome=RenderOutcome(html=RENDERED_HTML, final_url=CLEAN_PAGE_URL, status=200, elapsed_ms=9))
    service = _service(fetcher, pool)

    resp = await service.scrape(CLEAN_PAGE_URL)
    assert resp.rendered is True
    assert resp.block_suspected is False  # rendered page is clean
    assert "browser" in resp.text.lower()
    assert pool.calls == [CLEAN_PAGE_URL]


async def test_scrape_render_flag_forces_browser() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CLEAN_PAGE_HTML)})
    pool = FakePool(outcome=RenderOutcome(html=RENDERED_HTML, final_url=CLEAN_PAGE_URL, status=200, elapsed_ms=5))
    service = _service(fetcher, pool)

    resp = await service.scrape(CLEAN_PAGE_URL, render=1)
    assert resp.rendered is True
    assert pool.calls == [CLEAN_PAGE_URL]


async def test_scrape_render_not_served_from_fast_path_cache() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CLEAN_PAGE_HTML)})
    pool = FakePool(outcome=RenderOutcome(html=RENDERED_HTML, final_url=CLEAN_PAGE_URL, status=200, elapsed_ms=4))
    service = _service(fetcher, pool)

    fast = await service.scrape(CLEAN_PAGE_URL, render=0)
    assert fast.rendered is False

    rendered = await service.scrape(CLEAN_PAGE_URL, render=1)
    assert rendered.rendered is True
    assert rendered.cached is False  # fast-path cache entry must not leak into render=1
    assert pool.calls == [CLEAN_PAGE_URL]


async def test_scrape_rendered_page_still_blocked() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CHALLENGE_HTML)})
    pool = FakePool(outcome=RenderOutcome(html=CHALLENGE_HTML, final_url=CLEAN_PAGE_URL, status=200, elapsed_ms=3))
    service = _service(fetcher, pool)

    resp = await service.scrape(CLEAN_PAGE_URL)
    assert resp.rendered is True
    assert resp.block_suspected is True


async def test_scrape_sitemap_returns_loc_links() -> None:
    url = "https://example.com/sitemap.xml"
    fetcher = FakeFetcher({url: _fast(url, SITEMAP_XML, content_type="text/xml")})
    pool = FakePool()
    service = _service(fetcher, pool)

    resp = await service.scrape(url)
    assert resp.rendered is False
    assert resp.text == ""
    assert [x.url for x in resp.links] == ["https://example.com/a", "https://example.com/b"]
    assert pool.calls == []


async def test_scrape_thin_text_escalates_to_browser() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, BIG_THIN_HTML)})
    pool = FakePool(outcome=RenderOutcome(html=RENDERED_HTML, final_url=CLEAN_PAGE_URL, status=200, elapsed_ms=2))
    service = _service(fetcher, pool)

    resp = await service.scrape(CLEAN_PAGE_URL)
    assert resp.rendered is True
    assert pool.calls == [CLEAN_PAGE_URL]


async def test_scrape_second_call_is_cache_hit() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CLEAN_PAGE_HTML)})
    service = _service(fetcher, FakePool())

    first = await service.scrape(CLEAN_PAGE_URL)
    second = await service.scrape(CLEAN_PAGE_URL)
    assert first.cached is False
    assert second.cached is True
    assert second.age_seconds >= 0
    assert fetcher.calls == [CLEAN_PAGE_URL]  # fetched exactly once


async def test_scrape_respects_max_text_cap() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CLEAN_PAGE_HTML)})
    service = _service(fetcher, FakePool())
    resp = await service.scrape(CLEAN_PAGE_URL, max_text=40)
    assert len(resp.text) <= 40


async def test_scrape_fast_path_error_propagates() -> None:
    fetcher = FakeFetcher({}, error=FastPathError("connection refused"))
    service = _service(fetcher, FakePool())
    with pytest.raises(FastPathError):
        await service.scrape(CLEAN_PAGE_URL)


async def test_scrape_pool_unavailable_propagates() -> None:
    fetcher = FakeFetcher({CLEAN_PAGE_URL: _fast(CLEAN_PAGE_URL, CHALLENGE_HTML)})
    pool = FakePool(error=BrowserPoolUnavailable("worker down"))
    service = _service(fetcher, pool)
    with pytest.raises(BrowserPoolUnavailable):
        await service.scrape(CLEAN_PAGE_URL)
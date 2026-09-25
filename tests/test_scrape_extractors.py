"""Unit tests for the /scrape extractors: sitemap, content, block detection,
and the fast-path fetcher (TODO2 Phase 2)."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from fetchers.fast_path import FastPathError, FastPathFetcher
from parsers import block_detector as bd
from parsers.content import extract_content, extract_links, extract_text
from parsers.sitemap import looks_like_sitemap, parse_sitemap
from schemas.scrape import LinkItem

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>Example Page — about cats</title>
  <meta name="description" content="A page about cats.">
</head>
<body>
  <h1>Cats</h1>
  <article>
    <p>All about cats and kittens: their care, feeding, and behaviour.</p>
    <p>This second paragraph adds enough readable prose for the extractor.</p>
    <nav>
      <a href="https://example.com/nav">Nav</a>
      <a href="/relative">Relative link</a>
      <a href="/relative">Duplicate of relative link</a>
      <a href="https://other.example.com/x">External link</a>
      <a href="#fragment">Fragment only</a>
      <a href="javascript:void(0)">JS link</a>
      <a href="mailto:hi@example.com">Mail link</a>
    </nav>
  </article>
</body>
</html>"""


# --------------------------------------------------------------------------- sitemap


@pytest.mark.parametrize(
    "url,content_type,expected",
    [
        ("https://example.com/sitemap.xml", None, True),
        ("https://example.com/sitemap_index.xml", None, True),
        ("https://example.com/wp-sitemap-posts.xml", None, True),
        ("https://example.com/page", None, False),
        ("https://example.com/data", "application/xml", True),
        ("https://example.com/feed", "text/xml", True),
        ("https://example.com/feed", "text/html", False),
    ],
)
def test_looks_like_sitemap(url: str, content_type: str | None, expected: bool) -> None:
    assert looks_like_sitemap(url, content_type) is expected


def test_parse_sitemap_maps_loc_entries() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/c</loc></url>
    </urlset>"""
    links = parse_sitemap(xml, max_links=60)
    assert [link.url for link in links] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert all(isinstance(item, LinkItem) for item in links)


def test_parse_sitemap_index_and_cap() -> None:
    xml = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/s2.xml</loc></sitemap>
      <sitemap><loc>https://example.com/s3.xml</loc></sitemap>
    </sitemapindex>"""
    links = parse_sitemap(xml, max_links=2)
    assert len(links) == 2


# --------------------------------------------------------------------------- content


def test_extract_content_metadata_text_links() -> None:
    content = extract_content(PAGE_HTML, "https://example.com/", max_text=32_000, max_links=60)
    assert content.title == "Example Page — about cats"
    assert content.meta_description == "A page about cats."
    assert "cats" in content.text.lower()
    assert len(content.text) > 50


def test_extract_text_character_cap() -> None:
    assert len(extract_text(PAGE_HTML, max_text=40)) <= 40
    assert extract_text(PAGE_HTML, max_text=32_000)  # full extraction is non-empty


def test_extract_links_resolve_dedupe_and_flag_same_domain() -> None:
    links = extract_links(PAGE_HTML, "https://example.com/", max_links=60)
    urls = [link.url for link in links]

    assert "https://example.com/relative" in urls  # relative resolved
    assert urls.count("https://example.com/relative") == 1  # deduplicated
    assert "https://example.com/nav" in urls
    assert "https://other.example.com/x" in urls

    rel = next(x for x in links if x.url == "https://example.com/relative")
    ext = next(x for x in links if x.url == "https://other.example.com/x")
    assert rel.same_domain is True
    assert rel.anchor_text == "Relative link"
    assert ext.same_domain is False

    # Non-navigable targets are skipped entirely.
    assert not any(
        x.url.startswith(("mailto:", "javascript:"))
        or x.url == "https://example.com/#fragment"
        for x in links
    )


def test_extract_links_caps_output() -> None:
    links = extract_links(PAGE_HTML, "https://example.com/", max_links=2)
    assert len(links) == 2


# --------------------------------------------------------------------------- block detector


def test_blocked_status_codes() -> None:
    for code in (403, 429, 503):
        assert bd.quick_block("<html>hi</html>", code).blocked is True
    assert bd.quick_block("<html>hi</html>", 200).blocked is False
    assert bd.quick_block("<html>hi</html>", 404).blocked is False


@pytest.mark.parametrize(
    "marker",
    ["Just a moment...", "cf-chl", "g-recaptcha", "captcha-delivery",
     "Checking your browser before accessing", "cf_chl_manager"],
)
def test_challenge_markers(marker: str) -> None:
    html = f"""<html><body><h1>{marker}</h1><p>Please enable JS and cookies.</p></body></html>"""
    decision = bd.quick_block(html, 200)
    assert decision.blocked is True
    assert decision.reason == bd.REASON_CHALLENGE


def test_empty_body_is_blocked() -> None:
    assert bd.quick_block("", 200).blocked is True
    assert bd.quick_block("   ", 200).blocked is True


def test_render_request_never_trusts_fast_path() -> None:
    decision = bd.quick_block("<html>clean page</html>", 200, render_requested=True)
    assert decision.blocked is True
    assert decision.reason == bd.REASON_RENDER_REQUESTED


def test_thin_text_rule() -> None:
    # large shell, tiny content -> suspect
    assert bd.thin_text_block(html_size_bytes=40_000, text_len=50,
                              html_threshold=20_480, text_threshold=200).blocked is True
    # large shell, real content -> clean
    assert bd.thin_text_block(html_size_bytes=40_000, text_len=5_000,
                              html_threshold=20_480, text_threshold=200).blocked is False
    # small shell -> no anomaly regardless of text
    assert bd.thin_text_block(html_size_bytes=1_000, text_len=10,
                              html_threshold=20_480, text_threshold=200).blocked is False


def test_evaluate_uses_supplied_text_for_thin_rule() -> None:
    big_shell = "<html>" + " " * 30_000 + "<h1>x</h1></html>"
    decision = bd.evaluate(
        big_shell, 200, extracted_text="tiny", html_threshold=20_480, text_threshold=200,
    )
    assert decision.blocked is True and decision.reason == bd.REASON_THIN_TEXT
    clean = bd.evaluate(
        big_shell, 200, extracted_text="lots" * 1_000, html_threshold=20_480, text_threshold=200,
    )
    assert clean.blocked is False


# --------------------------------------------------------------------------- fast-path fetcher


def _transport(handler):
    """Wrap a sync handler callable into a mocked httpx transport."""
    return httpx.MockTransport(handler)


def _settings(**overrides) -> Settings:
    base = {"scrape_fast_path_timeout": 5.0, "scrape_max_body_bytes": 1_000_000}
    base.update(overrides)
    return Settings(**base)


async def test_fast_path_tracks_redirects_and_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><title>moved page</title></html>",
            request=request,
        )

    fetcher = FastPathFetcher(
        _settings(),
        client_factory=lambda: httpx.AsyncClient(
            transport=_transport(handler), follow_redirects=True
        ),
    )
    result = await fetcher.fetch("https://example.com/old")
    assert result.status == 200
    assert result.final_url == "https://example.com/new"
    assert result.redirect_count == 1
    assert result.content_type == "text/html"
    assert "moved page" in result.html


async def test_fast_path_keeps_status_and_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"content-type": "text/html"}, text="rate limited", request=request)

    fetcher = FastPathFetcher(
        _settings(),
        client_factory=lambda: httpx.AsyncClient(transport=_transport(handler), follow_redirects=True),
    )
    result = await fetcher.fetch("https://example.com/limited")
    assert result.status == 429
    assert result.content_type == "text/html"


async def test_fast_path_bounds_body_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 100_000, request=request)

    fetcher = FastPathFetcher(
        _settings(scrape_max_body_bytes=1_000),
        client_factory=lambda: httpx.AsyncClient(transport=_transport(handler), follow_redirects=True),
    )
    result = await fetcher.fetch("https://example.com/big")
    assert len(result.html) <= 1_000


async def test_fast_path_raises_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fetcher = FastPathFetcher(
        _settings(),
        client_factory=lambda: httpx.AsyncClient(transport=_transport(handler), follow_redirects=True),
    )
    with pytest.raises(FastPathError):
        await fetcher.fetch("https://example.com/down")
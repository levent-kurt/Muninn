"""Unit tests for the four engine parsers and unified block detection."""

from __future__ import annotations

import pytest

from app.models import SearchResult
from drivers.parsers import (
    BingParser,
    DuckDuckGoParser,
    GoogleParser,
    MojeekParser,
    get_parser,
)
from drivers.parsers.base import BaseParser
from tests.html_fixtures import (
    BING_HTML,
    BLOCK_PAGES,
    DDG_HTML,
    GOOGLE_HTML,
    MOJEEK_HTML,
)


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "parser,html",
    [
        (GoogleParser, GOOGLE_HTML),
        (BingParser, BING_HTML),
        (DuckDuckGoParser, DDG_HTML),
        (MojeekParser, MOJEEK_HTML),
    ],
)
def test_parse_returns_results(parser: type[BaseParser], html: str) -> None:
    results = parser.parse(html, max_results=10)
    assert results
    assert all(isinstance(r, SearchResult) for r in results)
    for r in results:
        assert r.title
        assert r.url.startswith("http")
        assert r.snippet


def test_parse_respects_max_results() -> None:
    assert len(GoogleParser.parse(GOOGLE_HTML, max_results=1)) == 1
    assert len(BingParser.parse(BING_HTML, max_results=5)) == 2


def test_parse_deduplicates_by_url() -> None:
    dup = GOOGLE_HTML + GOOGLE_HTML
    urls = [r.url for r in GoogleParser.parse(dup)]
    assert len(urls) == len(set(urls))


def test_ddg_unwraps_redirect_urls() -> None:
    live_style_html = """
    <div class="results">
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fbeautiful-soup-web-scraper-python%2F&rut=abc">Real Python</a>
        <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fbeautiful-soup-web-scraper-python%2F&rut=abc">Scrape with BeautifulSoup.</a>
      </div>
    </div>
    """
    results = DuckDuckGoParser.parse(live_style_html)
    assert len(results) == 1
    assert results[0].url == "https://realpython.com/beautiful-soup-web-scraper-python/"
    assert results[0].snippet == "Scrape with BeautifulSoup."


def test_detect_block_http_429() -> None:
    assert GoogleParser.detect_block("<html>ok</html>", status=429) == "429"


@pytest.mark.parametrize(
    "parser,html",
    [
        (GoogleParser, BLOCK_PAGES["google"]),
        (BingParser, BLOCK_PAGES["bing"]),
        (DuckDuckGoParser, BLOCK_PAGES["ddg"]),
        (MojeekParser, BLOCK_PAGES["mojeek"]),
    ],
)
def test_detect_block_captcha(parser: type[BaseParser], html: str) -> None:
    assert parser.detect_block(html) is not None


def test_detect_block_clean_page() -> None:
    for parser in (GoogleParser, BingParser, DuckDuckGoParser, MojeekParser):
        assert parser.detect_block(GOOGLE_HTML) is None


def test_search_urls_are_formatted() -> None:
    assert "q=python" in GoogleParser.search_url("python", 10)
    assert "python" in BingParser.search_url("python", 10)
    assert "python" in DuckDuckGoParser.search_url("python", 10)
    assert "python" in MojeekParser.search_url("python", 10)


def test_registry_returns_known_parsers() -> None:
    assert get_parser("google") is GoogleParser
    assert get_parser("bing") is BingParser
    assert get_parser("ddg") is DuckDuckGoParser
    assert get_parser("mojeek") is MojeekParser


def test_registry_rejects_unknown_engine() -> None:
    with pytest.raises(KeyError):
        get_parser("yahoo")
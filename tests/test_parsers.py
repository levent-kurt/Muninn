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


# --------------------------------------------------------------------------- fixtures

GOOGLE_HTML = """
<div id="search"><div id="rso">
  <div class="g">
    <a href="https://docs.python.org/3/"><h3>Python 3 Documentation</h3></a>
    <div class="VwiC3b">Official docs for the Python language.</div>
  </div>
  <div class="g">
    <a href="https://www.python.org/"><h3>Python.org</h3></a>
    <div class="VwiC3b">The official home of the Python Programming Language.</div>
  </div>
</div></div>
"""

BING_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Python (programming language)</a></h2>
    <p class="b_lineclamp4">Python is a high-level, interpreted programming language.</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/python">Python Tutorial</a></h2>
    <p>Learn Python from scratch.</p>
  </li>
</ol>
"""

DDG_HTML = """
<div class="results">
  <div class="result">
    <a class="result__a" href="https://docs.python.org/3/tutorial/">The Python Tutorial</a>
    <a class="result__snippet" href="https://docs.python.org/3/tutorial/">An informal introduction to Python.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://realpython.com/">Real Python</a>
    <div class="result__snippet">Tutorials for professional developers.</div>
  </div>
</div>
"""

MOJEEK_HTML = """
<ul class="results-standard">
  <li class="result standard">
    <h2><a class="title" href="https://www.mojeek.com/about/">About Mojeek</a></h2>
    <p class="s">Independent search engine with its own index.</p>
  </li>
  <li class="result standard">
    <h2><a class="title" href="https://example.org/py">Python on Example</a></h2>
    <p class="s">A Python overview page.</p>
  </li>
</ul>
"""

BLOCK_PAGES = {
    "google": """
      <html><body>
        <form id="captcha-form"><div class="g-recaptcha"></div></form>
        <p>Our systems have detected unusual traffic from your computer network.</p>
      </body></html>
    """,
    "bing": "<html><body><h1>Sorry, please verify you are not a robot</h1></body></html>",
    "ddg": "<html><body><h1>Anomaly detected - please solve the captcha</h1></body></html>",
    "mojeek": "<html><body><h1>Too many requests - rate limit exceeded</h1></body></html>",
}


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
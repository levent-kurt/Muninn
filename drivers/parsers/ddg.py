"""DuckDuckGo (HTML endpoint) search parser.

The lightweight ``html.duckduckgo.com/html`` endpoint renders server-side and
keeps the same DOM shape for years, which is far more headless-friendly than
the JS-heavy ``duckduckgo.com`` app. Result links are wrapped in
``//duckduckgo.com/l/?uddg=<encoded>`` redirects that must be unwrapped.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote_plus, urlsplit

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class DuckDuckGoParser(BaseParser):
    ENGINE_NAME = "ddg"
    BASE_URL = "https://html.duckduckgo.com/html"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "anomaly",
        "captcha",
        "verify you are human",
        "our systems have detected unusual traffic",
        "too many requests",
        "enable javascript",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        return f"{cls.BASE_URL}/?q={quote_plus(query)}&kl=us-en"

    @staticmethod
    def _unwrap(url: str) -> str:
        """Extract the real destination from a DDG ``/l/?uddg=`` redirect."""
        if "/l/?uddg=" not in url:
            return url
        token = parse_qs(urlsplit(url).query).get("uddg")
        return token[0] if token else url

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("div.result"):
            link = item.select_one("a.result__a[href]")
            if link is None:
                continue
            url = cls._unwrap(cls._abs_url(link, cls.BASE_URL))
            if not url or "duckduckgo.com" in url:
                continue
            snippet = cls._text(item.select_one("a.result__snippet[href], .result__snippet"))
            results.append(SearchResult(title=cls._text(link), url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]
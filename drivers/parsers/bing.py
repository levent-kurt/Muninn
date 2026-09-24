"""Bing Search parser."""

from __future__ import annotations

from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from app.models import SearchResult
from drivers.parsers.base import BaseParser


class BingParser(BaseParser):
    ENGINE_NAME = "bing"
    BASE_URL = "https://www.bing.com"

    BLOCK_SIGNATURES: tuple[str, ...] = (
        "captcha",
        "unusual traffic",
        "verify you're a human",
        "verify you are a human",
        "not a robot",
        "our systems have detected unusual traffic",
        "we're sorry, but your computer or network may be sending automated queries",
    )

    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        count = max(1, min(max_results, 50))
        return f"{cls.BASE_URL}/search?q={quote_plus(query)}&count={count}&setlang=en"

    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a[href]")
            if link is None:
                continue
            url = cls._abs_url(link, cls.BASE_URL)
            if not url or "bing.com/search" in url:
                continue
            snippet = cls._text(item.select_one("p, .b_caption p, .b_lineclamp2, .b_lineclamp3, .b_lineclamp4"))
            results.append(SearchResult(title=cls._text(item.select_one("h2")), url=url, snippet=snippet))
            if len(results) >= max_results:
                break

        return cls._dedupe(results)[:max_results]
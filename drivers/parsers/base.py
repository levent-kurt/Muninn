"""Base parser interface, block detection, and shared helpers.

If you are adding a new engine:
  1. Subclass ``BaseParser``.
  2. Set ``ENGINE_NAME``, ``BASE_URL``, content-local ``BLOCK_SIGNATURES``.
  3. Implement ``search_url`` and ``parse`` (use ``_text`` / ``_abs_url``).
  4. Register it in ``drivers.parsers.PARSER_REGISTRY``.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from app.models import SearchResult


class EngineBlockedError(Exception):
    """Raised when a search engine answers with 429 / CAPTCHA / block page."""

    def __init__(self, engine: str, reason: str) -> None:
        self.engine = engine
        self.reason = reason
        super().__init__(f"engine {engine!r} blocked: {reason}")


class BaseParser:
    """Base class for all search engine parsers."""

    ENGINE_NAME: str = ""
    BASE_URL: str = ""
    # Case-insensitive substrings that mark a CAPTCHA / bot-block page.
    BLOCK_SIGNATURES: tuple[str, ...] = ()

    # ------------------------------------------------------------------ URL
    @classmethod
    def search_url(cls, query: str, max_results: int = 10) -> str:
        """Build the engine search URL for ``query``."""
        raise NotImplementedError

    # --------------------------------------------------------------- blocks
    @classmethod
    def detect_block(cls, html: str, status: int | None = None) -> str | None:
        """Return a reason string if the page looks blocked, else ``None``.

        HTTP 429 and any CAPTCHA/bot-detection signature are treated as blocks.
        """
        if status == 429:
            return "429"
        lowered = (html or "").lower()
        for signature in cls.BLOCK_SIGNATURES:
            if signature in lowered:
                return "captcha"
        return None

    # -------------------------------------------------------------- parsing
    @classmethod
    def parse(cls, html: str, max_results: int = 10) -> list[SearchResult]:
        """Parse organic results out of the engine HTML."""
        raise NotImplementedError

    @staticmethod
    def _text(node) -> str:
        """Return collapsed inner text of a BeautifulSoup node."""
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

    @staticmethod
    def _abs_url(node, base_url: str) -> str:
        """Resolve a possibly-relative href to an absolute URL."""
        href = node.get("href", "") if node else ""
        if not href:
            return ""
        return urljoin(base_url, href)

    @classmethod
    def _dedupe(cls, results: list[SearchResult]) -> list[SearchResult]:
        """De-duplicate by URL, preserving order."""
        seen: set[str] = set()
        unique: list[SearchResult] = []
        for r in results:
            if r.url in seen:
                continue
            seen.add(r.url)
            unique.append(r)
        return unique
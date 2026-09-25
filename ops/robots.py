"""``robots.txt`` policy for the ``/scrape`` module.

Whether a target may be scraped is the crawler's decision to make, not just the
user's. When ``SCRAPE_RESPECT_ROBOTS`` is on (the default), every ``/scrape``
target is checked against that origin's ``robots.txt`` before the fast-path or
the browser fetch runs, and a disallowed target is refused with HTTP 403.

Deliberate behaviours:

* **Fail open.** A missing, unreachable, or unparsable ``robots.txt`` allows the
  fetch. Refusing everything whenever a probe fails would make the module
  useless behind proxies and on sites without a robots file.
* **One probe per origin, cached** in a bounded LRU, so a busy crawl does not
  re-download the same robots file.
* The user agent presented to robots.txt is the configured browser UA, so the
  rules that apply are the ones a crawler of this kind actually receives.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

DEFAULT_MAX_CACHED_ORIGINS = 256


class RobotsDeniedError(Exception):
    """The target's robots.txt disallows this path for our user agent."""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


class RobotsGate:
    """Cached ``robots.txt`` checker for outbound scrape targets."""

    def __init__(
        self,
        user_agent: str,
        timeout: float = 5.0,
        max_cached: int = DEFAULT_MAX_CACHED_ORIGINS,
        fetch_text: object | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_cached = max(1, max_cached)
        # Injected in tests; defaults to a plain httpx GET.
        self._fetch_text = fetch_text
        self._cache: OrderedDict[str, RobotFileParser | None] = OrderedDict()
        # Guards the cache only; never held across a network call.
        self._gate = asyncio.Lock()

    @property
    def cached_origins(self) -> int:
        return len(self._cache)

    async def _download(self, url: str) -> str | None:
        if self._fetch_text is not None:
            return await self._fetch_text(url)  # type: ignore[operator]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers={"User-Agent": self._user_agent})
        except httpx.HTTPError as exc:
            logger.debug("robots.txt unreachable for %s: %s", url, exc)
            return None
        if resp.status_code >= 400:
            logger.debug("robots.txt for %s returned HTTP %s", url, resp.status_code)
            return None
        return resp.text

    async def _parser_for(self, url: str) -> RobotFileParser | None:
        origin = _origin(url)
        async with self._gate:
            if origin in self._cache:
                self._cache.move_to_end(origin)
                return self._cache[origin]

        text = await self._download(_robots_url(url))
        parser: RobotFileParser | None = None
        if text is not None:
            parser = RobotFileParser()
            try:
                parser.parse(text.splitlines())
            except Exception:  # pragma: no cover - defensive, parser is lenient
                logger.debug("unparsable robots.txt for %s", origin, exc_info=True)
                parser = None

        async with self._gate:
            self._cache[origin] = parser
            self._cache.move_to_end(origin)
            while len(self._cache) > self._max_cached:
                self._cache.popitem(last=False)
        return parser

    async def allows(self, url: str) -> bool:
        """True when ``robots.txt`` permits this path (fail-open)."""
        parser = await self._parser_for(url)
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(self._user_agent, url))
        except Exception:  # pragma: no cover - defensive
            logger.debug("robots evaluation failed for %s", url, exc_info=True)
            return True

    async def check(self, url: str) -> None:
        """Raise :class:`RobotsDeniedError` when robots.txt disallows ``url``."""
        if not await self.allows(url):
            raise RobotsDeniedError(
                f"robots.txt disallows fetching {url!r} for user agent {self._user_agent!r}"
            )

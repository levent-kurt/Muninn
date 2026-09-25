"""Fast-path (plain HTTP) fetcher.

Emulates a real browser at the header level (realistic ``User-Agent`` and
``Accept-Language``) via ``httpx``. Redirects are followed transparently and
the post-redirect URL is captured as ``final_url``. Bodies are read with a
hard byte cap so one hostile response cannot exhaust memory.

Connection reuse: one ``httpx.AsyncClient`` (and therefore one TLS context and
one connection pool) is shared across requests, created on first use and closed
by :meth:`close`. Tests inject a per-request client factory with a mock
transport instead, which is why the factory path is still owned per call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class FastPathError(Exception):
    """Raised when a fast-path fetch fails outright (DNS, TLS, timeout)."""


@dataclass
class FastPathResult:
    url: str  # originally requested URL
    final_url: str  # post-redirect URL (redirects tracked)
    status: int
    content_type: str
    html: str
    redirect_count: int = 0


class FastPathFetcher:
    """Single-purpose HTTP fetch; callers own the politeness/cache policy."""

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject a client with a MockTransport here.
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Open the shared client up front so the first request is not slower."""
        if self._client is None and self._client_factory is None:
            self._client = self._default_client()

    async def close(self) -> None:
        """Close the shared client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- client ---------------------------------------------------------------

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._settings.scrape_fast_path_timeout,
                write=10.0,
                pool=10.0,
            ),
            follow_redirects=True,
            headers={
                "User-Agent": self._settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def _client_for_request(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        if self._client is None:
            self._client = self._default_client()
        return self._client

    # -- fetch ----------------------------------------------------------------

    async def fetch(self, url: str) -> FastPathResult:
        """Fetch ``url``, mapping every failure mode to :class:`FastPathError`.

        Client *construction* is inside the guard as well as the request: an
        SSL/TLS problem (missing CA bundle, unreadable trust store) raises from
        ``httpx.AsyncClient(...)`` itself, and that used to escape the endpoint
        as an opaque HTTP 500 instead of the documented 502.
        """
        injected = self._client_factory is not None
        try:
            client = self._client_for_request()
            try:
                async with client.stream("GET", url) as resp:
                    body = await self._read_bounded(resp)
                    final_url = str(resp.url)
                    status = resp.status_code
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                    encoding = resp.encoding
                    redirect_count = len(resp.history) if resp.history else 0
            finally:
                # Only an injected (per-request) client is ours to close.
                if injected:
                    await client.aclose()
        except FastPathError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise FastPathError(f"fast-path fetch failed for {url}: {exc}") from exc

        html = body.decode(encoding or "utf-8", errors="replace")
        return FastPathResult(
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            html=html,
            redirect_count=redirect_count,
        )

    async def _read_bounded(self, resp: httpx.Response) -> bytes:
        cap = self._settings.scrape_max_body_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            remaining = cap - size
            if remaining <= 0:
                logger.debug("body exceeded cap (%d bytes) for %s", cap, resp.url)
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                size = cap
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

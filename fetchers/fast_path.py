"""Fast-path (plain HTTP) fetcher.

Emulates a real browser at the header level (realistic ``User-Agent`` and
``Accept-Language``) via ``httpx``. Bodies are read with a hard byte cap so one
hostile response cannot exhaust memory.

**Redirects are followed by hand, and every hop is re-validated.** Letting httpx
follow them (``follow_redirects=True``) would make the SSRF guard
first-hop-only: a public host could answer ``302 Location:
http://169.254.169.254/`` and the guard would never see the hop that actually
reaches the internal address. The guard is injected by
:meth:`set_validator`, so the same policy object decides for every URL in the
chain.

Connection reuse: one ``httpx.AsyncClient`` (and therefore one TLS context and
one connection pool) is shared across requests, created on first use and closed
by :meth:`close`. Tests inject a per-request client factory with a mock
transport instead, which is why the factory path is still owned per call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import Settings
from ops.netguard import TargetNotAllowedError, validate_target_url

logger = logging.getLogger(__name__)


class FastPathError(Exception):
    """Raised when a fast-path fetch fails outright (DNS, TLS, timeout)."""


class TooManyRedirectsError(FastPathError):
    """The redirect chain exceeded ``SCRAPE_MAX_REDIRECTS``."""


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
        validate_url: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject a client with a MockTransport here.
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        # Defaults to the real guard so this class is safe on its own; the
        # scrape service replaces it with the policy it was built with.
        self._validate = validate_url or self._default_validate

    def set_validator(self, validate: Callable[[str], Awaitable[str]]) -> None:
        """Use ``validate`` for the initial URL and every redirect target.

        The scrape service calls this so the SSRF policy it already holds is
        applied to the whole chain, rather than validating twice with two
        independently configured guards.
        """
        self._validate = validate

    async def _default_validate(self, url: str) -> str:
        return await validate_target_url(
            url,
            allow_private=self._settings.scrape_allow_private_targets,
            allowed_hosts=self._settings.scrape_allowed_hosts,
        )

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
            # Redirects are handled below so each hop can be validated.
            follow_redirects=False,
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
        client = (
            self._client_factory()
            if self._client_factory is not None
            else (self._client or self._default_client())
        )
        if self._client is None and self._client_factory is None:
            self._client = client
        # The redirect loop below is the only thing allowed to follow a redirect,
        # because it is the only thing that re-validates each hop. Force the
        # setting on whatever client we were handed (including one injected by a
        # caller or a test) so the guard cannot be bypassed by construction.
        client.follow_redirects = False
        return client

    # -- fetch ----------------------------------------------------------------

    async def fetch(self, url: str) -> FastPathResult:
        """Fetch ``url``, following redirects under policy.

        Maps every failure mode to :class:`FastPathError`. Client *construction*
        is inside the guard as well as the request: an SSL/TLS problem (missing
        CA bundle, unreadable trust store) raises from ``httpx.AsyncClient(...)``
        itself, and that used to escape the endpoint as an opaque HTTP 500
        instead of the documented 502.
        """
        injected = self._client_factory is not None
        try:
            client = self._client_for_request()
            try:
                result = await self._fetch_following_redirects(client, url)
            finally:
                # Only an injected (per-request) client is ours to close.
                if injected:
                    await client.aclose()
        except FastPathError:
            raise
        except TargetNotAllowedError:
            # A policy decision, not a transport failure: let it through
            # unchanged so the endpoint reports 400 ("target refused"), not 502
            # ("upstream fetch failed"). A redirect to a private address is a
            # rejected target, and saying so is more useful than blaming the
            # network.
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise FastPathError(f"fast-path fetch failed for {url}: {exc}") from exc
        return result

    async def _fetch_following_redirects(
        self, client: httpx.AsyncClient, url: str
    ) -> FastPathResult:
        """Request ``url``, re-validating every redirect target."""
        limit = self._settings.scrape_max_redirects
        current = await self._validate(url)
        hops = 0
        while True:
            async with client.stream("GET", current) as resp:
                location = resp.headers.get("location") if resp.is_redirect else None
                if location and hops < limit:
                    # Exit this response (closing its stream) and re-request.
                    hops += 1
                    current = await self._validate(urljoin(str(resp.url), location))
                    continue
                if resp.is_redirect and location:
                    raise TooManyRedirectsError(
                        f"fast-path exceeded {limit} redirects for {url}"
                    )
                body = await self._read_bounded(resp)
                return FastPathResult(
                    url=url,
                    final_url=str(resp.url),
                    status=resp.status_code,
                    content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
                    html=body.decode(resp.encoding or "utf-8", errors="replace"),
                    redirect_count=hops,
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

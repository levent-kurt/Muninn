"""Regressions for the two live defects REPORT2 listed as unresolved.

Both were real bugs that shipped; both are covered here so they cannot come back
silently:

* a redirect could carry the fast path from a public host to a private one,
  because only the caller's URL was ever validated;
* a render whose navigation timed out reported HTTP 200, asserting a success
  that was never observed.

No test here performs DNS: real-policy tests use IP literals (which the guard
checks directly) and wiring tests use a recording stub.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from browser_pool.manager import BrowserPoolManager
from fetchers.fast_path import (
    FastPathFetcher,
    TooManyRedirectsError,
)
from ops.netguard import TargetNotAllowedError, validate_target_url

# A public address, used as a literal so the guard needs no DNS.
PUBLIC = "93.184.216.34"


def _fetcher(handler, **overrides) -> FastPathFetcher:
    settings = Settings(
        scrape_fast_path_timeout=5.0,
        scrape_max_body_bytes=1_000_000,
        scrape_max_redirects=3,
        **overrides,
    )
    return FastPathFetcher(
        settings,
        # Deliberately built with follow_redirects=True: the fetcher must force
        # it off, or an injected client could bypass the validated hop loop.
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )


async def _real_policy(url: str) -> str:
    """The production guard, used directly (it is already async)."""
    return await validate_target_url(url, allow_private=False)


# --------------------------------------------------------------------------- redirects


async def test_redirect_to_cloud_metadata_is_refused() -> None:
    """The reported bypass: a public URL answering 302 to cloud metadata."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        raise AssertionError(f"the private target was requested: {request.url}")

    fetcher = _fetcher(handler)
    fetcher.set_validator(_real_policy)
    with pytest.raises(TargetNotAllowedError):
        await fetcher.fetch(f"https://{PUBLIC}/start")
    # Only the first hop was ever requested; the metadata endpoint was not.
    assert requested == [f"https://{PUBLIC}/start"]


async def test_redirect_to_loopback_is_refused() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/x":
            return httpx.Response(301, headers={"location": "http://127.0.0.1:8000/health"})
        raise AssertionError(f"loopback was requested: {request.url}")

    fetcher = _fetcher(handler)
    fetcher.set_validator(_real_policy)
    with pytest.raises(TargetNotAllowedError):
        await fetcher.fetch(f"https://{PUBLIC}/x")
    assert requested == [f"https://{PUBLIC}/x"]


async def test_every_hop_passes_through_the_validator() -> None:
    """The wiring: one validation per hop, not just for the caller's URL."""
    seen: list[str] = []

    async def recording(url: str) -> str:
        seen.append(url)
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/a":
            return httpx.Response(301, headers={"location": f"https://{PUBLIC}/b"})
        if path == "/b":
            return httpx.Response(302, headers={"location": f"https://{PUBLIC}/c"})
        return httpx.Response(200, text="<html>done</html>",
                              headers={"content-type": "text/html"})

    fetcher = _fetcher(handler)
    fetcher.set_validator(recording)
    result = await fetcher.fetch(f"https://{PUBLIC}/a")

    assert result.final_url == f"https://{PUBLIC}/c"
    assert result.redirect_count == 2
    assert "done" in result.html
    assert seen == [f"https://{PUBLIC}/a", f"https://{PUBLIC}/b", f"https://{PUBLIC}/c"]


async def test_redirect_loop_is_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"https://{PUBLIC}/next"})

    fetcher = _fetcher(handler)

    async def allow(url: str) -> str:
        return url

    fetcher.set_validator(allow)
    with pytest.raises(TooManyRedirectsError):
        await fetcher.fetch(f"https://{PUBLIC}/start")


async def test_relative_redirect_is_resolved_against_the_current_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/end"})
        return httpx.Response(200, text="<html>end</html>",
                              headers={"content-type": "text/html"})

    fetcher = _fetcher(handler)

    async def allow(url: str) -> str:
        return url

    fetcher.set_validator(allow)
    result = await fetcher.fetch(f"https://{PUBLIC}/start")
    assert result.final_url == f"https://{PUBLIC}/end"
    assert result.redirect_count == 1


async def test_fetcher_forces_its_own_redirect_handling() -> None:
    """An injected client cannot re-enable unguarded redirect following."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
        follow_redirects=True,
    )
    fetcher = FastPathFetcher(Settings(), client_factory=lambda: client)
    used = fetcher._client_for_request()  # noqa: SLF001
    assert used.follow_redirects is False


# --------------------------------------------------------------------------- render status


async def test_manager_does_not_coerce_a_missing_status_to_200() -> None:
    """`data.get("status") or 200` turned "no response observed" into a 200."""
    manager = BrowserPoolManager(Settings(scrape_worker_mode="external"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            # 0 is the worker's "navigation timed out, no HTTP response" signal.
            return httpx.Response(
                200,
                json={"html": "<html/>", "final_url": "https://x/", "status": 0,
                      "elapsed_ms": 1, "error": "navigation-timeout"},
            )
        return httpx.Response(200, json={"ok": True})

    manager._client_factory = lambda base_url: httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url=base_url
    )
    outcome = await manager._render_once("https://x/", 5_000)  # noqa: SLF001
    assert outcome.status == 0, "a timed-out render must not be reported as 200"


async def test_manager_keeps_a_real_status() -> None:
    manager = BrowserPoolManager(Settings(scrape_worker_mode="external"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(
                200,
                json={"html": "<html/>", "final_url": "https://x/", "status": 404,
                      "elapsed_ms": 1, "error": None},
            )
        return httpx.Response(200, json={"ok": True})

    manager._client_factory = lambda base_url: httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url=base_url
    )
    outcome = await manager._render_once("https://x/", 5_000)  # noqa: SLF001
    assert outcome.status == 404

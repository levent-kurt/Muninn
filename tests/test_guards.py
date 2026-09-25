"""Outbound-target guard, robots.txt policy, and rate limiting.

These are the components that stand between an anonymous caller and the host's
own network, so they are tested directly rather than only through the endpoint.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ops.netguard import TargetNotAllowedError, validate_target_url
from ops.ratelimit import RateLimiter, RateLimitExceeded
from ops.robots import RobotsDeniedError, RobotsGate

# --------------------------------------------------------------------------- netguard

# IP literals: no DNS involved, so these are hermetic.
BLOCKED_URLS = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8000/health",
    "http://localhost/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://0.0.0.0/",
    "http://224.0.0.1/",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_private_and_reserved_targets_are_refused(url: str) -> None:
    with pytest.raises(TargetNotAllowedError):
        await validate_target_url(url)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/"],
)
async def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(TargetNotAllowedError):
        await validate_target_url(url)


async def test_public_ip_literal_is_allowed() -> None:
    assert await validate_target_url("https://93.184.216.34/x") == "https://93.184.216.34/x"


async def test_private_targets_allowed_when_explicitly_permitted() -> None:
    url = "http://10.0.0.5/admin"
    assert await validate_target_url(url, allow_private=True) == url


async def test_allowlist_overrides_network_rules() -> None:
    # In-range private IP permitted because it is on the allowlist...
    assert await validate_target_url(
        "http://10.0.0.5/admin", allowed_hosts=("10.0.0.5",)
    ) == "http://10.0.0.5/admin"
    # ...and a host outside the allowlist is refused without any DNS lookup.
    with pytest.raises(TargetNotAllowedError):
        await validate_target_url("http://evil.example.net/", allowed_hosts=("example.com",))


async def test_allowlist_supports_wildcard_suffix() -> None:
    ok = await validate_target_url("https://docs.example.com/x", allowed_hosts=("example.com",))
    assert ok == "https://docs.example.com/x"
    with pytest.raises(TargetNotAllowedError):
        await validate_target_url("https://example.org/x", allowed_hosts=("example.com",))


async def test_url_is_normalised() -> None:
    assert await validate_target_url("  https://8.8.8.8/path?q=1#frag  ") == (
        "https://8.8.8.8/path?q=1"
    )


# --------------------------------------------------------------------------- robots

ALLOW_ALL = """
User-agent: *
Disallow: /private/
"""

DISALLOW_ALL = """
User-agent: *
Disallow: /
"""


async def _robots_text(rules: str):
    async def fetch(_url: str) -> str:
        return rules
    return fetch


async def test_robots_allows_permitted_path() -> None:
    gate = RobotsGate("Muninn", fetch_text=await _robots_text(ALLOW_ALL))
    assert await gate.allows("https://example.com/public") is True


async def test_robots_denies_disallowed_path() -> None:
    gate = RobotsGate("Muninn", fetch_text=await _robots_text(ALLOW_ALL))
    with pytest.raises(RobotsDeniedError):
        await gate.check("https://example.com/private/secret")


async def test_robots_disallow_all() -> None:
    gate = RobotsGate("Muninn", fetch_text=await _robots_text(DISALLOW_ALL))
    assert await gate.allows("https://example.com/anything") is False


async def test_robots_fails_open_when_unreachable() -> None:
    async def missing(_url: str) -> str | None:
        return None

    gate = RobotsGate("Muninn", fetch_text=missing)
    assert await gate.allows("https://example.com/anything") is True


async def test_robots_is_cached_per_origin() -> None:
    calls: list[str] = []

    async def counting(url: str) -> str:
        calls.append(url)
        return ALLOW_ALL

    gate = RobotsGate("Muninn", fetch_text=counting)
    for _ in range(4):
        await gate.allows("https://example.com/a")
        await gate.allows("https://example.com/b")
    assert len(calls) == 1  # one probe for the origin
    assert gate.cached_origins == 1


async def test_robots_cache_is_bounded() -> None:
    async def allow(_url: str) -> str:
        return ALLOW_ALL

    gate = RobotsGate("Muninn", max_cached=4, fetch_text=allow)
    for i in range(20):
        await gate.allows(f"https://host{i}.example.com/a")
    assert gate.cached_origins == 4


# --------------------------------------------------------------------------- rate limit

async def test_rate_limiter_allows_up_to_burst_then_rejects() -> None:
    limiter = RateLimiter(per_minute=60, burst=3)
    for _ in range(3):
        await limiter.acquire("1.2.3.4")
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.acquire("1.2.3.4")
    assert exc.value.retry_after >= 1


async def test_rate_limiter_is_per_client() -> None:
    limiter = RateLimiter(per_minute=60, burst=1)
    await limiter.acquire("1.1.1.1")
    with pytest.raises(RateLimitExceeded):
        await limiter.acquire("1.1.1.1")
    await limiter.acquire("2.2.2.2")  # a different client is unaffected


async def test_rate_limiter_refills_over_time() -> None:
    limiter = RateLimiter(per_minute=6000, burst=1)  # 100 tokens/second
    await limiter.acquire("c")
    with pytest.raises(RateLimitExceeded):
        await limiter.acquire("c")
    await asyncio.sleep(0.05)
    await limiter.acquire("c")  # refilled


async def test_rate_limiter_bounds_client_table() -> None:
    limiter = RateLimiter(per_minute=60, burst=60, max_clients=10)
    for i in range(50):
        await limiter.acquire(f"10.0.0.{i}")
    assert limiter.tracked_clients <= 10


async def test_rate_limiter_sweeps_idle_clients() -> None:
    limiter = RateLimiter(per_minute=60, burst=5, idle_seconds=0.0)
    await limiter.acquire("old")
    time.sleep(0.01)
    await limiter.acquire("new")  # triggers the sweep
    assert limiter.tracked_clients == 1


def test_rate_limiter_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        RateLimiter(per_minute=0)

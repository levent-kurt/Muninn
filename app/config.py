"""Environment-driven global configuration for StealthSearch.

Every tunable (throttle delays, quarantine durations, cache TTL, browser
settings) lives here and can be overridden with an environment variable so the
behaviour is identical on a home PC, in CI tests, and inside Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Engine pool registered for Round-Robin rotation.
SUPPORTED_ENGINES: tuple[str, ...] = ("google", "bing", "ddg", "mojeek")


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Global, immutable settings for the gateway service."""

    # --- Throttling ---------------------------------------------------------
    # Randomized delay enforced between consecutive outbound search requests.
    throttle_min_delay: float = field(default_factory=lambda: _env_float("THROTTLE_MIN_DELAY", 15.0))
    throttle_max_delay: float = field(default_factory=lambda: _env_float("THROTTLE_MAX_DELAY", 30.0))

    # --- Circuit breaker / quarantine --------------------------------------
    # 1st CAPTCHA/429 detection -> 30 minutes; repeat after cooldown -> 12 hours.
    quarantine_first_seconds: int = field(default_factory=lambda: _env_int("QUARANTINE_FIRST_SECONDS", 1_800))
    quarantine_escalated_seconds: int = field(default_factory=lambda: _env_int("QUARANTINE_ESCALATED_SECONDS", 43_200))

    # --- Caching ------------------------------------------------------------
    # TTL for identical-query responses (24-48h default, spec section 2.2).
    cache_ttl_seconds: int = field(default_factory=lambda: _env_int("CACHE_TTL_SECONDS", 86_400))
    cache_db_path: str = field(default_factory=lambda: os.environ.get("CACHE_DB_PATH", "data/cache.db"))

    # --- API ----------------------------------------------------------------
    default_max_results: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_RESULTS", 10))
    max_max_results: int = field(default_factory=lambda: _env_int("MAX_MAX_RESULTS", 50))
    request_timeout_seconds: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT_SECONDS", 120))

    # --- Browser driver -----------------------------------------------------
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    page_load_timeout_ms: int = field(default_factory=lambda: _env_int("PAGE_LOAD_TIMEOUT_MS", 45_000))
    navigation_timeout_ms: int = field(default_factory=lambda: _env_int("NAVIGATION_TIMEOUT_MS", 60_000))
    browser_args: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            os.environ.get(
                "BROWSER_ARGS",
                "--disable-blink-features=AutomationControlled",
            ).split(",")
        )
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
    )
    locale: str = field(default_factory=lambda: os.environ.get("LOCALE", "en-US"))

    # --- Service ------------------------------------------------------------
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_settings: Settings | None = None
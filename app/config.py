"""Environment-driven global configuration for Muninn.

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
    # Chromium's sandbox is a real security boundary; it is off by default only
    # because it is unreliable inside minimal containers. Running Muninn as root
    # AND with the sandbox disabled means a browser escape is a host compromise,
    # so keep BROWSER_NO_SANDBOX=false outside Docker and grant the container the
    # capabilities Chromium's sandbox needs (see docker-compose.yml).
    browser_no_sandbox: bool = field(
        default_factory=lambda: _env_bool("BROWSER_NO_SANDBOX", True)
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
    )
    locale: str = field(default_factory=lambda: os.environ.get("LOCALE", "en-US"))

    # --- Scrape module -----------------------------------------------------
    # Content extraction / response shaping.
    default_max_text: int = field(default_factory=lambda: _env_int("DEFAULT_MAX_TEXT", 32_000))
    max_links_cap: int = field(default_factory=lambda: _env_int("MAX_LINKS_CAP", 60))
    # Pages whose extracted text is smaller than this (despite a large raw
    # HTML body) are treated as suspicious / blocked.
    text_min_char_threshold: int = field(default_factory=lambda: _env_int("TEXT_MIN_CHAR_THRESHOLD", 200))
    # A page whose raw HTML exceeds this is "large" for the thin-text rule.
    text_anomaly_html_bytes: int = field(default_factory=lambda: _env_int("TEXT_ANOMALY_HTML_BYTES", 20_480))

    # Scrape result cache (URL+render keyed, TTL, bounded LRU).
    scrape_cache_ttl: int = field(default_factory=lambda: _env_int("SCRAPE_CACHE_TTL", 3_600))
    scrape_cache_max_entries: int = field(
        default_factory=lambda: _env_int("SCRAPE_CACHE_MAX_ENTRIES", 2_000)
    )

    # Fast-path (plain HTTP) fetch.
    scrape_fast_path_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_FAST_PATH_TIMEOUT", 20.0))
    scrape_max_body_bytes: int = field(default_factory=lambda: _env_int("SCRAPE_MAX_BODY_BYTES", 10_000_000))

    # Per-host politeness: minimum gap between consecutive requests to one
    # hostname (applies across the fast-path AND browser-render legs).
    per_host_delay_seconds: float = field(default_factory=lambda: _env_float("PER_HOST_DELAY_SECONDS", 2.0))
    # Idle time after which a host's politeness bookkeeping is discarded.
    politeness_idle_evict_seconds: float = field(
        default_factory=lambda: _env_float("POLITENESS_IDLE_EVICT_SECONDS", 300.0)
    )

    # --- Outbound safety guards (see ops/netguard.py, ops/robots.py) --------
    # SSRF guard: private/loopback/link-local targets are refused by default.
    scrape_allow_private_targets: bool = field(
        default_factory=lambda: _env_bool("SCRAPE_ALLOW_PRIVATE_TARGETS", False)
    )
    # Comma-separated allowlist that overrides the network checks entirely
    # (e.g. "mycorp.lan,10.0.0.5"). Empty = apply the SSRF rules.
    scrape_allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in os.environ.get("SCRAPE_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
    )
    # robots.txt policy for /scrape targets (fail-open when unreachable).
    scrape_respect_robots: bool = field(
        default_factory=lambda: _env_bool("SCRAPE_RESPECT_ROBOTS", True)
    )
    # Coarse per-client rate limit on /scrape (abuse protection, not auth).
    scrape_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("SCRAPE_RATE_LIMIT_PER_MINUTE", 60)
    )

    # Stealth browser pool / worker process.
    browser_idle_timeout: int = field(default_factory=lambda: _env_int("BROWSER_IDLE_TIMEOUT", 300))
    browser_max_contexts: int = field(default_factory=lambda: _env_int("BROWSER_MAX_CONTEXTS", 1))
    scrape_render_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_RENDER_TIMEOUT", 45.0))
    # "subprocess" (manager spawns/supervises python -m browser_pool.worker)
    # or "external" (worker runs as its own supervised service).
    scrape_worker_mode: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_MODE", "subprocess"))
    scrape_worker_host: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_HOST", "127.0.0.1"))
    scrape_worker_port: int = field(default_factory=lambda: _env_int("SCRAPE_WORKER_PORT", 8_765))
    scrape_worker_url: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_URL", ""))
    scrape_worker_startup_timeout: float = field(default_factory=lambda: _env_float("SCRAPE_WORKER_STARTUP_TIMEOUT", 30.0))
    scrape_worker_log_file: str = field(default_factory=lambda: os.environ.get("SCRAPE_WORKER_LOG_FILE", "data/scrape-worker.log"))

    # --- Service ------------------------------------------------------------
    # Bind address. Defaults to loopback so a bare `uvicorn app.main:app` is
    # not reachable from the network; docker-compose overrides this to 0.0.0.0
    # because containers must bind all interfaces.
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    # Interactive API docs (/docs, /redoc, /openapi.json). Off by default: the
    # service is unauthenticated, so its schema should not be published too.
    docs_enabled: bool = field(default_factory=lambda: _env_bool("DOCS_ENABLED", False))


    def browser_launch_args(self) -> list[str]:
        """Chromium flags for both browser launch sites."""
        args = list(self.browser_args)
        if self.browser_no_sandbox:
            args.append("--no-sandbox")
        return args


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_settings: Settings | None = None
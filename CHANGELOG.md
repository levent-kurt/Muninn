# Changelog

All notable changes to Muninn are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-09-26

First public release: a self-hosted search gateway with an isolated
stealth-browser scrape pool.

### Added

**Search**
- `GET /search` with engine rotation (Google, Bing, DuckDuckGo, Mojeek), a FIFO
  queue and a randomized 15-30s throttle.
- Circuit breaker with two-stage quarantine (30 minutes, then 12 hours).
  Quarantine state is persisted to SQLite, so a restart does not forget it.
- SQLite response cache keyed by a hash of the normalized query, 24h TTL.

**Scrape**
- `GET /scrape` with a fast-path HTTP leg that escalates to a stealth-browser
  worker when a page is blocked, challenge-walled, suspiciously thin, or when
  `render=1` is requested.
- Isolated browser worker process with lazy Chromium start, a fresh browser
  context per job, and a deterministic idle shutdown that leaves no orphaned
  browser processes.
- Sitemap detection and `<loc>` extraction, block/challenge detection, and
  trafilatura-based text and link extraction.
- Per-host politeness (lock + minimum gap) with idle state eviction.
- Bounded TTL caches for both search and scrape results.

**Operations**
- `GET /health` and `GET /status`; Docker Compose stack with an API container
  and a separate scrape-worker container.
- `Makefile` targets for install, lint, typecheck, test and run.
- `ruff` lint/format and strict `mypy` type checking, both clean.

### Security

- **SSRF guard.** `/scrape` refuses loopback, link-local (including cloud
  metadata), private, multicast and reserved targets, and rejects non-HTTP
  schemes. Enforced in the API and again in the worker. Overridable with
  `SCRAPE_ALLOW_PRIVATE_TARGETS` or `SCRAPE_ALLOWED_HOSTS`. DNS-rebinding
  exposure is documented rather than papered over.
- **robots.txt policy** for scrape targets, on by default, failing open when
  unreachable, with a bounded per-origin cache.
- **Rate limiting** on `/scrape` (429 + `Retry-After`), with bounded state.
- **Safe-by-default binding**: the service listens on `127.0.0.1`, and
  `/docs`, `/redoc` and `/openapi.json` are disabled unless `DOCS_ENABLED=1`.
- Container hardening: non-root user, read-only root filesystem, `cap_drop:
  ALL`, `no-new-privileges`.
- `MIT` license. The service is explicitly single-user and unauthenticated; see
  `SECURITY.md` for the threat model.

[Unreleased]: https://github.com/leventkurt/muninn/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/leventkurt/muninn/releases/tag/v0.1.0

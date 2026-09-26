# Changelog

All notable changes to Muninn are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-26

First public release: a self-hosted search gateway with an isolated
stealth-browser scrape pool. Runs as a single Python process on Linux and macOS,
under systemd or launchd.

### Added

**Search**
- `GET /search` with engine rotation (Google, Bing, DuckDuckGo, Mojeek), a FIFO
  queue and a randomized 15–30s throttle.
- Circuit breaker with two-stage quarantine (30 minutes, then 12 hours),
  persisted to SQLite so a restart does not forget it.
- SQLite response cache keyed by a hash of the normalized query, 24h TTL.
- One persistent Chromium, one browser context per engine so cookies and DOM
  state never cross sites.

**Scrape**
- `GET /scrape` with a fast-path HTTP leg that escalates to a stealth-browser
  worker when a page is blocked, challenge-walled, suspiciously thin, or when
  `render=1` is requested.
- Browser worker: lazy Chromium start, a fresh browser context per job, and a
  deterministic idle shutdown that leaves no orphaned browser processes.
- Sitemap detection and `<loc>` extraction, block/challenge detection, and
  trafilatura-based text and link extraction (the document is parsed once).
- Per-host politeness (lock + minimum gap) with idle state eviction.
- Bounded TTL caches for both search and scrape results.

**Operations**
- `GET /health`, `/health/live` (cheap, for probes) and `/health/ready`, plus
  `GET /status`.
- Swagger UI at `/docs`, ReDoc at `/redoc`, OpenAPI schema at `/openapi.json`,
  with per-endpoint summaries, typed parameters, response schemas, worked
  examples and every error code each endpoint can return. `DOCS_ENABLED=false`
  removes them.
- `Makefile` targets for install, browser setup, lint, typecheck, test and run.
- Deployment examples for systemd (`deploy/muninn-api.service`) and launchd
  (`deploy/com.muninn.api.plist`), both running unprivileged.
- `ruff` lint/format and `mypy` type checking, both clean, enforced in CI on
  Python 3.10 and 3.12.

### Security

- **SSRF guard.** `/scrape` refuses loopback, link-local (including cloud
  metadata), private, multicast and reserved targets, and rejects non-HTTP
  schemes. Enforced in the API and again in the worker, before the cache lookup.
  Overridable with `SCRAPE_ALLOW_PRIVATE_TARGETS` or `SCRAPE_ALLOWED_HOSTS`. The
  DNS-rebinding exposure is documented rather than papered over.
- **robots.txt policy** for scrape targets, on by default, failing open when
  unreachable, with a bounded per-origin cache.
- **Rate limiting** on `/scrape` (429 + `Retry-After`) with bounded state.
- **Safe-by-default binding**: the service listens on `127.0.0.1`; the
  documented production command binds `0.0.0.0` and the security section says
  plainly what that exposes.
- `MIT` license. The service is explicitly single-user and unauthenticated; see
  `SECURITY.md` for the threat model and a hardening checklist.

### Fixed

- **Chromium leaked in externally supervised mode.** The deterministic teardown
  (process snapshot, group signal, detached reaper) was gated on
  `SCRAPE_WORKER_MODE=subprocess`, so a worker run as its own service — the
  normal production setup — logged "shut down" while Chromium processes
  survived and accumulated, one idle cycle leaking a few more. The teardown now
  runs in both modes; only the process exit is mode-specific.
- **The reaper could kill the worker.** Because the reaper child calls
  `setsid()`, it no longer shares the worker's process group, so a guard that
  compared against its own group never matched and the reaper's `killpg` pass
  signalled the worker itself. The worker's pid and group are now passed in
  explicitly and excluded.
- **`lxml_html_clean` was missing from the requirements.** The import chain
  `trafilatura → justext → lxml.html.clean` needs it at runtime, but no
  distribution declares it as a hard requirement (`lxml` ships it only as the
  optional extra `html-clean`), so a fresh install could fail at import. Now
  pinned, with a comment explaining why it must not be removed.
- **`Settings.port` was dead config.** A bare `uvicorn app.main:app` used
  uvicorn's own default rather than `PORT`. `python -m app.main` now serves on
  `Settings.host`/`Settings.port`, and a test keeps the port consistent across
  the settings, the Makefile, the README and the service units.

### Notes

- The requirements are fully pinned and the suite is hermetic: 186 tests, no
  network and no browser, in about 8 seconds.
- Platform support is Linux and macOS (the browser teardown uses POSIX process
  groups, `setsid` and `pgrep`/`ps`).
- There is no container image; see the README for service-based deployment.

[0.1.0]: https://github.com/levent-kurt/Muninn/releases/tag/v0.1.0

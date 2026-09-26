# Changelog

All notable changes to Muninn are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`lxml_html_clean` was missing from the requirements.** The import chain
  `trafilatura → justext → lxml.html.clean` needs it at runtime, but no
  distribution declares it as a hard requirement — `lxml` ships it only as the
  optional extra `html-clean`. A fresh install could therefore fail at import
  with *"lxml.html.clean module is now a separate project lxml_html_clean"*.
  Now pinned explicitly, with a comment explaining why it must not be removed.
- **`docker compose up` failed with a platform mismatch** on a host whose
  architecture differed from the cached image, because both services set
  `image: muninn:latest` and compose reused whatever that tag pointed at instead
  of building from the working directory. Both services now set
  `pull_policy: build`, so the image always matches the local build context. No
  `platform:` is hardcoded, so the stack still builds natively on amd64 and
  arm64.
- **README Ubuntu instructions could not work on Ubuntu 22.04.** They asked for
  `apt-get install python3.12`, which is not in 22.04's repositories (deadsnakes
  is required). The project supports Python 3.10+, which 22.04 ships, so the
  instructions now use `python3` and mention the PPA only as an option.
- **README conflated two different commands**, suggesting
  `playwright install-deps` was interchangeable with `make browsers`. Downloading
  the browser and installing the OS libraries it links against are separate
  steps; both are now spelled out.
- **Repository URLs were wrong** in `pyproject.toml`, `CHANGELOG.md` and the
  OpenAPI contact/license fields (`leventkurt/muninn` instead of
  `levent-kurt/Muninn`).

### Added

- `make setup` — the whole first-run path: virtualenv, dependencies, Chromium
  and its OS libraries.
- `make browser-deps` — installs the OS libraries Chromium needs on Linux
  (needs `sudo`), previously folded into `make browsers`.
- A **Troubleshooting** section in the README covering the missing-CA-import,
  missing browser, missing shared libraries, platform mismatch, unavailable
  `python3.12`, unhealthy container and port-in-use cases.
- `tests/test_deploy_manifests.py` — guards the deployment manifest: compose
  must rebuild rather than trust a stale tag, must not hardcode a platform, must
  not probe with `curl`, the container must not run as root, and every
  requirement must be pinned (including the phantom `lxml_html_clean`).
- `schemas/common.py` with an `ErrorResponse` model, so the `{"detail": ...}`
  body every 4xx/5xx returns is described in the OpenAPI schema rather than
  being implicit.

### Changed

- **Default port moved from `8000` to `9999`.** The application, the Docker
  image, `docker compose`, the Makefile, CI and every documented example now
  use 9999. The scrape worker's own port (`8765`) is unchanged.
- **API documentation is now served by default** at `/docs` (Swagger UI),
  `/redoc` and `/openapi.json`, with full endpoint metadata: tagged operations,
  summaries, descriptions, parameter examples, response schemas, worked
  response examples, and every error code each endpoint can return. Set
  `DOCS_ENABLED=0` to turn the schema off on deployments you do not control.
- Corrected a false claim in `SECURITY.md`: the `/search` queue is **not**
  bounded, and `/search` has no rate limit.
- `python -m app.main` (`make serve`) now serves on `Settings.host`/`Settings.port`;
  previously `HOST`/`PORT` were ignored by the default launch path.

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
- **Safe-by-default binding**: the service listens on `127.0.0.1` (and compose
  publishes the port there too).
- Container hardening: non-root user, read-only root filesystem, `cap_drop:
  ALL`, `no-new-privileges`.
- `MIT` license. The service is explicitly single-user and unauthenticated; see
  `SECURITY.md` for the threat model.

[Unreleased]: https://github.com/levent-kurt/Muninn/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/levent-kurt/Muninn/releases/tag/v0.1.0

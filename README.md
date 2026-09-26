# Muninn — Self-Hosted Stealth Web Search & Data Extraction Gateway for AI Agents

Muninn is a lightweight, self-hosted **search and scraping API** for a home
server. It exposes a clean REST API for web searches while a single persistent,
anti-bot-hardened Chromium scrapes **Google**, **Bing**, **DuckDuckGo** and
**Mojeek** for you — rotating engines, throttling traffic, and auto-quarantining
blocked engines behind the scenes.

It also ships a **`/scrape` module**: fetch any URL over plain HTTP, escalate to
an isolated stealth-browser process when a site blocks or challenges the request
(or when you ask for it), and get back clean text, metadata and links — with
per-host politeness and a bounded TTL cache.

> **Single-user, self-hosted, no authentication.** Muninn binds to `127.0.0.1` by
> default and has no auth on any endpoint. See [Security](#security) before
> exposing it to a network you do not control, and read
> [Legal & ethical use](#legal--ethical-use) first.

```
[ Client Scripts ]  →  GET /search
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ FastAPI Gateway                                         │
│   ├── Cache Layer        (SQLite, TTL, query hashing)   │
│   └── Async Request Queue (FIFO, 15–30s throttler)      │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Engine Manager & Circuit Breaker                        │
│   └── Round-Robin rotation, quarantine 30m → 12h        │
│   └── State persisted to SQLite (survives restarts)     │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Driver Layer (playwright-stealth)                       │
│   └── Persistent Chromium BrowserContext                │
│        ├── Google / Bing / DuckDuckGo / Mojeek parsers  │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│ /scrape module (separate worker process)                │
│   fast-path HTTP → block detect → stealth browser       │
│   SSRF guard · robots.txt · rate limit · politeness     │
└─────────────────────────────────────────────────────────┘
```

---

## Contents

1. [Install](#1-install)
2. [Run and deploy](#2-run-and-deploy)
3. [API](#3-api)
4. [Configuration](#4-configuration)
5. [How it works](#5-how-it-works)
6. [Security](#6-security)
7. [Legal & ethical use](#7-legal--ethical-use)
8. [Development](#8-development)
9. [Observability](#9-observability)
10. [Project layout](#10-project-layout)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Install

**Platforms:** Linux and macOS. Muninn uses POSIX process groups, `setsid` and
`pgrep`/`ps` to tear its browser down without leaving orphans, so **Windows is
not supported**.

**Python 3.10+** (3.12 recommended on macOS; Ubuntu 22.04's stock 3.10 is
fine).

### The short version (Linux and macOS)

```bash
git clone https://github.com/levent-kurt/Muninn.git
cd Muninn
make setup        # venv + dependencies + Chromium + its OS libraries
make run          # http://127.0.0.1:9999/docs
```

`make setup` is the whole first-run path. On Linux the `browser-deps` step needs
`sudo`; on macOS it is a no-op and you will be asked to allow incoming
connections the first time Chromium starts.

If you prefer to do it by hand, or you already have a virtualenv, the four
steps behind `setup` are:

```bash
python3 -m venv .venv && source .venv/bin/activate   # make venv
make install          # pip install -r requirements.txt -r requirements-dev.txt
make browser-deps     # OS libraries Chromium needs (Linux, needs sudo)
make browsers         # download the Chromium build Playwright expects
```

`make browsers` and `make browser-deps` are **not** interchangeable:
`browsers` downloads the browser, `browser-deps` installs the system libraries
it links against. You need both on a bare Linux box.

### macOS (Homebrew)

```bash
brew install python@3.12
```

then follow the four steps above with `python3.12` in place of `python3`.

### Ubuntu / Debian (Linux)

Ubuntu 22.04 ships **Python 3.10**, which satisfies Muninn's `>=3.10`
requirement, so nothing extra is needed:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git make
```

then follow the four steps above. If you would rather use 3.12, add the
[deadsnakes](https://github.com/deadsnakes/ppa) PPA first — `apt-get install
python3.12` fails on stock 22.04 because the package does not exist there.

On a minimal or headless server you may also need:

```bash
sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
                        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
                        libxfixes3 libxrandr2 libgbm1 libasound2
```

(`make browser-deps` installs these for you; they are listed here only for
locked-down images where you manage packages yourself.)

## 2. Run

### Locally

```bash
make run      # uvicorn with reload on 127.0.0.1:9999
```

`make run` reloads on save. `make serve` runs without the reloader and reads
`HOST`/`PORT` from the environment (via `app/config.py`) instead of from the
Makefile.

```bash
curl http://127.0.0.1:9999/health/live
open http://127.0.0.1:9999/docs      # Swagger UI
```

### The command that actually matters

Everything below is the same process started differently:

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9999
```

`--host 0.0.0.0` binds every interface. Muninn has **no authentication**, so
on a host reachable from anywhere you do not control, that turns it into an open
proxy backed by a stealth browser. Prefer one of:

- bind a private address instead: `--host 127.0.0.1` or `--host 10.0.0.5`;
- keep `0.0.0.0` but let a firewall drop external traffic to 9999;
- put a reverse proxy with authentication in front (nginx, Caddy) and let it be
  the only thing listening publicly.

Set `DOCS_ENABLED=false` in the same situations, so `/docs` and
`/openapi.json` are not published to whoever can reach the port.

### Running as a service (production)

Muninn is a single long-lived process: one uvicorn worker, one persistent
search browser, and a scrape-worker subprocess it spawns on demand. It is
restarted by the service manager, not by an init system of its own.

**Linux (systemd).** A ready unit is in [`deploy/muninn-api.service`](deploy/muninn-api.service):

```bash
sudo useradd --system --create-home --home-dir /opt/muninn muninn
sudo -u muninn git clone https://github.com/levent-kurt/Muninn.git /opt/muninn/app
sudo -u muninn python3 -m venv /opt/muninn/app/.venv
sudo -u muninn /opt/muninn/app/.venv/bin/pip install -r /opt/muninn/app/requirements.txt
sudo -u muninn /opt/muninn/app/.venv/bin/python -m playwright install chromium
sudo -u muninn mkdir -p /opt/muninn/data

sudo cp deploy/muninn-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now muninn-api
```

The unit already runs as the unprivileged `muninn` account with
`NoNewPrivileges`, `ProtectSystem=strict` and a read-only filesystem outside
`/opt/muninn/data`.

```bash
systemctl status muninn-api
journalctl -u muninn-api -f          # logs
sudo systemctl restart muninn-api
```

**macOS (launchd).** A template is in
[`deploy/com.muninn.api.plist`](deploy/com.muninn.api.plist). Edit its two
`TODO` paths to your checkout and account, then:

```bash
cp deploy/com.muninn.api.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.muninn.api.plist
# older macOS:
launchctl load ~/Library/LaunchAgents/com.muninn.api.plist

launchctl list | grep muninn
tail -f data/stdout.log data/stderr.log
```

**Notes for a real deployment**

- **Do not run it as root.** The service account exists so that a Chromium
  escape is a compromised low-privilege account rather than a compromised host.
  `--no-sandbox` is on by default because Chromium's sandbox is unreliable under
  a service manager; if your host allows the sandbox, set
  `BROWSER_NO_SANDBOX=false`.
- **One worker process only.** Run a single uvicorn process. Multiple workers
  would each start their own browser, and the throttle and the circuit breaker
  only make sense per process.
- **Persistent state lives in `data/`** — the SQLite cache and the worker log.
  Back that directory up; nothing else needs to survive a restart except the
  engine quarantine counters, which live in the same database.
- **The throttle is the point.** The default 15–30s delay and 24h cache keep a
  residential IP well below any engine's quota. Do not lower them to "go
  faster".
- **Put it behind TLS** if it leaves your machine. There is no auth in the app.

## 3. API

**Interactive documentation is served at <http://127.0.0.1:9999/docs>** (Swagger
UI), with a ReDoc view at `/redoc` and the raw schema at `/openapi.json`. Every
endpoint documents its parameters, response schema, worked examples and each
error code it can return.

The service is unauthenticated, so set `DOCS_ENABLED=0` anywhere the machine is
reachable by someone you would not trust with the schema.

### `GET /search` — execute a search

| Parameter        | Type    | Default | Notes                                    |
|------------------|---------|---------|------------------------------------------|
| `q`              | string  | —       | required, 1–500 chars                    |
| `max_results`    | int     | `10`    | 1–50                                     |
| `engine`         | string  | —       | `google` \| `bing` \| `ddg` \| `mojeek`  |
| `force_refresh`  | bool    | `false` | bypass the cache entirely: read through the engines, do not store the result |

```bash
curl "http://127.0.0.1:9999/search?q=python+web+scraping&max_results=5"
```

```json
{
  "query": "python web scraping",
  "engine_used": "google",
  "cached": false,
  "execution_time_ms": 22140,
  "results_count": 5,
  "results": [
    {"title": "...", "url": "https://...", "snippet": "..."}
  ]
}
```

Status codes: `200` ok · `422` bad parameters · `503` every engine quarantined ·
`504` timed out in the queue.

### `GET /scrape` — fetch + extract a page

| Parameter  | Type | Default | Notes                                              |
|------------|------|---------|----------------------------------------------------|
| `url`      | url  | —       | required, `http(s)` only                            |
| `render`   | 0/1  | `0`     | `1` forces the stealth browser                     |
| `max_text` | int  | `32000` | 1–32000 body-text characters                        |

```bash
curl "http://127.0.0.1:9999/scrape?url=https://example.com"
curl "http://127.0.0.1:9999/scrape?url=https://example.com&render=1"
```

```json
{
  "url": "https://example.com/",
  "final_url": "https://example.com/",
  "status": 200,
  "title": "Example Domain",
  "meta_description": "...",
  "text": "Example Domain ...",
  "links": [{"url": "https://example.com/more", "anchor_text": "More", "same_domain": true}],
  "rendered": false,
  "block_suspected": false,
  "cached": false,
  "age_seconds": 0,
  "content_type": "text/html"
}
```

Status codes: `200` ok · `400` target rejected by the SSRF guard · `403`
disallowed by `robots.txt` · `422` bad parameters · `429` rate limited (with
`Retry-After`) · `502` fast-path fetch failed · `503` browser pool unavailable.

```python
import httpx
r = httpx.get("http://127.0.0.1:9999/scrape", params={"url": "https://example.com"})
print(r.json()["title"])
```

### `GET /health` — liveness

Reports the search driver, engine quarantine list, queue depth, cache size and
the scrape pool state. A lazy worker that has not been used yet is **healthy**.

### `GET /status` — metrics

Queue depth, cache entry count, per-engine state and request counters.

---

## 4. Configuration

Everything is an environment variable; the full list with defaults lives in
`app/config.py`.

### Core

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address; pass `--host` to uvicorn to override |
| `PORT` | `9999` | Bind port |
| `DOCS_ENABLED` | `true` | Serve `/docs` (Swagger UI), `/redoc` and `/openapi.json` |
| `LOG_FORMAT` | `text` | `text` for a terminal, `json` for one JSON object per line |
| `LOG_LEVEL` | `INFO` | Root log level |
| `CACHE_DB_PATH` | `data/cache.db` | SQLite path (`:memory:` disables disk) |
| `CACHE_TTL_SECONDS` | `86400` | Search cache TTL |
| `THROTTLE_MIN_DELAY` | `15` | Min seconds between outbound searches |
| `THROTTLE_MAX_DELAY` | `30` | Max seconds between outbound searches |
| `QUARANTINE_FIRST_SECONDS` | `1800` | 1st 429/CAPTCHA → quarantine |
| `QUARANTINE_ESCALATED_SECONDS` | `43200` | 2nd consecutive failure → quarantine |
| `DEFAULT_MAX_RESULTS` | `10` | Default value for `max_results` |
| `MAX_MAX_RESULTS` | `50` | Hard cap for `max_results` |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Max time `/search` waits in the queue |
| `MAX_SEARCH_QUEUE` | `100` | Search queue depth; deeper requests get `503` + `Retry-After` |
| `SEARCH_RATE_LIMIT_PER_MINUTE` | `30` | Per-client `/search` budget; excess gets `429` |

### Browser

| Variable | Default | Description |
|---|---|---|
| `HEADLESS` | `true` | Run Chromium headless |
| `BROWSER_ARGS` | `--disable-blink-features=AutomationControlled` | Extra Chromium flags (comma-separated) |
| `BROWSER_NO_SANDBOX` | `true` | Adds `--no-sandbox`. Set `false` to keep Chromium's sandbox |
| `PAGE_LOAD_TIMEOUT_MS` | `45000` | Per-page load budget |
| `NAVIGATION_TIMEOUT_MS` | `60000` | `page.goto` timeout |
| `USER_AGENT` | Chrome UA | Browser identity (overrides the default UA) |
| `LOCALE` | `en-US` | Browser locale |

### Scrape module

| Variable | Default | Description |
|---|---|---|
| `SCRAPE_WORKER_MODE` | `subprocess` | `subprocess` (API supervises) or `external` (own service) |
| `SCRAPE_WORKER_URL` | *(derived)* | External worker base URL |
| `SCRAPE_WORKER_HOST` | `127.0.0.1` | Where the spawned worker binds |
| `SCRAPE_WORKER_PORT` | `8765` | Port the spawned worker binds |
| `SCRAPE_WORKER_STARTUP_TIMEOUT` | `30.0` | Seconds to wait for a spawned worker |
| `SCRAPE_WORKER_LOG_FILE` | `data/scrape-worker.log` | Worker log destination |
| `BROWSER_IDLE_TIMEOUT` | `300` | Shut the scrape browser down after N idle seconds |
| `BROWSER_MAX_CONTEXTS` | `1` | Concurrent scrape browser contexts |
| `SCRAPE_RENDER_TIMEOUT` | `45.0` | Max seconds per render job |
| `SCRAPE_FAST_PATH_TIMEOUT` | `20.0` | Fast-path HTTP read timeout |
| `SCRAPE_MAX_BODY_BYTES` | `10000000` | Hard cap on fetched/rendered HTML |
| `SCRAPE_MAX_REDIRECTS` | `5` | Redirect hops followed; each hop is re-validated |
| `DEFAULT_MAX_TEXT` | `32000` | Default extracted-text cap |
| `MAX_TEXT_CAP` | `200000` | Hard ceiling on a caller's `max_text` |
| `MAX_LINKS_CAP` | `60` | Max links per response |
| `TEXT_MIN_CHAR_THRESHOLD` | `200` | Below this (on a large shell) → block-suspect |
| `TEXT_ANOMALY_HTML_BYTES` | `20480` | HTML size where the thin-text rule applies |
| `SCRAPE_CACHE_TTL` | `3600` | Scrape cache TTL |
| `SCRAPE_CACHE_MAX_ENTRIES` | `2000` | Scrape cache size bound (LRU) |
| `PER_HOST_DELAY_SECONDS` | `2.0` | Min gap between requests to one hostname |
| `POLITENESS_IDLE_EVICT_SECONDS` | `300` | Forget a host's politeness state after N idle seconds |

### Safety guards

| Variable | Default | Description |
|---|---|---|
| `SCRAPE_ALLOW_PRIVATE_TARGETS` | `false` | Allow loopback/private/link-local targets (**dangerous**) |
| `SCRAPE_ALLOWED_HOSTS` | *(empty)* | Comma-separated allowlist that replaces the network rules (supports `*.suffix`) |
| `SCRAPE_RESPECT_ROBOTS` | `true` | Check each target's `robots.txt` (fails open if unreachable) |
| `SCRAPE_RATE_LIMIT_PER_MINUTE` | `60` | Per-client `/scrape` budget; excess returns `429` |

Example at scale — a daily budget of ~650 requests at ~22.5 s average spacing
maps to roughly 16 h of active searching; the throttler and 24 h cache are sized
for that workload.

---

## 5. How it works

**Search.** Every uncached query becomes a job on a FIFO queue drained by a
single worker, which enforces a randomized delay between outbound requests. The
engine manager rotates engines round-robin; a 429 or CAPTCHA quarantines that
engine (30 min, then 12 h for consecutive failures) and the job retries on the
next active one. Quarantine state is written to SQLite, so restarting the
service does not immediately re-hammer an engine that just blocked you. All
four engines share one persistent Chromium, but each engine gets its own browser
context, so cookies and DOM state never cross sites; every request opens a
fresh page that is closed afterwards.

**Scrape.** For each target:

1. **Guards** — SSRF validation (scheme, resolved addresses) and the
   `robots.txt` policy run *before* the cache lookup, so a target that has since
   become disallowed is not served from cache.
2. **Cache** — keyed by URL *and* render flag, so `render=1` is never answered
   from a fast-path entry. Bounded LRU.
3. **Politeness** — one in-flight request per hostname, with a minimum gap.
4. **Fast path** — plain HTTP with browser-like headers, redirects followed, body
   read under a hard byte cap.
5. **Sitemap?** — `.xml` or `text/xml` targets return their `<loc>` entries
   without a render pass.
6. **Block detection** — status codes, challenge markers, and the "large shell,
   almost no text" heuristic. Also triggers on `render=1`.
7. **Extraction** — trafilatura for body text, BeautifulSoup for the title,
   description and resolved links (the document is parsed once).
8. **Escalation** — a blocked or forced-render target goes to the browser pool,
   then steps 6–7 run again on the rendered HTML.

**The browser worker** is a separate process. Chromium starts lazily, every job
gets a fresh browser context (so no cookies or DOM state carry between sites),
and after `BROWSER_IDLE_TIMEOUT` the worker closes the browser and exits
cleanly. Its idle shutdown is deterministic: it snapshots its process tree
before teardown, then a detached reaper SIGKILLs the node driver and every
Chromium process — including any that detached into their own process group — so
a stop never leaves browser processes behind.

---

## 6. Security

Muninn is **unauthenticated by design**: it is a single-user service, and
building a half-working auth layer would be worse than being explicit about the
boundary. The defaults are therefore safe rather than secure-but-surprising:

- binds **`127.0.0.1`** unless you pass `--host 0.0.0.0`;
- containers run as a **non-root user** with a read-only root filesystem,
  `cap_drop: ALL` and `no-new-privileges`.

`/docs`, `/redoc` and `/openapi.json` are served **by default**, because they
are the fastest way to understand the API. They are the one default that widens
exposure, so turn them off with `DOCS_ENABLED=0` anywhere the machine is
reachable by someone you would not trust with the schema.

What the code *does* enforce:

- **SSRF guard** — `/scrape` refuses loopback, link-local (including
  `169.254.169.254`), private, multicast and reserved addresses, and rejects
  non-`http(s)` schemes. Enforced in the API *and* again in the worker. DNS
  rebinding is a documented limitation, not an oversight.
- **`robots.txt` policy** for scrape targets, on by default.
- **Rate limiting** on `/scrape` with bounded per-client state.
- **Bounded memory** — capped response bodies, LRU caches, swept politeness
  state.
- **No orphaned browsers** on shutdown.

Read [`SECURITY.md`](SECURITY.md) for the full threat model, the escape hatches,
and a hardening checklist before exposing Muninn anywhere.

---

## 7. Legal & ethical use

**Please read this before you deploy Muninn.**

Muninn automates access to third-party websites, including search engines whose
terms of service may prohibit automated access. Deploying it means **you** are
the one making requests, from **your** IP address, to sites you do not control.

- You are responsible for complying with each target's terms of service,
  `robots.txt`, rate limits, and any applicable law (including data-protection
  and copyright rules where you live). Neither the project nor the maintainer
  can do that for you.
- The default configuration is tuned for **personal, low-volume use on your own
  hardware**. The throttler and cache exist to keep a residential IP well below
  any quota; do not remove them to go faster.
- Muninn's `/scrape` module **respects `robots.txt` by default**
  (`SCRAPE_RESPECT_ROBOTS=true`). Turning that off is your decision and your
  responsibility.
- Do not use Muninn to circumvent access controls you are not authorised to
  bypass, to access systems you are not authorised to access, or to collect
  personal data without a lawful basis.
- The maintainer provides the software as-is, with no warranty
  (see [`LICENSE`](LICENSE)), and accepts no liability for how you use it.

**Not a goal of this project:** operating at scale, defeating detection systems,
or building a multi-user scraping service. If you need those, build them
yourself.

---

## 8. Development

```bash
make install     # runtime + dev dependencies
make browsers    # pinned Chromium
make check       # ruff + mypy + pytest, exactly what CI runs
make format      # auto-fix lint and formatting
make run         # local dev server with reload
```

The suite is **offline and fast** (~160 tests, a couple of seconds): no test
touches the network or launches a real browser.

```bash
# 100+ simulated searches through the whole pipeline (fake driver, no network)
pytest tests/test_batch_smoke.py

# OPT-IN live check against real engines (adds low-volume real traffic)
LIVE=1 python scripts/live_probe.py "python web scraping"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow, and
[`CHANGELOG.md`](CHANGELOG.md) for what changed.

---

## 9. Observability

Two endpoints, no metrics library.

**`GET /status`** — human-facing JSON: queue depth, cache counts, per-engine
circuit-breaker state and lifetime counters.

**`GET /metrics`** — the same numbers in Prometheus text format, for scraping:

```bash
curl -s http://127.0.0.1:9999/metrics | head -30
```

What is exported, and why those numbers:

| Metric | Type | What it tells you |
|---|---|---|
| `muninn_http_requests_total{endpoint,status}` | counter | traffic per endpoint, error rate |
| `muninn_http_request_seconds{endpoint}` | histogram | where request time is spent |
| `muninn_search_queue_wait_seconds` | histogram | time a search waited in the queue — this is the throttle, not the engine, and it is the usual answer to "why is search slow" |
| `muninn_search_engine_seconds{engine}` | histogram | per-engine latency, so a slow engine is visible before it gets quarantined |
| `muninn_scrape_leg_seconds{leg}` | histogram | `fast_path` vs `render` — shows what browser escalations cost |
| `muninn_scrape_total{outcome}` | counter | `ok`, `sitemap`, `blocked`, `error` |
| `muninn_search_queue_rejections_total` | counter | requests refused because the queue was full |
| `muninn_*_rate_limited_total` | counter | requests refused by a rate limiter |

Latency buckets are fixed at 5 ms … 60 s, chosen around the real costs (a cache
hit is ~0 s, a fast-path fetch 0.1–2 s, a browser render 1–45 s, a queued search
up to `REQUEST_TIMEOUT_SECONDS`).

Label cardinality is bounded on purpose: only known routes get their own series
and everything else is folded into `other`, so an anonymous caller cannot create
unbounded series by requesting random paths.

**Structured logs.** `LOG_FORMAT=json` switches every logger to one JSON object
per line, which is what journald, Loki and CloudWatch want:

```bash
LOG_FORMAT=json make serve
```

```
{"ts":"2026-09-26T21:04:12+0300","level":"INFO","logger":"app.search_service",
 "message":"query='python web scraping' engine=google results=10 served"}
```

No dependencies were added for any of this.

---

## 10. Project layout

```
app/                      FastAPI gateway, config, search cache, engine manager, queue
drivers/                  playwright-stealth browser driver + per-engine parsers
schemas/                  Pydantic models for /scrape (LinkItem, ScrapeResponse)
parsers/                  sitemap parser, content extractor, block detector
fetchers/                 fast-path (plain HTTP) fetcher
browser_pool/             stealth-browser worker process + supervising manager
ops/                      politeness, scrape cache, SSRF guard, robots, rate limit
services/                 scrape pipeline orchestrator
routers/                  /scrape and /health FastAPI routers
scripts/                  opt-in live probe against real engines
tests/                    unit + API integration tests
data/                     SQLite cache and worker log (gitignored)
deploy/                   systemd unit and launchd job for production
Makefile                  install / lint / typecheck / test / run targets
pyproject.toml            project metadata, ruff, mypy and pytest configuration
```

---

## 11. Troubleshooting

Failures a fresh clone on a new machine actually hits.

### `ImportError: lxml.html.clean module is now a separate project lxml_html_clean`

A dependency is missing. The import chain `trafilatura -> justext ->
lxml.html.clean` needs `lxml_html_clean`, but no package declares it as a hard
requirement, so it must be installed explicitly. `requirements.txt` pins it and
a test asserts the pin stays.

```bash
pip install "lxml_html_clean==0.4.5"     # or: make install
```

Installing with `pip install --no-deps`, or into a pre-existing environment, is
the usual way to end up without it.

### `Executable doesn't exist at ~/.cache/ms-playwright/...`

The Chromium build was never downloaded, or it was downloaded for a different
platform.

```bash
make browsers         # playwright install chromium
```

### Chromium fails to start on Linux: missing shared libraries

The OS libraries Chromium links against are absent. `make browsers` does **not**
install these - `make browser-deps` does, and it needs `sudo`.

```bash
make browser-deps
```

### The service will not start: `No usable Chromium`

Chromium was never downloaded, or the OS libraries it needs are missing. Both
steps are required on a fresh machine and are easy to forget on a server:

```bash
.venv/bin/python -m playwright install chromium      # the browser
.venv/bin/python -m playwright install-deps chromium  # system libraries (sudo)
```

### `Permission denied` writing `data/cache.db`

The service account cannot write its data directory. Give it ownership once:

```bash
sudo chown -R muninn:muninn /opt/muninn/data
```

### The worker never appears, or `/scrape?render=1` returns 503

The API spawns the scrape worker on demand and waits for it to answer `/ping`.
Check the worker's own log - it is separate from the API's:

```bash
tail -f /opt/muninn/data/scrape-worker.log
```

`SCRAPE_WORKER_MODE` decides who owns that process: `subprocess` (default) means
the API spawns and supervises it; `external` means you run it as its own
service and point `SCRAPE_WORKER_URL` at it. If you set `external` but never
started it, renders will fail with 503.

### `apt-get install python3.12` says "Unable to locate package"

You are on Ubuntu 22.04, whose default repositories ship Python 3.10 - which
Muninn supports. Use `python3` / `python3-venv`, or add the deadsnakes PPA if you
specifically want 3.12.

### Port already in use

```bash
PORT=10000 make serve
# or, with uvicorn directly:
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 10000
```

Find out what already has it: `ss -ltnp | grep 9999` (Linux) or
`lsof -nP -iTCP:9999 -sTCP:LISTEN` (macOS).

---

## License

MIT — see [`LICENSE`](LICENSE). You may use, modify and redistribute this
software, including commercially, with attribution.

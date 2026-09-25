# Muninn — self-hosted search & scrape gateway

Muninn is a lightweight, dockerized **search and scraping API** for a home
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
2. [Run](#2-run)
3. [API](#3-api)
4. [Configuration](#4-configuration)
5. [How it works](#5-how-it-works)
6. [Security](#6-security)
7. [Legal & ethical use](#7-legal--ethical-use)
8. [Development](#8-development)
9. [Project layout](#9-project-layout)

---

## 1. Install

**Platforms:** Linux and macOS. Muninn uses POSIX process groups, `setsid` and
`pgrep`/`ps` to tear its browser down without leaving orphans, so **Windows is
not supported**.

**Python 3.10+** (3.12 recommended).

### macOS (Homebrew)

```bash
brew install python@3.12

git clone https://github.com/leventkurt/muninn.git
cd muninn
python3.12 -m venv .venv
source .venv/bin/activate
make install         # or: pip install -r requirements.txt
make browsers        # download the pinned Chromium build
```

> The first browser launch may ask to accept incoming network connections for
> the Chromium helper; allow it.

### Ubuntu / Debian (Linux)

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv
python3 -m playwright install-deps chromium   # or: make browsers

git clone https://github.com/leventkurt/muninn.git
cd muninn
python3.12 -m venv .venv
source .venv/bin/activate
make install
```

### Docker (any OS)

```bash
git clone https://github.com/leventkurt/muninn.git
cd muninn
docker compose up -d
```

This starts two containers from one image: `muninn` (the API, published on
`127.0.0.1:8000`) and `scrape-worker` (the browser pool, reachable only on the
internal network).

---

## 2. Run

### Locally

```bash
make run      # uvicorn with reload on 127.0.0.1:8000
```

### With Docker Compose

```bash
docker compose up -d --build
docker compose logs -f muninn
docker compose ps
```

`GET /health` returns `{"status": "ok", ...}` when both the browser and the
scrape pool are healthy. Cached queries survive restarts in `./data`.

> The compose stack runs the containers as an **unprivileged user** with a
> read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`.

---

## 3. API

Interactive docs are **off by default** because the service is unauthenticated.
Set `DOCS_ENABLED=1` locally, then browse `http://127.0.0.1:8000/docs`.

### `GET /search` — execute a search

| Parameter        | Type    | Default | Notes                                    |
|------------------|---------|---------|------------------------------------------|
| `q`              | string  | —       | required, 1–500 chars                    |
| `max_results`    | int     | `10`    | 1–50                                     |
| `engine`         | string  | —       | `google` \| `bing` \| `ddg` \| `mojeek`  |
| `force_refresh`  | bool    | `false` | bypass the cache for the read           |

```bash
curl "http://127.0.0.1:8000/search?q=python+web+scraping&max_results=5"
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
curl "http://127.0.0.1:8000/scrape?url=https://example.com"
curl "http://127.0.0.1:8000/scrape?url=https://example.com&render=1"
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
r = httpx.get("http://127.0.0.1:8000/scrape", params={"url": "https://example.com"})
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
| `HOST` | `127.0.0.1` | Bind address (compose sets `0.0.0.0`) |
| `PORT` | `8000` | Bind port |
| `DOCS_ENABLED` | `false` | Publish `/docs`, `/redoc`, `/openapi.json` |
| `CACHE_DB_PATH` | `data/cache.db` | SQLite path (`:memory:` disables disk) |
| `CACHE_TTL_SECONDS` | `86400` | Search cache TTL |
| `THROTTLE_MIN_DELAY` | `15` | Min seconds between outbound searches |
| `THROTTLE_MAX_DELAY` | `30` | Max seconds between outbound searches |
| `QUARANTINE_FIRST_SECONDS` | `1800` | 1st 429/CAPTCHA → quarantine |
| `QUARANTINE_ESCALATED_SECONDS` | `43200` | 2nd consecutive failure → quarantine |
| `DEFAULT_MAX_RESULTS` / `MAX_MAX_RESULTS` | `10` / `50` | `max_results` default and cap |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Max time `/search` waits in the queue |

### Browser

| Variable | Default | Description |
|---|---|---|
| `HEADLESS` | `true` | Run Chromium headless |
| `BROWSER_ARGS` | `--disable-blink-features=AutomationControlled` | Extra Chromium flags (comma-separated) |
| `BROWSER_NO_SANDBOX` | `true` | Adds `--no-sandbox`. Set `false` to keep Chromium's sandbox |
| `PAGE_LOAD_TIMEOUT_MS` | `45000` | Per-page load budget |
| `NAVIGATION_TIMEOUT_MS` | `60000` | `page.goto` timeout |
| `USER_AGENT` / `LOCALE` | Chrome UA / `en-US` | Browser identity |

### Scrape module

| Variable | Default | Description |
|---|---|---|
| `SCRAPE_WORKER_MODE` | `subprocess` | `subprocess` (API supervises) or `external` (own service) |
| `SCRAPE_WORKER_URL` | *(derived)* | External worker base URL |
| `SCRAPE_WORKER_HOST` / `_PORT` | `127.0.0.1` / `8765` | Where the spawned worker binds |
| `SCRAPE_WORKER_STARTUP_TIMEOUT` | `30.0` | Seconds to wait for a spawned worker |
| `SCRAPE_WORKER_LOG_FILE` | `data/scrape-worker.log` | Worker log destination |
| `BROWSER_IDLE_TIMEOUT` | `300` | Shut the scrape browser down after N idle seconds |
| `BROWSER_MAX_CONTEXTS` | `1` | Concurrent scrape browser contexts |
| `SCRAPE_RENDER_TIMEOUT` | `45.0` | Max seconds per render job |
| `SCRAPE_FAST_PATH_TIMEOUT` | `20.0` | Fast-path HTTP read timeout |
| `SCRAPE_MAX_BODY_BYTES` | `10000000` | Hard cap on fetched/rendered HTML |
| `DEFAULT_MAX_TEXT` | `32000` | Default extracted-text cap |
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

- binds **`127.0.0.1`** (and compose publishes the port to `127.0.0.1`);
- **`/docs`, `/redoc` and `/openapi.json` are disabled** unless
  `DOCS_ENABLED=1`;
- containers run as a **non-root user** with a read-only root filesystem,
  `cap_drop: ALL` and `no-new-privileges`.

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

## 9. Project layout

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
Dockerfile                Python + Chromium runtime image (non-root)
docker-compose.yml        API + scrape-worker deployment
Makefile                  install / lint / typecheck / test / run targets
pyproject.toml            project metadata, ruff, mypy and pytest configuration
```

## License

MIT — see [`LICENSE`](LICENSE). You may use, modify and redistribute this
software, including commercially, with attribution.

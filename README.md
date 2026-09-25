# StealthSearch — Search API Gateway

`StealthSearch` is a lightweight, dockerized **Search API Gateway** for a home
PC (see `SPEC.md`). It exposes a clean REST API for web searches while a
single persistent, anti-bot-hardened Chromium instance scrapes **Google**,
**Bing**, **DuckDuckGo**, and **Mojeek** for you — rotating engines, throttling
traffic, and auto-quarantining blocked engines behind the scenes.

It also ships a **`/scrape` module**: fetch any URL quickly over plain HTTP
(TODO2 `fast-path`), automatically escalate to an isolated stealth-browser
process pool when a site blocks or challenges the request (or when
`render=1` is requested), and receive clean text, metadata, and links — with
per-host politeness and a URL-keyed TTL cache.

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
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Driver Layer (playwright-stealth)                       │
│   └── Persistent Chromium BrowserContext                │
│        ├── Google / Bing / DuckDuckGo / Mojeek parsers  │
└─────────────────────────────────────────────────────────┘
```

---

## 1. System dependency installation

### macOS (Homebrew)

```bash
# 1. Python 3.10+ (3.12 recommended)
brew install python@3.12

# 2. Project (uses a venv; Chromium is downloaded by Playwright, no brew needed)
cd search_api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Chromium browser binaries
python -m playwright install chromium
```

> macOS note: the first browser launch may ask to accept incoming network
> connections for the Chromium helper; allow it.

### Ubuntu / Debian (Linux)

```bash
# 1. Python 3.10+ (3.12 recommended)
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip

# 2. Project
cd search_api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Chromium + every system library Playwright needs
python -m playwright install --with-deps chromium
```

### Docker (any OS)

If you only need the container, install [Docker](https://docs.docker.com/get-docker/)
(Compose included) and skip the Python steps — the `Dockerfile` installs Python
and Chromium with all dependencies for you.

---

## 2. Running the service

### Locally with Python

```bash
source .venv/bin/activate
python -m playwright install chromium        # once
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or with a one-line env override (see the table below):

```bash
THROTTLE_MIN_DELAY=15 THROTTLE_MAX_DELAY=30 uvicorn app.main:app --port 8000
```

The API is then at `http://localhost:8000`, docs at
`http://localhost:8000/docs`.

### With Docker Compose (recommended)

```bash
docker compose up --build -d
docker compose logs -f stealthsearch        # watch it boot the browser
docker compose ps                           # stealthsearch + scrape-worker
curl http://localhost:8000/health           # "status": "ok"
```

The SQLite cache is persisted in `./data/cache.db` (bind-mounted volume), so
cached queries survive container restarts. Two containers run: `stealthsearch`
(the API) and `scrape-worker` (the isolated stealth-browser render process,
supervised with `restart: always`; see `SCRAPE_WORKER_MODE=external`).

---

## 3. API usage

### `GET /search` — execute a search

| Parameter      | Type    | Required | Default | Description                                  |
|----------------|---------|----------|---------|----------------------------------------------|
| `q`            | string  | yes      | —       | The query string                             |
| `max_results`  | int     | no       | `10`    | Max organic results to yield (1–50)          |
| `engine`       | string  | no       | —       | Preferred engine `google|bing|ddg|mojeek`    |
| `force_refresh`| boolean | no       | `false` | Bypass the cache                             |

**cURL**

```bash
curl "http://localhost:8000/search?q=python+web+scraping&max_results=5"
curl "http://localhost:8000/search?q=python+web+scraping&engine=bing"
curl "http://localhost:8000/search?q=python+web+scraping&force_refresh=true"
```

**Python (`requests`)**

```python
import requests

resp = requests.get(
    "http://localhost:8000/search",
    params={"q": "python web scraping", "max_results": 5, "engine": "ddg"},
    timeout=180,          # requests wait in the throttled queue
)
data = resp.json()
print(data["engine_used"], data["cached"], data["results_count"])
for r in data["results"]:
    print("-", r["title"], "|", r["url"])
```

**Response (`200 OK`)**

```json
{
  "query": "python web scraping",
  "engine_used": "bing",
  "cached": false,
  "execution_time_ms": 1820,
  "results_count": 5,
  "results": [
    {
      "title": "Web Scraping with Python - Full Tutorial",
      "url": "https://example.com/python-scraping",
      "snippet": "Learn how to parse HTML and handle web requests using Python..."
    }
  ]
}
```

> **Why does `/search` take 15–30 seconds?** Every cache-miss is serialized
> behind a randomized 15–30 s throttle to keep a residential IP safe. Identical
> queries are served instantly from the cache (default TTL 24 h).

**Error responses**

| Code | When                                                        |
|------|-------------------------------------------------------------|
| 422  | Missing `q`, or unknown `engine`                            |
| 503  | Every engine is quarantined (`all_engines_quarantined`)     |
| 504  | Request waited longer than `REQUEST_TIMEOUT_SECONDS`        |

### `GET /scrape` — fetch + extract a page

Fetches a URL, extracts clean main text, metadata, and links. A fast plain-HTTP
leg handles normal pages; when the fast-path looks blocked (403/429/503,
Cloudflare/challenge markers, a large shell with almost no text) — or when you
pass `render=1` — the request escalates to the isolated stealth-browser pool.

| Parameter  | Type   | Required | Default | Description                                      |
|------------|--------|----------|---------|--------------------------------------------------|
| `url`      | `HttpUrl` | yes   | —       | Target page URL (`http`/`https`)                 |
| `render`   | int    | no       | `0`     | `1` = force browser rendering, `0` = fast-path first |
| `max_text` | int    | no       | `32000` | Max body-text characters to return (1–32000)     |

**cURL**

```bash
curl "http://localhost:8000/scrape?url=https://example.com"
curl "http://localhost:8000/scrape?url=https://example.com&render=1"
curl "http://localhost:8000/scrape?url=https://example.com&max_text=5000"
```

**Python (`requests`)**

```python
import requests

resp = requests.get(
    "http://localhost:8000/scrape",
    params={"url": "https://example.com", "render": 1},
    timeout=120,
)
data = resp.json()
print(data["title"], "| rendered:", data["rendered"], "| blocked:", data["block_suspected"])
print(data["text"][:300])
for link in data["links"][:5]:
    print("-", link["anchor_text"] or link["url"], "| same_domain:", link["same_domain"])
```

**Response (`200 OK`)**

```json
{
  "url": "https://example.com/",
  "final_url": "https://example.com/",
  "status": 200,
  "title": "Example Domain",
  "meta_description": "Example Domain description.",
  "text": "Example Domain. This domain is for use in illustrative examples...",
  "links": [
    {"url": "https://example.com/iana", "anchor_text": "IANA", "same_domain": false}
  ],
  "rendered": false,
  "block_suspected": false,
  "cached": false,
  "age_seconds": 0,
  "content_type": "text/html"
}
```

* `cached: true` means the result was served from the URL-keyed TTL cache;
  `age_seconds` then tells you how old the cached copy is.
* `rendered: true` means the page was extracted from a real stealth-browser
  render (fast-path was blocked or `render=1` was requested).
* `block_suspected: true` means the *final* page (fast-path or rendered) still
  looks like a challenge/block page.
* URLs ending in `.xml` (or servers answering `text/xml` /
  `application/xml`) are treated as **sitemaps**: `<loc>` entries are returned
  as `links` immediately, skipping rendering and text extraction.

**Error responses**

| Code | When                                                        |
|------|-------------------------------------------------------------|
| 422  | Missing/invalid `url`, or `render` / `max_text` out of range |
| 502  | Fast-path HTTP fetch failed (DNS/TLS/timeout)               |
| 503  | Stealth browser pool unreachable / worker down              |

### `GET /health` — liveness

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "browser_ready": true,
  "queue_depth": 0,
  "cache_entries": 142,
  "active_engines": ["google", "bing", "ddg", "mojeek"],
  "quarantined_engines": [],
  "uptime_seconds": 3600,
  "fetcher": {
    "status": "ok",
    "engine": "httpx-fast-path",
    "max_body_bytes": 10000000,
    "per_host_delay": 2.0
  },
  "browser_pool": {
    "status": "ok",
    "mode": "subprocess",
    "detail": "lazy (not started)",
    "browser_started": false,
    "active_contexts": 0,
    "max_contexts": 1,
    "jobs": 0,
    "idle_seconds": 0.0
  }
}
```

`fetcher.*` reports the scrape fast-path HTTP client;
`browser_pool.*` reports the isolated stealth-browser worker process — its
mode (`subprocess` = the API spawns it on demand, `external` = a separate
supervised service), whether Chromium is currently running, how many of the
`max_contexts` job slots are active, and how long since the last render job.

### `GET /status` — quarantine + metrics dashboard

```bash
curl http://localhost:8000/status
```

```json
{
  "queue_depth": 3,
  "cached_queries_count": 142,
  "metrics": {
    "searches_served": 87,
    "cache_hits": 61,
    "cache_misses": 26,
    "circuit_breaker_trips": 1,
    "requests_enqueued": 26
  },
  "engines": {
    "google": {"status": "active", "fail_count": 0, "success_count": 20, "quarantine_level": 0},
    "bing":  {"status": "quarantined", "fail_count": 1, "remaining_cooldown_seconds": 1700, "quarantine_level": 1},
    "ddg":   {"status": "active", "fail_count": 0, "success_count": 18, "quarantine_level": 0},
    "mojeek": {"status": "active", "fail_count": 0, "success_count": 12, "quarantine_level": 0}
  }
}
```

---

## 4. Configuration

All settings are environment variables, defined in `app/config.py`.

| Variable                           | Default      | Description                                              |
|------------------------------------|--------------|----------------------------------------------------------|
| `THROTTLE_MIN_DELAY`               | `15`         | Min seconds between outbound searches                    |
| `THROTTLE_MAX_DELAY`               | `30`         | Max seconds between outbound searches (`random.uniform`) |
| `QUARANTINE_FIRST_SECONDS`         | `1800`       | 1st 429/CAPTCHA → quarantine (30 min)                    |
| `QUARANTINE_ESCALATED_SECONDS`     | `43200`      | 2nd consecutive failure → quarantine (12 h)              |
| `CACHE_TTL_SECONDS`                | `86400`      | Cache TTL for identical queries (24 h; spec allows 48 h) |
| `CACHE_DB_PATH`                    | `data/cache.db` | SQLite database path (use `:memory:` to disable disk)  |
| `DEFAULT_MAX_RESULTS`              | `10`         | Default value for `max_results`                          |
| `MAX_MAX_RESULTS`                  | `50`         | Hard cap for `max_results`                               |
| `REQUEST_TIMEOUT_SECONDS`          | `120`        | Max time a `/search` waits in the queue                  |
| `HEADLESS`                         | `true`       | Run Chromium headless                                    |
| `PAGE_LOAD_TIMEOUT_MS`             | `45000`      | Per-page load budget                                     |
| `NAVIGATION_TIMEOUT_MS`            | `60000`      | `page.goto` timeout                                      |
| `USER_AGENT`                       | Chrome UA    | Override the browser user agent                          |
| `LOCALE`                           | `en-US`      | Browser locale                                           |
| `BROWSER_ARGS`                     | `--disable-blink-features=AutomationControlled` | Extra Chromium flags (comma-separated) |
| `DEFAULT_MAX_TEXT`                 | `32000`      | Default cap on `/scrape` extracted text chars   |
| `MAX_LINKS_CAP`                    | `60`         | Max links returned per `/scrape` response       |
| `TEXT_MIN_CHAR_THRESHOLD`          | `200`        | Text below this (on a large HTML shell) → block-suspect flag |
| `TEXT_ANOMALY_HTML_BYTES`          | `20480`      | HTML size above which the thin-text rule applies |
| `PER_HOST_DELAY_SECONDS`           | `2.0`        | Min gap between requests to the same hostname   |
| `SCRAPE_CACHE_TTL`                 | `3600`       | URL-keyed `/scrape` result cache TTL (seconds)  |
| `SCRAPE_FAST_PATH_TIMEOUT`         | `20.0`       | Fast-path HTTP read timeout                     |
| `SCRAPE_MAX_BODY_BYTES`            | `10000000`   | Hard cap on fetched/rendered HTML size (bytes)  |
| `BROWSER_IDLE_TIMEOUT`             | `300`        | Shut down the scrape browser after N idle seconds |
| `BROWSER_MAX_CONTEXTS`             | `1`          | Max concurrent scrape browser contexts (1–2)    |
| `SCRAPE_RENDER_TIMEOUT`            | `45.0`       | Max seconds a single render job may take        |
| `SCRAPE_WORKER_MODE`               | `subprocess` | `subprocess` (API spawns/supervises) or `external` (own service) |
| `SCRAPE_WORKER_URL`                | *(derived)*  | External worker base URL, e.g. `http://scrape-worker:8765` |
| `SCRAPE_WORKER_HOST` / `_PORT`     | `127.0.0.1` / `8765` | Where the subprocess worker binds        |
| `SCRAPE_WORKER_STARTUP_TIMEOUT`    | `30.0`       | Max seconds to wait for a spawned worker to boot |

Example at scale — a daily budget of ~650 requests at ~22.5 s average spacing
maps to roughly 16 h of active scraping; the throttler + 24 h cache are sized
for that workload.

---

## 5. How it works

* **Persistent browser** — one Chromium is launched at startup
  (`drivers/browser_driver.py`); every search opens and closes a *page* in the
  same `BrowserContext`. No per-request browser spawns (verified by
  `scripts/live_probe.py`, which confirms the context object is reused and RSS
  stays flat).
* **Stealth** — `playwright-stealth` evasions are applied to every page.
* **Round-Robin rotation** — `app/engine_manager.py` picks engines in a
  round-robin across *active* engines; quarantined engines are skipped.
* **Circuit breaker** — a 429 or CAPTCHA quarantines an engine for 30 minutes;
  a repeat failure after cooldown escalates to 12 hours. Success resets the
  counter. If every engine is quarantined, `/search` returns **503**.
* **Throttling** — cache-misses go through an `asyncio.Queue` drained by a
  single worker enforcing `random.uniform(15, 30)` between outbound fetches.
* **Caching** — queries are normalized (`q.strip().lower()`) and SHA-256-hashed
  into SQLite (`app/cache.py`); identical queries return instantly until TTL
  expiry. `force_refresh=true` bypasses the lookup.

### How the `/scrape` module works

* **Pipeline** — for each request: check the URL-keyed TTL cache →
  acquire the per-host politeness slot → fast-path HTTP fetch (redirects
  tracked) → sitemap? parse `<loc>` and return → evaluate the block detector →
  clean? extract text/links → blocked or `render=1`? escalate to the stealth
  browser pool → save to cache.
* **Fast-path** — `httpx` with browser-like headers, bounded body reads, and
  `final_url` from redirect tracking (`fetchers/fast_path.py`).
* **Extraction** — `trafilatura.extract` for clean main text (capped at
  `DEFAULT_MAX_TEXT`), BeautifulSoup/lxml for title, meta description, and
  resolved/deduplicated/capped links with `same_domain` flags.
* **Block detector** — 403/429/503, Cloudflare/challenge markers, a > 20 KB
  shell with < 200 chars of text, or an explicit `render=1` all force the
  browser leg (`parsers/block_detector.py`).
* **Isolated browser pool** — the worker runs as its **own process**
  (`python -m browser_pool.worker`) speaking a tiny internal HTTP API
  (`GET /ping`, `POST /render`). Chromium is lazy-started on the first render,
  every job gets a **fresh context+page destroyed immediately after** (no
  state leaks), concurrency is capped at `BROWSER_MAX_CONTEXTS` (1–2), and the
  browser shuts down after `BROWSER_IDLE_TIMEOUT` idle seconds. The pool
  manager lazily spawns/supervises the worker (`subprocess` mode) or calls a
  separately supervised service (`external` mode, used by docker-compose with
  `restart: always`), respawning it once if it dies mid-job.
* **Politeness** — `ops/politeness.py` enforces a per-host lock + mandatory
  `PER_HOST_DELAY_SECONDS` gap before each outbound fast-path/render leg.

## 6. Verification

```bash
# Unit + API tests (no network)
pytest

# 100+ simulated requests through the full pipeline (fake driver, no network)
python scripts/batch_smoke.py

# OPT-IN live check against real engines (adds low-volume real traffic)
LIVE=1 python scripts/live_probe.py "python web scraping"
```

## 7. Project layout

```
app/                      FastAPI gateway, config, cache, engine manager, queue
drivers/                  playwright-stealth browser driver + engine parsers
schemas/                  Pydantic models for the /scrape module (LinkItem, ScrapeResponse)
parsers/                  sitemap parser + content extractor + block detector
fetchers/                 fast-path (plain HTTP) fetcher
browser_pool/             standalone stealth-browser worker process + pool manager
ops/                      per-host politeness + URL-keyed TTL scrape cache
services/                 scrape pipeline orchestrator
routers/                  /scrape + /health FastAPI routers
scripts/                  batch smoke test and live probe/benchmark
tests/                    unit + API integration tests
data/                     SQLite cache volume (gitignored)
Dockerfile                Python + Chromium runtime image
docker-compose.yml        API + scrape-worker deployment
requirements.txt          Python dependencies
SPEC.md / TODO.md         design spec and phase-by-phase roadmap
TODO2.md                  /scrape module phase-by-phase roadmap
```
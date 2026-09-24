# StealthSearch — Search API Gateway

`StealthSearch` is a lightweight, dockerized **Search API Gateway** for a home
PC (see `SPEC.md`). It exposes one clean REST API for web searches while a
single persistent, anti-bot-hardened Chromium instance scrapes **Google**,
**Bing**, **DuckDuckGo**, and **Mojeek** for you — rotating engines, throttling
traffic, and auto-quarantining blocked engines behind the scenes.

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
curl http://localhost:8000/health           # "status": "ok"
```

The SQLite cache is persisted in `./data/cache.db` (bind-mounted volume), so
cached queries survive container restarts.

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
  "uptime_seconds": 3600
}
```

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
scripts/                  batch smoke test and live probe/benchmark
tests/                    unit + API integration tests
data/                     SQLite cache volume (gitignored)
Dockerfile                Python + Chromium runtime image
docker-compose.yml        one-command local deployment
requirements.txt          Python dependencies
SPEC.md / TODO.md         design spec and phase-by-phase roadmap
```
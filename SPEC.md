# Search API Gateway Specification (StealthSearch)

## 1. Overview

`StealthSearch` is a lightweight, dockerized Search API Gateway designed to run on a home PC. It exposes a unified REST API for web searches by scraping multiple search engines (`Google`, `Bing`, `DuckDuckGo`, `Mojeek`) using a persistent Playwright browser instance while evading anti-bot mechanisms, rate limits, and CAPTCHA blocks.

### Key Objectives

* Maintain a daily volume of 600–700 search requests without residential IP bans.

* Isolate client scripts from anti-bot logic, delays, browser orchestration, and parsing details.

* Provide structured, clean JSON search results.

* Implement automatic rate throttling and dynamic circuit breaker quarantines.

## 2. Architecture & Components

```
[ Client Scripts ]
        │
        ▼ (HTTP GET /search)
┌────────────────────────────────────────────────────────┐
│ FastAPI Gateway                                        │
│  ├── Cache Layer (SQLite / In-Memory TTL)              │
│  └── Async Request Queue (Throttler: 15–30s delay)     │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Engine Manager & Circuit Breaker                       │
│  ├── Active Engine Rotator (Round-Robin)               │
│  └── Quarantine Tracker (30m → 12h Escalation)         │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Driver Layer (Playwright Stealth Engine)               │
│  └── Persistent Chromium Context                       │
│       ├── Google Parser                                │
│       ├── Bing Parser                                  │
│       ├── DuckDuckGo Parser                            │
│       └── Mojeek Parser                                │
└────────────────────────────────────────────────────────┘

```

### 2.1 API Gateway

* **Framework**: FastAPI (Python 3.10+).

* **Function**: Receives HTTP search requests, checks cache, queues non-cached requests, and streams back standard JSON responses.

### 2.2 Cache Layer

* **Storage**: SQLite or In-Memory dict with TTL (24-48 hours default).

* **Hashing**: Query normalization (`q.strip().lower()`) hashed with MD5/SHA256 as key.

* **Goal**: Prevent duplicate search executions and reduce outbound request count.

### 2.3 Async Request Queue & Throttler

* **Queueing**: FIFO task queue.

* **Throttling**: Enforces a randomized 15–30 second delay between consecutive outbound search queries to ensure compliance with residential IP limits.

### 2.4 Engine Manager & Circuit Breaker

* **Engine Pool**: `google`, `bing`, `ddg`, `mojeek`.

* **Selection Strategy**: Round-Robin across non-quarantined engines to distribute traffic evenly.

* **Quarantine Logic**:

  * **1st CAPTCHA/429 Detection**: Quarantines engine for **30 minutes**.

  * **2nd consecutive CAPTCHA/429**: Quarantines engine for **12 hours**.

  * **Success Execution**: Resets the consecutive error counter for that engine.

### 2.5 Driver Layer

* **`playwright-stealth` Driver**: Single persistent Chromium `BrowserContext` for all engines (`Google`, `Bing`, `DuckDuckGo`, `Mojeek`).

* Reuses context across requests to minimize CPU/RAM spikes and avoid launching browser instances repeatedly.

## 3. API Endpoints

### `GET /search`

Execute a web search.

#### Query Parameters:

| **Parameter** | **Type** | **Required** | **Default** | **Description** | 
| `q` | String | Yes | \- | The query string | 
| `max_results` | Integer | No | `10` | Max search results to yield | 
| `force_refresh` | Boolean | No | `false` | Bypass cache lookup | 

#### Response (`200 OK`):

```
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

### `GET /status`

Retrieve current service health, queue size, and engine quarantine states.

#### Response (`200 OK`):

```
{
  "queue_depth": 3,
  "cached_queries_count": 142,
  "engines": {
    "google": { "status": "active", "fail_count": 0 },
    "bing": { "status": "quarantined", "quarantined_until": "2026-09-25T02:00:00Z", "fail_count": 1 },
    "ddg": { "status": "active", "fail_count": 0 },
    "mojeek": { "status": "active", "fail_count": 0 }
  }
}

```

## 4. Docker Deployment Strategy

* Single `Dockerfile` containing:

  * Python runtime environment.

  * Playwright binaries (`playwright install --with-deps chromium`).

  * Container entrypoint running `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

* Mounted volumes:

  * `./data/cache.db`: SQLite database persistent across container restarts.
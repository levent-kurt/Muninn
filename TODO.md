# StealthSearch Project Roadmap & Implementation Tasks

## Phase 1: Project Skeleton & Configuration
- [x] Initialize project layout (`app/`, `drivers/`, `data/`, `tests/`).
- [x] Create `requirements.txt` (`fastapi`, `uvicorn`, `playwright`, `playwright-stealth`, `beautifulsoup4`, `aiosqlite`).
- [x] Implement `app/config.py` for global settings (delays, engine timeouts, cache TTL).
- [x] Write initial `Dockerfile` installing Python and Chromium dependencies.

## Phase 2: Driver Implementation (Playwright Stealth)
- [x] Set up single persistent Chromium browser context manager (`drivers/browser_driver.py`).
- [x] Implement DOM parser & scraper for **Google Search**.
- [x] Implement DOM parser & scraper for **Bing Search**.
- [x] Implement DOM parser & scraper for **DuckDuckGo Search**.
- [x] Implement DOM parser & scraper for **Mojeek Search**.
- [x] Add unified 429/CAPTCHA and block detection hooks across all parsers.

## Phase 3: Engine Manager & Circuit Breaker
- [x] Create `EngineState` and `EngineManager` classes in `app/engine_manager.py`.
- [x] Implement Round-Robin engine selection logic ignoring quarantined engines.
- [x] Implement quarantine escalation logic (30m quarantine on 1st error -> 12h on 2nd error).
- [x] Write unit tests for quarantine transitions.

## Phase 4: Queue, Caching & API Layer
- [x] Implement SQLite cache layer (`app/cache.py`) with query normalization and TTL.
- [x] Implement async FIFO task queue with randomized interval throttler (15–30s delay).
- [x] Build FastAPI routes:
  - [x] `GET /search`: Endpoint handling cache lookup, queue dispatch, and response formatting.
  - [x] `GET /status`: Monitoring dashboard endpoint for engine statuses and queue metrics.

## Phase 5: Containerization & Integration Testing
- [x] Create `docker-compose.yml` service definition.
- [x] Test end-to-end flow with simulated batch search queries (100+ requests).
- [x] Benchmark memory footprint and ensure Chromium browser context is reused cleanly.
- [x] Write integration documentation and API usage examples.
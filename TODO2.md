# TODO: /scrape Module & Stealth Browser Pool Integration

## Git & Workflow Guidelines

- [x] Initialize Git repository if not already present (`git init`).
- [x] Create atomic commits after completing each phase (e.g., `git commit -m "feat(scrape): complete Phase X - [description]"`).

---

## Phase 1: Models, Config & Helper Utilities

* [x] **Data Schemas (`schemas/scrape.py`)**
* [x] Create `LinkItem` Pydantic model (`url: str`, `anchor_text: str`, `same_domain: bool`).
* [x] Create `ScrapeResponse` Pydantic model:
* `url: str`
* `final_url: str`
* `status: int`
* `title: str | None`
* `meta_description: str | None`
* `text: str`
* `links: list[LinkItem]`
* `rendered: bool`
* `block_suspected: bool`
* `cached: bool`
* `age_seconds: int`
* `content_type: str`




* [x] **Configuration Settings (`config.py`)**
* [x] Add `DEFAULT_MAX_TEXT` (default: 32000).
* [x] Add `MAX_LINKS_CAP` (default: 60).
* [x] Add `TEXT_MIN_CHAR_THRESHOLD` for anomaly check (default: 200).
* [x] Add `BROWSER_IDLE_TIMEOUT` (default: 300 seconds).
* [x] Add `PER_HOST_DELAY_SECONDS` (default: 2.0 seconds).
* [x] Add `SCRAPE_CACHE_TTL` (default: 3600 seconds).


* [x] Stage changes and commit Phase 1 (`git commit -m "feat(scrape): complete Phase 1 - models and config"`).

---

## Phase 2: Core Extractors & Fast-Path Pipeline

* [x] **Sitemap Parser (`parsers/sitemap.py`)**
* [x] Detect `sitemap.xml` URL extensions or `text/xml` / `application/xml` headers.
* [x] Parse `` elements from XML structure.
* [x] Map `` entries into `LinkItem` objects and return instantly, bypassing rendering and text extraction.


* [x] **Fast-Path Fetcher (`fetchers/fast_path.py`)**
* [x] Implement plain HTTP fetch using `httpx` with realistic browser `User-Agent` and `Accept-Language` headers.
* [x] Track HTTP redirects to capture `final_url`.


* [x] **Content Parsers (`parsers/content.py`)**
* [x] Implement `trafilatura.extract(html)` for clean main-text extraction, enforcing `max_text` character cap.
* [x] Implement BeautifulSoup (`lxml`) parser to extract:
* `title` tag content.
* `` content.
* Hyperlinks (`` tags): Resolve relative URLs to absolute using `final_url`, extract `anchor_text`, compute `same_domain` boolean flag, deduplicate URLs, and cap output to 60 items.




* [x] **Block & Fallback Detection Engine (`parsers/block_detector.py`)**
* [x] Flag `block_suspected = True` if:
* Upstream status code is `403`, `429`, or `503`.
* HTML body contains Cloudflare/Challenge markers (`Just a moment...`, `cf-chl`, `g-recaptcha`, `captcha-delivery`).
* Extracted text is `< 200` characters while raw HTML size is large (> 20 KB).
* Query parameter `render=1` is explicitly requested.




* [x] Stage changes and commit Phase 2 (`git commit -m "feat(scrape): complete Phase 2 - fast path and extractors"`).

---

## Phase 3: Isolated Stealth Browser Pool

* [x] **Dependency Pinning (`requirements.txt`)**
* [x] Pin `playwright` and `playwright-stealth` to exact stable versions.


* [x] **Browser Pool Service (`browser_pool/manager.py`)**
* [x] Implement lazy-start logic: Initialize Chromium instance only upon the first `render=1` request or fast-path block trigger.
* [x] Set up auto-shutdown timer: Close browser context after 5 minutes of idle time.
* [x] Limit concurrency to max 1-2 `BrowserContext` instances.
* [x] Implement per-page recycling: Open fresh context/page per job and destroy it immediately after extraction to prevent memory leaks and tracking across sites.


* [x] **Standalone Process Architecture (`browser_pool/worker.py`)**
* [x] Decouple browser pool worker execution from the main API process using an internal IPC / HTTP endpoint or queue interface.
* [x] Ensure process supervisor settings are configured (`Restart=always`).


* [x] Stage changes and commit Phase 3 (`git commit -m "feat(scrape): complete Phase 3 - stealth browser pool"`).

---

## Phase 4: Server-Side Operations & Pipeline Orchestration

* [x] **Per-Host Politeness Manager (`ops/politeness.py`)**
* [x] Build in-memory domain queue with host-level locking.
* [x] Enforce a mandatory delay (e.g., 2 seconds) between consecutive requests targeting the same hostname.


* [x] **TTL Caching Layer (`ops/cache.py`)**
* [x] Store completed `ScrapeResponse` objects by URL key.
* [x] Populate `cached=True` and calculate `age_seconds` for cache hits.


* [x] **Scrape Pipeline Orchestrator (`services/scrape_service.py`)**
* [x] Check cache $\rightarrow$ If hit, return cached result.
* [x] Check if URL is a Sitemap $\rightarrow$ If yes, parse `` and return.
* [x] Acquire politeness slot for host.
* [x] Attempt Fast-Path fetch.
* [x] Evaluate `block_detector`. If clean and `render=0`, extract text/links and return.
* [x] If blocked or `render=1`, escalate request to Stealth Browser Pool.
* [x] Save result to cache and return response.


* [x] Stage changes and commit Phase 4 (`git commit -m "feat(scrape): complete Phase 4 - orchestration and ops"`).

---

## Phase 5: API Router & Service Deployment

* [x] **FastAPI Endpoint (`routers/scrape.py`)**
* [x] Add `GET /scrape` endpoint accepting parameters:
* `url: HttpUrl` (required)
* `render: int = 0` (optional, 0 or 1)
* `max_text: int = 32000` (optional)




* [x] **Health Check Enhancements (`routers/health.py`)**
* [x] Update `/health` endpoint to monitor and report status for both the HTTP fetcher and the Stealth Browser process pool.


* [x] **Docker & Deployment Updates**
* [x] Update `Dockerfile` to include Playwright Chromium system dependencies (`playwright install-deps chromium`).
* [x] Update `docker-compose.yml` health check parameters and restart policies (`restart: always`).


* [x] **Testing & Verification**
* [x] Write integration tests for fast-path scraping on standard HTML pages.
* [x] Write tests for challenge detection and automatic escalation to the stealth browser.
* [x] Write tests verifying sitemap extraction and link capping.


* [x] Stage changes and commit Phase 5 (`git commit -m "feat(scrape): complete Phase 5 - endpoint and docker setup"`).

---

## Phase 6: Documentation & Final Cleanup

* [ ] **Documentation (`README.md`)**
* [ ] Add `/scrape` endpoint API documentation:
* Request parameters (`url`, `render`, `max_text`).
* Response JSON structure fields (`title`, `text`, `links`, `block_suspected`, etc.).


* [ ] Add cURL and Python (`requests`/`httpx`) code snippets demonstrating `/scrape` usage.
* [ ] Document configuration environment variables (`BROWSER_IDLE_TIMEOUT`, `PER_HOST_DELAY_SECONDS`, `SCRAPE_CACHE_TTL`).


* [ ] **Final Repository Verification**
* [ ] Run full test suite to ensure all endpoints (`/search`, `/scrape`, `/health`) pass cleanly.
* [ ] Stage changes and create final release commit (`git commit -m "docs: update README with /scrape usage and configuration"`).
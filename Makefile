# Muninn - developer entry points.
# `make help` lists everything.

PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin
PIP     = $(BIN)/pip
PYTEST  = $(BIN)/pytest
RUFF    = $(BIN)/ruff
MYPY    = $(BIN)/mypy

.DEFAULT_GOAL := help
.PHONY: help venv install browsers check lint format typecheck test run docker-build docker-up docker-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv (Python 3.10+)
	$(PYTHON) -m venv $(VENV)
	@echo "activate with: source $(VENV)/bin/activate"

install: ## Install runtime + dev dependencies
	$(PIP) install -r requirements.txt -r requirements-dev.txt

browsers: ## Download the pinned Chromium build
	$(BIN)/python -m playwright install chromium

check: lint typecheck test ## Everything CI runs

lint: ## Ruff lint check
	$(RUFF) check .

format: ## Ruff auto-format + import sort (opt-in; not a CI gate)
	$(RUFF) format .
	$(RUFF) check --fix .

typecheck: ## mypy static types
	$(MYPY) app browser_pool drivers fetchers ops parsers routers schemas services

test: ## Run the test suite (offline, no browser needed)
	$(PYTEST)

run: ## Run the API locally with autoreload
	$(BIN)/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

docker-build: ## Build the image
	docker compose build

docker-up: ## Start API + scrape worker
	docker compose up -d

docker-down: ## Stop and remove the stack
	docker compose down

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -not -path './$(VENV)/*' -exec rm -rf {} +

.DEFAULT_GOAL := help

# ── Virtual-env helpers ──────────────────────────────────────────────────────
.PHONY: dev
dev:  ## Install package + dev dependencies into the current Python environment
	pip install -e ".[dev]"

# ── Quality gates ────────────────────────────────────────────────────────────
.PHONY: lint
lint:  ## Run ruff linter and mypy type-checker
	ruff check src tests
	mypy src

.PHONY: format
format:  ## Auto-fix lint issues and reformat with ruff
	ruff check --fix src tests
	ruff format src tests

.PHONY: test
test:  ## Run the full test suite with pytest
	pytest -v

.PHONY: test-cov
test-cov:  ## Run tests with coverage report
	pytest -v --cov=divvy_forge --cov-report=term-missing

# ── Agent entry points ───────────────────────────────────────────────────────
.PHONY: batch
batch:  ## Run batch mode across the full watchlist
	python -m divvy_forge.batch_runner

.PHONY: single
single:  ## Run single-ticker mode   TICKER=INFY make single
	@test -n "$(TICKER)" || (echo "Usage: TICKER=INFY make single" && exit 1)
	python -m divvy_forge.batch_runner --ticker $(TICKER)

# ── MCP servers (for local testing without TrueForge) ───────────────────────
.PHONY: serve-divvy-reader
serve-divvy-reader:  ## Start divvy-reader MCP server over stdio
	python -m divvy_forge.divvy_reader

.PHONY: serve-market-data
serve-market-data:  ## Start market-data-fetcher MCP server over stdio
	python -m divvy_forge.market_data_fetcher

.PHONY: serve-github-pr
serve-github-pr:  ## Start github-pr-opener MCP server over stdio
	python -m divvy_forge.github_pr_opener

# ── Deploy ───────────────────────────────────────────────────────────────────
.PHONY: deploy
deploy:  ## Register MCP servers and coordinator agent on the TrueForge instance
	python -m divvy_forge.deploy

# ── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

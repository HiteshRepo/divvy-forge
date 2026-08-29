.DEFAULT_GOAL := help

# Prompt evaluation (promptfoo) — requires Node 22 via nvm
NODE_BIN := $(HOME)/.nvm/versions/node/v22.22.0/bin
PROMPTFOO := $(NODE_BIN)/node node_modules/.bin/promptfoo

# ── Prompt evaluations ───────────────────────────────────────────────────────
.PHONY: eval-fundamentals
eval-fundamentals:  ## Evaluate fundamentals subagent prompt with promptfoo
	$(PROMPTFOO) eval -c evals/fundamentals_subagent/promptfoo.yaml

.PHONY: eval-risk
eval-risk:  ## Evaluate dividend-cut-risk subagent prompt with promptfoo
	$(PROMPTFOO) eval -c evals/risk_subagent/promptfoo.yaml

.PHONY: eval
eval: eval-fundamentals eval-risk  ## Run all prompt evaluations

.PHONY: eval-view
eval-view:  ## Open promptfoo results in browser
	$(PROMPTFOO) view

# ── Virtual-env helpers ──────────────────────────────────────────────────────
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv
venv:  ## Create .venv with Python 3.12
	python3.12 -m venv $(VENV)

.PHONY: dev
dev: venv  ## Install package + dev dependencies into .venv
	$(PIP) install --upgrade pip -q
	$(PIP) install -e ".[dev]"

# ── Quality gates ────────────────────────────────────────────────────────────
.PHONY: lint
lint:  ## Run ruff linter and mypy type-checker
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/mypy src

.PHONY: format
format:  ## Auto-fix lint issues and reformat with ruff
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

.PHONY: test
test:  ## Run the full test suite with pytest
	$(VENV)/bin/pytest -v

.PHONY: test-cov
test-cov:  ## Run tests with coverage report
	$(VENV)/bin/pytest -v --cov=divvy_forge --cov-report=term-missing

# ── Agent entry points ───────────────────────────────────────────────────────
.PHONY: batch
batch:  ## Run batch mode across the full watchlist
	$(PYTHON) -m divvy_forge.batch_runner

.PHONY: single
single:  ## Run single-ticker mode   TICKER=INFY make single
	@test -n "$(TICKER)" || (echo "Usage: TICKER=INFY make single" && exit 1)
	$(PYTHON) -m divvy_forge.batch_runner --ticker $(TICKER)

# ── Protocol demo ────────────────────────────────────────────────────────────
SERVER ?= market-data

.PHONY: demo-protocol
demo-protocol:  ## Show raw MCP JSON-RPC messages   SERVER=divvy-reader make demo-protocol
	$(PYTHON) scripts/protocol_demo.py $(SERVER)

# ── MCP servers (for local testing without TrueForge) ───────────────────────
MCP := $(VENV)/bin/mcp

.PHONY: serve-divvy-reader
serve-divvy-reader:  ## Start divvy-reader MCP server over stdio
	$(PYTHON) -m divvy_forge.divvy_reader

.PHONY: serve-market-data
serve-market-data:  ## Start market-data-fetcher MCP server over stdio
	$(PYTHON) -m divvy_forge.market_data_fetcher

.PHONY: serve-github-pr
serve-github-pr:  ## Start github-pr-opener MCP server over stdio
	$(PYTHON) -m divvy_forge.github_pr_opener

.PHONY: serve-divvy-reader-http
serve-divvy-reader-http:  ## Start divvy-reader as HTTP/SSE server on port 9001
	$(PYTHON) -m divvy_forge.divvy_reader --sse

.PHONY: serve-market-data-http
serve-market-data-http:  ## Start market-data-fetcher as HTTP/SSE server on port 9002
	$(PYTHON) -m divvy_forge.market_data_fetcher --sse

.PHONY: serve-github-pr-http
serve-github-pr-http:  ## Start github-pr-opener as HTTP/SSE server on port 9003
	$(PYTHON) -m divvy_forge.github_pr_opener --sse

.PHONY: inspect-divvy-reader
inspect-divvy-reader:  ## Open MCP Inspector for divvy-reader in browser
	$(MCP) dev src/divvy_forge/divvy_reader.py

.PHONY: inspect-market-data
inspect-market-data:  ## Open MCP Inspector for market-data-fetcher in browser
	$(MCP) dev src/divvy_forge/market_data_fetcher.py

.PHONY: inspect-github-pr
inspect-github-pr:  ## Open MCP Inspector for github-pr-opener in browser
	$(MCP) dev src/divvy_forge/github_pr_opener.py

# ── TrueForge resource management ───────────────────────────────────────────
TF_URL ?= http://localhost:8790
TF_DB := $(HOME)/Library/Application Support/trueforge/db/db.sqlite

.PHONY: trueforge-local-up
trueforge-local-up: 
	source ~/.nvm/nvm.sh && nvm use 22 && source .env && npx @truefoundry/trueforge

.PHONY: agents-list
agents-list:  ## List all registered agents
	@curl -s $(TF_URL)/api/v1/agents | python3 -m json.tool

.PHONY: agents-delete
agents-delete:  ## Delete agent by name   NAME=my-agent make agents-delete
	@test -n "$(NAME)" || (echo "Usage: NAME=my-agent make agents-delete" && exit 1)
	$(eval AGENT_ID := $(shell curl -s $(TF_URL)/api/v1/agents | python3 -c "import sys,json; agents=json.load(sys.stdin)['data']; match=[a for a in agents if a['name']=='$(NAME)']; print(match[0]['id'] if match else '')"))
	@test -n "$(AGENT_ID)" || (echo "Agent '$(NAME)' not found" && exit 1)
	@curl -s -X DELETE $(TF_URL)/api/v1/agents/$(AGENT_ID) && echo "Deleted agent '$(NAME)' ($(AGENT_ID))"

.PHONY: connectors-delete
connectors-delete:  ## Delete a connector by name   NAME=divvy-reader make connectors-delete
	@test -n "$(NAME)" || (echo "Usage: NAME=divvy-reader make connectors-delete" && exit 1)
	@sqlite3 "$(TF_DB)" "DELETE FROM mcp_server WHERE name='$(NAME)'; SELECT CASE changes() WHEN 0 THEN 'Connector not found: $(NAME)' ELSE 'Deleted connector: $(NAME)' END;"

.PHONY: connectors-clean
connectors-clean:  ## Delete ALL registered connectors
	@sqlite3 "$(TF_DB)" "DELETE FROM mcp_server; SELECT changes() || ' connector(s) deleted';"

.PHONY: mcp-list
mcp-list:  ## List all registered MCP servers
	@curl -s $(TF_URL)/api/v1/settings/mcp-servers | python3 -m json.tool

.PHONY: providers-list
providers-list:  ## List all registered model providers
	@curl -s $(TF_URL)/api/v1/settings/model-providers | python3 -m json.tool

.PHONY: providers-add-model
providers-add-model:  ## Open TrueForge UI to add a model (no API — use the UI)
	@echo "TrueForge has no API for adding models to an existing provider."
	@echo "Open $(TF_URL) → Settings → Model Providers → openai → Add model."
	@echo "Then update config/coordinator_agent.yaml with the new model name and run: make deploy"

.PHONY: sessions-list
sessions-list:  ## List recent sessions
	@curl -s $(TF_URL)/api/v1/sessions | python3 -m json.tool

# ── Sandbox verification ─────────────────────────────────────────────────────
.PHONY: sandbox-verify
sandbox-verify:  ## Smoke-test Daytona sandbox (deploy minimal agent, run trivial script)
	$(PYTHON) scripts/verify_sandbox.py

# ── Deploy ───────────────────────────────────────────────────────────────────
.PHONY: deploy
deploy:  ## Register MCP servers and coordinator agent on the TrueForge instance
	$(PYTHON) deploy.py

# ── Help ─────────────────────────────────────────────────────────────────────
.PHONY: help
help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

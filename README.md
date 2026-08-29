# divvy-forge

An approval-gated dividend review agent built on [TrueForge](https://github.com/truefoundry/trueforge).
For each stock in your watchlist, it fetches fresh fundamentals, runs parallel analysis subagents in a
[Daytona](https://daytona.io) sandbox, and opens a GitHub PR with the proposed changes.
**Nothing is applied until you merge the PR.**

Built for the [WeMakeDevs Agent Harness Hackathon](https://wemakedevs.org/hackathons/trueforge) (Aug 2026).

Demo: [Watch on YouTube](https://youtu.be/H8XZ2pNBTug) · Blog series: [Part 1](https://hiteshpattanayak.com/posts/trueforge-daytona-setup-what-the-docs-miss/) · [Part 2](https://hiteshpattanayak.com/posts/building-mcp-tool-servers-for-a-dividend-review-agent-divvy-forge-part-2/) · [Part 3](https://hiteshpattanayak.com/posts/divvy-forge-coordinator-prompt-design-and-evals/) · [Part 4](https://hiteshpattanayak.com/posts/divvy-forge-end-to-end-deployment-and-pr-generation/)

---

## How it works

![divvy-forge architecture](config/prompts/../../../static/images/divvy-forge-e2e/architecture.svg)

1. `batch_runner.py` reads your watchlist from `HiteshRepo/stock-screeners` via the **divvy-reader** MCP tool
2. For each ticker it creates a TrueForge session and runs the **coordinator agent**
3. The coordinator spawns two parallel subagents:
   - **fundamentals-analysis** — yield trend, payout sustainability, suggested yield update (runs Python in a Daytona sandbox)
   - **dividend-cut-risk** — recent news search for cut/suspension signals
4. The coordinator merges both findings and generates a minimal unified diff
5. **github-pr-opener** MCP tool opens a PR on `HiteshRepo/stock-screeners` — one PR per ticker

---

## Prerequisites

- Python 3.11+
- Node.js 22.22+
- [TrueForge](https://github.com/truefoundry/trueforge) running locally (`npx @truefoundry/trueforge`)
- [Daytona](https://daytona.io) account + API key (sandbox provider for TrueForge)
- OpenAI API key with `gpt-4o` or `gpt-5-4-mini` configured in TrueForge
- GitHub fine-grained token scoped to `HiteshRepo/stock-screeners` with `contents: read+write` and `pull_requests: write`

See [docs/setup.md](docs/setup.md) for the full one-time setup walkthrough.

---

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
# Fill in: GITHUB_TOKEN, TRUEFORGE_BASE_URL, DAYTONA_API_KEY, OPENAI_API_KEY

# 3. Start TrueForge (separate terminal)
make trueforge-local-up

# 4. Start MCP servers as HTTP (three separate terminals)
make serve-divvy-reader-http      # :9001
make serve-market-data-http       # :9002
make serve-github-pr-http         # :9003

# 5. Register connectors and coordinator agent
make deploy

# 6. Run
TICKER=INFY make single           # single ticker
make batch                        # full watchlist
```

---

## Makefile targets

| Target | Description |
|--------|-------------|
| `make dev` | Create `.venv` and install dependencies |
| `make test` | Run the full test suite |
| `make test-cov` | Run tests with coverage report |
| `make lint` | Run ruff + mypy |
| `make deploy` | Register MCP servers and coordinator agent in TrueForge |
| `make demo-reset` | Clean TrueForge state for a fresh demo run |
| `TICKER=X make single` | Run a single-ticker review |
| `make batch` | Run full watchlist batch |
| `make serve-divvy-reader-http` | Start divvy-reader as HTTP/SSE server on :9001 |
| `make serve-market-data-http` | Start market-data-fetcher as HTTP/SSE server on :9002 |
| `make serve-github-pr-http` | Start github-pr-opener as HTTP/SSE server on :9003 |
| `make connectors-delete NAME=x` | Delete a TrueForge connector by name |
| `make connectors-clean` | Delete all TrueForge connectors |
| `make agents-delete NAME=x` | Delete a TrueForge agent by name |
| `make eval-fundamentals` | Run promptfoo evals for the fundamentals subagent prompt |
| `make eval-risk` | Run promptfoo evals for the risk subagent prompt |

---

## Agent modes

Set `AGENT_MODE` in `.env` before running `make deploy`:

| Mode | Description |
|------|-------------|
| `subagent` (default) | Coordinator spawns two parallel subagents via TrueForge's thread mechanism |
| `single` | Coordinator does all analysis inline — no subagents, works with any TrueForge version |

---

## Project structure

```
divvy-forge/
├── src/divvy_forge/
│   ├── batch_runner.py        # CLI entry point, state persistence
│   ├── coordinator.py         # TrueForge turn runner, coordinator-output parser
│   ├── coordinator_prompts.py # Prompt loader (subagent + single modes)
│   ├── divvy_reader.py        # MCP server: reads stock-screeners via GitHub API
│   ├── market_data_fetcher.py # MCP server: Screener.in + yfinance fundamentals
│   ├── github_pr_opener.py    # MCP server: branch, commit, PR creation
│   ├── trueforge_client.py    # TrueForge REST + SSE client
│   ├── github_auth.py         # Token scope validation
│   └── config.py              # Env var validation
├── config/
│   ├── coordinator_agent.yaml # Agent manifest (model, mcp_servers, config)
│   └── prompts/               # Coordinator + subagent prompt files
├── evals/                     # promptfoo eval configs and test cases
├── docs/
│   ├── setup.md               # One-time TrueForge + Daytona setup guide
│   └── trueforge-kb.md        # TrueForge API notes and gotchas
├── deploy.py                  # Registers MCP servers and agent in TrueForge
└── batch_state.json           # Per-ticker run state (auto-generated)
```

---

## Running a clean demo

To reset TrueForge state and record a fresh run from scratch:

```bash
# 1. Reset: remove connectors, agent, and batch state
make demo-reset

# 2. Register everything fresh
make deploy

# 3. Run single ticker
TICKER=INFY make single

# 4. Check the PR opened on HiteshRepo/stock-screeners/pulls
```

`make demo-reset` removes all registered connectors (via SQLite), deletes the coordinator agent, and clears `batch_state.json` — leaving TrueForge in a clean state as if deploy had never been run.

---

## Links

- [TrueForge](https://github.com/truefoundry/trueforge) — agent harness
- [Daytona](https://daytona.io) — sandbox provider
- [HiteshRepo/stock-screeners](https://github.com/HiteshRepo/stock-screeners) — the divvy portfolio this agent reads and proposes changes to
- [docs/setup.md](docs/setup.md) — full setup guide

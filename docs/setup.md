# divvy-forge Setup Guide

This document covers the one-time setup steps required before running divvy-forge.

---

## Prerequisites

- Python 3.11+
- Node.js 22.14+ (for TrueForge)
- A GitHub account with access to `HiteshRepo/stock-screeners`
- An OpenAI API key (for the `openai/gpt-4o` model used by the coordinator agent)

---

## 1. Install divvy-forge

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## 2. Configure environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Personal access token scoped to `HiteshRepo/stock-screeners` with `contents:read+write` and `pull_requests:write` |
| `TRUEFORGE_BASE_URL` | TrueForge instance URL (default: `http://localhost:8790`) |
| `DAYTONA_API_KEY` | Daytona API key for sandboxed code execution (see section 4) |

Optional variables:

| Variable | Description |
|---|---|
| `SCREENER_COOKIE` | Screener.in session cookie; if absent market-data-fetcher falls back to yfinance |
| `TRUEFORGE_API_KEY` | Only required if auth is explicitly enabled on the TrueForge instance |

---

## 3. GitHub Token Setup

1. Go to [GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens](https://github.com/settings/tokens?type=beta)
2. Click **Generate new token**
3. Set **Resource owner** to the account hosting `stock-screeners`
4. Under **Repository access**, select **Only select repositories** → choose `HiteshRepo/stock-screeners`
5. Under **Repository permissions**, grant:
   - **Contents**: Read and write
   - **Pull requests**: Read and write
6. Copy the token and set it as `GITHUB_TOKEN` in your `.env`

> **Note:** The token requires `contents:write` (not just `contents:read`) because the `github-pr-opener` tool creates feature branches via the GitHub API.

---

## 4. Daytona Sandbox Setup

TrueForge uses [Daytona](https://www.daytona.io/) as its sandbox provider for isolated code execution. The coordinator agent runs agent-generated Python analysis code inside Daytona sandboxes.

### 4.1 Create a Daytona account

1. Go to [https://app.daytona.io/](https://app.daytona.io/) and sign up (GitHub OAuth recommended)
2. Complete email verification
3. Select the **Free tier** (sufficient for divvy-forge: 4 vCPU, 8 GiB RAM, 10 GiB storage per sandbox)
4. Choose region **EU** (or closest to your TrueForge instance for lower cold-start latency)

### 4.2 Obtain your Daytona API key

1. In the Daytona dashboard, navigate to **Settings → API Keys**
2. Click **Create API Key**, name it `divvy-forge`
3. Copy the generated key immediately (it will not be shown again)
4. Set it as `DAYTONA_API_KEY` in your `.env`:
   ```
   DAYTONA_API_KEY=your-daytona-api-key-here
   ```

### 4.3 Configure Daytona in TrueForge

1. Start TrueForge locally (or open your hosted instance):
   ```bash
   source .env && npx @truefoundry/trueforge
   # Opens at http://localhost:8790
   ```
2. Navigate to **Settings → Sandbox providers**
3. Click **Add provider** → select **Daytona** preset
4. Paste your `DAYTONA_API_KEY` in the API key field
5. Click **Save**

TrueForge will now route all `config.sandbox.enabled: true` agent executions through Daytona.

### 4.4 Verify the sandbox is working

Run the sandbox smoke test:

```bash
make sandbox-verify
# or directly:
python scripts/verify_sandbox.py
```

This deploys a minimal agent with `config.sandbox.enabled: true` against your TrueForge instance, runs a trivial Python script (`print("sandbox ok")`), and confirms the output is received. Expected output:

```
[divvy-forge] Registering smoke-test agent...
[divvy-forge] Creating session...
[divvy-forge] Running turn...
[divvy-forge] Sandbox output: sandbox ok
[divvy-forge] Sandbox verification PASSED ✓
```

If the test fails with a Daytona authentication error, double-check that the API key is saved correctly in TrueForge (step 4.3) and that `TRUEFORGE_BASE_URL` in `.env` points to the correct instance.

---

## 5. Start TrueForge

With `OPENAI_API_KEY` in your `.env`, source it before starting TrueForge:

```bash
source .env && npx @truefoundry/trueforge
```

TrueForge runs on `http://localhost:8790` by default.

---

## 6. Deploy divvy-forge agents and MCP servers

Once TrueForge is running and all env vars are set:

```bash
python deploy.py
```

This script registers all three MCP tool servers (`divvy-reader`, `market-data-fetcher`, `github-pr-opener`) and the coordinator agent in the correct order. It is idempotent — safe to re-run.

---

## 7. Run a review

**Single ticker:**
```bash
make single TICKER=INFY
# or: python -m divvy_forge.batch_runner --ticker INFY
```

**Full watchlist batch:**
```bash
make batch
# or: python -m divvy_forge.batch_runner
```

Batch state is saved to `batch_state.json` after each ticker. If the run is interrupted, re-running `make batch` resumes from where it stopped — tickers already in `pr_opened` state are skipped.

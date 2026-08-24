# TrueForge Knowledge Base

Source: https://github.com/truefoundry/trueforge

---

## What TrueForge Is

TrueForge is an open-source, self-hosted **agent harness runtime** — a full-stack platform that turns an LLM into a working agent. It provides:

- A web chat UI (bundled)
- An HTTP REST API with a TypeScript SDK
- A React UI SDK for embedding

It is model-agnostic: supports OpenAI, Anthropic, Google Gemini, Fireworks, Together AI, and any OpenAI-compatible endpoint.

**Key distinction:** TrueForge is not a framework you import into your agent code. It is the runtime that *hosts* and *executes* your agents. Your code lives in MCP tool servers and sandbox scripts; the agent loop runs inside TrueForge.

---

## Installation & Setup

### Local (development)

```bash
npx @truefoundry/trueforge
# Runs on http://localhost:8790
# Uses SQLite by default, no external dependencies
```

Key env vars:
- `PORT` — custom port (default 8790)
- `SQLITE_PATH` — custom data location
- `PUBLIC_BASE_URL` — required if behind a proxy or domain (OAuth callbacks)

### Hosted (production)

Deploy via Docker Compose or Kubernetes with:
- PostgreSQL database
- Redis cache (for executor peering)
- Multiple server replicas

Key env vars:
- `NODE_ENV: production`
- `STANDALONE: false`
- `REDIS_URL: redis://redis:6379`
- `PUBLIC_BASE_URL`

**Runtime requirement:** Node.js 22.14+ on the server side.

---

## SDK

The official SDK is **TypeScript only** (`@truefoundry/trueforge-sdk`). There is no Python SDK.

To call TrueForge from Python, use `httpx` against the REST API directly. The API surface is small enough that a thin wrapper covers all needed operations.

### Key REST endpoints

```
POST   /api/v1/agents
GET    /api/v1/agents/{agentId}
POST   /api/v1/sessions
POST   /api/v1/sessions/{sessionId}/turns
GET    /api/v1/sessions/{sessionId}/turns
GET    /api/v1/sessions/{sessionId}/turns/{turnId}
DELETE /api/v1/sessions/{sessionId}/turns/{turnId}
```

Full spec: `/docs/openapi.json` in the repo.

---

## Agents

### Defining an Agent

Agents are created via the REST API with a **manifest** (YAML or JSON):

```yaml
model: openai/gpt-4o          # or anthropic/claude-3-5-sonnet, etc.
instructions: |
  You are a research assistant...
mcp_servers:
  - divvy-reader
  - market-data-fetcher
  - github-pr-opener
config:
  dynamic_sub_agents:
    enabled: true             # enables parallel subagent delegation
  sandbox:
    enabled: true             # enables sandboxed code execution
  approvals:
    - dangerous-tool-name     # tools that require human approval before execution
```

### Creating an Agent (TypeScript SDK)

```typescript
import { TrueForge } from '@truefoundry/trueforge-sdk';

const client = new TrueForge({ baseUrl: 'http://localhost:8790' });

const agent = await client.agents.create({
  name: 'my-agent',
  manifest: {
    model: 'openai/gpt-4o',
    instructions: '...',
    mcp_servers: ['my-mcp-server'],
    config: { dynamic_sub_agents: { enabled: true } }
  }
});
```

---

## MCP Tools

### Registering MCP Servers

1. **Settings → Connectors** in the UI, or `client.settings.mcpServers.create()` via SDK
2. Select from catalog or provide a custom URL
3. Set authentication: none, header-based (static API keys), or OAuth

MCP servers can be exposed as:
- `mcp+stdio:///path/to/server` — local subprocess
- HTTP URL — remote server

Authentication credentials are stored in the connector config, never in the agent manifest.

### Writing MCP Tools in Python

Use the `mcp` Python SDK. Each tool server is a standalone Python process registered by its stdio path.

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("divvy-reader")

@mcp.tool()
def read_file(path: str) -> str:
    """Read a file from HiteshRepo/stock-screeners via GitHub API."""
    ...

if __name__ == "__main__":
    mcp.run()
```

Register in TrueForge:
```
url: mcp+stdio:///path/to/venv/bin/python /path/to/divvy_reader.py
```

---

## Sandboxed Code Execution

TrueForge supports on-demand sandboxed execution via the **Daytona** provider (currently the only supported provider).

### Setup

1. **Settings → Sandbox providers** in the UI
2. Select Daytona preset
3. Add your Daytona API key

### Daytona Account Details (divvy-forge)

- **Plan**: Tier 1 (free)
- **Region**: EU
- **Per-sandbox limits**: 4 vCPU, 8 GiB RAM, 10 GiB storage
- **Rate limits**: 600 sandbox creations/min, 50,000 lifecycle requests/min
- **GPU**: locked on free tier (not needed — pure Python analysis)
- **Billing**: pay-as-you-go after $200 free credits; on-demand spin-up + auto-shutdown means cost for this project is negligible

### Enabling per Agent

```yaml
config:
  sandbox:
    enabled: true
```

### How It Works

- The sandbox is **not** the agent runtime — it spins up only when the agent needs to execute code
- Isolation: code, files, and shell commands run isolated; credentials and model logic stay on the TrueForge server
- Files in the sandbox **persist between turns within a session**
- Sandbox shuts down automatically after an idle period (configurable)

### Code Mode

Agents can write Python scripts that run in the sandbox and call MCP tools internally:

```python
# Agent-generated code running in sandbox
from mcp import MCPClient

tools = MCPClient()
data = tools.fetch_fundamentals("INFY")
yield_trend = compute_trend(data["dividends"])
print({"yield_trend": yield_trend, "reasoning": "..."})
```

Benefits: single execution round-trip instead of multiple model turns; avoids context bloat from large tool responses.

---

## Parallel Subagent Delegation

### How It Works

- The **root agent** decides at runtime to delegate subtasks to subagents
- Multiple subagents run **concurrently**
- Only summaries are returned to the root agent (not full intermediate context)
- TrueForge handles spawning, execution, and result collection automatically

### Constraints

- **No nesting:** subagents cannot spawn further subagents — only the root agent delegates
- **No user interaction:** only the root agent communicates with the user
- **Shared environment:** all agents share the same MCP tools and sandbox

### Enabling

```yaml
config:
  dynamic_sub_agents:
    enabled: true   # true by default; set false to disable
```

### Event Stream (SSE)

```
thread.created   — subagent spawned
thread.done      — subagent finished
model.message    — nested message from subagent
```

---

## Sessions & Turns

### Model

```
Session
├── id
├── agentId
└── turns[]
    ├── id
    ├── userMessage
    ├── assistantMessage
    ├── toolCalls[]
    ├── events[]
    └── status: running | done | paused
```

### Lifecycle

```python
# Python equivalent using httpx

import httpx

BASE = "http://localhost:8790/api/v1"

# 1. Create session
session = httpx.post(f"{BASE}/sessions", json={"agentId": agent_id}).json()

# 2. Create turn
turn = httpx.post(
    f"{BASE}/sessions/{session['id']}/turns",
    json={"userMessage": "Review INFY"}
).json()

# 3. Stream events (SSE)
with httpx.stream("GET", f"{BASE}/sessions/{session['id']}/turns/{turn['id']}/stream") as r:
    for line in r.iter_lines():
        handle_sse_event(line)
```

### Storage

- Local mode: SQLite
- Hosted mode: PostgreSQL (persistent across restarts and replicas)

---

## Human Approval Primitive

When an agent calls a tool marked for approval, TrueForge:

1. Emits a `tool.approval_required` SSE event
2. Pauses the conversation
3. Shows an approval prompt in the chat UI
4. Resumes or aborts based on the user's decision

### Configuration

```yaml
config:
  approvals:
    - github-pr-opener   # any tool name listed here requires approval
```

### SSE Event

```json
{
  "type": "tool.approval_required",
  "toolName": "github-pr-opener",
  "args": { "ticker": "INFY", "date": "2026-08-23" }
}
```

**Note for divvy-forge:** We use the PR-as-approval-gate pattern (agent opens PR and terminates; human merges). We do not rely on TrueForge's approval primitive for the main gate. The primitive could be used for any destructive intermediate action if needed.

---

## SSE Event Reference

All event types emitted by `GET /sessions/{sessionId}/turns/{turnId}/stream`:

| Event | Description |
|---|---|
| `model.message` | Full assistant message |
| `model.message.delta` | Streaming token |
| `tool.call` | Tool invoked by agent |
| `tool.response` | Tool result returned |
| `tool.approval_required` | Paused — waiting for human approval |
| `tool.response_required` | Client callback needed |
| `thread.created` | Subagent spawned |
| `thread.done` | Subagent finished |
| `mcp.auth_required` | OAuth needed for MCP connector |

---

## Context Management

TrueForge uses a 4-layer strategy to prevent context bloat:

1. **Subagents** — isolate heavy work, return only summaries
2. **Large response offloading** — tool responses over ~6k tokens are saved to sandbox files instead of added to context
3. **Code Mode** — process data in sandbox Python, print only the summary
4. **Context compaction** — replaces old history with structured summaries at a configurable token threshold (default ~50k)

### Tuning

```yaml
config:
  context_management:
    large_tool_response:
      per_call_tokens: 6000
      combined_tokens: 10000
```

---

## Packages (Monorepo)

| Package | Purpose |
|---|---|
| `@truefoundry/trueforge` | Main CLI & server (HTTP API + bundled UI) |
| `@truefoundry/trueforge-core` | Agent execution library (sessions, tool calls, sandbox) |
| `@truefoundry/trueforge-sdk` | TypeScript HTTP client |
| `@truefoundry/trueforge-ui` | Embeddable React chat component |

Monorepo tooling: `pnpm` workspaces.

---

## divvy-forge Specifics

### Tech decisions

- **Language**: Python throughout (MCP tools, batch runner, REST client wrapper)
- **TrueForge calls**: `httpx` against the REST API (no Python SDK exists)
- **MCP tools**: `mcp` Python SDK, registered as `mcp+stdio://` servers
- **Sandbox**: Daytona (requires API key at setup time)
- **Agent manifests**: YAML files applied via `trueforge_client.py` at deploy time

### Model provider

**Model used:** `openai/gpt-4o`

Only `OPENAI_API_KEY` is available for this project. Configure it on the TrueForge
server (not in divvy-forge's `.env`) under **Settings → Model providers → OpenAI**,
or pass it when starting TrueForge locally:

```bash
OPENAI_API_KEY=sk-... npx @truefoundry/trueforge
```

All agent manifests in divvy-forge MUST use `openai/gpt-4o` (or another OpenAI model).
Do NOT use Anthropic or other provider model strings.

### Required env vars

```
# On the TrueForge server
OPENAI_API_KEY       # model provider credentials — set in TrueForge, not in divvy-forge .env

# In divvy-forge .env
GITHUB_TOKEN         # scoped to HiteshRepo/stock-screeners (contents:read+write, pull_requests:write)
SCREENER_COOKIE      # session cookie for Screener.in (or omit if yfinance-only)
TRUEFORGE_API_KEY    # optional — only needed if auth is explicitly enabled; not required for local mode
DAYTONA_API_KEY      # for sandbox execution
```

### Subagent structure

```
proposal-coordinator  (root agent)
├── fundamentals-subagent     (parallel)
└── dividend-cut-risk-subagent (parallel)
```

No further nesting — matches TrueForge's one-level delegation constraint.

### Deploy order (hard dependency)

MCP servers MUST be registered before the coordinator agent. TrueForge
resolves `mcp_servers` names in the manifest against registered connectors
at agent-creation time. Creating the agent before its connectors exist will
result in broken tool references.

Correct order in `deploy.py`:

```
1. register_mcp_server("divvy-reader", ...)
2. register_mcp_server("market-data-fetcher", ...)
3. register_mcp_server("github-pr-opener", ...)
4. register_agent("coordinator", manifest)   # references the above by name
```

All four calls are idempotent — safe to re-run on every deploy.

---

### Approval gate

Agent opens GitHub PR → terminates. Human reviews and merges. No `tool.approval_required` used for the main gate — the PR merge IS the approval.

### Batch state log

Stored as a JSON file committed to `divvy-forge` (not in TrueForge session state, since sandbox files only persist within a session). On restart, load the file and skip tickers with `pr_opened` status.


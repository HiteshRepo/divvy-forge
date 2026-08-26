#!/usr/bin/env python3
"""MCP Protocol Demo

Shows the raw JSON-RPC 2.0 messages exchanged over stdio between a minimal
MCP client and one of the divvy-forge servers.

Three phases:
  1. INITIALIZE     — version + capability handshake
  2. TOOLS/LIST     — schema discovery (what the LLM sees)
  3. TOOLS/CALL     — real tool invocations with commentary

Usage::

    python scripts/protocol_demo.py [market-data|divvy-reader]

Or via the Makefile::

    make demo-protocol
    make demo-protocol SERVER=divvy-reader
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ── ANSI helpers ──────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
CYAN   = "\033[36m"


def _pretty(obj: dict) -> str:
    return json.dumps(obj, indent=2)


def _arrow(direction: str, msg: dict, color: str) -> None:
    label = f"{color}{BOLD}{direction}{RESET}"
    body  = textwrap.indent(_pretty(msg), "    ")
    print(f"\n{label}\n{body}", flush=True)


# ── MCPClient ─────────────────────────────────────────────────────────────────


class MCPClient:
    """Minimal synchronous stdio MCP client for demo / inspection purposes.

    Spawns a server subprocess, sends JSON-RPC messages to its stdin, and
    reads responses from its stdout.  All messages are printed to the terminal
    so the protocol exchange is fully visible.
    """

    def __init__(self, module: str) -> None:
        python = ROOT / ".venv" / "bin" / "python"
        self.proc = subprocess.Popen(
            [str(python), "-m", module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # FastMCP logs to stderr; hidden for clean output
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            cwd=ROOT,
        )
        self._q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    # ------------------------------------------------------------------
    # Internal

    def _pump(self) -> None:
        """Read stdout line by line into a queue so recv() can block cleanly."""
        assert self.proc.stdout
        for line in self.proc.stdout:
            self._q.put(line)
        self._q.put(None)  # sentinel — server closed

    # ------------------------------------------------------------------
    # Public

    def send(self, msg: dict) -> None:
        """Write *msg* as a single-line JSON object to the server's stdin."""
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        _arrow("→ CLIENT", msg, BLUE)

    def notify(self, msg: dict) -> None:
        """Send a JSON-RPC notification (no ``id`` → no response expected)."""
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        _arrow("→ CLIENT  (notification — no response expected)", msg, BLUE)

    def recv(self, request_id: int, timeout: float = 60.0) -> dict:
        """Block until the server returns the response matching *request_id*.

        Skips blank lines and non-JSON lines (e.g. startup noise).
        Raises :class:`TimeoutError` if no matching response arrives within
        *timeout* seconds.
        """
        import time

        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"No response for id={request_id} after {timeout}s")

            try:
                line = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            if line is None:
                raise RuntimeError("Server closed stdout unexpectedly")

            stripped = line.strip()
            if not stripped:
                continue

            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # skip non-JSON output

            if msg.get("id") == request_id:
                _arrow("← SERVER", msg, GREEN)
                return msg

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.terminate()
        self.proc.wait(timeout=5)


# ── Pretty-print helpers ──────────────────────────────────────────────────────


def phase(title: str) -> None:
    bar = "═" * 64
    print(f"\n\n{bar}")
    print(f"  {BOLD}{YELLOW}{title}{RESET}")
    print(f"{bar}")


def note(text: str) -> None:
    wrapped = textwrap.fill(text, width=62, initial_indent="  ", subsequent_indent="  ")
    print(f"\n{DIM}{wrapped}{RESET}")


# ── Demo runner ───────────────────────────────────────────────────────────────


def run(server_key: str) -> None:
    cfg    = SERVERS[server_key]
    client = MCPClient(cfg["module"])

    print(f"\n{BOLD}divvy-forge MCP Protocol Demo — {server_key}{RESET}")
    print(f"{DIM}Raw JSON-RPC 2.0 over stdio.  Ctrl-C to quit.{RESET}")

    try:
        # ── PHASE 1: INITIALIZE ───────────────────────────────────────────────

        phase("PHASE 1 — INITIALIZE")
        note(
            "Every MCP session opens with a capability handshake. "
            "The client announces the protocol version it speaks; the server "
            "responds with its name, version, and supported features. "
            "No tool calls can happen before this exchange completes."
        )

        client.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "protocol-demo", "version": "1.0"},
            },
        })
        resp = client.recv(1)

        info = resp.get("result", {}).get("serverInfo", {})
        print(
            f"\n  {CYAN}Server identified:{RESET}  "
            f"{info.get('name', '?')}  v{info.get('version', '?')}"
        )

        client.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        note(
            "This notification tells the server: 'I've read your capabilities — "
            "we're ready.' It has no id, so no response is expected or sent."
        )

        # ── PHASE 2: TOOLS/LIST ───────────────────────────────────────────────

        phase("PHASE 2 — TOOLS/LIST  (schema discovery)")
        note(
            "The LLM runtime calls tools/list to learn what tools exist and "
            "how to invoke them.  FastMCP generates these JSON Schemas "
            "automatically from your Python type annotations and docstrings — "
            "the LLM never sees Python source code, only these schemas."
        )

        client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = client.recv(2)

        tools = resp.get("result", {}).get("tools", [])
        print(f"\n  {CYAN}Tools advertised by this server:{RESET}")
        for t in tools:
            schema   = t.get("inputSchema", {})
            props    = list(schema.get("properties", {}).keys())
            required = schema.get("required", [])
            print(f"    {BOLD}• {t['name']}{RESET}")
            print(f"        params   → {props or '(none)'}")
            print(f"        required → {required or '(none)'}")

        # ── PHASE 3: TOOLS/CALL ───────────────────────────────────────────────

        phase("PHASE 3 — TOOLS/CALL  (invocations)")
        note(
            "Each call is a JSON-RPC request. The LLM constructs the "
            "'arguments' object using the schema it received in tools/list, "
            "then reads the response from the 'content' array back into its "
            "context window."
        )

        for i, spec in enumerate(cfg["calls"], start=3):
            label = spec.get("_label", f"Call {i - 2}")
            body  = spec.get("_note")
            call  = {k: v for k, v in spec.items() if not k.startswith("_")}

            dash_count = max(0, 50 - len(label))
            print(f"\n  {YELLOW}── {label} {'─' * dash_count}{RESET}")
            if body:
                note(body)

            client.send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": call})
            client.recv(i)

    finally:
        client.close()

    print(f"\n\n{BOLD}{GREEN}Done.{RESET}\n")


# ── Server configs ────────────────────────────────────────────────────────────

SERVERS: dict[str, dict] = {
    "market-data": {
        "module": "divvy_forge.market_data_fetcher",
        "calls": [
            {
                "_label": "Primary path — Screener.in",
                "_note": (
                    "INFY is indexed on Screener.in.  "
                    "Watch for source='screener.in' in the response."
                ),
                "name": "get_fundamentals",
                "arguments": {"ticker": "INFY"},
            },
            {
                "_label": "Fallback path — yfinance",
                "_note": (
                    "AAPL is a US ticker, not indexed on Screener.in.  "
                    "The server automatically retries with yfinance.  "
                    "The response schema is identical — only 'source' changes."
                ),
                "name": "get_fundamentals",
                "arguments": {"ticker": "AAPL"},
            },
            {
                "_label": "Error contract — unknown ticker",
                "_note": (
                    "Both sources fail for a nonsense ticker.  "
                    "Instead of raising an exception (which would crash the "
                    "agent), the tool returns a structured error dict.  "
                    "The LLM reads error_code and decides what to do next."
                ),
                "name": "get_fundamentals",
                "arguments": {"ticker": "NOTASTOCK"},
            },
        ],
    },
    "divvy-reader": {
        "module": "divvy_forge.divvy_reader",
        "calls": [
            {
                "_label": "list_watchlist — ordered tickers",
                "_note": (
                    "Fetches watchlist.md from GitHub via the contents API.  "
                    "No arguments required."
                ),
                "name": "list_watchlist",
                "arguments": {},
            },
            {
                "_label": "read_ticker — parsed state",
                "_note": (
                    "Returns structured fields from the markdown table row for INFY.  "
                    "This is what the coordinator diffs against live market data."
                ),
                "name": "read_ticker",
                "arguments": {"ticker": "INFY"},
            },
            {
                "_label": "read_file — raw escape hatch",
                "_note": (
                    "Returns the verbatim markdown file.  Useful when the agent "
                    "needs historical notes or context beyond the parsed fields."
                ),
                "name": "read_file",
                "arguments": {"path": "dividend/data/watchlist.md"},
            },
        ],
    },
}


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "market-data"
    if arg not in SERVERS:
        keys = " | ".join(SERVERS)
        print(f"Usage: python scripts/protocol_demo.py [{keys}]")
        sys.exit(1)
    run(arg)


if __name__ == "__main__":
    main()

"""Sandbox smoke test for divvy-forge.

Deploys a minimal agent with ``config.sandbox.enabled: true`` against the
configured TrueForge instance, runs a trivial Python script inside the
sandbox (``print("sandbox ok")``), and confirms the output is received.

Usage::

    python scripts/verify_sandbox.py

Or via the Makefile::

    make sandbox-verify

Exit codes:
    0  — sandbox verification passed
    1  — verification failed (env misconfiguration, TrueForge unreachable,
          Daytona auth error, or unexpected output)
"""

from __future__ import annotations

import sys
import time

# Allow running the script directly without installing the package.
sys.path.insert(0, "src")

from divvy_forge.config import validate_env
from divvy_forge.trueforge_client import ModelMessageEvent, TrueForgeClient

_SMOKE_AGENT_NAME = "divvy-forge-sandbox-smoke-test"

_SMOKE_MANIFEST = {
    "model": "openai/gpt-4o",
    "instructions": (
        "You are a sandbox smoke-test agent. "
        "When the user says 'run smoke test', write a Python script that prints exactly "
        "'sandbox ok' (no quotes), execute it in the sandbox, and reply with only the "
        "sandbox stdout output, nothing else."
    ),
    "config": {
        "sandbox": {"enabled": True},
        "dynamic_sub_agents": {"enabled": False},
    },
}

_SMOKE_MESSAGE = "run smoke test"
_EXPECTED_OUTPUT = "sandbox ok"
_POLL_INTERVAL_S = 2
_MAX_WAIT_S = 120


def _print(msg: str) -> None:
    print(f"[divvy-forge] {msg}", flush=True)


def main() -> int:
    env = validate_env()
    base_url = env["TRUEFORGE_BASE_URL"]
    api_key = env.get("TRUEFORGE_API_KEY")

    client = TrueForgeClient(base_url=base_url, api_key=api_key, timeout=60.0)

    # 1. Register smoke-test agent (idempotent).
    _print("Registering smoke-test agent...")
    try:
        agent = client.register_agent(_SMOKE_AGENT_NAME, _SMOKE_MANIFEST)
    except Exception as exc:
        _print(f"Failed to register agent: {exc}")
        return 1

    # 2. Create session.
    _print("Creating session...")
    try:
        session = client.create_session(agent.id)
    except Exception as exc:
        _print(f"Failed to create session: {exc}")
        return 1

    # 3. Submit turn.
    _print("Running turn...")
    try:
        turn = client.create_turn(session.id, _SMOKE_MESSAGE)
    except Exception as exc:
        _print(f"Failed to create turn: {exc}")
        return 1

    # 4. Stream events and collect the assistant's reply.
    assistant_reply: str | None = None
    deadline = time.monotonic() + _MAX_WAIT_S
    try:
        for event in client.stream_turn(session.id, turn.id):
            if isinstance(event, ModelMessageEvent):
                assistant_reply = event.content
            if time.monotonic() > deadline:
                _print(f"Timed out after {_MAX_WAIT_S}s waiting for sandbox output.")
                return 1
    except Exception as exc:
        _print(f"Error while streaming turn: {exc}")
        return 1

    if assistant_reply is None:
        _print("No assistant reply received.")
        return 1

    # 5. Validate output.
    _print(f"Sandbox output: {assistant_reply.strip()}")
    if _EXPECTED_OUTPUT in assistant_reply:
        _print("Sandbox verification PASSED ✓")
        return 0

    _print(
        f"Sandbox verification FAILED — expected '{_EXPECTED_OUTPUT}' "
        f"in output but got: {assistant_reply!r}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

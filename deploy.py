"""Deploy script for divvy-forge.

Registers MCP servers and the coordinator agent in TrueForge.
Safe to run multiple times — all registrations are idempotent.

Usage::

    python deploy.py

Environment variables (loaded from .env if present):
    TRUEFORGE_BASE_URL   Required. Base URL of the TrueForge instance.
    TRUEFORGE_API_KEY    Optional. Auth token for TrueForge (if enabled).

What this script does
---------------------
1. Reads ``config/coordinator_agent.yaml``.
2. Registers three MCP servers in TrueForge:
   - divvy-reader        → src/divvy_forge/divvy_reader.py (stdio)
   - market-data-fetcher → src/divvy_forge/market_data_fetcher.py (stdio)
   - github-pr-opener    → src/divvy_forge/github_pr_opener.py (stdio)
3. Loads the assembled coordinator system prompt via
   ``divvy_forge.coordinator_prompts.COORDINATOR_SYSTEM_PROMPT``.
4. Builds the TrueForge agent manifest dict from the YAML plus the prompt.
5. Registers the coordinator agent as ``dividend-review-coordinator``.

Exit codes
----------
0 — success
1 — missing environment variable or registration error
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Repo root — all paths are resolved relative to this script's location so
# deploy.py works correctly regardless of the working directory.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.resolve()

# ---------------------------------------------------------------------------
# MCP server definitions
# Default ports match the PORT env vars read by each server module.
# ---------------------------------------------------------------------------

# Each entry: (registered_name, description, default_url_env_var, default_url)
_MCP_SERVER_DEFS: list[tuple[str, str, str, str]] = [
    ("divvy-reader",        "Reads divvy markdown files from HiteshRepo/stock-screeners",   "DIVVY_READER_URL",        "http://localhost:9001/sse"),
    ("market-data-fetcher", "Fetches dividend fundamentals from Screener.in / yfinance",     "MARKET_DATA_URL",         "http://localhost:9002/sse"),
    ("github-pr-opener",    "Opens GitHub PRs on HiteshRepo/stock-screeners",                "GITHUB_PR_OPENER_URL",    "http://localhost:9003/sse"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml_manifest() -> dict:
    """Read ``config/coordinator_agent.yaml`` and return the parsed dict."""
    manifest_path = _REPO_ROOT / "config" / "coordinator_agent.yaml"
    with manifest_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_system_prompt(mode: str) -> str:
    """Return the fully assembled coordinator system prompt for *mode*.

    Uses ``divvy_forge.coordinator_prompts.get_prompt_for_mode`` so that all
    <<SECTION>> markers are substituted from the individual prompt files.

    Parameters
    ----------
    mode:
        ``"subagent"`` or ``"single"``.
    """
    # Add src/ to the import path in case the package is not installed.
    src_dir = str(_REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from divvy_forge.coordinator_prompts import get_prompt_for_mode  # noqa: PLC0415

    return get_prompt_for_mode(mode)


def _build_agent_manifest(yaml_data: dict, system_prompt: str) -> dict:
    """Construct the TrueForge agent manifest dict from YAML + assembled prompt.

    TrueForge API shape:
    - ``model``:       ``{"name": "<model-id>"}``
    - ``mcp_servers``: ``[{"name": "<server-name>"}, ...]``
    - ``instructions``: plain string
    - ``config``:      object (passed through as-is from YAML)
    """
    # model is stored as a string in YAML; API expects {"name": "..."}
    model_value = yaml_data["model"]
    model = model_value if isinstance(model_value, dict) else {"name": model_value}

    # mcp_servers is stored as a list of strings in YAML; API expects [{"name": "..."}, ...]
    raw_servers = yaml_data.get("mcp_servers", [])
    mcp_servers = [
        s if isinstance(s, dict) else {"name": s}
        for s in raw_servers
    ]

    manifest: dict = {
        "model": model,
        "instructions": system_prompt,
        "mcp_servers": mcp_servers,
    }

    if "config" in yaml_data:
        manifest["config"] = yaml_data["config"]

    return manifest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def deploy() -> None:
    """Register MCP servers and the coordinator agent in TrueForge."""
    load_dotenv(override=False)

    base_url = os.environ.get("TRUEFORGE_BASE_URL", "").strip()
    if not base_url:
        print(
            "[deploy] ERROR: TRUEFORGE_BASE_URL is not set.\n"
            "Set it in .env or export it before running deploy.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    api_key = os.environ.get("TRUEFORGE_API_KEY") or None

    # Import here so the error is actionable if the package is not installed.
    from divvy_forge.trueforge_client import TrueForgeClient  # noqa: PLC0415

    client = TrueForgeClient(base_url=base_url, api_key=api_key)

    # ------------------------------------------------------------------
    # Step 1 — Register MCP servers
    # ------------------------------------------------------------------
    print("[deploy] Registering MCP servers...")
    for server_name, description, url_env, default_url in _MCP_SERVER_DEFS:
        url = os.environ.get(url_env, default_url)
        print(f"  [{server_name}] {url}")
        try:
            client.register_mcp_server(name=server_name, url=url, description=description)
            print(f"  [{server_name}] OK")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{server_name}] ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2 — Load and assemble the coordinator manifest
    # ------------------------------------------------------------------
    mode = os.environ.get("AGENT_MODE", "subagent").strip().lower()
    print(f"[deploy] Agent mode: {mode}")
    print("[deploy] Loading coordinator agent manifest...")
    yaml_data = _load_yaml_manifest()
    system_prompt = _load_system_prompt(mode)
    manifest = _build_agent_manifest(yaml_data, system_prompt)

    agent_name: str = yaml_data["name"]

    # ------------------------------------------------------------------
    # Step 3 — Register the coordinator agent (idempotent)
    # ------------------------------------------------------------------
    print(f"[deploy] Registering agent '{agent_name}'...")
    try:
        agent = client.register_agent(agent_name, manifest)
        print(f"[deploy] Agent registered: id={agent.id} name={agent.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[deploy] ERROR registering agent: {exc}", file=sys.stderr)
        sys.exit(1)

    print("[deploy] Done. TrueForge is ready to run divvy-forge.")


if __name__ == "__main__":
    deploy()

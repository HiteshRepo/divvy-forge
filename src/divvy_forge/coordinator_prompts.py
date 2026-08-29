"""Coordinator system prompt loader for divvy-forge.

Prompt text lives in ``config/prompts/`` as plain markdown files so they can
be edited without touching Python code.  This module reads those files at
import time and exposes the assembled constants that the rest of the codebase
uses.

Directory layout::

    config/prompts/
        fundamentals_subagent.md          — fundamentals subagent instructions (evals + subagent mode)
        risk_subagent.md                  — risk subagent instructions (evals + subagent mode)
        merge_findings.md                 — merge logic section (both modes)
        generate_diff.md                  — diff generation section (both modes)
        coordinator_system.md             — subagent-mode template (<<SECTION>> markers)
        coordinator_system_single.md      — single-agent-mode template (<<SECTION>> markers)

Agent modes
-----------
``AGENT_MODE=subagent`` (default)
    The coordinator spawns two parallel subagents (fundamentals + risk) via
    TrueForge's dynamic subagent mechanism.  Requires TrueForge support for
    ``config.dynamic_sub_agents.enabled``.

``AGENT_MODE=single``
    The coordinator performs the full analysis itself using MCP tools and the
    bash sandbox.  Risk assessment returns ``risk_level: "unknown"`` because no
    web search tool is available.  Works with any TrueForge version.

Usage::

    from divvy_forge.coordinator_prompts import COORDINATOR_SYSTEM_PROMPT
    from divvy_forge.coordinator_prompts import COORDINATOR_SYSTEM_PROMPT_SINGLE
    from divvy_forge.coordinator_prompts import get_prompt_for_mode
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Prompt directory
# ---------------------------------------------------------------------------

_PROMPTS_DIR: Path = Path(__file__).parent.parent.parent / "config" / "prompts"


def _load(filename: str) -> str:
    """Read *filename* from the prompts directory and return its contents."""
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Individual section constants (used by both modes and by promptfoo evals)
# ---------------------------------------------------------------------------

FUNDAMENTALS_SUBAGENT_INSTRUCTIONS: str = _load("fundamentals_subagent.md")
RISK_SUBAGENT_INSTRUCTIONS: str = _load("risk_subagent.md")
MERGE_FINDINGS_INSTRUCTIONS: str = _load("merge_findings.md")
GENERATE_DIFF_INSTRUCTIONS: str = _load("generate_diff.md")

# ---------------------------------------------------------------------------
# Subagent-mode coordinator prompt
# ---------------------------------------------------------------------------

_SUBAGENT_TEMPLATE: str = _load("coordinator_system.md")

COORDINATOR_SYSTEM_PROMPT: str = (
    _SUBAGENT_TEMPLATE
    .replace("<<FUNDAMENTALS_SUBAGENT_INSTRUCTIONS>>", FUNDAMENTALS_SUBAGENT_INSTRUCTIONS)
    .replace("<<RISK_SUBAGENT_INSTRUCTIONS>>", RISK_SUBAGENT_INSTRUCTIONS)
    .replace("<<MERGE_FINDINGS_INSTRUCTIONS>>", MERGE_FINDINGS_INSTRUCTIONS)
    .replace("<<GENERATE_DIFF_INSTRUCTIONS>>", GENERATE_DIFF_INSTRUCTIONS)
)

# ---------------------------------------------------------------------------
# Single-agent-mode coordinator prompt
# ---------------------------------------------------------------------------

_SINGLE_TEMPLATE: str = _load("coordinator_system_single.md")

COORDINATOR_SYSTEM_PROMPT_SINGLE: str = (
    _SINGLE_TEMPLATE
    .replace("<<MERGE_FINDINGS_INSTRUCTIONS>>", MERGE_FINDINGS_INSTRUCTIONS)
    .replace("<<GENERATE_DIFF_INSTRUCTIONS>>", GENERATE_DIFF_INSTRUCTIONS)
)

# ---------------------------------------------------------------------------
# Mode selector
# ---------------------------------------------------------------------------

_VALID_MODES = ("subagent", "single")


def get_prompt_for_mode(mode: str) -> str:
    """Return the coordinator system prompt for *mode*.

    Parameters
    ----------
    mode:
        ``"subagent"`` or ``"single"``.

    Raises
    ------
    ValueError
        If *mode* is not recognised.
    """
    if mode == "subagent":
        return COORDINATOR_SYSTEM_PROMPT
    if mode == "single":
        return COORDINATOR_SYSTEM_PROMPT_SINGLE
    raise ValueError(
        f"Unknown AGENT_MODE {mode!r}. Valid values: {_VALID_MODES}"
    )

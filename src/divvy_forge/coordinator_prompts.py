"""Coordinator system prompt loader for divvy-forge.

Prompt text lives in ``config/prompts/`` as plain markdown files so they can
be edited without touching Python code.  This module reads those files at
import time and exposes the assembled constants that the rest of the codebase
uses.

Directory layout::

    config/prompts/
        fundamentals_subagent.md   — instructions for the fundamentals subagent
        risk_subagent.md           — instructions for the dividend-cut-risk subagent
        merge_findings.md          — merge logic section
        generate_diff.md           — diff generation section
        coordinator_system.md      — outer template (uses <<SECTION>> markers)

The coordinator_system.md template uses ``<<NAME>>`` markers (not Python
format-string placeholders) so JSON examples with curly braces in the file
remain unambiguous and don't need escaping.

Usage::

    from divvy_forge.coordinator_prompts import COORDINATOR_SYSTEM_PROMPT
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Prompt directory — resolved relative to this file so the package works
# regardless of the working directory.
# ---------------------------------------------------------------------------

_PROMPTS_DIR: Path = Path(__file__).parent.parent.parent / "config" / "prompts"


def _load(filename: str) -> str:
    """Read *filename* from the prompts directory and return its contents."""
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Individual section constants (loaded from disk)
# ---------------------------------------------------------------------------

FUNDAMENTALS_SUBAGENT_INSTRUCTIONS: str = _load("fundamentals_subagent.md")
RISK_SUBAGENT_INSTRUCTIONS: str = _load("risk_subagent.md")
MERGE_FINDINGS_INSTRUCTIONS: str = _load("merge_findings.md")
GENERATE_DIFF_INSTRUCTIONS: str = _load("generate_diff.md")

# ---------------------------------------------------------------------------
# Assembled coordinator system prompt
# ---------------------------------------------------------------------------

_COORDINATOR_TEMPLATE: str = _load("coordinator_system.md")

COORDINATOR_SYSTEM_PROMPT: str = (
    _COORDINATOR_TEMPLATE
    .replace("<<FUNDAMENTALS_SUBAGENT_INSTRUCTIONS>>", FUNDAMENTALS_SUBAGENT_INSTRUCTIONS)
    .replace("<<RISK_SUBAGENT_INSTRUCTIONS>>", RISK_SUBAGENT_INSTRUCTIONS)
    .replace("<<MERGE_FINDINGS_INSTRUCTIONS>>", MERGE_FINDINGS_INSTRUCTIONS)
    .replace("<<GENERATE_DIFF_INSTRUCTIONS>>", GENERATE_DIFF_INSTRUCTIONS)
)

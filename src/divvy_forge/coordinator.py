"""Coordinator runner for divvy-forge.

Orchestrates a single-ticker review turn against the TrueForge coordinator
agent.  Streams SSE events, tracks subagent threads, and parses the final
``coordinator-output`` JSON block from the assistant's response.

Typical usage::

    from divvy_forge.trueforge_client import TrueForgeClient
    from divvy_forge.coordinator import run_coordinator_turn, CoordinatorResult

    client = TrueForgeClient(base_url="http://localhost:8790")
    result = run_coordinator_turn(client, session_id="sess-123", ticker="INFY")
    print(result.status, result.diff)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from divvy_forge.trueforge_client import (
    ModelMessageEvent,
    ThreadCreatedEvent,
    ThreadDoneEvent,
    TrueForgeClient,
    TurnDoneEvent,
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FundamentalsFindings:
    """Output of the fundamentals-analysis subagent."""

    status: str  # "ok" | "error"
    yield_trend: str | None = None  # "improving" | "stable" | "deteriorating"
    payout_sustainability: str | None = None  # "safe" | "watch" | "at_risk"
    suggested_yield_update: float | None = None
    reasoning: str | None = None
    error_message: str | None = None
    failed_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FundamentalsFindings":
        return cls(
            status=data.get("status", "error"),
            yield_trend=data.get("yield_trend"),
            payout_sustainability=data.get("payout_sustainability"),
            suggested_yield_update=data.get("suggested_yield_update"),
            reasoning=data.get("reasoning"),
            error_message=data.get("error_message"),
            failed_code=data.get("failed_code"),
        )


@dataclass
class RiskSource:
    """A cited source for a dividend-cut-risk signal."""

    title: str
    url: str


@dataclass
class RiskAssessment:
    """Output of the dividend-cut-risk subagent."""

    risk_level: str  # "low" | "medium" | "high" | "unknown"
    signals: list[str] = field(default_factory=list)
    sources: list[RiskSource] = field(default_factory=list)
    reasoning: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskAssessment":
        return cls(
            risk_level=data.get("risk_level", "unknown"),
            signals=data.get("signals", []),
            sources=[
                RiskSource(title=s["title"], url=s["url"])
                for s in data.get("sources", [])
                if isinstance(s, dict)
            ],
            reasoning=data.get("reasoning", ""),
        )


@dataclass
class CoordinatorResult:
    """Parsed output of a completed coordinator turn."""

    ticker: str
    status: str  # "ok" | "error" | "ticker_not_found"
    merge_reasoning: str
    fundamentals: FundamentalsFindings | None
    risk: RiskAssessment | None
    error_detail: str | None
    diff: str
    diff_generated: bool
    diff_empty_reason: str | None
    changed_fields: list[str]
    review_date: str
    # Raw SSE metadata
    threads_created: list[str] = field(default_factory=list)
    threads_done: list[str] = field(default_factory=list)
    raw_output: str = ""


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

_COORDINATOR_OUTPUT_RE = re.compile(
    r"```coordinator-output\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def _parse_coordinator_output(text: str) -> dict[str, Any]:
    """Extract and parse the ``coordinator-output`` JSON block from *text*.

    Returns the raw dict on success.

    Raises
    ------
    ValueError
        If no ``coordinator-output`` block is found or the JSON is malformed.
    """
    match = _COORDINATOR_OUTPUT_RE.search(text)
    if not match:
        raise ValueError(
            "No ```coordinator-output``` block found in assistant response. "
            f"Response preview: {text[:200]!r}"
        )
    raw_json = match.group(1)
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"coordinator-output block contains invalid JSON: {exc}") from exc


def _build_result(data: dict[str, Any], raw_output: str, threads_created: list[str], threads_done: list[str]) -> CoordinatorResult:
    fundamentals_data = data.get("fundamentals")
    risk_data = data.get("risk")
    return CoordinatorResult(
        ticker=data.get("ticker", ""),
        status=data.get("status", "error"),
        merge_reasoning=data.get("merge_reasoning", ""),
        fundamentals=FundamentalsFindings.from_dict(fundamentals_data) if fundamentals_data else None,
        risk=RiskAssessment.from_dict(risk_data) if risk_data else None,
        error_detail=data.get("error_detail"),
        diff=data.get("diff", ""),
        diff_generated=data.get("diff_generated", False),
        diff_empty_reason=data.get("diff_empty_reason"),
        changed_fields=data.get("changed_fields", []),
        review_date=data.get("review_date", ""),
        threads_created=threads_created,
        threads_done=threads_done,
        raw_output=raw_output,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_USER_MESSAGE_TEMPLATE = "Review ticker: {ticker}"
_MAX_CONTINUATION_TURNS = 10


def _stream_one_turn(
    client: TrueForgeClient,
    session_id: str,
    message: str,
    threads_created: list[str],
    threads_done: list[str],
) -> tuple[str, bool]:
    """Stream a single turn and return (final_output, had_error).

    Appends thread IDs to the provided lists in-place.
    Returns the last model/turn output text and whether the turn errored.
    """
    output = ""
    had_error = False
    error_msg = ""

    for event in client.stream_turn(session_id, message):
        if isinstance(event, ThreadCreatedEvent):
            threads_created.append(event.thread_id)
        elif isinstance(event, ThreadDoneEvent):
            threads_done.append(event.thread_id)
        elif isinstance(event, ModelMessageEvent):
            output = event.content
        elif isinstance(event, TurnDoneEvent):
            if event.status == "error":
                had_error = True
                error_msg = event.error_message or "unknown error"
            elif event.output_content:
                output = event.output_content

    if had_error:
        raise RuntimeError(error_msg)
    return output, had_error


def run_coordinator_turn(
    client: TrueForgeClient,
    session_id: str,
    ticker: str,
) -> CoordinatorResult:
    """Stream coordinator turns for *ticker* until coordinator-output is produced.

    TrueForge may split the coordinator's work across multiple turns (e.g. when
    ask_user_questions fires or when the agent pauses mid-task).  This function
    keeps submitting continuation turns until it finds a coordinator-output block
    or exceeds ``_MAX_CONTINUATION_TURNS``.

    Parameters
    ----------
    client:
        Authenticated :class:`TrueForgeClient`.
    session_id:
        An active TrueForge session bound to the coordinator agent.
    ticker:
        Stock ticker to review (e.g. ``"INFY"``).

    Returns
    -------
    CoordinatorResult
        Parsed findings, diff, and metadata from the coordinator agent.

    Raises
    ------
    ValueError
        If no coordinator-output block is found after all continuation turns.
    RuntimeError
        If any TrueForge turn ends with status ``"error"``.
    """
    threads_created: list[str] = []
    threads_done: list[str] = []

    # First turn — submit the user request.
    message = _USER_MESSAGE_TEMPLATE.format(ticker=ticker)
    final_output, _ = _stream_one_turn(client, session_id, message, threads_created, threads_done)

    # Check if we already have the coordinator-output block.
    if _COORDINATOR_OUTPUT_RE.search(final_output):
        data = _parse_coordinator_output(final_output)
        return _build_result(data, final_output, threads_created, threads_done)

    # Continuation loop — TrueForge may need additional turns to finish.
    for _ in range(_MAX_CONTINUATION_TURNS - 1):
        continuation_output, _ = _stream_one_turn(
            client, session_id, "continue", threads_created, threads_done
        )
        if continuation_output:
            final_output = continuation_output

        if _COORDINATOR_OUTPUT_RE.search(final_output):
            data = _parse_coordinator_output(final_output)
            return _build_result(data, final_output, threads_created, threads_done)

    raise ValueError(
        f"Coordinator produced no coordinator-output block for ticker '{ticker}' "
        f"after {_MAX_CONTINUATION_TURNS} turns. Last output: {final_output[:200]!r}"
    )

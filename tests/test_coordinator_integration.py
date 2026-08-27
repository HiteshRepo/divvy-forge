"""Integration tests for the coordinator runner (tasks 7.4–7.6).

These tests mock TrueForgeClient.stream_turn to return pre-canned SSE event
sequences and verify that run_coordinator_turn correctly handles:

  - Both subagents succeed
  - One subagent fails (fundamentals)
  - One subagent fails (risk)
  - Both subagents fail
  - Conflicting signals (safe fundamentals, high risk)
  - Aligned signals (deteriorating fundamentals, high risk)
  - Coordinator turn-level error
  - Missing coordinator-output block
  - Ticker not found by market-data-fetcher
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from divvy_forge.coordinator import (
    CoordinatorResult,
    FundamentalsFindings,
    RiskAssessment,
    _parse_coordinator_output,
    run_coordinator_turn,
)
from divvy_forge.trueforge_client import (
    ThreadCreatedEvent,
    ThreadDoneEvent,
    TurnDoneEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fundamentals(
    status: str = "ok",
    yield_trend: str = "stable",
    payout_sustainability: str = "safe",
    suggested_yield_update: float = 3.5,
    reasoning: str = "Payout ratio is 42 %, FCF is positive.",
) -> dict[str, Any]:
    return {
        "status": status,
        "yield_trend": yield_trend,
        "payout_sustainability": payout_sustainability,
        "suggested_yield_update": suggested_yield_update,
        "reasoning": reasoning,
        "error_message": None,
        "failed_code": None,
    }


def _make_risk(
    risk_level: str = "low",
    signals: list[str] | None = None,
    sources: list[dict] | None = None,
    reasoning: str = "No adverse signals found in the 90-day window.",
) -> dict[str, Any]:
    return {
        "risk_level": risk_level,
        "signals": signals or [],
        "sources": sources or [],
        "reasoning": reasoning,
    }


_SENTINEL = object()  # distinct from None — signals "use default"


def _make_coordinator_output(
    ticker: str = "INFY",
    status: str = "ok",
    merge_reasoning: str = "Fundamentals stable; risk is low.",
    fundamentals: dict | None | object = _SENTINEL,
    risk: dict | None | object = _SENTINEL,
    error_detail: str | None = None,
    diff: str = "--- a/dividend/data/watchlist.md\n+++ b/dividend/data/watchlist.md\n@@ -5,7 +5,7 @@\n-| INFY | 3.2% |\n+| INFY | 3.5% |\n",
    diff_generated: bool = True,
    diff_empty_reason: str | None = None,
    changed_fields: list[str] | None = None,
    review_date: str = "2026-08-27",
) -> str:
    """Wrap a coordinator-output JSON block in the expected markdown fence."""
    payload: dict[str, Any] = {
        "ticker": ticker,
        "status": status,
        "merge_reasoning": merge_reasoning,
        "fundamentals": _make_fundamentals() if fundamentals is _SENTINEL else fundamentals,
        "risk": _make_risk() if risk is _SENTINEL else risk,
        "error_detail": error_detail,
        "diff": diff,
        "diff_generated": diff_generated,
        "diff_empty_reason": diff_empty_reason,
        "changed_fields": changed_fields if changed_fields is not None else ["Yield %"],
        "review_date": review_date,
    }
    return f"```coordinator-output\n{json.dumps(payload, indent=2)}\n```"


def _make_turn_done_event(content: str, status: str = "done") -> TurnDoneEvent:
    return TurnDoneEvent(status=status, output_content=content)


def _mock_stream(events: list) -> MagicMock:
    """Return a mock TrueForgeClient whose stream_turn yields *events*."""
    client = MagicMock()
    client.stream_turn.return_value = iter(events)
    return client


# ---------------------------------------------------------------------------
# _parse_coordinator_output unit tests
# ---------------------------------------------------------------------------

class TestParseCoordinatorOutput:
    def test_valid_block_is_parsed(self) -> None:
        text = _make_coordinator_output()
        data = _parse_coordinator_output(text)
        assert data["ticker"] == "INFY"
        assert data["status"] == "ok"

    def test_raises_when_block_is_missing(self) -> None:
        with pytest.raises(ValueError, match="No.*coordinator-output"):
            _parse_coordinator_output("Just some text with no block.")

    def test_raises_on_malformed_json(self) -> None:
        bad = "```coordinator-output\n{bad json}\n```"
        with pytest.raises(ValueError, match="invalid JSON"):
            _parse_coordinator_output(bad)

    def test_block_with_surrounding_text(self) -> None:
        preamble = "Here is my analysis.\n\n"
        text = preamble + _make_coordinator_output(ticker="TCS")
        data = _parse_coordinator_output(text)
        assert data["ticker"] == "TCS"


# ---------------------------------------------------------------------------
# run_coordinator_turn integration scenarios
# ---------------------------------------------------------------------------

class TestRunCoordinatorTurn:
    """Integration-level tests — TrueForgeClient.stream_turn is mocked."""

    def test_both_subagents_succeed(self) -> None:
        fund = _make_fundamentals()
        risk = _make_risk()
        output = _make_coordinator_output(
            ticker="INFY",
            status="ok",
            merge_reasoning="Payout 42 % is sustainable. Risk is low — routine update.",
            fundamentals=fund,
            risk=risk,
            changed_fields=["Yield %"],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-1"),
            ThreadCreatedEvent(thread_id="thread-risk-1"),
            ThreadDoneEvent(thread_id="thread-fund-1", summary="ok"),
            ThreadDoneEvent(thread_id="thread-risk-1", summary="ok"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result: CoordinatorResult = run_coordinator_turn(client, "sess-1", "INFY")

        assert result.status == "ok"
        assert result.ticker == "INFY"
        assert result.diff_generated is True
        assert result.fundamentals is not None
        assert result.fundamentals.status == "ok"
        assert result.fundamentals.payout_sustainability == "safe"
        assert result.risk is not None
        assert result.risk.risk_level == "low"
        assert len(result.threads_created) == 2
        assert len(result.threads_done) == 2
        assert "Yield %" in result.changed_fields

    def test_fundamentals_subagent_fails(self) -> None:
        failed_fund = {
            "status": "error",
            "yield_trend": None,
            "payout_sustainability": None,
            "suggested_yield_update": None,
            "reasoning": None,
            "error_message": "ZeroDivisionError: division by zero",
            "failed_code": "print(1 / 0)",
        }
        risk = _make_risk(risk_level="medium", signals=["earnings miss Q2 2026"])
        output = _make_coordinator_output(
            status="ok",
            merge_reasoning=(
                "Fundamentals subagent failed (ZeroDivisionError). "
                "Proceeding with risk assessment only — analysis is partial."
            ),
            fundamentals=failed_fund,
            risk=risk,
            diff="",
            diff_generated=False,
            diff_empty_reason="Fundamentals unavailable; diff not generated.",
            changed_fields=[],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-2"),
            ThreadCreatedEvent(thread_id="thread-risk-2"),
            ThreadDoneEvent(thread_id="thread-fund-2", summary="error"),
            ThreadDoneEvent(thread_id="thread-risk-2", summary="ok"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-2", "INFY")

        assert result.status == "ok"
        assert result.fundamentals is not None
        assert result.fundamentals.status == "error"
        assert result.fundamentals.error_message is not None
        assert result.risk is not None
        assert result.risk.risk_level == "medium"
        assert result.diff_generated is False
        assert result.diff_empty_reason is not None

    def test_risk_subagent_fails(self) -> None:
        fund = _make_fundamentals(
            yield_trend="improving",
            payout_sustainability="safe",
            suggested_yield_update=4.1,
        )
        failed_risk = {
            "risk_level": "unknown",
            "signals": [],
            "sources": [],
            "reasoning": "Search tool returned an error: HTTP 503.",
        }
        output = _make_coordinator_output(
            status="ok",
            merge_reasoning=(
                "Risk subagent could not complete search (503). "
                "Proceeding with fundamentals only. Risk status: unknown."
            ),
            fundamentals=fund,
            risk=failed_risk,
            changed_fields=["Yield %"],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-3"),
            ThreadCreatedEvent(thread_id="thread-risk-3"),
            ThreadDoneEvent(thread_id="thread-fund-3", summary="ok"),
            ThreadDoneEvent(thread_id="thread-risk-3", summary="unknown"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-3", "INFY")

        assert result.status == "ok"
        assert result.risk is not None
        assert result.risk.risk_level == "unknown"
        assert result.fundamentals is not None
        assert result.fundamentals.yield_trend == "improving"

    def test_both_subagents_fail(self) -> None:
        failed_fund = {
            "status": "error",
            "yield_trend": None,
            "payout_sustainability": None,
            "suggested_yield_update": None,
            "reasoning": None,
            "error_message": "Sandbox timeout",
            "failed_code": None,
        }
        failed_risk = {
            "risk_level": "unknown",
            "signals": [],
            "sources": [],
            "reasoning": "Search tool unavailable.",
        }
        output = _make_coordinator_output(
            status="error",
            merge_reasoning=(
                "Both subagents failed: fundamentals sandbox timed out and "
                "risk search tool was unavailable. No diff will be generated."
            ),
            fundamentals=failed_fund,
            risk=failed_risk,
            diff="",
            diff_generated=False,
            diff_empty_reason="Both subagents failed.",
            error_detail="Fundamentals: Sandbox timeout. Risk: Search tool unavailable.",
            changed_fields=[],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-4"),
            ThreadCreatedEvent(thread_id="thread-risk-4"),
            ThreadDoneEvent(thread_id="thread-fund-4", summary="error"),
            ThreadDoneEvent(thread_id="thread-risk-4", summary="unknown"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-4", "INFY")

        assert result.status == "error"
        assert result.diff_generated is False
        assert result.error_detail is not None
        assert result.fundamentals is not None
        assert result.fundamentals.status == "error"
        assert result.risk is not None
        assert result.risk.risk_level == "unknown"

    def test_conflicting_signals_safe_fundamentals_high_risk(self) -> None:
        """Strong fundamentals + high risk → watch flag in notes, not abort."""
        fund = _make_fundamentals(
            yield_trend="stable",
            payout_sustainability="safe",
            suggested_yield_update=3.5,
            reasoning=(
                "Payout ratio is 42 %, within safe bounds. "
                "FCF is positive at ₹12,400 Cr. Yield history stable over 5 periods."
            ),
        )
        risk = _make_risk(
            risk_level="high",
            signals=["dividend cut announced"],
            sources=[{"title": "INFY cuts dividend for Q1", "url": "https://example.com/infy-cut"}],
            reasoning="Management explicitly announced a 30 % dividend reduction.",
        )
        output = _make_coordinator_output(
            status="ok",
            merge_reasoning=(
                "Fundamentals appear safe (42 % payout, positive FCF) but a "
                "high-risk signal — explicit dividend cut announcement — was "
                "detected. Flagging for human review rather than recommending "
                "a yield update; the stored yield should be treated as stale."
            ),
            fundamentals=fund,
            risk=risk,
            diff=(
                "--- a/dividend/data/watchlist.md\n"
                "+++ b/dividend/data/watchlist.md\n"
                "@@ -5,7 +5,7 @@\n"
                "-| INFY | 3.2% | ... | ... | - |\n"
                "+| INFY | 3.5% | ... | ... | ⚠️ high cut-risk: dividend cut announced |\n"
            ),
            diff_generated=True,
            changed_fields=["Yield %", "Notes"],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-5"),
            ThreadCreatedEvent(thread_id="thread-risk-5"),
            ThreadDoneEvent(thread_id="thread-fund-5", summary="ok"),
            ThreadDoneEvent(thread_id="thread-risk-5", summary="ok"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-5", "INFY")

        assert result.status == "ok"
        assert result.risk is not None
        assert result.risk.risk_level == "high"
        assert result.fundamentals is not None
        assert result.fundamentals.payout_sustainability == "safe"
        # Merge reasoning must address the conflict
        assert "high" in result.merge_reasoning.lower()
        assert "safe" in result.merge_reasoning.lower() or "42 %" in result.merge_reasoning
        # Notes field should be in changed_fields
        assert "Notes" in result.changed_fields
        assert result.diff_generated is True

    def test_aligned_signals_deteriorating_fundamentals_high_risk(self) -> None:
        """Both subagents indicate deterioration → stronger action in reasoning."""
        fund = _make_fundamentals(
            yield_trend="deteriorating",
            payout_sustainability="at_risk",
            suggested_yield_update=1.8,
            reasoning=(
                "Payout ratio is 95 %, above safe threshold. "
                "FCF turned negative (−₹2,100 Cr) in the latest period. "
                "Dividend-per-share fell from 22 to 14 over 3 periods."
            ),
        )
        risk = _make_risk(
            risk_level="high",
            signals=["earnings miss Q3 2025", "dividend cut announced"],
            sources=[
                {"title": "XYZ misses Q3 earnings by 40 %", "url": "https://example.com/xyz-miss"},
                {"title": "XYZ slashes dividend", "url": "https://example.com/xyz-cut"},
            ],
            reasoning=(
                "Two high-risk signals: explicit dividend cut and a 40 % earnings miss. "
                "Both corroborate deteriorating FCF in fundamentals."
            ),
        )
        output = _make_coordinator_output(
            ticker="XYZ",
            status="ok",
            merge_reasoning=(
                "Both subagents indicate deterioration. Payout ratio 95 %, negative FCF, "
                "and an announced dividend cut all point the same direction. "
                "Recommending position-size reduction flag in notes."
            ),
            fundamentals=fund,
            risk=risk,
            changed_fields=["Yield %", "Notes"],
        )
        events = [
            ThreadCreatedEvent(thread_id="thread-fund-6"),
            ThreadCreatedEvent(thread_id="thread-risk-6"),
            ThreadDoneEvent(thread_id="thread-fund-6", summary="ok"),
            ThreadDoneEvent(thread_id="thread-risk-6", summary="ok"),
            _make_turn_done_event(output),
        ]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-6", "XYZ")

        assert result.status == "ok"
        assert result.fundamentals is not None
        assert result.fundamentals.yield_trend == "deteriorating"
        assert result.fundamentals.payout_sustainability == "at_risk"
        assert result.risk is not None
        assert result.risk.risk_level == "high"
        assert len(result.risk.signals) == 2
        assert len(result.risk.sources) == 2
        # Reasoning should be more direct / mention reduction
        assert "deteriorat" in result.merge_reasoning.lower() or "position" in result.merge_reasoning.lower()

    def test_coordinator_turn_error_raises_runtime_error(self) -> None:
        events = [
            TurnDoneEvent(status="error", output_content=None, error_message="Sandbox crashed"),
        ]
        client = _mock_stream(events)

        with pytest.raises(RuntimeError, match="Coordinator turn failed"):
            run_coordinator_turn(client, "sess-7", "INFY")

    def test_missing_coordinator_output_block_raises_value_error(self) -> None:
        events = [
            _make_turn_done_event("The analysis is complete but I forgot to add the block."),
        ]
        client = _mock_stream(events)

        with pytest.raises(ValueError, match="coordinator-output"):
            run_coordinator_turn(client, "sess-8", "INFY")

    def test_empty_output_raises_value_error(self) -> None:
        # TurnDoneEvent with no output_content and no ModelMessageEvent
        events = [
            TurnDoneEvent(status="done", output_content=None, error_message=None),
        ]
        client = _mock_stream(events)

        with pytest.raises(ValueError, match="no output"):
            run_coordinator_turn(client, "sess-9", "INFY")

    def test_ticker_not_found_status(self) -> None:
        output = _make_coordinator_output(
            ticker="BOGUS",
            status="ticker_not_found",
            merge_reasoning="",
            fundamentals=None,
            risk=None,
            diff="",
            diff_generated=False,
            diff_empty_reason="Ticker BOGUS not found by market-data-fetcher.",
            error_detail="TICKER_NOT_FOUND",
            changed_fields=[],
        )
        events = [_make_turn_done_event(output)]
        client = _mock_stream(events)

        result = run_coordinator_turn(client, "sess-10", "BOGUS")

        assert result.status == "ticker_not_found"
        assert result.fundamentals is None
        assert result.risk is None
        assert result.diff_generated is False


# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------

class TestFundamentalsFindings:
    def test_from_dict_full(self) -> None:
        data = _make_fundamentals(
            yield_trend="improving",
            payout_sustainability="watch",
            suggested_yield_update=4.2,
        )
        obj = FundamentalsFindings.from_dict(data)
        assert obj.yield_trend == "improving"
        assert obj.payout_sustainability == "watch"
        assert obj.suggested_yield_update == pytest.approx(4.2)
        assert obj.status == "ok"

    def test_from_dict_partial_data(self) -> None:
        data = {
            "status": "ok",
            "yield_trend": "stable",
            "payout_sustainability": None,
            "suggested_yield_update": 3.1,
            "reasoning": "payout_ratio was null; sustainability not computed.",
            "error_message": None,
            "failed_code": None,
        }
        obj = FundamentalsFindings.from_dict(data)
        assert obj.payout_sustainability is None
        assert obj.reasoning is not None

    def test_from_dict_error_status(self) -> None:
        data = {
            "status": "error",
            "yield_trend": None,
            "payout_sustainability": None,
            "suggested_yield_update": None,
            "reasoning": None,
            "error_message": "ZeroDivisionError",
            "failed_code": "x = 1/0",
        }
        obj = FundamentalsFindings.from_dict(data)
        assert obj.status == "error"
        assert obj.error_message == "ZeroDivisionError"
        assert obj.failed_code == "x = 1/0"


class TestRiskAssessment:
    def test_from_dict_low_risk(self) -> None:
        data = _make_risk()
        obj = RiskAssessment.from_dict(data)
        assert obj.risk_level == "low"
        assert obj.signals == []
        assert obj.sources == []

    def test_from_dict_high_risk_with_sources(self) -> None:
        data = _make_risk(
            risk_level="high",
            signals=["dividend cut announced"],
            sources=[{"title": "Company X cuts dividend", "url": "https://example.com/cut"}],
        )
        obj = RiskAssessment.from_dict(data)
        assert obj.risk_level == "high"
        assert len(obj.signals) == 1
        assert len(obj.sources) == 1
        assert obj.sources[0].url == "https://example.com/cut"

    def test_from_dict_unknown_risk(self) -> None:
        data = {
            "risk_level": "unknown",
            "signals": [],
            "sources": [],
            "reasoning": "Search tool returned HTTP 503.",
        }
        obj = RiskAssessment.from_dict(data)
        assert obj.risk_level == "unknown"

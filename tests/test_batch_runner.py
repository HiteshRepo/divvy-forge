"""Integration tests for batch_runner (task 9.7).

Scenarios covered:
- Full run: all tickers processed, state written, PRs opened
- Interrupted-then-resumed: pr_opened tickers skipped on second run, no duplicate PRs
- Single-ticker mode: only the named ticker is processed
- Token validation failure: process exits before any tickers are processed
- Coordinator error: ticker state set to 'error', runner continues to next ticker
- PR already exists: returns pr_opened with existing URL
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from divvy_forge.batch_runner import (
    BatchState,
    TickerEntry,
    _DEFAULT_STATE_PATH,
    _process_ticker,
    load_state,
    main,
    save_state,
)
from divvy_forge.coordinator import CoordinatorResult, FundamentalsFindings, RiskAssessment
from divvy_forge.github_pr_opener import MergedProposal, PrResult
from divvy_forge.trueforge_client import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator_result(ticker: str = "INFY") -> CoordinatorResult:
    return CoordinatorResult(
        ticker=ticker,
        status="ok",
        merge_reasoning="Yield is stable and payout ratio is healthy.",
        fundamentals=FundamentalsFindings(
            status="ok",
            yield_trend="stable",
            payout_sustainability="safe",
            suggested_yield_update=3.5,
            reasoning="FCF positive.",
        ),
        risk=RiskAssessment(
            risk_level="low",
            signals=[],
            sources=[],
            reasoning="No recent cut signals found.",
        ),
        error_detail=None,
        diff="--- a/dividend/data/watchlist.md\n+++ b/dividend/data/watchlist.md\n@@ -1,3 +1,3 @@\n-| INFY | 3.2% |\n+| INFY | 3.5% |\n",
        diff_generated=True,
        diff_empty_reason=None,
        changed_fields=["dividend_yield_pct"],
        review_date="2024-01-15",
    )


def _make_pr_result(already_exists: bool = False) -> PrResult:
    return PrResult(
        pr_url="https://github.com/HiteshRepo/stock-screeners/pull/42",
        pr_number=42,
        already_exists=already_exists,
        branch="divvy-review/INFY/2024-01-15",
    )


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_when_file_missing(tmp_path: Path) -> None:
    state = load_state(tmp_path / "nonexistent.json")
    assert state.tickers == {}


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = BatchState()
    state.tickers["INFY"] = TickerEntry(ticker="INFY", status="pr_opened", pr_url="https://example.com/pr/1")
    state.tickers["TCS"] = TickerEntry(ticker="TCS", status="error", error_message="something failed")

    save_state(state, path)
    loaded = load_state(path)

    assert loaded.tickers["INFY"].status == "pr_opened"
    assert loaded.tickers["INFY"].pr_url == "https://example.com/pr/1"
    assert loaded.tickers["TCS"].status == "error"
    assert loaded.tickers["TCS"].error_message == "something failed"


def test_save_uses_atomic_tmp_rename(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = BatchState()
    state.tickers["INFY"] = TickerEntry(ticker="INFY", status="pending")
    save_state(state, path)

    # Tmp file should be gone after save completes.
    assert not path.with_suffix(".json.tmp").exists()
    assert path.exists()


# ---------------------------------------------------------------------------
# _process_ticker
# ---------------------------------------------------------------------------


def test_process_ticker_success(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = BatchState()
    client = MagicMock()
    client.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.run_coordinator_turn", return_value=_make_coordinator_result()),
        patch("divvy_forge.batch_runner._open_pr", return_value=_make_pr_result()),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        _process_ticker("INFY", "2024-01-15", client, state, state_path)

    assert state.tickers["INFY"].status == "pr_opened"
    assert state.tickers["INFY"].pr_url == "https://github.com/HiteshRepo/stock-screeners/pull/42"

    # State should be persisted to disk.
    loaded = load_state(state_path)
    assert loaded.tickers["INFY"].status == "pr_opened"


def test_process_ticker_coordinator_error(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = BatchState()
    client = MagicMock()
    client.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    error_result = _make_coordinator_result()
    error_result.status = "error"
    error_result.error_detail = "subagents both failed"

    with patch("divvy_forge.batch_runner.run_coordinator_turn", return_value=error_result):
        _process_ticker("INFY", "2024-01-15", client, state, state_path)

    assert state.tickers["INFY"].status == "error"
    assert "subagents both failed" in (state.tickers["INFY"].error_message or "")


def test_process_ticker_coordinator_raises(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = BatchState()
    client = MagicMock()
    client.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with patch(
        "divvy_forge.batch_runner.run_coordinator_turn",
        side_effect=RuntimeError("TrueForge unavailable"),
    ):
        _process_ticker("INFY", "2024-01-15", client, state, state_path)

    assert state.tickers["INFY"].status == "error"
    assert "TrueForge unavailable" in (state.tickers["INFY"].error_message or "")


def test_process_ticker_already_existing_pr(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = BatchState()
    client = MagicMock()
    client.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.run_coordinator_turn", return_value=_make_coordinator_result()),
        patch("divvy_forge.batch_runner._open_pr", return_value=_make_pr_result(already_exists=True)),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        _process_ticker("INFY", "2024-01-15", client, state, state_path)

    # Already-existing PR still counts as pr_opened (no duplicate created).
    assert state.tickers["INFY"].status == "pr_opened"


# ---------------------------------------------------------------------------
# main() — full run
# ---------------------------------------------------------------------------

_ENV_VARS = {
    "GITHUB_TOKEN": "ghp_test",
    "TRUEFORGE_BASE_URL": "http://localhost:8790",
    "DAYTONA_API_KEY": "daytona-key",
}

_VALID_TOKEN_RESULT = MagicMock()
_VALID_TOKEN_RESULT.login = "test-user"


def test_main_full_run(tmp_path: Path) -> None:
    """Full watchlist run processes all tickers and writes state."""
    state_path = tmp_path / "state.json"

    client_mock = MagicMock()
    client_mock.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch("divvy_forge.batch_runner.validate_token_scopes", return_value=_VALID_TOKEN_RESULT),
        patch("divvy_forge.batch_runner.TrueForgeClient", return_value=client_mock),
        patch("divvy_forge.batch_runner._list_watchlist", return_value=["INFY", "TCS"]),
        patch("divvy_forge.batch_runner.run_coordinator_turn", side_effect=[
            _make_coordinator_result("INFY"),
            _make_coordinator_result("TCS"),
        ]),
        patch("divvy_forge.batch_runner._open_pr", return_value=_make_pr_result()),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        main(["--state-file", str(state_path)])

    loaded = load_state(state_path)
    assert loaded.tickers["INFY"].status == "pr_opened"
    assert loaded.tickers["TCS"].status == "pr_opened"


def test_main_interrupted_then_resumed(tmp_path: Path) -> None:
    """Tickers marked pr_opened are skipped on second run — no duplicate PRs."""
    state_path = tmp_path / "state.json"

    # Simulate a first run that processed INFY but crashed before TCS.
    initial_state = BatchState()
    initial_state.tickers["INFY"] = TickerEntry(
        ticker="INFY",
        status="pr_opened",
        pr_url="https://github.com/HiteshRepo/stock-screeners/pull/1",
    )
    initial_state.tickers["TCS"] = TickerEntry(ticker="TCS", status="pending")
    save_state(initial_state, state_path)

    open_pr_mock = MagicMock(return_value=_make_pr_result())
    client_mock = MagicMock()
    client_mock.create_session.return_value = Session(id="sess-2", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch("divvy_forge.batch_runner.validate_token_scopes", return_value=_VALID_TOKEN_RESULT),
        patch("divvy_forge.batch_runner.TrueForgeClient", return_value=client_mock),
        patch("divvy_forge.batch_runner._list_watchlist", return_value=["INFY", "TCS"]),
        patch("divvy_forge.batch_runner.run_coordinator_turn", return_value=_make_coordinator_result("TCS")),
        patch("divvy_forge.batch_runner._open_pr", open_pr_mock),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        main(["--state-file", str(state_path)])

    # _open_pr must only be called once (for TCS), not for INFY.
    assert open_pr_mock.call_count == 1

    loaded = load_state(state_path)
    assert loaded.tickers["INFY"].status == "pr_opened"
    assert loaded.tickers["TCS"].status == "pr_opened"


# ---------------------------------------------------------------------------
# main() — single-ticker mode (task 9.4)
# ---------------------------------------------------------------------------


def test_main_single_ticker_mode(tmp_path: Path) -> None:
    """--ticker flag processes only the specified ticker."""
    state_path = tmp_path / "state.json"
    open_pr_mock = MagicMock(return_value=_make_pr_result())
    client_mock = MagicMock()
    client_mock.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch("divvy_forge.batch_runner.validate_token_scopes", return_value=_VALID_TOKEN_RESULT),
        patch("divvy_forge.batch_runner.TrueForgeClient", return_value=client_mock),
        patch("divvy_forge.batch_runner._list_watchlist") as list_mock,
        patch("divvy_forge.batch_runner.run_coordinator_turn", return_value=_make_coordinator_result("INFY")),
        patch("divvy_forge.batch_runner._open_pr", open_pr_mock),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        main(["--ticker", "INFY", "--state-file", str(state_path)])

    # Watchlist must NOT be read in single-ticker mode.
    list_mock.assert_not_called()

    # Exactly one PR opened for INFY.
    assert open_pr_mock.call_count == 1

    loaded = load_state(state_path)
    assert loaded.tickers["INFY"].status == "pr_opened"


# ---------------------------------------------------------------------------
# main() — token validation failure (task 9.5)
# ---------------------------------------------------------------------------


def test_main_aborts_on_invalid_token(tmp_path: Path) -> None:
    """Runner exits before processing any tickers when token is invalid."""
    from divvy_forge.github_auth import InvalidTokenError

    state_path = tmp_path / "state.json"

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch(
            "divvy_forge.batch_runner.validate_token_scopes",
            side_effect=InvalidTokenError("Token expired"),
        ),
        patch("divvy_forge.batch_runner._list_watchlist") as list_mock,
        patch("divvy_forge.batch_runner.run_coordinator_turn") as coord_mock,
        pytest.raises(SystemExit) as exc_info,
    ):
        main(["--state-file", str(state_path)])

    assert exc_info.value.code == 1
    list_mock.assert_not_called()
    coord_mock.assert_not_called()


def test_main_aborts_on_insufficient_scope(tmp_path: Path) -> None:
    """Runner exits before processing any tickers when token lacks required scopes."""
    from divvy_forge.github_auth import InsufficientScopeError

    state_path = tmp_path / "state.json"

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch(
            "divvy_forge.batch_runner.validate_token_scopes",
            side_effect=InsufficientScopeError("Missing pull_requests scope"),
        ),
        patch("divvy_forge.batch_runner._list_watchlist") as list_mock,
        pytest.raises(SystemExit) as exc_info,
    ):
        main(["--state-file", str(state_path)])

    assert exc_info.value.code == 1
    list_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Crash safety: state persisted after each ticker (task 9.6)
# ---------------------------------------------------------------------------


def test_state_persisted_after_each_ticker(tmp_path: Path) -> None:
    """State file is written after processing each ticker, not just at the end."""
    state_path = tmp_path / "state.json"
    persist_snapshots: list[dict] = []

    original_save = save_state

    def _capturing_save(state: BatchState, path: Path | str) -> None:
        original_save(state, path)
        # Record a snapshot of ticker statuses at this save point.
        persist_snapshots.append(
            {k: v.status for k, v in state.tickers.items()}
        )

    client_mock = MagicMock()
    client_mock.create_session.return_value = Session(id="sess-1", agent_id="agent-1")

    with (
        patch("divvy_forge.batch_runner.validate_env", return_value=_ENV_VARS),
        patch("divvy_forge.batch_runner.validate_token_scopes", return_value=_VALID_TOKEN_RESULT),
        patch("divvy_forge.batch_runner.TrueForgeClient", return_value=client_mock),
        patch("divvy_forge.batch_runner._list_watchlist", return_value=["INFY", "TCS"]),
        patch("divvy_forge.batch_runner.save_state", side_effect=_capturing_save),
        patch("divvy_forge.batch_runner.run_coordinator_turn", side_effect=[
            _make_coordinator_result("INFY"),
            _make_coordinator_result("TCS"),
        ]),
        patch("divvy_forge.batch_runner._open_pr", return_value=_make_pr_result()),
        patch("divvy_forge.batch_runner.format_pr_body", return_value="PR body"),
    ):
        main(["--state-file", str(state_path)])

    # Save must have been called multiple times (at least once per ticker).
    assert len(persist_snapshots) >= 2

    # After the first ticker is processed, INFY should be pr_opened before TCS starts.
    infy_opened_early = any(
        s.get("INFY") == "pr_opened" and s.get("TCS") in (None, "pending", "in_progress")
        for s in persist_snapshots
    )
    assert infy_opened_early, (
        "Expected INFY to be persisted as pr_opened before TCS was processed. "
        f"Snapshots: {persist_snapshots}"
    )

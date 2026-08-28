"""Batch runner for divvy-forge dividend review agent.

Iterates the full watchlist from HiteshRepo/stock-screeners, runs the
coordinator agent for each ticker, and opens one GitHub PR per ticker.

Entry points
------------
Full watchlist run::

    python -m divvy_forge.batch_runner

Single-ticker run::

    python -m divvy_forge.batch_runner --ticker INFY

Batch state is persisted to ``batch_state.json`` after each ticker so
interrupted runs can resume without reprocessing completed tickers.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from divvy_forge.config import validate_env
from divvy_forge.coordinator import run_coordinator_turn
from divvy_forge.divvy_reader import DivvyReaderError, _list_watchlist
from divvy_forge.github_auth import (
    InsufficientScopeError,
    InvalidTokenError,
    validate_token_scopes,
)
from divvy_forge.github_pr_opener import (
    MergedProposal,
    PrOpenerError,
    _open_pr,
    format_pr_body,
)
from divvy_forge.trueforge_client import TrueForgeClient

_TARGET_REPO = "HiteshRepo/stock-screeners"
_COORDINATOR_AGENT_NAME = "dividend-review-coordinator"
_DEFAULT_STATE_PATH = Path("batch_state.json")

TickerStatus = Literal["pending", "in_progress", "pr_opened", "skipped", "error"]


# ---------------------------------------------------------------------------
# BatchState model  (task 9.1)
# ---------------------------------------------------------------------------


@dataclass
class TickerEntry:
    """Per-ticker state record."""

    ticker: str
    status: TickerStatus = "pending"
    pr_url: str | None = None
    error_message: str | None = None


@dataclass
class BatchState:
    """JSON-serializable state log for a batch run.

    Attributes
    ----------
    tickers:
        Dict mapping ticker symbol → :class:`TickerEntry`.
    run_date:
        ISO date string for the batch run (``YYYY-MM-DD``).
    """

    tickers: dict[str, TickerEntry] = field(default_factory=dict)
    run_date: str = field(default_factory=lambda: date.today().isoformat())


# ---------------------------------------------------------------------------
# State persistence  (task 9.2)
# ---------------------------------------------------------------------------


def load_state(path: str | Path) -> BatchState:
    """Load :class:`BatchState` from *path*.

    Returns a fresh empty state if the file does not exist.

    Parameters
    ----------
    path:
        File path for the JSON state log (e.g. ``"batch_state.json"``).

    Returns
    -------
    BatchState
        Loaded (or freshly initialised) batch state.
    """
    p = Path(path)
    if not p.exists():
        return BatchState()

    raw = json.loads(p.read_text(encoding="utf-8"))
    state = BatchState(run_date=raw.get("run_date", date.today().isoformat()))
    for ticker, entry_dict in raw.get("tickers", {}).items():
        state.tickers[ticker] = TickerEntry(
            ticker=entry_dict.get("ticker", ticker),
            status=entry_dict.get("status", "pending"),
            pr_url=entry_dict.get("pr_url"),
            error_message=entry_dict.get("error_message"),
        )
    return state


def save_state(state: BatchState, path: str | Path) -> None:
    """Persist *state* to *path* as JSON (atomic write via temp rename).

    Parameters
    ----------
    state:
        The current :class:`BatchState` to write.
    path:
        Destination file path.
    """
    p = Path(path)
    payload: dict = {
        "run_date": state.run_date,
        "tickers": {
            ticker: asdict(entry) for ticker, entry in state.tickers.items()
        },
    }
    # Atomic write: write to a sibling tmp file then rename so that a crash
    # during serialisation does not leave a partial state file.
    tmp_path = p.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(p)


# ---------------------------------------------------------------------------
# Startup gate  (tasks 9.5)
# ---------------------------------------------------------------------------


def _check_github_token(token: str) -> None:
    """Validate that *token* has the required scopes on the target repo.

    Aborts the process with a descriptive error message if validation fails.
    This gate runs before any tickers are processed so that a bad token cannot
    cause partial batch runs (some PRs opened, some not).
    """
    print(f"[divvy-forge] Validating GitHub token against {_TARGET_REPO}...")
    try:
        result = validate_token_scopes(token, _TARGET_REPO)
        print(f"[divvy-forge] Token OK — authenticated as {result.login}")
    except InvalidTokenError as exc:
        print(
            f"[divvy-forge] ERROR: GitHub token is invalid or expired.\n{exc}\n"
            "Generate a new fine-grained token at:\n"
            "  GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens\n"
            "Ensure it is scoped to HiteshRepo/stock-screeners with:\n"
            "  Contents: Read and write\n"
            "  Pull requests: Read and write",
            file=sys.stderr,
        )
        sys.exit(1)
    except InsufficientScopeError as exc:
        print(
            f"[divvy-forge] ERROR: GitHub token lacks required permissions.\n{exc}\n"
            "Grant 'Contents: Read and write' and 'Pull requests: Read and write'\n"
            "under Repository permissions for HiteshRepo/stock-screeners.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Per-ticker processing  (task 9.3)
# ---------------------------------------------------------------------------


def _process_ticker(
    ticker: str,
    run_date: str,
    client: TrueForgeClient,
    state: BatchState,
    state_path: Path,
) -> None:
    """Run the coordinator for *ticker*, open a PR, and update *state*.

    Marks the ticker ``in_progress`` before starting, then updates to either
    ``pr_opened`` or ``error``. State is saved to disk after each status change
    for crash safety (task 9.6).

    Parameters
    ----------
    ticker:
        Stock ticker to review.
    run_date:
        ISO date string for the PR title / branch name (``YYYY-MM-DD``).
    client:
        Authenticated :class:`TrueForgeClient`.
    state:
        Mutable :class:`BatchState` for the current run.
    state_path:
        Where to persist state after each update.
    """
    entry = state.tickers.setdefault(ticker, TickerEntry(ticker=ticker))
    entry.status = "in_progress"
    save_state(state, state_path)

    print(f"[divvy-forge] Processing {ticker}...")
    try:
        session = client.create_session(_COORDINATOR_AGENT_NAME)
        result = run_coordinator_turn(client, session.id, ticker)

        if result.status == "error":
            entry.status = "error"
            entry.error_message = result.error_detail or "coordinator returned error status"
            save_state(state, state_path)
            print(f"[divvy-forge] {ticker}: coordinator error — {entry.error_message}")
            return

        proposal = MergedProposal(
            ticker=result.ticker,
            date=run_date,
            merge_reasoning=result.merge_reasoning,
            fundamentals=_dataclass_to_dict(result.fundamentals),
            risk=_dataclass_to_dict(result.risk),
            changed_fields=result.changed_fields,
            diff=result.diff,
        )
        pr_body = format_pr_body(proposal)
        pr_result = _open_pr(result.ticker, run_date, proposal, pr_body)

        entry.status = "pr_opened"
        entry.pr_url = pr_result.pr_url
        save_state(state, state_path)

        action = "already existed" if pr_result.already_exists else "opened"
        print(f"[divvy-forge] {ticker}: PR {action} — {pr_result.pr_url}")

    except (RuntimeError, ValueError, DivvyReaderError, PrOpenerError) as exc:
        entry.status = "error"
        entry.error_message = str(exc)
        save_state(state, state_path)
        print(f"[divvy-forge] {ticker}: ERROR — {exc}", file=sys.stderr)

    except Exception as exc:  # noqa: BLE001
        entry.status = "error"
        entry.error_message = f"unexpected error: {exc}"
        save_state(state, state_path)
        print(f"[divvy-forge] {ticker}: UNEXPECTED ERROR — {exc}", file=sys.stderr)


def _dataclass_to_dict(obj: object) -> dict | None:
    """Convert a dataclass instance to a plain dict, or return None."""
    if obj is None:
        return None
    try:
        return asdict(obj)  # type: ignore[arg-type]
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# CLI  (task 9.4)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="divvy-forge batch-runner",
        description="Run dividend review agent for all watchlist tickers (or a single ticker).",
    )
    parser.add_argument(
        "--ticker",
        metavar="SYMBOL",
        default=None,
        help="Process a single ticker instead of the full watchlist.",
    )
    parser.add_argument(
        "--state-file",
        metavar="PATH",
        default=str(_DEFAULT_STATE_PATH),
        help="Path to the JSON batch state file (default: batch_state.json).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main  (task 9.3)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    state_path = Path(args.state_file)

    # Load and validate all required env vars (exits on missing vars).
    env = validate_env()

    # Startup gate (task 9.5): abort immediately if the token lacks required permissions.
    _check_github_token(env["GITHUB_TOKEN"])

    # Build TrueForge client.
    client = TrueForgeClient(
        base_url=env["TRUEFORGE_BASE_URL"],
        api_key=env.get("TRUEFORGE_API_KEY"),
    )

    run_date = date.today().isoformat()
    state = load_state(state_path)
    # Update run_date for this session (keeps the file fresh).
    state.run_date = run_date

    if args.ticker:
        # Single-ticker mode (task 9.4)
        ticker = args.ticker.strip().upper()
        print(f"[divvy-forge] Single-ticker mode: {ticker}")
        _process_ticker(ticker, run_date, client, state, state_path)

    else:
        # Batch mode: read watchlist, skip completed tickers (task 9.3)
        print("[divvy-forge] Batch mode: reading watchlist...")
        try:
            tickers = _list_watchlist()
        except DivvyReaderError as exc:
            print(
                f"[divvy-forge] ERROR: Could not read watchlist: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"[divvy-forge] Watchlist has {len(tickers)} tickers.")

        # Initialise state entries for new tickers.
        for ticker in tickers:
            state.tickers.setdefault(ticker, TickerEntry(ticker=ticker))

        # Filter out already-completed tickers.
        skip_statuses = {"pr_opened", "skipped"}
        pending = [
            t for t in tickers
            if state.tickers[t].status not in skip_statuses
        ]
        skipped_count = len(tickers) - len(pending)
        if skipped_count:
            print(f"[divvy-forge] Skipping {skipped_count} already-processed ticker(s).")

        print(f"[divvy-forge] Processing {len(pending)} ticker(s).")
        for ticker in pending:
            _process_ticker(ticker, run_date, client, state, state_path)

    # Final summary
    statuses = [e.status for e in state.tickers.values()]
    opened = statuses.count("pr_opened")
    errors = statuses.count("error")
    print(
        f"[divvy-forge] Done. PRs opened: {opened}, errors: {errors}."
    )


if __name__ == "__main__":
    main()

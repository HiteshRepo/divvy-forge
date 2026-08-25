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
import sys

from divvy_forge.config import validate_env
from divvy_forge.github_auth import (
    InsufficientScopeError,
    InvalidTokenError,
    validate_token_scopes,
)

_TARGET_REPO = "HiteshRepo/stock-screeners"


# ---------------------------------------------------------------------------
# Startup gate
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
# CLI
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
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Load and validate all required env vars (exits on missing vars).
    env = validate_env()

    # Startup gate: abort immediately if the token lacks required permissions.
    _check_github_token(env["GITHUB_TOKEN"])

    if args.ticker:
        print(f"[divvy-forge] Single-ticker mode: {args.ticker}")
        # TODO (task 9.3/9.4): run coordinator for args.ticker, open PR, update state
    else:
        print("[divvy-forge] Batch mode: processing full watchlist")
        # TODO (task 9.1–9.3): load BatchState, iterate watchlist, run coordinator per ticker


if __name__ == "__main__":
    main()

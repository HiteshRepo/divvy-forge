"""Startup environment validation for divvy-forge.

Call `validate_env()` at the entry point of any runnable script. It reads
from the process environment (populated by python-dotenv or the shell) and
exits with a descriptive error if required variables are absent.
"""

import os
import sys

from dotenv import load_dotenv

# Variables that MUST be present for any run to proceed.
_REQUIRED_VARS: list[tuple[str, str]] = [
    ("GITHUB_TOKEN", "GitHub personal-access token with contents:read+write and pull_requests:write on HiteshRepo/stock-screeners"),
    ("TRUEFORGE_BASE_URL", "Base URL of the TrueForge instance (e.g. http://localhost:8790)"),
    ("DAYTONA_API_KEY", "Daytona API key used by TrueForge's sandbox provider"),
]

# Variables that are optional — listed here for documentation only.
_OPTIONAL_VARS: list[tuple[str, str]] = [
    ("SCREENER_COOKIE", "Screener.in session cookie; if absent, market-data-fetcher falls back to yfinance"),
    ("TRUEFORGE_API_KEY", "TrueForge API key; only required when auth is enabled on the instance"),
]


def validate_env(*, load_dotenv_file: bool = True) -> dict[str, str]:
    """Load .env (if present) and assert all required variables are set.

    Returns a dict of all env-var values (required + optional, excluding absent
    optionals).

    Exits with code 1 and a human-readable message if any required variable is
    missing or empty.
    """
    if load_dotenv_file:
        load_dotenv(override=False)

    missing: list[str] = []
    for name, description in _REQUIRED_VARS:
        if not os.environ.get(name, "").strip():
            missing.append(f"  {name}: {description}")

    if missing:
        lines = ["[divvy-forge] Missing required environment variables:\n"] + missing + [
            "\nSet them in .env or export them before running. See .env.example for details."
        ]
        print("\n".join(lines), file=sys.stderr)
        sys.exit(1)

    return {name: os.environ[name] for name, _ in _REQUIRED_VARS if name in os.environ} | {
        name: os.environ[name] for name, _ in _OPTIONAL_VARS if name in os.environ
    }


def get_env(key: str, default: str | None = None) -> str | None:
    """Return an env variable value, or default if not set."""
    return os.environ.get(key, default)

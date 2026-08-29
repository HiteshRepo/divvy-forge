"""divvy-reader: MCP tool server for reading divvy markdown files from HiteshRepo/stock-screeners.

Exposes three tools over stdio MCP transport:
- read_file(path) -> str: raw file content fetched via GitHub contents API
- list_watchlist() -> list[str]: ordered ticker symbols from dividend/data/watchlist.md
- read_ticker(ticker) -> dict: parsed TickerState for a watchlist entry

Run as a standalone server::

    python -m divvy_forge.divvy_reader

or via the registered entry-point::

    divvy-reader
"""

import base64
import os
import re
from dataclasses import asdict, dataclass

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
REPO = "HiteshRepo/stock-screeners"
WATCHLIST_PATH = "dividend/data/watchlist.md"

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class DivvyReaderError(Exception):
    """Structured error raised by divvy-reader tool functions.

    Attributes
    ----------
    code:
        Machine-readable error code (e.g. ``"NOT_FOUND"``, ``"AUTH_ERROR"``).
    context:
        Additional keyword context (e.g. ``path=...``, ``ticker=...``).
    """

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TickerState:
    """Parsed state for a single ticker from the divvy watchlist.

    Fields that could not be parsed are returned as ``None`` rather than
    raising a hard error.
    """

    ticker: str
    yield_pct: float | None
    payout_ratio: float | None
    last_review_date: str | None
    notes: str | None
    raw_markdown: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_raw_file(path: str, http_client: httpx.Client | None = None) -> str:
    """Fetch raw content of *path* from :data:`REPO` via the GitHub contents API.

    Parameters
    ----------
    path:
        Repo-relative file path (e.g. ``"dividend/data/watchlist.md"``).
    http_client:
        Optional pre-configured :class:`httpx.Client` (useful for testing).

    Raises
    ------
    DivvyReaderError
        With ``code="NOT_FOUND"`` when the file does not exist (HTTP 404).
        With ``code="AUTH_ERROR"`` on HTTP 401 or 403.
    httpx.HTTPStatusError
        For unexpected non-401/403/404 HTTP errors.
    """

    def _do(client: httpx.Client) -> str:
        resp = client.get(
            f"{GITHUB_API}/repos/{REPO}/contents/{path}",
            headers=_github_headers(),
        )
        if resp.status_code == 401:
            raise DivvyReaderError(
                "AUTH_ERROR",
                "GitHub token is invalid, expired, or missing (HTTP 401). "
                "Set GITHUB_TOKEN with contents:read scope on HiteshRepo/stock-screeners.",
            )
        if resp.status_code == 403:
            raise DivvyReaderError(
                "AUTH_ERROR",
                f"GitHub token lacks required scope on '{REPO}' (HTTP 403). "
                "Grant 'Contents: Read and write' under Repository permissions.",
            )
        if resp.status_code == 404:
            raise DivvyReaderError(
                "NOT_FOUND",
                f"File not found in {REPO}: {path}",
                path=path,
            )
        resp.raise_for_status()

        data = resp.json()
        raw_content: str = data.get("content", "")
        encoding: str = data.get("encoding", "base64")
        if encoding == "base64":
            return base64.b64decode(raw_content).decode("utf-8")
        return raw_content

    if http_client is not None:
        return _do(http_client)

    with httpx.Client() as client:
        return _do(client)


def _parse_markdown_table(content: str) -> list[dict[str, str]]:
    """Parse a GitHub-flavored markdown pipe table into a list of row dicts.

    Ignores non-table lines and the header-separator row (``|---|---|``).
    Returns an empty list if no table is found.
    """
    header: list[str] = []
    rows: list[dict[str, str]] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped[1:-1].split("|")]
        if not cells:
            continue
        # Separator row — all cells are runs of dashes
        if all(re.match(r"^-+$", c) for c in cells if c):
            continue
        if not header:
            header = cells
        elif len(cells) == len(header):
            rows.append(dict(zip(header, cells)))

    return rows


def _parse_float_field(raw: str | None) -> float | None:
    """Parse a numeric string (possibly with a trailing ``%``) or return ``None``."""
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip().rstrip("%"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool logic (testable without MCP layer)
# ---------------------------------------------------------------------------


def _read_file(path: str, http_client: httpx.Client | None = None) -> str:
    """Return raw markdown content of *path* from :data:`REPO`.

    Raises :class:`DivvyReaderError` on NOT_FOUND or AUTH_ERROR.
    """
    return _fetch_raw_file(path, http_client=http_client)


def _list_watchlist(http_client: httpx.Client | None = None) -> list[str]:
    """Return ordered ticker symbols from :data:`WATCHLIST_PATH`.

    Raises :class:`DivvyReaderError` with ``code="WATCHLIST_NOT_FOUND"`` if
    the watchlist file is missing.
    """
    try:
        content = _fetch_raw_file(WATCHLIST_PATH, http_client=http_client)
    except DivvyReaderError as exc:
        if exc.code == "NOT_FOUND":
            raise DivvyReaderError(
                "WATCHLIST_NOT_FOUND",
                f"Watchlist file not found at {WATCHLIST_PATH}",
            ) from exc
        raise

    rows = _parse_markdown_table(content)
    return [row["Ticker"] for row in rows if row.get("Ticker", "").strip()]


def _read_ticker(ticker: str, http_client: httpx.Client | None = None) -> dict:
    """Return a :class:`TickerState`-shaped dict for *ticker* from the watchlist.

    Missing or unparseable fields are returned as ``None``.

    Raises :class:`DivvyReaderError` with ``code="NOT_FOUND"`` if *ticker* is
    not in the watchlist (or if the watchlist file is missing).
    """
    try:
        content = _fetch_raw_file(WATCHLIST_PATH, http_client=http_client)
    except DivvyReaderError as exc:
        if exc.code == "NOT_FOUND":
            raise DivvyReaderError(
                "WATCHLIST_NOT_FOUND",
                f"Watchlist file not found at {WATCHLIST_PATH}",
            ) from exc
        raise

    rows = _parse_markdown_table(content)
    ticker_upper = ticker.strip().upper()
    matched: dict[str, str] | None = None
    for row in rows:
        if row.get("Ticker", "").strip().upper() == ticker_upper:
            matched = row
            break

    if matched is None:
        raise DivvyReaderError(
            "NOT_FOUND",
            f"Ticker '{ticker}' not found in watchlist",
            ticker=ticker,
        )

    state = TickerState(
        ticker=matched.get("Ticker", ticker),
        yield_pct=_parse_float_field(matched.get("Yield %")),
        payout_ratio=_parse_float_field(matched.get("Payout Ratio %")),
        last_review_date=matched.get("Date Added") or None,
        notes=matched.get("Notes") or None,
        raw_markdown=content,
    )
    return asdict(state)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_PORT = int(os.environ.get("DIVVY_READER_PORT", "9001"))
mcp = FastMCP("divvy-reader", port=_PORT)


@mcp.tool()
def read_file(path: str) -> str:
    """Fetch raw file content from HiteshRepo/stock-screeners via the GitHub contents API.

    Parameters
    ----------
    path:
        Repository-relative path (e.g. ``"dividend/data/watchlist.md"``).
    """
    return _read_file(path)


@mcp.tool()
def list_watchlist() -> list[str]:
    """Return an ordered list of ticker symbols from the divvy watchlist."""
    return _list_watchlist()


@mcp.tool()
def read_ticker(ticker: str) -> dict:
    """Return parsed ticker state for *ticker* from the divvy watchlist.

    Returns a dict with keys: ``ticker``, ``yield_pct``, ``payout_ratio``,
    ``last_review_date``, ``notes``, ``raw_markdown``. Any field that cannot
    be parsed is ``null``.
    """
    return _read_ticker(ticker)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    transport = "sse" if "--sse" in sys.argv else "stdio"
    mcp.run(transport=transport)

"""market-data-fetcher: MCP tool server for fetching dividend fundamentals.

Tries Screener.in first; falls back to yfinance on any Screener failure.
Returns a unified ``FundamentalsData`` dict with ``source``, ``fetched_at``,
and ``raw_response_excerpt`` fields for traceability.

Run as a standalone MCP server::

    python -m divvy_forge.market_data_fetcher

or via the registered entry-point::

    market-data-fetcher
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from divvy_forge import screener_client, yfinance_client
from divvy_forge.screener_client import ScreenerError
from divvy_forge.yfinance_client import YFinanceError

load_dotenv()


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class MarketDataError(Exception):
    """Structured error raised by :func:`fetch_fundamentals`.

    Attributes
    ----------
    code:
        Machine-readable code — ``DATA_FETCH_FAILED``, ``TICKER_NOT_FOUND``.
    context:
        Additional keyword context (``ticker``, ``screener_error``,
        ``yfinance_error``).
    """

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def fetch_fundamentals(
    ticker: str,
    http_client: httpx.Client | None = None,
    yfin_module: Any = None,
) -> dict:
    """Fetch fundamentals for *ticker* — primary Screener.in, fallback yfinance.

    Parameters
    ----------
    ticker:
        NSE/BSE ticker symbol (e.g. ``"INFY"``).
    http_client:
        Optional :class:`httpx.Client` forwarded to :mod:`screener_client`
        (useful for testing).
    yfin_module:
        Optional injected yfinance-compatible module forwarded to
        :mod:`yfinance_client` (useful for testing).

    Returns
    -------
    dict
        ``FundamentalsData``-compatible dict with fields: ``ticker``,
        ``source``, ``fetched_at``, ``dividend_yield_pct``, ``payout_ratio``,
        ``dividends_per_share_history``, ``eps``, ``free_cash_flow``,
        ``raw_response_excerpt``.

    Raises
    ------
    MarketDataError
        ``TICKER_NOT_FOUND`` — both sources confirm the ticker is unknown.
        ``DATA_FETCH_FAILED`` — at least one source errored and the other
        also failed (even if for a different reason).
    """
    screener_err_code: str | None = None
    screener_err_msg: str | None = None

    # --- Primary: Screener.in ---
    try:
        return screener_client.fetch_fundamentals(ticker, http_client=http_client)
    except ScreenerError as exc:
        screener_err_code = exc.code
        screener_err_msg = str(exc)

    # --- Fallback: yfinance ---
    yfinance_err_msg: str | None = None
    try:
        return yfinance_client.fetch_fundamentals(ticker, yfin_module=yfin_module)
    except YFinanceError as exc:
        yfinance_err_msg = str(exc)
        if exc.code == "TICKER_NOT_FOUND":
            # yfinance is authoritative on ticker existence; if it says not found,
            # trust it regardless of why screener failed.
            raise MarketDataError(
                "TICKER_NOT_FOUND",
                f"Ticker '{ticker}' not found by Screener.in or yfinance.",
                ticker=ticker,
                screener_error=screener_err_msg,
                yfinance_error=yfinance_err_msg,
            ) from exc

    raise MarketDataError(
        "DATA_FETCH_FAILED",
        f"Both data sources failed for '{ticker}'. "
        f"Screener.in: {screener_err_msg}. yfinance: {yfinance_err_msg}.",
        ticker=ticker,
        screener_error=screener_err_msg,
        yfinance_error=yfinance_err_msg,
    )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_PORT = int(os.environ.get("MARKET_DATA_PORT", "9002"))
mcp = FastMCP("market-data-fetcher", port=_PORT)


@mcp.tool()
def get_fundamentals(ticker: str) -> dict:
    """Fetch current fundamental data for *ticker*.

    Tries Screener.in first; falls back to yfinance on failure.

    Returns a dict with keys: ``ticker``, ``source``, ``fetched_at``,
    ``dividend_yield_pct``, ``payout_ratio``, ``dividends_per_share_history``,
    ``eps``, ``free_cash_flow``, ``raw_response_excerpt``.

    On failure, returns ``{"error_code": ..., "error_message": ..., ...}``.
    """
    try:
        return fetch_fundamentals(ticker)
    except MarketDataError as exc:
        return {
            "error_code": exc.code,
            "error_message": str(exc),
            **exc.context,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    transport = "sse" if "--sse" in sys.argv else "stdio"
    mcp.run(transport=transport)

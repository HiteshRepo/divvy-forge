"""yfinance fallback client for fetching fundamental data.

Returns the same dict schema as :func:`screener_client.fetch_fundamentals`.
For Indian NSE tickers without an exchange suffix, appends ``.NS`` automatically.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

_RATE_LIMIT_PHRASES = ("too many requests", "rate limit", "rate limited")
_RETRY_DELAYS = (5, 15)  # seconds between retries on rate-limit


class YFinanceError(Exception):
    """Structured error raised by yfinance_client functions.

    Attributes
    ----------
    code:
        Machine-readable code — ``TICKER_NOT_FOUND``, ``DATA_FETCH_FAILED``.
    context:
        Additional keyword context (e.g. ``ticker=...``).
    """

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _nse_ticker(ticker: str) -> str:
    """Return *ticker* with ``.NS`` suffix for NSE if no exchange suffix is present."""
    return ticker if "." in ticker else f"{ticker}.NS"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_fundamentals(
    ticker: str,
    yfin_module: Any = None,
) -> dict:
    """Fetch fundamental data from yfinance.

    Parameters
    ----------
    ticker:
        Stock ticker symbol. ``.NS`` is appended automatically for bare NSE symbols.
    yfin_module:
        Optional injected yfinance-compatible module (default: the real
        :mod:`yfinance` module). Pass a mock in tests.

    Returns
    -------
    dict
        FundamentalsData-compatible dict with ``source="yfinance"``.

    Raises
    ------
    YFinanceError
        ``TICKER_NOT_FOUND`` — ticker not recognized by yfinance (empty info).
        ``DATA_FETCH_FAILED`` — unexpected error from yfinance internals.
    """
    _yf = yfin_module or yf
    yt = _nse_ticker(ticker)

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + list(_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            stock = _yf.Ticker(yt)
            info: dict = stock.info or {}
            break
        except Exception as exc:
            last_exc = exc
            if any(p in str(exc).lower() for p in _RATE_LIMIT_PHRASES):
                continue  # retry on rate-limit
            raise YFinanceError(
                "DATA_FETCH_FAILED",
                f"yfinance raised an exception for '{ticker}': {exc}",
                ticker=ticker,
            ) from exc
    else:
        raise YFinanceError(
            "DATA_FETCH_FAILED",
            f"yfinance raised an exception for '{ticker}': {last_exc}",
            ticker=ticker,
        ) from last_exc

    # yfinance returns a nearly empty dict for unknown tickers.
    if not info.get("symbol") and not info.get("shortName"):
        raise YFinanceError(
            "TICKER_NOT_FOUND",
            f"Ticker '{ticker}' (queried as '{yt}') not recognized by yfinance.",
            ticker=ticker,
        )

    fetched_at = datetime.now(timezone.utc).isoformat()

    # yfinance 1.x: dividendYield is already a percentage (e.g. 4.42 = 4.42%)
    # yfinance 1.x: payoutRatio is still a decimal (e.g. 0.646 = 64.6%)
    dividend_yield_pct = _safe_float(info.get("dividendYield"))

    raw_payout = _safe_float(info.get("payoutRatio"))
    payout_ratio = raw_payout * 100 if raw_payout is not None else None

    eps = _safe_float(info.get("trailingEps"))

    # --- DPS history (best-effort) ---
    dividends_per_share_history: list[dict] | None = None
    try:
        dividends = stock.dividends
        if dividends is not None and not dividends.empty:
            recent = dividends.tail(5)
            dividends_per_share_history = [
                {"period": str(idx.date()), "value": float(val)}
                for idx, val in recent.items()
            ]
    except Exception:
        pass

    # --- Free cash flow (best-effort) ---
    free_cash_flow: float | None = None
    try:
        cf = stock.cashflow
        if cf is not None and not cf.empty:
            if "Free Cash Flow" in cf.index:
                fcf_val = _safe_float(cf.loc["Free Cash Flow"].iloc[0])
                free_cash_flow = fcf_val
            elif "Operating Cash Flow" in cf.index:
                op_cf = _safe_float(cf.loc["Operating Cash Flow"].iloc[0])
                capex = 0.0
                if "Capital Expenditure" in cf.index:
                    capex = abs(_safe_float(cf.loc["Capital Expenditure"].iloc[0]) or 0.0)
                if op_cf is not None:
                    free_cash_flow = op_cf - capex
    except Exception:
        pass

    raw_response_excerpt = json.dumps({k: info.get(k) for k in list(info)[:20]})[:500]

    return {
        "ticker": ticker,
        "source": "yfinance",
        "fetched_at": fetched_at,
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio": payout_ratio,
        "dividends_per_share_history": dividends_per_share_history,
        "eps": eps,
        "free_cash_flow": free_cash_flow,
        "raw_response_excerpt": raw_response_excerpt,
    }

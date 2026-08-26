"""Screener.in REST client for fetching fundamental data.

Uses the Screener.in JSON API (https://www.screener.in/api/company/{ticker}/).
Requires ``SCREENER_COOKIE`` env var for authenticated access.

Implements exponential backoff (initial 1 s, max 3 retries) on HTTP 429 and 5xx.
"""

from __future__ import annotations

import os
import time

import httpx

SCREENER_BASE_URL = "https://www.screener.in"
_INITIAL_BACKOFF_SECS: float = 1.0
_MAX_RETRIES: int = 3


class ScreenerError(Exception):
    """Structured error raised by screener_client functions.

    Attributes
    ----------
    code:
        Machine-readable code — ``TICKER_NOT_FOUND``, ``AUTH_FAILED``,
        ``DATA_FETCH_FAILED``.
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


def _screener_headers() -> dict[str, str]:
    cookie = os.environ.get("SCREENER_COOKIE", "")
    return {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": "divvy-forge/0.1",
        "X-Requested-With": "XMLHttpRequest",
    }


def _parse_ratio_value(ratios: list[dict], *names: str) -> float | None:
    """Find the first matching ratio name in the Screener ratios list and parse its value."""
    name_set = {n.lower() for n in names}
    for ratio in ratios:
        if ratio.get("name", "").strip().lower() in name_set:
            raw = str(ratio.get("value", "")).strip().rstrip("%").replace(",", "")
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    return None
    return None


def _parse_row_values(rows: list[dict], *row_names: str) -> list[float | None]:
    """Extract numeric values from a financial-statement row by name (first match)."""
    name_set = {n.lower() for n in row_names}
    for row in rows:
        if row.get("name", "").strip().lower() in name_set:
            result: list[float | None] = []
            for v in row.get("values", []):
                raw = str(v).strip().replace(",", "")
                try:
                    result.append(float(raw))
                except (ValueError, TypeError):
                    result.append(None)
            return result
    return []


def _parse_response(ticker: str, data: dict, raw_text: str) -> dict:
    """Parse a Screener.in API JSON response into a FundamentalsData-compatible dict."""
    from datetime import datetime, timezone

    fetched_at = datetime.now(timezone.utc).isoformat()
    raw_response_excerpt = raw_text[:500]

    ratios: list[dict] = data.get("ratios", [])

    dividend_yield_pct = _parse_ratio_value(ratios, "Dividend Yield")
    eps = _parse_ratio_value(ratios, "EPS in Rs", "EPS")
    payout_ratio = _parse_ratio_value(ratios, "Payout ratio", "Payout Ratio")

    # --- DPS history and FCF from cash flow statement ---
    headers: list[str] = []
    rows: list[dict] = []
    for source_key in ("consolidated", "standalone"):
        cf = data.get(source_key, {}).get("cash_flows", {})
        if cf:
            headers = cf.get("headers", [])
            rows = cf.get("rows", [])
            break

    dividends_per_share_history: list[dict] | None = None
    if headers and rows:
        dps_values = _parse_row_values(rows, "Dividends Paid")
        if dps_values:
            # headers[0] is typically a blank label; periods start at index 1
            period_headers = headers[1:] if headers and headers[0] == "" else headers
            paired = list(zip(period_headers, dps_values))
            valid = [(p, v) for p, v in paired if v is not None][-5:]
            dividends_per_share_history = [{"period": p, "value": v} for p, v in valid] or None

    free_cash_flow: float | None = None
    op_cf_vals = _parse_row_values(rows, "Cash from Operating Activity")
    capex_vals = _parse_row_values(rows, "Capital Expenditure")
    if op_cf_vals and op_cf_vals[0] is not None:
        capex = abs(capex_vals[0]) if capex_vals and capex_vals[0] is not None else 0.0
        free_cash_flow = op_cf_vals[0] - capex

    return {
        "ticker": ticker,
        "source": "screener.in",
        "fetched_at": fetched_at,
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio": payout_ratio,
        "dividends_per_share_history": dividends_per_share_history,
        "eps": eps,
        "free_cash_flow": free_cash_flow,
        "raw_response_excerpt": raw_response_excerpt,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_fundamentals(
    ticker: str,
    http_client: httpx.Client | None = None,
) -> dict:
    if not os.environ.get("SCREENER_COOKIE", "").strip():
        raise ScreenerError(
            "AUTH_FAILED",
            "SCREENER_COOKIE is not set — skipping Screener.in.",
        )
    """Fetch fundamental data from Screener.in with exponential backoff.

    Retries on HTTP 429 and 5xx with an initial delay of 1 s, doubling each
    attempt, up to :data:`_MAX_RETRIES` retries.

    Parameters
    ----------
    ticker:
        NSE/BSE ticker symbol (e.g. ``"INFY"``).
    http_client:
        Optional pre-configured :class:`httpx.Client` (useful for testing).

    Returns
    -------
    dict
        FundamentalsData-compatible dict with ``source="screener.in"``.

    Raises
    ------
    ScreenerError
        ``TICKER_NOT_FOUND`` — HTTP 404 from Screener.in.
        ``AUTH_FAILED`` — HTTP 401 or 403.
        ``DATA_FETCH_FAILED`` — max retries exceeded.
    """
    url = f"{SCREENER_BASE_URL}/api/company/{ticker}/"

    def _do(client: httpx.Client) -> dict:
        last_error: Exception | None = None
        backoff = _INITIAL_BACKOFF_SECS

        for attempt in range(_MAX_RETRIES + 1):
            resp = client.get(url, headers=_screener_headers())

            if resp.status_code == 200:
                return _parse_response(ticker, resp.json(), resp.text)

            if resp.status_code == 404:
                raise ScreenerError(
                    "TICKER_NOT_FOUND",
                    f"Ticker '{ticker}' not found on Screener.in (HTTP 404).",
                    ticker=ticker,
                )

            if resp.status_code in (401, 403):
                raise ScreenerError(
                    "AUTH_FAILED",
                    f"Screener.in authentication failed (HTTP {resp.status_code}). "
                    "Check SCREENER_COOKIE in your .env file.",
                )

            # Retryable: 429 and 5xx
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                # Retries exhausted — break to raise DATA_FETCH_FAILED below.
                break

            # Non-retryable HTTP error (4xx other than 401/403/404)
            resp.raise_for_status()

        raise ScreenerError(
            "DATA_FETCH_FAILED",
            f"Screener.in failed for '{ticker}' after {_MAX_RETRIES} retries. "
            f"Last error: {last_error}",
            ticker=ticker,
        )

    if http_client is not None:
        return _do(http_client)
    with httpx.Client() as client:
        return _do(client)

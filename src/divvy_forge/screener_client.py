"""Screener.in HTML scraper for fetching fundamental data.

Two-step flow (no authentication required):
  1. ``GET /api/company/search/?q={ticker}`` — confirm ticker exists and resolve
     the canonical consolidated page URL.
  2. ``GET /company/{ticker}/consolidated/`` — scrape the HTML page with
     BeautifulSoup to extract ratios, EPS, payout %, and cash-flow data.

Implements exponential backoff (initial 1 s, max 3 retries) on HTTP 429 and 5xx.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

SCREENER_BASE_URL = "https://www.screener.in"
_INITIAL_BACKOFF_SECS: float = 1.0
_MAX_RETRIES: int = 3

_HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; divvy-forge/0.1)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_JSON_HEADERS = {
    "User-Agent": "divvy-forge/0.1",
    "Accept": "application/json",
}


class ScreenerError(Exception):
    """Structured error raised by screener_client functions.

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
# HTTP helpers
# ---------------------------------------------------------------------------


def _get_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    ticker: str,
) -> httpx.Response:
    """GET *url* with exponential backoff on 429 / 5xx."""
    last_error: Exception | None = None
    backoff = _INITIAL_BACKOFF_SECS

    for attempt in range(_MAX_RETRIES + 1):
        resp = client.get(url, headers=headers, follow_redirects=True)

        if resp.status_code == 200:
            return resp

        if resp.status_code == 404:
            raise ScreenerError(
                "TICKER_NOT_FOUND",
                f"Ticker '{ticker}' not found on Screener.in (HTTP 404).",
                ticker=ticker,
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            if attempt < _MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            break

        resp.raise_for_status()

    raise ScreenerError(
        "DATA_FETCH_FAILED",
        f"Screener.in failed for '{ticker}' after {_MAX_RETRIES} retries. "
        f"Last error: {last_error}",
        ticker=ticker,
    )


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------


def _to_float(raw: str) -> float | None:
    """Parse a Screener number string (commas, %, trailing signs) to float."""
    cleaned = raw.strip().replace(",", "").rstrip("%").rstrip("+").rstrip("-").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_top_ratios(soup: BeautifulSoup) -> dict[str, str]:
    """Return name→raw-value pairs from the ``#top-ratios`` ul."""
    result: dict[str, str] = {}
    section = soup.find(id="top-ratios")
    if not section:
        return result
    for li in section.find_all("li"):
        name_tag = li.find(class_="name")
        # The value span has class "number" plus optional extra classes
        number_tag = li.find(class_="number")
        if name_tag and number_tag:
            result[name_tag.get_text(strip=True)] = number_tag.get_text(strip=True)
    return result


def _parse_section_table(
    soup: BeautifulSoup, section_id: str
) -> tuple[list[str], dict[str, list[str]]]:
    """Parse the first ``<table>`` inside ``<section id=section_id>``.

    Returns ``(period_headers, {row_name: [cell_values]})``.
    The first column (row label) is used as the key; period headers come from
    the ``<thead>`` row (skipping the first blank ``<th>``).
    """
    section = soup.find(id=section_id)
    if not section:
        return [], {}

    table = section.find("table")
    if not table:
        return [], {}

    headers: list[str] = []
    rows: dict[str, list[str]] = {}

    thead = table.find("thead")
    if thead:
        header_cells = thead.find_all("th")
        # First th is blank label column; rest are period headers
        headers = [th.get_text(strip=True) for th in header_cells[1:]]

    tbody = table.find("tbody")
    if not tbody:
        return headers, rows

    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row_name = cells[0].get_text(strip=True).rstrip(" +").rstrip(" -").strip()
        if row_name:
            rows[row_name] = [c.get_text(strip=True) for c in cells[1:]]

    return headers, rows


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_html(ticker: str, html: str) -> dict:
    """Extract fundamentals from a Screener.in consolidated company page."""
    soup = BeautifulSoup(html, "lxml")
    fetched_at = datetime.now(timezone.utc).isoformat()
    raw_response_excerpt = html[:500]

    # --- Live ratios from #top-ratios ---
    top = _parse_top_ratios(soup)
    dividend_yield_pct = _to_float(top.get("Dividend Yield", ""))

    # --- P&L table: EPS and payout ratio ---
    pl_headers, pl_rows = _parse_section_table(soup, "profit-loss")

    eps: float | None = None
    for key in ("EPS in Rs", "EPS"):
        if key in pl_rows and pl_rows[key]:
            # Use the most recent non-TTM value (last before TTM, or last available)
            vals = [_to_float(v) for v in pl_rows[key]]
            non_null = [v for v in vals if v is not None]
            eps = non_null[-1] if non_null else None
            break

    payout_ratio: float | None = None
    for key in ("Dividend Payout %", "Dividend Payout"):
        if key in pl_rows and pl_rows[key]:
            vals = [_to_float(v) for v in pl_rows[key]]
            non_null = [v for v in vals if v is not None]
            payout_ratio = non_null[-1] if non_null else None
            break

    # --- DPS history: EPS × payout% for each period (last 5) ---
    dividends_per_share_history: list[dict] | None = None
    eps_vals = [_to_float(v) for v in pl_rows.get("EPS in Rs", pl_rows.get("EPS", []))]
    payout_vals = [
        _to_float(v)
        for v in pl_rows.get("Dividend Payout %", pl_rows.get("Dividend Payout", []))
    ]
    if pl_headers and eps_vals and payout_vals:
        periods = pl_headers[: len(eps_vals)]
        history = []
        for period, e, p in zip(periods, eps_vals, payout_vals):
            if e is not None and p is not None:
                history.append({"period": period, "value": round(e * p / 100, 2)})
        if history:
            dividends_per_share_history = history[-5:]

    # --- Cash flow table: FCF = operating CF − |capex| ---
    _, cf_rows = _parse_section_table(soup, "cash-flow")

    free_cash_flow: float | None = None
    op_cf_row = cf_rows.get("Cash from Operating Activity", [])
    # Capex lives under Investing activity; look for a dedicated row or use investing total
    capex_row = cf_rows.get("Capital Expenditure", cf_rows.get("Fixed assets", []))

    if op_cf_row:
        op_vals = [_to_float(v) for v in op_cf_row]
        op_non_null = [v for v in op_vals if v is not None]
        if op_non_null:
            op_cf = op_non_null[-1]
            capex = 0.0
            if capex_row:
                cap_vals = [_to_float(v) for v in capex_row]
                cap_non_null = [v for v in cap_vals if v is not None]
                if cap_non_null:
                    capex = abs(cap_non_null[-1])
            free_cash_flow = op_cf - capex

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
    """Fetch fundamental data by scraping Screener.in (no auth required).

    Step 1: ``/api/company/search/?q={ticker}`` — verify ticker exists.
    Step 2: ``/company/{ticker}/consolidated/`` — scrape HTML fundamentals.

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
        ``TICKER_NOT_FOUND`` — ticker not found via search or page returns 404.
        ``DATA_FETCH_FAILED`` — max retries exceeded on transient errors.
    """

    def _do(client: httpx.Client) -> dict:
        # Step 1: confirm ticker exists via search
        search_url = f"{SCREENER_BASE_URL}/api/company/search/?q={ticker}"
        search_resp = _get_with_retry(client, search_url, _JSON_HEADERS, ticker)
        results = search_resp.json()
        if not results:
            raise ScreenerError(
                "TICKER_NOT_FOUND",
                f"Ticker '{ticker}' not found on Screener.in (empty search results).",
                ticker=ticker,
            )

        # Use the canonical URL from search (e.g. "/company/INFY/consolidated/")
        page_path = results[0].get("url", f"/company/{ticker}/consolidated/")
        if not page_path.endswith("consolidated/"):
            page_path = page_path.rstrip("/") + "/consolidated/"

        # Step 2: scrape the consolidated HTML page
        page_url = f"{SCREENER_BASE_URL}{page_path}"
        page_resp = _get_with_retry(client, page_url, _HTML_HEADERS, ticker)
        return _parse_html(ticker, page_resp.text)

    if http_client is not None:
        return _do(http_client)
    with httpx.Client() as client:
        return _do(client)

"""Unit tests for the market-data-fetcher module stack.

Covers:
  screener_client  — HTML scraping success, DPS / FCF parsing, 429 retry,
                     5xx retry, ticker-not-found (empty search), max-retry
  yfinance_client  — success, NS suffix, dividend history, missing fields,
                     ticker-not-found, data-fetch-failed
  market_data_fetcher — primary success, fallback triggered, both fail,
                        ticker-not-found on both
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import respx

from divvy_forge import market_data_fetcher, screener_client, yfinance_client
from divvy_forge.market_data_fetcher import MarketDataError
from divvy_forge.screener_client import ScreenerError
from divvy_forge.yfinance_client import YFinanceError

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "https://www.screener.in/api/company/search/?q=INFY"
PAGE_URL = "https://www.screener.in/company/INFY/consolidated/"
SEARCH_RESULT = [{"id": 1489, "name": "Infosys Ltd", "url": "/company/INFY/consolidated/"}]

SAMPLE_SCREENER_HTML = """
<!DOCTYPE html><html><body>
<ul id="top-ratios">
  <li><span class="name">Dividend Yield</span><span class="number">3.50 %</span></li>
  <li><span class="name">Stock P/E</span><span class="number">26.5</span></li>
</ul>
<section id="profit-loss">
  <table>
    <thead>
      <tr><th></th><th>Mar 2020</th><th>Mar 2021</th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr>
    </thead>
    <tbody>
      <tr><td>Sales</td><td>76,329</td><td>90,791</td><td>1,21,641</td><td>1,46,767</td><td>1,53,670</td></tr>
      <tr><td>EPS in Rs</td><td>38.57</td><td>46.31</td><td>58.75</td><td>60.06</td><td>63.95</td></tr>
      <tr><td>Dividend Payout %</td><td>56</td><td>59</td><td>68</td><td>70</td><td>71</td></tr>
    </tbody>
  </table>
</section>
<section id="cash-flow">
  <table>
    <thead>
      <tr><th></th><th>Mar 2020</th><th>Mar 2021</th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr>
    </thead>
    <tbody>
      <tr><td>Cash from Operating Activity +</td><td>600</td><td>700</td><td>800</td><td>900</td><td>1,000</td></tr>
      <tr><td>Capital Expenditure</td><td>60</td><td>80</td><td>100</td><td>150</td><td>200</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

SAMPLE_YFINANCE_INFO = {
    "symbol": "INFY.NS",
    "shortName": "Infosys Limited",
    "dividendYield": 2.5,   # yfinance 1.x returns percentage directly (not decimal)
    "payoutRatio": 0.40,    # yfinance 1.x still returns decimal for payoutRatio
    "trailingEps": 65.3,
    "regularMarketPrice": 1450.0,
}


def _make_yfinance_mock(
    *,
    info: dict | None = None,
    dividends=None,
    cashflow=None,
    raise_exc: Exception | None = None,
) -> MagicMock:
    """Build a minimal mock yfinance module."""
    import pandas as pd

    mock_yf = MagicMock()

    if raise_exc is not None:
        mock_yf.Ticker.side_effect = raise_exc
        return mock_yf

    mock_ticker = MagicMock()
    mock_ticker.info = info if info is not None else {}
    mock_ticker.dividends = dividends if dividends is not None else pd.Series(dtype=float)
    mock_ticker.cashflow = cashflow if cashflow is not None else pd.DataFrame()
    mock_yf.Ticker.return_value = mock_ticker
    return mock_yf


def _mock_screener_success():
    """Register respx mocks for a successful screener fetch of INFY."""
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_RESULT))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=SAMPLE_SCREENER_HTML))


# ===========================================================================
# screener_client tests
# ===========================================================================


class TestScreenerClientSuccess:
    @respx.mock
    def test_returns_source_and_ticker(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["source"] == "screener.in"
        assert result["ticker"] == "INFY"

    @respx.mock
    def test_parses_dividend_yield_from_top_ratios(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["dividend_yield_pct"] == pytest.approx(3.50)

    @respx.mock
    def test_parses_eps_from_pl_table(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["eps"] == pytest.approx(63.95)  # most recent year

    @respx.mock
    def test_parses_payout_ratio_from_pl_table(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["payout_ratio"] == pytest.approx(71.0)  # most recent year

    @respx.mock
    def test_computes_dps_history_from_eps_and_payout(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        history = result["dividends_per_share_history"]
        assert history is not None
        assert len(history) == 5
        # Mar 2024: 63.95 × 71 / 100 = 45.40
        assert history[-1]["period"] == "Mar 2024"
        assert history[-1]["value"] == pytest.approx(63.95 * 71 / 100, rel=1e-2)

    @respx.mock
    def test_computes_free_cash_flow(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        # FCF = 1000 - 200 = 800 (most recent year)
        assert result["free_cash_flow"] == pytest.approx(800.0)

    @respx.mock
    def test_fetched_at_and_excerpt_present(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["fetched_at"] is not None
        assert isinstance(result["raw_response_excerpt"], str)
        assert len(result["raw_response_excerpt"]) <= 500

    @respx.mock
    def test_missing_sections_return_none(self):
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_RESULT))
        respx.get(PAGE_URL).mock(
            return_value=httpx.Response(200, text="<html><body></body></html>")
        )
        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)
        assert result["dividend_yield_pct"] is None
        assert result["eps"] is None
        assert result["payout_ratio"] is None
        assert result["dividends_per_share_history"] is None
        assert result["free_cash_flow"] is None


class TestScreenerClientErrors:
    @respx.mock
    def test_raises_ticker_not_found_on_empty_search(self):
        respx.get("https://www.screener.in/api/company/search/?q=FAKE").mock(
            return_value=httpx.Response(200, json=[])
        )
        with httpx.Client() as client:
            with pytest.raises(ScreenerError) as exc_info:
                screener_client.fetch_fundamentals("FAKE", http_client=client)
        assert exc_info.value.code == "TICKER_NOT_FOUND"

    @respx.mock
    def test_raises_ticker_not_found_on_page_404(self):
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_RESULT))
        respx.get(PAGE_URL).mock(return_value=httpx.Response(404))
        with httpx.Client() as client:
            with pytest.raises(ScreenerError) as exc_info:
                screener_client.fetch_fundamentals("INFY", http_client=client)
        assert exc_info.value.code == "TICKER_NOT_FOUND"

    @respx.mock
    def test_raises_data_fetch_failed_on_search_503(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
        with httpx.Client() as client:
            with pytest.raises(ScreenerError) as exc_info:
                screener_client.fetch_fundamentals("INFY", http_client=client)
        assert exc_info.value.code == "DATA_FETCH_FAILED"


class TestScreenerClientRateLimitRetry:
    @respx.mock
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)

        call_count = 0
        search_responses = [httpx.Response(429), httpx.Response(200, json=SEARCH_RESULT)]

        def search_side_effect(request):
            nonlocal call_count
            resp = search_responses[min(call_count, len(search_responses) - 1)]
            call_count += 1
            return resp

        respx.get(SEARCH_URL).mock(side_effect=search_side_effect)
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=SAMPLE_SCREENER_HTML))

        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)

        assert result["source"] == "screener.in"
        assert call_count == 2

    @respx.mock
    def test_exhausts_retries_on_persistent_429(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
        with httpx.Client() as client:
            with pytest.raises(ScreenerError) as exc_info:
                screener_client.fetch_fundamentals("INFY", http_client=client)
        assert exc_info.value.code == "DATA_FETCH_FAILED"

    @respx.mock
    def test_retries_on_503_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)

        call_count = 0
        search_responses = [httpx.Response(503), httpx.Response(200, json=SEARCH_RESULT)]

        def search_side_effect(request):
            nonlocal call_count
            resp = search_responses[min(call_count, len(search_responses) - 1)]
            call_count += 1
            return resp

        respx.get(SEARCH_URL).mock(side_effect=search_side_effect)
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=SAMPLE_SCREENER_HTML))

        with httpx.Client() as client:
            result = screener_client.fetch_fundamentals("INFY", http_client=client)

        assert result["source"] == "screener.in"


# ===========================================================================
# yfinance_client tests
# ===========================================================================


class TestYFinanceClientSuccess:
    def test_returns_source_and_ticker(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert result["source"] == "yfinance"
        assert result["ticker"] == "INFY"

    def test_returns_yield_as_pct(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert result["dividend_yield_pct"] == pytest.approx(2.5)  # already % in yfinance 1.x

    def test_converts_decimal_payout_to_pct(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert result["payout_ratio"] == pytest.approx(40.0)  # 0.40 × 100

    def test_returns_eps(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert result["eps"] == pytest.approx(65.3)

    def test_appends_ns_suffix_for_bare_ticker(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        mock_yf.Ticker.assert_called_once_with("INFY.NS")

    def test_does_not_double_suffix(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        yfinance_client.fetch_fundamentals("INFY.NS", yfin_module=mock_yf)
        mock_yf.Ticker.assert_called_once_with("INFY.NS")

    def test_parses_dividend_history(self):
        import pandas as pd

        idx = pd.to_datetime(["2024-06-01", "2023-06-01", "2022-06-01"])
        dividends = pd.Series([14.0, 12.5, 11.0], index=idx)
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO, dividends=dividends)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        history = result["dividends_per_share_history"]
        assert history is not None
        assert len(history) == 3
        assert history[0]["value"] == pytest.approx(14.0)

    def test_returns_none_for_missing_yield(self):
        info = {"symbol": "XYZ", "shortName": "XYZ Corp"}
        mock_yf = _make_yfinance_mock(info=info)
        result = yfinance_client.fetch_fundamentals("XYZ", yfin_module=mock_yf)
        assert result["dividend_yield_pct"] is None

    def test_fetched_at_and_excerpt_present(self):
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)
        result = yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert result["fetched_at"] is not None
        assert isinstance(result["raw_response_excerpt"], str)


class TestYFinanceClientErrors:
    def test_raises_ticker_not_found_for_empty_info(self):
        mock_yf = _make_yfinance_mock(info={})
        with pytest.raises(YFinanceError) as exc_info:
            yfinance_client.fetch_fundamentals("NOSUCHTICKET", yfin_module=mock_yf)
        assert exc_info.value.code == "TICKER_NOT_FOUND"

    def test_raises_data_fetch_failed_on_exception(self):
        mock_yf = _make_yfinance_mock(raise_exc=RuntimeError("network error"))
        with pytest.raises(YFinanceError) as exc_info:
            yfinance_client.fetch_fundamentals("INFY", yfin_module=mock_yf)
        assert exc_info.value.code == "DATA_FETCH_FAILED"


# ===========================================================================
# market_data_fetcher (orchestrator) tests
# ===========================================================================


class TestMarketDataFetcherPrimarySuccess:
    @respx.mock
    def test_returns_screener_data_when_available(self):
        _mock_screener_success()
        with httpx.Client() as client:
            result = market_data_fetcher.fetch_fundamentals("INFY", http_client=client)
        assert result["source"] == "screener.in"


class TestMarketDataFetcherFallback:
    @respx.mock
    def test_falls_back_to_yfinance_on_screener_5xx(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)

        with httpx.Client() as client:
            result = market_data_fetcher.fetch_fundamentals(
                "INFY", http_client=client, yfin_module=mock_yf
            )
        assert result["source"] == "yfinance"

    @respx.mock
    def test_falls_back_to_yfinance_on_screener_ticker_not_found(self):
        respx.get("https://www.screener.in/api/company/search/?q=RARE").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock_yf = _make_yfinance_mock(info=SAMPLE_YFINANCE_INFO)

        with httpx.Client() as client:
            result = market_data_fetcher.fetch_fundamentals(
                "RARE", http_client=client, yfin_module=mock_yf
            )
        assert result["source"] == "yfinance"


class TestMarketDataFetcherBothFail:
    @respx.mock
    def test_raises_data_fetch_failed_when_both_sources_error(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
        mock_yf = _make_yfinance_mock(raise_exc=RuntimeError("yfinance down"))

        with httpx.Client() as client:
            with pytest.raises(MarketDataError) as exc_info:
                market_data_fetcher.fetch_fundamentals(
                    "INFY", http_client=client, yfin_module=mock_yf
                )
        assert exc_info.value.code == "DATA_FETCH_FAILED"

    @respx.mock
    def test_raises_ticker_not_found_when_both_confirm_unknown(self):
        respx.get("https://www.screener.in/api/company/search/?q=FAKE").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock_yf = _make_yfinance_mock(info={})

        with httpx.Client() as client:
            with pytest.raises(MarketDataError) as exc_info:
                market_data_fetcher.fetch_fundamentals(
                    "FAKE", http_client=client, yfin_module=mock_yf
                )
        assert exc_info.value.code == "TICKER_NOT_FOUND"

    @respx.mock
    def test_error_includes_both_source_messages(self, monkeypatch):
        monkeypatch.setattr("divvy_forge.screener_client.time.sleep", lambda _: None)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
        mock_yf = _make_yfinance_mock(raise_exc=RuntimeError("timeout"))

        with httpx.Client() as client:
            with pytest.raises(MarketDataError) as exc_info:
                market_data_fetcher.fetch_fundamentals(
                    "INFY", http_client=client, yfin_module=mock_yf
                )
        err = exc_info.value
        assert "screener_error" in err.context
        assert "yfinance_error" in err.context

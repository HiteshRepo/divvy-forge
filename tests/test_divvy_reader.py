"""Unit tests for divvy_reader tool functions.

Coverage (task 5.5):
- read_file: success, file-not-found (NOT_FOUND), auth-error (AUTH_ERROR)
- list_watchlist: success with tickers, empty watchlist, watchlist missing (WATCHLIST_NOT_FOUND)
- read_ticker: success (all fields), partial parse (missing fields → null), ticker not found
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from divvy_forge.divvy_reader import (
    GITHUB_API,
    REPO,
    WATCHLIST_PATH,
    DivvyReaderError,
    TickerState,
    _list_watchlist,
    _parse_markdown_table,
    _read_file,
    _read_ticker,
)

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

TOKEN = "ghp_test_reader_token"
CONTENTS_URL = f"{GITHUB_API}/repos/{REPO}/contents"

_WATCHLIST_WITH_TICKERS = """\
# Watchlist

| Ticker | Company | Sector | Yield % | Payout Ratio % | Notes | Date Added |
|--------|---------|--------|---------|----------------|-------|------------|
| INFY | Infosys | IT | 3.50 | 45.00 | Consistent payer | 2024-01-15 |
| TCS | Tata Consultancy | IT | 2.80 | 40.00 | | 2024-02-10 |
| ITC | ITC Ltd | FMCG | 5.10 | 65.00 | Tobacco exposure | 2024-03-01 |
"""

_WATCHLIST_PARTIAL = """\
# Watchlist

| Ticker | Company | Sector | Yield % | Payout Ratio % | Notes | Date Added |
|--------|---------|--------|---------|----------------|-------|------------|
| HDFC | HDFC Bank | Finance | | | | |
"""

_WATCHLIST_EMPTY = """\
# Watchlist

| Ticker | Company | Sector | Yield % | Payout Ratio % | Notes | Date Added |
|--------|---------|--------|---------|----------------|-------|------------|
"""

_SOME_FILE_CONTENT = "# INFY\n\nDividend info here.\n"


def _encode(text: str) -> str:
    """Base64-encode text the same way GitHub API returns it."""
    return base64.b64encode(text.encode()).decode()


def _github_file_response(content: str, path: str = "some/file.md") -> dict:
    return {
        "name": path.split("/")[-1],
        "path": path,
        "content": _encode(content),
        "encoding": "base64",
    }


# ---------------------------------------------------------------------------
# _parse_markdown_table (pure function — no HTTP needed)
# ---------------------------------------------------------------------------


class TestParseMarkdownTable:
    def test_parses_full_table(self) -> None:
        rows = _parse_markdown_table(_WATCHLIST_WITH_TICKERS)
        assert len(rows) == 3
        assert rows[0]["Ticker"] == "INFY"
        assert rows[0]["Yield %"] == "3.50"
        assert rows[1]["Ticker"] == "TCS"

    def test_empty_table_returns_no_rows(self) -> None:
        rows = _parse_markdown_table(_WATCHLIST_EMPTY)
        assert rows == []

    def test_skips_non_table_lines(self) -> None:
        content = "# Header\n\nSome prose.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        rows = _parse_markdown_table(content)
        assert len(rows) == 1
        assert rows[0]["A"] == "1"

    def test_partial_row_values(self) -> None:
        rows = _parse_markdown_table(_WATCHLIST_PARTIAL)
        assert len(rows) == 1
        assert rows[0]["Ticker"] == "HDFC"
        assert rows[0]["Yield %"] == ""


# ---------------------------------------------------------------------------
# _read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_returns_raw_content_on_success(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/dividend/data/watchlist.md").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_SOME_FILE_CONTENT, "dividend/data/watchlist.md")
                )
            )
            with httpx.Client() as client:
                result = _read_file("dividend/data/watchlist.md", http_client=client)

        assert result == _SOME_FILE_CONTENT

    def test_raises_not_found_on_404(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/dividend/missing.md").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_file("dividend/missing.md", http_client=client)

        assert exc_info.value.code == "NOT_FOUND"
        assert "dividend/missing.md" in str(exc_info.value)

    def test_raises_auth_error_on_401(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/some/file.md").mock(
                return_value=httpx.Response(401, json={"message": "Bad credentials"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_file("some/file.md", http_client=client)

        assert exc_info.value.code == "AUTH_ERROR"

    def test_raises_auth_error_on_403(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/some/file.md").mock(
                return_value=httpx.Response(403, json={"message": "Forbidden"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_file("some/file.md", http_client=client)

        assert exc_info.value.code == "AUTH_ERROR"

    def test_propagates_unexpected_http_error(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/some/file.md").mock(
                return_value=httpx.Response(500, json={"message": "Internal Server Error"})
            )
            with httpx.Client() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    _read_file("some/file.md", http_client=client)


# ---------------------------------------------------------------------------
# _list_watchlist
# ---------------------------------------------------------------------------


class TestListWatchlist:
    def test_returns_ordered_tickers(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_WITH_TICKERS, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _list_watchlist(http_client=client)

        assert result == ["INFY", "TCS", "ITC"]

    def test_returns_empty_list_for_empty_watchlist(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_EMPTY, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _list_watchlist(http_client=client)

        assert result == []

    def test_raises_watchlist_not_found_when_file_missing(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _list_watchlist(http_client=client)

        assert exc_info.value.code == "WATCHLIST_NOT_FOUND"

    def test_propagates_auth_error(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(401, json={"message": "Bad credentials"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _list_watchlist(http_client=client)

        assert exc_info.value.code == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# _read_ticker
# ---------------------------------------------------------------------------


class TestReadTicker:
    def test_returns_full_ticker_state_on_success(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_WITH_TICKERS, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _read_ticker("INFY", http_client=client)

        assert result["ticker"] == "INFY"
        assert result["yield_pct"] == pytest.approx(3.50)
        assert result["payout_ratio"] == pytest.approx(45.00)
        assert result["last_review_date"] == "2024-01-15"
        assert result["notes"] == "Consistent payer"
        assert result["raw_markdown"] == _WATCHLIST_WITH_TICKERS

    def test_ticker_lookup_is_case_insensitive(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_WITH_TICKERS, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _read_ticker("infy", http_client=client)

        assert result["ticker"] == "INFY"

    def test_returns_null_for_missing_fields(self) -> None:
        """Partial row — Yield %, Payout Ratio %, Notes, Date Added all empty."""
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_PARTIAL, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _read_ticker("HDFC", http_client=client)

        assert result["ticker"] == "HDFC"
        assert result["yield_pct"] is None
        assert result["payout_ratio"] is None
        assert result["last_review_date"] is None
        assert result["notes"] is None
        assert result["raw_markdown"] == _WATCHLIST_PARTIAL

    def test_raises_not_found_for_unknown_ticker(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_WITH_TICKERS, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_ticker("RELIANCE", http_client=client)

        assert exc_info.value.code == "NOT_FOUND"
        assert "RELIANCE" in str(exc_info.value)

    def test_raises_watchlist_not_found_when_file_missing(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_ticker("INFY", http_client=client)

        assert exc_info.value.code == "WATCHLIST_NOT_FOUND"

    def test_raises_auth_error_on_401(self) -> None:
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(401, json={"message": "Bad credentials"})
            )
            with httpx.Client() as client:
                with pytest.raises(DivvyReaderError) as exc_info:
                    _read_ticker("INFY", http_client=client)

        assert exc_info.value.code == "AUTH_ERROR"

    def test_empty_notes_returned_as_none(self) -> None:
        """TCS row has an empty Notes column — should map to None."""
        with respx.mock:
            respx.get(f"{CONTENTS_URL}/{WATCHLIST_PATH}").mock(
                return_value=httpx.Response(
                    200, json=_github_file_response(_WATCHLIST_WITH_TICKERS, WATCHLIST_PATH)
                )
            )
            with httpx.Client() as client:
                result = _read_ticker("TCS", http_client=client)

        assert result["ticker"] == "TCS"
        assert result["yield_pct"] == pytest.approx(2.80)
        assert result["notes"] is None

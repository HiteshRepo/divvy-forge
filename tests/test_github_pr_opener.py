"""Unit tests for github_pr_opener tool functions.

Coverage (task 8.7):
- check_existing_pr: PR found, no match, 403 insufficient scope
- create_branch: success, 422 already exists, 403 insufficient scope
- commit_diff: success, 404 file not found, 403 insufficient scope, 409 conflict
- open_pr: success path, duplicate PR detected, 403 on PR create, branch creation failure
- _apply_unified_diff: add lines, remove lines, context lines, empty diff
- format_pr_body: full proposal, no fundamentals, no risk
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from divvy_forge.github_pr_opener import (
    GITHUB_API,
    TARGET_REPO,
    BranchCreationError,
    CommitError,
    InsufficientScopeError,
    MergedProposal,
    PrResult,
    _apply_unified_diff,
    _branch_name,
    _check_existing_pr,
    _commit_diff,
    _create_branch,
    _open_pr,
    _pr_title,
    format_pr_body,
)

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TICKER = "INFY"
DATE = "2024-01-15"
BRANCH = f"divvy-review/{TICKER}/{DATE}"
PR_TITLE = f"Divvy Review: {TICKER} ({DATE})"
WATCHLIST_PATH = "dividend/data/watchlist.md"

PULLS_URL = f"{GITHUB_API}/repos/{TARGET_REPO}/pulls"
REFS_URL = f"{GITHUB_API}/repos/{TARGET_REPO}/git/refs"
MAIN_REF_URL = f"{GITHUB_API}/repos/{TARGET_REPO}/git/ref/heads/main"
CONTENTS_URL = f"{GITHUB_API}/repos/{TARGET_REPO}/contents/{WATCHLIST_PATH}"

MAIN_SHA = "abc123mainsha"
BLOB_SHA = "def456blobsha"

_SIMPLE_CONTENT = "# Watchlist\n\nSome content here.\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _file_response(content: str = _SIMPLE_CONTENT, sha: str = BLOB_SHA) -> dict:
    return {
        "name": "watchlist.md",
        "path": WATCHLIST_PATH,
        "content": _b64(content),
        "encoding": "base64",
        "sha": sha,
    }


def _pr_response(
    number: int = 42,
    html_url: str = "https://github.com/HiteshRepo/stock-screeners/pull/42",
    title: str = PR_TITLE,
) -> dict:
    return {"number": number, "html_url": html_url, "title": title, "state": "open"}


def _make_proposal(diff: str = "") -> MergedProposal:
    return MergedProposal(
        ticker=TICKER,
        date=DATE,
        merge_reasoning="Strong fundamentals, low risk.",
        fundamentals={
            "status": "ok",
            "yield_trend": "stable",
            "payout_sustainability": "safe",
            "suggested_yield_update": 3.75,
            "reasoning": "Yield consistent over 5 periods.",
        },
        risk={
            "risk_level": "low",
            "signals": ["No recent cut signals"],
            "sources": [{"title": "Reuters", "url": "https://reuters.com/infy"}],
        },
        changed_fields=["yield_pct", "last_review_date"],
        diff=diff,
    )


# ---------------------------------------------------------------------------
# _apply_unified_diff
# ---------------------------------------------------------------------------


class TestApplyUnifiedDiff:
    def test_empty_diff_returns_original(self) -> None:
        original = "line1\nline2\n"
        assert _apply_unified_diff(original, "") == original

    def test_whitespace_only_diff_returns_original(self) -> None:
        original = "line1\nline2\n"
        assert _apply_unified_diff(original, "   \n") == original

    def test_adds_line(self) -> None:
        original = "line1\nline2\n"
        diff = "@@ -1,2 +1,3 @@\n line1\n+new line\n line2\n"
        result = _apply_unified_diff(original, diff)
        assert "new line\n" in result
        assert "line1\n" in result
        assert "line2\n" in result

    def test_removes_line(self) -> None:
        original = "line1\nremove me\nline2\n"
        diff = "@@ -1,3 +1,2 @@\n line1\n-remove me\n line2\n"
        result = _apply_unified_diff(original, diff)
        assert "remove me" not in result
        assert "line1\n" in result
        assert "line2\n" in result

    def test_replaces_line(self) -> None:
        original = "yield: 3.50\n"
        diff = "@@ -1,1 +1,1 @@\n-yield: 3.50\n+yield: 3.75\n"
        result = _apply_unified_diff(original, diff)
        assert "yield: 3.75\n" in result
        assert "yield: 3.50" not in result

    def test_skips_file_header_lines(self) -> None:
        """--- and +++ header lines must not appear in output."""
        original = "hello\n"
        diff = "--- a/file.md\n+++ b/file.md\n@@ -1,1 +1,1 @@\n-hello\n+world\n"
        result = _apply_unified_diff(original, diff)
        assert "---" not in result
        assert "+++" not in result
        assert "world\n" in result


# ---------------------------------------------------------------------------
# _check_existing_pr
# ---------------------------------------------------------------------------


class TestCheckExistingPr:
    def test_returns_url_when_pr_found(self) -> None:
        pr = _pr_response()
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[pr]))
            with httpx.Client() as client:
                url = _check_existing_pr(TICKER, DATE, http_client=client)

        assert url == pr["html_url"]

    def test_returns_none_when_no_match(self) -> None:
        other_pr = _pr_response(title="Divvy Review: TCS (2024-02-01)")
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[other_pr]))
            with httpx.Client() as client:
                url = _check_existing_pr(TICKER, DATE, http_client=client)

        assert url is None

    def test_returns_none_on_empty_list(self) -> None:
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[]))
            with httpx.Client() as client:
                url = _check_existing_pr(TICKER, DATE, http_client=client)

        assert url is None

    def test_raises_insufficient_scope_on_403(self) -> None:
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _check_existing_pr(TICKER, DATE, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_propagates_unexpected_5xx(self) -> None:
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(500, json={"message": "Server Error"}))
            with httpx.Client() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    _check_existing_pr(TICKER, DATE, http_client=client)

    def test_ticker_uppercased_in_title(self) -> None:
        """Title uses uppercase ticker regardless of input casing."""
        pr = _pr_response()  # title uses INFY (uppercase)
        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[pr]))
            with httpx.Client() as client:
                url = _check_existing_pr("infy", DATE, http_client=client)

        assert url == pr["html_url"]


# ---------------------------------------------------------------------------
# _create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:
    def _mock_main_ref(self) -> None:
        respx.get(MAIN_REF_URL).mock(
            return_value=httpx.Response(200, json={"object": {"sha": MAIN_SHA}})
        )

    def test_returns_branch_name_on_success(self) -> None:
        with respx.mock:
            self._mock_main_ref()
            respx.post(REFS_URL).mock(return_value=httpx.Response(201, json={}))
            with httpx.Client() as client:
                branch = _create_branch(TICKER, DATE, http_client=client)

        assert branch == BRANCH

    def test_raises_branch_creation_error_on_422(self) -> None:
        """422 means branch already exists."""
        with respx.mock:
            self._mock_main_ref()
            respx.post(REFS_URL).mock(
                return_value=httpx.Response(422, json={"message": "Reference already exists"})
            )
            with httpx.Client() as client:
                with pytest.raises(BranchCreationError) as exc_info:
                    _create_branch(TICKER, DATE, http_client=client)

        assert exc_info.value.code == "BRANCH_CREATION_FAILED"
        assert BRANCH in str(exc_info.value)

    def test_raises_insufficient_scope_on_403_for_ref_get(self) -> None:
        with respx.mock:
            respx.get(MAIN_REF_URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _create_branch(TICKER, DATE, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_raises_insufficient_scope_on_403_for_ref_post(self) -> None:
        with respx.mock:
            self._mock_main_ref()
            respx.post(REFS_URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _create_branch(TICKER, DATE, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_branch_name_uses_uppercase_ticker(self) -> None:
        with respx.mock:
            self._mock_main_ref()
            respx.post(REFS_URL).mock(return_value=httpx.Response(201, json={}))
            with httpx.Client() as client:
                branch = _create_branch("infy", DATE, http_client=client)

        assert branch == BRANCH


# ---------------------------------------------------------------------------
# _commit_diff
# ---------------------------------------------------------------------------


class TestCommitDiff:
    _DIFF = "@@ -1,1 +1,1 @@\n-# Watchlist\n+# Watchlist (updated)\n"

    def test_success_path(self) -> None:
        """Success: GET file → apply diff → PUT updated content."""
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response()))
            respx.put(CONTENTS_URL).mock(return_value=httpx.Response(200, json={"commit": {"sha": "newsha"}}))
            with httpx.Client() as client:
                _commit_diff(BRANCH, WATCHLIST_PATH, self._DIFF, BLOB_SHA, http_client=client)

        # If no exception raised, the commit succeeded

    def test_raises_commit_error_on_404(self) -> None:
        """File not found on branch → CommitError."""
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
            with httpx.Client() as client:
                with pytest.raises(CommitError) as exc_info:
                    _commit_diff(BRANCH, WATCHLIST_PATH, self._DIFF, BLOB_SHA, http_client=client)

        assert exc_info.value.code == "COMMIT_FAILED"

    def test_raises_insufficient_scope_on_403_get(self) -> None:
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _commit_diff(BRANCH, WATCHLIST_PATH, self._DIFF, BLOB_SHA, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_raises_insufficient_scope_on_403_put(self) -> None:
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response()))
            respx.put(CONTENTS_URL).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _commit_diff(BRANCH, WATCHLIST_PATH, self._DIFF, BLOB_SHA, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_raises_commit_error_on_409_conflict(self) -> None:
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response()))
            respx.put(CONTENTS_URL).mock(return_value=httpx.Response(409, json={"message": "Conflict"}))
            with httpx.Client() as client:
                with pytest.raises(CommitError) as exc_info:
                    _commit_diff(BRANCH, WATCHLIST_PATH, self._DIFF, BLOB_SHA, http_client=client)

        assert exc_info.value.code == "COMMIT_FAILED"
        assert "conflict" in str(exc_info.value).lower()

    def test_empty_diff_still_commits(self) -> None:
        """Empty diff → apply returns original → still writes (no-op content update)."""
        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response()))
            respx.put(CONTENTS_URL).mock(return_value=httpx.Response(200, json={"commit": {"sha": "newsha"}}))
            with httpx.Client() as client:
                _commit_diff(BRANCH, WATCHLIST_PATH, "", BLOB_SHA, http_client=client)

    def test_uses_sha_from_get_response(self) -> None:
        """The blob SHA from the GET response takes precedence over base_sha param."""
        captured_body: dict = {}

        def _capture_put(request: httpx.Request) -> httpx.Response:
            import json as _json
            captured_body.update(_json.loads(request.content))
            return httpx.Response(200, json={"commit": {"sha": "newsha"}})

        with respx.mock:
            respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response(sha="get-response-sha")))
            respx.put(CONTENTS_URL).mock(side_effect=_capture_put)
            with httpx.Client() as client:
                _commit_diff(BRANCH, WATCHLIST_PATH, "", "fallback-sha", http_client=client)

        assert captured_body.get("sha") == "get-response-sha"


# ---------------------------------------------------------------------------
# format_pr_body
# ---------------------------------------------------------------------------


class TestFormatPrBody:
    def test_contains_ticker_and_date(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert TICKER in body
        assert DATE in body

    def test_contains_merge_reasoning(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert proposal.merge_reasoning in body

    def test_contains_changed_fields(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert "`yield_pct`" in body
        assert "`last_review_date`" in body

    def test_contains_diff_block(self) -> None:
        proposal = _make_proposal(diff="-old line\n+new line\n")
        body = format_pr_body(proposal)
        assert "```diff" in body
        assert "-old line" in body
        assert "+new line" in body

    def test_contains_fundamentals_section(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert "Fundamentals Analysis" in body
        assert "stable" in body  # yield_trend
        assert "safe" in body  # payout_sustainability
        assert "3.75" in body  # suggested_yield_update

    def test_contains_risk_section_with_sources(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert "Dividend-Cut Risk" in body
        assert "low" in body
        assert "Reuters" in body
        assert "https://reuters.com/infy" in body

    def test_no_fundamentals_section_when_none(self) -> None:
        proposal = MergedProposal(
            ticker=TICKER, date=DATE, merge_reasoning="ok",
            fundamentals=None, risk=None, changed_fields=[], diff=""
        )
        body = format_pr_body(proposal)
        assert "Fundamentals Analysis" not in body
        assert "Dividend-Cut Risk" not in body

    def test_collapsed_details_tags(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert "<details>" in body
        assert "</details>" in body
        assert "<summary>" in body

    def test_footer_contains_divvy_forge(self) -> None:
        proposal = _make_proposal()
        body = format_pr_body(proposal)
        assert "divvy-forge" in body


# ---------------------------------------------------------------------------
# _open_pr
# ---------------------------------------------------------------------------


class TestOpenPr:
    """Integration-style tests for the full _open_pr orchestration."""

    _DIFF = "@@ -1,1 +1,1 @@\n-# Watchlist\n+# Watchlist (updated)\n"

    def _mock_no_existing_pr(self) -> None:
        respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[]))

    def _mock_main_ref(self) -> None:
        respx.get(MAIN_REF_URL).mock(
            return_value=httpx.Response(200, json={"object": {"sha": MAIN_SHA}})
        )

    def _mock_create_branch_ok(self) -> None:
        respx.post(REFS_URL).mock(return_value=httpx.Response(201, json={}))

    def _mock_commit_ok(self) -> None:
        respx.get(CONTENTS_URL).mock(return_value=httpx.Response(200, json=_file_response()))
        respx.put(CONTENTS_URL).mock(return_value=httpx.Response(200, json={"commit": {"sha": "csha"}}))

    def _mock_create_pr_ok(self) -> None:
        respx.post(PULLS_URL).mock(
            return_value=httpx.Response(201, json=_pr_response())
        )

    def test_success_path_returns_pr_result(self) -> None:
        proposal = _make_proposal(diff=self._DIFF)
        pr_body = format_pr_body(proposal)

        with respx.mock:
            self._mock_no_existing_pr()
            self._mock_main_ref()
            self._mock_create_branch_ok()
            self._mock_commit_ok()
            self._mock_create_pr_ok()
            with httpx.Client() as client:
                result = _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

        assert isinstance(result, PrResult)
        assert result.already_exists is False
        assert result.pr_number == 42
        assert "pull/42" in result.pr_url
        assert result.branch == BRANCH

    def test_returns_existing_pr_when_duplicate_detected(self) -> None:
        existing_url = "https://github.com/HiteshRepo/stock-screeners/pull/7"
        existing_pr = _pr_response(number=7, html_url=existing_url)
        proposal = _make_proposal(diff=self._DIFF)
        pr_body = format_pr_body(proposal)

        with respx.mock:
            respx.get(PULLS_URL).mock(return_value=httpx.Response(200, json=[existing_pr]))
            with httpx.Client() as client:
                result = _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

        assert result.already_exists is True
        assert result.pr_url == existing_url
        # No branch creation or PR creation calls should have been made

    def test_skips_commit_when_diff_is_empty(self) -> None:
        """Empty diff → skip commit_diff step entirely."""
        proposal = _make_proposal(diff="")
        pr_body = format_pr_body(proposal)

        with respx.mock:
            self._mock_no_existing_pr()
            self._mock_main_ref()
            self._mock_create_branch_ok()
            # No GET/PUT for contents (commit step is skipped)
            self._mock_create_pr_ok()
            with httpx.Client() as client:
                result = _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

        assert result.already_exists is False
        assert result.pr_number == 42

    def test_raises_insufficient_scope_on_403_pr_create(self) -> None:
        proposal = _make_proposal(diff="")
        pr_body = format_pr_body(proposal)

        with respx.mock:
            self._mock_no_existing_pr()
            self._mock_main_ref()
            self._mock_create_branch_ok()
            respx.post(PULLS_URL).mock(
                return_value=httpx.Response(403, json={"message": "Forbidden"})
            )
            with httpx.Client() as client:
                with pytest.raises(InsufficientScopeError) as exc_info:
                    _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"

    def test_propagates_branch_creation_error(self) -> None:
        """If the branch already exists (422), BranchCreationError propagates."""
        proposal = _make_proposal(diff="")
        pr_body = format_pr_body(proposal)

        with respx.mock:
            self._mock_no_existing_pr()
            self._mock_main_ref()
            respx.post(REFS_URL).mock(
                return_value=httpx.Response(422, json={"message": "Reference already exists"})
            )
            with httpx.Client() as client:
                with pytest.raises(BranchCreationError):
                    _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

    def test_pr_branch_follows_naming_pattern(self) -> None:
        """The PR head branch must follow divvy-review/<ticker>/<date>."""
        proposal = _make_proposal(diff="")
        pr_body = format_pr_body(proposal)

        with respx.mock:
            self._mock_no_existing_pr()
            self._mock_main_ref()
            self._mock_create_branch_ok()
            self._mock_create_pr_ok()
            with httpx.Client() as client:
                result = _open_pr(TICKER, DATE, proposal, pr_body, http_client=client)

        assert result.branch.startswith("divvy-review/")
        assert TICKER in result.branch
        assert DATE in result.branch

"""Unit tests for github_auth.validate_token_scopes.

Coverage (task 4.3):
- Valid token with required permissions (fine-grained and classic PAT)
- Missing scope — contents:write absent, repo not accessible (403/404)
- Expired / invalid token — 401 from /user and from /repos
- Unexpected HTTP errors propagate as HTTPStatusError
"""

from __future__ import annotations

import httpx
import pytest
import respx

from divvy_forge.github_auth import (
    InsufficientScopeError,
    InvalidTokenError,
    TokenScopeResult,
    validate_token_scopes,
)

REPO = "HiteshRepo/stock-screeners"
TOKEN = "ghp_testtoken123"
GITHUB_API = "https://api.github.com"

_USER_RESPONSE = {"login": "hiteshrepo", "id": 1}
_REPO_RESPONSE_FULL_PERMISSIONS = {
    "id": 42,
    "full_name": REPO,
    "permissions": {"admin": False, "push": True, "pull": True},
}
_FINE_GRAINED_HEADERS = {"X-OAuth-Scopes": ""}  # empty = fine-grained token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_user_ok() -> None:
    respx.get(f"{GITHUB_API}/user").mock(
        return_value=httpx.Response(200, json=_USER_RESPONSE)
    )


def _mock_repo(
    status_code: int = 200,
    permissions: dict | None = None,
    oauth_scopes: str = "",
) -> None:
    repo_data = dict(_REPO_RESPONSE_FULL_PERMISSIONS)
    if permissions is not None:
        repo_data = {**repo_data, "permissions": permissions}
    headers = {"X-OAuth-Scopes": oauth_scopes}
    respx.get(f"{GITHUB_API}/repos/{REPO}").mock(
        return_value=httpx.Response(status_code, json=repo_data, headers=headers)
    )


# ---------------------------------------------------------------------------
# Valid token — fine-grained PAT
# ---------------------------------------------------------------------------


class TestValidTokenFineGrained:
    def test_returns_scope_result_on_success(self) -> None:
        with respx.mock:
            _mock_user_ok()
            _mock_repo()  # push=True, pull=True, empty X-OAuth-Scopes
            result = validate_token_scopes(TOKEN, REPO)

        assert isinstance(result, TokenScopeResult)
        assert result.valid is True
        assert result.has_contents_write is True
        assert result.has_pull_requests_write is True
        assert result.login == "hiteshrepo"

    def test_accepts_injected_http_client(self) -> None:
        with respx.mock:
            _mock_user_ok()
            _mock_repo()
            with httpx.Client() as client:
                result = validate_token_scopes(TOKEN, REPO, http_client=client)

        assert result.valid is True
        assert result.login == "hiteshrepo"


# ---------------------------------------------------------------------------
# Valid token — classic PAT
# ---------------------------------------------------------------------------


class TestValidTokenClassicPAT:
    def test_repo_scope_passes(self) -> None:
        with respx.mock:
            _mock_user_ok()
            _mock_repo(oauth_scopes="repo, read:user")
            result = validate_token_scopes(TOKEN, REPO)

        assert result.valid is True

    def test_public_repo_scope_passes(self) -> None:
        with respx.mock:
            _mock_user_ok()
            _mock_repo(oauth_scopes="public_repo, read:user")
            result = validate_token_scopes(TOKEN, REPO)

        assert result.valid is True

    def test_read_only_scope_raises_insufficient_scope(self) -> None:
        """Classic PAT with only 'read:user' scope (no 'repo') is rejected."""
        with respx.mock:
            _mock_user_ok()
            _mock_repo(oauth_scopes="read:user, gist")
            with pytest.raises(InsufficientScopeError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"
        assert "repo" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Missing scope — permission checks
# ---------------------------------------------------------------------------


class TestMissingScope:
    def test_raises_when_push_permission_missing(self) -> None:
        """Token can read the repo but cannot write (no branch creation or PR opening)."""
        with respx.mock:
            _mock_user_ok()
            _mock_repo(permissions={"admin": False, "push": False, "pull": True})
            with pytest.raises(InsufficientScopeError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"
        assert "Contents: Write" in str(exc_info.value)

    def test_raises_when_pull_permission_missing(self) -> None:
        """Token has no read access to the repo at all."""
        with respx.mock:
            _mock_user_ok()
            _mock_repo(permissions={"admin": False, "push": False, "pull": False})
            with pytest.raises(InsufficientScopeError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"
        assert "Contents: Read" in str(exc_info.value)

    def test_raises_on_404_repo(self) -> None:
        """Repository not visible — fine-grained token not scoped to it."""
        with respx.mock:
            _mock_user_ok()
            respx.get(f"{GITHUB_API}/repos/{REPO}").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            with pytest.raises(InsufficientScopeError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"
        assert "404" in str(exc_info.value)

    def test_raises_on_403_repo(self) -> None:
        """Repository forbidden — token exists but lacks scope for this repo."""
        with respx.mock:
            _mock_user_ok()
            respx.get(f"{GITHUB_API}/repos/{REPO}").mock(
                return_value=httpx.Response(403, json={"message": "Forbidden"})
            )
            with pytest.raises(InsufficientScopeError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INSUFFICIENT_SCOPE"
        assert "403" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Expired / invalid token
# ---------------------------------------------------------------------------


class TestExpiredToken:
    def test_raises_when_user_endpoint_returns_401(self) -> None:
        """Primary detection path: /user returns 401 (bad credentials)."""
        with respx.mock:
            respx.get(f"{GITHUB_API}/user").mock(
                return_value=httpx.Response(401, json={"message": "Bad credentials"})
            )
            with pytest.raises(InvalidTokenError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INVALID_TOKEN"
        assert "401" in str(exc_info.value)

    def test_raises_when_repo_endpoint_returns_401(self) -> None:
        """/user succeeds but /repos returns 401 (e.g., token revoked mid-call)."""
        with respx.mock:
            _mock_user_ok()
            respx.get(f"{GITHUB_API}/repos/{REPO}").mock(
                return_value=httpx.Response(401, json={"message": "Bad credentials"})
            )
            with pytest.raises(InvalidTokenError) as exc_info:
                validate_token_scopes(TOKEN, REPO)

        assert exc_info.value.code == "INVALID_TOKEN"

    def test_unexpected_5xx_from_user_propagates(self) -> None:
        """GitHub API 5xx errors propagate as HTTPStatusError, not our custom errors."""
        with respx.mock:
            respx.get(f"{GITHUB_API}/user").mock(
                return_value=httpx.Response(500, json={"message": "Internal Server Error"})
            )
            with pytest.raises(httpx.HTTPStatusError):
                validate_token_scopes(TOKEN, REPO)

    def test_unexpected_5xx_from_repo_propagates(self) -> None:
        """GitHub API 5xx on /repos propagates as HTTPStatusError."""
        with respx.mock:
            _mock_user_ok()
            respx.get(f"{GITHUB_API}/repos/{REPO}").mock(
                return_value=httpx.Response(503, json={"message": "Service Unavailable"})
            )
            with pytest.raises(httpx.HTTPStatusError):
                validate_token_scopes(TOKEN, REPO)

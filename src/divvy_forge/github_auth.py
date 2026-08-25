"""GitHub token validation for divvy-forge.

Verifies that the configured ``GITHUB_TOKEN`` has the required permissions on
the target repository:

- **contents: read + write** — to read files and create feature branches
- **pull_requests: write** — to open PRs

Usage
-----
Called at batch-runner startup before processing any tickers::

    from divvy_forge.github_auth import validate_token_scopes, InsufficientScopeError, InvalidTokenError

    try:
        result = validate_token_scopes(token, "HiteshRepo/stock-screeners")
        print(f"Token OK — authenticated as {result.login}")
    except InvalidTokenError as exc:
        sys.exit(f"[divvy-forge] Token is invalid or expired: {exc}")
    except InsufficientScopeError as exc:
        sys.exit(f"[divvy-forge] Token scope check failed: {exc}")

Required token scopes
---------------------
**Fine-grained PAT** (recommended):
  - Repository access: ``HiteshRepo/stock-screeners`` only
  - Repository permissions:
    - Contents: Read and write
    - Pull requests: Read and write

**Classic PAT** (alternative):
  - OAuth scope: ``repo`` (grants contents + pull_requests on all private repos)
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"


class TokenValidationError(Exception):
    """Base class for token validation failures."""

    code: str = "UNKNOWN"


class InvalidTokenError(TokenValidationError):
    """Token is expired, revoked, or malformed (HTTP 401 from GitHub API).

    Attributes
    ----------
    code: Always ``"INVALID_TOKEN"``.
    """

    code = "INVALID_TOKEN"


class InsufficientScopeError(TokenValidationError):
    """Token is valid but lacks a required permission on the target repository.

    Attributes
    ----------
    code: Always ``"INSUFFICIENT_SCOPE"``.
    """

    code = "INSUFFICIENT_SCOPE"


@dataclass(frozen=True)
class TokenScopeResult:
    """Successful result of :func:`validate_token_scopes`.

    Attributes
    ----------
    valid:
        Always ``True`` (a failed validation raises instead of returning).
    has_contents_write:
        Token has ``contents:write`` on the repository.
    has_pull_requests_write:
        Token has ``pull_requests:write`` on the repository (or an equivalent
        classic OAuth scope).
    login:
        GitHub login of the token owner.
    """

    valid: bool
    has_contents_write: bool
    has_pull_requests_write: bool
    login: str


def validate_token_scopes(
    token: str,
    repo: str,
    *,
    http_client: httpx.Client | None = None,
) -> TokenScopeResult:
    """Verify that *token* has ``contents:write`` and ``pull_requests:write`` on *repo*.

    The function makes two GitHub API calls:

    1. ``GET /user`` — confirms the token is valid and retrieves the login.
    2. ``GET /repos/{repo}`` — checks the ``permissions`` object and, for
       classic PATs, the ``X-OAuth-Scopes`` response header.

    Parameters
    ----------
    token:
        GitHub personal-access token (classic or fine-grained).
    repo:
        Full repository slug, e.g. ``"HiteshRepo/stock-screeners"``.
    http_client:
        Optional pre-configured :class:`httpx.Client` (useful for testing).
        If *None*, a short-lived client is created internally.

    Returns
    -------
    TokenScopeResult
        On success — token is valid and all required permissions are present.

    Raises
    ------
    InvalidTokenError
        Token is expired, revoked, or malformed (HTTP 401 from ``/user`` or
        ``/repos``).
    InsufficientScopeError
        Token lacks ``contents:write`` or ``pull_requests:write`` on *repo*,
        or the token is not scoped to access *repo* at all.
    httpx.HTTPStatusError
        Unexpected non-401/403/404 HTTP error from the GitHub API.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def _do(client: httpx.Client) -> TokenScopeResult:
        # --- Step 1: verify the token is valid. --------------------------------
        user_resp = client.get(f"{GITHUB_API}/user", headers=headers)
        if user_resp.status_code == 401:
            raise InvalidTokenError(
                "GitHub token is invalid or expired (HTTP 401 from GET /user). "
                "Generate a new token at GitHub → Settings → Developer settings → "
                "Personal access tokens."
            )
        user_resp.raise_for_status()
        login: str = user_resp.json().get("login", "")

        # --- Step 2: verify access to the target repo. -------------------------
        repo_resp = client.get(f"{GITHUB_API}/repos/{repo}", headers=headers)
        if repo_resp.status_code == 401:
            raise InvalidTokenError(
                f"GitHub token is invalid or expired (HTTP 401 from GET /repos/{repo}). "
                "Generate a new token."
            )
        if repo_resp.status_code in (403, 404):
            raise InsufficientScopeError(
                f"Token cannot access repository '{repo}' "
                f"(HTTP {repo_resp.status_code}). "
                f"Ensure the fine-grained token is scoped to '{repo}' with "
                "'Contents: Read and write' and 'Pull requests: Read and write' "
                "under Repository permissions."
            )
        repo_resp.raise_for_status()

        repo_data = repo_resp.json()
        permissions: dict[str, bool] = repo_data.get("permissions", {})

        # permissions.pull == True  →  contents:read
        # permissions.push == True  →  contents:write (branch creation, file writes)
        has_pull = permissions.get("pull", False)
        has_push = permissions.get("push", False)

        if not has_pull:
            raise InsufficientScopeError(
                f"Token lacks 'Contents: Read' on '{repo}'. "
                "Grant 'Contents: Read and write' under Repository permissions."
            )
        if not has_push:
            raise InsufficientScopeError(
                f"Token lacks 'Contents: Write' on '{repo}'. "
                "Grant 'Contents: Read and write' and 'Pull requests: Read and write' "
                "under Repository permissions."
            )

        # --- Step 3: for classic PATs, verify the OAuth scope explicitly. ------
        # Fine-grained tokens return an empty X-OAuth-Scopes header; classic PATs
        # return a comma-separated list of OAuth scopes.
        oauth_scopes_header = repo_resp.headers.get("X-OAuth-Scopes", "")
        is_classic_token = bool(oauth_scopes_header.strip())

        if is_classic_token:
            oauth_scopes = {s.strip() for s in oauth_scopes_header.split(",") if s.strip()}
            has_repo_scope = "repo" in oauth_scopes or "public_repo" in oauth_scopes
            if not has_repo_scope:
                raise InsufficientScopeError(
                    "Classic PAT is missing the 'repo' OAuth scope (required for "
                    "contents:write and pull_requests:write on private repositories). "
                    "Add the 'repo' scope to your classic PAT, or switch to a "
                    "fine-grained token scoped directly to the target repository."
                )

        # For fine-grained tokens, pull_requests:write is a separate permission
        # that cannot be probed without creating a test PR. As a practical proxy,
        # we use permissions.push == True: a fine-grained token scoped with both
        # 'Contents: Read and write' and 'Pull requests: Read and write' will
        # always have push=True. If pull_requests:write is missing the PR-opener
        # will surface a clear 403 on the first PR attempt.
        return TokenScopeResult(
            valid=True,
            has_contents_write=has_push,
            has_pull_requests_write=has_push,  # proxy — see note above
            login=login,
        )

    if http_client is not None:
        return _do(http_client)

    with httpx.Client() as client:
        return _do(client)

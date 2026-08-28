"""github-pr-opener: MCP tool server for opening GitHub PRs on HiteshRepo/stock-screeners.

Exposes four tools over stdio MCP transport:
- check_existing_pr(ticker, date) -> str | None: detect duplicate open PRs
- create_branch(ticker, date) -> str: create feature branch divvy-review/<ticker>/<date>
- commit_diff(branch, path, diff, base_sha) -> None: apply diff as file commit on branch
- open_pr(ticker, date, proposal_json, pr_body) -> dict: orchestrate full PR creation

Run as a standalone server::

    python -m divvy_forge.github_pr_opener

or via the registered entry-point::

    github-pr-opener
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
TARGET_REPO = "HiteshRepo/stock-screeners"
BRANCH_PREFIX = "divvy-review"
WATCHLIST_PATH = "dividend/data/watchlist.md"

# PR title: "Divvy Review: {TICKER} ({YYYY-MM-DD})"
_PR_TITLE_FMT = "Divvy Review: {ticker} ({date})"


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class PrOpenerError(Exception):
    """Base class for github-pr-opener errors."""

    code: str = "UNKNOWN"


class InsufficientScopeError(PrOpenerError):
    """GitHub token lacks a required permission on the target repository.

    Attributes
    ----------
    code: Always ``"INSUFFICIENT_SCOPE"``.
    """

    code = "INSUFFICIENT_SCOPE"


class BranchCreationError(PrOpenerError):
    """Feature branch could not be created (e.g. it already exists).

    Attributes
    ----------
    code: Always ``"BRANCH_CREATION_FAILED"``.
    """

    code = "BRANCH_CREATION_FAILED"


class CommitError(PrOpenerError):
    """File commit failed (e.g. file not found on branch, merge conflict).

    Attributes
    ----------
    code: Always ``"COMMIT_FAILED"``.
    """

    code = "COMMIT_FAILED"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class MergedProposal:
    """Coordinator output used to open a PR.

    Constructed from :class:`~divvy_forge.coordinator.CoordinatorResult`
    by the batch runner before calling :func:`_open_pr`.
    """

    ticker: str
    date: str
    merge_reasoning: str
    fundamentals: dict[str, Any] | None
    risk: dict[str, Any] | None
    changed_fields: list[str] = field(default_factory=list)
    diff: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergedProposal":
        return cls(
            ticker=data["ticker"],
            date=data["date"],
            merge_reasoning=data.get("merge_reasoning", ""),
            fundamentals=data.get("fundamentals"),
            risk=data.get("risk"),
            changed_fields=data.get("changed_fields", []),
            diff=data.get("diff", ""),
        )


@dataclass
class PrResult:
    """Result of a :func:`_open_pr` call.

    Attributes
    ----------
    pr_url:
        HTML URL of the created (or existing) pull request.
    pr_number:
        PR number in the target repository (``0`` when ``already_exists`` is
        ``True`` and the number is not re-fetched).
    already_exists:
        ``True`` when a PR with the same title was found and returned instead
        of creating a duplicate.
    branch:
        Head branch of the PR (``divvy-review/<ticker>/<date>``).
    """

    pr_url: str
    pr_number: int
    already_exists: bool
    branch: str


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


def _branch_name(ticker: str, date: str) -> str:
    return f"{BRANCH_PREFIX}/{ticker.upper()}/{date}"


def _pr_title(ticker: str, date: str) -> str:
    return _PR_TITLE_FMT.format(ticker=ticker.upper(), date=date)


def _apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff string to *original* and return the new content.

    Handles the common case produced by the coordinator: hunks with context
    lines (space-prefixed), removed lines (``-``), and added lines (``+``).
    Lines starting with ``---`` / ``+++`` (file headers) are skipped.

    If *diff* is empty or contains no hunks the original is returned
    unchanged.

    Parameters
    ----------
    original:
        Full file content before the patch is applied.
    diff:
        Unified diff string (as produced by ``diff -u`` or the coordinator).
    """
    if not diff.strip():
        return original

    # Normalise: ensure every original line ends with a newline
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    result: list[str] = []
    i = 0  # pointer into *lines* (0-based)

    for raw_line in diff.splitlines():
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue

        if raw_line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", raw_line)
            if m:
                old_start = int(m.group(1)) - 1  # convert to 0-based
                # Copy untouched lines from the original up to this hunk
                while i < old_start:
                    result.append(lines[i])
                    i += 1
            continue

        if raw_line.startswith("-"):
            # Remove this line from the original
            i += 1
        elif raw_line.startswith("+"):
            # Insert the new line
            content = raw_line[1:]
            if not content.endswith("\n"):
                content += "\n"
            result.append(content)
        else:
            # Context line — keep the original
            if i < len(lines):
                result.append(lines[i])
            i += 1

    # Append any trailing lines from the original not covered by hunks
    while i < len(lines):
        result.append(lines[i])
        i += 1

    return "".join(result)


# ---------------------------------------------------------------------------
# Tool logic (testable without MCP layer)
# ---------------------------------------------------------------------------


def _check_existing_pr(
    ticker: str,
    date: str,
    http_client: httpx.Client | None = None,
) -> str | None:
    """Return the URL of an open PR matching *ticker* + *date*, or ``None``.

    Searches open PRs in :data:`TARGET_REPO` by title pattern
    ``"Divvy Review: {TICKER} ({date})"``.

    Parameters
    ----------
    ticker:
        Stock ticker symbol (case-insensitive).
    date:
        Review date string (``YYYY-MM-DD``).
    http_client:
        Optional pre-configured :class:`httpx.Client` for testing.

    Returns
    -------
    str | None
        The ``html_url`` of the first matching PR, or ``None``.

    Raises
    ------
    InsufficientScopeError
        On HTTP 403 (token lacks ``pull_requests:read``).
    httpx.HTTPStatusError
        On unexpected HTTP errors.
    """
    title = _pr_title(ticker, date)

    def _do(client: httpx.Client) -> str | None:
        resp = client.get(
            f"{GITHUB_API}/repos/{TARGET_REPO}/pulls",
            headers=_github_headers(),
            params={"state": "open", "per_page": 100},
        )
        if resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks pull_requests:read on '{TARGET_REPO}' (HTTP 403). "
                "Grant 'Pull requests: Read and write' under Repository permissions."
            )
        resp.raise_for_status()
        for pr in resp.json():
            if pr.get("title", "") == title:
                return pr["html_url"]
        return None

    if http_client is not None:
        return _do(http_client)
    with httpx.Client() as client:
        return _do(client)


def _create_branch(
    ticker: str,
    date: str,
    http_client: httpx.Client | None = None,
) -> str:
    """Create branch ``divvy-review/<ticker>/<date>`` from ``main`` HEAD.

    Returns the branch name on success.

    Raises
    ------
    BranchCreationError
        If the branch already exists (HTTP 422) in the target repository.
    InsufficientScopeError
        On HTTP 403 (token lacks ``contents:write``).
    httpx.HTTPStatusError
        On unexpected HTTP errors.
    """
    branch = _branch_name(ticker, date)

    def _do(client: httpx.Client) -> str:
        # Resolve main HEAD SHA
        ref_resp = client.get(
            f"{GITHUB_API}/repos/{TARGET_REPO}/git/ref/heads/main",
            headers=_github_headers(),
        )
        if ref_resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks contents:read on '{TARGET_REPO}' (HTTP 403). "
                "Grant 'Contents: Read and write' under Repository permissions."
            )
        ref_resp.raise_for_status()
        sha: str = ref_resp.json()["object"]["sha"]

        # Create the feature branch
        create_resp = client.post(
            f"{GITHUB_API}/repos/{TARGET_REPO}/git/refs",
            headers=_github_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        if create_resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks contents:write on '{TARGET_REPO}' (HTTP 403). "
                "Grant 'Contents: Read and write' under Repository permissions."
            )
        if create_resp.status_code == 422:
            raise BranchCreationError(
                f"Branch '{branch}' already exists in '{TARGET_REPO}'."
            )
        create_resp.raise_for_status()
        return branch

    if http_client is not None:
        return _do(http_client)
    with httpx.Client() as client:
        return _do(client)


def _commit_diff(
    branch: str,
    path: str,
    diff: str,
    base_sha: str,
    http_client: httpx.Client | None = None,
) -> None:
    """Apply *diff* to *path* on *branch* as a new commit.

    Fetches the current file content from *branch*, applies the unified diff
    to produce new content, then writes it back via the GitHub contents API.

    Parameters
    ----------
    branch:
        Target branch (e.g. ``"divvy-review/INFY/2024-01-15"``).
    path:
        Repo-relative file path (e.g. ``"dividend/data/watchlist.md"``).
    diff:
        Unified diff string produced by the coordinator.
    base_sha:
        Hint for the file blob SHA (used as fallback if the GET response does
        not return one). The value from the GET response takes precedence.
    http_client:
        Optional pre-configured :class:`httpx.Client` for testing.

    Raises
    ------
    CommitError
        If the file is not found on the branch (HTTP 404) or if a write
        conflict occurs (HTTP 409).
    InsufficientScopeError
        On HTTP 403 (token lacks ``contents:write``).
    httpx.HTTPStatusError
        On unexpected HTTP errors.
    """

    def _do(client: httpx.Client) -> None:
        # Fetch current file content and blob SHA from the branch
        get_resp = client.get(
            f"{GITHUB_API}/repos/{TARGET_REPO}/contents/{path}",
            headers=_github_headers(),
            params={"ref": branch},
        )
        if get_resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks contents:read on '{TARGET_REPO}' (HTTP 403)."
            )
        if get_resp.status_code == 404:
            raise CommitError(
                f"File '{path}' not found on branch '{branch}' in '{TARGET_REPO}'."
            )
        get_resp.raise_for_status()

        file_data = get_resp.json()
        blob_sha: str = file_data.get("sha") or base_sha
        raw_b64: str = file_data.get("content", "")
        current_content = base64.b64decode(raw_b64).decode("utf-8")

        # Apply the unified diff
        new_content = _apply_unified_diff(current_content, diff)
        new_content_b64 = base64.b64encode(new_content.encode("utf-8")).decode("ascii")

        # Derive a readable commit message from the branch name
        # e.g. "divvy-review/INFY/2024-01-15" → "INFY"
        parts = branch.split("/")
        ticker_label = parts[1] if len(parts) >= 2 else branch

        put_resp = client.put(
            f"{GITHUB_API}/repos/{TARGET_REPO}/contents/{path}",
            headers=_github_headers(),
            json={
                "message": f"divvy-review: update {path.split('/')[-1]} for {ticker_label}",
                "content": new_content_b64,
                "sha": blob_sha,
                "branch": branch,
            },
        )
        if put_resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks contents:write on '{TARGET_REPO}' (HTTP 403). "
                "Grant 'Contents: Read and write' under Repository permissions."
            )
        if put_resp.status_code == 409:
            raise CommitError(
                f"Conflict committing to '{path}' on branch '{branch}'. "
                "The branch may have diverged from main."
            )
        put_resp.raise_for_status()

    if http_client is not None:
        return _do(http_client)
    with httpx.Client() as client:
        return _do(client)


def format_pr_body(proposal: MergedProposal) -> str:
    """Format a GitHub PR body with full traceability for a divvy review.

    The body includes:

    - Ticker/date header
    - Proposed changed fields
    - ``merge_reasoning`` from the coordinator
    - Inline diff (fenced code block)
    - Collapsed ``<details>`` sections for fundamentals and risk findings
    - Cited sources from the risk assessment

    Parameters
    ----------
    proposal:
        Merged coordinator output for the ticker being reviewed.

    Returns
    -------
    str
        Markdown-formatted PR body.
    """
    lines: list[str] = []

    lines.append(f"## Divvy Review: {proposal.ticker} ({proposal.date})")
    lines.append("")

    # ---- Proposed changes ---------------------------------------------------
    lines.append("### Proposed Changes")
    if proposal.changed_fields:
        for f in proposal.changed_fields:
            lines.append(f"- `{f}`")
    else:
        lines.append("- _(no fields changed)_")
    lines.append("")

    # ---- Coordinator reasoning ----------------------------------------------
    lines.append("### Coordinator Reasoning")
    lines.append(proposal.merge_reasoning or "_No reasoning provided._")
    lines.append("")

    # ---- Diff ---------------------------------------------------------------
    lines.append("### Diff")
    lines.append("```diff")
    lines.append(proposal.diff.strip() if proposal.diff.strip() else "# (empty diff — no changes proposed)")
    lines.append("```")
    lines.append("")

    # ---- Fundamentals findings (collapsed) ----------------------------------
    if proposal.fundamentals:
        f = proposal.fundamentals
        lines.append("<details>")
        lines.append("<summary>Fundamentals Analysis</summary>")
        lines.append("")
        lines.append(f"**Yield Trend:** {f.get('yield_trend') or 'N/A'}")
        lines.append(f"**Payout Sustainability:** {f.get('payout_sustainability') or 'N/A'}")
        suggested = f.get("suggested_yield_update")
        if suggested is not None:
            lines.append(f"**Suggested Yield Update:** {float(suggested):.2f}%")
        reasoning = f.get("reasoning")
        if reasoning:
            lines.append("")
            lines.append(reasoning)
        if f.get("status") == "error":
            lines.append("")
            lines.append(f"> **Error:** {f.get('error_message') or 'unknown error'}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---- Risk findings (collapsed) ------------------------------------------
    if proposal.risk:
        r = proposal.risk
        risk_level = r.get("risk_level", "unknown")
        lines.append("<details>")
        lines.append("<summary>Dividend-Cut Risk Assessment</summary>")
        lines.append("")
        lines.append(f"**Risk Level:** {risk_level}")
        signals: list[str] = r.get("signals", [])
        if signals:
            lines.append("")
            lines.append("**Signals:**")
            for signal in signals:
                lines.append(f"- {signal}")
        sources: list[Any] = r.get("sources", [])
        if sources:
            lines.append("")
            lines.append("**Sources:**")
            for src in sources:
                if isinstance(src, dict):
                    title = src.get("title", "Source")
                    url = src.get("url", "")
                    lines.append(f"- [{title}]({url})" if url else f"- {title}")
                else:
                    lines.append(f"- {src}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Generated by [divvy-forge](https://github.com/HiteshRepo/divvy-forge) "
        "· Review and merge to apply changes._"
    )

    return "\n".join(lines)


def _open_pr(
    ticker: str,
    date: str,
    proposal: MergedProposal,
    pr_body: str,
    http_client: httpx.Client | None = None,
) -> PrResult:
    """Orchestrate: check-existing → create-branch → commit-diff → create-PR.

    All GitHub API calls share a single :class:`httpx.Client` instance
    (created internally when *http_client* is ``None``).

    Parameters
    ----------
    ticker:
        Stock ticker symbol.
    date:
        Review date (``YYYY-MM-DD``).
    proposal:
        Merged coordinator output containing the diff and reasoning.
    pr_body:
        Pre-formatted PR body markdown (typically from :func:`format_pr_body`).
    http_client:
        Optional pre-configured :class:`httpx.Client` for testing.

    Returns
    -------
    PrResult
        PR URL, number, branch, and whether the PR already existed.

    Raises
    ------
    InsufficientScopeError
        If the GitHub token lacks required permissions at any step.
    BranchCreationError
        If the feature branch already exists (idempotency failure).
    CommitError
        If the file commit fails (file not found on branch, conflict).
    httpx.HTTPStatusError
        On unexpected HTTP errors from the GitHub API.
    """

    def _execute(client: httpx.Client) -> PrResult:
        # Step 1 — check for duplicate PR
        existing_url = _check_existing_pr(ticker, date, http_client=client)
        if existing_url:
            return PrResult(
                pr_url=existing_url,
                pr_number=0,
                already_exists=True,
                branch=_branch_name(ticker, date),
            )

        branch = _branch_name(ticker, date)

        # Step 2 — create feature branch
        _create_branch(ticker, date, http_client=client)

        # Step 3 — commit the diff (skip if empty)
        if proposal.diff.strip():
            _commit_diff(branch, WATCHLIST_PATH, proposal.diff, base_sha="", http_client=client)

        # Step 4 — open the pull request
        title = _pr_title(ticker, date)
        pr_resp = client.post(
            f"{GITHUB_API}/repos/{TARGET_REPO}/pulls",
            headers=_github_headers(),
            json={
                "title": title,
                "body": pr_body,
                "head": branch,
                "base": "main",
            },
        )
        if pr_resp.status_code == 403:
            raise InsufficientScopeError(
                f"Token lacks pull_requests:write on '{TARGET_REPO}' (HTTP 403). "
                "Grant 'Pull requests: Read and write' under Repository permissions."
            )
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()
        return PrResult(
            pr_url=pr_data["html_url"],
            pr_number=pr_data["number"],
            already_exists=False,
            branch=branch,
        )

    if http_client is not None:
        return _execute(http_client)
    with httpx.Client() as client:
        return _execute(client)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("github-pr-opener")


@mcp.tool()
def check_existing_pr(ticker: str, date: str) -> str | None:
    """Check whether an open PR for *ticker* on *date* already exists.

    Parameters
    ----------
    ticker:
        Stock ticker symbol (e.g. ``"INFY"``).
    date:
        Review date string in ``YYYY-MM-DD`` format.

    Returns
    -------
    str | None
        The ``html_url`` of the existing PR, or ``null`` if none found.
    """
    return _check_existing_pr(ticker, date)


@mcp.tool()
def create_branch(ticker: str, date: str) -> str:
    """Create feature branch ``divvy-review/<ticker>/<date>`` from ``main``.

    Parameters
    ----------
    ticker:
        Stock ticker symbol (e.g. ``"INFY"``).
    date:
        Review date string in ``YYYY-MM-DD`` format.

    Returns
    -------
    str
        The created branch name.
    """
    return _create_branch(ticker, date)


@mcp.tool()
def commit_diff(branch: str, path: str, diff: str, base_sha: str) -> None:
    """Apply *diff* to *path* on *branch* as a new commit.

    Parameters
    ----------
    branch:
        Target branch (e.g. ``"divvy-review/INFY/2024-01-15"``).
    path:
        Repo-relative path of the file to patch.
    diff:
        Unified diff string.
    base_sha:
        SHA hint for the file blob being replaced.
    """
    _commit_diff(branch, path, diff, base_sha)


@mcp.tool()
def open_pr(ticker: str, date: str, proposal_json: str, pr_body: str) -> dict:
    """Orchestrate the full PR creation workflow for a divvy review.

    Parameters
    ----------
    ticker:
        Stock ticker symbol.
    date:
        Review date (``YYYY-MM-DD``).
    proposal_json:
        JSON-serialized :class:`MergedProposal` dict.
    pr_body:
        Pre-formatted PR body markdown.

    Returns
    -------
    dict
        Serialised :class:`PrResult` with ``pr_url``, ``pr_number``,
        ``already_exists``, and ``branch``.
    """
    proposal = MergedProposal.from_dict(json.loads(proposal_json))
    result = _open_pr(ticker, date, proposal, pr_body)
    return asdict(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

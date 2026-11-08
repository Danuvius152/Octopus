"""GitHub connector MCP server (connectors.md Phase B / §6).

Spawned as `python -m server.mcp_servers.connectors.github` with the shared
callback env + OCTOPUS_INSTALLATION_ID. Each tool fetches the installation's
token from the host, calls the GitHub REST API, projects the response to the
fields the model needs, and caps the result size. 401 → mark the installation
needs-reconnect and tell the model to ask the user; only safe reads retry.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Make `server` importable regardless of the spawning cwd (mirrors viewer.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    ConnectorTokenError,
    request_failure,
    request_with_retry,
    response_failure,
    retry_after,
    retry_delay,
    to_text,
    token_failure_message,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s github-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-github")
ctx = ConnectorContext()

_API = "https://api.github.com"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _github_retry_delay(response: httpx.Response, attempt: int) -> float | None:
    """GitHub primary/secondary limits can use 403 as well as 429."""
    limited = response.status_code == 429 or (
        response.status_code == 403 and (
            "Retry-After" in response.headers
            or response.headers.get("X-RateLimit-Remaining") == "0"
            or "rate limit" in response.text.lower()
        )
    )
    if not limited:
        return retry_delay(response, attempt)
    delay = retry_after(response)
    if response.headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset = float(response.headers["X-RateLimit-Reset"])
            reset_delay = max(0.0, reset - time.time()) if math.isfinite(reset) else 60.0
        except (KeyError, ValueError, OverflowError):
            reset_delay = 60.0
        delay = max(delay or 0.0, reset_delay)
    return max(float(2**attempt), delay if delay is not None else 60.0)


async def _gh(
    method: str, path: str, *, retry_safe: bool = False,
    operation: str = "", check: str = "Check the original GitHub resource.", **kw: Any,
) -> tuple[Any, str | None]:
    """Authenticated GitHub call. Returns (parsed_body, None) on success or
    ({}, error_message) on failure — the error string is itself a fine tool
    result.

    The failure body is an empty dict rather than None on purpose. Every
    caller returns early on a non-None error, so the two are interchangeable
    for correct code — but with None, a caller that forgot the `if err:` guard
    raised AttributeError deep inside a tool, while an empty dict degrades to
    an empty result. It also lets the return type drop the `| None`, so a
    type checker stops reporting the 20-odd `data.get(...)` calls that are
    already guarded."""
    try:
        info = await asyncio.to_thread(ctx.token_info)
    except ConnectorTokenError as token_error:
        return {}, token_failure_message(token_error)
    headers = {**_GH_HEADERS, "Authorization": f"Bearer {info['access_token']}"}
    url = path if path.startswith("http") else f"{_API}{path}"
    operation = operation or f"{method} {url}"
    r, error = await request_with_retry(
        method, url, retry_safe=retry_safe, operation=operation,
        check=check, delay_for_response=_github_retry_delay, headers=headers, **kw,
    )
    if error:
        return {}, error
    assert r is not None
    if r.status_code == 401:
        authorization = await asyncio.to_thread(ctx.rejection_message, info)
        return {}, "Error: rejected: " + authorization.removeprefix("Error: ")
    error = response_failure(
        r, retry_safe=retry_safe, operation=operation, check=check,
        rejected_statuses=frozenset({400, 403, 404, 422}),
    )
    if error:
        return {}, error
    try:
        return (r.json() if r.content else {}), None
    except ValueError:
        return {}, request_failure("Invalid GitHub JSON response", retry_safe=retry_safe, operation=operation, check=check)


def _issue_brief(it: dict) -> dict:
    repo = ""
    if it.get("repository_url"):
        repo = it["repository_url"].split("/repos/", 1)[-1]
    return {
        "repo": repo,
        "number": it.get("number"),
        "title": it.get("title"),
        "state": it.get("state"),
        "is_pr": "pull_request" in it,
        "url": it.get("html_url"),
    }


@mcp.tool(name="search_issues")
async def search_issues(query: str, limit: int = 20) -> str:
    """Search issues and pull requests with GitHub search syntax
    (e.g. 'repo:owner/name is:open label:bug'). Returns brief matches."""
    data, err = await _gh(
        "GET", "/search/issues", retry_safe=True, params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    items = [_issue_brief(it) for it in data.get("items", [])]
    return truncate(to_text({"total": data.get("total_count"), "items": items}))


@mcp.tool(name="get_issue")
async def get_issue(repo: str, number: int) -> str:
    """Full issue: title, state, author, labels, and body. `repo` is
    'owner/name'."""
    data, err = await _gh("GET", f"/repos/{repo}/issues/{number}", retry_safe=True)
    if err:
        return err
    out = {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "author": (data.get("user") or {}).get("login"),
        "labels": [lbl.get("name") for lbl in data.get("labels", [])],
        "comments": data.get("comments"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }
    return truncate(to_text(out))


@mcp.tool(name="get_pr")
async def get_pr(repo: str, number: int) -> str:
    """Full pull request: title, state, head→base branches, merge state,
    and body. `repo` is 'owner/name'."""
    data, err = await _gh("GET", f"/repos/{repo}/pulls/{number}", retry_safe=True)
    if err:
        return err
    out = {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "merged": data.get("merged"),
        "draft": data.get("draft"),
        "head": (data.get("head") or {}).get("ref"),
        "base": (data.get("base") or {}).get("ref"),
        "author": (data.get("user") or {}).get("login"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }
    return truncate(to_text(out))


@mcp.tool(name="list_repos")
async def list_repos(limit: int = 30) -> str:
    """The authenticated user's repositories, most-recently-updated first."""
    data, err = await _gh(
        "GET",
        "/user/repos",
        retry_safe=True,
        params={"per_page": min(limit, 100), "sort": "updated"},
    )
    if err:
        return err
    repos = [
        {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "description": r.get("description"),
            "url": r.get("html_url"),
        }
        for r in data
    ]
    return truncate(to_text(repos))


@mcp.tool(name="get_file")
async def get_file(repo: str, path: str, ref: str | None = None) -> str:
    """Read a file's text from a repo. `repo` is 'owner/name'; `ref` is an
    optional branch/tag/sha."""
    params = {"ref": ref} if ref else None
    data, err = await _gh("GET", f"/repos/{repo}/contents/{path}", retry_safe=True, params=params)
    if err:
        return err
    if isinstance(data, list):  # a directory
        return truncate(to_text([{"name": e.get("name"), "type": e.get("type")} for e in data]))
    if data.get("encoding") == "base64" and data.get("content") is not None:
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", "replace")
        except Exception as e:  # binary or malformed
            return f"Error: could not decode {path}: {e}"
        return truncate(text)
    return truncate(to_text(data))


@mcp.tool(name="search_code")
async def search_code(query: str, limit: int = 20) -> str:
    """Search code with GitHub code-search syntax (e.g. 'addEventListener
    repo:owner/name'). Returns matching file paths."""
    data, err = await _gh(
        "GET", "/search/code", retry_safe=True, params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    hits = [
        {
            "repo": (it.get("repository") or {}).get("full_name"),
            "path": it.get("path"),
            "url": it.get("html_url"),
        }
        for it in data.get("items", [])
    ]
    return truncate(to_text({"total": data.get("total_count"), "items": hits}))


@mcp.tool(name="create_issue")
async def create_issue(repo: str, title: str, body: str = "") -> str:
    """Open a new issue. This WRITES to GitHub — explain what you're filing
    before calling. `repo` is 'owner/name'."""
    data, err = await _gh(
        "POST", f"/repos/{repo}/issues", json={"title": title, "body": body},
        operation=f"create_issue repo={repo!r} title={title!r}",
        check=f"Check issues in https://github.com/{repo}/issues for the original title and body.",
    )
    if err:
        return err
    return to_text(
        {"created": True, "number": data.get("number"), "url": data.get("html_url")}
    )


@mcp.tool(name="comment")
async def comment(repo: str, number: int, body: str) -> str:
    """Comment on an issue or PR. This WRITES to GitHub. `repo` is
    'owner/name'; `number` is the issue/PR number."""
    data, err = await _gh(
        "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body},
        operation=f"comment repo={repo!r} number={number} body={body!r}",
        check=f"Check comments on https://github.com/{repo}/issues/{number} for the original text.",
    )
    if err:
        return err
    return to_text({"created": True, "url": data.get("html_url")})


if __name__ == "__main__":
    mcp.run()

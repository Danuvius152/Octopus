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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# Make `server` importable regardless of the spawning cwd (mirrors viewer.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    ConnectorTokenError,
    ResourceDecodeError,
    array_data,
    decode_response,
    logical_resource,
    object_data,
    request_failure,
    request_with_retry,
    response_failure,
    retry_after,
    retry_delay,
    text_data,
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
    operation: str = "", check: str = "Check the original GitHub resource.",
    decode: Callable[[Any], Any], **kw: Any,
) -> tuple[Any, str | None]:
    """Return a fully projected resource or an explicit tool error.

    The callback owns fallible resource parsing; tool-level formatting follows
    the single terminal sample and cannot mark malformed input as success.
    """
    with logical_resource(ctx) as result:
        try:
            info = await asyncio.to_thread(ctx.token_info)
        except ConnectorTokenError as token_error:
            result.error_code = "token"
            return {}, token_failure_message(token_error)
        headers = {**_GH_HEADERS, "Authorization": f"Bearer {info['access_token']}"}
        url = path if path.startswith("http") else f"{_API}{path}"
        operation = operation or f"{method} {url}"
        r, failure = await request_with_retry(
            method, url, retry_safe=retry_safe, operation=operation,
            check=check, delay_for_response=_github_retry_delay, headers=headers, **kw,
        )
        if failure:
            result.error_code = failure.code
            return {}, failure.message
        assert r is not None
        if r.status_code == 401:
            result.error_code = "auth"
            authorization = await asyncio.to_thread(ctx.rejection_message, info)
            return {}, "Error: rejected: " + authorization.removeprefix("Error: ")
        error = response_failure(
            r, retry_safe=retry_safe, operation=operation, check=check,
            rejected_statuses=frozenset({400, 403, 404, 422}),
        )
        if error:
            result.error_code = "http"
            return {}, error
        data, failure = decode_response(r, decode, provider="GitHub")
        if failure:
            result.error_code = failure.code
            return {}, request_failure(failure.message, retry_safe=retry_safe, operation=operation, check=check)
        result.error_code = None
        return data, None


def _issue_brief(value: Any) -> dict:
    it = object_data(value)
    repo = ""
    if it.get("repository_url"):
        repo = text_data(it["repository_url"]).split("/repos/", 1)[-1]
    return {
        "repo": repo,
        "number": it.get("number"),
        "title": it.get("title"),
        "state": it.get("state"),
        "is_pr": "pull_request" in it,
        "url": it.get("html_url"),
    }


def _issues(value: Any) -> dict:
    data = object_data(value)
    return {"total": data.get("total_count"), "items": [_issue_brief(it) for it in array_data(data.get("items", []))]}


@mcp.tool(name="search_issues")
async def search_issues(query: str, limit: int = 20) -> str:
    """Search issues and pull requests with GitHub search syntax
    (e.g. 'repo:owner/name is:open label:bug'). Returns brief matches."""
    data, err = await _gh(
        "GET", "/search/issues", retry_safe=True, decode=_issues, params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    return truncate(to_text(data))


def _issue(value: Any) -> Any:
    data = object_data(value)
    return {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "author": object_data(data.get("user") or {}).get("login"),
        "labels": [object_data(lbl).get("name") for lbl in array_data(data.get("labels", []))],
        "comments": data.get("comments"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }


@mcp.tool(name="get_issue")
async def get_issue(repo: str, number: int) -> str:
    """Full issue: title, state, author, labels, and body. `repo` is
    'owner/name'."""
    data, err = await _gh("GET", f"/repos/{repo}/issues/{number}", retry_safe=True, decode=_issue)
    if err:
        return err
    return truncate(to_text(data))


def _pr(value: Any) -> Any:
    data = object_data(value)
    return {
        "number": data.get("number"),
        "title": data.get("title"),
        "state": data.get("state"),
        "merged": data.get("merged"),
        "draft": data.get("draft"),
        "head": object_data(data.get("head") or {}).get("ref"),
        "base": object_data(data.get("base") or {}).get("ref"),
        "author": object_data(data.get("user") or {}).get("login"),
        "url": data.get("html_url"),
        "body": data.get("body") or "",
    }


@mcp.tool(name="get_pr")
async def get_pr(repo: str, number: int) -> str:
    """Full pull request: title, state, head→base branches, merge state,
    and body. `repo` is 'owner/name'."""
    data, err = await _gh("GET", f"/repos/{repo}/pulls/{number}", retry_safe=True, decode=_pr)
    if err:
        return err
    return truncate(to_text(data))


def _repos(value: Any) -> Any:
    data = value
    return [
        {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "description": r.get("description"),
            "url": r.get("html_url"),
        }
        for r in map(object_data, array_data(data))
    ]


@mcp.tool(name="list_repos")
async def list_repos(limit: int = 30) -> str:
    """The authenticated user's repositories, most-recently-updated first."""
    data, err = await _gh(
        "GET",
        "/user/repos",
        retry_safe=True, decode=_repos,
        params={"per_page": min(limit, 100), "sort": "updated"},
    )
    if err:
        return err
    return truncate(to_text(data))


def _file_content(data: Any) -> Any:
    if isinstance(data, list):
        return [{"name": e.get("name"), "type": e.get("type")} for e in map(object_data, data)]
    data = object_data(data)
    if data.get("encoding") == "base64" and data.get("content") is not None:
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        except (ValueError, TypeError) as error:
            raise ResourceDecodeError(f"could not decode file: {error}") from error
    return data


@mcp.tool(name="get_file")
async def get_file(repo: str, path: str, ref: str | None = None) -> str:
    """Read a file's text from a repo. `repo` is 'owner/name'; `path` is a raw
    repository-relative path (empty lists the root); `ref` is a branch/tag/sha."""
    repo_parts = repo.split("/")
    if len(repo_parts) != 2 or any(part in {"", ".", ".."} for part in repo_parts):
        return "Error: repo must be owner/name with two nonempty segments and no dot segments."
    path_parts = path.split("/")
    if path and any(part in {"", ".", ".."} for part in path_parts):
        return "Error: path must be repository-relative with no empty or dot segments."
    encoded_repo = "/".join(quote(part, safe="") for part in repo_parts)
    encoded_path = "/".join(quote(part, safe="") for part in path_parts)
    params = {"ref": ref} if ref else None
    data, err = await _gh("GET", f"/repos/{encoded_repo}/contents/{encoded_path}", retry_safe=True, decode=_file_content, params=params)
    if err:
        return err
    return truncate(to_text(data))


def _code_hits(value: Any) -> Any:
    data = object_data(value)
    hits = [
        {
            "repo": object_data(it.get("repository") or {}).get("full_name"),
            "path": it.get("path"),
            "url": it.get("html_url"),
        }
        for it in map(object_data, array_data(data.get("items", [])))
    ]
    return {"total": data.get("total_count"), "items": hits}


@mcp.tool(name="search_code")
async def search_code(query: str, limit: int = 20) -> str:
    """Search code with GitHub code-search syntax (e.g. 'addEventListener
    repo:owner/name'). Returns matching file paths."""
    data, err = await _gh(
        "GET", "/search/code", retry_safe=True, decode=_code_hits, params={"q": query, "per_page": min(limit, 50)}
    )
    if err:
        return err
    return truncate(to_text(data))


@mcp.tool(name="create_issue")
async def create_issue(repo: str, title: str, body: str = "") -> str:
    """Open a new issue. This WRITES to GitHub — explain what you're filing
    before calling. `repo` is 'owner/name'."""
    data, err = await _gh(
        "POST", f"/repos/{repo}/issues", decode=lambda value: {"created": True, "number": object_data(value).get("number"), "url": value.get("html_url")}, json={"title": title, "body": body},
        operation=f"create_issue repo={repo!r} title={title!r}",
        check=f"Check issues in https://github.com/{repo}/issues for the original title and body.",
    )
    if err:
        return err
    return to_text(data)


@mcp.tool(name="comment")
async def comment(repo: str, number: int, body: str) -> str:
    """Comment on an issue or PR. This WRITES to GitHub. `repo` is
    'owner/name'; `number` is the issue/PR number."""
    data, err = await _gh(
        "POST", f"/repos/{repo}/issues/{number}/comments", decode=lambda value: {"created": True, "url": object_data(value).get("html_url")}, json={"body": body},
        operation=f"comment repo={repo!r} number={number} body={body!r}",
        check=f"Check comments on https://github.com/{repo}/issues/{number} for the original text.",
    )
    if err:
        return err
    return to_text(data)


if __name__ == "__main__":
    mcp.run()

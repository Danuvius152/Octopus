"""Generic MCP server for custom (user-defined) connectors.

One module serves every custom connector kind: it reads the connector's API
base with its token from the account-scoped installation callback and
exposes a single authenticated `request` tool the agent uses to call that API.
Token fetch + reconnect handling are shared with the typed connectors.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

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
    to_text,
    token_failure_message,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s custom-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("octopus-custom-connector")
ctx = ConnectorContext()



def _request_url(api_base: str, path: str) -> str:
    """Keep every request below the configured API origin and path prefix."""
    base = urlsplit(api_base)
    if (base.scheme not in {"http", "https"} or not base.hostname
            or base.username or base.password or base.query or base.fragment):
        raise ValueError("connector misconfigured (invalid API base)")
    decoded = path
    while True:
        parts = urlsplit(decoded)
        if (parts.scheme or parts.netloc or decoded.startswith("//")
                or "\\" in decoded or parts.fragment
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                or ".." in parts.path.split("/")):
            raise ValueError("path must be relative to the connector API base without traversal")
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    return f"{api_base.rstrip('/')}/{path.lstrip('/')}"


@mcp.tool(name="request")
async def request(
    method: str, path: str, query: dict | None = None, body: Any = None
) -> str:
    """Make an authenticated HTTP call to this connector's API. `method` is
    GET/POST/PATCH/DELETE/…; `path` is relative to the connector's API base
    (e.g. '/issues'); `query` are optional query params; `body` is an optional
    JSON body for writes. The connector's OAuth token is attached automatically.
    """
    try:
        info = await asyncio.to_thread(ctx.token_info)
    except ConnectorTokenError as token_error:
        return token_failure_message(token_error)
    api_base = info.get("api_base")
    if not api_base:
        return "Error: connector misconfigured (no API base)."
    try:
        url = _request_url(api_base, path)
    except ValueError as exc:
        return f"Error: {exc}"
    token = info["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    method = method.upper()
    retry_safe = method in {"GET", "HEAD", "OPTIONS"}
    operation = f"{method} {url} query={query!r} body={body!r}"
    check = f"Check the original operation's result at {url} using this provider's read tools or interface."
    r, error = await request_with_retry(
        method, url, retry_safe=retry_safe, operation=operation, check=check,
        headers=headers, params=query, json=body,
    )
    if error:
        return error
    assert r is not None
    if r.status_code == 401:
        authorization = await asyncio.to_thread(ctx.rejection_message, info)
        return authorization if retry_safe else request_failure(
            authorization, retry_safe=False, operation=operation, check=check,
        )
    error = response_failure(r, retry_safe=retry_safe, operation=operation, check=check)
    if error:
        return error
    try:
        return truncate(to_text(r.json()))
    except ValueError:
        return truncate(r.text)


if __name__ == "__main__":
    mcp.run()

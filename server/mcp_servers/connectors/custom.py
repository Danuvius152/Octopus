"""Generic MCP server for custom (user-defined) connectors.

One module serves every custom connector kind: it reads the connector's API
base with its token from the account-scoped installation callback and
exposes a single authenticated `request` tool the agent uses to call that API.
Token fetch + reconnect handling are shared with the typed connectors.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    to_text,
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

_RECONNECT = (
    "Error: token expired or revoked — ask the user to reconnect this "
    "connector in Octopus's sidebar."
)


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
def request(
    method: str, path: str, query: dict | None = None, body: Any = None
) -> str:
    """Make an authenticated HTTP call to this connector's API. `method` is
    GET/POST/PATCH/DELETE/…; `path` is relative to the connector's API base
    (e.g. '/issues'); `query` are optional query params; `body` is an optional
    JSON body for writes. The connector's OAuth token is attached automatically.
    """
    info = ctx.token_info()
    if info is None:
        return "Error: connector unavailable — reconnect it in Octopus."
    api_base = info.get("api_base")
    if not api_base:
        return "Error: connector misconfigured (no API base)."
    try:
        url = _request_url(api_base, path)
    except ValueError as exc:
        return f"Error: {exc}"
    token = info["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in range(3):
        try:
            r = httpx.request(
                method.upper(),
                url,
                headers=headers,
                params=query,
                json=body,
                timeout=30.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if r.status_code == 401:
            ctx.mark_needs_reconnect("invalid_grant")
            return _RECONNECT
        if r.status_code in {301, 302, 303, 307, 308}:
            return "Error: upstream redirect refused; use a path within this connector's API base."
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < 2:
                time.sleep(min(float(r.headers.get("Retry-After", "1") or 1), 5.0))
                continue
            return f"Error: upstream unavailable ({r.status_code})."
        if r.status_code >= 400:
            return f"Error: HTTP {r.status_code}: {r.text[:300]}"
        try:
            return truncate(to_text(r.json()))
        except Exception:
            return truncate(r.text)
    return "Error: rate-limited; try again shortly."


if __name__ == "__main__":
    mcp.run()

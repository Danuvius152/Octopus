"""Token failures retain provider facts through the internal callback boundary."""

from typing import Literal

import httpx

TokenFailureKind = Literal["temporary", "reconnect", "unavailable", "configuration", "permission", "unknown"]


class ConnectorError(Exception):
    """Connector business-rule violation."""


class ConnectorTokenError(ConnectorError):
    def __init__(self, message: str, *, kind: TokenFailureKind, code: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


class ConnectorRefreshError(ConnectorTokenError):
    def __init__(self, status: int, code: str) -> None:
        if status == 429 or status >= 500:
            kind: TokenFailureKind = "temporary"
        elif status in {400, 401} and code == "invalid_grant":
            kind = "reconnect"
        elif code in {"invalid_client", "unauthorized_client", "unsupported_grant_type", "invalid_scope"}:
            kind = "configuration"
        elif status == 403:
            kind = "permission"
        else:
            kind = "unknown"
        super().__init__(f"Token refresh HTTP {status} ({code})", kind=kind, code=code)
        self.status = status


def refresh_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code != 200 or body.get("error"):
        code = body.get("error")
        raise ConnectorRefreshError(response.status_code, code if isinstance(code, str) else "refresh_failed")
    return body

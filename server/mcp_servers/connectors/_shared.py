"""Shared plumbing for connector MCP servers (connectors.md §5.3).

Each shared connector namespace calls back to the host for the current
installation's token. The host owns token refresh and persistence; a namespace
serves multiple installations and must not retain their bearer tokens. Results
are capped so a large payload can't trip the CLI premature-exit bug
(docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ...connectors.errors import ConnectorTokenError, TokenFailureKind
from ...connectors.grants import GrantDisposition
from .. import _host

logger = logging.getLogger(__name__)

# Hard cap per tool result. Above this the CLI may silently drop the
# tool_result event; we truncate and tell the model to narrow/paginate.
MAX_RESULT_BYTES = 32 * 1024
_HTTP_TIMEOUT = 10.0


class ConnectorContext:
    """Reads the host callback env and brokers the installation's token.

    Callback identity is resolved on every call, including the legacy
    environment-based diagnostic path.
    """

    # Resolved per call from the request's verified scope and from settings.
    # These used to read the process environment, which worked when each
    # installation had its own sidecar; served in-process one module handles
    # every installation, so the installation has to come from the call
    # (polish-2026-09.md §4 B1).
    @property
    def api_base(self) -> str | None:
        return _host.resolve("OCTOPUS_API_BASE")

    @property
    def auth_token(self) -> str | None:
        return _host.resolve("OCTOPUS_AUTH_TOKEN")

    @property
    def installation_id(self) -> str | None:
        scope = _host.scope()
        return scope.installation_id if scope else _host.resolve("OCTOPUS_INSTALLATION_ID")

    def ready(self) -> bool:
        return bool(self.api_base and self.auth_token and self.installation_id)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth_token}"}

    def token_info(self) -> dict[str, Any]:
        """Read token and definition together, scoped to the same installation."""
        if not self.ready():
            logger.error("connector MCP misconfigured: missing callback env")
            raise ConnectorTokenError("Connector callback is not configured", kind="configuration", code="callback_missing")
        try:
            r = httpx.get(
                f"{self.api_base}/api/connectors/{self.installation_id}/token",
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.warning("token fetch failed: %s", e)
            raise ConnectorTokenError("Connector token service is temporarily unavailable", kind="temporary", code="transport") from e
        if r.status_code != 200:
            logger.warning("token fetch HTTP %s", r.status_code)
            _record_connector(self.installation_id, ok=False,
                              error_code=f"token_http_{r.status_code}")
            try:
                detail = r.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            kinds: tuple[TokenFailureKind, ...] = ("temporary", "reconnect", "unavailable", "configuration", "permission", "unknown")
            if isinstance(detail, dict) and detail.get("kind") in kinds:
                raise ConnectorTokenError(detail.get("message") or "Connector token resolution failed",
                                          kind=detail["kind"], code=detail.get("code") or "unknown")
            kind: TokenFailureKind = "temporary" if r.status_code == 429 or r.status_code >= 500 else "unavailable"
            raise ConnectorTokenError(f"Connector token service HTTP {r.status_code}", kind=kind, code=f"http_{r.status_code}")
        try:
            body = r.json()
            if (not isinstance(body, dict) or not isinstance(body.get("access_token"), str)
                    or not isinstance(body.get("grant_stamp"), str)):
                raise ValueError("Incomplete token snapshot")
        except ValueError as error:
            raise ConnectorTokenError("Invalid token service response", kind="unknown", code="invalid_response") from error
        return body

    def mark_needs_reconnect(self, stamp: str) -> GrantDisposition | None:
        if not self.ready():
            return None
        try:
            response = httpx.post(
                f"{self.api_base}/api/connectors/{self.installation_id}"
                "/mark-needs-reconnect",
                json={"grant_stamp": stamp},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
            if response.status_code == 200:
                disposition = response.json().get("disposition")
                if disposition in {"applied", "obsolete", "gone"}:
                    return disposition
        except (ValueError, AttributeError) as error:
            logger.warning("Invalid connector rejection receipt: %s", error)
        except httpx.HTTPError as e:
            logger.warning("mark-needs-reconnect failed: %s", e)
        return None

    def rejection_message(self, info: dict[str, Any]) -> str:
        _record_connector(self.installation_id, ok=False, error_code="auth_rejected")
        disposition = self.mark_needs_reconnect(info["grant_stamp"])
        if disposition == "applied":
            return "Error: the API rejected the current connector authorization. Ask the user to reconnect it in Octopus."
        if disposition == "obsolete":
            return "Error: the API rejected the authorization used by this call. The connector authorization has since changed; its current sign-in was left unchanged."
        if disposition == "gone":
            return "Error: the API rejected this call's authorization. The installation or account is no longer available."
        return "Error: the API rejected this call's authorization. Updating the connector state could not be confirmed."


def token_failure_message(error: ConnectorTokenError) -> str:
    if error.kind == "reconnect":
        return f"Error: {error}. Ask the user to reconnect this connector in Octopus."
    return f"Error: connector token {error.kind} ({error.code}): {error}"


def _record_connector(installation_id, *, ok: bool, error_code: str | None = None,
                      duration_ms: float | None = None) -> None:
    """Count a connector call. This is the hook the Gmail outage needed: the
    connector was unavailable for eleven consecutive days, a schedule failed on
    each of them, and nothing anywhere recorded the repetition
    (polish-2026-09.md §9 G0)."""
    from ...mcp_identity import current_scope
    from ...monitor import Event, record

    scope = current_scope()
    record(Event(
        kind="connector_call",
        session_id=scope.session_id if scope else None,
        duration_ms=duration_ms,
        ok=ok,
        error_code=error_code,
        detail={"installation_id": installation_id},
    ))


def truncate(text: str, cap: int = MAX_RESULT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    head = raw[:cap].decode("utf-8", "ignore")
    return (
        f"{head}\n…[truncated {len(raw) - cap} bytes — narrow your query or "
        "request a specific item]"
    )


def to_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)

"""Shared plumbing for connector MCP servers (connectors.md §5.3).

Each shared connector namespace calls back to the host for the current
installation's token. The host owns token refresh and persistence; a namespace
serves multiple installations and must not retain their bearer tokens. Results
are capped so a large payload can't trip the CLI premature-exit bug
(docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from asyncio import CancelledError
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from ...connectors.errors import ConnectorTokenError, TokenFailureKind
from ...connectors.grants import GrantDisposition
from .. import _host

logger = logging.getLogger(__name__)

# Hard cap per tool result. Above this the CLI may silently drop the
# tool_result event; we truncate and tell the model to narrow/paginate.
MAX_RESULT_BYTES = 32 * 1024
_HTTP_TIMEOUT = 10.0
RESOURCE_CALL_BUDGET_SECONDS = 30.0

FailureCode = Literal[
    "token", "auth", "configuration", "http", "budget", "retry_exhausted",
    "transport", "json_decode", "resource_decode", "cancelled", "internal",
]


@dataclass(frozen=True)
class RequestFailure:
    message: str
    code: FailureCode


class ResourceDecodeError(ValueError):
    """A provider resource cannot supply the fields this call needs."""


def object_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResourceDecodeError("expected a resource object")
    return value


def array_data(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ResourceDecodeError("expected a resource array")
    return value


def text_data(value: Any) -> str:
    if not isinstance(value, str):
        raise ResourceDecodeError("expected resource text")
    return value


def decode_response(
    response: httpx.Response, project: Callable[[Any], Any], *, provider: str,
) -> tuple[Any, RequestFailure | None]:
    """Accept JSON and the resource projection together, with finite failures.

    Wrappers still own the original write's operation and reconciliation hint.
    Unexpected decoder exceptions propagate to the resource finalizer.
    """
    try:
        data = response.json() if response.content else {}
    except ValueError:
        return {}, RequestFailure(f"Invalid {provider} JSON response", "json_decode")
    try:
        return project(data), None
    except ResourceDecodeError as error:
        return {}, RequestFailure(f"Invalid {provider} resource response: {error}", "resource_decode")


@dataclass
class ResourceResult:
    # A wrapper must explicitly accept a fully decoded result. An unexpected
    # exception still propagates, with one failed sample rather than success.
    error_code: FailureCode | None = "internal"


@contextmanager
def logical_resource(ctx: ConnectorContext) -> Iterator[ResourceResult]:
    """One lossy terminal sample, including token preparation and decoding."""
    from ...mcp_identity import current_scope

    installation = ctx.installation_id
    scope = current_scope()
    session = scope.session_id if scope else None
    start = time.monotonic()
    result = ResourceResult()
    try:
        yield result
    except CancelledError:
        result.error_code = "cancelled"
        raise
    except Exception:
        result.error_code = "internal"
        raise
    finally:
        try:
            from ...monitor import Event, record

            record(Event(
                kind="connector_call", session_id=session,
                duration_ms=(time.monotonic() - start) * 1000,
                ok=result.error_code is None, error_code=result.error_code,
                detail={"installation_id": installation, "unit": "logical_resource_v1"},
            ))
        except Exception:
            # Telemetry must never change the result or replay a resource call.
            logger.debug("connector metric dropped", exc_info=True)


def request_failure(reason: str, *, retry_safe: bool, operation: str, check: str) -> str:
    """Preserve the original write and its reconciliation target for the caller."""
    # Bound fields separately so long tool arguments cannot hide the outcome
    # or the check target. Even JSON escaping control characters stays <32 KiB.
    return "Error: " + to_text({
        "outcome": "failed" if retry_safe else "outcome_unknown",
        "operation": truncate(operation, cap=1024),
        "reason": truncate(reason, cap=1024),
        "next_step": truncate(check, cap=1024) + ("" if retry_safe else " Do not repeat the write before checking its result."),
    })


def response_failure(
    response: httpx.Response, *, retry_safe: bool, operation: str, check: str,
    rejected_statuses: frozenset[int] = frozenset(),
) -> str | None:
    """Only a provider's documented pre-effect rejection can reject a write."""
    status = response.status_code
    if 200 <= status < 300:
        return None
    reason = f"HTTP {status}: {response.text[:300]}"
    if 300 <= status < 400:
        reason = f"HTTP {status}: upstream redirect refused; check the original API target."
    if status in rejected_statuses:
        return f"Error: rejected: {reason}"
    return request_failure(reason, retry_safe=retry_safe, operation=operation, check=check)


def retry_after(response: httpx.Response) -> float | None:
    """HTTP delay-seconds or HTTP-date; an invalid hint supplies no deadline."""
    value = response.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdigit():
        try:
            return float(value)
        except OverflowError:
            return math.inf
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def retry_delay(response: httpx.Response, attempt: int) -> float | None:
    if response.status_code != 429 and response.status_code < 500:
        return None
    delay = retry_after(response)
    return max(float(2**attempt), delay) if delay is not None else float(2**attempt)


async def request_with_retry(
    method: str, url: str, *, retry_safe: bool, operation: str, check: str,
    delay_for_response: Callable[[httpx.Response, int], float | None] = retry_delay,
    **kwargs: Any,
) -> tuple[httpx.Response | None, RequestFailure | None]:
    """One resource budget includes response bodies, all attempts and backoff.

    Host token/rejection callbacks retain their separate existing timeout. Use
    the host loop: closing a per-call asyncio.run would wait for DNS threads.
    """
    deadline = time.monotonic() + RESOURCE_CALL_BUDGET_SECONDS
    reason = f"resource call budget ({RESOURCE_CALL_BUDGET_SECONDS:g}s) exhausted"
    code: FailureCode = "budget"
    try:
        async with asyncio.timeout(RESOURCE_CALL_BUDGET_SECONDS):
            async with httpx.AsyncClient(follow_redirects=False) as client:
                for attempt in range(3):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    response = await client.request(method, url, timeout=remaining, **kwargs)
                    delay = delay_for_response(response, attempt)
                    if delay is None:
                        return response, None
                    limited = response.status_code in {403, 429}
                    reason = f"{'rate_limited' if limited else 'upstream_unavailable'}: HTTP {response.status_code}; retry delay at least {delay:g}s"
                    if not retry_safe:
                        code = "http"
                        break
                    remaining = deadline - time.monotonic()
                    if delay >= remaining:
                        reason += f"; required wait {delay:g}s exceeds remaining resource call budget ({max(0.0, remaining):g}s)"
                        break
                    if attempt == 2:
                        reason += "; three attempts exhausted"
                        code = "retry_exhausted"
                        break
                    await asyncio.sleep(delay)
                    if time.monotonic() >= deadline:
                        reason += "; resource call budget exhausted during wait"
                        break
    except TimeoutError:
        reason = f"resource call budget ({RESOURCE_CALL_BUDGET_SECONDS:g}s) exhausted"
        code = "budget"
    except httpx.HTTPError as error:
        # Preserve the old direct-failure behavior for transport errors. An
        # unsafe write may already have taken effect before any such error.
        reason = f"request failed: {error}"
        code = "transport"
    return None, RequestFailure(request_failure(reason, retry_safe=retry_safe, operation=operation, check=check), code)


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

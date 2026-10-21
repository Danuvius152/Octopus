"""Per-session identity for the HTTP-served MCP tool namespaces.

The stdio sidecars each received their identity as process environment at
spawn time. A single HTTP endpoint serving every session has no per-process
environment, so identity has to travel with the request instead.

It travels in the bearer, not in a custom header. Claude Code accepts
arbitrary per-server headers; Codex does not — its `--env` is documented
stdio-only and the sole per-server hook for an HTTP server names an env var
to read a bearer from. A header-based scheme would therefore work on one
backend and be impossible on the other, which is exactly the kind of
capability difference `server/harness/` exists to keep out of feature code.

The shape follows `applications.app_scope_token`: derived rather than stored,
so there is no new secret to persist, back up or leak, and rotating
`OCTOPUS_AUTH_TOKEN` rotates every scope with it (token-rotation.md).
"""

from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar
from dataclasses import dataclass

# Set per request by the ASGI middleware below and read by the tool bodies.
# A ContextVar rather than a parameter because the MCP SDK owns the call path
# between the HTTP request and the tool function, so there is nowhere to thread
# an argument through; and a ContextVar is task-local, so concurrent turns from
# different sessions cannot observe each other's scope.
_current: ContextVar[McpScope | None] = ContextVar("octopus_mcp_scope", default=None)
_closed_works: set[str] = set()
_run_leases: dict[str, tuple[str, str | None, str | None]] = {}


def work_is_open(work_id: str | None) -> bool:
    """Work closure is monotonic for this server process's lifetime."""
    return work_id is None or work_id not in _closed_works


def close_work(work_id: str) -> None:
    _closed_works.add(work_id)
    for lease, (_, _, owner) in list(_run_leases.items()):
        if owner == work_id:
            _run_leases.pop(lease, None)


def register_run(run_lease_id: str, session_id: str, user_id: str | None, work_id: str | None) -> None:
    if work_is_open(work_id):
        _run_leases[run_lease_id] = (session_id, user_id, work_id)


def revoke_run(run_lease_id: str | None) -> None:
    if run_lease_id:
        _run_leases.pop(run_lease_id, None)


def revoke_ownerless_runs() -> None:
    """Retire the pre-accounts authority at the install's one-way upgrade."""
    for lease, (_, user_id, _) in list(_run_leases.items()):
        if user_id is None:
            _run_leases.pop(lease, None)


@dataclass(frozen=True)
class McpScope:
    """Who a tool call belongs to: always a session, plus a connector
    installation when the namespace is a connector's."""

    session_id: str
    installation_id: str | None = None
    # Whose call this is (multi-tenancy.md §7). Carried alongside the session
    # rather than looked up from it, so a tool body that needs an owner has one
    # without a round trip — and so the signature covers it, which is what
    # stops a bearer being edited into somebody else's.
    user_id: str | None = None
    wiki_task_id: str | None = None
    run_lease_id: str | None = None
    work_id: str | None = None


def scope_is_live(scope: McpScope) -> bool:
    """Check the signed owner against the actual selected run, never a session's
    current request. Legacy diagnostic scopes carry no work authority.
    """
    if not work_is_open(scope.work_id):
        return False
    if scope.run_lease_id is None:
        return scope.work_id is None
    return _run_leases.get(scope.run_lease_id) == (
        scope.session_id, scope.user_id, scope.work_id,
    )


async def scope_is_authorized(scope: McpScope) -> bool:
    """A live claim also has to belong to the install's current identity era."""
    return await scope_rejection_reason(scope) is None


async def scope_rejection_reason(scope: McpScope) -> str | None:
    """Only call with a verified signature; never diagnose untrusted claims."""
    from .deps import accounts_exist, owner_is_available

    if not work_is_open(scope.work_id):
        return "closed_work"
    if scope.run_lease_id is None:
        if scope.work_id is not None:
            return "missing_run_lease"
    else:
        lease = _run_leases.get(scope.run_lease_id)
        if lease is None:
            return "retired_run_lease"
        if lease != (scope.session_id, scope.user_id, scope.work_id):
            return "mismatched_run_lease"
    if scope.user_id is None:
        return "legacy_era" if await accounts_exist() else None
    if not await owner_is_available(scope.user_id):
        return "owner_unavailable"
    return None


def require_live_scope(scope: McpScope | None, session_id: str) -> None:
    if scope is not None and (
        scope.run_lease_id is None or scope.session_id != session_id or not scope_is_live(scope)
    ):
        raise ValueError("Model run is no longer active")


def _sign(payload: str) -> str:
    """Signed with the **master key**, not the access token.

    It was the access token, from the era when that string was also the
    encryption key and the client credential. Now that a password can change
    and a bearer can be revoked, signing with either would invalidate every
    tool call in a turn that is currently running the moment somebody changed
    their password (multi-tenancy.md §7). The master key belongs to the
    server, changes only when an operator rotates it, and is exactly the right
    lifetime for this.
    """
    from .crypto import master_key

    return hmac.new(
        master_key().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _payload(scope: McpScope) -> str:
    payload = (
        f"mcp:{scope.session_id}:{scope.installation_id or ''}:{scope.user_id or ''}"
    )
    payload += f":wiki:{scope.wiki_task_id}" if scope.wiki_task_id else ""
    if scope.run_lease_id or scope.work_id:
        payload += f":run:{scope.run_lease_id or ''}:{scope.work_id or ''}"
    return payload


def mint(
    session_id: str,
    installation_id: str | None = None,
    user_id: str | None = None,
    wiki_task_id: str | None = None,
    run_lease_id: str | None = None,
    work_id: str | None = None,
) -> str:
    """The bearer for one session (and one connector installation, if any).

    The scope is carried in the clear alongside its signature so the server can
    tell *which* session is calling without a lookup table, while the signature
    is what makes the claim trustworthy. Nothing here is stored.
    """
    scope = McpScope(session_id, installation_id, user_id, wiki_task_id, run_lease_id, work_id)
    if run_lease_id or work_id:
        return ".".join((
            session_id, installation_id or "", user_id or "", wiki_task_id or "",
            run_lease_id or "", work_id or "", _sign(_payload(scope)),
        ))
    return (
        f"{scope.session_id}.{scope.installation_id or ''}."
        f"{scope.user_id or ''}."
        f"{scope.wiki_task_id + '.' if scope.wiki_task_id else ''}{_sign(_payload(scope))}"
    )


def verify(bearer: str | None) -> McpScope | None:
    """The scope a bearer proves, or None if it proves nothing.

    Returns None rather than raising: an unverifiable bearer is an ordinary
    "not authorised" outcome on a public HTTP surface, not an exception.
    """
    if not bearer:
        return None
    parts = bearer.split(".")
    if len(parts) not in (4, 5, 7):
        return None
    session_id, installation_id, user_id = parts[:3]
    signature = parts[-1]
    wiki_task_id = (parts[3] or None) if len(parts) in (5, 7) else None
    if not session_id:
        return None
    scope = McpScope(
        session_id, installation_id or None, user_id or None, wiki_task_id,
        (parts[4] or None) if len(parts) == 7 else None,
        (parts[5] or None) if len(parts) == 7 else None,
    )
    if not hmac.compare_digest(signature, _sign(_payload(scope))):
        return None
    return scope


def current_scope() -> McpScope | None:
    """The scope of the request being served, or None outside one."""
    return _current.get()


def set_current_scope(scope: McpScope | None):
    """Bind the scope for this task. Returns the ContextVar token so the caller
    can restore the previous value."""
    return _current.set(scope)


def reset_current_scope(token) -> None:  # type: ignore[no-untyped-def]
    _current.reset(token)

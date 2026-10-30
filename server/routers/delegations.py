"""REST endpoints for agent-to-agent delegations (agent-collaboration.md).

Shape mirrors the bg-tasks router: a thin translation layer between
HTTP and the in-process `DelegationManager`. The `ask_agent` MCP server
(Phase 2) will POST to these endpoints from inside a running harness;
in Phase 1 the only caller is tests, by design.

All routes are session-scoped (`/api/sessions/{sid}/delegations`)
because a delegation always has a parent session. The delegation id
*is* the child session id — there's no parallel id space.

Auth: same bearer token as every other API surface.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import verify_token
from ..delegations import DelegationError, TerminalDisposition, delegation_manager
from ..deps import ScopeUser, SessionMgr
from ..mcp_identity import verify
from ..sessions import SessionManager

router = APIRouter(prefix="/api/sessions", tags=["delegations"])


class StartDelegationRequest(BaseModel):
    agent_name: str
    request: str
    # Optional list of file paths the parent agent thinks the child
    # should look at. Rendered into the child's first user message
    # verbatim; we don't validate existence here (the child's tools
    # will report missing files naturally).
    files: list[str] | None = None


class DelegationResponse(BaseModel):
    delegation_id: str
    sub_session_id: str
    parent_session_id: str
    target_agent_id: str
    target_agent_name: str
    request: str
    work_id: str
    state: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    created_at: str
    finished_at: str | None
    error: str | None
    terminal_disposition: TerminalDisposition | None
    terminal_delivery_error: str | None
    recording_state: Literal["committed"]


class CancelDelegationRequest(BaseModel):
    reason: str | None = None
    work_id: str | None = None


class AnswerDelegationQuestionRequest(BaseModel):
    """Complete ordered/ID-indexed answers; choice retains single-question compatibility."""
    choice: str | None = None
    answers: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None
    question_id: str | None = None


class FollowUpDelegationRequest(BaseModel):
    """Body for the continuation mode of `mcp__ask_agent__ask`
    (delegation_id passed in place of name): continue a prior
    delegation with a new request in the SAME child session, so the
    target agent keeps her in-session transcript across rounds.

    The original brief lives in the child's transcript; we don't
    repeat it — only the new request is needed."""
    request: str


def _require_session(
    session_manager: SessionManager, session_id: str, user_id: str | None = None
) -> None:
    """Live sessions only — delegations attach to in-memory sessions so
    the broadcast listener has a target. Archived sessions are
    read-only history. Scoped: another account's session is a 404, so a
    delegation cannot be started from, or listed out of, somebody else's
    conversation."""
    session = session_manager.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )


@router.post(
    "/{session_id}/delegations",
    status_code=status.HTTP_201_CREATED,
    response_model=DelegationResponse,
)
async def start_delegation(
    session_manager: SessionMgr,
    session_id: str,
    req: StartDelegationRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Spawn a child session under the named agent and start its first
    turn. Returns immediately — the reply arrives later as a turn
    injected into the parent session.

    Status codes:
    - 201 Created with the delegation record on success
    - 404 if the parent session is gone or the target agent name
      doesn't resolve
    - 409 on cycle, depth, self-delegation, or ambiguous name
    """
    _require_session(session_manager, session_id, user_id)
    try:
        rec = await delegation_manager.start_delegation(
            parent_session_id=session_id,
            agent_name=req.agent_name,
            request=req.request,
            files=req.files,
            scope=verify(_),
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))
    return rec.to_public_dict()


@router.get("/{session_id}/delegations", response_model=list[DelegationResponse])
async def list_delegations(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> list[dict[str, Any]]:
    """Recent delegations spawned by this session, newest first. The
    list includes finished ones so the model can see what it's
    asked recently. Currently capped at 25 inside the manager."""
    # Not `_require_session`: a parent session may have been archived while a
    # delegation is still in our in-memory registry, and we still want list to
    # work for inspection. Ownership is checked either way —
    # `session_belongs_to` answers for the archived case too.
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    rows = delegation_manager.list_delegations(session_id)
    return [r.to_public_dict() for r in rows]


@router.post("/{session_id}/delegations/{delegation_id}/cancel", response_model=DelegationResponse)
async def cancel_delegation(
    session_manager: SessionMgr,
    session_id: str,
    delegation_id: str,
    req: CancelDelegationRequest | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Best-effort cancel. Idempotent — cancelling a finished
    delegation preserves its terminal record and parent reply while cleaning
    up that round's remaining background work and descendants. The parent
    gets an `[agent-error:…]` injection on the
    transition from running → cancelled."""
    rec = delegation_manager.get_delegation(delegation_id)
    captured_work_id = req.work_id if req and req.work_id is not None else (rec.work_id if rec else None)
    if (
        rec is None
        or rec.parent_session_id != session_id
        or not await session_manager.session_belongs_to(session_id, user_id)
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Delegation not found"
        )
    try:
        updated = await delegation_manager.cancel_delegation(
            delegation_id, reason=(req.reason if req else None),
            work_id=captured_work_id, scope=verify(_),
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))
    return updated.to_public_dict()


@router.post("/{session_id}/delegations/{delegation_id}/follow-up", response_model=DelegationResponse)
async def follow_up_delegation(
    session_manager: SessionMgr,
    session_id: str,
    delegation_id: str,
    req: FollowUpDelegationRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Continue a prior delegation with a new request in the SAME
    child session. The target agent keeps her in-session transcript,
    so review/iteration rounds don't make her re-read everything
    (agent-collaboration.md §5.x).

    Status codes:
    - 201 Created on a successful new round (state flips to
      "running" again; reply arrives as a fresh `[agent-reply:…]`
      injection when the child finishes)
    - 404 if the parent session is no longer live (archived /
      deleted between rounds; the model must be invoked from a
      live session for the reply to land somewhere visible) or if
      the delegation doesn't belong to this parent
    - 409 if the delegation is still running (wait for its terminal
      first), or if the child session is hard-deleted (start a
      fresh `ask` instead)
    """
    # Vera-round-5 finding: without this guard, an archived /
    # deleted parent session could still receive a follow-up that
    # round-resets the record, starts the child, and then silently
    # drops the terminal turn because there's no live parent to
    # inject into. Matches the start_delegation route's behaviour.
    _require_session(session_manager, session_id, user_id)
    try:
        rec = await delegation_manager.follow_up_delegation(
            parent_session_id=session_id,
            delegation_id=delegation_id,
            request=req.request,
            scope=verify(_),
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))
    return rec.to_public_dict()


@router.post("/{session_id}/delegations/{delegation_id}/answer")
async def answer_delegation_question(
    session_manager: SessionMgr,
    session_id: str,
    delegation_id: str,
    req: AnswerDelegationQuestionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Parent-side answer to a question the child agent raised via
    its own `ask` MCP tool. The child's pending question is drained
    with the parent's chosen label, the child's long-poll wakes, and
    the child resumes (agent-collaboration.md §5.5).

    Status codes:
    - 200 OK on successful answer
    - 404 if the delegation isn't ours
    - 409 if there's no pending question, or the human UI raced us
    """
    rec = delegation_manager.get_delegation(delegation_id)
    if (
        rec is None
        or rec.parent_session_id != session_id
        or not await session_manager.session_belongs_to(session_id, user_id)
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Delegation not found"
        )
    try:
        return await delegation_manager.answer_pending_question(
            delegation_id, req.choice, answers=req.answers, question_id=req.question_id,
        )
    except DelegationError as e:
        raise HTTPException(e.status_code, str(e))

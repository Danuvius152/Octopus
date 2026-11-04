"""REST endpoints for the MCP-based AskUserQuestion flow.

Question endpoints:

  POST /api/sessions/{id}/questions
      Called by the `ask` MCP server (server/mcp_servers/ask.py) when
      the model invokes `mcp__ask__user`. Body: `{questions: [...]}`.
      Octopus generates a question_id, persists the question into the
      session's chat history (so reload re-renders the form), and
      broadcasts the `question_request` WS event to the frontend.
      Returns `{question_id: "...", deadline: "..." | null}`. The host
      owns expiry; null means that auto-answer is disabled.

  GET  /api/sessions/{id}/questions/{question_id}/answer
      Long-polled by the ask MCP server. Blocks on an asyncio.Event
      until the user submits the answer (or the per-call timeout
      hits). Returns `{answer: "..."}` on success, or 408 on timeout
      so the MCP server can retry. The MCP server retries until it
      gets an answer or the session-level auto-answer timer fires.
      With structured=true, returns the original saved resolution,
      readable answer and closure reason, including known closed questions.
      Unknown IDs and authentication failures remain errors.

  POST /api/sessions/{id}/questions/{question_id}/answer
      Called by the frontend when the user submits the form. Routes
      into session_manager.answer_question() which sets the Event
      that unblocks the long-poll, persists the answer, and
      broadcasts the question_answer WS event.

  DELETE /api/sessions/{id}/questions/{question_id}
      Closes an abandoned tool wait, wakes its pollers with 404 and
      broadcasts a persisted question_closed event to remove the form.
      The structured opt-in can read that exact saved closure; plain
      long-poll compatibility still reports closed questions as 404.

Auth: same bearer-token model as every other API surface. The MCP
server holds OCTOPUS_AUTH_TOKEN; the frontend sends the user's
bearer.

This replaces the previous CLI control-protocol path
(`--permission-prompt-tool=stdio` + deny-channel answer) that was
exposing us to the CLI's premature-exit bug at large context scale.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth import verify_token
from ..deps import ScopeUser, SessionMgr, resolve_principal
from ..mcp_identity import verify
from ..question_resolution import QuestionAnswerInvalid
from ..sessions.questions import QuestionClosedError

router = APIRouter(prefix="/api/sessions", tags=["questions"])


class CreateQuestionRequest(BaseModel):
    """Body for POST /questions. The schema mirrors the existing
    AskUserQuestion tool input so the model's call shape is unchanged."""
    questions: list[dict[str, Any]]
    mcp_call_id: str | None = None


class AnswerItem(BaseModel):
    """One entry in the frontend's answer submission. Mirrors the
    legacy WS answer_question payload so existing UI code keeps
    working."""
    selected: list[str] | None = None
    text: str | None = None


class SubmitAnswerRequest(BaseModel):
    answers: list[AnswerItem]


@router.post(
    "/{session_id}/questions",
    status_code=status.HTTP_201_CREATED,
)
async def create_question(
    session_manager: SessionMgr,
    session_id: str,
    req: CreateQuestionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Called by the MCP server. Creates a pending question, broadcasts
    the WS event so the frontend renders the form, returns the
    question_id the MCP server should long-poll on."""
    if not req.questions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "questions must be non-empty")
    if session_manager.get_session(session_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    try:
        qid = await session_manager.create_pending_question(
            session_id, req.questions, scope=verify(_), call_id=req.mcp_call_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if qid is None:
        # The session existed at check time but vanished — race with
        # delete. Surface as 404 so the MCP server can report cleanly.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    session = session_manager.get_session(session_id, user_id)
    pending = session._questions.get(qid) if session else None
    if pending is not None and pending.state != "pending":
        pending = None
    return {"question_id": qid, "deadline": pending.deadline if pending else None}


@router.get("/{session_id}/questions/{question_id}/answer")
async def wait_for_answer(
    request: Request,
    session_manager: SessionMgr,
    session_id: str,
    question_id: str,
    timeout: float = 60.0,
    structured: bool = False,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """MCP-server-facing long-poll. Returns when the user (or the
    session-level auto-answer timeout) submits. 408 on per-call
    timeout — the MCP server should loop and retry."""
    # Tells the request monitor this call's duration is the user's think
    # time and its 408 is "no answer yet", not a slow or failing request.
    request.state.long_poll = True
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    # Cap timeout so a misbehaving MCP server can't park a connection
    # forever. 5 min is plenty given the auto-answer default is 30 min.
    timeout = min(max(timeout, 1.0), 300.0)
    try:
        if structured:
            result = await session_manager.wait_for_question_result(session_id, question_id, timeout)
            if result is None:
                raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "no answer yet")
            return result
        answer = await session_manager.wait_for_question_answer(
            session_id, question_id, timeout=timeout
        )
    except QuestionClosedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if answer is None:
        # Distinguishable status code so the MCP server loops vs. errors.
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "no answer yet")
    return {"answer": answer}


@router.post("/{session_id}/questions/{question_id}/answer")
async def submit_answer(
    session_manager: SessionMgr,
    session_id: str,
    question_id: str,
    req: SubmitAnswerRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Frontend-facing. The user pressed submit; route into the
    session_manager which formats the answer, persists the chat
    entry, broadcasts the question_answer WS event, and sets the
    Event that wakes the MCP server's long-poll."""
    if session_manager.get_session(session_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    principal = await resolve_principal(_)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    try:
        ok = await session_manager.answer_question(
            session_id, question_id, [a.model_dump() for a in req.answers], source=principal.answer_source(),
        )
    except QuestionAnswerInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Question not found or already answered",
        )
    return {"ok": True}


@router.get("/{session_id}/questions/{question_id}/result")
async def question_result(
    session_manager: SessionMgr, session_id: str, question_id: str,
    user_id: ScopeUser = None, _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Observe the original committed resolution without polling or resubmitting."""
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    try:
        result = await session_manager.input_database().get_question_result(session_id, question_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Original question resolution is unknown") from exc
    if result is not None:
        return {"state": "resolved", **result}
    pending = next((record for record in session_manager.get_pending_questions(session_id)
                    if record.question_id == question_id), None)
    if pending is not None:
        return {"state": "pending", "question_id": question_id,
                "request_id": pending.request_id, "attempt_id": pending.attempt_id}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Original question resolution is unknown")


@router.delete("/{session_id}/questions/{question_id}")
async def close_question(
    session_manager: SessionMgr,
    session_id: str,
    question_id: str,
    reason: Literal["cancelled", "communication_failure"] = Query("cancelled"),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, bool]:
    if session_manager.get_session(session_id, user_id) is None:
        raise HTTPException(404, "Session not found")
    principal = await resolve_principal(_)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    if not await session_manager.close_pending_question(session_id, question_id, reason=reason, source=principal.answer_source()):
        raise HTTPException(404, "Question not found or closed")
    return {"ok": True}

"""REST endpoints for cross-turn background tasks.

The frontend uses these to render the chip + popover (list/get
endpoints) and to wire up the cancel button. The MCP server in
`server/mcp_servers/bg.py` uses the POST endpoint to start tasks —
it's a child of the `claude` CLI and can't reach the in-process
manager any other way.

Auth: same bearer token as every other API surface.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from ..auth import verify_token
from ..bg_tasks import BgTaskError, BgTaskLimits, BgTaskRecord, bg_task_manager
from ..deps import ScopeUser, SessionMgr
from ..knowledge import capture, derive
from ..mcp_identity import verify
from ..sessions import SessionManager

router = APIRouter(prefix="/api/sessions", tags=["bg-tasks"])


class StartBgTaskRequest(BgTaskLimits):
    model_config = ConfigDict(extra="forbid")
    command: str
    description: str | None = None


class BgTaskMetadata(BaseModel):
    id: str
    session_id: str
    command: str
    description: str | None
    status: Literal["pending", "running", "completed", "failed", "cancelled", "interrupted"]
    exit_code: int | None
    truncated: bool
    started_at: str
    completed_at: str | None
    idle_timeout_seconds: int | None
    timeout_seconds: int | None
    stop_reason: str | None
    delivery_disposition: Literal["stopped", "unknown"] | None
    delivery_error: str | None


class BgTaskResponse(BgTaskMetadata):
    working_dir: str
    stdout: str
    stderr: str
    work_id: str | None


class BgTaskSummaryResponse(BgTaskMetadata):
    output_preview: str
    has_output: bool
    preview_truncated: bool


def _require_session(
    session_manager: SessionManager, session_id: str, user_id: str | None = None
) -> str:
    """Live sessions only — bg tasks attach to in-memory sessions so
    the cross-turn delivery has a target. Archived sessions are
    read-only history. Scoped: another account's session is a 404."""
    session = session_manager.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )
    return session.working_dir


@router.post(
    "/{session_id}/bg-tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=BgTaskResponse,
)
async def start_bg_task(
    session_manager: SessionMgr,
    session_id: str,
    req: StartBgTaskRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> BgTaskRecord:
    """Start a new background task. Called by the bg MCP server.

    Returns the freshly-created task row so the MCP tool can echo the
    task_id back to the model. The subprocess is already running by the
    time this returns — callers don't wait for completion.
    """
    if not req.command.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "command must be non-empty")
    working_dir = _require_session(session_manager, session_id, user_id)
    try:
        scope = verify(_)
        session = session_manager.get_session(session_id, user_id)
        parent = capture(session, scope, prompt=req.command)
        owned = derive(parent, req.command)
        rec = await bg_task_manager.start_task(
            session_id=session_id,
            command=req.command,
            working_dir=working_dir,
            description=req.description,
            idle_timeout_seconds=req.idle_timeout_seconds,
            timeout_seconds=req.timeout_seconds,
            scope=scope,
            owned_context=owned,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except BgTaskError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return rec


@router.get("/{session_id}/bg-tasks", response_model=list[BgTaskResponse])
async def list_bg_tasks(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> list[BgTaskRecord]:
    """All bg tasks for a session, most-recent first. Used by the
    sidebar / chip popover. Includes finished tasks so users can scroll
    back through history."""
    # We don't require a *live* session here — chat history can outlive the
    # in-memory session (archived) — but we do require it to be this account's,
    # which `session_belongs_to` answers for both.
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return await bg_task_manager.list_tasks(session_id)


@router.get("/{session_id}/bg-tasks/{task_id}", response_model=BgTaskResponse)
async def get_bg_task(
    session_manager: SessionMgr,
    session_id: str,
    task_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> BgTaskRecord:
    rec = await bg_task_manager.get_task(task_id)
    if (
        rec is None
        or rec.session_id != session_id
        or not await session_manager.session_belongs_to(session_id, user_id)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Background task not found")
    return rec


@router.get("/{session_id}/bg-task-summaries", response_model=list[BgTaskSummaryResponse])
async def list_bg_task_summaries(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> list[dict[str, Any]]:
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return await bg_task_manager.list_task_summaries(session_id)


@router.get("/{session_id}/bg-task-summaries/{task_id}", response_model=BgTaskSummaryResponse)
async def get_bg_task_summary(
    session_manager: SessionMgr,
    session_id: str,
    task_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    return await _require_task_summary(session_manager, session_id, task_id, user_id)


async def _require_task_summary(
    session_manager: SessionManager, session_id: str, task_id: str, user_id: str | None,
) -> dict[str, Any]:
    if await session_manager.session_belongs_to(session_id, user_id):
        summary = await bg_task_manager.get_task_summary(session_id, task_id)
        if summary is not None:
            return summary
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Background task not found")


@router.post("/{session_id}/bg-tasks/{task_id}/cancel")
async def cancel_bg_task(
    session_manager: SessionMgr,
    session_id: str,
    task_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Best-effort cancel. Returns {cancelled: bool}; cancelled=False
    means the task wasn't currently running (already finished, or
    server restarted and lost the in-memory handle)."""
    await _require_task_summary(session_manager, session_id, task_id, user_id)
    cancelled = await bg_task_manager.cancel_task(task_id)
    return {"cancelled": cancelled, "task_id": task_id}

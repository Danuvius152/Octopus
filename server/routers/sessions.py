from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse

from ..auth import verify_token
from ..config import settings
from ..credential_bindings import CredentialBindingError
from ..deps import ScopeUser, SessionMgr
from ..harness import BackendForkNotSupported, StdinMode, get_harness
from ..models import (
    CreateSessionRequest,
    DispatchState,
    DuplicateSessionRequest,
    ForkSessionRequest,
    ImportSessionRequest,
    MessageContent,
    MessagePage,
    PendingQuestionInfo,
    SessionDetail,
    SessionInfo,
    SessionUpdate,
    SubagentRun,
)
from ..session_manager import ForkError, fork_info_fields
from ..sessions import SessionManager
from ..sessions.runtime_types import QuotaExceeded
from ..submission_models import (
    ClientSubmissionRefusal,
    ClientSubmissionRequest,
    ClientSubmissionStatus,
    SubmissionRecoveryPage,
)
from ..wiki.contracts import JobCard

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# How much transcript an open returns. 46,448 messages live in this database
# across 66 sessions, and the largest single session holds 4,873 — which the
# client was handed in full every time it opened one (polish-2026-09.md §4 B2).
# 200 covers the great majority of sessions whole; the rest arrive by cursor.
MESSAGE_WINDOW = 200


def _fork_fields(s) -> dict:
    """The public fork fields for SessionInfo, from a live Session."""
    return fork_info_fields(
        backend=s.backend,
        forked_from_session_id=s.forked_from_session_id,
        fork_after_seq=s.fork_after_seq,
        fork_metadata=s.fork_metadata,
        fork_revert_record=s.fork_revert_record,
    )


def _can_steer(backend: str) -> bool:
    """Whether a turn on this backend can take a message mid-flight.

    A harness capability, derived the same way `can_fork` is: only a backend
    that takes its input on stdin has a channel to steer through
    (inline-steering.md §8). Codex keeps queue-until-idle.
    """
    try:
        return get_harness(backend).profile.stdin_mode is StdinMode.STREAM_JSON
    except Exception:
        return False


def _to_session_info(
    s, message_count: int | None = None, archived: bool = False
) -> SessionInfo:
    return SessionInfo(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=s._message_count if message_count is None else message_count,
        transcript_integrity=s._transcript_integrity,
        claude_session_id=s.claude_session_id,
        credential_id=s.credential_id,
        agent_id=s.agent_id,
        origin=s.origin,
        backend=s.backend,
        model=s.model,
        reasoning_effort=s.reasoning_effort,
        service_tier=s.service_tier,
        parent_session_id=s.parent_session_id,
        delegation_request=s.delegation_request,
        app_id=s.app_id,
        archived=archived,
        can_steer=_can_steer(s.backend),
        **_fork_fields(s),
    )


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    session_manager: SessionMgr,
    user_id: ScopeUser = None,
    include_archived: bool = Query(False),
    _: str = Depends(verify_token),
):
    live = [_to_session_info(s) for s in session_manager.list_sessions(user_id)]
    if not include_archived:
        return live
    archived = await session_manager.list_archived_sessions(user_id)
    return live + archived


def _own_session(
    session_manager: SessionManager, session_id: str, user_id: str | None
) -> None:
    """404 unless this account owns `session_id` (multi-tenancy.md §5).

    For the routes that act on a session without reading it back — fork,
    duplicate, reset, archive, delete. They pass the id straight to a manager
    method that has no idea who is asking, so the check has to happen here, in
    one named place, rather than as an `if` each of them might be written
    without.
    """
    if session_manager.get_session(session_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


async def _check_credential_backend(
    session_manager: SessionManager, credential_id: str | None, backend: str
) -> None:
    """A session must not run a credential whose backend differs from its own
    (codex-backend.md §4.2) — e.g. a Codex subscription on a Claude session.
    400 on mismatch. A missing credential is tolerated (resolved later)."""
    if not credential_id:
        return
    row = await session_manager.db.get_credential(credential_id)
    if row is None:
        return
    if row["backend"] != backend:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Credential backend {row['backend']!r} does not match session "
            f"backend {backend!r}",
        )


@router.patch("/{session_id}", response_model=SessionInfo)
async def update_session(
    session_manager: SessionMgr,
    session_id: str, req: SessionUpdate, user_id: ScopeUser = None,
    _: str = Depends(verify_token)
):
    """Update a session's credential, name, model, reasoning effort or speed.

    The credential is the point: it used to be fixed at creation, so a lapsed
    or deleted sign-in stranded the conversation with no way back. The
    backend-match rule still applies (a Codex credential can't run a Claude
    session), and an unknown credential id is refused rather than silently
    stored, because a dangling id is exactly the state this route exists to
    get out of."""
    async with session_manager.configuration_lock:
        return await _update_session_settings(session_manager, session_id, req, user_id)


async def _update_session_settings(
    session_manager, session_id: str, req: SessionUpdate, user_id: str | None,
) -> SessionInfo:
    session = session_manager.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    fields = req.model_dump(exclude_unset=True)
    updates: dict[str, object] = {}

    if "credential_id" in fields:
        cred_id = fields["credential_id"] or None
        if cred_id is not None:
            row = await session_manager.db.get_credential(cred_id)
            if row is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "Credential not found"
                )
            await _check_credential_backend(session_manager, cred_id, session.backend)
        updates["credential_id"] = cred_id

    # `/model`. Deliberately not validated against a list: both CLIs accept
    # aliases, and new model names appear between Octopus releases — refusing an
    # unknown string would make this route the thing that needs shipping. Empty
    # means "back to the agent's model", same as null.
    if "model" in fields:
        raw = fields["model"]
        updates["model"] = (str(raw).strip() or None) if raw is not None else None

    if "reasoning_effort" in fields:
        updates["reasoning_effort"] = fields["reasoning_effort"]
    if "service_tier" in fields:
        updates["service_tier"] = fields["service_tier"]
    if {"model", "reasoning_effort", "service_tier", "credential_id"} & updates.keys():
        from .backends import (
            model_settings_for,
            validate_reasoning,
            validate_service_tier,
        )

        agent = (
            await session_manager.db.get_agent(session.agent_id, user_id)
            if session.agent_id else None
        ) or {}
        effort = updates.get("reasoning_effort", session.reasoning_effort) or agent.get("reasoning_effort")
        tier = updates.get("service_tier", session.service_tier) or agent.get("service_tier")
        if effort is not None or tier is not None:
            catalog = await model_settings_for(
                session_manager, session.backend,
                updates.get("credential_id", session.credential_id) or agent.get("credential_id"),
                user_id,
            )
            validate_reasoning(
                catalog, updates.get("model", session.model) or agent.get("model"), effort,
            )
            validate_service_tier(
                catalog, updates.get("model", session.model) or agent.get("model"), tier,
            )

    if "name" in fields and fields["name"] is not None:
        name = str(fields["name"]).strip()
        if not name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Session name cannot be empty"
            )
        updates["name"] = name

    if updates:
        await session_manager.db.update_session_field(session_id, **updates)
        for key, value in updates.items():
            setattr(session, key, value)

    return _to_session_info(session)


@router.post("", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_manager: SessionMgr,
    req: CreateSessionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    agent_id = req.agent_id
    # Inherit the owning agent's default backend when none is pinned. Scoped,
    # because a session takes its owner from its agent: an agent id belonging to
    # somebody else would otherwise create a session in their account.
    agent = await session_manager.db.get_agent(agent_id, user_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    backend = (
        req.backend.value
        if req.backend is not None
        else agent.get("backend") or settings.default_backend
    )
    await _check_credential_backend(session_manager, req.credential_id, backend)
    try:
        s = await session_manager.create_session(
            agent_id,
            req.name,
            req.working_dir,
            credential_id=req.credential_id,
            backend=backend,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _to_session_info(s, message_count=0)


@router.post("/{session_id}/dispatch/resume", response_model=DispatchState)
async def resume_dispatch(
    session_manager: SessionMgr,
    session_id: str,
    resume_queue: bool = True,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> DispatchState:
    session = session_manager.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    try:
        if not await session_manager.resume_dispatch(session_id, resume_queue=resume_queue):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    except QuotaExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    if session_manager.get_session(session_id, user_id) is not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return DispatchState(**session_manager.dispatch_snapshot(session_id))


@router.post("/import", response_model=SessionDetail, status_code=status.HTTP_201_CREATED)
async def import_session(
    session_manager: SessionMgr,
    req: ImportSessionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    agent_id = req.agent_id
    if agent_id is None:
        sys_agent = await session_manager.db.get_system_agent(user_id)
        agent_id = sys_agent["id"] if sys_agent else None
    elif await session_manager.db.get_agent(agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    try:
        s = await session_manager.import_session(
            name=req.name,
            working_dir=req.working_dir,
            claude_session_id=req.claude_session_id,
            credential_id=req.credential_id,
            messages=req.messages,
            agent_id=agent_id,
            backend=req.backend.value,
        )
    except CredentialBindingError as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    messages_raw = await session_manager.db.load_messages(s.id)
    messages = [MessageContent(**m) for m in messages_raw]
    return SessionDetail(
        id=s.id,
        name=s.name,
        working_dir=s.working_dir,
        status=s.status,
        created_at=s.created_at,
        message_count=s._message_count,
        claude_session_id=s.claude_session_id,
        credential_id=s.credential_id,
        agent_id=s.agent_id,
        origin=s.origin,
        backend=s.backend,
        model=s.model,
        reasoning_effort=s.reasoning_effort,
        service_tier=s.service_tier,
        parent_session_id=s.parent_session_id,
        delegation_request=s.delegation_request,
        app_id=s.app_id,
        **_fork_fields(s),
        messages=messages,
        dispatch_state=DispatchState(**session_manager.dispatch_snapshot(s.id)),
        pending_questions=[
            PendingQuestionInfo(
                question_id=q.question_id, questions=q.questions, deadline=q.deadline,
                request_id=q.request_id, attempt_id=q.attempt_id,
            )
            for q in session_manager.get_pending_questions(s.id)
        ],
        subagents=[SubagentRun(**asdict(u)) for u in s._subagents.values()],
        # High-water mark: clients use this as the dedup baseline so any
        # WS event with seq < next_message_seq is treated as already
        # applied (it's in the messages list above).
        next_message_seq=max((m.seq for m in messages if m.seq is not None), default=-1) + 1,
    )


@router.get("/{session_id}/wiki-jobs", response_model=list[JobCard])
async def wiki_jobs(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    job_id: str | None = Query(None, min_length=1),
    vault_id: str | None = Query(None, min_length=1),
    _: str = Depends(verify_token),
):
    binding = await session_manager.db.get_session_binding(session_id)
    if binding is None or binding["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if (job_id is None) != (vault_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Supply both Wiki job and vault identities")
    return await session_manager.db.wiki_job_cards(session_id, user_id, offset=offset, limit=limit,
                                                  job_id=job_id, vault_id=vault_id)


@router.post("/{session_id}/submissions", response_model=ClientSubmissionStatus | ClientSubmissionRefusal,
             response_model_exclude_none=True)
async def submit_input(session_manager: SessionMgr, session_id: str, request: ClientSubmissionRequest,
                       user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..sessions.client_submissions import submit_client_message
    from ..wiki.tasks import explicit_mode

    _own_session(session_manager, session_id, user_id)
    if not request.content.strip() and not request.attachment_ids:
        raise HTTPException(422, "Supply message content or attachments")
    return await submit_client_message(
        session_manager, session_id, request.content, request.attachment_ids, request.client_request_id,
        wiki_mode=explicit_mode(request.content, request.wiki_mode), recovery_of_client_id=request.recovery_of_client_id,
    )


@router.get("/{session_id}/submissions", response_model=SubmissionRecoveryPage)
async def get_recoverable_inputs(session_manager: SessionMgr, session_id: str,
                                before: int | None = Query(None, gt=0), limit: int = Query(20, ge=1, le=100),
                                user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..sessions.client_submissions import status_from_row
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(404, "Session not found")
    rows, cursor = await session_manager.db.list_recoverable_submissions(session_id, before=before, limit=limit)
    items = []
    for row in rows:
        status = status_from_row(session_id, row, include_input=False)
        items.append({"client_request_id": row["client_id"], "request_id": row["request_id"],
                      "execution": status["execution"], "preview": row["payload"]["content"][:200],
                      "attachment_count": len(row["payload"]["attachment_ids"])})
    return {"items": items, "next_cursor": cursor}


@router.get("/{session_id}/submissions/{client_id}", response_model=ClientSubmissionStatus)
async def get_submission_status(session_manager: SessionMgr, session_id: str, client_id: str,
                                user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..sessions.client_submissions import submission_status
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(404, "Session not found")
    receipt = await submission_status(session_manager, session_id, client_id)
    if receipt is None:
        raise HTTPException(404, "Submission not found")
    return receipt


@router.delete("/{session_id}/submissions/{client_id}/recovery", status_code=204)
async def dismiss_recoverable_input(session_manager: SessionMgr, session_id: str, client_id: str,
                                    user_id: ScopeUser = None, _: str = Depends(verify_token)):
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(404, "Session not found")
    if not await session_manager.dismiss_recoverable_input(session_id, client_id):
        raise HTTPException(404, "Recoverable input not found")


@router.get("/{session_id}/submissions/{client_id}/attachments/{attachment_id}")
async def recover_submission_attachment(session_manager: SessionMgr, session_id: str, client_id: str,
                                         attachment_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    import asyncio
    import hashlib
    from urllib.parse import quote

    from ..attachments import get_path
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(404, "Session not found")
    row = await session_manager.db.get_client_submission(session_id, client_id)
    files = (row or {}).get("payload") or {}
    saved = next((file for file in files.get("attachments", []) if file["id"] == attachment_id), None)
    path = get_path(session_id, attachment_id) if saved else None
    if path is None:
        raise HTTPException(409, "A required attachment is unavailable. Re-upload the complete files.")
    assert saved is not None

    def read():
        if path.stat().st_size != saved["size"]:
            raise ValueError("Attachment size changed")
        with path.open("rb") as stream:
            data = stream.read(saved["size"] + 1)
        if len(data) != saved["size"] or hashlib.sha256(data).hexdigest() != saved["sha256"]:
            raise ValueError("Attachment content changed")
        return data

    try:
        content = await asyncio.to_thread(read)
    except (ValueError, OSError) as error:
        raise HTTPException(409, "A required attachment is unavailable or changed. Re-upload the complete files.") from error
    return Response(content, media_type=saved["mime_type"], headers={
        "Cache-Control": "no-store", "Content-Disposition": f"attachment; filename*=utf-8''{quote(saved['filename'])}",
    })


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    # Live session: read straight from the in-memory state (includes
    # pending queue / pending questions / live status).
    s = session_manager.get_session(session_id, user_id)
    if s is not None:
        messages_raw = await session_manager.db.load_messages(
            s.id, limit=MESSAGE_WINDOW, newest_first=True
        )
        if session_manager.get_session(session_id, user_id) is not s:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
        messages = [MessageContent(**m) for m in messages_raw]
        message_count, _next_sequence, duplicates = await session_manager.db.message_sequence_state(s.id)
        if duplicates:
            # Sequence-keyed clients cannot represent these rows faithfully.
            # Keep them available through the storage-id-preserving export.
            messages = []
        oldest = messages[0].seq if messages else None
        return SessionDetail(
            id=s.id,
            name=s.name,
            working_dir=s.working_dir,
            status=s.status,
            created_at=s.created_at,
            message_count=message_count,
            transcript_integrity="duplicate_seq" if duplicates else "ok",
            claude_session_id=s.claude_session_id,
            credential_id=s.credential_id,
            agent_id=s.agent_id,
            origin=s.origin,
            backend=s.backend,
            model=s.model,
            reasoning_effort=s.reasoning_effort,
            service_tier=s.service_tier,
            parent_session_id=s.parent_session_id,
            delegation_request=s.delegation_request,
            app_id=s.app_id,
            **_fork_fields(s),
            messages=messages,
            active_attempt={"request_id": s._run.request.request_id, "attempt_id": s._run.question_attempt_id}
            if s._run.request and s._run.question_attempt_id else None,
            dispatch_state=DispatchState(**session_manager.dispatch_snapshot(s.id)),
            pending_questions=[
                PendingQuestionInfo(
                    question_id=q.question_id, questions=q.questions, deadline=q.deadline,
                    request_id=q.request_id, attempt_id=q.attempt_id,
                )
                for q in session_manager.get_pending_questions(s.id)
            ],
            subagents=[SubagentRun(**asdict(u)) for u in s._subagents.values()],
            oldest_loaded_seq=oldest,
            has_more_messages=await session_manager.db.has_messages_before(s.id, oldest),
            # The allocator may include an unsaved terminal reservation, or
            # advance while this snapshot is loading. A dedup watermark must
            # describe these exact rows, never the in-memory allocator.
            next_message_seq=max((m.seq for m in messages if m.seq is not None), default=-1) + 1,
        )

    # Archived session: not in memory; read history straight from DB.
    archived_detail = await session_manager.load_archived_session_detail(session_id, user_id)
    if archived_detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return archived_detail


@router.get("/{session_id}/transcript-export")
async def export_transcript(
    session_manager: SessionMgr, session_id: str, user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    binding = await session_manager.db.get_session_binding(session_id)
    if binding is None or binding["user_id"] != user_id:
        raise HTTPException(404, "Session not found")
    return StreamingResponse(
        session_manager.db.raw_message_rows(session_id, user_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="transcript.jsonl"'},
    )


@router.get("/{session_id}/messages", response_model=MessagePage)
async def older_messages(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    before_seq: int = Query(..., ge=0, description="Return messages with seq < this"),
    limit: int = Query(MESSAGE_WINDOW, ge=1, le=500),
    _: str = Depends(verify_token),
):
    """The page of transcript immediately before `before_seq`, oldest-first.

    Scroll-back for the windowed open above. Live and archived sessions read the
    same rows — the transcript lives in the database either way — so this does
    not care which the id refers to; it only refuses an id with no rows at all,
    which is the same 404 as opening it.

    The archived fallback has to carry the scope too. It reads the database
    rather than the in-memory map, so a bare "does a row with this id exist"
    answered *yes* for another account's session and handed over the transcript
    — the live lookup above was scoped and this one silently was not. It is the
    exact shape of leak that makes per-route scoping a bad idea, and the
    cross-tenant test is what found it.
    """
    if session_manager.db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no database")
    if session_manager.get_session(session_id, user_id) is None:
        rows = await session_manager.db.load_sessions(include_archived=True)
        owned = [
            r
            for r in rows
            if r["id"] == session_id
            and (user_id is None or r.get("user_id") == user_id)
        ]
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if before_seq == 0:
        return MessagePage(messages=[], oldest_loaded_seq=None, has_more_messages=False)
    if (await session_manager.db.message_sequence_state(session_id))[2]:
        raise HTTPException(409, {"code": "duplicate_message_sequences", "message": "Export and review the original transcript rows before paging"})
    raw = await session_manager.db.load_messages(
        session_id, limit=limit, max_seq=before_seq - 1, newest_first=True
    )
    messages = [MessageContent(**m) for m in raw]
    oldest = messages[0].seq if messages else None
    return MessagePage(
        messages=messages,
        oldest_loaded_seq=oldest,
        has_more_messages=await session_manager.db.has_messages_before(session_id, oldest),
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    _own_session(session_manager, session_id, user_id)
    deleted = await session_manager.delete_session(session_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


@router.get("/{session_id}/fork-preview")
async def fork_preview(
    session_manager: SessionMgr,
    session_id: str,
    rewind_to_msg_seq: int = Query(...),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    """Side-effect classification + revert preflight for the fork-confirm
    popover (session-rewind.md §5.6.2). Commits nothing."""
    try:
        return await session_manager.fork_preview(
            session_id, rewind_to_msg_seq, user_id
        )
    except ForkError as e:
        raise HTTPException(
            e.status_code, detail={"reason": e.reason, "message": str(e)}
        )


@router.post(
    "/{session_id}/fork",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def fork_session(
    session_manager: SessionMgr,
    session_id: str,
    req: ForkSessionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    """Fork a session at a chosen user message (session-rewind.md §5.1).
    409 responses carry a structured `{reason, backend}` body."""
    try:
        fork = await session_manager.fork_session(
            session_id,
            req.rewind_to_msg_seq,
            revert_files=req.revert_files,
            label=req.label,
            user_id=user_id,
        )
    except ForkError as e:
        raise HTTPException(
            e.status_code, detail={"reason": e.reason, "message": str(e)}
        )
    except BackendForkNotSupported as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "fork_not_supported_on_backend",
                "backend": e.backend,
            },
        )
    return _to_session_info(fork)


@router.post(
    "/{session_id}/duplicate",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_session(
    session_manager: SessionMgr,
    session_id: str,
    req: DuplicateSessionRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    """`/fork`: duplicate a session onto an independent full copy of its working
    directory (session-fork.md). The parent is left untouched. 409
    responses carry a structured `{reason}` / `{reason, backend}` body."""
    try:
        fork = await session_manager.duplicate_session(
            session_id, label=req.label, user_id=user_id
        )
    except ForkError as e:
        raise HTTPException(
            e.status_code, detail={"reason": e.reason, "message": str(e)}
        )
    except BackendForkNotSupported as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "fork_not_supported_on_backend",
                "backend": e.backend,
            },
        )
    return _to_session_info(fork)


@router.post("/{session_id}/reset")
async def reset_session(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    _own_session(session_manager, session_id, user_id)
    try:
        await session_manager.reset_session(session_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return {"status": "ok"}


@router.post(
    "/{session_id}/archive",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def archive_session(
    session_manager: SessionMgr,
    session_id: str,
    replace: bool = Query(True),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    """Archive the current session and return a fresh one.

    Same name / working_dir / credential_id as the archived session,
    but a brand-new id and no message history. Schedules
    mappings repoint from old to new so user-facing automation
    continues uninterrupted.

    `?replace=false` archives without a successor — the sidebar's archive
    button, which puts a conversation away rather than restarting it — and
    answers 204. Either way the history stays and `/unarchive` brings it back.
    """
    _own_session(session_manager, session_id, user_id)
    try:
        new = await session_manager.archive_session(session_id, replace=replace)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if new is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _to_session_info(new)


@router.post("/{session_id}/unarchive", response_model=SessionInfo)
async def unarchive_session(
    session_manager: SessionMgr,
    session_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    """Bring an archived session back as a live session.

    Flips the DB row's `archived=0` and reloads it into the in-memory
    session map so writes (sendMessage, schedules, etc.) work again.
    """
    try:
        s = await session_manager.unarchive_session(session_id, user_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return _to_session_info(s)

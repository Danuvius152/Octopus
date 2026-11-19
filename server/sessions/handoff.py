"""A same-chat handoff retains the admitted human contract and starts fresh native history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from ..mcp_identity import McpScope, require_live_scope, scope_is_live
from .base import TICKET_BATCH_ORIGINS, QueuedPrompt

if TYPE_CHECKING:
    from ..knowledge import AdmittedRequest, KnowledgeContext
    from . import SessionManager
    from .base import Session


Pointer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
ProgressQuestion = Annotated[str, StringConstraints(min_length=1)]
_CLAUDE_FIRST_INPUT_WAIT_SECONDS = 3.0


class HandoffPointers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: Pointer
    accepted_ticket: Pointer
    candidate: Pointer
    next_work: Pointer
    answered_progress_questions: tuple[ProgressQuestion, ...] = ()

    def prompt(self, handoff_id: str) -> str:
        return (f"[ticket-handoff {handoff_id}] Query the handoff tool once with this ID and no pointers. "
                "On started, locate this ID in accepted_ticket and record confirmation there. "
                "Start from next_work, the relevant part of spec, and the fixed candidate. "
                "Consult earlier acceptance evidence for a dependency, check reuse, recovery, or final acceptance. "
                "If the receipt is unconfirmed, leave the handoff pending.\n"
                + self.model_dump_json(exclude={"answered_progress_questions"}))

    def digest(self) -> str:
        """Idempotency binds the submitted material, independently of prompt wording."""
        data = json.dumps(self.model_dump(exclude_defaults=True), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def matches_legacy(self, payload: dict[str, Any] | None) -> bool:
        if (not payload or payload["source"] != "handoff" or "answered_progress_request_ids" in payload
                or self.answered_progress_questions):
            return False
        # Original submissions ended with the four JSON pointers.
        try:
            return self == self.model_validate_json(payload["operation"]["content"].rsplit("\n", 1)[-1])
        except ValidationError:
            return False


async def resolve_answered_progress(manager: SessionManager, chat: Session, parent: AdmittedRequest,
                                    carriers: list[str], questions: tuple[str, ...]) -> list[str]:
    from .client_submissions import confirmed_execution

    if not questions:
        return []
    db = manager.input_database()
    matches: dict[str, list[str]] = {question: [] for question in questions}
    for carrier in carriers:
        if not await db.request_in_model_history(chat.id, carrier, None):
            continue
        confirmed = await confirmed_execution(db, chat.id, carrier, require_terminal=carrier != parent.reference.request_id)
        if confirmed is None or confirmed.original.origin != parent.context.origin:
            raise ValueError("The ticket batch has unconfirmed human input")
        for _, edit in confirmed.steers:
            text = edit.context.trusted_request
            if text in matches:
                matches[text].append(edit.reference.request_id)
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError("Each answered progress question must exactly match one confirmed human steer in this ticket")
    return [found[0] for found in matches.values()]


async def continuation_context(manager: SessionManager, chat: Session, request_id: str) -> KnowledgeContext | None:
    from .client_submissions import confirmed_execution

    db = manager.input_database()
    row = await db.get_submission_for_request(chat.id, request_id)
    payload = row["payload"] if row else None
    if (not payload or payload.get("handoff_backend") != chat.backend
            or payload.get("previous_native_id") != chat.claude_session_id):
        return None
    parent = await db.get_submission_for_request(chat.id, payload["handoff_from"])
    if parent is None or (parent["terminal"] or {}).get("status") != "success":
        return None
    closed = await confirmed_execution(db, chat.id, parent["request_id"])
    if closed is None:
        return None
    return await updated_batch_context(
        manager, chat, closed.effective, frozenset(payload.get("answered_progress_request_ids", ())),
    )


async def updated_batch_context(manager: SessionManager, chat: Session, context: KnowledgeContext,
                                answered_progress: frozenset[str] = frozenset()) -> KnowledgeContext | None:
    from ..knowledge import revise
    from .client_submissions import confirmed_execution

    if context.origin.session_id != chat.id or context.origin.user_id != chat.user_id:
        return None
    db = manager.input_database()
    carriers = await db.handoff_human_executions(chat.id, context.origin.request_id)
    if carriers is None:
        return None
    authority = None
    steers: list[tuple[int, AdmittedRequest]] = []
    for carrier in carriers:
        confirmed = await confirmed_execution(db, chat.id, carrier)
        if confirmed is None or confirmed.original.origin != context.origin:
            return None
        if carrier == context.origin.request_id:
            authority = confirmed.original
        steers.extend(confirmed.steers)
    # Start from the verified human request to avoid duplicating edits that
    # the parent or a previous handoff already includes.
    if authority is None or authority.registration != context.registration:
        return None
    earlier_answered = await db.answered_handoff_progress_ids(chat.id, context.origin.request_id)
    answered = earlier_answered | answered_progress
    mode = "read" if authority.mode == "read" else context.mode
    for _, edit in sorted(steers, key=lambda member: member[0]):
        # A presentation omission does not change the edit's structured requirements.
        changes = {} if edit.reference.request_id in answered else {"prompt": edit.context.trusted_request}
        authority = revise(authority, edit, changes, human=True)
    limits = authority.maintenance.restrict(context.maintenance)
    if mode == "maintain" and not limits.may_submit:
        mode = "read"
    return replace(context, trusted_request=authority.trusted_request, maintenance=limits, mode=mode,
                   sources=tuple(dict.fromkeys((*authority.sources, *context.sources))))


def handoff_receipt(row: dict[str, Any], handoff_id: str) -> dict[str, Any]:
    payload = row["payload"] or {}
    if payload.get("source") != "handoff" or payload.get("delivery_id") != handoff_id:
        raise ValueError("This identifier belongs to a different input")
    state = row["execution_state"]
    fresh = payload.get("fresh_native_id")
    if fresh:
        state = "started"
    elif row["delivery_state"] == "unknown" or state == "unknown":
        state = "unknown"
    elif state == "pending":
        state = "registered"
    elif state == "running":
        state = "starting"
    elif state == "done":
        state = "failed" if row["delivery_state"] == "none" else "unknown"
    return {"handoff_id": handoff_id, "request_id": row["request_id"], "state": state,
            "native_id": fresh, "reason": row["reason_code"]}


def _main_chat(manager: SessionManager, scope: McpScope) -> Session:
    require_live_scope(scope, scope.session_id)
    session = manager.get_session(scope.session_id, scope.user_id)
    if session is None or session.user_id != scope.user_id or session.origin not in TICKET_BATCH_ORIGINS:
        raise ValueError("Ticket batches require the original main chat")
    return session


async def begin_ticket_batch(manager: SessionManager, scope: McpScope) -> dict[str, str]:
    from .owner_runtime import RegisterTicketBatch

    session = _main_chat(manager, scope)
    return await manager._session_owner(session).call_runtime(RegisterTicketBatch(scope))


async def ticket_handoff(manager: SessionManager, scope: McpScope, handoff_id: str,
                         pointers: HandoffPointers | None = None) -> dict[str, Any]:
    """Passing only the original ID reads its receipt and never schedules work."""
    from .owner_runtime import Submit

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}", handoff_id):
        raise ValueError("A ticket handoff ID must be 1–96 letters, digits, dots, dashes or underscores")
    session = _main_chat(manager, scope)
    client_id = f"handoff:{handoff_id}"
    if pointers is not None:
        request = QueuedPrompt(pointers.prompt(handoff_id), [], source="handoff", delivery_id=handoff_id)
        receipt = await manager._session_owner(session).call_runtime(Submit(request, handoff=(scope, pointers)))
        if not receipt.accepted:
            raise ValueError("The original chat is closed to handoff")
    db = manager.input_database()
    row = await db.get_client_submission(session.id, client_id)
    run = session._run
    backend = run.backend
    settlement = run.settlement
    if (pointers is None and row and (row["payload"] or {}).get("source") == "handoff"
            and session.backend == "claude-code" and backend
            and settlement and settlement.request_id == row["request_id"]
            and backend.run_lease_id == scope.run_lease_id and backend.input_may_have_run):
        clock = asyncio.get_running_loop().time
        deadline = clock() + _CLAUDE_FIRST_INPUT_WAIT_SECONDS
        # Only this live attempt can prove which native opening input was sent.
        while (row and not row["payload"].get("fresh_native_id")
               and session._run is run and run.backend is backend and run.settlement is settlement and scope_is_live(scope)):
            await backend.record_installed_input()
            row = await db.get_client_submission(session.id, client_id)
            remaining = deadline - clock()
            if not row or row["payload"].get("fresh_native_id") or remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
    return handoff_receipt(row, handoff_id) if row is not None else {"handoff_id": handoff_id, "state": "unregistered"}

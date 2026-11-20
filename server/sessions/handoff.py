"""A same-chat handoff retains the admitted human contract and starts fresh native history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ..mcp_identity import McpScope, require_live_scope, scope_is_live
from ..session_operations import (
    HandoffHumanScope,
    HandoffScopeClause,
    SavedHandoffSelection,
    TicketPhase,
    handoff_human_scope,
)
from .base import TICKET_BATCH_ORIGINS, QueuedPrompt

if TYPE_CHECKING:
    from ..database import Database
    from ..knowledge import AdmittedRequest, KnowledgeContext, RequestRef
    from ..models import MessageContent
    from . import SessionManager
    from .base import Session


Pointer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
ProgressQuestion = Annotated[str, StringConstraints(min_length=1)]
_CLAUDE_FIRST_INPUT_WAIT_SECONDS = 3.0
TICKET_BATCH_RETURN_STATES = frozenset({"registered", "finishing"})
FINISH_TICKET_INSTRUCTIONS = (
    "The user has limited this batch to acceptance of the current ticket. Complete that ticket's checks and review, "
    "including any necessary corrections. After recording acceptance, call mcp__ask_agent__finish_ticket_batch "
    "and report the outcome. Do not start another ticket or batch acceptance."
)
TICKET_BATCH_CANDIDATE_INSTRUCTIONS = (
    "Read the human message below before continuing. If it directs you to complete acceptance of the current "
    "ticket and stop the batch, call mcp__ask_agent__limit_ticket_batch with the exact human message. "
    "If it supplements the authorized batch, call mcp__ask_agent__adopt_ticket_batch_update with the exact message. "
    "A separate task is a new request."
)


class HumanScopeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: ProgressQuestion
    carry: tuple[HandoffScopeClause, ...]
    request_id: str | None = Field(default=None, min_length=1, description="Confirmed message ID supplied for a scope correction")


class _ScopeCorrection(ValueError):
    def __init__(self, selections: list[HumanScopeSelection]) -> None:
        super().__init__("Correct the previously carried excerpts from the confirmed originals and retry this handoff")
        self.selections = selections


_SCOPE_INSTRUCTIONS = {
    "batch": "",
    "next": "这条要求只适用于当前票。\n",
}


class HandoffPointers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: Pointer
    accepted_ticket: Pointer
    candidate: Pointer
    next_work: Pointer
    next_phase: TicketPhase = "ticket"
    answered_progress_questions: tuple[ProgressQuestion, ...] = ()
    human_scope: tuple[HumanScopeSelection, ...] = ()

    def prompt(self, handoff_id: str) -> str:
        return (f"[ticket-handoff {handoff_id}] Query the handoff tool once with this ID and no pointers. "
                "On started, locate this ID in accepted_ticket and record confirmation there. "
                "Start from next_work, the relevant part of spec, and the fixed candidate. "
                "Consult earlier acceptance evidence for a dependency, check reuse, recovery, or final acceptance. "
                "If the receipt is unconfirmed, leave the handoff pending.\n"
                + self.model_dump_json(exclude_defaults=True, exclude={"answered_progress_questions", "human_scope"}))

    def digest(self) -> str:
        """Idempotency binds the submitted material, independently of prompt wording."""
        data = json.dumps(self.model_dump(exclude_defaults=True), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def matches_legacy(self, payload: dict[str, Any] | None) -> bool:
        if (not payload or payload["source"] != "handoff" or "answered_progress_request_ids" in payload
                or "handoff_human_scope" in payload or self.answered_progress_questions or self.human_scope):
            return False
        # Original submissions ended with the four JSON pointers.
        try:
            return self == self.model_validate_json(payload["operation"]["content"].rsplit("\n", 1)[-1])
        except ValidationError:
            return False


def _ordered_scope_clauses(text: str, clauses: tuple[HandoffScopeClause, ...]) -> tuple[HandoffScopeClause, ...]:
    spans = [_locate_scope_clause(text, clause) for clause in clauses]
    end = 0
    ordered = []
    for start, stop, clause in sorted(spans, key=lambda span: span[0]):
        if start < end:
            raise ValueError("Carried clauses must not overlap")
        ordered.append(clause)
        end = stop
    return tuple(ordered)


def _locate_scope_clause(text: str, clause: HandoffScopeClause) -> tuple[int, int, HandoffScopeClause]:
    if not clause.text.strip():
        raise ValueError("Carried clauses require nonempty original text")
    start = clause.start
    if start is None:
        start = text.find(clause.text)
        if start < 0 or start != text.rfind(clause.text):
            raise ValueError("Each carried clause must occur exactly once in its original human update "
                             "unless its start offset identifies an exact occurrence")
        clause = clause.model_copy(update={"start": start})
    if not text.startswith(clause.text, start):
        raise ValueError("The carried clause must match the original human update at the given start offset")
    return start, start + len(clause.text), clause


def _applicable_scope(text: str, selection: SavedHandoffSelection) -> tuple[HandoffScopeClause, ...] | None:
    # Selections saved by the old writer have no validated offsets. Octo must
    # revisit the entire original before any of those excerpts can carry again.
    if any(clause.start is None for clause in selection.clauses):
        return None
    try:
        clauses = _ordered_scope_clauses(text, selection.clauses)
    except ValueError:
        return None
    return tuple(clause for clause in clauses if clause.scope == "batch" or not selection.next_expired)


def _scope_projection(clauses: tuple[HandoffScopeClause, ...], seq: int) -> str:
    return "\n".join(_SCOPE_INSTRUCTIONS[clause.scope] + (
        f"原聊天消息 #{seq} 中有一项适用限制。暂缓执行引用中指明的操作，继续其余已授权工作。\n"
        f"须暂缓的操作（原文引用）\n{json.dumps(clause.text, ensure_ascii=False)}"
        if clause.hold else clause.text
    ) for clause in clauses)


@dataclass
class _ConfirmedBatchInputs:
    original: KnowledgeContext
    updates: list[tuple[int, AdmittedRequest]]
    adopted: set[str]


async def _confirmed_batch_inputs(db: Database, origin: RequestRef,
                                  carriers: list[str], *, require_terminal: bool = False,
                                  amending: str | None = None,
                                  ) -> _ConfirmedBatchInputs | None:
    from .client_submissions import confirmed_execution

    original = None
    steers: list[tuple[int, AdmittedRequest]] = []
    adopted: set[str] = set()
    for carrier in carriers:
        confirmed = await confirmed_execution(db, origin.session_id, carrier, require_terminal=require_terminal)
        if confirmed is None:
            return None
        proposed = carrier == amending and carrier != origin.request_id and confirmed.adopted_for is None
        if (confirmed.adopted_for == origin.request_id or proposed) and confirmed.primary is not None:
            steers.append(confirmed.primary)
            adopted.add(confirmed.primary[1].reference.request_id)
        elif confirmed.original.origin != origin:
            return None
        if carrier == origin.request_id:
            original = confirmed.original
        steers.extend(confirmed.steers)
    if original is None:
        return None
    return _ConfirmedBatchInputs(original, sorted(steers, key=lambda member: member[0]), adopted)


async def resolve_handoff_human_scope(manager: SessionManager, chat: Session, parent: AdmittedRequest,
                                      carriers: list[str], pointers: HandoffPointers) -> HandoffHumanScope:
    db = manager.input_database()
    origin = parent.context.origin
    confirmed = await _confirmed_batch_inputs(db, origin, carriers)
    if confirmed is None or confirmed.original.registration != parent.context.registration:
        raise ValueError("The ticket batch has unconfirmed human input")
    earlier = await db.saved_handoff_human_scope(chat.id, origin.request_id, advancing=True)
    pending: dict[str, str] = {}
    repairs: list[HumanScopeSelection] = []
    for _, edit in confirmed.updates:
        input_id, text = edit.reference.request_id, edit.context.trusted_request
        prior = earlier.get(input_id)
        if prior is None:
            pending[input_id] = text
        elif _applicable_scope(text, prior) is None:
            pending[input_id] = text
            repairs.append(HumanScopeSelection(request_id=input_id, message=text, carry=prior.clauses))
    # If the same text was submitted repeatedly, cite its latest confirmed occurrence.
    latest = {text: request_id for request_id, text in pending.items()}
    decisions: HandoffHumanScope = {}
    for selection in pointers.human_scope:
        request_id = selection.request_id or latest.get(selection.message)
        if request_id is None or pending.get(request_id) != selection.message or request_id in decisions:
            raise ValueError("Each carried update must match an unreviewed, confirmed human message")
        decisions[request_id] = _ordered_scope_clauses(selection.message, selection.carry)
    corrections = [repair for repair in repairs if repair.request_id not in decisions]
    if corrections:
        raise _ScopeCorrection(corrections)
    for question in dict.fromkeys(pointers.answered_progress_questions):
        matching = [request_id for request_id, text in pending.items() if text == question
                    and request_id not in earlier
                    and await db.request_in_model_history(chat.id, request_id, None)]
        if len(matching) != 1 or decisions.get(matching[0]):
            raise ValueError("Each answered progress question must exactly match one confirmed human steer in this ticket")
    return dict.fromkeys(pending, ()) | decisions


async def continuation_context(manager: SessionManager, chat: Session, request_id: str) -> KnowledgeContext | None:
    from .client_submissions import confirmed_execution, saved_context

    db = manager.input_database()
    row = await db.get_submission_for_request(chat.id, request_id)
    if row is None:
        return None
    payload = row["payload"]
    if (not payload or payload.get("handoff_backend") != chat.backend
            or payload.get("previous_native_id") != chat.claude_session_id):
        return None
    parent = await db.get_submission_for_request(chat.id, payload["handoff_from"])
    if parent is None or (parent["terminal"] or {}).get("status") != "success":
        return None
    closed = await confirmed_execution(db, chat.id, parent["request_id"])
    registered = saved_context(chat.id, row)
    if closed is None or registered is None:
        return None
    origin = registered.origin
    if closed.original.origin != origin and closed.adopted_for != origin.request_id:
        return None
    if await db.ticket_batch_state(chat.id, origin.request_id) != "registered":
        return None
    return await updated_batch_context(manager, chat, registered, handoff_human_scope(payload))


async def updated_batch_context(manager: SessionManager, chat: Session, context: KnowledgeContext,
                                new_scope: HandoffHumanScope | None = None, *,
                                amending: str | None = None) -> KnowledgeContext | None:
    if context.origin.session_id != chat.id or context.origin.user_id != chat.user_id:
        return None
    db = manager.input_database()
    carriers = await db.handoff_human_executions(chat.id, context.origin.request_id, amending=amending)
    if carriers is None:
        return None
    finishing = new_scope is None and await db.ticket_batch_state(chat.id, context.origin.request_id) == "finishing"
    # Confirmed human input survives an interrupted carrier. Starting a new
    # native history also requires the immediate parent to finish successfully.
    confirmed = await _confirmed_batch_inputs(db, context.origin, carriers, amending=amending)
    # Start from the verified human request to avoid duplicating edits that
    # the parent or a previous handoff already includes.
    if confirmed is None or confirmed.original.registration != context.registration:
        return None
    decisions = await db.saved_handoff_human_scope(chat.id, context.origin.request_id, advancing=new_scope is not None)
    if new_scope is not None:
        decisions.update({request_id: SavedHandoffSelection(clauses, next_expired=False)
                          for request_id, clauses in new_scope.items()})
    return _project_batch_context(context, confirmed, decisions, finishing=finishing)


def _project_batch_context(context: KnowledgeContext, confirmed: _ConfirmedBatchInputs,
                           decisions: dict[str, SavedHandoffSelection], *, finishing: bool = False) -> KnowledgeContext:
    from ..knowledge import revise

    authority = confirmed.original
    mode = "read" if authority.mode == "read" else context.mode
    sources = dict.fromkeys(authority.sources)
    for seq, edit in confirmed.updates:
        text = edit.context.trusted_request
        selection = decisions.get(edit.reference.request_id)
        clauses: tuple[HandoffScopeClause, ...] | None = ()
        presentation = None
        if selection is not None:
            clauses = _applicable_scope(text, selection)
            if clauses is None:
                presentation = f"请核对原聊天消息 #{seq} 中未确认的限制，暂缓相关操作，继续其余已授权工作。"
            else:
                presentation = _scope_projection(clauses, seq)
        # Source commands are admitted from the leading text of a human input.
        if selection is None or any(not clause.hold and clause.start is not None and not text[:clause.start].strip()
                                    for clause in clauses or ()):
            sources.update(dict.fromkeys(edit.context.sources))
        if (finishing or edit.reference.request_id in confirmed.adopted) and (presentation is None or presentation):
            authority = replace(authority, maintenance=authority.maintenance.restrict(edit.context.maintenance))
            if edit.context.mode == "read":
                mode = "read"
        authority = revise(authority, edit, {"prompt": text}, human=True, presented_prompt=presentation)
    limits = authority.maintenance.restrict(context.maintenance)
    if mode == "maintain" and not limits.may_submit:
        mode = "read"
    return replace(context, trusted_request=authority.trusted_request, presented_request=authority.presented_request,
                   maintenance=limits, mode=mode, sources=tuple(sources))


async def recheck_replay_scope(db: Database, messages: list[MessageContent], *,
                               user_id: str | None) -> list[MessageContent]:
    """Reproject a legacy handoff at the boundary it originally received."""
    from ..fork_helpers import last_handoff_index
    from ..knowledge import decode_context, encode

    index = last_handoff_index(messages)
    if index is None:
        return messages
    message = messages[index]
    assert message.tool_input is not None
    metadata = message.tool_input["request_context"]
    snapshot = decode_context(json.dumps(metadata.get("knowledge")))
    if snapshot is None or metadata.get("handoff_scope_verified") is True:
        return messages
    request_id = metadata.get("request_id")
    projected = None
    if snapshot.origin.user_id == user_id:
        if isinstance(request_id, str):
            projected = await _legacy_replay_scope(db, message, snapshot, request_id)
        if projected is None and (snapshot.presented_request is None or _matches_copied_batch_request(messages, snapshot)):
            return messages
    if projected is None:
        replay_context = metadata | {"knowledge": None, "replay_scope_unconfirmed": True}
    else:
        replay_context = metadata | {"knowledge": json.loads(encode(projected))}
    replay_metadata = message.tool_input | {"request_context": replay_context}
    return [*messages[:index], message.model_copy(update={"tool_input": replay_metadata}), *messages[index + 1:]]


def _matches_copied_batch_request(messages: list[MessageContent], snapshot: KnowledgeContext) -> bool:
    from ..knowledge import decode_context
    from ..models import MessageRole

    for message in messages:
        metadata = (message.tool_input or {}).get("request_context")
        if (message.role != MessageRole.user or message.type != "text" or not isinstance(metadata, dict)
                or metadata.get("source") != "human" or metadata.get("request_id") != snapshot.origin.request_id):
            continue
        original = decode_context(json.dumps(metadata.get("knowledge")))
        if original and original.origin == snapshot.origin and original.registration == snapshot.registration:
            return snapshot.presented_request == original.trusted_request
    return False


async def _legacy_replay_scope(db: Database, message: MessageContent, snapshot: KnowledgeContext,
                               request_id: str) -> KnowledgeContext | None:
    from .client_submissions import saved_context

    origin = snapshot.origin
    row = await db.get_submission_for_request(origin.session_id, request_id)
    payload = row["payload"] if row else None
    if (row is None or not payload or payload.get("source") != "handoff" or not payload.get("fresh_native_id")
            or payload["operation"]["content"] != message.content or saved_context(origin.session_id, row) != snapshot):
        return None
    carriers = await db.handoff_human_executions(origin.session_id, origin.request_id, before_handoff=request_id)
    if carriers is None:
        return None
    confirmed = await _confirmed_batch_inputs(db, origin, carriers, require_terminal=True)
    if confirmed is None or confirmed.original.registration != snapshot.registration:
        return None
    decisions = await db.saved_handoff_human_scope(origin.session_id, origin.request_id, through_handoff=request_id)
    projected = _project_batch_context(snapshot, confirmed, decisions)
    return projected if projected.trusted_request == snapshot.trusted_request else None


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
    elif state == "withdrawn" and (row["terminal"] or {}).get("status") == "failed":
        state = "failed"
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


async def finish_ticket_batch(manager: SessionManager, scope: McpScope) -> dict[str, str]:
    from .owner_runtime import FinishTicketBatch

    session = _main_chat(manager, scope)
    return await manager._session_owner(session).call_runtime(FinishTicketBatch(scope))


async def limit_ticket_batch(manager: SessionManager, scope: McpScope, message: str) -> dict[str, str]:
    from .owner_runtime import ApplyTicketBatchInstruction

    session = _main_chat(manager, scope)
    return await manager._session_owner(session).call_runtime(ApplyTicketBatchInstruction(scope, message, limit=True))


async def adopt_ticket_batch_update(manager: SessionManager, scope: McpScope, message: str) -> dict[str, str]:
    from .owner_runtime import ApplyTicketBatchInstruction

    session = _main_chat(manager, scope)
    return await manager._session_owner(session).call_runtime(ApplyTicketBatchInstruction(scope, message, limit=False))


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
        try:
            receipt = await manager._session_owner(session).call_runtime(Submit(request, handoff=(scope, pointers)))
        except _ScopeCorrection as correction:
            return {"handoff_id": handoff_id, "state": "unregistered", "reason": str(correction),
                    "human_scope_corrections": [item.model_dump(exclude_defaults=True) for item in correction.selections]}
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

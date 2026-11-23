"""Browser input recovery, independent of the process-local dispatch queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..attachments import AttachmentUnavailable, validate_message_attachments
from ..service_health import ServiceUnavailable
from ..session_operations import adopted_ticket_batch
from ..submission_models import (
    ClientSubmissionStatus,
    CompactInProgress,
    InputAdmissionNotReady,
)
from .runtime_types import PreviousWriterUnconfirmed, QuotaExceeded

if TYPE_CHECKING:
    from ..database import Database
    from ..knowledge import AdmittedRequest, KnowledgeContext
    from ..submission_models import ClientSubmissionRequest
    from . import SessionManager
    from .base import PendingInput, SessionRequest
    from .submission import SubmissionReceipt

logger = logging.getLogger(__name__)


def input_digest(payload: dict[str, Any]) -> str:
    """Identity covers the submitted content; attachment bytes are saved too."""
    if payload.get("kind") == "compact":
        return hashlib.sha256(b'{"kind":"compact"}').hexdigest()
    value = {key: payload[key] for key in ("content", "attachment_ids", "wiki_mode")}
    if payload.get("recovery_of_client_id"):
        value["recovery_of_client_id"] = payload["recovery_of_client_id"]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def saved_context(session_id: str, row: dict[str, Any]) -> KnowledgeContext | None:
    """Read the committed execution contract without reinterpreting raw controls."""
    from ..knowledge import decode_context

    payload = row["payload"]
    context = decode_context(payload.get("knowledge")) if payload else None
    if (context is None or context.trusted_request != payload.get("trusted_request")
            or context.mode == "read" and context.maintenance.may_submit):
        return None
    # Owned background work retains its original caller's origin; a direct
    # human input owns its own recorded context, including a batch amendment.
    if payload.get("source") == "human" and (
            context.origin.session_id != session_id or context.origin.request_id != row["request_id"]):
        return None
    return context


@dataclass(frozen=True)
class ConfirmedExecution:
    original: KnowledgeContext
    effective: KnowledgeContext
    primary: tuple[int, AdmittedRequest] | None
    steers: list[tuple[int, AdmittedRequest]]
    adopted_for: str | None
    progress_only: bool


def confirmed_human_input(session_id: str, user_id: str | None, row: dict[str, Any],
                          *, steered: bool) -> tuple[int, AdmittedRequest] | None:
    """Bind a human submission to the matching user message in its execution."""
    from ..knowledge import AdmittedRequest, RequestRef, decode_context

    payload, receipt = row["payload"], row["receipt"]
    if (not isinstance(payload, dict) or payload.get("source") != "human"
            or not isinstance(receipt, dict) or receipt.get("accepted") is not True
            or receipt.get("disposition") != ("steered" if steered else "accepted")):
        return None
    if row["delivery_state"] not in ({"confirmed"} if steered else {"none", "confirmed"}):
        return None
    entries = row["native_inputs"]
    if entries:
        kind = "steer" if steered else "start"
        if row["delivery_state"] != "confirmed" or any(item["state"] == "unknown" for item in entries) or not any(
                item["state"] == "accepted" and item["kind"] == kind and item["input_id"] == row["request_id"] for item in entries):
            return None
    context = saved_context(session_id, row)
    metadata = row["tool_input"]
    request = metadata.get("request_context") if isinstance(metadata, dict) else None
    if (context is None or context.origin.user_id != user_id or not isinstance(request, dict)
            or request.get("request_id") != row["request_id"] or request.get("source") != "human"
            or request.get("work_id") != payload.get("work_id") or payload["operation"]["kind"] != "message"
            or row["content"] != payload["operation"]["content"] or not isinstance(row["seq"], int)
            or context.trusted_request != row["content"]):
        return None
    if not steered and decode_context(json.dumps(request.get("knowledge"))) != context:
        return None
    return row["seq"], AdmittedRequest(RequestRef(session_id, row["request_id"], user_id), context, payload.get("work_id"))


async def execution_context(database: Database, session_id: str, original_row: dict[str, Any]) -> KnowledgeContext | None:
    execution = await confirmed_execution(database, session_id, original_row["request_id"])
    return execution.effective if execution else None


async def confirmed_execution(database: Database, session_id: str, request_id: str,
                              *, require_terminal: bool = True) -> ConfirmedExecution | None:
    """Recover an execution's confirmed human inputs and effective contract.

    Continuations require the original terminal. Handoff registration can inspect
    the live carrier to resolve message identity. Both read the same persisted
    delivery acknowledgments and verify the execution owner and message order.
    """
    try:
        snapshot = await database.execution_context_inputs(session_id, request_id)
    except (KeyError, TypeError, ValueError):
        return None
    return confirmed_execution_from_inputs(session_id, snapshot, require_terminal=require_terminal) if snapshot is not None else None


def confirmed_execution_from_inputs(session_id: str, snapshot: dict[str, Any], *, require_terminal: bool) -> ConfirmedExecution | None:
    from ..knowledge import decode_context, revise

    try:
        original, owner = snapshot["original"], snapshot["owner"]
        if not isinstance(original["payload"], dict):
            return None
        context = saved_context(session_id, original)
        if (context is None or context.origin.user_id != owner or (require_terminal and original["terminal"] is None)
                or original["delivery_state"] == "unknown"
                or original["execution_owner"] != original["request_id"]):
            return None
        base = context
        primary_row = snapshot["primary"]
        primary = confirmed_human_input(session_id, owner, primary_row, steered=False) if primary_row is not None else None
        seen = set()
        steers: list[tuple[int, AdmittedRequest]] = []
        for inline in snapshot["inputs"]:
            payload, receipt = inline["payload"], inline["receipt"]
            if (not isinstance(payload, dict) or payload.get("source") != "human"
                    or not isinstance(receipt, dict) or receipt.get("accepted") is not True
                    or receipt.get("disposition") != "steered"):
                return None
            delivery = inline["delivery_state"]
            if delivery == "none":
                continue  # The durable unknown marker precedes any frame send.
            if delivery != "confirmed" or inline["request_id"] in seen:
                return None
            seen.add(inline["request_id"])
            member = confirmed_human_input(session_id, owner, inline, steered=True)
            if member is None:
                return None
            _, admitted = member
            merged = decode_context(json.dumps(inline["tool_input"]["request_context"].get("knowledge")))
            expected = revise(context, admitted, {"prompt": admitted.context.trusted_request}, human=True)
            if merged is None or merged != expected:
                return None
            context = merged
            steers.append(member)
        return ConfirmedExecution(base, context, primary, steers, adopted_ticket_batch(original["payload"]),
                                  original["payload"].get("ticket_batch_progress_only", False))
    except (KeyError, TypeError, ValueError):
        return None


def _files(session_id: str, identifiers: list[str], *, user_id: str) -> list[dict[str, Any]]:
    result = []
    for record in validate_message_attachments(session_id, identifiers, user_id=user_id):
        try:
            with record.path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as error:
            raise AttachmentUnavailable() from error
        result.append({"id": record.id, "filename": record.filename, "size": record.size,
                       "mime_type": record.mime_type, "sha256": digest})
    return result


async def load_pending_input(manager: SessionManager, session_id: str, pending: PendingInput) -> SessionRequest:
    from .base import CompactRequest, QueuedPrompt

    row = await manager.input_database().get_submission_for_request(session_id, pending.request_id)
    # Stop can retain an unstarted original claim in this process's queue.
    # It is reloadable only under that same generation and before any frame
    # delivery; restart/unknown inputs never re-enter this queue.
    retained_claim = row is not None and row["execution_state"] == "running" and (
        row["execution_generation"] == manager._dispatch_generation and row["delivery_state"] == "none"
    )
    if row is None or not (row["execution_state"] == "pending" or retained_claim) or row["payload"] is None:
        raise ValueError("The queued input is no longer pending")
    payload = row["payload"]
    operation = payload["operation"]
    if operation["kind"] == "compact":
        return CompactRequest(request_id=row["request_id"], priority=pending.priority, order=payload["order"],
                              native_session_id=payload["native_session_id"])
    context = saved_context(session_id, row)
    session = manager.get_session(session_id)
    if context is None or session is None or context.origin.user_id != session.user_id:
        raise ValueError("The saved input has no valid request context")
    return QueuedPrompt(
        operation["content"], operation["attachment_ids"], wiki_mode=context.mode,
        trusted_request=payload["trusted_request"], knowledge_context=context,
        source=payload["source"], priority=pending.priority, request_id=row["request_id"],
        delivery_id=payload["delivery_id"], work_id=payload["work_id"], order=payload["order"],
        question_session_id=payload["question_session_id"], question_id=payload["question_id"],
        batch_return=payload.get("batch_return", False),
        ticket_batch_candidate_for=payload.get("ticket_batch_candidate_for"),
    )


def status_from_row(session_id: str, row: dict[str, Any], *, include_input=True) -> dict[str, Any]:
    context = saved_context(session_id, row)
    state = row["execution_state"]
    owner = row["execution_owner"] or row["request_id"]
    terminal = row["terminal"]
    if terminal is not None and row["delivery_state"] != "unknown" and not (
        row["native_inputs"] and row["delivery_state"] == "none"
    ):
        state = "done"
    receipt = row["receipt"]
    result = {"session_id": session_id, "client_request_id": row["client_id"], "request_id": row["request_id"],
              "operation": row["payload"]["operation"]["kind"] if row["payload"] else "message",
              **(receipt or {"accepted": state in {"pending", "running", "done"}})}
    result["execution"] = {"state": state, "reason_code": row["reason_code"],
                           "state_version": row["state_version"], "updated_at": row["updated_at"],
                           "execution_owner_request_id": owner,
                           "delivery_state": row["delivery_state"],
                           "native_inputs": row["native_inputs"],
                           "recoverable_input": context is not None and state == "withdrawn"
                           and not row["dismissed"] and not row["recovered_by"]}
    if terminal is not None:
        result["outcome"] = terminal
    if row["recovery_of"]:
        result["recovery_of_client_id"] = row["recovery_of"]
    if row["recovered_by"]:
        result["recovered_by_client_id"] = row["recovered_by"]
    if state == "unknown":
        result.update(state="unknown", code="execution_unknown" if row["payload"] else "admission_unknown",
                      message={
                          "service_restart_execution_unknown": "The service restarted after execution may have begun. Review the input and conversation; it will not run again automatically.",
                          "steer_delivery_unknown": "Delivery to the active model was not acknowledged. Its result does not confirm whether this input was received; review it before starting new work.",
                          "input_acceptance_unknown": "The native runtime may have received this input, but acceptance is unconfirmed. Review the saved input and results; it will not be replayed automatically.",
                      }.get(row["reason_code"], "Execution is unconfirmed. Review the saved input and conversation; this request will not run again automatically."))
    elif receipt is None and terminal is None and state not in {"withdrawn", "rejected"}:
        result.update(accepted=False, state="unknown", code="admission_unknown",
                      message="Admission is not yet confirmed. Confirm this same submission identity before sending new work.")
    elif state == "withdrawn":
        result["message"] = ("This compaction was withdrawn before execution. It will not run again automatically."
                             if result["operation"] == "compact" else
                             "This retained input has no verifiable request context. Review the original record; it cannot be recovered."
                             if context is None else
                             "This input was withdrawn before execution. You can restore it and send it again.")
    if any(item.get("kind") == "steer" and item["state"] == "accepted" for item in row["native_inputs"]):
        result["message"] = "The native runtime accepted this input. Model processing is unconfirmed; the root result does not prove this instruction was consumed."
    if row["payload"] and row["payload"].get("source") == "handoff":
        from .handoff import handoff_receipt

        handoff = handoff_receipt(row, row["payload"]["delivery_id"])
        result["execution"]["recoverable_input"] = False
        result["message"] = f"Ticket handoff {handoff['handoff_id']}: {handoff['state']}. Check this original handoff ID to resume."
    if include_input and row["payload"] is not None:
        payload = row["payload"]
        result["input"] = (payload["operation"] if payload["operation"]["kind"] == "compact" else
            {**payload["operation"], "attachments": payload.get("attachments", []), "wiki_mode": context.mode if context else None})
    return result


async def submission_status(manager: SessionManager, session_id: str, client_id: str) -> dict[str, Any] | None:
    row = await manager.input_database().get_client_submission(session_id, client_id)
    return status_from_row(session_id, row) if row is not None else None


async def submit_client_operation(manager: SessionManager, session_id: str,
                                  request: ClientSubmissionRequest) -> dict[str, Any]:
    from ..wiki.tasks import explicit_mode

    operation = request.session_operation()
    if operation.kind == "compact":
        return await submit_client_compact(manager, session_id, request.client_request_id)
    return await submit_client_message(
        manager, session_id, operation.content, operation.attachment_ids, request.client_request_id,
        wiki_mode=explicit_mode(operation.content, operation.wiki_mode),
        recovery_of_client_id=request.recovery_of_client_id,
    )


async def _submit_client_input(manager: SessionManager, session_id: str, client_id: str, digest: str,
                               admit: Callable[[], Awaitable[SubmissionReceipt]], *, closed_message: str) -> dict[str, Any]:
    base = {"session_id": session_id, "client_request_id": client_id}
    try:
        database = manager.input_database()
    except InputAdmissionNotReady as error:
        return {**base, "accepted": False, "code": error.code, "message": str(error)}

    try:
        row = await database.get_client_submission(session_id, client_id)
        if row is None:
            try:
                receipt = await admit()
            except (QuotaExceeded, ValueError) as error:
                # The Owner can encounter an original receipt that committed
                # after our first read. Establish its state before refusing.
                database.health.require_healthy()
                row = await database.get_client_submission(session_id, client_id)
                if row is None:
                    refusal = {**base, "accepted": False, "code": getattr(error, "code", "admission_refused"),
                               "message": str(error)}
                    if isinstance(error, CompactInProgress):
                        refusal.update(canonical_request_id=error.request_id, canonical_client_request_id=error.client_id)
                    return refusal
            else:
                if not receipt.accepted:
                    return {**base, "accepted": False, "disposition": receipt.disposition, "message": closed_message}
                row = await database.get_client_submission(session_id, client_id)
                assert row is not None
        if row["payload_sha256"] != digest:
            return {**base, "accepted": False, "code": "submission_conflict",
                    "message": "This submission identity already belongs to different input."}
        status = ClientSubmissionStatus.model_validate(status_from_row(session_id, row))
        return status.model_dump(mode="json", exclude_unset=True)
    except ServiceUnavailable:
        raise
    except Exception as error:
        # An ending Owner may wrap the original failure when waking its waiter.
        # Check the captured database's health in memory before classifying it.
        database.health.require_healthy()
        logger.error("Client submission admission is unconfirmed session=%s client=%s error=%s",
                     session_id, client_id, type(error).__name__, exc_info=not isinstance(error, ValidationError))
        return {**base, "accepted": False, "state": "unknown", "code": "admission_unknown",
                "message": "Input saving is unconfirmed. Keep your draft and confirm this submission before sending new work."}


async def submit_client_compact(manager: SessionManager, session_id: str, client_id: str) -> dict[str, Any]:
    return await _submit_client_input(
        manager, session_id, client_id, input_digest({"kind": "compact"}),
        partial(manager.start_compact, session_id, client_request_id=client_id),
        closed_message="This conversation is closed to new operations.",
    )


async def submit_client_message(manager: SessionManager, session_id: str, content: str, attachment_ids: list[str],
                                client_id: str, *, wiki_mode: str | None,
                                recovery_of_client_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content, "attachment_ids": attachment_ids, "wiki_mode": wiki_mode}
    if recovery_of_client_id:
        payload["recovery_of_client_id"] = recovery_of_client_id
    return await _submit_client_input(
        manager, session_id, client_id, input_digest(payload),
        partial(manager.start_message, session_id, content, attachment_ids=attachment_ids, steerable=True,
            client_request_id=client_id, recovery_of_client_id=recovery_of_client_id,
            wiki_mode=wiki_mode),
        closed_message="This conversation is closed to new messages. Your message was not accepted.",
    )


async def claim_input(manager: SessionManager, session_id: str, request_id: str, *, owner: str | None = None) -> bool:
    return await manager.input_database().claim_client_submission(
        session_id, request_id, owner or request_id, manager._dispatch_generation)


async def validate_claimed_attachments(manager: SessionManager, session_id: str, request_id: str) -> None:
    stored = await manager.input_database().get_submission_for_request(session_id, request_id)
    if stored is not None:
        payload = stored["payload"]
        if payload is not None and payload["operation"]["kind"] == "message":
            session = manager.get_session(session_id)
            if session is None or session.user_id is None:
                raise AttachmentUnavailable()
            observed = await asyncio.to_thread(_files, session_id, payload["operation"]["attachment_ids"], user_id=session.user_id)
            if observed != payload.get("attachments", []):
                raise AttachmentUnavailable("Required attachments changed after this input was accepted. Restore the input and re-upload the complete files.")


async def require_retired_writers(manager: SessionManager, session_id: str) -> None:
    from ..execution_identity import process_group_exited
    for row in await manager.input_database().uncertain_submission_writers(session_id):
        if row["execution_generation"] in manager._retired_dispatch_generations:
            continue
        if row["delivery_state"] != "unknown" and row["terminal"] is not None:
            continue
        if not process_group_exited(row["process_group"], row["execution_boot"]):
            raise PreviousWriterUnconfirmed(
                "Previous execution has an unknown result and its process exit is unconfirmed. "
                "Hiding its saved input does not retire the writer. Wait for the original execution "
                "to stop, or restart through the managed service. Standalone execution requires "
                "independent confirmation that the previous writer has exited."
            )

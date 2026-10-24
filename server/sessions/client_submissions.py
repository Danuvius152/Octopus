"""Browser input recovery, independent of the process-local dispatch queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from ..attachments import AttachmentUnavailable, validate_message_attachments
from .runtime_types import QuotaExceeded

if TYPE_CHECKING:
    from ..knowledge import KnowledgeContext
    from . import SessionManager
    from .base import PendingInput, QueuedPrompt

logger = logging.getLogger(__name__)


def input_digest(payload: dict[str, Any]) -> str:
    """Identity covers the submitted content; attachment bytes are saved too."""
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
    # human input owns its new origin rather than borrowing another request.
    if payload.get("source") == "human" and (
            context.origin.session_id != session_id or context.origin.request_id != row["request_id"]):
        return None
    return context


def _files(session_id: str, identifiers: list[str]) -> list[dict[str, Any]]:
    result = []
    for record in validate_message_attachments(session_id, identifiers):
        with record.path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result.append({"id": record.id, "filename": record.filename, "size": record.size,
                       "mime_type": record.mime_type, "sha256": digest})
    return result


async def load_pending_input(manager: SessionManager, session_id: str, pending: PendingInput) -> QueuedPrompt:
    from .base import QueuedPrompt

    row = await manager.db.get_submission_for_request(session_id, pending.request_id)
    if row is None or row["execution_state"] != "pending" or row["payload"] is None:
        raise ValueError("The queued input is no longer pending")
    payload = row["payload"]
    context = saved_context(session_id, row)
    session = manager.get_session(session_id)
    if context is None or session is None or context.origin.user_id != session.user_id:
        raise ValueError("The saved input has no valid request context")
    return QueuedPrompt(
        payload["content"], payload["attachment_ids"], wiki_mode=context.mode,
        trusted_request=payload["trusted_request"], knowledge_context=context,
        source=payload["source"], priority=pending.priority, request_id=row["request_id"],
        delivery_id=payload["delivery_id"], work_id=payload["work_id"], order=payload["order"],
        question_session_id=payload["question_session_id"], question_id=payload["question_id"],
        durable_client_input=True,
    )


def status_from_row(session_id: str, row: dict[str, Any], *, include_input=True) -> dict[str, Any]:
    context = saved_context(session_id, row)
    state = row["execution_state"]
    owner = row["execution_owner"] or row["request_id"]
    terminal = row["terminal"]
    if terminal is not None and row["delivery_state"] != "unknown":
        state = "done"
    receipt = row["receipt"]
    result = {"session_id": session_id, "client_request_id": row["client_id"], "request_id": row["request_id"],
              **(receipt or {"accepted": state in {"pending", "running", "done"}})}
    result["execution"] = {"state": state, "reason_code": row["reason_code"],
                           "state_version": row["state_version"], "updated_at": row["updated_at"],
                           "execution_owner_request_id": owner,
                           "delivery_state": row["delivery_state"],
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
                      }.get(row["reason_code"], "Execution is unconfirmed. Review the saved input and conversation; this request will not run again automatically."))
    elif receipt is None and terminal is None and state not in {"withdrawn", "rejected"}:
        result.update(accepted=False, state="unknown", code="admission_unknown",
                      message="Admission is not yet confirmed. Confirm this same submission identity before sending new work.")
    elif state == "withdrawn":
        result["message"] = ("This retained input has no verifiable request context. Review the original record; it cannot be recovered."
                             if context is None else
                             "This input was withdrawn before execution. You can restore it and send it again.")
    if include_input and row["payload"] is not None:
        payload = row["payload"]
        result["input"] = {"content": payload["content"], "attachment_ids": payload["attachment_ids"],
                           "attachments": payload.get("attachments", []), "wiki_mode": context.mode if context else None}
    return result


async def submission_status(manager: SessionManager, session_id: str, client_id: str) -> dict[str, Any] | None:
    row = await manager.db.get_client_submission(session_id, client_id)
    return status_from_row(session_id, row) if row is not None else None


async def submit_client_message(manager: SessionManager, session_id: str, content: str, attachment_ids: list[str],
                                client_id: str, *, wiki_mode: str | None,
                                recovery_of_client_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content, "attachment_ids": attachment_ids, "wiki_mode": wiki_mode}
    if recovery_of_client_id:
        payload["recovery_of_client_id"] = recovery_of_client_id
    digest = input_digest(payload)
    base = {"session_id": session_id, "client_request_id": client_id}
    existing = await manager.db.get_client_submission(session_id, client_id)
    if existing is not None:
        if existing["payload_sha256"] != digest:
            return {**base, "accepted": False, "code": "submission_conflict", "message": "This submission identity already belongs to different input."}
        return status_from_row(session_id, existing)
    try:
        receipt = await manager.start_message(
            session_id, content, attachment_ids=attachment_ids, steerable=True,
            client_request_id=client_id, recovery_of_client_id=recovery_of_client_id,
            wiki_mode=wiki_mode,
        )
        if not receipt.accepted:
            return {**base, "accepted": False, "disposition": receipt.disposition,
                    "message": "This conversation is closed to new messages. Your message was not accepted."}
        result = await submission_status(manager, session_id, client_id)
        assert result is not None
        return result
    except (AttachmentUnavailable, QuotaExceeded, ValueError) as error:
        return {**base, "accepted": False, "message": str(error),
                "code": getattr(error, "code", "admission_refused")}
    except Exception:
        logger.exception("Client submission admission is unconfirmed session=%s client=%s", session_id, client_id)
        return {**base, "accepted": False, "state": "unknown", "code": "admission_unknown",
                "message": "Input saving is unconfirmed. Keep your draft and confirm this submission before sending new work."}


async def claim_input(manager: SessionManager, session_id: str, request_id: str, *, owner: str | None = None) -> bool:
    if manager.db is None:
        return True
    if not await manager.db.claim_client_submission(session_id, request_id, owner or request_id, manager._dispatch_generation):
        return False
    stored = await manager.db.get_submission_for_request(session_id, request_id)
    if stored is not None:
        payload = stored["payload"]
        if payload is not None:
            observed = await asyncio.to_thread(_files, session_id, payload["attachment_ids"])
            if observed != payload.get("attachments", []):
                raise AttachmentUnavailable("Required attachments changed after this input was accepted. Restore the input and re-upload the complete files.")
    return True


async def require_retired_writers(manager: SessionManager, session_id: str) -> None:
    from ..execution_identity import process_group_exited
    if manager.db is None:
        return
    for row in await manager.db.uncertain_submission_writers(session_id):
        if row["execution_generation"] in manager._retired_dispatch_generations:
            continue
        if row["delivery_state"] != "unknown" and row["terminal"] is not None:
            continue
        if not process_group_exited(row["process_group"], row["execution_boot"]):
            raise ValueError("Previous execution has an unknown result and its process exit is unconfirmed. "
                             "Hiding its saved input does not retire the writer. Wait for the original execution "
                             "to stop, or restart through the managed service. Standalone execution requires "
                             "independent confirmation that the previous writer has exited.")

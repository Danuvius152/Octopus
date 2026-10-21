"""Browser input recovery, independent of the process-local dispatch queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ..attachments import AttachmentUnavailable, validate_message_attachments
from .runtime_types import QuotaExceeded

if TYPE_CHECKING:
    from . import SessionManager

logger = logging.getLogger(__name__)


def _files(session_id: str, identifiers: list[str]) -> list[dict[str, Any]]:
    result = []
    for record in validate_message_attachments(session_id, identifiers):
        with record.path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result.append({"id": record.id, "filename": record.filename, "size": record.size,
                       "mime_type": record.mime_type, "sha256": digest})
    return result


async def status_from_row(manager: SessionManager, session_id: str, row: dict[str, Any], *, include_input=True) -> dict[str, Any]:
    state = row["execution_state"]
    owner = row["execution_owner"] or row["request_id"]
    terminal = await manager.db.get_committed_terminal(session_id, owner)
    if terminal is not None and row["reason_code"] != "steer_delivery_unknown":
        state = {"success": "succeeded", "failed": "failed", "interrupted": "interrupted"}.get(terminal.get("status"), "unknown")
    receipt = row["receipt"]
    result = {"session_id": session_id, "client_request_id": row["client_id"], "request_id": row["request_id"],
              **(receipt or {"accepted": state not in {"reserved", "unknown", "withdrawn", "rejected"}})}
    result["execution"] = {"state": state, "reason_code": row["reason_code"],
                           "state_version": row["state_version"], "updated_at": row["updated_at"],
                           "execution_owner_request_id": owner,
                           "recoverable_input": row["payload"] is not None and state == "withdrawn"
                           and not row["dismissed"] and not row["recovered_by"]}
    if terminal is not None:
        result["outcome"] = terminal
    if row["recovery_of"]:
        result["recovery_of_client_id"] = row["recovery_of"]
    if row["recovered_by"]:
        result["recovered_by_client_id"] = row["recovered_by"]
    if state == "unknown":
        result.update(state="unknown", code="execution_unknown" if row["payload"] else "admission_unknown",
                      message="Execution is unconfirmed. Review the saved input and conversation; this request will not run again automatically.")
    elif receipt is None and terminal is None and state not in {"withdrawn", "rejected"}:
        result.update(accepted=False, state="unknown", code="admission_unknown",
                      message="Admission is not yet confirmed. Confirm this same submission identity before sending new work.")
    elif state == "withdrawn":
        result["message"] = "This input was withdrawn before execution. You can restore it and send it again."
    if include_input and row["payload"] is not None:
        payload = row["payload"]
        result["input"] = {"content": payload["content"], "attachment_ids": payload["attachment_ids"],
                           "attachments": payload.get("attachments", []), "wiki_mode": payload["wiki_mode"]}
    return result


async def submission_status(manager: SessionManager, session_id: str, client_id: str) -> dict[str, Any] | None:
    row = await manager.db.get_client_submission(session_id, client_id)
    return await status_from_row(manager, session_id, row) if row is not None else None


async def submit_client_message(manager: SessionManager, session_id: str, content: str, attachment_ids: list[str],
                                client_id: str, *, wiki_mode: str | None,
                                recovery_of_client_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content, "attachment_ids": attachment_ids, "wiki_mode": wiki_mode}
    if recovery_of_client_id:
        payload["recovery_of_client_id"] = recovery_of_client_id
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    base = {"session_id": session_id, "client_request_id": client_id}
    existing = await manager.db.get_client_submission(session_id, client_id)
    if existing is None:
        try:
            files = await asyncio.to_thread(_files, session_id, attachment_ids)
            payload["attachments"] = files
            created, row = await manager.db.reserve_client_submission(
                session_id, client_id, digest, uuid.uuid4().hex, payload=payload, recovery_of=recovery_of_client_id,
            )
        except (AttachmentUnavailable, ValueError) as error:
            return {**base, "accepted": False, "message": str(error), "code": getattr(error, "code", "recovery_unavailable")}
        except Exception:
            logger.exception("Input saving is unconfirmed session=%s client=%s", session_id, client_id)
            return {**base, "accepted": False, "state": "unknown", "code": "admission_unknown",
                    "message": "Input saving is unconfirmed. Keep your draft and confirm this submission before sending new work."}
    else:
        created, row = False, existing
    base["request_id"] = row["request_id"]
    if row["payload_sha256"] != digest:
        return {**base, "accepted": False, "code": "submission_conflict", "message": "This submission identity already belongs to different input."}
    if not created:
        return await status_from_row(manager, session_id, row)
    try:
        try:
            receipt = await manager.start_message(session_id, content, attachment_ids=attachment_ids,
                                                  steerable=True, request_id=row["request_id"], durable_client_input=True,
                                                  **({"wiki_mode": wiki_mode} if wiki_mode else {}))
            response = {**base, "accepted": receipt.accepted, "disposition": receipt.disposition}
            if not receipt.accepted:
                response.update(message="This conversation is closed to new messages. Your message was not accepted.")
        except (AttachmentUnavailable, QuotaExceeded, ValueError) as error:
            response = {**base, "accepted": False, "message": str(error)}
            if getattr(error, "code", None):
                response["code"] = error.code
        await manager.db.settle_client_submission(session_id, client_id, row["request_id"], response)
        result = await submission_status(manager, session_id, client_id)
        assert result is not None
        return result
    except Exception:
        logger.exception("Client submission admission/receipt is unconfirmed session=%s request=%s", session_id, row["request_id"])
        status = await submission_status(manager, session_id, client_id)
        assert status is not None
        return status


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
        owner = row["execution_owner"] or row["request_id"]
        if (row["reason_code"] != "steer_delivery_unknown"
                and await manager.db.get_committed_terminal(session_id, owner) is not None):
            continue
        if not process_group_exited(row["process_group"], row["execution_boot"]):
            raise ValueError("Previous execution has an unknown result and its process exit is unconfirmed. Review the saved request; new execution is blocked until the old process group exits or the host restarts.")

"""Reserve one browser input before admitting it into in-memory dispatch."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ..attachments import AttachmentUnavailable
from .turns import QuotaExceeded

if TYPE_CHECKING:
    from . import SessionManager

logger = logging.getLogger(__name__)


async def submission_status(manager: SessionManager, session_id: str, client_id: str) -> dict[str, Any] | None:
    row = await manager.db.get_client_submission(session_id, client_id)
    if row is None:
        return None
    if row["receipt"] is not None:
        return row["receipt"]
    terminal = await manager.db.get_committed_terminal(session_id, row["request_id"])
    if terminal is not None:
        return {"session_id": session_id, "client_request_id": client_id, "request_id": row["request_id"],
                "accepted": True, "disposition": "duplicate", "outcome": terminal}
    return {"session_id": session_id, "client_request_id": client_id, "request_id": row["request_id"],
            "accepted": False, "state": "unknown", "code": "admission_unknown",
            "message": "Admission is unconfirmed. This identity will not start a second execution; inspect the original request before submitting new work."}


async def submit_client_message(manager: SessionManager, session_id: str, content: str, attachment_ids: list[str], client_id: str, *, wiki_mode: str | None) -> dict[str, Any]:
    payload = {"content": content, "attachment_ids": attachment_ids, "wiki_mode": wiki_mode}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    created, row = await manager.db.reserve_client_submission(session_id, client_id, digest, uuid.uuid4().hex)
    base = {"session_id": session_id, "client_request_id": client_id, "request_id": row["request_id"]}
    if row["payload_sha256"] != digest:
        return {**base, "accepted": False, "code": "submission_conflict", "message": "This submission identity already belongs to different input."}
    if not created:
        status = await submission_status(manager, session_id, client_id)
        assert status is not None
        return status
    try:
        try:
            receipt = await manager.start_message(session_id, content, attachment_ids=attachment_ids,
                                                  steerable=True, request_id=row["request_id"],
                                                  **({"wiki_mode": wiki_mode} if wiki_mode else {}))
            response = {**base, "accepted": receipt.accepted, "disposition": receipt.disposition}
            if not receipt.accepted:
                response.update(message="This conversation is closed to new messages. Your message was not accepted.")
        except (AttachmentUnavailable, QuotaExceeded, ValueError) as error:
            response = {**base, "accepted": False, "message": str(error)}
            if getattr(error, "code", None):
                response["code"] = error.code
        await manager.db.settle_client_submission(session_id, client_id, row["request_id"], response)
        return response
    except Exception:
        logger.exception("Client submission admission/receipt is unconfirmed session=%s request=%s", session_id, row["request_id"])
        status = await submission_status(manager, session_id, client_id)
        assert status is not None
        return status

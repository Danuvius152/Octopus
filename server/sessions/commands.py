"""Correlate controls with a specific execution target and receipt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..question_resolution import UNKNOWN_SOURCE, AnswerSource, QuestionAnswerInvalid
from .owner_questions import AnswerQuestion

if TYPE_CHECKING:
    from . import SessionManager


@dataclass(frozen=True)
class CorrelatedControl:
    data: dict[str, Any]
    user_id: str | None
    source: AnswerSource = AnswerSource()


async def process_command(manager: SessionManager, data: dict[str, Any], user_id: str | None, *,
                          source: AnswerSource = UNKNOWN_SOURCE) -> dict[str, Any]:
    sid = data.get("session_id")
    session = manager.get_session(sid) if isinstance(sid, str) else None
    if session is None or session.user_id != user_id:
        return {"type": "command_receipt", "client_command_id": data.get("client_command_id"),
                "session_id": sid, "operation": data.get("type"), "accepted": False,
                "code": "not_found", "message": "Session not found"}
    return await manager._session_owner(session).call_runtime(CorrelatedControl(dict(data), user_id, source))


async def _process_owned_command(manager: SessionManager, session: Any, data: dict[str, Any], user_id: str | None,
                                 source: AnswerSource = UNKNOWN_SOURCE) -> dict[str, Any]:
    command_id, sid, operation = data.get("client_command_id"), data.get("session_id"), data.get("type")
    response = {"type": "command_receipt", "client_command_id": command_id, "session_id": sid, "operation": operation, "accepted": False}
    if not isinstance(command_id, str) or not command_id or len(command_id) > 128 or not isinstance(sid, str):
        return {**response, "code": "invalid_command", "message": "Invalid command identity"}
    session = manager.get_session(sid)
    if session is None or session.user_id != user_id:
        return {**response, "code": "not_found", "message": "Session not found"}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = session._command_receipts.get(command_id)
    if previous:
        return previous[1] if previous[0] == digest else {**response, "code": "command_conflict", "message": "Command identity was reused for a different operation"}
    stale = {**response, "code": "stale_command", "message": "The execution target changed. Refresh this conversation before trying again."}
    current = session._run.request
    request_id = current.request_id if current else None
    attempt_id = session._run.question_attempt_id
    if operation not in ("interrupt", "answer_question"):
        result = {**response, "code": "invalid_command", "message": "Unsupported command"}
    elif manager.get_session(sid) is not session or "request_id" not in data or "attempt_id" not in data:
        result = stale
    elif operation == "interrupt":
        snapshot = manager.dispatch_snapshot(sid)
        if (data["request_id"] != request_id or data["attempt_id"] != attempt_id
                or data.get("generation") != snapshot["generation"]
                or (request_id is None and data.get("revision") != snapshot["revision"])):
            result = stale
        else:
            await manager.interrupt(sid)
            result = {**response, "accepted": True}
    else:
        pending = session._questions.get(data.get("question_id")) if isinstance(data.get("question_id"), str) else None
        if pending is not None and pending.state != "pending":
            pending = None
        answers = data.get("answers")
        if pending is None or data["request_id"] != pending.request_id or data["attempt_id"] != pending.attempt_id:
            result = stale
        else:
            try:
                ok = await manager._session_owners[id(session)].questions.handle(AnswerQuestion(pending.question_id, answers, source))
            except QuestionAnswerInvalid as error:
                result = {**response, "code": "invalid_command", "message": str(error)}
            else:
                result = {**response, "accepted": True} if ok else stale
    session._command_receipts[command_id] = (digest, result)
    while len(session._command_receipts) > 256:
        session._command_receipts.pop(next(iter(session._command_receipts)))
    return result

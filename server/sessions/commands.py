"""Correlate controls with a specific execution target and receipt."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import SessionManager


async def process_command(manager: SessionManager, data: dict[str, Any], user_id: str | None) -> dict[str, Any]:
    command_id, sid, operation = data.get("client_command_id"), data.get("session_id"), data.get("type")
    response = {"type": "command_receipt", "client_command_id": command_id, "session_id": sid, "operation": operation, "accepted": False}
    if not isinstance(command_id, str) or not command_id or len(command_id) > 128 or not isinstance(sid, str):
        return {**response, "code": "invalid_command", "message": "Invalid command identity"}
    session = manager.get_session(sid)
    if session is None or session.user_id != user_id:
        return {**response, "code": "not_found", "message": "Session not found"}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    async with session._command_lock:
        previous = session._command_receipts.get(command_id)
        if previous:
            return previous[1] if previous[0] == digest else {**response, "code": "command_conflict", "message": "Command identity was reused for a different operation"}
        stale = {**response, "code": "stale_command", "message": "The execution target changed. Refresh this conversation before trying again."}
        current = session._current_request
        request_id = current.request_id if current else None
        attempt_id = session._question_attempt_id
        if manager.get_session(sid) is not session or "request_id" not in data or "attempt_id" not in data:
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
        elif operation == "answer_question":
            pending = session._pending_questions.get(data.get("question_id")) if isinstance(data.get("question_id"), str) else None
            answers = data.get("answers")
            if (pending is None or data["request_id"] != pending.request_id or data["attempt_id"] != pending.attempt_id
                    or not isinstance(answers, list) or len(answers) != len(pending.questions)
                    or any(not isinstance(answer, dict) for answer in answers)):
                result = stale
            else:
                valid = all(isinstance(answer.get("selected", []), list)
                            and all(isinstance(label, str) for label in answer.get("selected", []))
                            and (answer.get("text") is None or isinstance(answer["text"], str))
                            and (bool(answer.get("selected")) or bool((answer.get("text") or "").strip())) for answer in answers)
                if not valid:
                    result = {**response, "code": "invalid_command", "message": "Provide a complete selection or text answer for every question"}
                else:
                    ok = await manager.answer_question(sid, pending.question_id, answers)
                    result = {**response, "accepted": True} if ok else stale
        elif operation in {"approve_tool", "deny_tool"}:
            # Retain receipts and replay/conflict semantics for old clients.
            result = stale
        else:
            result = {**response, "code": "invalid_command", "message": "Unsupported command"}
        session._command_receipts[command_id] = (digest, result)
        while len(session._command_receipts) > 256:
            session._command_receipts.pop(next(iter(session._command_receipts)))
        return result

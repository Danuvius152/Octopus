"""Controlled model completions for consumer tests that replace start_message.

Requests and their final-attempt text are real SQLite records. The test owns
each pending receipt; final broadcasts alone never finish consumer business.
Do not use this stand-in with a live SessionOwner or Harness execution.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from server.knowledge import encode
from server.models import SessionStatus, TurnOutcome
from server.sessions.runtime_types import _Settlement
from server.sessions.submission import RequestCompletion, SubmissionReceipt


@dataclass
class ModelRequest:
    session: object
    receipt: SubmissionReceipt
    attempt_id: str
    work_id: str | None


class CompletionModels:
    """A fake model's admitted requests, not a second consumer registry."""

    def __init__(self):
        self.requests: dict[tuple[int, str], list[ModelRequest]] = {}

    def start_for(self, manager):
        return partial(self.start_message, manager)

    async def start_message(self, manager, session_id, prompt, attachment_ids=None, *,
                            source="human", work_id=None, **kwargs):
        session = manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        assert not manager.session_is_executing(session), "Use the real receipt for an actual execution"
        request_id, attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        receipt = SubmissionReceipt("accepted", request_id, future)
        knowledge = kwargs.get("knowledge_context")
        _, seq, _ = await manager.db.message_sequence_state(session_id)
        await manager.db.record_request(
            request_id=request_id, session_id=session_id, seq=seq, role="user", type="text", content=prompt,
            tool_input={"request_context": {"request_id": request_id, "work_id": work_id, "source": source,
                                           "knowledge": json.loads(encode(knowledge)) if knowledge is not None else {}}},
        )
        requests = self.requests.setdefault((id(manager), session_id), [])
        requests.append(ModelRequest(session, receipt, attempt_id, work_id))
        first = next(item for item in requests if not item.receipt._completion.done())
        session._run.settlement = _Settlement(request_id=first.receipt.request_id, work_id=first.work_id)
        return receipt

    def pending(self, manager, session_id):
        return next((request for request in self.requests.get((id(manager), session_id), [])
                     if not request.receipt._completion.done()), None)

    async def emit(self, consumer, event, *, wait=True):
        """Persist model facts, publish progress, then complete its original receipt."""
        manager = getattr(consumer, "session_mgr", None) or consumer
        request = self.pending(manager, event.get("session_id"))
        matching = request is not None and event.get("work_id", request.work_id) == request.work_id
        if matching and event.get("type") == "status":
            request.session.status = SessionStatus(event["status"])
        if matching and event.get("type") == "assistant_text":
            _, seq, _ = await manager.db.message_sequence_state(event["session_id"])
            await manager.db.append_message(
                session_id=event["session_id"], seq=seq, role="assistant", type="text", content=event["content"],
                tool_input={"attempt_context": {"request_id": request.receipt.request_id,
                                                "attempt_id": request.attempt_id}},
            )
        # Questions, activity and recovery still use the actual broadcast path.
        if consumer is manager:
            await manager._broadcast(event)
        else:
            await consumer._on_broadcast(event)
        if not matching or event.get("retrying") or event.get("type") not in {"result", "error"}:
            return
        failed = event["type"] == "error" or event.get("is_error", False)
        outcome = TurnOutcome(request_id=request.receipt.request_id, status="failed" if failed else "success",
                              message=event.get("message"))
        _, seq, _ = await manager.db.message_sequence_state(event["session_id"])
        await manager.db.record_terminal(
            request_id=request.receipt.request_id, session_id=event["session_id"], seq=seq,
            role="assistant", type="result", content=outcome.model_dump(mode="json"), is_error=failed,
            created_at=datetime.now(UTC).isoformat(),
            tool_input={"attempt_context": {"request_id": request.receipt.request_id,
                                            "attempt_id": request.attempt_id}},
        )
        completion = RequestCompletion(input_request_id=request.receipt.request_id,
                                       request_id=request.receipt.request_id, state="completed", outcome=outcome,
                                       recording_state="committed", notification_state="delivered")
        # This test owns fake admission. Register only at completion so teardown
        # cannot mistake an unexecuted fake request for a real live execution.
        request.session._request_completions[request.receipt.request_id] = request.receipt._completion
        application_updated = asyncio.Event()
        observer_key = f"test-model-completion:{request.receipt.request_id}"

        async def updated(notice):
            application = notice.get("application") or {}
            if notice.get("type") == "application_updated" and application.get("session_id") == event["session_id"]:
                application_updated.set()

        applications = await manager.db.get_applications_for_session(event["session_id"]) if consumer is manager else []
        manager.on_broadcast(observer_key, updated)
        manager._finish_receipt(request.session, completion)
        following = self.pending(manager, event["session_id"])
        request.session._run.settlement = (
            _Settlement(request_id=following.receipt.request_id, work_id=following.work_id) if following else None
        )
        request.session.status = SessionStatus.running if following else SessionStatus.idle
        try:
            if wait:
                if applications:
                    await asyncio.wait_for(application_updated.wait(), 2)
                if consumer is not manager:
                    await consumer.confirm_recording()
        finally:
            manager.remove_broadcast(observer_key)

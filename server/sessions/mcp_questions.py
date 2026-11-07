"""One original MCP ask call's creation receipt and local retirement evidence."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from ..mcp_identity import McpScope
from ..question_resolution import AnswerSource

if TYPE_CHECKING:
    from .base import PendingQuestion
    from .owner import SessionOwner
    from .owner_questions import QuestionResult
    from .runtime_types import _Attempt


class InputCleanup(TypedDict):
    """Bounded facts consumed on the original attempt, later serialized as diagnostics."""

    request_id: str
    attempt_id: str
    thread_id: str
    turn_id: str
    item_id: str
    type: str
    server: str
    tool: str
    session_id: str
    user_id: str | None
    run_lease_id: str | None
    work_id: str | None
    question_id: str
    disposition: str
    close_reason: str
    creation_observed: bool
    question_resolved: bool
    waiters_released: bool
    runner_retired: bool
    provider_consumption: str


@dataclass
class McpQuestionCall:
    owner: SessionOwner
    scope: McpScope
    identity: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    creation: asyncio.Future[QuestionResult] | None = None
    pending: PendingQuestion | None = None
    task: asyncio.Task[str] | None = None
    started: bool = False
    cancelled: bool = False
    error: BaseException | None = None
    attempt: _Attempt | None = None
    request_id: str | None = None
    root: tuple[str, str] | None = None
    cleanup_complete: bool = False
    close_reason: str | None = None
    consumed: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        if self.cancelled:
            return  # The original runner already owns cancellation or cleanup.
        self.cancelled = True
        if self.started and self.task is not None and not self.task.done() and not self.task.cancelling():
            self.task.cancel()

    async def finish(self, reason: str) -> None:
        """Run outside Owner: observe its original envelope, close, then join waiters."""
        from .owner_questions import CloseQuestion

        try:
            # An HTTP response/error is not the original Owner command receipt.
            # Retain the call until that envelope settles. Late, unadmitted
            # HTTP creates cannot enter after this record retires.
            if self.creation is not None:
                try:
                    await asyncio.shield(self.creation)
                except Exception:
                    if self.pending is not None:
                        raise
            pending = self.pending
            if pending is not None:
                if not pending.ready.is_set():
                    await self.owner.call_question(CloseQuestion(pending.question_id, reason,
                        AnswerSource("agent", user_id=self.scope.user_id, session_id=self.scope.session_id,
                                     work_id=self.scope.work_id, run_lease_id=self.scope.run_lease_id)))
                if not pending.ready.is_set() or pending.resolution is None:
                    raise RuntimeError(f"MCP question {pending.question_id} resolution was not confirmed")
                await pending.waiters_released.wait()
        except BaseException as error:
            self.error = error
            self.owner.wake()
            raise
        else:
            self.error = None
            self.cleanup_complete = True
            self.close_reason = reason
            self.owner.wake()

    def cleanup_receipt(self) -> InputCleanup | None:
        """Keep only exact, authenticated pure-input facts, never tool metadata bodies."""
        pending, attempt = self.pending, self.attempt
        metadata = self.identity.get("metadata") or {}
        if not isinstance(metadata, dict):
            return None
        turn = metadata.get("x-codex-turn-metadata") or {}
        if not isinstance(turn, dict):
            return None
        call_id = metadata.get("callId")
        if (not self.cleanup_complete or self.error is not None or self.close_reason != "cancelled"
                or self.task is None or not self.task.done() or not self.task.cancelled()
                or pending is None or not pending.ready.is_set() or pending.resolution is None
                or pending.waiters or not pending.waiters_released.is_set()
                or self.creation is None or not self.creation.done() or self.creation.cancelled()
                or attempt is None or self.root is None or self.request_id is None or not isinstance(call_id, str) or not call_id
                or (pending.request_id, pending.attempt_id) != (self.request_id, attempt.attempt_id)
                or (metadata.get("threadId"), turn.get("thread_id"), turn.get("turn_id"))
                    != (self.root[0], self.root[0], self.root[1])
                or (self.identity.get("server"), self.identity.get("tool")) != ("ask", "user")):
            return None
        if self.creation.exception() is not None or self.creation.result() is not pending:
            return None
        return {"request_id": self.request_id, "attempt_id": attempt.attempt_id,
                "thread_id": self.root[0], "turn_id": self.root[1], "item_id": call_id,
                "type": "mcpToolCall", "server": "ask", "tool": "user",
                "session_id": self.scope.session_id, "user_id": self.scope.user_id,
                "run_lease_id": self.scope.run_lease_id, "work_id": self.scope.work_id,
                "question_id": pending.question_id, "disposition": pending.resolution["disposition"],
                "close_reason": self.close_reason, "creation_observed": True, "question_resolved": True,
                "waiters_released": True, "runner_retired": True, "provider_consumption": "unknown"}


def input_gap_receipts(attempt: _Attempt, location: dict[str, Any]) -> list[InputCleanup] | None:
    """Exact local cleanup can explain only this original required-item diagnostic."""
    missing = location.get("missing_items")
    if (location.get("kind") != "native_collection_incomplete" or location.get("origin") != "required_items"
            or not isinstance(missing, list) or not missing or location.get("missing_total") != len(missing)):
        return None
    proofs = []
    for gap in missing:
        matches = [receipt for receipt in attempt.input_cleanup
                   if all(receipt.get(key) == gap.get(key) for key in ("thread_id", "turn_id", "item_id", "type", "server", "tool"))
                   and (receipt["thread_id"], receipt["turn_id"])
                       == (location.get("root_thread_id"), location.get("root_turn_id"))]
        if len(matches) != 1:
            return None
        proofs.append(matches[0])
    return proofs


def resolved_input_gap(attempt: _Attempt) -> bool:
    backend, final = attempt.backend, attempt.final_answer
    if not getattr(getattr(backend, "profile", None), "native_input_evidence", False):
        return False
    root = backend.observed_root
    locations = backend.protocol_locations
    return bool(backend.native_terminal_status == "completed"
                and attempt.result is not None and not attempt.result.is_error and attempt.timeout is None
                and root is not None and final is not None
                and (final.thread_id, final.turn_id) == (root.thread_id, root.turn_id)
                and backend.protocol_issues == {"native_collection_incomplete": 1} and len(locations) == 1
                and input_gap_receipts(attempt, locations[0]))

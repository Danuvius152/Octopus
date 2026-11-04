"""One original MCP ask call's creation receipt and local retirement evidence."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..mcp_identity import McpScope
from ..question_resolution import AnswerSource

if TYPE_CHECKING:
    from .base import PendingQuestion
    from .owner import SessionOwner
    from .owner_questions import QuestionResult


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
            self.owner.questions.mcp_calls.pop(self.id, None)
            self.owner.wake()

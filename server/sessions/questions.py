"""Public question commands and read-only views of their original records."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import Any

from ..mcp_identity import McpScope
from ..question_resolution import SYSTEM_SOURCE, UNKNOWN_SOURCE, AnswerSource
from .base import PendingQuestion, Session, SessionManagerBase
from .owner import SessionOwner
from .owner_questions import (
    AnswerQuestion,
    CaptureQuestion,
    CloseQuestion,
    CreateQuestion,
    QuestionClosedError,
    RegisterNativeQuestion,
)

__all__ = ["QuestionClosedError", "QuestionsMixin"]


class QuestionsMixin(SessionManagerBase):
    def _question_owner(self, session: Session, *, internal: bool = False) -> SessionOwner:
        # Original attempt cleanup is allowed after archive seals new external
        # admission. It must use the original owner, never resurrect a failure.
        if internal and (owner := self._session_owners.get(id(session))) is not None:
            if owner.session is not session:
                raise RuntimeError("Question cleanup lost its original Session owner")
            owner.require_available()
            return owner
        return self._session_owner(session)

    async def create_pending_question(
        self, session_id: str, questions: list[dict[str, Any]], *,
        attempt_id: str | None = None, scope: McpScope | None = None,
    ) -> str | None:
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if self._admission_closed or session._admission_closed:
            raise ValueError("Session admission is closed")
        pending = await self._question_owner(session).call_question(CreateQuestion(
            deepcopy(questions), attempt_id=attempt_id, scope=scope,
        ))
        assert pending is None or isinstance(pending, PendingQuestion)
        return pending.question_id if pending is not None else None

    async def _record_native_question(
        self, session: Session, question_id: str, questions: list[dict[str, Any]], *, attempt_id: str,
    ) -> PendingQuestion | None:
        owner = self._question_owner(session)
        command = RegisterNativeQuestion(question_id, attempt_id, deepcopy(questions))
        pending = (await owner.questions.handle(command) if asyncio.current_task() is owner.task
                   else await owner.call_question(command))
        assert pending is None or isinstance(pending, PendingQuestion)
        return pending

    async def wait_for_question_answer(
        self, session_id: str, question_id: str, timeout: float = 60.0,
    ) -> str | None:
        """Capture/expire in the owner; wait for the original Event outside it."""
        session = self.sessions.get(session_id)
        pending = session._questions.get(question_id) if session is not None else None
        if session is None or pending is None or pending.state == "closed":
            raise QuestionClosedError("Question not found or closed")
        # A confirmed answer remains a readable fact after emergency shutdown.
        if pending.state == "answered" and pending.ready.is_set():
            if pending.answer is None:
                raise QuestionClosedError("Question answer recording is unavailable")
            return pending.answer
        captured = await self._question_owner(session, internal=True).call_question(CaptureQuestion(question_id))
        if not isinstance(captured, PendingQuestion):
            raise QuestionClosedError("Question not found or closed")
        try:
            await asyncio.wait_for(captured.ready.wait(), timeout=timeout)
        except TimeoutError:
            return None
        if captured.state != "answered" or captured.answer is None:
            raise QuestionClosedError("Question not found or closed")
        return captured.answer

    def get_pending_questions(self, session_id: str) -> tuple[PendingQuestion, ...]:
        session = self.sessions.get(session_id)
        if session is None:
            return ()
        return tuple(pending for pending in session._questions.values()
                     if self.has_pending_question(session_id, pending.question_id))

    def has_pending_question(self, session_id: str, question_id: str) -> bool:
        session = self.sessions.get(session_id)
        pending = session._questions.get(question_id) if session is not None else None
        if pending is None or pending.state != "pending" or session is None:
            return False
        return pending.is_current(session._run.question_attempt_id) and (
            pending.expires_at is None or time.monotonic() < pending.expires_at
        )

    async def answer_question(self, session_id: str, question_id: str, answers: list[dict[str, Any]], *,
                              source: AnswerSource = UNKNOWN_SOURCE) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        result = await self._question_owner(session).call_question(AnswerQuestion(question_id, deepcopy(answers), source))
        assert isinstance(result, bool)
        return result

    async def close_pending_question(self, session_id: str, question_id: str, *, reason: str = "cancelled",
                                     source: AnswerSource = SYSTEM_SOURCE) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        result = await self._question_owner(session).call_question(CloseQuestion(question_id, reason, source))
        assert isinstance(result, bool)
        return result

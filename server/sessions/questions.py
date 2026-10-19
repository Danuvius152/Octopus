"""Questions the model asks the human, with legacy approval compatibility.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import settings
from ..mcp_identity import McpScope, require_live_scope
from ..models import MessageContent, MessageRole
from .base import (
    PendingQuestion,
    Session,
    SessionManagerBase,
    logger,
)


class QuestionClosedError(ValueError):
    """A long-poll targets a question that no longer accepts an answer."""


class QuestionsMixin(SessionManagerBase):

    async def create_pending_question(
        self,
        session_id: str,
        questions: list[dict[str, Any]],
        *,
        attempt_id: str | None = None,
        scope: McpScope | None = None,
    ) -> str | None:
        """Called by the ask MCP server (via REST) when the model invokes
        `mcp__ask__user`. Generates a question_id, records the pending
        question, broadcasts the `question_request` WS event so the
        frontend renders the form, and schedules the auto-answer
        timeout. Returns the question_id (which the MCP server then
        passes to the long-poll endpoint).
        """
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if scope is not None:
            require_live_scope(scope, session_id)
            if (
                scope.user_id != session.user_id
                or session._settlement is None
                or scope.work_id != session._settlement.work_id
                or session._question_attempt_id is None
            ):
                raise ValueError("Model run is no longer active")
        if attempt_id is not None and attempt_id != session._question_attempt_id:
            return None
        question_id = uuid.uuid4().hex[:16]
        pending = self._register_pending_question(session, question_id, questions)
        session._pending_question_events[question_id] = asyncio.Event()

        # Persist + broadcast a question matching the transcript form.
        msg = MessageContent(
            role=MessageRole.assistant, type="question_request",
            tool_name="AskUserQuestion", tool_use_id=question_id,
            tool_input={
                "questions": questions, "deadline": pending.deadline,
                "request_id": pending.request_id, "attempt_id": pending.attempt_id,
                "work_id": pending.work_id,
            },
        )
        try:
            msg_seq = await self._persist_message(session, msg)
            event: dict[str, Any] = {
                "type": "question_request", "session_id": session.id,
                "question_id": question_id, "questions": questions,
                "deadline": pending.deadline, "request_id": pending.request_id,
                "attempt_id": pending.attempt_id,
                "work_id": pending.work_id,
            }
            self._tag_persisted(event, msg_seq, msg)
            await self._broadcast(event, user_id=session.user_id)
        except asyncio.CancelledError:
            await self._close_registered_question(session, question_id, reason="tool_cancelled")
            raise
        self._schedule_question_timeout(session, question_id)
        return question_id

    def _register_pending_question(
        self, session: Session, question_id: str, questions: list[dict[str, Any]]
    ) -> PendingQuestion:
        existing = session._pending_questions.get(question_id)
        if existing is not None:
            return existing
        timeout = settings.ask_user_question_timeout_seconds
        pending = PendingQuestion(
            question_id=question_id,
            questions=questions,
            deadline=(datetime.now(UTC) + timedelta(seconds=timeout)).isoformat() if timeout > 0 else None,
            expires_at=time.monotonic() + timeout if timeout > 0 else None,
            request_id=session._settlement.request_id if session._settlement else None,
            attempt_id=session._question_attempt_id,
            work_id=session._settlement.work_id if session._settlement else None,
        )
        session._pending_questions[question_id] = pending
        if session._question_watchdog is not None:
            session._question_watchdog.setdefault("waiting_questions", set()).add(question_id)
        return pending

    async def _begin_question_attempt(self, session: Session, attempt_id: str, state: dict[str, Any]) -> None:
        await self._end_question_attempt(session, session._question_attempt_id)
        session._question_attempt_id = attempt_id
        session._question_watchdog = state
        state["waiting_questions"] = set()
        state["last"] = time.monotonic()

    async def _end_question_attempt(self, session: Session, attempt_id: str | None, *, reason: str = "attempt_ended") -> None:
        # Detach before publishing closure events yields to other tool calls.
        if session._question_attempt_id == attempt_id:
            session._question_attempt_id = None
            session._question_watchdog = None
        ids = [qid for qid, pending in session._pending_questions.items() if pending.attempt_id == attempt_id]
        for qid in ids:
            await self._close_registered_question(session, qid, reason=reason)
        await self._drain_question_publications(session)
        for qid, owner in list(session._question_answer_attempts.items()):
            if owner == attempt_id:
                session._pending_question_answers.pop(qid, None)
                session._pending_question_events.pop(qid, None)
                session._question_answer_attempts.pop(qid, None)

    async def _close_session_questions(self, session: Session, *, reason: str) -> None:
        for qid in list(session._pending_questions):
            await self._close_registered_question(session, qid, reason=reason)
        await self._drain_question_publications(session)
        self._cancel_all_question_timers(session)
        for event in session._pending_question_events.values():
            event.set()
        session._pending_question_events.clear()
        session._pending_question_answers.clear()
        session._question_answer_attempts.clear()

    async def wait_for_question_answer(
        self,
        session_id: str,
        question_id: str,
        timeout: float = 60.0,
    ) -> str | None:
        """Long-poll waiter used by the ask MCP server's HTTP loop.

        Returns the answer text when the user (or auto-answer) submits,
        None on timeout. The MCP server retries while the host keeps the
        question open; an unknown or closed question raises QuestionClosedError.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise QuestionClosedError("Question not found or closed")
        answer = session._pending_question_answers.get(question_id)
        if answer is not None:
            return answer
        if question_id not in session._pending_questions:
            raise QuestionClosedError("Question not found or closed")
        pending = session._pending_questions[question_id]
        if pending.attempt_id is not None and pending.attempt_id != session._question_attempt_id:
            await self._close_registered_question(session, question_id, reason="attempt_ended")
            raise QuestionClosedError("Question not found or closed")
        if await self._resolve_expired_question(session, question_id):
            return session._pending_question_answers.get(question_id)
        ev = session._pending_question_events.get(question_id)
        if ev is None:
            # Already-delivered case: answer might be sitting in the
            # answers dict from a fast delivery; return immediately.
            ans = session._pending_question_answers.get(question_id)
            return ans
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except TimeoutError:
            return None
        answer = session._pending_question_answers.get(question_id)
        if answer is None and question_id not in session._pending_questions:
            raise QuestionClosedError("Question not found or closed")
        return answer

    def has_pending_question(self, session_id: str, question_id: str) -> bool:
        """Whether a queued question delivery still refers to a live form."""
        session = self.sessions.get(session_id)
        pending = session._pending_questions.get(question_id) if session else None
        if pending is None or session is None:
            return False
        if pending.attempt_id is not None and pending.attempt_id != session._question_attempt_id:
            return False
        return pending.expires_at is None or time.monotonic() < pending.expires_at

    async def _resolve_expired_question(self, session: Session, question_id: str) -> bool:
        pending = session._pending_questions.get(question_id)
        if pending is None or pending.expires_at is None or time.monotonic() < pending.expires_at:
            return False
        await self._deliver_question_answer(session, question_id, self.AUTO_ANSWER_TEXT, auto=True)
        return True

    def _claim_question_resolution(self, session: Session, question_id: str) -> PendingQuestion | None:
        pending = session._pending_questions.pop(question_id, None)
        if pending is not None:
            self._cancel_question_timer(session, question_id)
            state = session._question_watchdog
            if state is not None and pending.attempt_id == session._question_attempt_id:
                waiting = state.get("waiting_questions", set())
                had_waiting = bool(waiting)
                waiting.discard(question_id)
                if had_waiting and not waiting:
                    state["last"] = time.monotonic()
        return pending

    async def close_pending_question(
        self, session_id: str, question_id: str, *, reason: str = "cancelled"
    ) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        return await self._close_registered_question(session, question_id, reason=reason)

    async def _close_registered_question(self, session: Session, question_id: str, *, reason: str) -> bool:
        pending = self._claim_question_resolution(session, question_id)
        if pending is None:
            return False
        ev = session._pending_question_events.pop(question_id, None)
        if ev is not None:
            ev.set()
        human = f"Question closed: {reason.replace('_', ' ')}. No answer was received."
        msg = MessageContent(
            role=MessageRole.system, type="question_closed", content=human,
            tool_use_id=question_id, tool_input={"reason": reason},
        )
        event = {
            "type": "question_closed", "session_id": session.id,
            "question_id": question_id, "message": human, "reason": reason,
        }
        await self._publish_question_resolution(session, msg, event)
        return True

    async def _publish_question_resolution(
        self, session: Session, message: MessageContent, event: dict[str, Any]
    ) -> None:
        """A claimed resolution survives its HTTP request disconnecting."""
        async def publish() -> None:
            seq = await self._persist_message(session, message)
            self._tag_persisted(event, seq, message)
            await self._broadcast(event, user_id=session.user_id)

        task = asyncio.create_task(publish(), name=f"question-resolution-{message.tool_use_id}")
        session._question_publications.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            session._question_publications.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                logger.error("Question resolution publication failed", exc_info=error)

        task.add_done_callback(finished)
        await asyncio.shield(task)

    @staticmethod
    async def _drain_question_publications(session: Session) -> None:
        if session._question_publications:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(session._question_publications)))

    async def answer_question(
        self,
        session_id: str,
        question_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Called by the frontend (REST or legacy WS) when the user
        submits answers to the form. Formats them, stores the text,
        wakes any waiting MCP-server long-poll via the asyncio.Event,
        persists the chat history entry, and broadcasts the WS event.
        """
        session = self.sessions.get(session_id)
        if not session:
            return False
        pending = session._pending_questions.get(question_id)
        if pending is None:
            return False
        if pending.attempt_id is not None and pending.attempt_id != session._question_attempt_id:
            await self._close_registered_question(session, question_id, reason="attempt_ended")
            return False
        if await self._resolve_expired_question(session, question_id):
            return False
        if session._pending_questions.get(question_id) is not pending:
            return False
        answer_text = self._format_answers(pending.questions, answers)
        return await self._deliver_question_answer(
            session, question_id, answer_text, auto=False
        )

    async def _deliver_question_answer(
        self,
        session: Session,
        question_id: str,
        answer_text: str,
        *,
        auto: bool,
    ) -> bool:
        """Common path for both human and timeout-driven answers.

        Sets the per-question Event so the ask MCP server's long-poll
        unblocks and returns the answer to the model. Persists the
        user-visible question_answer chat entry, broadcasts the WS
        event with the `auto` flag set when the timeout fired.
        """
        pending = session._pending_questions.get(question_id)
        if pending is not None and pending.attempt_id is not None and pending.attempt_id != session._question_attempt_id:
            await self._close_registered_question(session, question_id, reason="attempt_ended")
            return False
        # Reserve the resolution before the first await. A human answer and
        # the deadline (or two tabs) must not both deliver the same question.
        pending = self._claim_question_resolution(session, question_id)
        if pending is None:
            return False

        # Stash the text + signal the waiter. Even if no MCP long-poll
        # is currently waiting (e.g. the MCP request retried just now),
        # the answer sits in the answers dict for the next poll.
        session._pending_question_answers[question_id] = answer_text
        session._question_answer_attempts[question_id] = pending.attempt_id
        ev = session._pending_question_events.get(question_id)
        if ev is not None:
            ev.set()

        ans_msg = MessageContent(
            role=MessageRole.user,
            type="question_answer",
            tool_use_id=question_id,
            content=answer_text,
        )
        event: dict[str, Any] = {
            "type": "question_answer",
            "session_id": session.id,
            "question_id": question_id,
            "content": answer_text,
        }
        if auto:
            event["auto"] = True
        await self._publish_question_resolution(session, ans_msg, event)

        # Keep the answer text around briefly for any in-flight MCP
        # long-poll that arrives just AFTER set() — it'll fetch from
        # the answers dict directly. The owning attempt's finalizer clears
        # these entries; they cannot answer a new attempt's questions.
        return True

    def _schedule_question_timeout(self, session: Session, question_id: str) -> None:
        timeout: float = settings.ask_user_question_timeout_seconds
        pending = session._pending_questions.get(question_id)
        if pending is None:
            return
        if pending.expires_at is not None:
            timeout = max(0.0, pending.expires_at - time.monotonic())
        elif pending.deadline is None and pending.expires_at is None and timeout <= 0:
            return
        if timeout <= 0:
            if pending.expires_at is None:
                return  # auto-answer disabled
        # Replace any existing timer for this question_id — defensive,
        # we don't expect the same id to be emitted twice.
        self._cancel_question_timer(session, question_id)
        task = asyncio.create_task(
            self._auto_answer_after(session, question_id, timeout),
            name=f"auto-answer-{session.id}-{question_id}",
        )
        session._question_timers[question_id] = task

    def _cancel_question_timer(self, session: Session, question_id: str) -> None:
        task = session._question_timers.pop(question_id, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_question_timers(self, session: Session) -> None:
        for task in list(session._question_timers.values()):
            if not task.done():
                task.cancel()
        session._question_timers.clear()

    async def _auto_answer_after(
        self, session: Session, question_id: str, timeout: float
    ) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return  # the user (or a cleanup path) cancelled us first
        # The user might have answered during the sleep — re-check.
        if question_id not in session._pending_questions:
            return
        # Pop our own timer entry so _deliver_question_answer doesn't
        # try to cancel a task that's currently running (self).
        session._question_timers.pop(question_id, None)
        try:
            await self._deliver_question_answer(
                session, question_id, self.AUTO_ANSWER_TEXT, auto=True
            )
        except Exception:
            logger.exception(
                "Auto-answer for session %s question %s failed",
                session.id,
                question_id,
            )

    @staticmethod
    def _format_answers(
        questions: list[dict[str, Any]], answers: list[dict[str, Any]]
    ) -> str:
        """Render the user's answers as a string Claude can read.

        `answers` is a list aligned with `questions`; each entry has
        either {"selected": [labels]} or {"text": "free-form"}.
        """
        lines: list[str] = []
        for i, q in enumerate(questions):
            question_text = q.get("question", "")
            ans = answers[i] if i < len(answers) else {}
            if ans.get("text"):
                lines.append(f"Q: {question_text}\nA: {ans['text']}")
            else:
                selected = ans.get("selected") or []
                if isinstance(selected, str):
                    selected = [selected]
                lines.append(
                    f"Q: {question_text}\nA: {', '.join(selected) if selected else '(no answer)'}"
                )
        return "\n\n".join(lines)

    async def approve_tool(self, session_id: str, tool_use_id: str) -> bool:
        """Reject legacy approvals; confirmation uses AskUserQuestion."""
        return False

    async def deny_tool(
        self, session_id: str, tool_use_id: str, reason: str = ""
    ) -> bool:
        """Reject legacy approvals; confirmation uses AskUserQuestion."""
        return False

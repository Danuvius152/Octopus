"""Concrete question commands, handled only by their original Session owner."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..config import settings
from ..harness import HarnessRun
from ..harness.events import HarnessEvent
from ..mcp_identity import McpScope, require_live_scope
from ..models import MessageContent, MessageRole
from ..question_resolution import (
    SYSTEM_SOURCE,
    AnswerSource,
    format_answers,
    structured_answers,
    validate_answers,
)
from .base import PendingQuestion
from .publication import Publication
from .runtime_types import SessionMailboxFull

if TYPE_CHECKING:
    from .owner import SessionOwner

_MAX_NATIVE_QUESTIONS = 64


class QuestionClosedError(ValueError):
    """A long-poll targets a question that no longer accepts an answer."""


@dataclass(frozen=True)
class CreateQuestion:
    questions: list[dict[str, Any]]
    attempt_id: str | None = None
    scope: McpScope | None = None


@dataclass(frozen=True)
class RegisterNativeQuestion:
    question_id: str
    attempt_id: str
    questions: list[dict[str, Any]]
    backend: HarnessRun | None = None
    event: HarnessEvent | None = None


@dataclass(frozen=True)
class AnswerQuestion:
    question_id: str
    answers: object  # OwnerQuestions validates the raw answer before using it.
    source: AnswerSource = AnswerSource()


@dataclass(frozen=True)
class CloseQuestion:
    question_id: str
    reason: str
    source: AnswerSource = SYSTEM_SOURCE


@dataclass(frozen=True)
class CaptureQuestion:
    question_id: str


@dataclass(frozen=True)
class BeginQuestionAttempt:
    attempt_id: str
    watchdog: dict[str, Any]


@dataclass(frozen=True)
class EndQuestionAttempt:
    attempt_id: str | None
    reason: str
    captured: tuple[PendingQuestion, ...]


@dataclass(frozen=True)
class CloseSessionQuestions:
    reason: str
    captured: tuple[PendingQuestion, ...]


QuestionCommand = (CreateQuestion | RegisterNativeQuestion | AnswerQuestion | CloseQuestion | CaptureQuestion
                   | BeginQuestionAttempt | EndQuestionAttempt | CloseSessionQuestions)
QuestionResult = bool | PendingQuestion | None


def question_command_bytes(command: QuestionCommand) -> int:
    """Account captured input, rather than stringify referenced live records."""
    if isinstance(command, (CreateQuestion, RegisterNativeQuestion)):
        payload: object = command.questions
    elif isinstance(command, AnswerQuestion):
        payload = (command.question_id, command.answers, command.source.details())
    elif isinstance(command, CloseQuestion):
        payload = (command.question_id, command.reason, command.source.details())
    elif isinstance(command, CaptureQuestion):
        payload = command.question_id
    elif isinstance(command, BeginQuestionAttempt):
        payload = command.attempt_id
    else:
        # End/close transport references to the original records, whose
        # bodies are already owned by the Session, not another form copy.
        return len(command.captured) * 8 + len(command.reason.encode("utf-8"))
    return len(json.dumps(payload, ensure_ascii=True).encode("ascii"))


@dataclass
class QuestionNotification:
    """One of the question's two original, ordered notification slots."""

    event: dict[str, Any]
    order: int
    backend: HarnessRun | None
    publication: Publication | None = None

    @property
    def completed(self) -> bool:
        return self.publication is not None and (
            self.publication.delivered or self.publication.error is not None
        )


@dataclass(frozen=True)
class QuestionPublished:
    record: PendingQuestion
    notification: QuestionNotification

    @property
    def backend(self) -> HarnessRun | None:
        return self.notification.backend


class OwnerQuestions:
    """State transitions invoked by SessionOwner, never by timer/subscriber I/O."""

    def __init__(self, owner: SessionOwner) -> None:
        self.owner = owner
        self._notice_order = 0

    async def handle(self, command: QuestionCommand) -> QuestionResult:
        if isinstance(command, (CreateQuestion, RegisterNativeQuestion)):
            return await self._create(command)
        if isinstance(command, AnswerQuestion):
            pending = self.owner.session._questions.get(command.question_id)
            if pending is None or pending.state != "pending":
                return False
            answers = validate_answers(pending.questions, command.answers)
            if not pending.is_current(self.owner.session._run.question_attempt_id):
                await self._close(pending, "attempt_ended")
                return False
            if await self._expire(pending):
                return False
            return await self._answer(pending, format_answers(pending.questions, answers), auto=False,
                source=command.source, answers=structured_answers(pending.questions, answers))
        if isinstance(command, CloseQuestion):
            pending = self.owner.session._questions.get(command.question_id)
            return await self._close(pending, command.reason, source=command.source) if pending is not None else False
        if isinstance(command, CaptureQuestion):
            pending = self.owner.session._questions.get(command.question_id)
            if pending is not None and pending.state == "pending":
                if not pending.is_current(self.owner.session._run.question_attempt_id):
                    await self._close(pending, "attempt_ended")
                else:
                    await self._expire(pending)
            return pending if pending is not None and pending.state != "closed" else None
        if isinstance(command, BeginQuestionAttempt):
            session = self.owner.session
            captured = tuple(record for record in session._questions.values()
                             if record.attempt_id == session._run.question_attempt_id)
            await self._end(captured, session._run.question_attempt_id, "attempt_ended")
            session._run.question_attempt_id = command.attempt_id
            session._run.question_watchdog = command.watchdog
            command.watchdog["waiting_questions"] = set()
            command.watchdog["last"] = time.monotonic()
            return None
        if isinstance(command, EndQuestionAttempt):
            await self._end(command.captured, command.attempt_id, command.reason)
            return None
        if isinstance(command, CloseSessionQuestions):
            for pending in command.captured:
                if self._owned(pending):
                    await self._close(pending, command.reason)
                    pending.retirement_requested = True
                    self._retire_if_finished(pending)
            return None
        raise AssertionError(f"Unrecognized question command: {type(command).__name__}")

    def _owned(self, pending: PendingQuestion) -> bool:
        return self.owner.session._questions.get(pending.question_id) is pending

    async def _create(self, command: CreateQuestion | RegisterNativeQuestion) -> PendingQuestion | None:

        session, manager = self.owner.session, self.owner.manager
        if manager.sessions.get(session.id) is not session:
            return None
        if manager._admission_closed or session._admission_closed:
            raise ValueError("Session admission is closed")
        if isinstance(command, CreateQuestion) and command.scope is not None:
            require_live_scope(command.scope, session.id)
            if (command.scope.user_id != session.user_id or session._run.settlement is None
                    or command.scope.work_id != session._run.settlement.work_id
                    or session._run.question_attempt_id is None):
                raise ValueError("Model run is no longer active")
        native_ref = command.event.native_question if isinstance(command, RegisterNativeQuestion) and command.event else None
        if command.attempt_id is not None and command.attempt_id != session._run.question_attempt_id and native_ref is None:
            return None
        if native_ref is not None:
            retained = [record for record in session._questions.values() if record.native_ref is not None]
            payload = sum(len(json.dumps([record.questions, record.answer], ensure_ascii=True).encode()) for record in retained)
            incoming = len(json.dumps(command.questions, ensure_ascii=True).encode())
            if len(retained) >= _MAX_NATIVE_QUESTIONS or payload + incoming > 1024 * 1024:
                assert isinstance(command, RegisterNativeQuestion) and command.backend is not None
                command.backend.fail_stream("native_question_overflow", "Retained native questions exceeded capacity")
                return None
        question_id = command.question_id if isinstance(command, RegisterNativeQuestion) else uuid.uuid4().hex[:16]
        if (existing := session._questions.get(question_id)) is not None:
            return existing
        timeout = settings.ask_user_question_timeout_seconds
        pending = PendingQuestion(
            question_id=question_id, questions=command.questions,
            deadline=(datetime.now(UTC) + timedelta(seconds=timeout)).isoformat() if timeout > 0 else None,
            expires_at=time.monotonic() + timeout if timeout > 0 else None,
            request_id=native_ref.request_id if native_ref else session._run.settlement.request_id if session._run.settlement else None,
            attempt_id=native_ref.attempt_id if native_ref else session._run.question_attempt_id,
            work_id=session._run.settlement.work_id if session._run.settlement else None,
            backend=command.backend if isinstance(command, RegisterNativeQuestion) and command.backend is not None else
                session._run.backend if isinstance(session._run.backend, HarnessRun) else None,
            native_ref=command.event.native_question if isinstance(command, RegisterNativeQuestion) and command.event else None,
        )
        details: dict[str, Any] = {
            "questions": pending.questions, "deadline": pending.deadline,
            "request_id": pending.request_id, "attempt_id": pending.attempt_id, "work_id": pending.work_id,
        }
        if pending.native_ref is not None:
            from dataclasses import asdict
            details["native_question"] = asdict(pending.native_ref)
        event = {"type": "question_request", "session_id": session.id, "question_id": question_id, **details}
        if (isinstance(command, CreateQuestion)
                and (self.has_waiting_notifications or not self.owner.publisher.can_offer(event))):
            raise SessionMailboxFull("Session notification capacity is full; retry this question later")
        session._questions[question_id] = pending
        if session._run.question_watchdog is not None and (native_ref is None or native_ref.blocking):
            session._run.question_watchdog.setdefault("waiting_questions", set()).add(question_id)
        if isinstance(command, CreateQuestion) or pending.native_ref is not None:
            message = MessageContent(role=MessageRole.assistant, type="question_request",
                                     tool_name="AskUserQuestion", tool_use_id=question_id, tool_input=details)
            await self._persist(pending, message, event, request=True)
        if pending.expires_at is not None:
            pending.timer = asyncio.create_task(
                asyncio.sleep(max(0.0, pending.expires_at - time.monotonic())),
                name=f"question-deadline-{session.id}-{question_id}",
            )
            # The task owns only time. Its original record remains the fact;
            # the callback neither mutates it nor starts an enqueue task.
            pending.timer.add_done_callback(lambda _done: self.owner.wake())
        if pending.backend is not None and pending.backend.captures_retirement_output and not pending.is_current(session._run.question_attempt_id):
            await self._close(pending, "attempt_ended")
        return pending

    async def _persist(
        self, pending: PendingQuestion, message: MessageContent, event: dict[str, Any], *, request: bool = False,
    ) -> None:
        manager, session = self.owner.manager, self.owner.session
        try:
            seq = await manager._persist_message(session, message)
        except ValueError as error:
            # A recording failure after claim is not a rejected answer. Let
            # the established Owner failure/cleanup boundary contain it.
            raise RuntimeError("Question recording failed") from error
        manager._tag_persisted(event, seq, message)
        self._notice_order += 1
        notice = QuestionNotification(event, self._notice_order, pending.backend or session._run.backend)
        if request:
            pending.request_publication = notice
        else:
            pending.publication = notice
        self.flush_notifications()

    async def _end(self, captured: tuple[PendingQuestion, ...], attempt_id: str | None, reason: str) -> None:
        session = self.owner.session
        if session._run.question_attempt_id == attempt_id:
            session._run.question_attempt_id = None
            session._run.question_watchdog = None
        for pending in captured:
            if self._owned(pending):
                self._retain_prefix(pending)
                await self._close(pending, reason)
                self._retain_prefix(pending)
                pending.retirement_requested = True
                self._retire_if_finished(pending)

    def _claim(self, pending: PendingQuestion, state: str) -> bool:
        if not self._owned(pending) or pending.state != "pending":
            return False
        assert state in {"answered", "closed"}
        pending.state = "answered" if state == "answered" else "closed"
        timer, pending.timer = pending.timer, None
        if timer is not None and not timer.done():
            timer.cancel()
        session = self.owner.session
        watchdog = session._run.question_watchdog
        if watchdog is not None and pending.attempt_id == session._run.question_attempt_id:
            waiting = watchdog.get("waiting_questions", set())
            had_waiting = bool(waiting)
            waiting.discard(pending.question_id)
            if had_waiting and not waiting:
                watchdog["last"] = time.monotonic()
        return True

    def _resolution(self, pending: PendingQuestion, disposition: str, source: AnswerSource,
                    *, answers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"version": 1, "question_id": pending.question_id, "request_id": pending.request_id,
                "attempt_id": pending.attempt_id, "work_id": pending.work_id,
                "disposition": disposition, "source": source.details(), "answers": answers}

    async def _answer(self, pending: PendingQuestion, answer: str, *, auto: bool,
                      source: AnswerSource = SYSTEM_SOURCE, answers: list[dict[str, Any]] | None = None) -> bool:
        if pending.native_ref is not None:
            assert pending.backend is not None
            retained = sum(len(json.dumps([record.questions, record.answer], ensure_ascii=True).encode())
                           for record in self.owner.session._questions.values() if record.native_ref is not None)
            if retained + len(json.dumps(answer, ensure_ascii=True).encode()) > 1024 * 1024:
                pending.backend.fail_stream("native_question_overflow", "Retained native answers exceeded capacity")
                await self._close(pending, "response_overflow")
                return False
        if not self._claim(pending, "answered"):
            return False
        message = MessageContent(role=MessageRole.user, type="question_answer",
            tool_use_id=pending.question_id, content=answer,
            tool_input={"question_resolution": self._resolution(pending, "expired" if auto else "answered", source, answers=answers)})
        event: dict[str, Any] = {
            "type": "question_answer", "session_id": self.owner.session.id,
            "question_id": pending.question_id, "content": answer, "tool_input": message.tool_input,
        }
        if auto:
            event["auto"] = True
        await self._persist(pending, message, event)
        if pending.native_ref is not None:
            assert pending.backend is not None
            pending.backend.reply_question(pending.native_ref, answers)
        pending.answer = answer
        pending.ready.set()
        return True

    async def _close(self, pending: PendingQuestion, reason: str, *, source: AnswerSource = SYSTEM_SOURCE) -> bool:
        if not self._claim(pending, "closed"):
            return False
        human = f"Question closed: {reason.replace('_', ' ')}. No answer was received."
        message = MessageContent(role=MessageRole.system, type="question_closed", content=human,
            tool_use_id=pending.question_id, tool_input={"reason": reason,
                "question_resolution": self._resolution(pending, "closed", source)})
        event = {
            "type": "question_closed", "session_id": self.owner.session.id,
            "question_id": pending.question_id, "message": human, "reason": reason, "tool_input": message.tool_input,
        }
        await self._persist(pending, message, event)
        if pending.native_ref is not None:
            assert pending.backend is not None
            pending.backend.reply_question(pending.native_ref, None)
        pending.ready.set()
        pending.retirement_requested = True
        self._retire_if_finished(pending)
        return True

    async def _expire(self, pending: PendingQuestion) -> bool:
        if (pending.state != "pending" or pending.expires_at is None
                or time.monotonic() < pending.expires_at):
            return False
        if not pending.is_current(self.owner.session._run.question_attempt_id):
            return await self._close(pending, "attempt_ended")
        return await self._answer(pending, self.owner.manager.AUTO_ANSWER_TEXT, auto=True)

    async def deadlines(self) -> None:
        backend = self.owner.session._run.backend
        execution = self.owner.runtime.current
        if backend is None and execution is not None and execution.attempt is not None:
            backend = execution.attempt.backend
        if isinstance(backend, HarnessRun):
            event = backend.take_question()
            if event is not None:
                assert event.native_question is not None
                await self._create(RegisterNativeQuestion(event.tool_use_id or "", event.native_question.attempt_id,
                    (event.tool_input or {})["questions"], backend, event))
                self.owner.wake()
        for pending in tuple(self.owner.session._questions.values()):
            if (pending.backend is not None and pending.backend.captures_retirement_output
                    and pending.state == "pending" and not pending.is_current(self.owner.session._run.question_attempt_id)):
                await self._close(pending, "attempt_ended")
                self.owner.wake()
                return
        for pending in tuple(self.owner.session._questions.values()):
            timer = pending.timer
            if timer is not None and timer.done() and not timer.cancelled():
                pending.timer = None
                await self._expire(pending)
                # Each real timer keeps its own fact. Process one before
                # giving queued controls their next turn, rather than make
                # a simultaneous deadline batch delay Interrupt/Close.
                self.owner.wake()
                return

    @property
    def has_waiting_notifications(self) -> bool:
        return any(notice is not None and notice.publication is None
                   for pending in self.owner.session._questions.values()
                   for notice in (pending.request_publication, pending.publication))

    def capture_prefix(self, request_id: str) -> tuple[QuestionNotification, ...]:
        """The original request's finite notifications, derived from its records."""
        return tuple(notice for pending in self.owner.session._questions.values()
                     if pending.request_id == request_id
                     for notice in (pending.request_publication, pending.publication)
                     if notice is not None)

    def flush_notifications(self) -> None:
        waiting = sorted(
            ((notice.order, pending, notice)
             for pending in self.owner.session._questions.values()
             for notice in (pending.request_publication, pending.publication)
             if notice is not None and notice.publication is None),
            key=lambda item: item[0],
        )
        for _order, pending, notice in waiting:
            publication = self.owner.publisher.offer(
                notice.event, self.owner.session.user_id, marker=QuestionPublished(pending, notice),
            )
            if publication is None:
                break
            notice.publication = publication

    def published(self, marker: QuestionPublished) -> None:
        if self._owned(marker.record):
            if marker.notification.publication is not None and marker.notification.publication.error is not None:
                self._retain_prefix(marker.record)
            if marker.record.request_publication is marker.notification:
                marker.record.request_publication = None
            if marker.record.publication is marker.notification:
                marker.record.publication = None
            self._retire_if_finished(marker.record)

    def _retain_prefix(self, pending: PendingQuestion) -> None:
        """The original request retains unfinished/failed notification evidence."""
        session = self.owner.session
        settlement = session._run.settlement
        if (settlement is None or pending.request_id != settlement.request_id
                or settlement.request_id not in session._request_completions):
            return
        for notice in (pending.request_publication, pending.publication):
            if notice is not None and (
                not notice.completed or (notice.publication is not None and notice.publication.error is not None)
            ) and all(previous is not notice for previous in settlement.question_prefix):
                settlement.question_prefix += (notice,)

    def _retire_if_finished(self, pending: PendingQuestion) -> None:
        if (pending.retirement_requested and self._owned(pending)
                and all(notice is None or notice.completed
                        for notice in (pending.request_publication, pending.publication))):
            self.owner.session._questions.pop(pending.question_id)

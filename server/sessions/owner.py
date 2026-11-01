"""Session commands and their ordered I/O, without a second state store."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ..config import settings
from ..diagnostics import exception_diagnostic
from ..harness import HarnessRun
from ..models import MessageContent, MessageRole
from .base import BackendRetirement, ForkError, Session, SessionManagerBase, logger
from .commands import CorrelatedControl, command_digest
from .owner_questions import (
    CreateQuestion,
    OwnerQuestions,
    QuestionCommand,
    QuestionPublished,
    QuestionResult,
    question_command_bytes,
)
from .owner_runtime import (
    Execution,
    IdleEvent,
    IdlePublication,
    OwnerRuntime,
    RuntimeCommand,
    Submit,
)
from .publication import SessionPublisher
from .runtime_types import QuotaExceeded
from .runtime_types import SessionMailboxFull as SessionMailboxFull
from .runtime_types import SessionOwnerUnavailable as SessionOwnerUnavailable

if TYPE_CHECKING:
    from . import SessionManager


@dataclass(frozen=True)
class RetainResultNotice:
    delivery_id: str
    content: str


@dataclass
class _NoticeCommand:
    notice: RetainResultNotice
    reply: asyncio.Future[bool]
    size: int


@dataclass
class _QuestionCommand:
    command: QuestionCommand
    reply: asyncio.Future[QuestionResult]
    size: int


@dataclass
class _RuntimeCommand:
    command: RuntimeCommand
    reply: asyncio.Future
    size: int


@dataclass(frozen=True)
class _NoticeDiagnostic:
    backend: HarnessRun | None


class SessionOwner:
    """Own command handoff and the Session fields used by those commands."""

    def __init__(self, manager: SessionManagerBase, session: Session) -> None:
        self.manager = cast("SessionManager", manager)
        self.session = session
        self._wake = asyncio.Event()
        self._commands: deque[_NoticeCommand | _QuestionCommand | _RuntimeCommand] = deque()
        self._command_bytes = 0
        self._controls: deque[_QuestionCommand | _RuntimeCommand] = deque()
        self._control_bytes = 0
        self._control_space = asyncio.Event()
        self._control_space.set()
        self._control_burst = 0
        self._closing = False
        self._current: _NoticeCommand | _QuestionCommand | _RuntimeCommand | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._commands_drained = asyncio.Event()
        self._commands_drained.set()
        self.publisher = SessionPublisher(manager._broadcast, self._wake.set)
        self.questions = OwnerQuestions(self)
        self.runtime = OwnerRuntime(self)
        self.task = asyncio.create_task(self._run(), name=f"session-owner-{session.id}")
        self.cleanup: asyncio.Task[None] | None = None
        self.resource_cleanup: asyncio.Task[None] | None = None

    @property
    def failed(self) -> bool:
        return self.task.done() and (self.task.cancelled() or self.task.exception() is not None)

    @property
    def sealed(self) -> bool:
        return self._closing

    def require_available(self) -> None:
        if self._closing or self.task.done():
            raise SessionOwnerUnavailable("Session owner is unavailable; retained input requires review")

    async def call(self, notice: RetainResultNotice) -> bool:
        self.require_available()
        size = len(json.dumps((notice.delivery_id, notice.content), ensure_ascii=True).encode("ascii"))
        if len(self._commands) >= 256 or (
            self._commands and self._command_bytes + size > 1024 * 1024
        ):
            raise SessionMailboxFull("Session command capacity is full; retry this delivery later")
        reply: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        # The envelope, rather than its HTTP/BG waiter, owns this result.
        reply.add_done_callback(self._observe_reply)
        self._commands.append(_NoticeCommand(notice, reply, size))
        self._command_bytes += size
        self._idle.clear()
        self._commands_drained.clear()
        self._wake.set()
        return await asyncio.shield(reply)

    async def call_question(self, command: QuestionCommand) -> QuestionResult:
        """Question controls retain entry independently of ordinary commands.

        Capacity waiting precedes command handoff. Once an envelope is owned,
        disconnecting its caller cannot cancel the transition or its notices.
        """
        self.require_available()
        size = question_command_bytes(command)
        if isinstance(command, CreateQuestion):
            if len(self._commands) >= 256 or (self._commands and self._command_bytes + size > 1024 * 1024):
                raise SessionMailboxFull("Session command capacity is full; retry this question later")
        else:
            while len(self._controls) >= 256 or (self._controls and self._control_bytes + size > 1024 * 1024):
                self._control_space.clear()
                await self._control_space.wait()
                self.require_available()
        reply: asyncio.Future[QuestionResult] = asyncio.get_running_loop().create_future()
        reply.add_done_callback(self._observe_reply)
        envelope = _QuestionCommand(command, reply, size)
        if isinstance(command, CreateQuestion):
            self._commands.append(envelope)
            self._command_bytes += size
        else:
            self._controls.append(envelope)
            self._control_bytes += size
        self._idle.clear()
        self._commands_drained.clear()
        self._wake.set()
        return await asyncio.shield(reply)

    def wake(self) -> None:
        self._wake.set()

    def control_conflicts(self, data: dict[str, Any]) -> bool:
        """The first original envelope owns its ID before a receipt can commit."""
        for entry in (self._current, *self._controls):
            if (isinstance(entry, _RuntimeCommand) and isinstance(entry.command, CorrelatedControl)
                    and entry.command.data.get("client_command_id") == data.get("client_command_id")):
                return command_digest(entry.command.data) != command_digest(data)
        return False

    async def call_runtime(self, command: RuntimeCommand) -> Any:
        if asyncio.current_task() is self.task:
            return await self.runtime.handle(command)
        self.require_available()
        size = (len(command.request.prompt.encode("utf-8")) + len(command.request.attachment_ids) * 64
                if isinstance(command, Submit) else
                len(json.dumps((command.data, command.source.details()), ensure_ascii=True).encode("ascii")) + 128
                if isinstance(command, CorrelatedControl) else 128)
        ordinary = isinstance(command, Submit)
        if ordinary:
            if len(self._commands) >= 256 or (self._commands and self._command_bytes + size > 1024 * 1024):
                raise SessionMailboxFull("Session command capacity is full; retry this input later")
        else:
            while len(self._controls) >= 256 or (self._controls and self._control_bytes + size > 1024 * 1024):
                self._control_space.clear()
                await self._control_space.wait()
                self.require_available()
        reply = asyncio.get_running_loop().create_future()
        reply.add_done_callback(self._observe_reply)
        envelope = _RuntimeCommand(command, reply, size)
        if ordinary:
            self._commands.append(envelope)
            self._command_bytes += size
        else:
            self._controls.append(envelope)
            self._control_bytes += size
            self.runtime.freeze_native_control(command)
        self._idle.clear()
        self._commands_drained.clear()
        self._wake.set()
        return await asyncio.shield(reply)

    @staticmethod
    def _observe_reply(reply: asyncio.Future) -> None:
        if not reply.cancelled():
            reply.exception()

    def seal(self) -> None:
        """Close is independent of ordinary capacity and drains handed-off work."""
        self._closing = True
        self._wake.set()

    async def close(self) -> None:
        self.seal()
        # A subscriber may seal its owner but cannot join its own publication.
        if not self.publisher.in_callback():
            await asyncio.shield(self.task)

    @property
    def idle(self) -> bool:
        return (self._current is None and not self._commands and not self._controls
                and not self.questions.has_waiting_notifications and self.publisher.idle
                and self.runtime.idle)

    def drain_blockers(self) -> tuple[str, ...]:
        """Observe this original writer; a held idle host retires at shutdown."""
        session = self.session
        if session._pending_queue or session._steer_queue:
            return ("retained input",)
        if session._forking or session._archive_reservation is not None:
            return ("session lifecycle",)
        if self.failed:
            cleanup = self.cleanup
            if cleanup is None or not cleanup.done() or cleanup.cancelled() or cleanup.exception() is not None:
                return ("failed owner cleanup",)
            return ()
        if not self.idle or any(question.state == "pending" for question in session._questions.values()):
            return ("command, execution, question or publication",)
        return ()

    async def wait_idle(self) -> None:
        while not self.idle:
            if self.failed:
                raise SessionOwnerUnavailable("Session owner failed during publication drain")
            await self._idle.wait()

    async def wait_commands(self) -> None:
        """A row can retire after its original commands, before subscriber I/O."""
        while self._current is not None or self._commands or self._controls:
            if self.failed:
                raise SessionOwnerUnavailable("Session owner failed before its commands retired")
            await self._commands_drained.wait()

    async def _retain(self, notice: RetainResultNotice) -> bool:
        session, manager = self.session, self.manager
        if manager.sessions.get(session.id) is not session:
            return False
        if notice.delivery_id in session._delivery_notice_ids:
            return True
        if manager.db and await manager.db.has_delivery_notice(session.id, notice.delivery_id):
            session._delivery_notice_ids.add(notice.delivery_id)
            return True

        message = MessageContent(
            role=MessageRole.system, type="delivery_notice", content=notice.content,
            tool_input={"delivery_id": notice.delivery_id}, created_at=datetime.now(UTC).isoformat(),
        )
        event = manager._tag_persisted({
            "type": "delivery_notice", "session_id": session.id,
            "content": message.content, "tool_input": message.tool_input,
        }, session._next_message_seq if manager.db else None, message)
        # Only this owner offers notifications, so a successful capacity check
        # remains valid across the bounded database operation below.
        if self.questions.has_waiting_notifications or not self.publisher.can_offer(event):
            raise SessionMailboxFull("Session notification capacity is full; retry this delivery later")
        seq = await manager._persist_message(session, message)
        manager._tag_persisted(event, seq, message)
        publication = self.publisher.offer(event, session.user_id, marker=_NoticeDiagnostic(session._run.backend))
        assert publication is not None, "Owner publication reservation was lost"
        session._delivery_notice_ids.add(notice.delivery_id)
        return True

    async def _run(self) -> None:
        try:
            while True:
                self._wake.clear()
                for publication in self.publisher.take_completed():
                    self.runtime.published(publication)
                    if isinstance(publication.marker, QuestionPublished):
                        self.questions.published(publication.marker)
                    if publication.error is not None:
                        captured = publication.marker
                        detail = (captured.format_exception(publication.error) if isinstance(captured, Execution) else
                                  captured.backend.format_exception(publication.error)
                                  if isinstance(captured, (_NoticeDiagnostic, QuestionPublished, BackendRetirement, IdleEvent)) and captured.backend is not None
                                  else exception_diagnostic(publication.error, secrets=(settings.auth_token,)))
                        logger.error(
                            "Session notice delivery failed session=%s error=%s\n%s",
                            self.session.id, type(publication.error).__name__,
                            detail,
                        )
                native_stop = next((entry for entry in self._controls if isinstance(entry, _RuntimeCommand)
                                    and self.runtime.native_control_matches(entry.command)), None)
                # The accepted Stop already sealed this native host. Apply its
                # original control before interpreting responses caused by that
                # retirement; otherwise an early EOF/refusal can steal outcome
                # selection while the Owner was recording an answer.
                if native_stop is None:
                    await self.questions.deadlines()
                    self.questions.flush_notifications()
                    await self.runtime.advance()
                self.questions.flush_notifications()
                self.runtime.flush_ready()
                control = native_stop or next((entry for entry in self._controls
                                if not isinstance(entry, _RuntimeCommand) or self.runtime.can_handle(entry.command)), None)
                if control is not None or self._commands:
                    current: _NoticeCommand | _QuestionCommand | _RuntimeCommand
                    if control is not None and (native_stop is not None or not self._commands or self._control_burst < 8):
                        self._controls.remove(control)
                        current = self._current = control
                        self._control_bytes -= current.size
                        self._control_space.set()
                        self._control_burst += 1
                    else:
                        current = self._current = self._commands.popleft()
                        self._command_bytes -= current.size
                        self._control_burst = 0
                    try:
                        if isinstance(current, _RuntimeCommand):
                            try:
                                result = await self.runtime.handle(current.command)
                            except (ValueError, ForkError, QuotaExceeded) as error:
                                if not current.reply.done():
                                    current.reply.set_exception(error)
                            else:
                                if isinstance(result, IdlePublication):
                                    result.reply = current.reply
                                elif not current.reply.done():
                                    current.reply.set_result(result)
                        elif isinstance(current, _QuestionCommand):
                            try:
                                result = await self.questions.handle(current.command)
                            except ValueError as error:
                                if not current.reply.done():
                                    current.reply.set_exception(error)
                            else:
                                if not current.reply.done():
                                    current.reply.set_result(result)
                        else:
                            retained = await self._retain(current.notice)
                            if not current.reply.done():
                                current.reply.set_result(retained)
                    except SessionMailboxFull as error:
                        if not current.reply.done():
                            current.reply.set_exception(error)
                    self._current = None
                    if not self._commands and not self._controls:
                        self._commands_drained.set()
                    continue
                if self.idle:
                    self._idle.set()
                if self._closing and self.publisher.idle and self.runtime.idle:
                    await self.publisher.close()
                    return
                await self._wake.wait()
        finally:
            self._closing = True
            self._idle.set()
            self._commands_drained.set()
            self._control_space.set()
            error = SessionOwnerUnavailable("Session owner ended before confirming this command")
            self.runtime.end_reports(error)
            remaining = (([self._current] if self._current is not None else [])
                         + list(self._controls) + list(self._commands))
            for command in remaining:
                if not command.reply.done():
                    command.reply.set_exception(error)
            self._commands.clear()
            self._controls.clear()
            self._command_bytes = 0
            self._control_bytes = 0

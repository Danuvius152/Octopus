"""One Session's execution decisions and the results of its captured I/O.

Only SessionOwner invokes the transition methods below. Resource operations
retain their results in the original task; their callbacks merely wake that
owner. A stream has one outstanding read, so publication backpressure reaches
the Harness producer rather than creating a second event buffer.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from .. import fork_helpers
from ..attachments import AttachmentUnavailable, validate_message_attachments
from ..config import settings
from ..diagnostics import diagnostic_text, exception_diagnostic, safe_diagnostic
from ..harness import HarnessCredential, HarnessEvent, HarnessRun, get_harness
from ..harness.events import HarnessPreparationError
from ..harness.model_settings import effective_settings
from ..large_prompts import spill_if_large
from ..models import AttachmentMetadata, MessageContent, MessageRole, SessionStatus
from ..users import owner_is_available
from .base import (
    _MAX_PENDING_STEERS,
    ActiveRun,
    BackendRetirement,
    ForkError,
    PendingInput,
    QueuedPrompt,
    RecordedInput,
    Session,
    _augment_prompt_with_attachments,
    logger,
)
from .client_submissions import claim_input, load_pending_input, require_retired_writers
from .commands import CorrelatedControl, _process_owned_command
from .owner_questions import (
    BeginQuestionAttempt,
    CloseSessionQuestions,
    EndQuestionAttempt,
)
from .publication import Publication
from .runtime_types import (
    QuotaExceeded,
    SessionMailboxFull,
    SessionOwnerUnavailable,
    _Attempt,
    _Settlement,
    _Turn,
)
from .submission import RequestCompletion, SubmissionReceipt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..wiki.tasks import WikiTask
    from .owner import SessionOwner


@dataclass(frozen=True)
class Submit:
    request: QueuedPrompt
    steerable: bool = False
    client_id: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    recovery_of: str | None = None
    schedule: dict[str, Any] | None = None


@dataclass(frozen=True)
class Resume:
    resume_queue: bool = True


@dataclass(frozen=True)
class Interrupt:
    cause: Literal["user", "shutdown"] = "user"


@dataclass(frozen=True)
class CancelRequest:
    request_id: str
    cause: Literal["user", "shutdown"] = "user"


@dataclass(frozen=True)
class Reset:
    pass


@dataclass(frozen=True)
class RecoveryDismissed:
    """The durable recovery row was already dismissed by its database owner."""

    client_id: str


@dataclass(frozen=True)
class CloseWork:
    work_id: str


@dataclass(frozen=True)
class WorkRetirement:
    resources: tuple[asyncio.Future[Any], ...]


@dataclass(frozen=True)
class RetireProcess:
    backend: HarnessRun
    reason: str = "process_ended"


@dataclass(frozen=True)
class RetireHeldProcess:
    backend: HarnessRun
    held_since: float


@dataclass(frozen=True)
class CloseExecution:
    reason: str
    reservation: object | None = None
    only_request_id: str | None = None


@dataclass(frozen=True)
class ReserveFork:
    reserve: bool


@dataclass(frozen=True)
class ApplyOwner:
    user_id: str


@dataclass(frozen=True)
class ReleaseArchive:
    reservation: object


@dataclass(frozen=True)
class AutoArchive:
    operation: asyncio.Task[bool]


@dataclass(frozen=True)
class FinishAutoArchive:
    reservation: object
    revision: int
    retirement: asyncio.Task[None] | None


@dataclass(frozen=True)
class IdleEvent:
    backend: HarnessRun
    event: HarnessEvent
    request_id: str | None
    attempt_id: str | None
    run_lease_id: str | None
    work_id: str | None


@dataclass
class IdlePublication:
    """The original stdout reader's one accepted, not-yet-offered frame.

    The reader waits for this handoff, so it cannot produce a second frame.
    Resource retirement can cancel that waiter without cancelling the frame
    already captured by the owner or joining its calling subscriber.
    """

    original: IdleEvent
    frame: dict[str, Any]
    audience: str | None
    reply: asyncio.Future[bool] | None = None


RuntimeCommand = (Submit | Resume | Interrupt | CancelRequest | Reset | CloseWork | RetireProcess | RetireHeldProcess
                  | CloseExecution | ReserveFork | ApplyOwner | ReleaseArchive | AutoArchive | FinishAutoArchive
                  | IdleEvent | CorrelatedControl | RecoveryDismissed)


@dataclass(frozen=True)
class _PreparedInput:
    attachments: tuple[AttachmentMetadata, ...]
    paths: tuple[str, ...]
    git_head: str | None
    git_clean: bool | None


@dataclass(frozen=True)
class _PreparedRun:
    configured: Session
    agent: dict[str, Any] | None
    credential: HarnessCredential | None
    connectors: list[tuple[Any, Any]]
    backend: HarnessRun
    prompt: str


@dataclass
class PreparationDiagnostic:
    """The original preparation retains its own credential, even on failure."""

    credential: HarnessCredential | None = None

    def format_exception(self, error: BaseException) -> str:
        return exception_diagnostic(error, secrets=(settings.auth_token, self.credential.secret if self.credential else ""))


@dataclass
class _Activity:
    """Observations belong to this operation, not to the shared attempt."""

    latest: float = 0.0
    consumed: float = 0.0

    def observe(self, timestamp: float | None = None) -> None:
        self.latest = max(self.latest, time.monotonic() if timestamp is None else timestamp)


@dataclass
class RuntimeReader:
    """The original reader's one offered frame, with captured provenance."""

    backend: HarnessRun
    request_id: str
    attempt_id: str
    run_lease_id: str | None
    work_id: str | None
    event: HarnessEvent | None = None
    ready: bool = False
    consumed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    async def read(self, stream: AsyncIterator[HarnessEvent], wake: Any) -> None:
        async for event in stream:
            if event.content is not None and not isinstance(event.content, str):
                # A malformed provider frame fails this captured stream, not
                # the owner which consumes normalized text from the Harness.
                raise TypeError(f"Invalid {event.type} content ({type(event.content).__name__}): {event.content!r}")
            self.consumed.clear()
            self.event, self.ready = event, True
            wake()
            await self.consumed.wait()

    def take(self) -> HarnessEvent:
        assert self.ready and self.event is not None
        event = self.event
        self.event, self.ready = None, False
        self.consumed.set()
        return event


@dataclass
class RuntimeDeadline:
    when: float | None = None
    expired: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    async def wait(self, wake: Any) -> None:
        while True:
            self.changed.clear()
            when = self.when
            if when is None:
                await self.changed.wait()
                continue
            try:
                async with asyncio.timeout(max(0, when - time.monotonic())):
                    await self.changed.wait()
            except TimeoutError:
                self.expired = True
                wake()
                await self.changed.wait()

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


@dataclass
class Execution:
    request: QueuedPrompt
    settlement: _Settlement
    completed: asyncio.Future[None]
    retired: asyncio.Future[None]
    phase: Literal["claim", "input", "prepare", "start", "stream", "retire", "retry", "terminal"] = "claim"
    operation: asyncio.Task[Any] | None = None
    turn: _Turn | None = None
    attempt: _Attempt | None = None
    prepared: _PreparedRun | None = None
    preparation_diagnostic: PreparationDiagnostic | None = None
    reader: RuntimeReader | None = None
    activity: _Activity | None = None
    activity_token: object | None = None
    deadline: RuntimeDeadline | None = None
    interrupted: bool = False
    stream_ended: bool = False
    start_ended: bool = False
    frames: deque[dict[str, Any]] = field(default_factory=deque)
    prefix: list[Publication] = field(default_factory=list)
    terminal: Publication | None = None
    retirement: asyncio.Task[None] | None = None
    publication_failed: bool = False
    completion_reason: Literal["expired"] | None = None

    def format_exception(self, error: BaseException) -> str:
        backend = self.attempt.backend if self.attempt is not None else None
        diagnostic = self.preparation_diagnostic
        detail = backend.format_exception(error) if backend is not None else exception_diagnostic(error)
        credential = self.prepared.credential if self.prepared is not None else diagnostic.credential if diagnostic is not None else None
        return diagnostic_text(detail, secrets=(settings.auth_token, credential.secret if credential else ""))


@dataclass
class _Steer:
    request: QueuedPrompt
    execution: Execution
    backend: HarnessRun
    previous: Any
    prepared: asyncio.Task[Any]
    write: asyncio.Task[bool] | None = None
    message: MessageContent | None = None
    seq: int | None = None
    merged: Any = None
    wiki_task_id: str | None = None
    previous_wiki_task: WikiTask | None = None
    installed_wiki_task: WikiTask | None = None


def capture_configuration(session: Session) -> Session:
    """Capture configuration without retaining live maps, locks or run state."""
    return Session(
        id=session.id, name=session.name, working_dir=session.working_dir,
        claude_session_id=session.claude_session_id, credential_id=session.credential_id,
        agent_id=session.agent_id, origin=session.origin, backend=session.backend,
        model=session.model, reasoning_effort=session.reasoning_effort,
        service_tier=session.service_tier, user_id=session.user_id,
        parent_session_id=session.parent_session_id, app_id=session.app_id,
        forked_from_session_id=session.forked_from_session_id, fork_after_seq=session.fork_after_seq,
        fork_needs_replay=session.fork_needs_replay, fork_metadata=session.fork_metadata,
        _run=ActiveRun(knowledge_context=session._run.knowledge_context,
                       wiki_task_id=session._run.wiki_task_id, wiki_mode=session._run.wiki_mode,
                       settlement=replace(session._run.settlement) if session._run.settlement is not None else None),
    )


class OwnerRuntime:
    def __init__(self, owner: SessionOwner) -> None:
        self.owner = owner
        self.current: Execution | None = None
        self.steer: _Steer | None = None
        self.dispatch_notice: dict[str, Any] | None = None
        self.status_notice: dict[str, Any] | None = None
        self._idle_action: asyncio.Task[Any] | None = None
        self._auto_archive_operation: asyncio.Task[bool] | None = None
        self._idle_publication: IdlePublication | None = None

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.owner.session._run.retirements)

    @property
    def idle(self) -> bool:
        return (not self.busy and self.steer is None and self.dispatch_notice is None
                and self.status_notice is None and self._idle_action is None
                and self._auto_archive_operation is None and self._idle_publication is None)

    def task(self, operation: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation, name=name)
        # A result is retained in this exact task until the owner consumes it.
        task.add_done_callback(lambda _done: self.owner.wake())
        return task

    def changed(self) -> None:
        self.dispatch_notice = self.owner.manager._dispatch_event(self.owner.session)
        self.owner.wake()

    def notify_status(self, value: SessionStatus) -> None:
        session = self.owner.session
        session.status = value
        self.status_notice = {"type": "status", "session_id": session.id, "status": value.value}

    def can_handle(self, command: RuntimeCommand) -> bool:
        if isinstance(command, IdleEvent):
            return self._idle_publication is None
        return True

    async def handle(self, command: RuntimeCommand) -> Any:
        session, manager = self.owner.session, self.owner.manager
        if isinstance(command, Submit):
            return await self._submit(command)
        if isinstance(command, RecoveryDismissed):
            self.changed()
            return True
        if isinstance(command, Resume):
            if session._pending_queue:
                manager._check_turn_quota(session)
            if command.resume_queue:
                session._dispatch_paused = False
            self.changed()
            return True
        if isinstance(command, Interrupt):
            session._dispatch_paused = True
            for request in (*session._pending_queue, *session._steer_queue):
                request.priority = False
            return await self._interrupt(command.cause, retain_unstarted=True)
        if isinstance(command, CancelRequest):
            removed = [item for item in (*session._pending_queue, *session._steer_queue)
                       if item.request_id == command.request_id]
            session._pending_queue[:] = [item for item in session._pending_queue if item not in removed]
            session._steer_queue[:] = [item for item in session._steer_queue if item not in removed]
            for request in removed:
                await self.complete(request, reason="shutdown" if command.cause == "shutdown" else "withdrawn")
            if self.current is not None and self.current.request.request_id == command.request_id:
                original = self.current
                await self._interrupt(command.cause)
                return True, original.retired
            self.changed()
            return bool(removed), None
        if isinstance(command, CloseWork):
            captured = self.current if self.current is not None and self.current.request.work_id == command.work_id else None
            removed = [item for item in (*session._pending_queue, *session._steer_queue)
                       if item.work_id == command.work_id]
            session._pending_queue[:] = [item for item in session._pending_queue if item not in removed]
            session._steer_queue[:] = [item for item in session._steer_queue if item not in removed]
            for request in removed:
                await self.complete(request, reason="withdrawn")
            steer = self.steer if self.steer is not None and self.steer.request.work_id == command.work_id else None
            if self.current is not None and self.current.request.work_id == command.work_id:
                await self._interrupt("user", code="work_cancelled", message="(delegated work cancelled)")
            backend = session._run.backend
            if backend is not None and backend.work_id == command.work_id:
                self.retire(backend, "work_cancelled")
            self.changed()
            resources: tuple[asyncio.Future[Any], ...] = (
                (captured.retired,) if captured is not None else ()
            ) + tuple(resource.task for resource in session._run.retirements.values()
                      if resource.backend.work_id == command.work_id) + (
                (asyncio.gather(
                    steer.prepared, *((steer.write,) if steer.write is not None else ()),
                    return_exceptions=True,
                ),) if steer is not None else ()
            )
            # The original Owner classifies an uncertain input write. Work
            # retirement only joins its I/O; that input error cannot stop a
            # different work's active request or poison cancellation drain.
            return WorkRetirement(resources)
        if isinstance(command, (Reset, CloseExecution)):
            if isinstance(command, CloseExecution):
                if not self.can_close_request(command.only_request_id):
                    return False
                if command.reservation is not None:
                    if session._archive_reservation is not None or session._admission_closed:
                        raise ValueError(f"Session {session.id} is already being archived")
                    session._archive_reservation = command.reservation
                if command.reason != "shutdown":
                    session._admission_closed = True
            reason = command.reason if isinstance(command, CloseExecution) else "reset"
            pending = [*session._pending_queue, *session._steer_queue]
            session._pending_queue.clear()
            session._steer_queue.clear()
            for request in pending:
                await self.complete(request, reason=reason)
            session._dispatch_paused = isinstance(command, CloseExecution)
            await self._interrupt("shutdown" if reason == "shutdown" else "user")
            await self.owner.questions.handle(CloseSessionQuestions(reason, tuple(session._questions.values())))
            self.changed()
            return True
        if isinstance(command, RetireProcess):
            return self.retire(command.backend, command.reason)
        if isinstance(command, RetireHeldProcess):
            if (self.current is not None or session._run.backend is not command.backend
                    or session._run.held_since != command.held_since):
                return None
            return self.retire(command.backend, "held_process_reaped")
        if isinstance(command, ReserveFork):
            if command.reserve:
                from ..delegations import delegation_manager
                if session._dispatch_paused:
                    raise ForkError("Parent session dispatch is paused; resume it before forking", reason="fork_blocked_parent_turn_active", status_code=409)
                if self.busy or session._dispatch_paused or session._pending_queue or session._forking:
                    raise ForkError("Parent session has active work", reason="fork_blocked_parent_turn_active", status_code=409)
                if delegation_manager.has_active_delegation_for_parent(session.id):
                    raise ForkError("Parent session has an active delegation", reason="fork_blocked_parent_turn_active", status_code=409)
                session._forking = True
                return capture_configuration(session)
            else:
                session._forking = False
            return True
        if isinstance(command, ApplyOwner):
            from ..mcp_identity import revoke_run
            revoke_run(getattr(session._run.backend, "run_lease_id", None))
            session.user_id = command.user_id
            return True
        if isinstance(command, ReleaseArchive):
            if session._archive_reservation is command.reservation:
                session._archive_reservation = None
                if manager.sessions.get(session.id) is session:
                    session._admission_closed = False
            return True
        if isinstance(command, AutoArchive):
            if (self.current is not None or session._dispatch_paused or session._pending_queue
                    or session._archive_reservation is not None
                    or session._forking or manager.sessions.get(session.id) is not session):
                return None
            reservation = session._archive_reservation = object()
            self._auto_archive_operation = command.operation
            command.operation.add_done_callback(lambda _done: self.owner.wake())
            backend = session._run.backend
            retirement = self.retire(backend, "auto_archive") if backend is not None else None
            return FinishAutoArchive(reservation, session._dispatch_revision, retirement)
        if isinstance(command, FinishAutoArchive):
            if (session._archive_reservation is not command.reservation
                    or self.current is not None or session._dispatch_paused or session._pending_queue
                    or session._dispatch_revision != command.revision
                    or manager.sessions.get(session.id) is not session):
                return False
            session._admission_closed = True
            session._archive_reservation = None
            manager.sessions.pop(session.id)
            manager._session_removed(session)
            return True
        if isinstance(command, IdleEvent):
            return await self._idle_event(command)
        if isinstance(command, CorrelatedControl):
            return await _process_owned_command(manager, session, command.data, command.user_id)
        raise AssertionError(f"Unknown Session command: {type(command).__name__}")

    def can_close_request(self, request_id: str | None) -> bool:
        """The same original-input boundary survives a failed writer."""
        session = self.owner.session
        return request_id is None or all(request.request_id == request_id for request in (
            *session._pending_queue, *session._steer_queue,
            *((session._run.request,) if session._run.request is not None else ()),
        ))

    def owns_work(self, work_id: str) -> bool:
        """Include original preparation and retirement after queue removal."""
        session = self.owner.session
        return (any(request.work_id == work_id for request in (*session._pending_queue, *session._steer_queue))
                or self.current is not None and self.current.request.work_id == work_id
                or self.steer is not None and self.steer.request.work_id == work_id
                or session._run.backend is not None and session._run.backend.work_id == work_id
                or any(resource.backend.work_id == work_id for resource in session._run.retirements.values()))

    async def _submit(self, command: Submit) -> SubmissionReceipt:
        from ..knowledge import for_entry
        from ..mcp_identity import work_is_open
        from .client_submissions import input_digest

        session, manager, request = self.owner.session, self.owner.manager, command.request
        payload: dict[str, Any] = {"content": request.prompt, "attachment_ids": request.attachment_ids,
                   "attachments": list(command.attachments), "wiki_mode": request.wiki_mode}
        if command.recovery_of:
            payload["recovery_of_client_id"] = command.recovery_of
        digest = input_digest(payload)
        client_id = command.client_id or (
            f"{request.source}:{request.delivery_id}" if request.delivery_id else request.request_id
        )
        if manager.db is not None:
            existing = await manager.db.get_client_submission(session.id, client_id)
            if existing is not None:
                return self._existing_input(existing, digest)
        new_session = command.schedule is not None and session.origin == "schedule" and session.id not in manager.sessions
        if not new_session and manager.sessions.get(session.id) is not session:
            raise ValueError(f"Session {session.id} not found")
        if request.delivery_id and request.delivery_id in session._delivery_receipts:
            return replace(session._delivery_receipts[request.delivery_id], disposition="duplicate")
        await manager.require_active_agent(session.agent_id)
        if session._transcript_integrity != "ok":
            raise ValueError("Transcript has duplicate sequences; export and review it before continuing")
        closed = (manager._admission_closed or manager._legacy_transition or session._admission_closed
                  or session.agent_id in manager._closed_agents or not work_is_open(request.work_id))
        if closed:
            return SubmissionReceipt.closed(request.request_id, "shutdown" if manager._admission_closed else "withdrawn")
        await require_retired_writers(manager, session.id)
        if session._forking or (session._archive_reservation is not None
                and not (session._dispatch_paused and self._auto_archive_operation is not None)):
            raise ValueError(f"Session {session.id} is busy (forking or archiving)")
        request.knowledge_context = request.knowledge_context or for_entry(
            session, request.prompt if request.trusted_request is None else request.trusted_request,
            request_id=request.request_id, selected=request.wiki_mode,
        )
        if request.knowledge_context.origin.user_id != session.user_id:
            raise ValueError("Request knowledge belongs to another account")
        request.wiki_mode = request.knowledge_context.mode
        request.trusted_request = request.knowledge_context.trusted_request
        request.priority = session._dispatch_paused and request.source == "human"
        if request.source == "human":
            manager._check_turn_quota(session)
        steering = (command.steerable and request.source == "human" and self.current is not None
                and session._steer_open and not request.attachment_ids
                and request.wiki_mode in {None, "auto"} and session._run.wiki_mode in {None, "auto"}
                and (not session._dispatch_paused or self.current.request.priority)
                and len(session._steer_queue) < _MAX_PENDING_STEERS)
        # An accepted input owns its notification too. At most the current
        # handoff can wait outside the publisher's budget; a slow subscriber
        # must not create a full queue snapshot on every pending request.
        if (self.owner.questions.has_waiting_notifications
                or any(item.queued_event is not None for item in session._pending_queue)):
            raise SessionMailboxFull("Session notification capacity is full; retry this input later")
        snapshot = manager._session_dispatch_snapshot(session)
        snapshot["revision"] += 1
        if not steering:
            snapshot["items"].append({
                "request_id": request.request_id, "source": request.source,
                "summary": request.prompt[:1000], "priority": request.priority,
            })
        queued = not steering and (self.current is not None or session._dispatch_paused)
        notification = ({
            "type": "queued", "session_id": session.id, "content": request.prompt,
            "request_id": request.request_id, "queue_length": len(session._pending_queue) + 1,
            "dispatch_state": snapshot,
        } if queued else {"type": "dispatch_state", "session_id": session.id, "dispatch_state": snapshot})
        if not self.owner.publisher.can_offer(notification):
            raise SessionMailboxFull("Session notification capacity is full; retry this input later")
        request.order = session._dispatch_order + 1
        disposition = "accepted"
        if steering:
            disposition = "steered"
        if manager.db is not None:
            from ..knowledge import encode

            payload.update(source=request.source, delivery_id=request.delivery_id, work_id=request.work_id,
                           question_session_id=request.question_session_id, question_id=request.question_id,
                           trusted_request=request.trusted_request, knowledge=encode(request.knowledge_context),
                           priority=request.priority, order=request.order)
            created, row = await manager.db.admit_client_submission(
                session.id, client_id, digest, request.request_id, payload=payload,
                receipt={"accepted": True, "disposition": disposition}, recovery_of=command.recovery_of,
                schedule=command.schedule,
                new_session={key: getattr(session, key) for key in (
                    "id", "name", "working_dir", "created_at", "credential_id", "agent_id", "origin", "backend", "user_id",
                )} if new_session else None,
            )
            if not created:
                return self._existing_input(row, digest)
            request.durable_client_input = True
        if new_session:
            manager.sessions[session.id] = session
        if queued:
            request.queued_event = notification
        session._dispatch_order = request.order
        if steering:
            session._steer_queue.append(request)
        else:
            session._pending_queue.append(self._pending_input(request))
        receipt = manager._new_receipt(session, request, disposition)
        if request.delivery_id:
            session._delivery_receipts[request.delivery_id] = receipt
        self.changed()
        return receipt

    @staticmethod
    def _pending_input(request: QueuedPrompt) -> PendingInput | QueuedPrompt:
        if not request.durable_client_input:
            return request
        return PendingInput(request.request_id, request.source, request.summary, request.work_id,
                            request.priority, request.order, request.queued_event)

    def _existing_input(self, row: dict[str, Any], digest: str) -> SubmissionReceipt:
        """A duplicate observes the original input; it never queues it again."""
        if row["payload_sha256"] != digest:
            from ..submission_models import SubmissionConflict

            raise SubmissionConflict("This submission identity already belongs to different input.")
        from .submission import receipt_for_input

        return receipt_for_input(row, self.owner.session._request_completions.get(row["request_id"]))

    async def _interrupt(
        self, cause: Literal["user", "shutdown"], *, retain_unstarted: bool = False,
        code: str | None = None, message: str | None = None,
    ) -> bool:
        session, execution = self.owner.session, self.current
        session._steer_open = False
        if self.steer is not None:
            if self.steer.write is not None:
                self.steer.write.cancel()
        if session._steer_queue:
            session._pending_queue.extend(session._steer_queue)
            session._pending_queue.sort(key=lambda request: request.order)
            session._steer_queue.clear()
        if execution is None:
            if session._run.backend is not None:
                self.retire(session._run.backend, "service_shutdown" if cause == "shutdown" else "user_stop")
            await self.owner.questions.handle(CloseSessionQuestions("interrupted", tuple(session._questions.values())))
            self.changed()
            return False
        if execution.interrupted:
            self.changed()
            return True
        execution.interrupted = True
        if execution.deadline is not None:
            execution.deadline.cancel()
        execution.settlement.request_interruption(cause)
        if not execution.request.started and retain_unstarted:
            execution.request.priority = False
            session._pending_queue.append(self._pending_input(execution.request))
            session._pending_queue.sort(key=lambda request: request.order)
        elif execution.request.started:
            original_message, original_code = execution.settlement.interruption()
            await self.owner.manager._settle_turn(
                session, execution.settlement, status="interrupted",
                message=message or original_message, code=code or original_code,
            )
        else:
            await self.complete(execution.request, reason="shutdown" if cause == "shutdown" else "withdrawn")
        if (execution.phase in {"start", "stream", "retry"}
                and execution.operation is not None and not execution.operation.done()):
            execution.operation.cancel()
        if execution.reader is not None and execution.reader.task is not None and not execution.reader.task.done():
            execution.reader.task.cancel()
        if execution.attempt is not None:
            execution.retirement = self.retire(execution.attempt.backend, "service_shutdown" if cause == "shutdown" else "user_stop")
        await self._end_questions(execution, "interrupted")
        self.changed()
        return True

    async def _end_questions(self, execution: Execution, reason: str = "attempt_ended") -> None:
        attempt = execution.attempt
        if attempt is not None:
            await self.owner.questions.handle(EndQuestionAttempt(
                attempt.attempt_id, reason,
                tuple(question for question in self.owner.session._questions.values()
                      if question.attempt_id == attempt.attempt_id),
            ))

    def retire(self, backend: HarnessRun, reason: str) -> asyncio.Task[None]:
        session, manager = self.owner.session, self.owner.manager
        existing = session._run.retirements.get(id(backend))
        if existing is not None and existing.backend is backend:
            return existing.task
        invalidate = getattr(backend, "invalidate_activity", None)
        if invalidate is not None:
            invalidate()
        frames = manager._forget_backend(session, backend, reason=reason)
        if self.current is not None:
            self.current.frames.extend(frames)
        session_id = session.id
        async def retire_original() -> None:
            try:
                await backend.interrupt()
            except Exception as error:
                logger.warning("Backend interrupt failed session=%s error=%s\n%s",
                               session_id, type(error).__name__, backend.format_exception(error))
            await backend.stop()
        task = self.task(retire_original(), f"backend-retirement-{session.id}")
        session._run.retirements[id(backend)] = BackendRetirement(
            backend, task, deque(frames if self.current is None else ()), session.user_id,
        )
        return task

    def completion(
        self, request: QueuedPrompt | PendingInput, *, settlement: _Settlement | None = None,
        reason: str | None = None, actual_request_id: str | None = None,
        notification_state: Literal["delivered", "failed", "unavailable"] = "delivered",
        owner_ended: bool = False,
    ) -> RequestCompletion:
        """Classify original input evidence, also after its writer has ended."""
        uncertain = request.started and settlement is None
        unsent = False
        steer = self.steer
        if steer is not None and steer.request is request and steer.write is not None:
            write = steer.write
            if not write.done() or write.cancelled() or write.exception() is not None:
                settlement, uncertain = None, True
            elif not write.result():
                settlement, uncertain, unsent = None, False, True
                actual_request_id = request.request_id
        outcome = settlement.outcome if settlement is not None else None
        if owner_ended and settlement is not None and settlement.recording_state != "committed":
            outcome = None
        recording = settlement.recording_state if outcome is not None else "none"
        state = "completed" if outcome is not None else (
            "unavailable" if uncertain or owner_ended and not unsent else
            reason if reason in {"deleted", "shutdown", "expired"} else "withdrawn"
        )
        if outcome is not None and recording != "committed":
            state = "recording_failed"
            if reason is None and settlement is not None and settlement.event is not None:
                reason = settlement.event.get("message")
        if uncertain and notification_state == "delivered":
            notification_state = "unavailable"
        return RequestCompletion(
            input_request_id=request.request_id, request_id=actual_request_id or request.request_id,
            state=state, outcome=outcome, recording_state=recording, reason=reason,
            notification_state=notification_state,
        )

    def notification_state(self, execution: Execution) -> Literal["delivered", "failed", "unavailable"]:
        prefix = (*execution.settlement.question_prefix,
                  *self.owner.questions.capture_prefix(execution.request.request_id))
        publications = (*execution.prefix, *((execution.terminal,) if execution.terminal is not None else ()))
        if (execution.publication_failed or any(item.error is not None for item in publications)
                or any(notice.publication is not None and notice.publication.error is not None for notice in prefix)):
            return "failed"
        if execution.settlement.outcome is None:
            return "unavailable"
        if (execution.frames or any(not item.delivered for item in publications)
                or any(notice.publication is None or not notice.publication.delivered for notice in prefix)
                or execution.settlement.event is not None and execution.terminal is None):
            return "unavailable"
        return "delivered"

    def end_completions(self) -> None:
        """Manager takes over only the ended writer's original observations."""
        session, manager, execution = self.owner.session, self.owner.manager, self.current
        inputs = {request.request_id: request for request in (*session._pending_queue, *session._steer_queue)}
        if execution is not None:
            inputs[execution.request.request_id] = execution.request
            inputs.update((request.request_id, request) for request in
                          session._inline_requests.get(execution.request.request_id, ()))
        for request_id in tuple(session._request_completions):
            request = inputs.get(request_id)
            if request is None:
                completion = RequestCompletion(request_id, request_id, "unavailable",
                    reason="Session owner failed; retained input requires review", notification_state="unavailable")
            else:
                carrying = execution if execution is not None and (
                    request is execution.request or any(request is inline for inline in
                        session._inline_requests.get(execution.request.request_id, ()))
                ) else None
                completion = self.completion(request,
                    settlement=carrying.settlement if carrying is not None else None,
                    actual_request_id=carrying.request.request_id if carrying is not None else None,
                    notification_state=self.notification_state(carrying) if carrying is not None else "unavailable",
                    reason="Session owner failed; retained input requires review", owner_ended=True)
            manager._finish_receipt(session, completion)

    async def complete(
        self, request: QueuedPrompt | PendingInput, *, settlement: _Settlement | None = None,
        reason: str | None = None, actual_request_id: str | None = None,
        published: bool = True,
    ) -> None:

        session, manager = self.owner.session, self.owner.manager
        future = session._request_completions.get(request.request_id)
        if future is None or future.done():
            return
        completion = self.completion(request, settlement=settlement, reason=reason,
            actual_request_id=actual_request_id, notification_state="delivered" if published else "failed")
        if request.durable_client_input and manager.db is not None and not (
            completion.state == "completed" and completion.recording_state == "committed"
            and completion.request_id == request.request_id
        ):
            await manager.db.complete_client_submission(
                session.id, request.request_id, owner=actual_request_id or request.request_id,
                state="done" if completion.state == "completed" and completion.recording_state == "committed" else
                      "unknown" if request.started or completion.outcome is not None else "withdrawn",
                reason=reason or completion.state,
            )
            self.changed()
        manager._finish_receipt(session, completion)

    def _offer_idle_publication(self) -> bool:
        if self.owner.questions.has_waiting_notifications:
            return False
        pending = self._idle_publication
        if pending is not None:
            if self.owner.publisher.offer(pending.frame, pending.audience, marker=pending.original) is None:
                return False
            self._idle_publication = None
            if pending.reply is not None and not pending.reply.done():
                pending.reply.set_result(True)
        return True

    def _flush(self, execution: Execution | None) -> bool:
        if not self._offer_idle_publication():
            return False
        if execution is not None:
            while execution.frames:
                event = execution.frames[0]
                if event.get("terminal"):
                    prefix = (*execution.settlement.question_prefix,
                              *self.owner.questions.capture_prefix(execution.request.request_id))
                    if any(not notice.completed for notice in prefix):
                        return False
                    failed = execution.publication_failed or any(
                        notice.publication is not None and notice.publication.error is not None for notice in prefix
                    )
                    if failed:
                        execution.publication_failed = True
                        execution.frames.popleft()
                        continue
                publication = self.owner.publisher.offer(event, self.owner.session.user_id, marker=execution)
                if publication is None:
                    return False
                execution.frames.popleft()
                execution.prefix.append(publication)
                if event.get("terminal") or event.get("type") == "recording_failed":
                    execution.terminal = publication
        for request in self.owner.session._pending_queue:
            if request.queued_event is not None:
                if self.owner.publisher.offer(request.queued_event, self.owner.session.user_id) is None:
                    return False
                request.queued_event = None
        for attribute in ("status_notice", "dispatch_notice"):
            event = getattr(self, attribute)
            if event is not None:
                if self.owner.publisher.offer(event, self.owner.session.user_id) is None:
                    return False
                setattr(self, attribute, None)
        return True

    def flush_ready(self) -> None:
        self._flush(self.current)

    async def advance(self) -> None:
        """Consume a bounded amount of ready evidence before the next command."""
        session, manager = self.owner.session, self.owner.manager
        idle_prefix_ready = self._offer_idle_publication()
        for key, resource in tuple(session._run.retirements.items()):
            # Real output already handed off by the original reader precedes
            # synthetic native endings captured when that backend retires.
            while idle_prefix_ready and resource.notifications:
                publication = self.owner.publisher.offer(resource.notifications[0], resource.audience, marker=resource)
                if publication is None:
                    break
                resource.notifications.popleft()
            task = resource.task
            if task.done():
                # Idle resources have the same original owner as an active
                # attempt. Preserve a failed reference and let Manager's
                # cleanup confirm it or enter the existing fatal boundary.
                failed = task.cancelled() or task.exception() is not None
                execution = self.current
                if failed:
                    if (execution is None or execution.attempt is None
                            or execution.attempt.backend is not resource.backend):
                        task.result()
                    # The active original attempt still selects its failed
                    # terminal at the existing retirement decision below.
                elif not resource.notifications:
                    session._run.retirements.pop(key)
                    for other in manager._session_owners.values():
                        other.wake()
        if self._idle_action is not None and self._idle_action.done():
            self._idle_action.result()
            self._idle_action = None
        if self._auto_archive_operation is not None and self._auto_archive_operation.done():
            self._auto_archive_operation.result()
            self._auto_archive_operation = None
        execution = self.current
        if execution is not None and self.steer is not None and execution.phase in {"retire", "terminal"}:
            await self._steering(execution)
        publication_ready = self._flush(execution)
        if execution is None:
            if not publication_ready:
                return
            eligible = manager._eligible_request(session)
            if (eligible is None or manager._legacy_transition or manager._admission_closed
                    or session._forking or session._archive_reservation is not None or session._run.retirements
                    or self._idle_action is not None):
                return
            pending = session._pending_queue[eligible]
            request = await load_pending_input(manager, session.id, pending) if isinstance(pending, PendingInput) else pending
            # Retain the projection while loading. An awaited database read
            # must not make admitted work disappear or leave a stale quota
            # decision in force before this Owner reserves its active slot.
            if manager._admission_closed or manager._legacy_transition:
                return
            try:
                manager._check_turn_quota(session)
            except QuotaExceeded:
                return
            session._pending_queue.pop(eligible)
            settlement = _Settlement(request_id=request.request_id, work_id=request.work_id)
            execution = Execution(request, settlement, asyncio.get_running_loop().create_future(),
                                  asyncio.get_running_loop().create_future())
            self.current = execution
            session._run.request, session._run.settlement = request, settlement
            state = manager._dispatch_event(session)
            execution.frames.extend([state, {
                "type": "dequeued", "session_id": session.id, "request_id": request.request_id,
                "queue_length": len(session._pending_queue), "dispatch_state": state["dispatch_state"],
            }])
            self.owner.wake()
            return
        await self._deadline(execution)
        if execution.interrupted:
            if self.steer is not None:
                await self._steering(execution)
            await self._finish_interrupted(execution)
            return
        if not publication_ready:
            if execution.phase in {"start", "stream"}:
                await self._consume_runtime(execution, consume_events=False)
                if self.steer is not None:
                    await self._steering(execution)
            return
        if execution.phase == "claim":
            # Claim publication precedes actual execution; a subscriber can
            # pause and retain this unstarted request without self-cancellation.
            if not all(item.delivered or item.error is not None for item in execution.prefix):
                return
            from ..mcp_identity import work_is_open
            if not work_is_open(execution.request.work_id):
                await self._interrupt("user")
                return
            original = execution.request
            if (original.question_session_id and original.question_id
                    and not manager.has_pending_question(original.question_session_id, original.question_id)):
                notice = MessageContent(
                    role=MessageRole.system, type="text",
                    content=f"[agent-question-expired] Question {original.question_id} is already closed; no answer turn was started.",
                    tool_input={"request_context": {"request_id": original.request_id, "source": original.source}},
                )
                seq = await manager._persist_message(session, notice)
                execution.frames.append(manager._tag_persisted({
                    "type": "delivery_notice", "session_id": session.id,
                    "content": notice.content, "request_id": original.request_id,
                }, seq, notice))
                execution.completion_reason = "expired"
                execution.phase = "terminal"
                self.owner.wake()
                return
            execution.request.started = True
            configured = capture_configuration(session)
            request = replace(execution.request)
            execution.operation = self.task(self._prepare_input(configured, request), f"request-input-{request.request_id}")
            execution.phase = "input"
            return
        if execution.phase == "input":
            operation = execution.operation
            assert operation is not None
            if not operation.done():
                return
            try:
                prepared = operation.result()
            except Exception as error:
                await self._failed(execution, error, "initialization_error")
                return
            if prepared is None:
                await self._interrupt("user")
                return
            await self._input_ready(execution, prepared)
            return
        if execution.phase == "prepare":
            operation = execution.operation
            assert operation is not None
            if not operation.done():
                return
            try:
                prepared = execution.prepared = operation.result()
            except Exception as error:
                await self._failed(execution, error, "backend_exception")
                return
            await self._begin_attempt(execution, prepared)
            return
        if execution.phase in {"start", "stream"}:
            await self._consume_runtime(execution)
            await self._steering(execution)
            return
        if execution.phase == "retire":
            if any(not resource.task.done() for resource in session._run.retirements.values()):
                return
            if (execution.deadline is not None and execution.deadline.task is not None
                    and not execution.deadline.task.done()):
                return
            try:
                for resource in session._run.retirements.values():
                    resource.task.result()
            except Exception as error:
                await self._failed(execution, error, "backend_exception")
                return
            await self._decide(execution)
            return
        if execution.phase == "retry":
            if execution.operation is not None and not execution.operation.done():
                return
            if await self._overall_expired(execution):
                return
            assert execution.prepared is not None and execution.turn is not None
            configured = capture_configuration(execution.prepared.configured)
            configured.claude_session_id = session.claude_session_id
            execution.operation = self.task(self._prepare_retry(configured, execution.prepared, execution.turn.current_prompt),
                                            f"request-retry-{execution.request.request_id}")
            execution.phase = "prepare"
            return
        if execution.phase == "terminal":
            if not self._confirm_retired(execution):
                return
            if execution.completion_reason is not None and not execution.frames and not execution.prefix:
                await self._finish(execution)
                return
            if execution.publication_failed and not execution.frames and not execution.prefix:
                await self._finish(execution)
                return
            if execution.terminal is not None and (execution.terminal.delivered or execution.terminal.error is not None):
                await self._finish(execution)

    async def _prepare_input(self, configured: Session, request: QueuedPrompt) -> _PreparedInput | None:
        manager = self.owner.manager
        if request.durable_client_input:
            if not await claim_input(manager, configured.id, request.request_id):
                return None
        await require_retired_writers(manager, configured.id)
        files = await asyncio.to_thread(validate_message_attachments, configured.id, request.attachment_ids)
        head, clean = await fork_helpers.capture_git_anchor(configured.working_dir)
        return _PreparedInput(
            tuple(AttachmentMetadata(id=item.id, filename=item.filename, size=item.size, mime_type=item.mime_type) for item in files),
            tuple(str(item.path) for item in files), head, clean,
        )

    async def _input_ready(self, execution: Execution, prepared: _PreparedInput) -> None:
        from ..knowledge import encode, render
        from ..wiki.tasks import command_prompt, tasks

        session, manager, request = self.owner.session, self.owner.manager, execution.request
        knowledge = request.knowledge_context
        assert knowledge is not None
        mode = tasks.mode_for(session, knowledge.mode, request_id=request.request_id)
        assignment = tasks.assignments.get(session.id)
        worker = assignment and assignment.get("assigned_request_id") in {None, request.request_id}
        if mode == "maintain" and not knowledge.maintenance.may_submit and not worker:
            mode = "read"
        message = MessageContent(role=MessageRole.user, type="text", content=request.prompt,
                                 attachments=list(prepared.attachments), tool_input={"request_context": {
                                     "request_id": request.request_id, "source": request.source,
                                     "work_id": request.work_id, "knowledge": json.loads(encode(knowledge)),
                                 }})
        seq = await manager._persist_message(session, message, git_head=prepared.git_head, git_status_clean=prepared.git_clean)
        request.recorded_input = RecordedInput(seq, message, prepared.git_head, prepared.git_clean, True)
        session._run.knowledge_context, session._run.wiki_mode = knowledge, mode
        session._run.wiki_task_id = tasks.open(session, mode, knowledge.trusted_request,
                                              request_id=request.request_id, knowledge=knowledge) if mode in {"auto", "read", "maintain"} else None
        event: dict[str, Any] = {"type": "user_message", "session_id": session.id, "content": request.prompt, "tool_input": message.tool_input}
        if prepared.attachments:
            event["attachments"] = [item.model_dump() for item in prepared.attachments]
        execution.frames.append(manager._tag_persisted(event, seq, message))
        self.notify_status(SessionStatus.running)
        prompt = command_prompt(request.prompt, mode)
        if knowledge.origin.request_id != request.request_id:
            prompt = render(knowledge) + "\n\n" + prompt
        prompt = _augment_prompt_with_attachments(prompt, list(prepared.paths))
        copied: list[MessageContent] = []
        if session.fork_needs_replay and manager.db is not None:
            cutoff = session.fork_after_seq if session.fork_after_seq is not None else -1
            copied = [MessageContent(**row) for row in await manager.db.load_messages(session.id, max_seq=cutoff)]
        configured = capture_configuration(session)
        diagnostic = execution.preparation_diagnostic = PreparationDiagnostic()
        execution.operation = self.task(self._prepare_run(configured, prompt, copied, diagnostic), f"request-prepare-{request.request_id}")
        execution.phase = "prepare"
        self.owner.wake()

    async def _prepare_run(self, configured: Session, prompt: str, copied: list[MessageContent], diagnostic: PreparationDiagnostic) -> _PreparedRun:
        manager = self.owner.manager
        async with manager.configuration_lock:
            # The Owner's copy carries this request's runtime context. Read
            # its defaults together with the agent, after pending updates
            # commit, rather than mixing an earlier session copy with them.
            defaults = manager.sessions[configured.id]
            configured = replace(configured, backend=defaults.backend, credential_id=defaults.credential_id,
                                 model=defaults.model, reasoning_effort=defaults.reasoning_effort,
                                 service_tier=defaults.service_tier)
            agent = await manager._load_agent(configured)
        harness = get_harness(configured.backend)
        credential = await manager._resolve_credential(configured, agent, harness)
        diagnostic.credential = credential
        effective = effective_settings(configured, agent)
        catalog = harness.model_settings(credential)
        effort, tier, model = effective["reasoning_effort"], effective["service_tier"], effective["model"]
        if catalog.reasoning_efforts and effort and effort not in catalog.efforts_for(model):
            raise ValueError(f"Reasoning effort {effort!r} is unavailable for this model.")
        if catalog.service_tiers and tier and tier not in catalog.tiers_for(model):
            raise ValueError("Fast speed is unavailable for this model. Choose Normal speed." if tier == "fast" else f"Speed {tier!r} is unavailable for this model.")
        connectors = await manager._load_connectors(agent)
        if configured.fork_needs_replay:
            prompt = fork_helpers.wrap_for_fork_replay(prompt, copied)
        prompt = await asyncio.to_thread(spill_if_large, configured.id, prompt)
        if configured.agent_id:
            from ..agent_memory import ensure_agent_dirs
            await asyncio.to_thread(ensure_agent_dirs, configured.agent_id, configured.user_id)
        backend = manager._make_run(configured, agent, connectors)
        return _PreparedRun(configured, agent, credential, connectors, backend, prompt)

    async def _prepare_retry(self, captured: Session, prepared: _PreparedRun, prompt: str) -> _PreparedRun:
        prompt = await asyncio.to_thread(spill_if_large, captured.id, prompt)
        if captured.agent_id:
            from ..agent_memory import ensure_agent_dirs
            await asyncio.to_thread(ensure_agent_dirs, captured.agent_id, captured.user_id)
        backend = self.owner.manager._make_run(captured, prepared.agent, prepared.connectors)
        return replace(prepared, configured=captured, backend=backend, prompt=prompt)

    async def _begin_attempt(self, execution: Execution, prepared: _PreparedRun) -> None:

        if await self._overall_expired(execution):
            return
        session = self.owner.session
        # Each attempt owns its actual clock. Its predecessor has been joined
        # by the retirement phase; a cancelled clock cannot time a retry.
        execution.deadline = None
        if execution.turn is None:
            execution.settlement.started_at = time.monotonic()
            execution.turn = _Turn(
                prepared.prompt, prepared.prompt, get_harness(prepared.configured.backend),
                prepared.configured.credential_id or (prepared.agent or {}).get("credential_id"),
                session.claude_session_id, execution.settlement,
            )
        backend, reused = prepared.backend, False
        held = session._run.backend
        if held is not None:
            if (held.reusable and held.is_alive()
                    and held.spawn_signature(session.working_dir, prepared.credential)
                    == backend.spawn_signature(session.working_dir, prepared.credential)):
                backend, reused = held, True
            else:
                self.retire(held, "configuration_changed")
                execution.phase = "prepare"
                # The completed preparation remains the original result; do
                # not replace an unretired writer with its candidate.
                return
        if session._run.retirements:
            return
        attempt = execution.attempt = _Attempt(backend, prepared.prompt, reused)
        execution.settlement.attempt_id = attempt.attempt_id
        session._run.backend, session._run.held_since = backend, None
        if hasattr(backend, "bind_tool_context"):
            backend.bind_tool_context(execution.request.request_id, attempt.attempt_id)
        if hasattr(backend, "set_idle_handler"):
            backend.set_idle_handler(partial(
                self.owner.manager._handle_idle_event, session.id, backend=backend,
                request_id=execution.request.request_id, attempt_id=attempt.attempt_id,
                run_lease_id=getattr(backend, "run_lease_id", None), work_id=execution.request.work_id,
            ))
        activity = execution.activity = _Activity()
        if hasattr(backend, "bind_activity"):
            execution.activity_token = backend.bind_activity(activity.observe)
        await self.owner.questions.handle(BeginQuestionAttempt(attempt.attempt_id, attempt.watchdog))
        execution.frames.append({"type": "attempt_started", "session_id": session.id,
                                 "request_id": execution.request.request_id, "attempt_id": attempt.attempt_id})
        execution.start_ended, execution.stream_ended = False, False
        if reused and hasattr(backend, "prepare_turn_stream"):
            backend.prepare_turn_stream()
        # A new Harness stream is readable before spawn/stdin finishes. A
        # reused stream was synchronously reset above before this read begins.
        reader = execution.reader = RuntimeReader(backend, execution.request.request_id, attempt.attempt_id,
            getattr(backend, "run_lease_id", None), execution.request.work_id)
        reader.task = self.task(reader.read(backend.stream(), self.owner.wake), f"runtime-read-{attempt.attempt_id}")
        execution.operation = self.task(self._start_original(
            backend, prepared.prompt, reused, prepared.configured.working_dir,
            session.claude_session_id, prepared.credential, session._run.wiki_task_id,
        ), f"runtime-start-{attempt.attempt_id}")
        execution.phase = "start"
        self._arm_deadline(execution)

    async def _start_original(
        self, backend: HarnessRun, prompt: str, reused: bool, working_dir: str,
        resume_id: str | None, credential: HarnessCredential | None, wiki_task_id: str | None,
    ) -> None:
        if hasattr(backend, "bind_wiki_task"):
            backend.bind_wiki_task(wiki_task_id)
        if reused:
            if hasattr(backend, "write_turn_input"):
                await backend.write_turn_input(prompt)
            else:
                await backend.send_turn(prompt)
        else:
            await backend.start(prompt, working_dir, resume_id, credential=credential)

    def _arm_deadline(self, execution: Execution) -> None:
        attempt, turn = execution.attempt, execution.turn
        if attempt is None or turn is None or execution.interrupted:
            return
        deadlines = []
        if settings.turn_max_seconds > 0:
            deadlines.append(turn.started_at + settings.turn_max_seconds)
        if settings.turn_idle_timeout_seconds > 0 and not attempt.watchdog.get("waiting_questions"):
            deadlines.append(attempt.watchdog["last"] + settings.turn_idle_timeout_seconds)
        if execution.deadline is None:
            deadline = execution.deadline = RuntimeDeadline()
            deadline.task = self.task(deadline.wait(self.owner.wake), f"runtime-deadline-{attempt.attempt_id}")
        execution.deadline.when = min(deadlines) if deadlines else None
        execution.deadline.expired = False
        execution.deadline.changed.set()

    async def _deadline(self, execution: Execution | None) -> None:
        if (execution is None or execution.interrupted or execution.attempt is None or execution.turn is None
                or execution.attempt.timeout is not None):
            return
        activity = execution.activity
        if activity is not None and activity.latest > activity.consumed:
            activity.consumed = activity.latest
            execution.attempt.touch(activity.latest)
            self._arm_deadline(execution)
        if execution.deadline is None or not execution.deadline.expired:
            return
        attempt, turn = execution.attempt, execution.turn
        now = time.monotonic()
        reason = None
        if settings.turn_max_seconds > 0 and now - turn.started_at >= settings.turn_max_seconds:
            reason = ("overall", settings.turn_max_seconds)
        elif (settings.turn_idle_timeout_seconds > 0 and not attempt.watchdog.get("waiting_questions")
              and now - attempt.watchdog["last"] >= settings.turn_idle_timeout_seconds):
            reason = ("idle", settings.turn_idle_timeout_seconds)
        if reason is None:
            self._arm_deadline(execution)
            return
        attempt.watchdog["tripped"] = reason
        execution.deadline.cancel()
        self.owner.session._steer_open = False
        execution.retirement = self.retire(attempt.backend, "parent_timeout")
        if execution.operation is not None and not execution.operation.done():
            execution.operation.cancel()
        if execution.reader is not None and execution.reader.task is not None and not execution.reader.task.done():
            execution.reader.task.cancel()
        execution.start_ended = execution.stream_ended = True
        execution.phase = "retire"
        await self._end_questions(execution)

    async def _consume_runtime(self, execution: Execution, *, consume_events: bool = True) -> None:
        session, manager, attempt = self.owner.session, self.owner.manager, execution.attempt
        assert attempt is not None
        if not execution.start_ended and execution.operation is not None and execution.operation.done():
            execution.start_ended = True
            try:
                execution.operation.result()
            except HarnessPreparationError as error:
                attempt.preparation_failed = True
                manager._capture_error_text(attempt, HarnessEvent(type="error", content=str(error), error_code=error.error_code))
                execution.stream_ended = True
                if execution.reader is not None:
                    assert execution.reader.task is not None
                    execution.reader.task.cancel()
            except asyncio.CancelledError:
                if attempt.timeout is None:
                    raise
            except Exception as error:
                await self._failed(execution, error, "backend_exception")
                return
            else:
                session._steer_open = bool(attempt.backend.reusable and attempt.result is None)
                execution.phase = "stream"
                group = getattr(attempt.backend, "process_group_id", None)
                if isinstance(group, int) and manager.db is not None:
                    await manager.db.note_submission_process(session.id, execution.request.request_id, group)
        reader = execution.reader
        if (consume_events and not execution.stream_ended and reader is not None
                and (reader.ready or reader.task is not None and reader.task.done())):
            try:
                if reader.ready:
                    event = reader.take()
                else:
                    assert reader.task is not None
                    reader.task.result()
                    event = None
            except Exception as error:
                await self._failed(execution, error, "backend_exception")
                return
            if event is None:
                execution.stream_ended = True
                session._steer_open = False
                await self._end_questions(execution)
            else:
                if execution.activity_token is None:
                    attempt.touch()
                frames = await manager._handle_stream_event(session, attempt, event)
                for frame in frames:
                    frame.setdefault("request_id", execution.request.request_id)
                    frame.setdefault("work_id", execution.request.work_id)
                    frame["attempt_id"] = attempt.attempt_id
                execution.frames.extend(frames)
                self._arm_deadline(execution)
                self.owner.wake()
        if execution.stream_ended and execution.start_ended and self.steer is None:
            if execution.activity_token is not None:
                attempt.backend.unbind_activity(execution.activity_token)
                execution.activity_token = None
            if execution.deadline is not None:
                execution.deadline.cancel()
                execution.deadline = None
            session._steer_open = False
            if session._steer_queue:
                session._pending_queue.extend(session._steer_queue)
                session._pending_queue.sort(key=lambda request: request.order)
                session._steer_queue.clear()
            if manager._turn_ended_cleanly(attempt) and session._run.backend is attempt.backend:
                session._run.held_since = time.monotonic()
            else:
                execution.retirement = self.retire(attempt.backend, "parent_error" if attempt.failed else "parent_done")
            execution.phase = "retire"
            self.owner.wake()

    async def _decide(self, execution: Execution) -> None:
        session, manager, turn, attempt = self.owner.session, self.owner.manager, execution.turn, execution.attempt
        assert turn is not None and attempt is not None
        settlement = execution.settlement
        turn.produced_output |= attempt.produced_output
        for kind, count in getattr(attempt.backend, "protocol_issues", {}).items():
            settlement.protocol_issues[kind] = settlement.protocol_issues.get(kind, 0) + count
        settlement.protocol_locations.extend({**location, "attempt_id": attempt.attempt_id}
            for location in getattr(attempt.backend, "protocol_locations", [])[:max(0, 8 - len(settlement.protocol_locations))])
        if attempt.result is not None and attempt.result.cost is not None:
            settlement.cost = (settlement.cost or 0) + attempt.result.cost
        decision = await manager._after_attempt(session, turn, attempt)
        if decision.retry:
            if attempt.failed and attempt.error_text:
                execution.frames.append(await manager._recovery_frame(
                    session, code="attempt_failed", phase="diagnostic", message=attempt.error_text,
                ))
            execution.frames.extend({**event, "retrying": True} for event in decision.events)
            if decision.prompt is not None:
                turn.current_prompt = decision.prompt
            remaining = manager._remaining_turn_time(turn)
            delay = decision.delay if remaining is None else min(decision.delay, remaining)
            execution.operation = self.task(asyncio.sleep(delay), f"runtime-retry-{attempt.attempt_id}")
            execution.phase = "retry"
            self.owner.wake()
            return
        detail = decision.events[-1] if decision.events else {}
        failed = attempt.failed or attempt.timeout is not None
        reason = detail.get("message")
        raw = attempt.result.content if attempt.result else attempt.error_text
        if failed and raw:
            reason = f"{reason}\n{raw}" if reason else raw
        if failed and not reason:
            reason = safe_diagnostic(getattr(attempt.backend, "stderr_text", "")) or "Backend ended without a terminal result"
        await manager._settle_turn(session, settlement, status="failed" if failed else "success",
                                   message=reason, code=detail.get("code"), result=attempt.result,
                                   metadata=detail, provider_code=attempt.failure_code)
        execution.phase = "terminal"
        self.owner.wake()

    async def _overall_expired(self, execution: Execution) -> bool:
        turn, manager = execution.turn, self.owner.manager
        if turn is None:
            return False
        remaining = manager._remaining_turn_time(turn)
        if remaining is None or remaining > 0:
            return False
        event = await manager._surface_turn_timeout(
            self.owner.session, reason="overall", limit=settings.turn_max_seconds,
            backend=turn.harness.backend, retries=turn.settlement.recovery["idle"],
        )
        await manager._settle_turn(
            self.owner.session, execution.settlement, status="failed",
            message=event["message"], code=event["code"],
        )
        execution.phase = "terminal"
        self.owner.wake()
        return True

    async def _failed(self, execution: Execution, error: Exception, code: str) -> None:
        session = self.owner.session
        session._steer_open = False
        if execution.reader is not None and execution.reader.task is not None:
            execution.reader.task.cancel()
        if execution.deadline is not None:
            execution.deadline.cancel()
        if self.steer is not None and self.steer.execution is execution and self.steer.write is not None:
            self.steer.write.cancel()
        backend = execution.attempt.backend if execution.attempt is not None else None
        credential = execution.prepared.credential if execution.prepared is not None else None
        if credential is None and execution.preparation_diagnostic is not None:
            credential = execution.preparation_diagnostic.credential
        detail = (backend.format_exception(error) if backend is not None else
                  execution.preparation_diagnostic.format_exception(error) if execution.preparation_diagnostic is not None else
                  exception_diagnostic(error, secrets=(settings.auth_token, credential.secret if credential else "")))
        logger.error("Request failed session=%s request=%s error=%s\n%s", session.id,
                     execution.request.request_id, type(error).__name__, detail)
        if backend is not None:
            execution.retirement = self.retire(backend, "parent_error")
        await self._end_questions(execution)
        await self.owner.manager._settle_turn(
            session, execution.settlement, status="failed",
            message=diagnostic_text(str(error), secrets=(settings.auth_token, credential.secret if credential else "")),
            code=error.code if isinstance(error, AttachmentUnavailable) else code,
            rejected_input={"content": execution.request.prompt, "attachment_ids": execution.request.attachment_ids}
                if isinstance(error, AttachmentUnavailable) else None,
        )
        execution.phase = "terminal"
        self.owner.wake()

    async def _finish_interrupted(self, execution: Execution) -> None:
        if self.steer is not None and self.steer.execution is execution:
            return
        for task in (execution.operation, execution.reader.task if execution.reader is not None else None):
            if task is not None and not task.done():
                return
            if task is not None and not task.cancelled():
                try:
                    result = task.result()
                except Exception:
                    pass  # The interrupted resource remains explicitly retired below.
                else:
                    if isinstance(result, _PreparedRun):
                        execution.retirement = self.retire(result.backend, "late_preparation")
                        execution.operation = None
        if execution.attempt is not None and execution.retirement is None:
            execution.retirement = self.retire(execution.attempt.backend, "user_stop")
        if execution.retirement is not None:
            if not execution.retirement.done():
                return
            execution.retirement.result()
        if any(not resource.task.done() for resource in self.owner.session._run.retirements.values()):
            return
        if not self._confirm_retired(execution):
            return
        if execution.request.started and execution.settlement.event is not None:
            execution.phase = "terminal"
            if execution.publication_failed and not execution.frames and not execution.prefix:
                await self._finish(execution)
                return
            if execution.terminal is None:
                return
            if not (execution.terminal.delivered or execution.terminal.error is not None):
                return
        await self._finish(execution)

    def _confirm_retired(self, execution: Execution) -> bool:
        if execution.retired.done():
            return True
        if self.steer is not None and self.steer.execution is execution:
            return False
        for operation in (execution.operation, execution.reader.task if execution.reader is not None else None,
                          execution.deadline.task if execution.deadline is not None else None):
            if operation is not None and not operation.done():
                return False
        for resource in self.owner.session._run.retirements.values():
            if not resource.task.done():
                return False
            resource.task.result()
        execution.retired.set_result(None)
        if self.owner.session.status is not SessionStatus.idle:
            self.notify_status(SessionStatus.idle)
        return True

    async def _finish(self, execution: Execution) -> None:
        session, manager = self.owner.session, self.owner.manager
        assert self._confirm_retired(execution), "Execution resources must retire before completion"
        if execution.deadline is not None:
            execution.deadline.cancel()
        self._retire_request_context(execution)
        prefix = (*execution.settlement.question_prefix, *self.owner.questions.capture_prefix(execution.request.request_id))
        published = not execution.publication_failed and all(
            notice.publication is not None and notice.publication.delivered for notice in prefix
        ) and all(item.delivered for item in execution.prefix)
        # A stopped, unstarted claim remains in the original pending queue.
        if execution.request.started or not any(item.request_id == execution.request.request_id for item in session._pending_queue):
            await self.complete(execution.request, settlement=execution.settlement,
                                reason=execution.completion_reason or ("request_not_executed" if execution.settlement.outcome is None else None),
                                published=published)
        for inline in session._inline_requests.pop(execution.request.request_id, []):
            await self.complete(inline, settlement=execution.settlement,
                                actual_request_id=execution.request.request_id, published=published)
        session._run.request = None
        self.current = None
        self.notify_status(SessionStatus.idle)
        self.changed()
        if not execution.completed.done():
            execution.completed.set_result(None)
        for other in manager._session_owners.values():
            other.wake()
        if not session._dispatch_paused and not session._pending_queue and not session._admission_closed:
            # These coordinators only submit concrete owner commands and
            # perform subscriber/notifier I/O; they never write Session state.
            captured = capture_configuration(session)
            async def became_idle() -> None:
                await manager._enforce_held_cap(keep_session_id=captured.id)
                await manager._fire_session_idle_notification(captured)
                if captured.origin in manager._AUTO_ARCHIVE_ORIGINS:
                    await manager.auto_archive_scheduled_session(captured.id)
            self._idle_action = self.task(became_idle(), f"session-idle-{session.id}")

    def _retire_request_context(self, execution: Execution) -> None:
        """Release the original request's grants after its resources retire."""
        from ..wiki.tasks import tasks

        if execution.attempt is not None and execution.activity_token is not None:
            execution.attempt.backend.unbind_activity(execution.activity_token)
            execution.activity_token = None
        session = self.owner.session
        tasks.close(session._run.wiki_task_id)
        session._run.wiki_task_id = session._run.wiki_mode = session._run.knowledge_context = None

    async def _steering(self, execution: Execution) -> None:
        session, manager, attempt = self.owner.session, self.owner.manager, execution.attempt
        assert attempt is not None
        steer = self.steer
        if steer is None:
            if not session._steer_open or not session._steer_queue or not execution.start_ended:
                return
            request = session._steer_queue[0]
            prepared = self.task(self._prepare_steer(
                capture_configuration(session), replace(request), session._run.knowledge_context,
                execution.request.request_id,
            ), f"steer-input-{request.request_id}")
            self.steer = _Steer(request, execution, attempt.backend, session._run.knowledge_context, prepared)
            return
        if steer.write is None:
            if not steer.prepared.done():
                return
            try:
                merged, prepared = steer.prepared.result()
                if (not session._steer_open or self.current is not steer.execution
                        or session._run.backend is not steer.backend
                        or not any(request is steer.request for request in session._steer_queue)):
                    self.steer = None
                    return
                steer.merged = merged
                request = steer.request
                request.knowledge_context, request.trusted_request, request.wiki_mode = merged, merged.trusted_request, merged.mode
                if request.recorded_input is None:
                    from ..knowledge import encode
                    message = MessageContent(role=MessageRole.user, type="text", content=request.prompt,
                        tool_input={"request_context": {"request_id": request.request_id, "source": request.source,
                            "work_id": request.work_id, "knowledge": json.loads(encode(merged))}})
                    seq = await manager._persist_message(session, message, git_head=prepared.git_head, git_status_clean=prepared.git_clean)
                    request.recorded_input = RecordedInput(seq, message, prepared.git_head, prepared.git_clean, True)
                steer.message, steer.seq = request.recorded_input.message, request.recorded_input.seq
                # Constraints are installed before frame I/O can expose them to
                # tools. The original owner restores them if no frame is sent.
                session._run.knowledge_context = merged
                from ..wiki.tasks import tasks
                token = session._run.wiki_task_id
                if token and token in tasks.active:
                    steer.wiki_task_id = token
                    steer.previous_wiki_task = tasks.active[token]
                    steer.installed_wiki_task = replace(tasks.active[token], request=merged.trusted_request, knowledge=merged)
                    tasks.active[token] = steer.installed_wiki_task
                # The original record owns a possibly submitted frame from
                # this handoff onward. Stop cannot put it back in the queue.
                request.started = True
                if request in session._steer_queue:
                    session._steer_queue.remove(request)
                session._inline_requests.setdefault(execution.request.request_id, []).append(request)
                steer.write = self.task(self._write_steer(
                    steer.backend, request.prompt, session.id, session.user_id, request.request_id,
                    execution.request.request_id, request.durable_client_input,
                ), f"steer-write-{request.request_id}")
            except (asyncio.CancelledError, Exception) as error:
                if isinstance(error, Exception):
                    logger.error("Steer preparation failed session=%s error=%s\n%s", session.id,
                                 type(error).__name__, attempt.backend.format_exception(error))
                self.steer = None
                session._steer_open = False
            return
        if not steer.write.done():
            return
        request = steer.request
        try:
            delivered = steer.write.result()
        except (asyncio.CancelledError, Exception) as error:
            delivered = False
            logger.error("Steer delivery unconfirmed session=%s request=%s error=%s\n%s",
                         session.id, request.request_id, type(error).__name__, steer.backend.format_exception(error))
            await self.complete(request, reason="steer_delivery_unknown", actual_request_id=execution.request.request_id)
        if delivered:
            request.started = True
            if request in session._steer_queue:
                session._steer_queue.remove(request)
            assert steer.message is not None
            execution.frames.append(manager._tag_persisted({"type": "steered", "session_id": session.id,
                "content": request.prompt, "tool_input": steer.message.tool_input}, steer.seq, steer.message))
        else:
            if session._run.knowledge_context is steer.merged:
                session._run.knowledge_context = steer.previous
            from ..wiki.tasks import tasks
            if (steer.wiki_task_id is not None and steer.previous_wiki_task is not None
                    and tasks.active.get(steer.wiki_task_id) is steer.installed_wiki_task):
                tasks.active[steer.wiki_task_id] = steer.previous_wiki_task
            if not steer.write.cancelled() and steer.write.exception() is None:
                request.started = False
                await self.complete(request, reason="account_unavailable")
            if request in session._steer_queue:
                session._steer_queue.remove(request)
            session._steer_open = False
        self.steer = None
        self.owner.wake()

    async def _prepare_steer(self, configured: Session, request: QueuedPrompt, previous: Any, owner: str) -> tuple[Any, _PreparedInput]:
        from ..knowledge import AdmittedRequest, RequestRef, revise
        if request.durable_client_input:
            if not await claim_input(self.owner.manager, configured.id, request.request_id, owner=owner):
                raise ValueError("Steer input could not be claimed")
        assert request.knowledge_context is not None
        merged = revise(previous, AdmittedRequest(RequestRef(configured.id, request.request_id, configured.user_id),
                                                 request.knowledge_context, request.work_id),
                        {"prompt": request.prompt if request.trusted_request is None else request.trusted_request}, human=True)
        head, clean = await fork_helpers.capture_git_anchor(configured.working_dir)
        return merged, _PreparedInput((), (), head, clean)

    async def _write_steer(
        self, backend: HarnessRun, prompt: str, session_id: str, user_id: str | None,
        request_id: str, owner_request_id: str, durable: bool,
    ) -> bool:
        from ..mcp_identity import McpScope, scope_is_live
        database = self.owner.manager.db
        if durable and database is not None:
            group = getattr(backend, "process_group_id", None)
            if isinstance(group, int):
                await database.note_submission_process(session_id, owner_request_id, group)
            available = await database.set_steer_delivery(session_id, request_id, confirmed=False)
        else:
            available = database is None or await owner_is_available(database, user_id)
        # This check is at actual frame submission, after the final DB await.
        # It captures the original run lease; no current Session is consulted.
        if not available or not scope_is_live(McpScope(session_id, user_id=user_id,
                    run_lease_id=backend.run_lease_id, work_id=backend.work_id)):
            return False
        await backend.send_user_frame(prompt)
        if durable and database is not None:
            await database.set_steer_delivery(session_id, request_id, confirmed=True)
        return True

    async def _idle_event(self, command: IdleEvent) -> bool | IdlePublication:
        from ..mcp_identity import McpScope, scope_is_live
        session, manager, backend, event = self.owner.session, self.owner.manager, command.backend, command.event
        if session._run.backend is not backend or not scope_is_live(McpScope(
            session.id, user_id=session.user_id, run_lease_id=command.run_lease_id, work_id=command.work_id,
        )):
            return False
        if event.type == "text_delta":
            return True
        if event.type == "session_started" and event.session_id:
            await manager._remember_resume_id(session, event.session_id)
            return True
        if event.type == "subagent" and event.subagent is not None:
            manager._record_subagent(session, event.subagent)
        message = manager._event_to_message_content(event)
        frame = manager._event_to_ws_message(session.id, event)
        if message is not None:
            seq = await manager._persist_message(session, message)
            if frame is not None:
                manager._tag_persisted(frame, seq, message)
        if frame is not None:
            frame["work_id"] = command.work_id
            frame["request_id"] = command.request_id
            frame["attempt_id"] = command.attempt_id
            pending = IdlePublication(command, frame, session.user_id)
            assert self._idle_publication is None, "The original stdout reader already has a pending frame"
            self._idle_publication = pending
            return pending
        return True

    def end_reports(self, error: BaseException) -> None:
        pending = self._idle_publication
        if pending is not None and pending.reply is not None and not pending.reply.done():
            pending.reply.set_exception(error)

    def published(self, publication: Publication) -> None:
        execution = publication.marker
        if isinstance(execution, Execution):
            if publication.error is not None:
                execution.publication_failed = True
            execution.prefix[:] = [item for item in execution.prefix if item is not publication]
        self.owner.wake()

    async def cleanup(self) -> None:
        """Manager invokes this only after the original owner has ended."""
        execution = self.current
        operations: set[asyncio.Task[Any]] = set()
        if execution is not None:
            if execution.operation is not None:
                operations.add(execution.operation)
                if execution.phase not in {"input", "prepare"} and not execution.operation.done():
                    execution.operation.cancel()
            for task in (execution.reader.task if execution.reader is not None else None,
                         execution.deadline.task if execution.deadline is not None else None):
                if task is not None:
                    operations.add(task)
                    if not task.done():
                        task.cancel()
        if self.steer is not None:
            operations.add(self.steer.prepared)
            if self.steer.write is not None:
                operations.add(self.steer.write)
                if not self.steer.write.done():
                    self.steer.write.cancel()
        if self._idle_action is not None:
            operations.add(self._idle_action)
            self._idle_action.cancel()
        # The lifecycle command can itself be waiting for these resources.
        # Its database effect is joined by Manager's full cleanup, afterwards.
        if operations:
            results = await asyncio.gather(*operations, return_exceptions=True)
            for result in results:
                if isinstance(result, _PreparedRun):
                    await result.backend.stop()
        backend = self.owner.session._run.backend
        if backend is not None:
            await backend.stop()
        for resource in tuple(self.owner.session._run.retirements.values()):
            await asyncio.shield(resource.task)
        if execution is not None:
            self._retire_request_context(execution)
            if not execution.retired.done():
                execution.retired.set_result(None)
            if not execution.completed.done():
                execution.completed.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                execution.completed.set_exception(SessionOwnerUnavailable("Session owner failed"))

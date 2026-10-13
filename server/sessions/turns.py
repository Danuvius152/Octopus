"""Driving one turn: queueing, dispatch, the backend run loop, steering, interrupt.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from .. import fork_helpers
from .. import monitor as _monitor
from ..aio import drain_cancelled
from ..attachments import MAX_ATTACHMENTS_PER_MESSAGE
from ..attachments import get_path as get_attachment_path
from ..config import settings
from ..harness import (
    Harness,
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    RunConfig,
    get_harness,
)
from ..large_prompts import spill_if_large
from ..models import (
    AttachmentMetadata,
    MessageContent,
    MessageRole,
    SessionStatus,
    TurnOutcome,
    TurnRecovery,
)
from ..monitor import Event as _MonEvent
from .base import (
    _BACKEND_RETIRE_TIMEOUT,
    _DELTA_FLUSH_SECONDS,
    _MAX_PENDING_STEERS,
    PendingQuestion,
    QueuedPrompt,
    Session,
    SessionManagerBase,
    _augment_prompt_with_attachments,
    _guess_mime,
    _split_tool_list,
    logger,
)
from .diagnostics import safe_diagnostic
from .submission import RequestCompletion, SubmissionReceipt


@dataclass
class _Settlement:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    work_id: str | None = None
    recovery: dict[str, int] = field(default_factory=lambda: {
        "transient": 0, "idle": 0, "stale_session": 0, "premature_exit": 0,
    })
    final_task: asyncio.Task[dict[str, Any]] | None = None
    cost: float | None = None
    outcome: TurnOutcome | None = None
    recording_state: Literal["selected", "writing", "committed", "failed", "unconfirmed"] = "selected"
    stored: MessageContent | None = None
    seq: int | None = None
    event: dict[str, Any] | None = None
    publication_started: bool = False


@dataclass
class _Attempt:
    """One CLI invocation inside a logical turn.

    These fields were local variables in a 473-line `_run_backend`, and
    the fact that eight of its branches read and wrote them is precisely why
    that method could not be split: an event handler needs the same mutable
    view of "what this invocation has seen so far" as the failure classifiers
    that read it afterwards. Naming that view is what makes both extractable
    (docs/plans/polish-2026-09.md §3 A1.5).
    """

    backend: HarnessRun
    # The prompt THIS invocation ran — the original on the first pass, and
    # whatever the retry decided on later ones.
    prompt: str
    # True when the process was already holding this conversation, so the
    # prompt goes in as the next message rather than a fresh invocation.
    reused: bool
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    saw_result: bool = False
    result: HarnessEvent | None = None
    saw_tool_use: bool = False
    # Whether any assistant text streamed, including unfinished delta blocks.
    # Once output exists, recovery requires the captured conversation.
    saw_text: bool = False
    # Error notifications are diagnostic until the final result is known.
    saw_error_event: bool = False
    error_code: str | None = None
    error_text: str = ""
    # Streaming-text coalescing (inline-steering.md §4 S1). The CLI emits one
    # delta per token; forwarding each as its own WS frame would be a frame
    # per token and a React render per token. We batch them into at most one
    # frame every _DELTA_FLUSH_SECONDS, which is still far below the threshold
    # where a human sees stepping, and flush before any non-delta event so
    # ordering with tool calls and the final block is exact.
    delta_buf: list[str] = field(default_factory=list)
    delta_last_flush: float = 0.0
    # Per-turn watchdog state (turn-safety.md §3): the watchdog stops a turn
    # that goes silent (idle) or runs too long (overall) so it can never hang
    # forever the way the deep-research wedge did.
    watchdog: dict[str, Any] = field(
        default_factory=lambda: {"last": time.monotonic(), "tripped": None}
    )

    def touch(self) -> None:
        """Mark progress, so the idle watchdog doesn't trip on a live turn."""
        self.watchdog["last"] = time.monotonic()

    @property
    def timeout(self) -> tuple[str, int] | None:
        """`(reason, limit)` when the watchdog stopped this attempt."""
        return self.watchdog["tripped"]

    @property
    def failed(self) -> bool:
        """A turn that errored, or ended without ever saying it was done."""
        return self.result is None or self.result.is_error

    @property
    def failure_code(self) -> str | None:
        return (self.result.error_code if self.result else None) or self.error_code

    @property
    def produced_output(self) -> bool:
        return self.saw_tool_use or self.saw_text


@dataclass
class _Turn:
    """What survives across the CLI invocations of one logical turn.

    The retry budgets are per turn, not per attempt — that is the whole point
    of them — and so is the resume id the turn started from.
    """

    # The prompt the user (or the injector) actually sent. A transient retry
    # with no output yet re-runs THIS, not whatever a later attempt was given.
    prompt: str
    # The prompt the next attempt will run: `prompt`, "continue", or a
    # history-replay wrapper.
    current_prompt: str
    harness: Harness
    # The effective credential id (session override, else the agent's). Used
    # to flag the right row needs_reconnect on a mid-turn 401
    # (harness-credential-reauth.md §4). None = host-default CLI auth.
    cred_id: str | None
    # The resume id this logical turn STARTED from. A transient retry re-runs
    # the original invocation, so it must restore this — a failed no-output
    # attempt can still emit `session_started` and mutate
    # session.claude_session_id, which would otherwise turn the retry into a
    # `--resume <failed-id>` of the same prompt (Vera review,
    # harness-transient-retry.md §4).
    resume_at_turn_start: str | None
    settlement: _Settlement = field(default_factory=_Settlement)
    recovery_attempts: int = 0
    transient_attempts: int = 0
    # A dangling resume id is recoverable exactly once per turn: clear it,
    # start fresh. A second failure is a different problem.
    stale_session_retries: int = 0
    idle_timeout_retries: int = 0
    started_at: float = field(default_factory=time.monotonic)
    produced_output: bool = False


@dataclass
class _Decision:
    """What one finished CLI invocation means: stop, or run it again.

    `events` are the frames the user sees before either happens — the
    explanation for whichever it is. Returning this rather than yielding in
    place is what lets each classifier be an ordinary method instead of a
    branch inside an async generator.
    """

    retry: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    # The prompt the retry runs; None keeps whatever the turn already had.
    prompt: str | None = None
    delay: float = 0.0

    @classmethod
    def stop(cls, *events: dict[str, Any]) -> _Decision:
        return cls(retry=False, events=list(events))

    @classmethod
    def again(
        cls, prompt: str, *events: dict[str, Any], delay: float = 0.0
    ) -> _Decision:
        return cls(retry=True, events=list(events), prompt=prompt, delay=delay)


class QuotaExceeded(Exception):
    """Too many turns at once for one account (multi-tenancy.md §8).

    Its own type rather than a ValueError because the router answers it with
    429 — "try again shortly" — and that is a different thing from the 400 a
    malformed request gets.
    """


class TurnsMixin(SessionManagerBase):

    def dispatch_snapshot(self, session_id: str) -> dict[str, Any]:
        return self._session_dispatch_snapshot(self.sessions[session_id])

    def _session_dispatch_snapshot(self, session: Session) -> dict[str, Any]:
        return {
            "generation": self._dispatch_generation,
            "revision": session._dispatch_revision,
            "paused": session._dispatch_paused,
            "recording_failure": ({"request_id": session._recording_blocked.request_id,
                                   "state": session._recording_blocked.recording_state}
                                  if session._recording_blocked else None),
            "active_request_id": (
                session._current_request.request_id if session._current_request else None
            ),
            "items": [{
                "request_id": item.request_id, "source": item.source,
                "summary": item.prompt[:1000], "priority": item.priority,
            } for item in session._pending_queue],
        }

    def _dispatch_event(self, session: Session) -> dict[str, Any]:
        session._dispatch_revision += 1
        return {
            "type": "dispatch_state", "session_id": session.id,
            "dispatch_state": self._session_dispatch_snapshot(session),
        }

    async def _settle_turn(
        self, session: Session, settlement: _Settlement, *,
        status: Literal["success", "failed", "interrupted"],
        message: str | None = None, code: str | None = None,
        result: HarnessEvent | None = None, metadata: dict[str, Any] | None = None,
        provider_code: str | None = None,
    ) -> dict[str, Any]:
        """Reserve, persist and broadcast one final outcome, even during Stop.

        Reservation happens before the first await. Shielding the publication
        lets the cancelled producer finish its record and delivery; a racing
        interrupt observes that same task instead of publishing a second result.
        """
        if settlement.final_task is None:
            outcome = TurnOutcome(
                request_id=settlement.request_id, status=status,
                message=safe_diagnostic(message), code=code,
                provider_code=getattr(result, "error_code", None) or provider_code,
                recovery=dict(settlement.recovery),
            )

            settlement.outcome = outcome
            settlement.stored = MessageContent(
                role=MessageRole.system, type="result", content=outcome.model_dump(),
                is_error=status != "success",
                session_id=result.session_id if result else session.claude_session_id,
                cost=settlement.cost, created_at=datetime.now(UTC).isoformat(),
            )
            if self.db:
                settlement.seq = session._message_count
                session._message_count += 1
            event = {
                "type": "result", "session_id": session.id,
                "request_id": settlement.request_id, "work_id": settlement.work_id,
                "claude_session_id": settlement.stored.session_id,
                "content": outcome.model_dump(), "is_error": status != "success",
                "cost": settlement.cost, "code": code, "message": outcome.message,
                "terminal": True, "turns": result.num_turns if result else None,
                "duration_ms": result.duration_ms if result else None,
            }
            if metadata:
                for key in ("credential_id", "backend"):
                    if key in metadata:
                        event[key] = metadata[key]
            settlement.event = self._tag_persisted(event, settlement.seq, settlement.stored)
            task = asyncio.create_task(self._record_and_publish(session, settlement))
            settlement.final_task = task
            self._finalization_tasks.add(task)
            task.add_done_callback(self._finalization_tasks.discard)
            self._own_task(task)
        return await asyncio.shield(settlement.final_task)

    async def _record_and_publish(self, session: Session, settlement: _Settlement) -> dict[str, Any]:
        assert settlement.stored is not None and settlement.event is not None
        if settlement.recording_state != "committed":
            settlement.recording_state = "writing"
            stored = settlement.stored
            try:
                if self.db:
                    await self.db.record_terminal(
                        request_id=settlement.request_id, session_id=session.id,
                        seq=settlement.seq, role=stored.role.value, type=stored.type,
                        content=stored.content, is_error=stored.is_error,
                        session_id_ref=stored.session_id, cost=stored.cost,
                        created_at=stored.created_at,
                    )
            except Exception:
                # A commit acknowledgement may be lost after the write succeeds.
                # Only storage reconciliation can prove which happened.
                settlement.recording_state = "unconfirmed"
                session._recording_blocked = settlement
                logger.exception("Terminal recording unconfirmed session=%s request=%s",
                                 session.id, settlement.request_id)
                notice = {"type": "recording_failed", "session_id": session.id,
                          "request_id": settlement.request_id,
                          "recording_state": "unconfirmed", "terminal": False,
                          "message": "Result saving failed or is unconfirmed. Automatic dispatch is paused.",
                          "outcome": settlement.outcome.model_dump() if settlement.outcome else None}
                notice["dispatch_state"] = self._dispatch_event(session)["dispatch_state"]
                try:
                    await self._broadcast(notice, user_id=session.user_id)
                except Exception:
                    logger.exception("Recording notice delivery failed session=%s", session.id)
                return notice
            settlement.recording_state = "committed"
            if session._recording_blocked is settlement:
                session._recording_blocked = None
        if not settlement.publication_started:
            settlement.publication_started = True
            try:
                await self._broadcast(settlement.event, user_id=session.user_id)
            except Exception:
                logger.exception("Committed terminal notification failed session=%s request=%s",
                                 session.id, settlement.request_id)
        return settlement.event

    async def _reconcile_recording(self, session: Session) -> bool:
        settlement = session._recording_blocked
        if settlement is None:
            return True
        if settlement.final_task is not None and not settlement.final_task.done():
            await asyncio.shield(settlement.final_task)
        task = asyncio.create_task(self._record_and_publish(session, settlement))
        self._finalization_tasks.add(task)
        task.add_done_callback(self._finalization_tasks.discard)
        self._own_task(task)
        await asyncio.shield(task)
        return session._recording_blocked is None

    async def _recovery_frame(
        self, session: Session, *, code: str, message: str,
        phase: Literal["diagnostic", "retrying", "recovered"] = "retrying",
        attempt: int | None = None, maximum: int | None = None,
        delay: float | None = None,
    ) -> dict[str, Any]:
        settlement = session._settlement
        payload = TurnRecovery(
            request_id=settlement.request_id if settlement else "untracked",
            phase=phase, code=code, message=safe_diagnostic(message) or "Backend notification",
            attempt=attempt, maximum=maximum, delay=delay,
        )
        stored = MessageContent(role=MessageRole.system, type="recovery", content=payload.model_dump())
        seq = await self._persist_message(session, stored)
        return self._tag_persisted(
            {"type": "recovery", "session_id": session.id,
             "request_id": payload.request_id, "content": stored.content,
             "message": payload.message, "code": code, "terminal": False,
             "retrying": phase == "retrying"}, seq, stored,
        )

    @staticmethod
    def _tag_persisted(
        event: dict[str, Any], seq: int | None, msg: MessageContent
    ) -> dict[str, Any]:
        """Say which stored row a WS event is.

        `seq` is what a reconnecting client dedupes against its snapshot;
        `created_at` is what lets it show the message's time straight away
        instead of only after a refetch. Both come from the same persist, so
        they are attached in the same place.
        """
        if seq is not None:
            event["seq"] = seq
        if msg.created_at:
            event["created_at"] = msg.created_at
        if msg.runtime_metadata:
            event["runtime_metadata"] = msg.runtime_metadata.model_dump()
        return event

    async def _persist_message(
        self,
        session: Session,
        msg: MessageContent,
        *,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
    ) -> int | None:
        """Persist and return the assigned seq (or None if no DB).

        Callers tag broadcast/yield events with this seq so clients can
        dedupe against the snapshot returned by GET /api/sessions/{id}
        after a reconnect.

        `git_head` / `git_status_clean` are the turn-start anchor captured for
        user-message rows (session-rewind.md §5.6.3); None elsewhere.
        """
        if not self.db:
            return None
        seq = session._message_count
        session._message_count += 1
        # Decided here rather than in the DB layer so the caller's own
        # MessageContent carries it onto the WS event as well as into the row.
        msg.created_at = msg.created_at or datetime.now(UTC).isoformat()
        await self.db.append_message(
            session_id=session.id,
            seq=seq,
            role=msg.role.value,
            type=msg.type,
            content=msg.content,
            tool_name=msg.tool_name,
            tool_input=msg.tool_input,
            tool_use_id=msg.tool_use_id,
            is_error=msg.is_error,
            session_id_ref=msg.session_id,
            cost=msg.cost,
            attachments=[a.model_dump() for a in msg.attachments] if msg.attachments else None,
            git_head=git_head,
            git_status_clean=git_status_clean,
            created_at=msg.created_at,
            runtime_metadata=msg.runtime_metadata.model_dump() if msg.runtime_metadata else None,
        )
        return seq

    def _own_task(self, task: asyncio.Task[Any]) -> None:
        self._owned_tasks.add(task)

        def observed(done: asyncio.Task[Any]) -> None:
            self._owned_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                assert error is not None
                logger.error("Session-owned task %s failed", done.get_name(),
                             exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(observed)

    def _notify_admission(self, session: Session, events: list[dict[str, Any]]) -> None:
        async def publish() -> None:
            for event in events:
                await self._broadcast(event, user_id=session.user_id)

        task = asyncio.create_task(publish(), name=f"admission-notice-{session.id}")
        self._own_task(task)

    @staticmethod
    def _new_receipt(session: Session, queued: QueuedPrompt, disposition: str = "accepted") -> SubmissionReceipt:
        future: asyncio.Future[RequestCompletion] = asyncio.get_running_loop().create_future()
        session._request_completions[queued.request_id] = future
        return SubmissionReceipt(disposition, queued.request_id, future)  # type: ignore[arg-type]

    def _complete_request(
        self, session: Session, queued: QueuedPrompt, *, reason: str | None = None,
        state: str = "completed", settlement: _Settlement | None = None,
        actual_request_id: str | None = None,
    ) -> None:
        future = session._request_completions.pop(queued.request_id, None)
        if future is None or future.done():
            return
        outcome = settlement.outcome if settlement else None
        recording = settlement.recording_state if settlement and outcome else "none"
        if outcome is not None and recording != "committed":
            state = "recording_failed"
            recording = "unconfirmed" if recording == "unconfirmed" else "failed"
        future.set_result(RequestCompletion(
            input_request_id=queued.request_id,
            request_id=actual_request_id or queued.request_id,
            state=state, outcome=outcome, recording_state=recording, reason=reason,  # type: ignore[arg-type]
        ))

    def _withdraw_requests(self, session: Session, requests: list[QueuedPrompt], *, reason: str) -> None:
        for request in requests:
            self._complete_request(session, request, state=reason if reason in {"deleted", "shutdown", "expired"} else "withdrawn", reason=reason)

    def _retire_backend(self, session: Session, backend: HarnessRun) -> asyncio.Task[None]:
        captured = session._backend_retirements.get(id(backend))
        if captured is not None and captured[0] is backend:
            return captured[1]
        self._forget_backend(session, backend)

        async def retire() -> None:
            try:
                await backend.interrupt()
            except Exception:
                logger.warning("Backend interrupt failed; confirming retirement with stop", exc_info=True)
            await backend.stop()

        task = asyncio.create_task(retire(), name=f"backend-retirement-{session.id}")
        session._backend_retirements[id(backend)] = (backend, task)
        session._backend_cleanup_tasks.add(task)
        self._own_task(task)
        # Failed retirement stays attached: a subsequent attempt must not start
        # against a writer whose exit could not be confirmed.
        def finished(done: asyncio.Task[None]) -> None:
            if not done.cancelled() and done.exception() is None:
                session._backend_cleanup_tasks.discard(done)
                captured = session._backend_retirements.get(id(backend))
                if captured is not None and captured[1] is done:
                    session._backend_retirements.pop(id(backend))
        task.add_done_callback(finished)
        return task

    async def _await_backend_retirement(self, session: Session, *, timeout: float | None = None) -> None:
        tasks = tuple(session._backend_cleanup_tasks)
        if not tasks:
            return
        limit = _BACKEND_RETIRE_TIMEOUT if timeout is None else min(_BACKEND_RETIRE_TIMEOUT, max(0.0, timeout))
        _, pending = await asyncio.wait(tasks, timeout=limit)
        if pending:
            raise RuntimeError("Previous backend retirement remains unconfirmed")
        for task in tasks:
            await asyncio.shield(task)

    async def drain_turns(self, timeout: float = 5.0) -> int:
        """Close admission before draining execution and manager-owned cleanup."""
        self._admission_closed = True
        deadline = time.monotonic() + max(0.0, timeout)
        tasks: list[asyncio.Task[Any]] = []
        drivers: list[asyncio.Task[Any]] = []
        questions: list[asyncio.Task[None]] = []
        for session in list(self.sessions.values()):
            self._withdraw_requests(session, [*session._pending_queue, *session._steer_queue], reason="shutdown")
            session._pending_queue.clear()
            session._steer_queue.clear()
            session._steer_open = False
            if session._backend is not None:
                self._retire_backend(session, session._backend)
            if session._active_task is not None and not session._active_task.done():
                if not session._active_task.cancelling():
                    session._active_task.cancel()
                tasks.append(session._active_task)
                drivers.append(session._active_task)
        for session in list(self.sessions.values()):
            question_cleanup = asyncio.create_task(
                self._close_session_questions(session, reason="shutdown"),
                name=f"shutdown-questions-{session.id}",
            )
            self._own_task(question_cleanup)
            tasks.append(question_cleanup)
            questions.append(question_cleanup)
        # A cancelling driver may create its final recorder while it unwinds.
        # Refresh the owned set until all producers retire within one budget.
        while True:
            owned = {*tasks, *self._owned_tasks, *self._finalization_tasks}
            pending = {task for task in owned if not task.done()}
            if not pending:
                break
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                logger.error("Shutdown teardown remains unconfirmed: %d task(s)", len(pending))
                raise RuntimeError("Session teardown remains unconfirmed")
            await asyncio.wait(pending, timeout=remaining)
        for task in questions:
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception) as error:
                raise RuntimeError("Question teardown remains unconfirmed") from error
        for session in list(self.sessions.values()):
            await self._await_backend_retirement(session, timeout=0)
            if session._recording_blocked is not None:
                logger.error("Shutdown with unconfirmed result session=%s request=%s",
                             session.id, session._recording_blocked.request_id)
        return sum(task.done() for task in drivers)

    def running_turns_for(self, user_id: str | None) -> int:
        """How many of this account's sessions are mid-turn."""
        return sum(
            1
            for s in self.sessions.values()
            if s.user_id == user_id and s._active_task and not s._active_task.done()
        )

    def _check_turn_quota(self, session: Session) -> None:
        """Refuse a turn that would put an account over its cap (§8).

        Checked before the busy/idle split rather than after, so it applies to
        the message that *starts* a turn and not to one queued behind a turn
        that is already running — a queue is one CLI process however long it
        gets, and refusing to queue would make the cap feel like data loss.

        A single-user install is unaffected: `user_id` is None there, and a cap
        on "everybody's sessions" is not what this is for.
        """
        cap = settings.max_concurrent_turns_per_user
        if cap <= 0 or session.user_id is None:
            return
        if session._active_task and not session._active_task.done():
            return  # this one is already counted; the message will queue
        if self.running_turns_for(session.user_id) >= cap:
            raise QuotaExceeded(
                f"You already have {cap} turns running. Wait for one to finish, "
                "or interrupt it."
            )

    @staticmethod
    def _eligible_request(session: Session) -> int | None:
        if session._admission_closed or session._recording_blocked is not None:
            return None
        return next((
            i for i, item in enumerate(session._pending_queue)
            if not session._dispatch_paused or item.priority
        ), None)

    def _schedule_dispatch(self, session: Session) -> bool:
        """Claim the driver synchronously; its short lock never spans I/O."""
        if self._admission_closed or session._admission_closed:
            return False
        if session._active_task and not session._active_task.done():
            return False
        if self._eligible_request(session) is None:
            return False
        self._check_turn_quota(session)
        session._active_task = asyncio.create_task(self._drive_messages(session.id))
        return True

    async def start_message(
        self,
        session_id: str,
        prompt: str,
        attachment_ids: list[str] | None = None,
        *,
        steerable: bool = False,
        wiki_mode: str | None = None,
        source: str = "human",
        delivery_id: str | None = None,
        work_id: str | None = None,
        question_session_id: str | None = None,
        question_id: str | None = None,
    ) -> SubmissionReceipt:
        """Accept a request through the in-memory dispatch gate.

        Only the composer is steerable. Internal callers supply their source
        and delivery identity; markers remain part of the model's transcript.
        Stop preserves older work; humans accepted afterwards have priority
        until explicit resume, without reopening automatic dispatch.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        from ..mcp_identity import work_is_open
        if attachment_ids and len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(f"too many attachments: max {MAX_ATTACHMENTS_PER_MESSAGE}")
        if delivery_id and delivery_id in session._delivery_receipts:
            return replace(session._delivery_receipts[delivery_id], disposition="duplicate")
        if self._admission_closed or session._admission_closed or not work_is_open(work_id):
            queued = QueuedPrompt(prompt="", attachment_ids=[])
            receipt = self._new_receipt(session, queued, "closed")
            self._withdraw_requests(session, [queued], reason="shutdown" if self._admission_closed else "withdrawn")
            return receipt
        queued = QueuedPrompt(
            prompt=prompt, attachment_ids=list(attachment_ids or []),
            wiki_mode=wiki_mode, source=source, delivery_id=delivery_id, work_id=work_id,
            question_session_id=question_session_id, question_id=question_id,
        )
        # Stop closes the steering window synchronously before yielding. A
        # correction may steer another correction, but never the retired turn.
        if (
            source == "human" and steerable
            and session._active_task and not session._active_task.done()
            and (not session._dispatch_paused or (
                session._current_request is not None and session._current_request.priority
            ))
            and session._recording_blocked is None
            and await self._try_steer(session, queued)
        ):
            return self._new_receipt(session, queued, "steered")
        async with session._dispatch_lock:
            if self.sessions.get(session_id) is not session:
                raise ValueError(f"Session {session_id} not found")
            if delivery_id and delivery_id in session._delivery_receipts:
                return replace(session._delivery_receipts[delivery_id], disposition="duplicate")
            if self._admission_closed or session._admission_closed or not work_is_open(work_id):
                receipt = self._new_receipt(session, queued, "closed")
                self._withdraw_requests(session, [queued], reason="shutdown" if self._admission_closed else "withdrawn")
                return receipt
            if session._forking:
                raise ValueError(f"Session {session_id} is busy (forking)")
            queued.priority = session._dispatch_paused and source == "human"
            if source == "human" and session._recording_blocked is None:
                self._check_turn_quota(session)
            session._dispatch_order += 1
            queued.order = session._dispatch_order
            session._pending_queue.append(queued)
            receipt = self._new_receipt(session, queued)
            if delivery_id:
                session._delivery_receipts[delivery_id] = receipt
            try:
                launched = self._schedule_dispatch(session)
            except QuotaExceeded:
                # An automatic return has already been accepted. Another
                # driver's release wakes this queue when capacity is free.
                launched = False
            event = self._dispatch_event(session)
            queue_length = len(session._pending_queue)
        events = [event]
        if not launched:
            events.append({
                "type": "queued", "session_id": session_id,
                "content": prompt, "request_id": queued.request_id,
                "queue_length": queue_length, "dispatch_state": event["dispatch_state"],
            })
        self._notify_admission(session, events)
        return receipt

    async def _run_claim(
        self, session_id: str, queued: QueuedPrompt, gate: asyncio.Event
    ) -> None:
        await gate.wait()
        from ..mcp_identity import work_is_open
        session = self.sessions.get(session_id)
        if session is None:
            return
        if not work_is_open(queued.work_id):
            self._withdraw_requests(session, [queued], reason="withdrawn")
            return
        queued.started = True
        settlement = _Settlement(request_id=queued.request_id, work_id=queued.work_id)
        session._settlement = settlement
        try:
            await self._consume_message(session_id, queued)
        finally:
            state = "completed" if settlement.outcome is not None else "withdrawn"
            self._complete_request(session, queued, state=state, settlement=settlement,
                                   reason="request_not_executed" if settlement.outcome is None else None)
            for inline in session._inline_requests.pop(queued.request_id, []):
                self._complete_request(session, inline, state=state, settlement=settlement, actual_request_id=queued.request_id)

    async def _drive_messages(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        driver = asyncio.current_task()
        try:
            while True:
                async with session._dispatch_lock:
                    eligible = self._eligible_request(session)
                    if eligible is None:
                        if session._active_task is driver:
                            session._active_task = None
                        break
                    current = session._pending_queue.pop(eligible)
                    gate = asyncio.Event()
                    inner = asyncio.create_task(self._run_claim(session_id, current, gate))
                    session._current_request = current
                    session._inner_task = inner
                    claimed_event = self._dispatch_event(session)
                try:
                    # A claim is visible before it starts. Stop can cancel
                    # the registered inner task and restore an unstarted item.
                    await self._broadcast(claimed_event, user_id=session.user_id)
                    await self._broadcast({
                        "type": "dequeued", "session_id": session_id,
                        "request_id": current.request_id,
                        "queue_length": len(claimed_event["dispatch_state"]["items"]),
                        "dispatch_state": claimed_event["dispatch_state"],
                    }, user_id=session.user_id)
                    gate.set()
                    await inner
                except asyncio.CancelledError:
                    if driver is not None and driver.cancelling():
                        inner.cancel()
                        await drain_cancelled(inner, f"session {session_id} inner turn")
                        if not current.started:
                            self._withdraw_requests(session, [current], reason="shutdown")
                        raise
                except Exception:
                    logger.exception("Background task error for session %s", session_id)
                finally:
                    async with session._dispatch_lock:
                        if session._inner_task is inner:
                            session._inner_task = None
                        if session._current_request is current:
                            session._current_request = None
                        finished_event = self._dispatch_event(session)
                    if self.sessions.get(session_id) is session:
                        await self._broadcast(finished_event, user_id=session.user_id)
        finally:
            if session._active_task is driver:
                session._active_task = None
        # A paused session has stopped executing, but its pending work is not
        # finished. Completion notifications and transient archival must wait.
        if (not session._dispatch_paused and session._recording_blocked is None
                and not session._pending_queue and not session._active_task):
            await self._fire_session_idle_notification(session)
            if session.origin in self._AUTO_ARCHIVE_ORIGINS:
                await self.auto_archive_scheduled_session(session_id)
        for waiting in list(self.sessions.values()):
            if waiting is session:
                continue
            async with waiting._dispatch_lock:
                try:
                    self._schedule_dispatch(waiting)
                except QuotaExceeded:
                    pass

    async def resume_dispatch(self, session_id: str, *, resume_queue: bool = True) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        if session._recording_blocked is not None and not await self._reconcile_recording(session):
            await self._broadcast(self._dispatch_event(session), user_id=session.user_id)
            return False
        async with session._dispatch_lock:
            if self.sessions.get(session_id) is not session:
                return False
            if session._pending_queue:
                self._check_turn_quota(session)
            if resume_queue:
                session._dispatch_paused = False
            self._schedule_dispatch(session)
            event = self._dispatch_event(session)
        await self._broadcast(event, user_id=session.user_id)
        return True

    def has_pending_or_active_work(self, session_id: str, work_id: str) -> bool:
        """Whether dispatch accepted this work before its caller stopped waiting."""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        current = session._current_request
        return (current is not None and current.work_id == work_id) or any(
            request.work_id == work_id for request in (*session._pending_queue, *session._steer_queue)
        )

    def close_work(self, work_id: str) -> list[tuple[Session, _Settlement | None, str | None]]:
        """Close one owner before yielding, retaining independent requests.

        The captured task/process references are retired here; cleanup never
        looks up a replacement process that could belong to a newer request.
        """
        from ..mcp_identity import close_work

        close_work(work_id)
        affected = []
        for session in self.sessions.values():
            pending = len(session._pending_queue)
            removed = [q for q in (*session._pending_queue, *session._steer_queue) if q.work_id == work_id]
            self._withdraw_requests(session, removed, reason="withdrawn")
            session._pending_queue[:] = [q for q in session._pending_queue if q.work_id != work_id]
            session._steer_queue[:] = [q for q in session._steer_queue if q.work_id != work_id]
            current = session._current_request
            settlement = None
            question_attempt_id = None
            owned_current = current is not None and current.work_id == work_id
            if owned_current:
                settlement = session._settlement
                if settlement is not None and settlement.request_id != current.request_id:
                    settlement = None
                question_attempt_id = session._question_attempt_id
                inner = session._inner_task
                if current is not None and not current.started:
                    self._withdraw_requests(session, [current], reason="withdrawn")
                if inner is not None and not inner.done():
                    inner.cancel()
                session._steer_open = False
            if session._work_id == work_id and session._backend is not None:
                backend = session._backend
                self._retire_backend(session, backend)
            if owned_current or pending != len(session._pending_queue):
                affected.append((session, settlement, question_attempt_id))
        return affected

    async def finish_work_cancellation(
        self, affected: list[tuple[Session, _Settlement | None, str | None]]
    ) -> None:
        for session, settlement, question_attempt_id in affected:
            if question_attempt_id is not None:
                await self._end_question_attempt(session, question_attempt_id, reason="work_cancelled")
            if settlement is not None:
                await self._settle_turn(
                    session, settlement, status="interrupted",
                    message="(delegated work cancelled)", code="work_cancelled",
                )
            await self._broadcast(self._dispatch_event(session), user_id=session.user_id)

    async def deliver_bg_result(self, rec) -> bool:  # type: ignore[no-untyped-def]
        """Inject a synthesized user message into a session when a bg
        task completes. Threaded through the same start_message path
        as a real user prompt, so it queues behind an in-flight turn
        instead of racing it.

        `rec` is a server.bg_tasks.BgTaskRecord — passed by name
        rather than imported at module top to avoid a circular import
        (bg_tasks depends on Database; the manager wires the delivery
        callback into us in main.py's lifespan).

        Returns True if the session exists and the prompt was accepted,
        False if the session was already gone (e.g. deleted while the
        bg task was running). Marker `[bg-task-result]` in the prompt
        body is what the frontend keys off of for the "auto" badge —
        keeping it textual means the model also sees the marker in
        chat history on resume, which is the right cue.
        """
        from ..bg_tasks import render_delivery_prompt

        session = self.sessions.get(rec.session_id)
        if session is None:
            logger.info(
                "bg task %s completed for missing session %s; dropping result",
                rec.id,
                rec.session_id,
            )
            return False
        prompt = render_delivery_prompt(rec)
        try:
            receipt = await self.start_message(
                rec.session_id, prompt, attachment_ids=None,
                source="bg", delivery_id=f"bg:{rec.id}",
                work_id=getattr(rec, "work_id", None),
            )
        except Exception:
            logger.exception(
                "Failed to inject bg result for task %s into session %s",
                rec.id,
                rec.session_id,
            )
            return False
        return receipt.accepted

    async def _consume_message(
        self, session_id: str, queued: QueuedPrompt
    ) -> None:
        if queued.question_session_id and queued.question_id:
            if not self.has_pending_question(queued.question_session_id, queued.question_id):
                session = self.sessions.get(session_id)
                if session is not None:
                    notice = MessageContent(
                        role=MessageRole.system, type="text",
                        content=f"[agent-question-expired] Question {queued.question_id} is already closed; no answer turn was started.",
                        tool_input={"request_context": {
                            "request_id": queued.request_id, "source": queued.source,
                        }},
                    )
                    seq = await self._persist_message(session, notice)
                    await self._broadcast(self._tag_persisted({
                        "type": "delivery_notice", "session_id": session_id,
                        "content": notice.content, "request_id": queued.request_id,
                    }, seq, notice), user_id=session.user_id)
                self._withdraw_requests(session, [queued], reason="expired")
                return
        async for _event in self._execute_claimed_request(
            session_id, queued.prompt, queued.attachment_ids,
            request_id=queued.request_id,
            source=queued.source, work_id=queued.work_id,
            **({"wiki_mode": queued.wiki_mode} if queued.wiki_mode else {})
        ):
            pass  # The executor persists and broadcasts each event.

    async def _execute_claimed_request(
        self,
        session_id: str,
        prompt: str,
        attachment_ids: list[str] | None = None,
        *,
        wiki_mode: str | None = None,
        request_id: str | None = None,
        source: str = "human",
        work_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        from ..mcp_identity import work_is_open
        if not work_is_open(work_id):
            return

        try:
            await asyncio.wait_for(session._lock.acquire(), timeout=5.0)
        except TimeoutError:
            raise ValueError(f"Session {session_id} is busy")
        if not work_is_open(work_id):
            session._lock.release()
            return

        from ..wiki.tasks import tasks as wiki_tasks
        session._work_id = work_id
        settlement = session._settlement
        if settlement is None or request_id is None or settlement.request_id != request_id:
            settlement = _Settlement(work_id=work_id, **({"request_id": request_id} if request_id else {}))
        session._settlement = settlement
        wiki_task_id = None
        try:
            session._wiki_mode = wiki_mode
            session._wiki_task_id = wiki_tasks.open(session, wiki_mode, prompt) if wiki_mode else None
            wiki_task_id = session._wiki_task_id
            # Resolve attachment ids → on-disk paths so the prompt can
            # cite absolute paths the agent's `Read` tool will open.
            # Missing files are dropped (with a logged warning) rather
            # than failing the whole turn — the user already typed the
            # prompt; an orphaned id from a deleted file shouldn't eat it.
            attachments_meta: list[AttachmentMetadata] = []
            attachment_paths: list[str] = []
            for aid in attachment_ids or []:
                path = get_attachment_path(session_id, aid)
                if path is None or not path.is_file():
                    logger.warning(
                        "Session %s: dropped missing attachment %s", session_id, aid
                    )
                    continue
                # Reconstruct the user-visible filename from the on-disk
                # `<id>__<filename>` layout.
                fname = path.name.split("__", 1)[1] if "__" in path.name else path.name
                attachments_meta.append(
                    AttachmentMetadata(
                        id=aid,
                        filename=fname,
                        size=path.stat().st_size,
                        mime_type=_guess_mime(fname),
                    )
                )
                attachment_paths.append(str(path))

            # Capture the turn-start git anchor onto the user-message row
            # (session-rewind.md §5.6.3): this row's git state IS the
            # branch-point state if the user later forks here. (None, None)
            # when working_dir isn't a git repo.
            git_head, git_status_clean = await fork_helpers.capture_git_anchor(
                session.working_dir
            )

            # Record user message — content is the *raw* prompt the user
            # typed; the augmented `<attachments>` block is only what we
            # hand to the backend.
            user_msg = MessageContent(
                role=MessageRole.user,
                type="text",
                content=prompt,
                attachments=attachments_meta,
                tool_input={"request_context": {
                    "request_id": settlement.request_id, "source": source, "work_id": work_id,
                }},
            )
            seq = await self._persist_message(
                session,
                user_msg,
                git_head=git_head,
                git_status_clean=git_status_clean,
            )
            event: dict[str, Any] = {
                "type": "user_message",
                "session_id": session_id,
                "content": prompt,
                "tool_input": user_msg.tool_input,
            }
            if attachments_meta:
                event["attachments"] = [a.model_dump() for a in attachments_meta]
            self._tag_persisted(event, seq, user_msg)
            await self._broadcast(event, user_id=session.user_id)
            yield event

            session.status = SessionStatus.running
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "running"},
                user_id=session.user_id,
            )

            # Attachments wrap the raw prompt; the `/showme` viewer flow now
            # resolves on the client + dedicated resolution endpoint instead of
            # being rewritten into a backend command.
            wiki_prompt = prompt
            if wiki_mode and prompt.strip().split(maxsplit=1)[0:1] == ["/wiki"]:
                pieces = prompt.strip().split(maxsplit=2)
                skip = 2 if len(pieces) > 1 and pieces[1] == "maintain" else 1
                wiki_prompt = prompt.strip().split(maxsplit=skip)[skip] if len(prompt.strip().split(maxsplit=skip)) > skip else "Explain the enabled Wiki task and ask what the user wants to do."
            augmented_prompt = _augment_prompt_with_attachments(
                wiki_prompt, attachment_paths
            )

            # Fork first-turn replay (HISTORY_REPLAY backends, e.g. Codex —
            # session-rewind.md §3.5/§5.3.2). DISPATCH-ONLY: the raw user
            # text was persisted/broadcast above; only the prompt the backend
            # subprocess sees is wrapped with the truncated parent transcript
            # (the fork's own copied prefix, seq <= fork_after_seq). The flag
            # clears after the first `result` lands, so turn 2+ isn't wrapped.
            if session.fork_needs_replay and self.db:
                cutoff = (
                    session.fork_after_seq
                    if session.fork_after_seq is not None
                    else -1
                )
                # Bounded in SQL rather than loaded whole and filtered: the
                # prefix is all that is wanted, and on a long session the
                # discarded remainder is the bulk of it (B3).
                copied = [
                    MessageContent(**m)
                    for m in await self.db.load_messages(session.id, max_seq=cutoff)
                ]
                augmented_prompt = fork_helpers.wrap_for_fork_replay(
                    augmented_prompt, copied
                )

            # Spill prompts that would blow Linux's MAX_ARG_STRLEN
            # (~128 KB per argv element) to a per-session file and
            # hand the backend a small pointer instead. Triggers most
            # often on bg-task-result injection of large test-suite
            # output. See server/large_prompts.py.
            backend_dispatch_prompt = spill_if_large(session_id, augmented_prompt)

            try:
                async for ws_event in self._run_backend(session, backend_dispatch_prompt):
                    ws_event.setdefault("request_id", settlement.request_id)
                    ws_event.setdefault("work_id", settlement.work_id)
                    if ws_event["type"] not in ("result", "recording_failed"):
                        await self._broadcast(ws_event, user_id=session.user_id)
                    yield ws_event
            except Exception as e:
                logger.exception("Backend error in session %s", session_id)
                event = await self._settle_turn(
                    session, settlement, status="failed", message=str(e), code="backend_exception",
                )
                yield event
        except asyncio.CancelledError:
            await self._settle_turn(
                session, settlement, status="interrupted", message="(interrupted by user)",
                code="interrupted",
            )
            raise
        except Exception as exc:
            logger.exception("Request initialization failed in session %s", session_id)
            yield await self._settle_turn(
                session, settlement, status="failed", message=str(exc), code="initialization_error",
            )
        finally:
            wiki_tasks.close(wiki_task_id)
            if session._settlement is settlement:
                session._wiki_task_id = None
                session._wiki_mode = None
                session.status = SessionStatus.idle
            try:
                if self.db:
                    await self.db.flush()
                if session._settlement is settlement:
                    await self._broadcast(
                        {"type": "status", "session_id": session_id, "status": "idle"},
                        user_id=session.user_id,
                    )
            finally:
                session._lock.release()

    async def interrupt(self, session_id: str) -> bool:
        _monitor.record(_MonEvent(kind="turn_interrupt", session_id=session_id))
        """Pause dispatch and cancel the request captured before any await.

        Best-effort: if the backend subprocess is wedged (e.g. waiting on
        a control_response we'll never send), interrupt still releases the
        UI immediately by cancelling the inner task — the subprocess gets
        torn down in the background. We never block the caller on
        backend.interrupt(), which can take seconds for stdin-close →
        SIGTERM → SIGKILL escalation.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return False

        session._dispatch_paused = True
        session._steer_open = False
        for pending in (*session._pending_queue, *session._steer_queue):
            pending.priority = False
        current = session._current_request
        unstarted = current is not None and not current.started
        if unstarted:
            assert current is not None
            current.priority = False
            if not any(item.request_id == current.request_id for item in session._pending_queue):
                session._pending_queue.append(current)
                session._pending_queue.sort(key=lambda item: item.order)
        pause_event = self._dispatch_event(session)

        # Fire backend teardown in the background and return fast. The
        # inner task cancellation below releases session._lock via
        # The executor's finally clause, so new turns become possible
        # even before the subprocess actually exits.
        if session._backend:
            backend = session._backend
            # Let go of it immediately. The teardown task below holds its own
            # reference, but the session must not: an interrupted process is
            # being torn down, and with reuse a next turn that found it still
            # briefly alive would hand its prompt to a dying CLI.
            self._retire_backend(session, backend)

        inner = session._inner_task
        settlement = session._settlement
        if current is not None and (
            settlement is None or settlement.request_id != current.request_id
        ):
            settlement = None
        question_attempt = session._question_attempt_id
        had_active = inner is not None and not inner.done()
        if had_active:
            inner.cancel()
        elif session._lock.locked():
            # Wedged state: no live task to cancel but the lock is still
            # held (typically: previous turn's task got cancelled but its
            # finally clause was bypassed somehow). Force-release so the
            # UI isn't soft-locked. Distinguish this from a truly idle
            # session, which should return False below.
            try:
                session._lock.release()
            except RuntimeError:
                pass
            session.status = SessionStatus.idle
            await self._broadcast(
                {"type": "status", "session_id": session_id, "status": "idle"},
                user_id=session.user_id,
            )
        else:
            # Truly idle — nothing to interrupt.
            await self._end_question_attempt(session, question_attempt, reason="interrupted")
            await self._broadcast(pause_event, user_id=session.user_id)
            return False

        await self._end_question_attempt(session, question_attempt, reason="interrupted")
        await self._broadcast(pause_event, user_id=session.user_id)

        if unstarted:
            return True  # The retained request has no execution outcome yet.

        if settlement is not None:
            await self._settle_turn(
                session, settlement, status="interrupted", message="(interrupted by user)",
                code="interrupted",
            )
            return True

        marker = MessageContent(
            role=MessageRole.system,
            type="error",
            content="(interrupted by user)",
        )
        marker_seq = await self._persist_message(session, marker)
        event: dict[str, Any] = {
            "type": "error",
            "session_id": session_id,
            "message": "(interrupted by user)",
        }
        self._tag_persisted(event, marker_seq, marker)
        await self._broadcast(event, user_id=session.user_id)
        return True

    async def cancel_request(self, session_id: str, request_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        removed = [q for q in (*session._pending_queue, *session._steer_queue) if q.request_id == request_id]
        session._pending_queue[:] = [q for q in session._pending_queue if q.request_id != request_id]
        session._steer_queue[:] = [q for q in session._steer_queue if q.request_id != request_id]
        self._withdraw_requests(session, removed, reason="withdrawn")
        current = session._current_request
        if current is None or current.request_id != request_id:
            return bool(removed)
        session._steer_open = False
        if not current.started:
            self._withdraw_requests(session, [current], reason="withdrawn")
        backend = session._backend
        if backend is not None:
            self._retire_backend(session, backend)
        task = session._inner_task
        if task is not None and not task.done():
            task.cancel()
            await drain_cancelled(task, f"cancel request {request_id}")
        await self._await_backend_retirement(session)
        return True

    async def reset_session(self, session_id: str) -> None:
        """Force-reset a stuck session."""
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        self._withdraw_requests(session, [*session._pending_queue, *session._steer_queue], reason="withdrawn")
        session._pending_queue.clear()
        session._steer_queue.clear()
        session._dispatch_paused = False
        if session._inner_task and not session._inner_task.done():
            session._inner_task.cancel()
        if session._active_task and not session._active_task.done():
            session._active_task.cancel()
        if session._backend:
            self._retire_backend(session, session._backend)
        await self._await_backend_retirement(session)
        if session._lock.locked():
            session._lock.release()
        session.status = SessionStatus.idle
        session._pending_approvals.clear()
        await self._close_session_questions(session, reason="reset")
        await self._broadcast(
            {"type": "status", "session_id": session_id, "status": "idle"}
        )
        if self.sessions.get(session_id) is session:
            await self._broadcast(self._dispatch_event(session), user_id=session.user_id)

    # ------------------------------------------------------------------ #
    # One CLI invocation: acquire a process, start it, retire it
    # ------------------------------------------------------------------ #

    async def _acquire_backend(
        self,
        session: Session,
        credential: HarnessCredential | None,
        agent: dict[str, Any] | None,
        connectors: list[tuple[Any, Any]] | None,
        configured_session: Session | None = None,
        *, retirement_timeout: float | None = None,
    ) -> tuple[HarnessRun, bool]:
        """The process this attempt runs in, and whether it already holds the
        conversation.

        Reuse the session's live process when nothing it baked in at spawn has
        changed (inline-steering.md §7); otherwise spawn. Declining to reuse
        must not simply overwrite the handle: that orphans a live ~255MB
        process that nothing points at any more, so neither the reaper nor
        shutdown can ever reach it. Let it go properly first.
        """
        retirement_deadline = None if retirement_timeout is None else time.monotonic() + retirement_timeout
        await self._await_backend_retirement(session, timeout=retirement_timeout)
        configured = configured_session or session
        configured._backend = session._backend
        configured._steer_open = session._steer_open
        reused = self._reusable_run(
            configured, session.working_dir, credential, agent, connectors
        )
        if reused is None and session._backend is not None:
            stale = session._backend
            self._retire_backend(session, stale)
            remaining = None if retirement_deadline is None else max(0.0, retirement_deadline - time.monotonic())
            await self._await_backend_retirement(session, timeout=remaining)
        backend = reused or self._make_run(configured, agent, connectors)
        session._backend = backend
        return backend, reused is not None

    async def _start_attempt(
        self,
        session: Session,
        attempt: _Attempt,
        credential: HarnessCredential | None,
    ) -> None:
        """Hand this attempt's prompt to the process.

        A reused process already holds this conversation, so there is no prompt
        to re-render and no transcript to resume: just hand it the next
        message.
        """
        if attempt.reused:
            await attempt.backend.send_turn(attempt.prompt)
        else:
            await attempt.backend.start(
                attempt.prompt,
                session.working_dir,
                session.claude_session_id,
                credential=credential,
            )

    async def _open_steering_window(
        self, session: Session, backend: HarnessRun
    ) -> asyncio.Task[int] | None:
        """Open the steering window and start the writer that drains it.

        Open from here until `result` (inline-steering.md §8). Only a backend
        that takes input on stdin can be steered; everything else keeps
        queueing, so it gets no writer.
        """
        if not backend.reusable:
            return None
        async with session._steer_lock:
            session._steer_open = True
            if session._steer_queue:
                session._steer_ready.set()
        return asyncio.create_task(
            self._steer_writer(session, backend),
            name=f"steer-writer-{session.id}",
        )

    async def _close_steering_window(
        self, session: Session, steer_writer: asyncio.Task | None
    ) -> None:
        """Shut the steering window and hand back anything the writer missed.

        Ordering matters and is the reason this runs before anything else
        touches the process: once the window is shut, a frame written now would
        start a fresh turn rather than steer this one. A steer that missed its
        turn is not dropped — it becomes a normal queued prompt and runs next
        (inline-steering.md §9).
        """
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            session._steer_open = False
            await drain_cancelled(steer_writer, f"session {session.id} steer writer")
        else:
            async with session._steer_lock:
                session._steer_open = False
            await drain_cancelled(steer_writer, f"session {session.id} steer writer")
        dispatch_event = None
        async with session._steer_lock:
            if session._steer_queue:
                logger.info(
                    "session %s: %d steer(s) missed the turn; queuing them",
                    session.id,
                    len(session._steer_queue),
                )
                async with session._dispatch_lock:
                    session._pending_queue.extend(session._steer_queue)
                    session._pending_queue.sort(key=lambda item: item.order)
                    session._steer_queue.clear()
                    dispatch_event = self._dispatch_event(session)
            session._steer_ready.clear()
        if dispatch_event and self.sessions.get(session.id) is session:
            await self._broadcast(dispatch_event, user_id=session.user_id)

    async def _stop_watchdog(self, watchdog: asyncio.Task | None) -> None:
        """Cancel the turn watchdog and wait for it to finish unwinding."""
        await drain_cancelled(watchdog, "turn watchdog")

    @staticmethod
    def _turn_ended_cleanly(attempt: _Attempt) -> bool:
        """Whether the CLI process is worth keeping for the next turn.

        Only a turn that ended the way a turn is supposed to — a clean
        `result`, nothing tripped, nothing to retry. A watchdog timeout, an
        error or an interrupt all leave the CLI in a state we would rather not
        inherit, so those shut it down and the next turn spawns fresh.
        """
        return (
            attempt.backend.reusable
            and attempt.backend.is_alive()
            and attempt.saw_result
            and not attempt.failed
            and attempt.timeout is None
        )

    async def _retire_or_hold_process(
        self, session: Session, attempt: _Attempt, *, retirement_timeout: float | None = None
    ) -> None:
        """Hold the process for the next turn, or shut it down now."""
        if self._turn_ended_cleanly(attempt) and session._backend is attempt.backend:
            session._held_run_at = time.monotonic()
            # Enforce the cap HERE, not only on the reaper's tick. The reaper
            # is a background task that doesn't exist in tests and runs every
            # 30s in production, so leaving the bound to it means N finished
            # sessions can each pin ~255MB in between — which is exactly how
            # the backend suite got OOM-killed.
            await self._enforce_held_cap(keep_session_id=session.id)
            return
        deadline = None if retirement_timeout is None else time.monotonic() + retirement_timeout
        if session._backend is attempt.backend:
            self._retire_backend(session, attempt.backend)
        try:
            await self._await_backend_retirement(session, timeout=retirement_timeout)
        except Exception:
            if deadline is not None and time.monotonic() >= deadline:
                # The captured retirement stays owned and gates successors;
                # reaching the logical turn cap retains its existing outcome.
                attempt.watchdog["tripped"] = ("overall", settings.turn_max_seconds)
                return
            raise

    # ------------------------------------------------------------------ #
    # One event off the stream
    # ------------------------------------------------------------------ #

    def _coalesce_delta(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> dict[str, Any] | None:
        """Buffer a token delta, emitting a frame at most every flush window."""
        if event.content:
            attempt.delta_buf.append(event.content)
        now = time.monotonic()
        if now - attempt.delta_last_flush < _DELTA_FLUSH_SECONDS:
            return None
        attempt.delta_last_flush = now
        return self._flush_text_deltas(session.id, attempt.delta_buf)

    async def _remember_resume_id(self, session: Session, resume_id: str) -> None:
        """Persist the engine-side conversation id this session resumes from."""
        if session.claude_session_id == resume_id:
            return
        session.claude_session_id = resume_id
        if self.db:
            await self.db.update_session_field(
                session.id, claude_session_id=resume_id
            )

    def _note_question_request(
        self, session: Session, question_id: str, tool_input: dict[str, Any] | None
    ) -> PendingQuestion:
        """Track pending question state for reconnect re-render."""
        pending = self._register_pending_question(session, question_id, (tool_input or {}).get("questions") or [])
        self._schedule_question_timeout(session, question_id)
        return pending

    async def _handle_result_event(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> None:
        """The turn said it was done: record it and settle the session."""
        attempt.saw_result = True
        attempt.result = event
        _monitor.record(_MonEvent(
            kind="turn",
            session_id=session.id,
            agent_id=session.agent_id,
            backend=session.backend,
            duration_ms=event.duration_ms,
            ok=not event.is_error,
            detail={"cost": event.cost, "num_turns": event.num_turns},
        ))
        # Shut the steering window first: past this point the CLI is idle, and
        # a frame written now would start a fresh turn rather than steer this
        # one.
        async with session._steer_lock:
            session._steer_open = False
        # Update the resume id in case the CLI reissued a different one
        # mid-stream.
        if event.session_id:
            await self._remember_resume_id(session, event.session_id)
        # First fork turn produced a result: drop the ephemeral fork state so
        # turn 2+ behaves like a normal resumed session (session-rewind.md
        # §5.3.2/§5.6.5).
        await self._clear_fork_first_turn_state(session)

    @staticmethod
    def _capture_error_text(attempt: _Attempt, event: HarnessEvent) -> None:
        """Keep a failed turn's own words for the classifiers that run after it
        (harness-credential-reauth.md §4). `tool_result` errors never get here
        — a tool failing isn't the turn failing."""
        attempt.saw_error_event = True
        if event.error_code:
            attempt.error_code = event.error_code
        if event.content:
            attempt.error_text += event.content + "\n"
        if event.raw:
            try:
                attempt.error_text += json.dumps(event.raw) + "\n"
            except (TypeError, ValueError):
                pass

    async def _handle_stream_event(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> list[dict[str, Any]]:
        """One harness event → the WS frames it produces, in order.

        The dispatch the whole loop reduces to. The event vocabulary is
        normalized and enumerated in `harness/events.py`, so this branches on a
        contract rather than on whatever a CLI happened to print.
        """
        from ..harness.tool_identity import ToolIdentityTracker
        tracker = getattr(attempt.backend, "_tool_identity_tracker", None)
        if tracker is None:
            tracker = ToolIdentityTracker()
            attempt.backend._tool_identity_tracker = tracker
        tracker.normalize(event, session._settlement.request_id if session._settlement else "untracked",
                          attempt.attempt_id)
        if event.type == "text_delta":
            if event.content and event.content.strip():
                attempt.saw_text = True
            flushed = self._coalesce_delta(session, attempt, event)
            return [flushed] if flushed is not None else []

        frames: list[dict[str, Any]] = []
        # Anything else ends the current delta run: flush it first so the
        # partial text can never arrive after the completed block that
        # supersedes it.
        flushed = self._flush_text_deltas(session.id, attempt.delta_buf)
        if flushed is not None:
            attempt.delta_last_flush = time.monotonic()
            frames.append(flushed)

        # session_started arrives on the CLI's init event, before any tool
        # work. Persist the resume id immediately so the recovery path can use
        # it even if the bug suppresses `result`. Internal event — never
        # persisted or broadcast.
        if event.type == "session_started" and event.session_id:
            await self._remember_resume_id(session, event.session_id)
            return frames

        # A sub-agent's progress: remembered on the session so a reload can
        # still paint the card, broadcast so the open UI paints it now, and
        # never persisted — the Task tool call and its result are the durable
        # record (native-subagents.md §4).
        if event.type == "subagent" and event.subagent is not None:
            self._record_subagent(session, event.subagent)

        if event.type == "tool_use":
            attempt.saw_tool_use = True
        if event.type == "text" and event.content and event.content.strip():
            attempt.saw_text = True

        # Persist whichever message shape this event maps to. The returned seq
        # goes onto the WS event so reconnecting clients can dedupe against
        # their snapshot.
        if event.type == "result":
            await self._handle_result_event(session, attempt, event)
            if event.is_error:
                self._capture_error_text(attempt, event)
            return frames
        if event.type == "error":
            self._capture_error_text(attempt, event)
            frames.append(await self._recovery_frame(
                session, code="backend_diagnostic", phase="diagnostic",
                message=event.content or "Backend notification",
            ))
            return frames
        pending_question = None
        if event.type == "question_request" and event.tool_use_id:
            pending_question = self._note_question_request(session, event.tool_use_id, event.tool_input)
        msg_content = self._event_to_message_content(event)
        msg_seq: int | None = None
        if msg_content is not None:
            if pending_question is not None:
                msg_content.tool_input = {
                    **(msg_content.tool_input or {}),
                    "deadline": pending_question.deadline,
                    "request_id": pending_question.request_id,
                    "attempt_id": pending_question.attempt_id,
                    "work_id": pending_question.work_id,
                }
            msg_seq = await self._persist_message(session, msg_content)
        # Translate into the WS message shape the front-end expects.
        ws_event = self._event_to_ws_message(session.id, event)
        if ws_event is not None and pending_question is not None:
            ws_event.update(
                deadline=pending_question.deadline,
                request_id=pending_question.request_id,
                attempt_id=pending_question.attempt_id,
                work_id=pending_question.work_id,
            )
        if ws_event is not None and msg_content is not None:
            self._tag_persisted(ws_event, msg_seq, msg_content)
        if ws_event is not None:
            frames.append(ws_event)
        return frames

    # ------------------------------------------------------------------ #
    # What a finished attempt means: stop, or run the CLI again
    # ------------------------------------------------------------------ #

    @staticmethod
    def _failure_blob(attempt: _Attempt) -> str:
        """Everything a failed attempt said, for the pattern matchers: the
        terminal event's content/raw plus the CLI's stderr, capped.

        getattr: a real HarnessRun exposes stderr_text; lightweight test and
        backend stand-ins may not.
        """
        stderr_text = getattr(attempt.backend, "stderr_text", "") or ""
        return (attempt.error_text + "\n" + stderr_text)[:8000]

    async def _timeout_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision | None:
        """Resume an idle stop within its budget; the logical-turn cap stops
        all recovery. Output requires a resume id so completed tools aren't
        replayed from the original request."""
        if attempt.timeout is None:
            return None
        reason, limit = attempt.timeout
        overall = settings.turn_max_seconds
        if overall > 0 and time.monotonic() - turn.started_at >= overall:
            reason, limit = "overall", overall
        maximum = max(0, settings.turn_idle_max_retries)
        if (
            reason == "idle"
            and turn.idle_timeout_retries < maximum
            and (not turn.produced_output or session.claude_session_id)
        ):
            turn.idle_timeout_retries += 1
            turn.settlement.recovery["idle"] = turn.idle_timeout_retries
            if turn.produced_output:
                next_prompt = (
                    "The previous attempt stopped after a period without backend "
                    "activity. Continue the user's unfinished request from this "
                    "conversation. Check existing files and tool outcomes before "
                    "repeating operations: interrupted tools may already have "
                    "changed things. Keep any existing delegations and background "
                    "jobs; do not duplicate them. Work in smaller steps and use "
                    "background execution for long commands when available."
                )
            else:
                next_prompt = turn.prompt
                if session.claude_session_id != turn.resume_at_turn_start:
                    session.claude_session_id = turn.resume_at_turn_start
                    if self.db:
                        await self.db.update_session_field(
                            session.id, claude_session_id=turn.resume_at_turn_start
                        )
            return _Decision.again(
                next_prompt,
                await self._surface_idle_retry(
                    session, limit=limit, attempt=turn.idle_timeout_retries,
                    max_attempts=maximum,
                ),
            )
        return _Decision.stop(
            await self._surface_turn_timeout(
                session, reason=reason, limit=limit, backend=turn.harness.backend,
                retries=turn.idle_timeout_retries,
            )
        )

    async def _auth_decision(
        self, session: Session, turn: _Turn, blob: str, *, error_code: str | None = None
    ) -> _Decision | None:
        """Reactive auth-expiry: a failed turn whose error text matches this
        backend's auth-rejection patterns means the bound credential is dead
        (revoked / rotated / expired past what the proactive refresh caught).
        Flag it needs_reconnect and surface a re-authorize prompt, then STOP —
        re-auth won't fix itself, and a retry just burns the budget
        (harness-credential-reauth.md §4)."""
        if not turn.harness.is_auth_error(blob, code=error_code):
            return None
        return _Decision.stop(
            await self._surface_auth_expiry(
                session, cred_id=turn.cred_id, backend=turn.harness.backend
            )
        )

    async def _stale_session_decision(
        self, session: Session, turn: _Turn, blob: str
    ) -> _Decision | None:
        """Dangling resume id: the engine no longer holds the conversation this
        session is pinned to (its local transcript was rotated or cleaned; ours
        lives in the DB and is intact).

        This is NOT transient — retrying the same id fails identically forever,
        which bricks the session silently: a result with zero turns, zero cost
        and no text. Drop the dead id and re-run the same prompt once as a
        fresh engine-side conversation, saying out loud that the engine lost
        its own history so the model starts this turn without it.
        """
        if not (
            session.claude_session_id
            and turn.stale_session_retries < 1
            and turn.harness.is_stale_session_error(blob)
        ):
            return None
        turn.stale_session_retries += 1
        turn.settlement.recovery["stale_session"] = turn.stale_session_retries
        logger.warning(
            "Session %s: resume id %s is gone from the engine; "
            "clearing it and starting a fresh conversation",
            session.id,
            session.claude_session_id,
        )
        session.claude_session_id = None
        turn.resume_at_turn_start = None
        if self.db:
            await self.db.update_session_field(session.id, claude_session_id=None)
        # Don't restart cold: Octopus still has the whole conversation (the
        # engine's transcript is a cache of ours, not the record), so replay
        # its tail into this turn through the same channel a fork uses when its
        # backend can't resume natively. The model continues the conversation
        # instead of appearing to forget it.
        replayed = 0
        omitted = 0
        recovery_prompt = turn.prompt
        if self.db:
            history = [
                MessageContent(**m) for m in await self.db.load_messages(session.id)
            ]
            kept, omitted = fork_helpers.select_lost_history(history)
            replayed = len(kept)
            if kept:
                recovery_prompt = spill_if_large(
                    session.id,
                    fork_helpers.wrap_for_lost_history(
                        turn.prompt, kept, omitted=omitted
                    ),
                )
        return _Decision.again(
            recovery_prompt,
            await self._surface_stale_session(
                session,
                backend=turn.harness.backend,
                replayed=replayed,
                omitted=omitted,
            ),
        )

    async def _transient_decision(
        self, session: Session, turn: _Turn, blob: str, *, error_code: str | None = None
    ) -> _Decision | None:
        """Transient provider-reliability failure (5xx / overloaded / dropped
        connection / server-side throttle) → bounded retry.

        TWO modes, by whether any attempt in this turn already produced output:

        * **no output yet** → re-run the ORIGINAL prompt from the turn-start
          resume state (side-effect-free; discard any resume id a failed
          no-output attempt captured — Vera).
        * **output already streamed** (tool_use/text) AND a resume id was
          captured → RESUME with "continue" so we pick up where it left off
          WITHOUT re-running tools or duplicating text. This is the common
          case: a long agent turn throttled mid-flight — the earlier
          no-output-only gate let it stop.

        Quota/credit errors match no pattern here → they surface as-is
        (harness-transient-retry.md §4).
        """
        if not turn.harness.is_transient_error(blob, code=error_code):
            return None
        can_retry = turn.transient_attempts < self._MAX_TRANSIENT_RETRIES and (
            not turn.produced_output or bool(session.claude_session_id)
        )
        if not can_retry:
            # Budget exhausted (or output with no resume id to continue from) —
            # surface a clear error so the user knows it wasn't their request
            # that failed.
            return _Decision.stop(
                await self._surface_transient_exhausted(
                    session,
                    backend=turn.harness.backend,
                    attempts=turn.transient_attempts,
                )
            )

        turn.transient_attempts += 1
        turn.settlement.recovery["transient"] = turn.transient_attempts
        delay = self._TRANSIENT_RETRY_BASE_DELAY * (2 ** (turn.transient_attempts - 1))
        logger.warning(
            "Session %s: transient backend error; retrying in "
            "%.1fs (attempt %d/%d, resume=%s)",
            session.id,
            delay,
            turn.transient_attempts,
            self._MAX_TRANSIENT_RETRIES,
            turn.produced_output,
        )
        if turn.produced_output:
            # Continue the in-progress conversation from its captured resume
            # id — no re-run, no duplication.
            next_prompt = (
                "Continue the user's unfinished request from this conversation. "
                "Check existing files and tool outcomes before repeating operations: "
                "interrupted tools may already have changed things. Keep existing "
                "delegations and background jobs; do not duplicate them."
            )
        else:
            next_prompt = turn.prompt  # original invocation
            if session.claude_session_id != turn.resume_at_turn_start:
                session.claude_session_id = turn.resume_at_turn_start
                if self.db:
                    await self.db.update_session_field(
                        session.id, claude_session_id=turn.resume_at_turn_start
                    )
        return _Decision.again(
            next_prompt,
            await self._surface_transient_retry(
                session,
                attempt=turn.transient_attempts,
                max_attempts=self._MAX_TRANSIENT_RETRIES,
                delay=delay,
            ),
            delay=delay,
        )

    async def _premature_exit_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision:
        """Whether to respawn after the CLI's premature-exit bug.

        The signature is: the CLI exited without a `result` event AFTER
        emitting a `tool_use`. Anything else — a clean turn, an immediate crash
        with no tool use, a turn we've already retried once — is left alone.
        This is the last word on an attempt, so it always decides.
        """
        if attempt.saw_result:
            return _Decision.stop()
        if not turn.harness.premature_exit_recovery:
            # Harness opts out of the Claude-CLI premature-exit recovery (Codex
            # runs exactly once per turn) — codex-backend.md §5.6.
            return _Decision.stop()
        if turn.recovery_attempts >= self._MAX_RECOVERY_ATTEMPTS:
            logger.warning(
                "Session %s: CLI premature-exit retry budget exhausted; "
                "giving up on this turn", session.id
            )
            return _Decision.stop()
        if not attempt.saw_tool_use:
            return _Decision.stop()
        if not session.claude_session_id:
            # No resume id captured (init never arrived) — we can't respawn
            # into the same conversation.
            return _Decision.stop()

        turn.recovery_attempts += 1
        turn.settlement.recovery["premature_exit"] = turn.recovery_attempts
        logger.warning(
            "Session %s: detected CLI premature-exit after tool_use; "
            "auto-respawning with 'continue' (attempt %d/%d)",
            session.id, turn.recovery_attempts, self._MAX_RECOVERY_ATTEMPTS,
        )
        marker_event = await self._recovery_frame(
            session, code="premature_exit", message="(auto-resumed after CLI exited mid-turn)",
            attempt=turn.recovery_attempts, maximum=self._MAX_RECOVERY_ATTEMPTS,
        )
        return _Decision.again("continue", marker_event)

    async def _after_attempt(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision:
        """Classify a finished CLI invocation.

        The order is the design, not a preference. A watchdog timeout is read
        first so it uses its own budget rather than a transient retry. Auth comes
        before the retries because re-auth won't fix itself. A dangling resume
        id comes before transient because it looks transient and is not.
        Premature-exit recovery is last and always answers, because "the CLI
        just stopped" is only knowable once nothing else claimed the failure.
        """
        if self._remaining_turn_time(turn) == 0 and attempt.timeout is None:
            attempt.watchdog["tripped"] = ("overall", settings.turn_max_seconds)
            if session._backend is attempt.backend:
                await self._retire_or_hold_process(
                    session, attempt, retirement_timeout=self._remaining_turn_time(turn)
                )
        decision = await self._timeout_decision(session, turn, attempt)
        if decision is not None:
            return decision

        if attempt.failed:
            blob = self._failure_blob(attempt)
            decision = await self._auth_decision(session, turn, blob, error_code=attempt.failure_code)
            if decision is not None:
                return decision
            decision = await self._stale_session_decision(session, turn, blob)
            if decision is not None:
                return decision
            decision = await self._transient_decision(
                session, turn, blob, error_code=attempt.failure_code,
            )
            if decision is not None:
                return decision

        return await self._premature_exit_decision(session, turn, attempt)

    @staticmethod
    def _remaining_turn_time(turn: _Turn) -> float | None:
        limit = settings.turn_max_seconds
        return max(0.0, limit - (time.monotonic() - turn.started_at)) if limit > 0 else None

    async def _settle_overall_timeout(self, session: Session, turn: _Turn) -> dict[str, Any]:
        detail = await self._surface_turn_timeout(
            session, reason="overall", limit=settings.turn_max_seconds, backend=turn.harness.backend,
        )
        return await self._settle_turn(
            session, turn.settlement, status="failed", message=detail["message"],
            code="turn_timeout",
        )

    async def _run_backend(
        self, session: Session, prompt: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive one logical turn through the backend, recovering from
        CLI premature-exit-after-tool-roundtrip if it fires.

        Each iteration of the loop is one CLI invocation. It normally runs
        exactly once and returns after a `result` event; `_after_attempt` is
        what decides otherwise — a bounded retry for a transient failure, a
        fresh conversation for a dangling resume id, a `"continue"` respawn for
        the premature-exit bug post-mortemed in
        docs/post-mortems/2026-05-18-bg-pipeline-hardening.md §2.
        """
        # Load the owning agent fresh each turn — this is the live-reference
        # point: editing an agent's prompt/model/tools/MCP affects its
        # already-open sessions on their next turn (agent-refactor.md §5.2).
        async with self.configuration_lock:
            configured_session = replace(session)
            agent = await self._load_agent(configured_session)
        harness = get_harness(configured_session.backend)
        credential = await self._resolve_credential(configured_session, agent, harness)
        # Defend the invocation boundary too: a refreshed catalog or an
        # internal caller can change availability without going through PATCH.
        tier = configured_session.service_tier or (agent or {}).get("service_tier")
        if tier == "fast":
            catalog = harness.model_settings(credential)
            model = configured_session.model or (agent or {}).get("model")
            if catalog.service_tiers and tier not in catalog.tiers_for(model):
                raise ValueError("Fast speed is unavailable for this model. Choose Normal speed.")
        connectors = await self._load_connectors(agent)
        settlement = session._settlement
        if settlement is None or settlement.final_task is not None:
            settlement = _Settlement()
            session._settlement = settlement
        # A setting saved while this turn is running belongs to the next turn,
        # including when the current one needs an automatic retry.
        turn = _Turn(
            prompt=prompt,
            current_prompt=prompt,
            harness=harness,
            cred_id=configured_session.credential_id
            or (agent.get("credential_id") if agent else None),
            resume_at_turn_start=session.claude_session_id,
            settlement=settlement,
        )

        while True:
            if self._remaining_turn_time(turn) == 0:
                yield await self._settle_overall_timeout(session, turn)
                return
            try:
                backend, reused = await self._acquire_backend(
                    session, credential, agent, connectors, configured_session,
                    retirement_timeout=self._remaining_turn_time(turn),
                )
            except RuntimeError:
                if self._remaining_turn_time(turn) == 0:
                    yield await self._settle_overall_timeout(session, turn)
                    return
                raise
            attempt = _Attempt(
                backend=backend, prompt=turn.current_prompt, reused=reused
            )
            if hasattr(backend, "bind_tool_context"):
                backend.bind_tool_context(turn.settlement.request_id, attempt.attempt_id)
            await self._begin_question_attempt(session, attempt.attempt_id, attempt.watchdog)
            watchdog = self._start_turn_watchdog(
                backend, attempt.watchdog, started_at=turn.started_at
            )
            # Bound before the try: the `finally` reads it, and a spawn that
            # raises would otherwise unwind through an unbound name and hide
            # the real error.
            steer_writer: asyncio.Task[int] | None = None

            try:
                if self._remaining_turn_time(turn) == 0:
                    yield await self._settle_overall_timeout(session, turn)
                    return
                await self._start_attempt(session, attempt, credential)
                steer_writer = await self._open_steering_window(session, backend)
                async for event in backend.stream():
                    attempt.touch()
                    for frame in await self._handle_stream_event(
                        session, attempt, event
                    ):
                        yield frame
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if attempt.timeout is None or task is None or task.cancelling():
                    raise
                # A watchdog may retire a run while its spawn is returning.
                # That run's synthetic cancellation retains the timeout cause.
            finally:
                # Close the steering window and stop the writer before anything
                # else touches the process (§9), then decide the process's fate.
                await self._close_steering_window(session, steer_writer)
                await self._stop_watchdog(watchdog)
                await self._end_question_attempt(session, attempt.attempt_id)
                if self._remaining_turn_time(turn) == 0:
                    attempt.watchdog["tripped"] = ("overall", settings.turn_max_seconds)
                await self._retire_or_hold_process(
                    session, attempt, retirement_timeout=self._remaining_turn_time(turn)
                )

            turn.produced_output = turn.produced_output or attempt.produced_output
            if attempt.result is not None and attempt.result.cost is not None:
                settlement.cost = (settlement.cost or 0) + attempt.result.cost
            decision = await self._after_attempt(session, turn, attempt)
            if not decision.retry:
                detail = decision.events[-1] if decision.events else {}
                failed = attempt.failed or attempt.timeout is not None
                reason = detail.get("message")
                raw_reason = attempt.result.content if attempt.result else attempt.error_text
                if failed and raw_reason:
                    reason = f"{reason}\n{raw_reason}" if reason else raw_reason
                if failed and not reason:
                    reason = safe_diagnostic(getattr(backend, "stderr_text", "")) or "Backend ended without a terminal result"
                yield await self._settle_turn(
                    session, settlement, status="failed" if failed else "success",
                    message=reason, code=detail.get("code"), result=attempt.result,
                    metadata=detail, provider_code=attempt.failure_code,
                )
                return
            if attempt.failed and attempt.error_text:
                yield await self._recovery_frame(
                    session, code="attempt_failed", phase="diagnostic", message=attempt.error_text,
                )
            for frame in decision.events:
                frame["retrying"] = True
                yield frame
            if decision.prompt is not None:
                turn.current_prompt = decision.prompt
            if decision.delay:
                remaining = self._remaining_turn_time(turn)
                await asyncio.sleep(decision.delay if remaining is None else min(decision.delay, remaining))

    async def _load_agent(self, session: Session) -> dict[str, Any] | None:
        """Fetch the session's owning agent row (or None for legacy/no-DB)."""
        if self.db is None or not session.agent_id:
            return None
        return await self.db.get_agent(session.agent_id)

    async def _load_connectors(
        self, agent: dict[str, Any] | None
    ) -> list[tuple[Any, Any]]:
        """The agent's enabled connectors as (ConnectorBase, installation)
        tuples — loaded fresh each turn (same live-reference contract as the
        agent itself). Kinds no longer registered are skipped."""
        if self.db is None or agent is None:
            return []
        from ..connectors.base import ConnectorInstallation
        from ..connectors.custom import resolve_connector

        rows = await self.db.get_enabled_connectors_for_agent(agent["id"])
        out: list[tuple[Any, Any]] = []
        for row in rows:
            connector = await resolve_connector(self.db, row["kind"])
            if connector is not None:
                out.append((connector, ConnectorInstallation.from_row(row)))
        return out

    def _make_run(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> HarnessRun:
        """Build the per-turn run for a session via its harness. Single seam
        the run loop calls (and tests monkeypatch); dispatches on
        `session.backend` through the registry — no kind branching here."""
        run = get_harness(session.backend).create_run(
            self._run_config(session, agent, connectors)
        )
        # A held process keeps working after a turn ends — an asynchronous
        # sub-agent is still running, and the CLI wakes the agent to report it
        # when it lands. Those events belong to the session
        # (native-subagents.md §7).
        run.set_idle_handler(
            lambda event, sid=session.id: self._handle_idle_event(sid, event, backend=run)
        )
        return run

    async def _handle_idle_event(
        self, session_id: str, event: HarnessEvent, *, backend: HarnessRun | None = None,
    ) -> None:
        """An event that arrived on a held process with no turn in flight.

        The CLI can produce a turn's worth of work nobody asked for at that
        moment: an async sub-agent finishing (`Async agent launched
        successfully` returns immediately and the work continues), the
        follow-up turn where the agent reports the result, a native cron tick.
        Dropping those — what happened before — loses the answer outright and
        leaves the sub-agent card spinning forever.

        They are handled exactly like in-turn events, minus the turn
        machinery: persisted if they map to a message, broadcast either way.
        Session status is deliberately NOT flipped to `running`: no
        `_active_task` exists to interrupt, and claiming otherwise would offer
        the user a stop button that stops nothing.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return
        if backend is not None:
            from ..mcp_identity import McpScope, scope_is_live
            if session._backend is not backend or not scope_is_live(McpScope(
                session_id, user_id=session.user_id, run_lease_id=backend.run_lease_id,
                work_id=backend.work_id,
            )):
                return

        # Token deltas are a live view of a block that is about to arrive
        # complete. Out of turn there is nothing live to view — the turn's
        # stream is closed and the UI has already painted its final text — so
        # delivering them repaints a stale partial *after* the answer. Drop
        # them; the completed `text` that follows is the real event.
        if event.type == "text_delta":
            return

        if event.type == "subagent" and event.subagent is not None:
            self._record_subagent(session, event.subagent)
        elif event.type == "session_started" and event.session_id:
            if session.claude_session_id != event.session_id:
                session.claude_session_id = event.session_id
                if self.db:
                    await self.db.update_session_field(
                        session_id, claude_session_id=event.session_id
                    )
            return

        msg_content = self._event_to_message_content(event)
        msg_seq: int | None = None
        if msg_content is not None:
            msg_seq = await self._persist_message(session, msg_content)

        ws_event = self._event_to_ws_message(session_id, event)
        if ws_event is not None and msg_content is not None:
            self._tag_persisted(ws_event, msg_seq, msg_content)
        if ws_event is not None:
            if backend is not None:
                ws_event["work_id"] = backend.work_id
            await self._broadcast(ws_event)

    async def _try_steer(self, session: Session, queued: QueuedPrompt) -> bool:
        """Offer a message to the running turn. True if it was accepted.

        Everything that isn't accepted falls through to the normal queue, so
        the message always runs — the only question is whether it reaches the
        agent mid-turn or as the next turn. Refused when the window is shut
        (the turn is finishing), when the backlog is full, or when the message
        carries attachments, which the frame channel doesn't take.
        """
        if queued.attachment_ids or queued.wiki_mode or session._wiki_mode:
            return False
        async with session._steer_lock:
            if self._admission_closed or session._admission_closed or not session._steer_open:
                return False
            if len(session._steer_queue) >= _MAX_PENDING_STEERS:
                logger.info(
                    "session %s: steer backlog full, queueing instead", session.id
                )
                return False
            session._dispatch_order += 1
            queued.order = session._dispatch_order
            queued.priority = session._dispatch_paused and queued.source == "human"
            session._steer_queue.append(queued)
            session._steer_ready.set()
        return True

    async def _steer_writer(self, session: Session, backend: HarnessRun) -> int:
        """The turn's only writer of mid-turn user frames.

        Waits on `_steer_ready` rather than on the event stream, because during
        a long tool call no events arrive and a steer would otherwise sit
        unwritten for exactly as long as the tool runs. Returns when cancelled;
        the count of frames written is tracked on the session so the run loop
        can tell whether anything was delivered after `result`.
        """
        written = 0
        while True:
            await session._steer_ready.wait()
            session._steer_ready.clear()
            while True:
                # The lock is held ACROSS the write, not just around the pop.
                # Closing the window takes the same lock, so a close can never
                # interleave with a write already under way: the frame is
                # either fully delivered while the turn was live, or never
                # written at all. Without that, a half-written frame could land
                # after the CLI went idle and be taken as a whole new turn,
                # whose reply nobody is reading.
                async with session._steer_lock:
                    if not session._steer_open or not session._steer_queue:
                        break
                    queued = session._steer_queue[0]
                    try:
                        await backend.send_user_frame(queued.prompt)
                    except Exception:
                        logger.exception(
                            "failed writing steer for session %s; it will be "
                            "queued as the next turn instead",
                            session.id,
                        )
                        # Left in the queue: the turn's cleanup moves it to the
                        # normal pending queue, so the message still runs.
                        return written
                    session._steer_queue.pop(0)
                    owner = session._settlement
                    if owner is not None:
                        session._inline_requests.setdefault(owner.request_id, []).append(queued)
                written += 1
                # Persist + broadcast only once it's actually been handed over,
                # so the transcript order matches what the engine saw.
                message = MessageContent(
                    role=MessageRole.user, type="text", content=queued.prompt,
                    tool_input={"request_context": {
                        "request_id": queued.request_id, "source": queued.source,
                        "work_id": queued.work_id,
                    }},
                )
                seq = await self._persist_message(session, message)
                await self._broadcast(
                    self._tag_persisted({
                        "type": "steered",
                        "session_id": session.id,
                        "content": queued.prompt,
                        "tool_input": message.tool_input,
                    }, seq, message),
                    user_id=session.user_id,
                )
        return written

    def _run_config(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> RunConfig:
        """Build the per-turn RunConfig from the (freshly-loaded) agent. The
        agent supplies the system prompt, model, built-in MCP set, and tool
        allow/deny policy (agent-refactor.md §5.2). The harness this is handed
        to (`get_harness(session.backend).create_run(config)`) renders it the
        way its profile dictates — no backend-kind branching here."""
        system_prompt: str | None = None
        model: str | None = None
        reasoning_effort: str | None = None
        service_tier: str | None = None
        mcp_servers: list[str] | None = None
        tool_allow: list[str] | None = None
        tool_deny: list[str] | None = None
        if agent:
            system_prompt = agent.get("system_prompt") or None
            model = agent.get("model") or None
            reasoning_effort = agent.get("reasoning_effort") or None
            service_tier = agent.get("service_tier") or None
            servers = agent.get("mcp_servers")
            mcp_servers = list(servers) if servers is not None else None
            tool_allow = _split_tool_list(agent.get("tool_allow"))
            tool_deny = _split_tool_list(agent.get("tool_deny"))
        # `/model` — a per-session override, applied after the agent's so it
        # wins. Both unset means the backend picks, which is the default.
        if session.model:
            model = session.model
        if session.reasoning_effort:
            reasoning_effort = session.reasoning_effort
        if session.service_tier:
            service_tier = session.service_tier

        # Per-agent native memory (docs/plans/memory.md): derive the agent's
        # canonical memory dir and ensure it exists. None when there's no agent
        # → memory wiring is inert. Both harnesses point at this one dir —
        # Claude via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its blurb —
        # so memory never touches CLAUDE_CONFIG_DIR / CODEX_HOME (auth + resume
        # stay put).
        memory_dir: str | None = None
        if session.agent_id:
            from .. import agent_memory

            agent_memory.ensure_agent_dirs(session.agent_id, session.user_id)
            memory_dir = str(
                agent_memory.agent_memory_dir(session.agent_id, session.user_id)
            )

        # Fork first-turn note (session-rewind.md §5.6.4): present while
        # the fork's ephemeral fork_metadata is set (i.e. before its first
        # result clears it), so it appears on turn 1 only — framing, not
        # transcript. The replay block lives in the user channel, not here.
        fork_note: str | None = None
        if session.fork_metadata:
            try:
                fork_note = json.loads(session.fork_metadata).get("first_turn_note")
            except (json.JSONDecodeError, AttributeError):
                fork_note = None

        subagents = (agent or {}).get("subagents") or []
        if not isinstance(subagents, list):
            subagents = []

        return RunConfig(
            session_id=session.id,
            user_id=session.user_id,
            wiki_task_id=session._wiki_task_id,
            wiki_mode=session._wiki_mode,
            work_id=session._work_id,
            system_prompt=system_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            mcp_servers=mcp_servers,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            connectors=connectors or [],
            memory_dir=memory_dir,
            fork_note=fork_note,
            subagents=subagents,
        )

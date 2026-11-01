"""Public command entry and owner-invoked outcome/event conversion helpers.

SessionOwner owns runtime transitions. Captured preparation, Harness I/O and
publication tasks report their original results without mutating Session.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from .. import fork_helpers
from .. import monitor as _monitor
from ..config import settings
from ..diagnostics import exception_diagnostic, safe_diagnostic
from ..harness import (
    HarnessEvent,
    HarnessRun,
    RunConfig,
    get_harness,
)
from ..harness.model_settings import effective_settings
from ..models import (
    FinalAnswerRef,
    MessageContent,
    MessageRole,
    RuntimeMetadata,
    TurnRecovery,
)
from ..monitor import Event as _MonEvent
from ..service_health import ServiceUnavailable
from .base import (
    _BACKEND_RETIRE_TIMEOUT,
    _DELTA_FLUSH_SECONDS,
    PendingQuestion,
    QueuedPrompt,
    Session,
    SessionManagerBase,
    _split_tool_list,
    logger,
)
from .observation import RequestObservation
from .owner import RetainResultNotice, SessionOwner, SessionOwnerUnavailable
from .owner_runtime import (
    CancelRequest,
    CloseExecution,
    CloseWork,
    IdleEvent,
    Interrupt,
    RecoveryDismissed,
    Reset,
    Resume,
    RetireProcess,
    Submit,
    WorkRetirement,
)
from .runtime_types import (
    QuotaExceeded,
    SessionAdmissionBusy,
    SessionAdmissionClosed,
    SessionMailboxFull,
    _Attempt,
    _Decision,
    _Turn,
)
from .submission import (
    RequestCompletion,
    ReturnAdmission,
    ReturnBody,
    ReturnRefused,
    ReturnSkipped,
    ReturnTarget,
    ReturnUnconfirmed,
    SubmissionReceipt,
    observed_input_receipt,
)

if TYPE_CHECKING:
    from ..bg_tasks import BgTaskRecord
    from ..knowledge import KnowledgeContext




class TurnsMixin(SessionManagerBase):

    async def dismiss_recoverable_input(self, session_id: str, client_id: str) -> bool:
        """Keep the confirmed recovery edit available after runtime retirement."""

        if not await self.input_database().dismiss_client_submission(session_id, client_id):
            return False
        session = self.sessions.get(session_id)
        if session is not None:
            owner = self._session_owners.get(id(session))
            if owner is not None and not owner.failed and not owner.sealed:
                try:
                    await owner.call_runtime(RecoveryDismissed(client_id))
                except Exception:
                    # The edit is already committed. A dying original runtime
                    # cannot veto it or be replaced to publish a projection.
                    logger.warning("Saved-input dismissal committed; original Owner notification failed session=%s client=%s",
                                   session_id, client_id, exc_info=True)
        return True

    def dispatch_snapshot(self, session_id: str) -> dict[str, Any]:
        return self._session_dispatch_snapshot(self.sessions[session_id])

    def _session_dispatch_snapshot(self, session: Session) -> dict[str, Any]:
        return {
            "generation": self._dispatch_generation,
            "revision": session._dispatch_revision,
            "paused": session._dispatch_paused,
            "active_request_id": (
                session._run.request.request_id if session._run.request else None
            ),
            "items": [{
                "request_id": item.request_id, "source": item.source,
                "summary": item.summary, "priority": item.priority,
            } for item in session._pending_queue],
        }

    def _dispatch_event(self, session: Session) -> dict[str, Any]:
        session._dispatch_revision += 1
        return {
            "type": "dispatch_state", "session_id": session.id,
            "dispatch_state": self._session_dispatch_snapshot(session),
        }


    async def _recovery_frame(
        self, session: Session, *, code: str, message: str,
        phase: Literal["diagnostic", "retrying", "recovered"] = "retrying",
        attempt: int | None = None, maximum: int | None = None,
        delay: float | None = None,
        source: HarnessEvent | None = None,
    ) -> dict[str, Any]:
        settlement = session._run.settlement
        payload = TurnRecovery(
            request_id=settlement.request_id if settlement else "untracked",
            phase=phase, code=code, message=safe_diagnostic(message) or "Backend notification",
            attempt=attempt, maximum=maximum, delay=delay,
        )
        stored = MessageContent(role=MessageRole.system, type="recovery", content=payload.model_dump(),
            runtime_metadata=RuntimeMetadata.model_validate(source.runtime_metadata) if source and source.runtime_metadata else None)
        if settlement and settlement.attempt_id:
            stored.tool_input = {"attempt_context": {"request_id": settlement.request_id,
                                                      "attempt_id": settlement.attempt_id}}
        if source is not None and source.error_details is not None:
            stored.tool_input = {**(stored.tool_input or {}), "provider_error": source.error_details}
        seq = await self._persist_message(session, stored)
        return self._tag_persisted(
            {"type": "recovery", "session_id": session.id,
             "request_id": payload.request_id, "content": stored.content,
             "attempt_id": settlement.attempt_id if settlement else None,
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
        sequence: int | None = None,
    ) -> int | None:
        """Persist and return the assigned seq.

        Callers tag broadcast/yield events with this seq so clients can
        dedupe against the snapshot returned by GET /api/sessions/{id}
        after a reconnect.

        `git_head` / `git_status_clean` are the turn-start anchor captured for
        user-message rows (session-rewind.md §5.6.3); None elsewhere.
        """
        self._require_session_writer(session)
        assert self.db is not None
        seq = session._next_message_seq if sequence is None else sequence
        session._next_message_seq = max(session._next_message_seq, seq + 1)
        # Decided here rather than in the DB layer so the caller's own
        # MessageContent carries it onto the WS event as well as into the row.
        msg.created_at = msg.created_at or datetime.now(UTC).isoformat()
        request_context = (msg.tool_input or {}).get("request_context", {})
        request_id = request_context.get("request_id") if isinstance(request_context, dict) else None
        record = (
            partial(self.db.record_request, request_id=request_id)
            if msg.role == MessageRole.user and isinstance(request_id, str)
            and isinstance(request_context.get("knowledge"), dict)
            else self.db.append_message
        )
        await record(
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
        session._message_count += 1
        return seq


    def _own_task(self, task: asyncio.Task[Any]) -> None:
        self._owned_tasks.add(task)

        def observed(done: asyncio.Task[Any]) -> None:
            self._owned_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                assert error is not None
                logger.error("Session-owned task %s failed error=%s\n%s", done.get_name(), type(error).__name__,
                             exception_diagnostic(error, secrets=(settings.auth_token,)))

        task.add_done_callback(observed)


    @staticmethod
    def _new_receipt(session: Session, queued: QueuedPrompt, disposition: str = "accepted") -> SubmissionReceipt:
        future: asyncio.Future[RequestCompletion] = asyncio.get_running_loop().create_future()
        session._request_completions[queued.request_id] = future
        observation = RequestObservation(queued.request_id, queued.work_id, asyncio.get_running_loop().create_future())
        session._request_observations[queued.request_id] = observation
        return SubmissionReceipt(disposition, queued.request_id, future, observation)  # type: ignore[arg-type]

    def _finish_receipt(self, session: Session, completion: RequestCompletion) -> None:
        future = session._request_completions.pop(completion.input_request_id, None)
        if future is None or future.done():
            return
        future.set_result(completion)
        # Register the original observer operations synchronously, before
        # drain can see a done receipt without its consumer handoff.
        for callback in tuple(self._completion_callbacks.values()):
            self._own_task(asyncio.create_task(
                callback(session.id, completion), name=f"request-completion-{session.id}",
            ))



    async def _retire_backend(self, session: Session, backend: HarnessRun, *, reason: str = "process_ended") -> asyncio.Task[None]:
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        failed = await self._failed_owner_retirement(owner)
        if failed is not None:
            return failed
        try:
            return await owner.call_runtime(RetireProcess(backend, reason))
        except SessionOwnerUnavailable:
            failed = await self._failed_owner_retirement(owner)
            if failed is None:
                raise
            return failed

    async def _await_backend_retirement(self, session: Session, *, timeout: float | None = None) -> None:
        owner = self._session_owners.get(id(session))
        failed = await self._failed_owner_retirement(owner) if owner is not None else None
        tasks = (failed,) if failed is not None else tuple(resource.task for resource in session._run.retirements.values())
        if not tasks:
            return
        limit = _BACKEND_RETIRE_TIMEOUT if timeout is None else min(_BACKEND_RETIRE_TIMEOUT, max(0.0, timeout))
        _, pending = await asyncio.wait(tasks, timeout=limit)
        if pending:
            raise RuntimeError("Previous backend retirement remains unconfirmed")
        for task in tasks:
            await asyncio.shield(task)

    def close_admission(self) -> None:
        """Seal request admission synchronously before other owners stop."""
        self._admission_closed = True

    async def drain_turns(self, timeout: float = 5.0) -> int:
        self.close_admission()
        owners = tuple(self._session_owners.values())
        active = sum(owner.runtime.current is not None for owner in owners)
        async def close_original(owner: Any) -> None:
            if owner.failed:
                return
            if not owner.sealed:
                try:
                    await owner.call_runtime(CloseExecution("shutdown"))
                    return
                except SessionOwnerUnavailable:
                    if not owner.sealed or owner.failed:
                        raise
            # Removal already closed admission and retired command effects.
            # Its original publication may still be completing; join it
            # without submitting another command to the sealed owner.
            await owner.close()
        async with asyncio.timeout(max(0, timeout)):
            await asyncio.gather(*(close_original(owner) for owner in owners))
            for owner in owners:
                if owner.failed:
                    if owner.cleanup is not None:
                        await asyncio.shield(owner.cleanup)
                    continue
                execution = owner.runtime.current
                if execution is not None:
                    await asyncio.shield(execution.completed)
                await self._await_backend_retirement(owner.session)
            while any(not task.done() for task in self._owned_tasks):
                await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._owned_tasks)
                                       if task is not asyncio.current_task()), return_exceptions=True)
        return active

    def running_turns_for(self, user_id: str | None) -> int:
        return sum(1 for session in self.sessions.values()
                   if session.user_id == user_id and self.session_is_executing(session))

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
        if self.session_is_executing(session):
            return  # this one is already counted; the message will queue
        if session._run.retirements:
            return  # Rejected before spawn; the existing writer owns this slot.
        if self.running_turns_for(session.user_id) >= cap:
            raise QuotaExceeded(
                f"You already have {cap} turns running. Wait for one to finish, "
                "or interrupt it."
            )

    @staticmethod
    def _eligible_request(session: Session) -> int | None:
        if session._admission_closed:
            return None
        return next((
            i for i, item in enumerate(session._pending_queue)
            if not session._dispatch_paused or item.priority
        ), None)


    async def start_message(
        self, session_id: str, prompt: str, attachment_ids: list[str] | None = None, *,
        steerable: bool = False, wiki_mode: str | None = None, source: str = "human",
        delivery_id: str | None = None, work_id: str | None = None,
        question_session_id: str | None = None, question_id: str | None = None,
        request_id: str | None = None, trusted_request: str | None = None,
        knowledge_context: KnowledgeContext | None = None,
        client_request_id: str | None = None, recovery_of_client_id: str | None = None,
        schedule: dict[str, Any] | None = None,
    ) -> SubmissionReceipt:
        database = self.input_database()
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        if self._admission_closed:
            from ..submission_models import SubmissionConflict
            from .client_submissions import input_digest

            identity = client_request_id or (f"{source}:{delivery_id}" if delivery_id else request_id)
            if identity:
                row = await database.get_client_submission(session.id, identity)
                if row is not None:
                    payload: dict[str, Any] = {"content": prompt, "attachment_ids": list(attachment_ids or []), "wiki_mode": wiki_mode}
                    if recovery_of_client_id:
                        payload["recovery_of_client_id"] = recovery_of_client_id
                    if row["payload_sha256"] != input_digest(payload):
                        raise SubmissionConflict("This submission identity already belongs to different input.")
                    return observed_input_receipt(row, session)
            return SubmissionReceipt.closed(request_id or uuid.uuid4().hex, "shutdown")
        from .client_submissions import _files

        if session.user_id is None:
            raise ValueError("The session has no attachment owner")
        files = await asyncio.to_thread(_files, session_id, list(attachment_ids or []), user_id=session.user_id)
        queued = QueuedPrompt(
            prompt, list(attachment_ids or []), wiki_mode=wiki_mode, source=source,
            delivery_id=delivery_id, work_id=work_id, question_session_id=question_session_id,
            question_id=question_id, trusted_request=trusted_request, knowledge_context=knowledge_context,
            **({"request_id": request_id} if request_id else {}),
        )
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        return await owner.call_runtime(Submit(queued, steerable, client_request_id, tuple(files), recovery_of_client_id, schedule))

    async def source_receipt(self, source: str, delivery_id: str, *, session_id: str | None = None) -> tuple[str, SubmissionReceipt] | None:
        """Confirm the original acceptance even after its target or caller retires."""
        database = self.input_database()
        row = await database.get_source_submission(source, delivery_id, session_id=session_id)
        if row is None:
            return None
        session = self.sessions.get(row["session_id"])
        return row["session_id"], observed_input_receipt(row, session)

    async def admit_return(
        self, target: ReturnTarget, *, prepare: Callable[[], Awaitable[ReturnBody | ReturnSkipped]],
    ) -> ReturnAdmission:
        """Own the complete receipt-first handoff; the source owns retry policy.

        Preparation is pure, may repeat across callers, and holds no mailbox,
        lifecycle lock or transaction. Wake-up and input COMMIT stay separate.
        """
        from ..db.applications import ApplicationDeleting

        async def observe() -> SubmissionReceipt | ReturnUnconfirmed | None:
            try:
                existing = await self.source_receipt(target.source, target.delivery_id, session_id=target.session_id)
                return existing[1] if existing is not None and existing[1].accepted else None
            except (SessionOwnerUnavailable, SessionMailboxFull, TimeoutError, ConnectionError) as error:
                return ReturnUnconfirmed(error)

        async def reconcile(result: ReturnRefused | ReturnUnconfirmed) -> ReturnAdmission:
            observation = await observe()
            if isinstance(observation, ReturnUnconfirmed):
                # Keep the original uncertain handoff's cause when observation
                # fails too. No lookup failure permits a new submission.
                return result if isinstance(result, ReturnUnconfirmed) else observation
            return observation or result

        async def eligibility() -> ReturnRefused | None:
            try:
                observation = await self._task_return_eligibility(
                    target.owned, session_id=target.session_id, work_id=target.work_id,
                )
                return observation if isinstance(observation, ReturnRefused) else None
            except (SessionAdmissionClosed, ApplicationDeleting) as error:
                return ReturnRefused("admission_closed", error)
            except SessionAdmissionBusy as error:
                return ReturnRefused("busy", error)

        try:
            existing = await observe()
            if existing is not None:
                return existing
            refusal = await eligibility()
        except (SessionOwnerUnavailable, SessionMailboxFull, TimeoutError, ConnectionError) as error:
            return ReturnUnconfirmed(error)
        if refusal is not None:
            return await reconcile(refusal)

        body = await prepare()  # Preparation errors and cancellation retain their identity.
        try:
            existing = await observe()
            if existing is not None:
                return existing
            if isinstance(body, ReturnSkipped):
                return ReturnRefused("source_withdrawn")
            refusal = await eligibility()
            if refusal is not None:
                return await reconcile(refusal)
            resolved = await self._restore_task_return(target.owned, session_id=target.session_id, work_id=target.work_id)
            if resolved is None:
                return await reconcile(ReturnRefused("destination_unavailable"))
            # Restoration may wait for another lifecycle command. Revalidate
            # the captured binding before handing off to the original Owner.
            refusal = await eligibility()
            if refusal is not None:
                return await reconcile(refusal)
            _, context = resolved
            receipt = await self.start_message(
                target.session_id, body.prompt, source=target.source, delivery_id=target.delivery_id,
                work_id=target.work_id, knowledge_context=context,
                question_session_id=body.question_session_id, question_id=body.question_id,
            )
            if receipt.accepted:
                return receipt
            result: ReturnRefused | ReturnUnconfirmed = ReturnRefused("admission_closed")
        except (SessionMailboxFull, QuotaExceeded) as error:
            result = ReturnRefused("capacity", error)
        except SessionAdmissionBusy as error:
            result = ReturnRefused("busy", error)
        except (SessionAdmissionClosed, ApplicationDeleting) as error:
            result = ReturnRefused("admission_closed", error)
        except (SessionOwnerUnavailable, TimeoutError, ConnectionError) as error:
            logger.warning("Return acknowledgment unavailable source=%s delivery=%s\n%s",
                           target.source, target.delivery_id, exception_diagnostic(error))
            result = ReturnUnconfirmed(error)
        return await reconcile(result)

    async def start_one_shot(self, schedule: dict[str, Any], prompt: str, context: KnowledgeContext) -> tuple[str, SubmissionReceipt]:
        """Prepare the target; Owner commits target, input and consumption once."""
        from ..db.schedules import ScheduleChangedError

        delivery_id = f"schedule:{schedule['id']}:{schedule['run_at']}"
        existing = await self.source_receipt("schedule", delivery_id)
        if existing is not None:
            return existing
        origin = self.sessions.get(schedule.get("origin_session_id"))
        if origin is not None:
            receipt = await self.start_message(origin.id, prompt, source="schedule", delivery_id=delivery_id,
                                               knowledge_context=context, schedule=schedule)
            return origin.id, receipt

        async def admit():
            candidate = await self._create_session(schedule["agent_id"], origin="schedule", persist=False)
            owner = self._session_owner(candidate)
            try:
                request = QueuedPrompt(prompt, [], source="schedule", delivery_id=delivery_id, knowledge_context=context)
                try:
                    receipt = await owner.call_runtime(Submit(request, schedule=schedule))
                except ScheduleChangedError:
                    existing = await self.source_receipt("schedule", delivery_id)
                    if existing is None:
                        raise
                    return existing
                return candidate.id, receipt
            finally:
                if candidate.id not in self.sessions:
                    await owner.close()

        return await self._run_agent_operation(schedule["agent_id"], admit)

    async def await_session_execution(self, session_id: str, *, timeout: float | None = None) -> None:
        """Wait on captured execution/retirement resources, never a driver flag."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        owner = self._session_owners.get(id(session))
        current = owner.runtime.current if owner is not None else None
        async def wait() -> None:
            if current is not None:
                await asyncio.shield(current.retired)
            await self._await_backend_retirement(session)
        if timeout is None:
            await wait()
        else:
            async with asyncio.timeout(timeout):
                await wait()

    def session_is_executing(self, session: Session) -> bool:
        owner = self._session_owners.get(id(session))
        return owner is not None and owner.runtime.busy




    async def resume_dispatch(self, session_id: str, *, resume_queue: bool = True) -> bool:
        session = self.sessions.get(session_id)
        return bool(session is not None and await self._session_owner(session).call_runtime(Resume(resume_queue)))

    def has_pending_or_active_work(self, session_id: str, work_id: str) -> bool:
        """Whether dispatch accepted this work before its caller stopped waiting."""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        current = session._run.request
        return (current is not None and current.work_id == work_id) or any(
            request.work_id == work_id for request in (*session._pending_queue, *session._steer_queue)
        )

    def work_has_resources(self, work_id: str) -> bool:
        """Original work's queued, preparing, held or retiring resources.

        Absence is not a retirement proof. Callers also need their successful
        close_work receipt (or its durable domain-owned acknowledgment).
        """
        return any(owner.runtime.owns_work(work_id) for owner in tuple(self._session_owners.values()))

    def session_has_resources(self, session_id: str, request_ids: set[str] | None = None) -> bool:
        """Conservative legacy scope where no original work identity was saved."""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        requests = (*session._pending_queue, *session._steer_queue,
                    *((session._run.request,) if session._run.request else ()))
        return (any(request_ids is None or request.request_id in request_ids for request in requests)
                or bool(session._run.retirements))

    async def close_work(self, work_id: str) -> WorkRetirement:
        from ..mcp_identity import close_work
        close_work(work_id)
        owners = tuple(owner for owner in self._session_owners.values() if owner.runtime.owns_work(work_id))
        references = await asyncio.gather(*(self._close_original_work(owner, work_id) for owner in owners))
        return WorkRetirement(tuple(resource for reference in references for resource in reference.resources))

    async def _close_original_work(self, owner: SessionOwner, work_id: str) -> WorkRetirement:
        retirement = await self._failed_owner_retirement(owner)
        if retirement is None:
            try:
                return await owner.call_runtime(CloseWork(work_id))
            except SessionOwnerUnavailable:
                retirement = await self._failed_owner_retirement(owner)
                if retirement is None:
                    raise
        return WorkRetirement((retirement,))

    async def finish_work_cancellation(self, affected: list[WorkRetirement]) -> None:
        await asyncio.gather(*(asyncio.shield(resource) for reference in affected for resource in reference.resources))

    async def retain_result_notice(self, session_id: str, delivery_id: str, content: str) -> bool:
        """Save one visible material-only notice without admitting new work."""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        return await self._session_owner(session).call(RetainResultNotice(delivery_id, content))

    async def deliver_bg_result(self, rec: BgTaskRecord) -> ReturnAdmission:
        """Adapt original BG material; admission and retry have separate owners."""
        from ..bg_tasks import render_delivery_prompt
        from ..knowledge import decode_owned

        async def prepare() -> ReturnBody | ReturnSkipped:
            if rec.delivery_disposition is not None:
                return ReturnSkipped("The source has stopped new submission; observe its original receipt only")
            return ReturnBody(render_delivery_prompt(rec))

        result = await self.admit_return(
            ReturnTarget(rec.session_id, "bg", f"bg:{rec.id}", rec.work_id, decode_owned(rec.knowledge_context)),
            prepare=prepare,
        )
        if (isinstance(result, ReturnRefused) and result.reason in {"binding_unavailable", "admission_closed"}
                and rec.delivery_disposition is None):
            try:
                await self.retain_result_notice(
                    rec.session_id, f"bg:{rec.id}",
                    f"Background task {rec.id}: output retained in the task record. "
                    "The original request contract is missing, closed or could not be verified; no automatic continuation was started.",
                )
            except ServiceUnavailable:
                raise
            except Exception:
                logger.exception("Background task %s auxiliary delivery notice failed", rec.id)
        return result



    async def interrupt(self, session_id: str) -> bool:
        _monitor.record(_MonEvent(kind="turn_interrupt", session_id=session_id))
        session = self.sessions.get(session_id)
        if session is None:
            return False
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        return bool(await owner.call_runtime(Interrupt()))

    async def cancel_request(
        self, session_id: str, request_id: str, *, cause: Literal["user", "shutdown"] = "user",
    ) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        cancelled, original_retirement = await owner.call_runtime(CancelRequest(request_id, cause))
        if original_retirement is not None and not owner.publisher.in_callback():
            await asyncio.shield(original_retirement)
        return bool(cancelled)

    async def reset_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        await owner.call_runtime(Reset())
        if not owner.publisher.in_callback():
            await self.await_session_execution(session_id)

    # ------------------------------------------------------------------ #
    # One CLI invocation: acquire a process, start it, retire it
    # ------------------------------------------------------------------ #






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
            and attempt.result is not None
            and not attempt.failed
            and attempt.timeout is None
        )


    # ------------------------------------------------------------------ #
    # One event off the stream
    # ------------------------------------------------------------------ #

    def _coalesce_delta(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> dict[str, Any] | None:
        """Buffer a token delta, emitting a frame at most every flush window."""
        flushed = None
        if attempt.delta_metadata != event.runtime_metadata:
            flushed = self._flush_text_deltas(session.id, attempt.delta_buf, attempt.delta_metadata)
            attempt.delta_metadata = event.runtime_metadata
        if event.content:
            attempt.delta_buf.append(event.content)
        if flushed is not None:
            return flushed
        now = time.monotonic()
        if now - attempt.delta_last_flush < _DELTA_FLUSH_SECONDS:
            return None
        attempt.delta_last_flush = now
        return self._flush_text_deltas(session.id, attempt.delta_buf, attempt.delta_metadata)

    async def _remember_resume_id(self, session: Session, resume_id: str) -> None:
        """Persist the engine-side conversation id this session resumes from."""
        if session.claude_session_id == resume_id:
            return
        session.claude_session_id = resume_id
        if self.db:
            await self.db.update_session_field(
                session.id, claude_session_id=resume_id
            )

    async def _note_question_request(
        self, session: Session, question_id: str, tool_input: dict[str, Any] | None, *, attempt_id: str,
    ) -> PendingQuestion | None:
        """Track pending question state for reconnect re-render."""
        return await self._record_native_question(
            session, question_id, (tool_input or {}).get("questions") or [], attempt_id=attempt_id,
        )

    async def _handle_result_event(
        self, session: Session, attempt: _Attempt, event: HarnessEvent
    ) -> None:
        """The turn said it was done: record it and settle the session."""
        attempt.result = event
        _monitor.record(_MonEvent(
            kind="turn",
            session_id=session.id,
            agent_id=session.agent_id,
            backend=session.backend,
            duration_ms=event.duration_ms,
            ok=not event.is_error,
            detail={"cost": event.cost, "num_turns": event.num_turns,
                    "duration_scope": "attempt", "attempt_id": attempt.attempt_id,
                    "request_id": session._run.settlement.request_id if session._run.settlement else None},
        ))
        # Shut the steering window first: past this point the CLI is idle, and
        # a frame written now would start a fresh turn rather than steer this
        # one.
        session._steer_open = False
        # Update the resume id in case the CLI reissued a different one
        # mid-stream.
        if event.session_id:
            attempt.conversation_id = event.session_id
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
        if event.type == "model_measurement":
            # The verified native adapter emits metadata only. Keep these
            # observations out of transcripts, activity signals and WS frames.
            if session.backend == "codex" and session._run.backend is attempt.backend:
                detail = dict(event.raw)
                kind = detail.pop("kind", None)
                completed = detail.pop("completed_at", None)
                if kind in {"codex_response", "codex_turn_latency"} and isinstance(completed, (int, float)):
                    detail.update(attempt_id=attempt.attempt_id,
                                  request_id=session._run.settlement.request_id if session._run.settlement else None)
                    _monitor.record(_MonEvent(kind=kind, ts=completed, session_id=session.id,
                                              agent_id=session.agent_id, backend="codex",
                                              duration_ms=event.duration_ms, detail=detail))
            return []
        if event.type == "subagent" and session._run.backend is not attempt.backend and not (event.runtime_metadata or {}).get("native"):
            return []
        from ..harness.tool_identity import ToolIdentityTracker
        tracker = getattr(attempt.backend, "_tool_identity_tracker", None)
        if tracker is None:
            tracker = ToolIdentityTracker()
            attempt.backend._tool_identity_tracker = tracker
        tracker.normalize(event, session._run.settlement.request_id if session._run.settlement else "untracked",
                          attempt.attempt_id)
        native = (event.runtime_metadata or {}).get("native")
        if native and attempt.final_answer is None:
            attempt.final_answer = FinalAnswerRef(thread_id=native["root_thread_id"], turn_id=native["root_turn_id"])
        if event.type == "text_delta":
            flushed = self._coalesce_delta(session, attempt, event)
            return [flushed] if flushed is not None else []

        frames: list[dict[str, Any]] = []
        # Anything else ends the current delta run: flush it first so the
        # partial text can never arrive after the completed block that
        # supersedes it.
        flushed = self._flush_text_deltas(session.id, attempt.delta_buf, attempt.delta_metadata)
        if flushed is not None:
            attempt.delta_last_flush = time.monotonic()
            frames.append(flushed)

        # session_started arrives on the CLI's init event, before any tool
        # work. Persist the resume id immediately so the recovery path can use
        # it even if the bug suppresses `result`. Internal event — never
        # persisted or broadcast.
        if event.type == "session_started" and event.session_id:
            attempt.conversation_id = event.session_id
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

        # Persist whichever message shape this event maps to. The returned seq
        # goes onto the WS event so reconnecting clients can dedupe against
        # their snapshot.
        if event.type == "result":
            await self._handle_result_event(session, attempt, event)
            if event.is_error:
                self._capture_error_text(attempt, event)
                if (event.runtime_metadata or {}).get("native") and (event.content or event.error_details):
                    frames.append(await self._recovery_frame(
                        session, code=event.error_code or "backend_diagnostic", phase="diagnostic",
                        message=event.content or "Backend notification", source=event,
                    ))
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
            # Keep the native frame in this reader's ordered prefix until the
            # complete runtime stream moves to the publisher. Registration and
            # its deadline belong to the question owner already.
            pending_question = await self._note_question_request(
                session, event.tool_use_id, event.tool_input, attempt_id=attempt.attempt_id,
            )
        msg_content = self._event_to_message_content(event)
        msg_seq: int | None = None
        if msg_content is not None:
            source = (event.runtime_metadata or {}).get("native")
            if event.type == "text" and (not source or source["thread_id"] == source["root_thread_id"]):
                msg_content.tool_input = {**(msg_content.tool_input or {}), "attempt_context": {
                    "request_id": session._run.settlement.request_id if session._run.settlement else "untracked",
                    "attempt_id": attempt.attempt_id,
                }}
            if pending_question is not None:
                msg_content.tool_input = {
                    **(msg_content.tool_input or {}),
                    "deadline": pending_question.deadline,
                    "request_id": pending_question.request_id,
                    "attempt_id": pending_question.attempt_id,
                    "work_id": pending_question.work_id,
                }
            msg_seq = await self._persist_message(session, msg_content)
            if (event.type == "text" and msg_seq is not None and source
                    and source["thread_id"] == source["root_thread_id"]
                    and source["turn_id"] == source["root_turn_id"] and source.get("phase") == "final_answer"):
                assert attempt.final_answer is not None
                attempt.final_answer.message_seqs.append(msg_seq)
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
        terminal event's content/raw plus the CLI's stderr. Display summaries
        are bounded separately; classification must also see trailing evidence.

        getattr: a real HarnessRun exposes stderr_text; lightweight test and
        backend stand-ins may not.
        """
        stderr_text = getattr(attempt.backend, "stderr_text", "") or ""
        return attempt.error_text + "\n" + stderr_text

    async def _timeout_decision(
        self, session: Session, turn: _Turn, attempt: _Attempt
    ) -> _Decision | None:
        """The logical deadline bounds preparation and continuation alike."""
        if attempt.timeout is None:
            return None
        reason, limit = attempt.timeout
        overall = settings.turn_max_seconds
        ended_at = (attempt.backend.admission_closed_at
                    if getattr(attempt.backend, "captures_retirement_output", False) else None)
        if overall > 0 and self._remaining_turn_time(turn, at=ended_at) == 0:
            reason, limit = "overall", overall
        maximum = max(0, settings.turn_idle_max_retries)
        recovery = turn.settlement.recovery
        next_prompt = self._recovery_prompt(turn, attempt, idle=True)
        if (
            reason == "idle"
            and recovery["idle"] < maximum
            and next_prompt is not None
        ):
            recovery["idle"] += 1
            return _Decision.again(
                next_prompt,
                await self._surface_idle_retry(
                    session, limit=limit, attempt=recovery["idle"],
                    max_attempts=maximum,
                ),
            )
        return _Decision.stop(
            await self._surface_turn_timeout(
                session, reason=reason, limit=limit, backend=turn.harness.backend,
                retries=recovery["idle"],
            )
        )

    @staticmethod
    def _recovery_prompt(turn: _Turn, attempt: _Attempt, *, idle: bool = False) -> str | None:
        """No output is never evidence of no effect.

        A preparation refusal proves no input submission. Otherwise only a
        captured conversation and the adapter's continuation contract permit
        another input, with no exactly-once claim for external operations.
        """
        if attempt.preparation_failed or getattr(attempt.backend, "input_may_have_run", True) is False:
            return attempt.prompt
        if not (turn.harness.profile.can_continue and turn.continuation_id):
            return None
        prefix = "The previous attempt stopped after a period without backend activity. " if idle else ""
        return prefix + (
            "Continue the user's unfinished request from this conversation. "
            "Check existing files and tool outcomes before repeating operations: "
            "interrupted tools may already have changed things. Keep existing "
            "delegations and background jobs; do not duplicate them."
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
        self, session: Session, turn: _Turn, attempt: _Attempt, blob: str
    ) -> _Decision | None:
        """Lost history permits fresh preparation only before submission."""
        recovery = turn.settlement.recovery
        if not (
            session.claude_session_id
            and recovery["stale_session"] < 1
            and turn.harness.is_stale_session_error(blob)
        ):
            return None
        if (not attempt.preparation_failed and getattr(attempt.backend, "input_may_have_run", True)
                or turn.continuation_id is not None):
            return _Decision.stop({
                "code": "recovery_unproven",
                "message": "Native conversation history is unavailable. The request may have changed external state; "
                           "automatic replay was stopped. Inspect existing effects before continuing. " + blob.strip(),
            })
        recovery["stale_session"] += 1
        logger.warning(
            "Session %s: resume id is gone from the engine; "
            "clearing it and starting a fresh conversation",
            session.id,
        )
        session.claude_session_id = None
        if self.db:
            await self.db.update_session_field(session.id, claude_session_id=None)
        # Don't restart cold: Octopus still has the whole conversation (the
        # engine's transcript is a cache of ours, not the record), so replay
        # its tail into this turn through the same channel a fork uses when its
        # backend can't resume natively. The model continues the conversation
        # instead of appearing to forget it.
        replayed = 0
        omitted = 0
        recovery_prompt = attempt.prompt
        if self.db:
            history = [
                MessageContent(**m) for m in await self.db.load_messages(session.id)
            ]
            kept, omitted = fork_helpers.select_lost_history(history)
            replayed = len(kept)
            if kept:
                recovery_prompt = fork_helpers.wrap_for_lost_history(attempt.prompt, kept, omitted=omitted)
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
        self, session: Session, turn: _Turn, attempt: _Attempt, blob: str, *, error_code: str | None = None
    ) -> _Decision | None:
        """Transient classification supplies a budget, never replay permission."""
        if not turn.harness.is_transient_error(blob, code=error_code):
            return None
        recovery = turn.settlement.recovery
        next_prompt = self._recovery_prompt(turn, attempt)
        can_retry = recovery["transient"] < self._MAX_TRANSIENT_RETRIES and next_prompt is not None
        if not can_retry:
            return _Decision.stop(
                await self._surface_transient_exhausted(
                    session,
                    backend=turn.harness.backend,
                    attempts=recovery["transient"],
                )
            )

        recovery["transient"] += 1
        delay = self._TRANSIENT_RETRY_BASE_DELAY * (2 ** (recovery["transient"] - 1))
        logger.warning(
            "Session %s: transient backend error; retrying in "
            "%.1fs (attempt %d/%d, resume=%s)",
            session.id,
            delay,
            recovery["transient"],
            self._MAX_TRANSIENT_RETRIES,
            bool(turn.continuation_id),
        )
        assert next_prompt is not None
        return _Decision.again(
            next_prompt,
            await self._surface_transient_retry(
                session,
                attempt=recovery["transient"],
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
        if attempt.result is not None:
            return _Decision.stop()
        if not turn.harness.premature_exit_recovery:
            # Codex opts out of this Claude-specific premature-exit workaround;
            # its other recovery still follows the common evidence contract.
            return _Decision.stop()
        recovery = turn.settlement.recovery
        if recovery["premature_exit"] >= self._MAX_RECOVERY_ATTEMPTS:
            logger.warning(
                "Session %s: CLI premature-exit retry budget exhausted; "
                "giving up on this turn", session.id
            )
            return _Decision.stop()
        if not attempt.saw_tool_use:
            return _Decision.stop()
        if not (turn.harness.profile.can_continue and turn.continuation_id):
            # No resume id captured (init never arrived) — we can't respawn
            # into the same conversation.
            return _Decision.stop()

        recovery["premature_exit"] += 1
        logger.warning(
            "Session %s: detected CLI premature-exit after tool_use; "
            "auto-respawning with 'continue' (attempt %d/%d)",
            session.id, recovery["premature_exit"], self._MAX_RECOVERY_ATTEMPTS,
        )
        marker_event = await self._recovery_frame(
            session, code="premature_exit", message="(auto-resumed after CLI exited mid-turn)",
            attempt=recovery["premature_exit"], maximum=self._MAX_RECOVERY_ATTEMPTS,
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
        if getattr(attempt.backend, "protocol_issues", {}).get("fatal_protocol_error"):
            return _Decision.stop({"code": "protocol_failed", "message": "Backend protocol ownership could not be established; execution stopped."})
        ended_at = (attempt.backend.admission_closed_at
                    if getattr(attempt.backend, "captures_retirement_output", False) else None)
        if self._remaining_turn_time(turn, at=ended_at) == 0 and attempt.timeout is None:
            attempt.watchdog["tripped"] = ("overall", settings.turn_max_seconds)
            if session._run.backend is attempt.backend:
                self._session_owners[id(session)].runtime.retire(attempt.backend, "parent_timeout")
        elif (ended_at is not None and attempt.timeout is None and settings.turn_idle_timeout_seconds > 0
              and not attempt.watchdog.get("waiting_questions")
              and ended_at - attempt.watchdog["last"] >= settings.turn_idle_timeout_seconds):
            attempt.watchdog["tripped"] = ("idle", settings.turn_idle_timeout_seconds)
        decision = await self._timeout_decision(session, turn, attempt)
        if decision is not None:
            return decision

        if attempt.failed:
            blob = self._failure_blob(attempt)
            decision = await self._auth_decision(session, turn, blob, error_code=attempt.failure_code)
            if decision is not None:
                return decision
            if attempt.preparation_failed:
                decision = await self._stale_session_decision(session, turn, attempt, blob)
                if decision is not None:
                    return decision
                decision = await self._transient_decision(session, turn, attempt, blob, error_code=attempt.failure_code)
                if decision is not None:
                    return decision
                return _Decision.stop({"code": "preparation_failed"})
            if getattr(attempt.backend, "protocol_issues", None):
                return _Decision.stop({"code": "protocol_incomplete"})
            decision = await self._stale_session_decision(session, turn, attempt, blob)
            if decision is not None:
                return decision
            decision = await self._transient_decision(
                session, turn, attempt, blob, error_code=attempt.failure_code,
            )
            if decision is not None:
                return decision

        return await self._premature_exit_decision(session, turn, attempt)

    @staticmethod
    def _remaining_turn_time(turn: _Turn, *, at: float | None = None) -> float | None:
        limit = settings.turn_max_seconds
        return max(0.0, limit - ((time.monotonic() if at is None else at) - turn.started_at)) if limit > 0 else None



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
        return run

    async def _handle_idle_event(
        self, session_id: str, event: HarnessEvent, *, backend: HarnessRun | None = None,
        request_id: str | None = None, attempt_id: str | None = None,
        run_lease_id: str | None = None, work_id: str | None = None,
    ) -> None:
        session = self.sessions.get(session_id)
        if session is None or backend is None:
            return
        owner = self._session_owners.get(id(session))
        if owner is None or owner.failed:
            return
        context = event.runtime_metadata or {}
        await owner.call_runtime(IdleEvent(
            backend, event, context.get("request_id") or request_id,
            context.get("attempt_id") or attempt_id,
            run_lease_id or getattr(backend, "run_lease_id", None), work_id or getattr(backend, "work_id", None),
        ))



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
        effective = effective_settings(session, agent)
        model = effective["model"]
        reasoning_effort = effective["reasoning_effort"]
        service_tier = effective["service_tier"]

        # Per-agent native memory (docs/implemented/memory.md): derive the agent's
        # canonical memory dir. Managed preparation provisions it outside the
        # owner before constructing this configuration. No agent means that
        # → memory wiring is inert. Both harnesses point at this one dir —
        # Claude via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its blurb —
        # so memory never touches CLAUDE_CONFIG_DIR / CODEX_HOME (auth + resume
        # stay put).
        memory_dir: str | None = None
        if session.agent_id:
            from .. import agent_memory

            memory_dir = str(
                agent_memory.agent_memory_dir(session.agent_id, session.user_id)
            )

        # Fork first-turn note (session-rewind.md §5.6.4): present while
        # the fork's ephemeral fork_metadata is set (i.e. before its first
        # result clears it). Delivered as trusted turn context rather than
        # stable controller rules. The replay block lives in the user channel.
        fork_note: str | None = None
        if session.fork_metadata:
            try:
                fork_note = json.loads(session.fork_metadata).get("first_turn_note")
            except (json.JSONDecodeError, AttributeError):
                fork_note = None

        subagents = (agent or {}).get("subagents") or []
        if not isinstance(subagents, list):
            subagents = []

        from ..knowledge import render

        knowledge_instructions = render(session._run.knowledge_context, original=False)

        database, user_id = self.db, session.user_id
        credential_id, backend = effective["credential_id"], session.backend

        async def authorize_spawn() -> None:
            assert database is not None, "Execution requires its admitted input database"
            await database.require_run_authority(user_id, credential_id, backend)

        return RunConfig(
            session_id=session.id,
            transport=settings.codex_transport if session.backend == "codex" else None,
            credential_id=credential_id,
            user_id=session.user_id,
            wiki_task_id=session._run.wiki_task_id,
            wiki_mode=session._run.wiki_mode,
            work_id=session._run.work_id,
            system_prompt=system_prompt,
            turn_instructions=knowledge_instructions,
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
            authorize_spawn=authorize_spawn,
        )

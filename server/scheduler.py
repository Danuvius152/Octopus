from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .agent_configuration import AgentConfigurationError
from .credential_bindings import CredentialBindingError
from .db.applications import ApplicationDeleting
from .db.base import DatabaseBusy
from .knowledge import RequestingWorkClosedError, decode_context
from .models import SessionStatus
from .schedule_ai import (
    ScheduleParseError,
    cron_trigger,
    interval_trigger,
    local_timezone,
)
from .schedule_targets import ScheduleTargetError, resolve_schedule_target
from .service_health import ServiceUnavailable
from .sessions.runtime_types import (
    QuotaExceeded,
    SessionAdmissionBusy,
    SessionAdmissionClosed,
    SessionMailboxFull,
    SessionOwnerUnavailable,
)
from .sessions.submission import TemporaryScheduleReceipt

if TYPE_CHECKING:
    from server.database import Database
    from server.knowledge import AdmittedRequest
    from server.session_manager import SessionManager
    from server.sessions.submission import SubmissionReceipt

from .db.schedules import (
    ScheduleChangedError,
    ScheduleOriginChanged,
    schedule_delivery_id,
    stored_run_at,
)
from .monitor import Event as _MonEvent
from .monitor import record as _mon_record

logger = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(seconds=5)


@dataclass(frozen=True)
class _ScheduleTiming:
    run_at: str | None = None
    cron: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None

    @classmethod
    def capture(cls, row: dict) -> _ScheduleTiming:
        if row.get("run_at") is not None:
            return cls(run_at=row["run_at"])
        if row.get("cron"):
            return cls(cron=row["cron"], timezone=row.get("timezone") or "UTC")
        return cls(interval_seconds=row["interval_seconds"], timezone=row.get("timezone"))


@dataclass(eq=False)
class _ScheduleRegistration:
    schedule_id: str
    job_id: str
    timing: _ScheduleTiming
    agent_id: str
    admission_revoked: bool = False


class ScheduleRegistrationError(RuntimeError):
    """The definition write completed, but its trigger was not confirmed."""

    def __init__(self, schedule_id: str) -> None:
        self.schedule_id = schedule_id
        super().__init__(
            f"Schedule {schedule_id}: configuration may have been saved; registration is unconfirmed. "
            "Read this schedule's current state before making another change; do not repeat creation."
        )


class ScheduleRunner:
    def __init__(self, session_mgr: SessionManager, db: Database) -> None:
        self._executor = AsyncIOExecutor()
        self._scheduler = AsyncIOScheduler(executors={"default": self._executor})
        self._session_mgr = session_mgr
        self._db = db
        self._closing = False
        self._registration_lock = asyncio.Lock()
        self._registrations: dict[str, _ScheduleRegistration] = {}
        self._control_tasks: set[asyncio.Task[None]] = set()
        self._shutdown_controls: tuple[asyncio.Task[None], ...] = ()
        self._shutdown_tasks: tuple[asyncio.Future, ...] | None = None
        self._session_mgr.set_schedule_runner(self)

    def next_run_at(self, schedule_id: str) -> str | None:
        """ISO timestamp of the next fire, straight from APScheduler's own
        trigger state — the only place that knows it for a cron in an
        arbitrary timezone. None without an armed and eligible registration."""
        try:
            registration = self._registrations.get(schedule_id)
            if registration is not None and registration.admission_revoked:
                return None
            job = self._scheduler.get_job(registration.job_id if registration is not None else schedule_id)
        except Exception:
            return None
        run_time = getattr(job, "next_run_time", None) if job else None
        return run_time.isoformat() if run_time else None

    async def initialize(self, *, paused: bool = False) -> None:
        if self._closing:
            raise RuntimeError("ScheduleRunner is shutting down")
        for row in await self._db.load_schedules():
            await self.reconcile(row["id"])
        # Metrics retention, on the durable scheduler this class already owns
        # rather than a second timer or a cron entry (§9 G2). Daily at 04:00
        # local: cheap, and outside any plausible working hour.
        self._scheduler.add_job(
            self._prune_metrics,
            CronTrigger(hour=4, minute=0),
            id="__metrics_retention__",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._prune_wiki,
            CronTrigger(hour=4, minute=15),
            id="__wiki_text_retention__",
            replace_existing=True,
            max_instances=1,
        )
        if self._closing:
            raise RuntimeError("ScheduleRunner is shutting down")
        self._scheduler.start(paused=paused)

    def start(self) -> None:
        """Release due fires only after the service has recovered all owners."""
        if self._closing:
            raise RuntimeError("ScheduleRunner is shutting down")
        self._scheduler.resume()

    async def _prune_wiki(self):
        if self._closing:
            return
        from .wiki.cleanup import sweep

        await sweep()

    def _add_job(self, row: dict, *, not_before: datetime | None = None) -> None:
        timing = _ScheduleTiming.capture(row)
        schedule_id = row["id"]
        # Periodic jobs share the instance limit across edits. Each date arm
        # gets its own executor slot even if its predecessor is still unwinding.
        job_id = f"{schedule_id}:date:{uuid4().hex}" if timing.run_at is not None else schedule_id
        registration = _ScheduleRegistration(schedule_id, job_id, timing, row["agent_id"])
        common = dict(id=job_id, args=[registration], replace_existing=True)
        if timing.run_at is not None:
            run_date = max(stored_run_at(timing.run_at), not_before or datetime.now(UTC))
            self._scheduler.add_job(self._fire, DateTrigger(run_date=run_date), misfire_grace_time=None, **common)
        elif timing.cron is not None:
            # cron_trigger, not CronTrigger.from_crontab: crontab counts
            # day-of-week from Sunday and APScheduler from Monday
            # (schedule_ai._crontab_dow_field).
            try:
                trigger = cron_trigger(timing.cron, timing.timezone or "UTC")
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ScheduleParseError(f"Invalid stored cron: {exc}") from exc
            self._scheduler.add_job(self._fire, trigger, **common)
        else:
            trigger = interval_trigger(timing.interval_seconds, now=datetime.now(UTC), tz=timing.timezone or local_timezone())
            self._scheduler.add_job(self._fire, trigger, **common)
        self._registrations[schedule_id] = registration

    def _owns_registration(self, registration: _ScheduleRegistration) -> bool:
        return (not self._closing and not registration.admission_revoked
                and self._registrations.get(registration.schedule_id) is registration)

    def _forget_date(self, registration: _ScheduleRegistration) -> None:
        if self._owns_registration(registration):
            self._registrations.pop(registration.schedule_id)

    async def _fire(self, registration: _ScheduleRegistration) -> None:
        """Run the schedule's prompt for this fire.

        Delivery modes (docs/implemented/scheduled-runs.md):

        * **Append into the origin session** — when the schedule was created from
          a `/schedule` chat command and that session is still live, the run is
          queued into it (`start_message`, so it lands behind any in-flight turn
          instead of being dropped) and the result shows up in the conversation
          the user already has open. The session is *not* archived.
        * **Fresh schedule-origin session** — no origin recorded, or it has since
          been deleted/archived. Materialize a throwaway session under the agent,
          run the prompt, and hide it on idle. Continuity across fires comes from
          agent memory, not a reused session.

        Origin delivery checks queued schedule inputs; the temporary-session
        branch waits for its turn. These are different overlap boundaries;
        the time-field and overlap discrepancy is documented in the contract.

        A one-shot is removed only by the target/input admission transaction.
        Returning or losing this callback cannot consume an unaccepted fire.
        """
        if not self._owns_registration(registration):
            return
        if registration.timing.run_at is not None:
            await self._fire_date(registration, registration.timing.run_at)
            return
        fired_at = datetime.now(UTC).isoformat()
        try:
            row = await self._capture_fire(registration)
        except (TimeoutError, ConnectionError, ServiceUnavailable):
            logger.warning("Schedule %s: could not capture periodic fire", registration.schedule_id, exc_info=True)
            return
        if row is not None:
            await self._fire_periodic(row, fired_at)

    async def _capture_fire(self, registration: _ScheduleRegistration) -> dict | None:
        row = await self._db.get_schedule_definition(registration.schedule_id)
        if (not self._owns_registration(registration) or row is None or not row["enabled"]
                or _ScheduleTiming.capture(row) != registration.timing):
            return None
        reason = await self._target_unavailable_reason(row)
        if reason is not None:
            logger.warning("Schedule %s suppressed: %s", registration.schedule_id, reason)
            async with self._registration_lock:
                if self._owns_registration(registration):
                    await self._register(registration.schedule_id)
            return None
        if registration.timing.run_at is not None and not self._owns_registration(registration):
            return None
        return row

    async def _fire_date(self, registration: _ScheduleRegistration, run_at: str) -> None:
        schedule_id = registration.schedule_id
        delivery_id = schedule_delivery_id(schedule_id, run_at)
        while self._owns_registration(registration):
            try:
                row = await self._capture_fire(registration)
            except ServiceUnavailable:
                return
            except (TimeoutError, ConnectionError):
                logger.info("Schedule %s: definition temporarily unreadable", schedule_id)
                await self._recover_date(registration, delivery_id)
                return
            if row is None:
                self._forget_date(registration)
                return
            context = decode_context(row.get("knowledge_context"))
            assert context is not None  # Validated with the captured target.
            try:
                _, receipt = await self._session_mgr.start_one_shot(
                    row, self._compose_prompt(row, row["prompt"]), context,
                    registration_open=lambda: self._owns_registration(registration),
                )
            except ScheduleOriginChanged:
                # Rebinding keeps this fire and its original registration.
                continue
            except ServiceUnavailable:
                return
            except (AgentConfigurationError, CredentialBindingError):
                logger.warning("Schedule %s: Agent configuration prevents admission", schedule_id)
                await self._recover_date(registration, delivery_id)
                return
            except (ApplicationDeleting, DatabaseBusy, ScheduleChangedError, QuotaExceeded,
                    SessionAdmissionBusy, SessionAdmissionClosed, SessionMailboxFull):
                await self._recover_date(registration, delivery_id)
                return
            except (TimeoutError, ConnectionError, SessionOwnerUnavailable):
                logger.warning("Schedule %s: admission acknowledgment unconfirmed", schedule_id)
                await self._recover_date(registration, delivery_id, unconfirmed=True)
                return
            if not receipt.accepted:
                await self._recover_date(registration, delivery_id)
                return
            self._forget_date(registration)
            if isinstance(receipt, TemporaryScheduleReceipt):
                await asyncio.shield(receipt.retirement)
            return

    async def _recover_date(self, registration: _ScheduleRegistration, delivery_id: str, *, unconfirmed: bool = False) -> None:
        """Keep the original date responsibility until it is settled or rearmed."""
        while not self._closing:
            try:
                self._db.health.require_healthy()
                if unconfirmed:
                    if await self._session_mgr.source_receipt("schedule", delivery_id) is not None:
                        break
                    if await self._session_mgr.wait_for_one_shot_admission(delivery_id):
                        # The original Owner could commit after the empty read.
                        continue
                if not self._owns_registration(registration):
                    return
                async with self._registration_lock:
                    if not self._owns_registration(registration) or self._scheduler.get_job(registration.job_id) is not None:
                        return
                    current = await self._db.get_schedule_definition(registration.schedule_id)
                    if (current is None or not current["enabled"]
                            or _ScheduleTiming.capture(current) != registration.timing
                            or await self._target_unavailable_reason(current) is not None
                            or await self._session_mgr.source_receipt("schedule", delivery_id) is not None):
                        break
                    # Control writes can commit while target and input are being
                    # observed. Install only against the last definition read.
                    if await self._db.get_schedule_definition(registration.schedule_id) != current:
                        continue
                    self._db.health.require_healthy()
                    if self._owns_registration(registration) and self._db.health.state != "stopping":
                        self._add_job(current, not_before=datetime.now(UTC) + _RETRY_DELAY)
                    return
            except ServiceUnavailable:
                return
            except (TimeoutError, ConnectionError):
                await asyncio.sleep(_RETRY_DELAY.total_seconds())
        self._forget_date(registration)

    async def _fire_periodic(self, row: dict, fired_at: str) -> None:
        schedule_id = row["id"]
        agent_id = row["agent_id"]
        origin_session_id = row.get("origin_session_id")
        prompt = row["prompt"]
        body = self._compose_prompt(row, prompt)
        context = decode_context(row.get("knowledge_context"))
        assert context is not None  # Target validation checked this exact definition snapshot.

        if origin_session_id and self._session_mgr.get_session(origin_session_id):
            if self._backlogged(origin_session_id):
                logger.info(
                    "Schedule %s skipped: its session already has a fire "
                    "waiting to run",
                    schedule_id,
                )
                return
            try:
                # Queue-aware: appends to the live session, waiting its turn if
                # the user is mid-conversation rather than failing on a held lock.
                receipt = await self._session_mgr.start_message(origin_session_id, body, source="schedule", knowledge_context=context)
                if not receipt.accepted:
                    logger.info("Schedule %s suppressed: session admission is closed", schedule_id)
                    return
                await self._db.update_schedule(
                    schedule_id,
                    last_run_at=fired_at,
                    last_run_session_id=origin_session_id,
                )
            except ValueError as e:
                _mon_record(_MonEvent(kind="schedule_fire", agent_id=agent_id,
                                     ok=False, error_code="overlap_skip",
                                     detail={"schedule_id": schedule_id}))
                logger.info("Schedule %s skipped: %s", schedule_id, e)
            except Exception:
                logger.exception("Schedule %s failed", schedule_id)
            return

        if self._previous_fire_running(row):
            logger.info(
                "Schedule %s skipped: the previous run (session %s) is "
                "still going",
                schedule_id,
                (row or {}).get("last_run_session_id"),
            )
            return

        session = None
        try:
            session = await self._session_mgr.create_session(
                agent_id, origin="schedule"
            )
            _mon_record(_MonEvent(
                kind="schedule_fire", agent_id=agent_id, ok=True,
                detail={"schedule_id": schedule_id},
            ))
            # Recorded BEFORE the turn runs: the overlap guard on the next
            # tick has to be able to see a fire that is still in flight,
            # and a fire that crashes mid-turn still leaves a session the
            # user can open and read.
            await self._db.update_schedule(
                schedule_id, last_run_session_id=session.id
            )
            receipt = await self._session_mgr.start_message(session.id, body, source="schedule", knowledge_context=context)
            if not receipt.accepted:
                logger.info("Schedule %s suppressed: session admission is closed", schedule_id)
                return
            if not await self._completion_recorded(schedule_id, session.id, receipt):
                return
            await self._db.update_schedule(schedule_id, last_run_at=fired_at)
        except ValueError as e:
            _mon_record(_MonEvent(kind="schedule_fire", agent_id=agent_id,
                                 ok=False, error_code="overlap_skip",
                                 detail={"schedule_id": schedule_id}))
            logger.info("Schedule %s skipped: %s", schedule_id, e)
        except Exception:
            logger.exception("Schedule %s failed", schedule_id)
        finally:
            if session is not None:
                await self._session_mgr.auto_archive_scheduled_session(session.id)

    async def _completion_recorded(self, schedule_id: str, session_id: str, receipt: SubmissionReceipt) -> bool:
        """Use the same original completion and retirement evidence for each mode."""
        completion = await receipt.wait()
        await self._session_mgr.await_session_execution(session_id)
        recorded = completion.operation == "message" and completion.state == "completed" and completion.recording_state == "committed"
        if not recorded:
            logger.error("Schedule %s completion is %s, recording is %s (session=%s request=%s)",
                         schedule_id, completion.state, completion.recording_state, session_id, completion.request_id)
        return recorded

    async def _prune_metrics(self) -> None:
        """Drop metrics past the retention window. Best-effort: a pruning
        failure must not disturb the schedules this runner exists to fire."""
        if self._closing:
            return
        from . import monitor as monitor_pkg

        store = monitor_pkg.monitor
        if store is None:
            return
        try:
            await store.prune()
        except Exception:
            logger.warning("metrics retention sweep failed", exc_info=True)

    @staticmethod
    def _compose_prompt(row: dict | None, prompt: str) -> str:
        """Mark scheduled input and require an autonomous report: no human is
        waiting to answer a follow-up question for this fire.
        """
        if not row:
            return prompt
        from .schedule_ai import recurrence_label_for

        name = (row.get("name") or "").strip() or "Scheduled task"
        return (
            f"[scheduled:{name} — {recurrence_label_for(row)}]\n"
            f"This turn was started by a schedule, not by the user. Do the "
            f"work and report the result; nobody is waiting to answer "
            f"questions.\n\n{prompt}"
        )

    def _backlogged(self, session_id: str) -> bool:
        """True when a session already has something queued.

        A fire queued behind the user's in-flight turn is fine and by design;
        a *second* fire queued behind the first one is a pile-up.
        """
        session = self._session_mgr.get_session(session_id)
        return bool(session and session._pending_queue)

    def _previous_fire_running(self, row: dict | None) -> bool:
        """True while the session the last fire ran in is still working.

        Only meaningful for the throwaway-session mode: each fire gets its own
        session, so a busy one means the previous fire hasn't finished.
        """
        sid = (row or {}).get("last_run_session_id")
        if not sid:
            return False
        session = self._session_mgr.get_session(sid)
        if session is None:
            return False
        return (
            session.status != SessionStatus.idle
            or bool(session._pending_queue)
            or session._run.request is not None
            or self._session_mgr.session_is_executing(session)
        )

    async def create(self, schedule_id: str, **definition: Any) -> None:
        await self._change(schedule_id, lambda: self._db.save_schedule(
            schedule_id=schedule_id, require_future=True, **definition,
        ))

    async def update(self, existing: dict, updates: dict, *, request: AdmittedRequest | None = None,
                     require_future: bool = False) -> None:
        await self._change(existing["id"], lambda: self._db.update_schedule(
            existing["id"], expected=existing, request=request, require_future=require_future, **updates,
        ))

    async def delete(self, schedule_id: str) -> None:
        await self._change(schedule_id, lambda: self._db.delete_schedule(schedule_id))

    async def _change(self, schedule_id: str, write: Callable[[], Awaitable[None]] | None = None) -> None:
        """Take ownership before the first DB/registration wait, until convergence.

        Each edit is a distinct operation. Cancelling a waiter cannot cancel an
        admitted definition write or lose the subsequent trigger update.
        """
        if self._closing:
            raise RuntimeError("ScheduleRunner is shutting down")

        async def finish() -> None:
            if write is not None:
                await write()
                # Revoke date admission as soon as the write is acknowledged.
                # Reconciliation still needs the old ID to confirm removal.
                registration = self._registrations.get(schedule_id)
                if registration is not None and registration.timing.run_at is not None:
                    registration.admission_revoked = True
            try:
                async with self._registration_lock:
                    await self._register(schedule_id)
            except Exception as exc:
                raise ScheduleRegistrationError(schedule_id) from exc
            if write is not None:
                try:
                    await self._session_mgr._broadcast({"type": "schedules_changed"})
                except Exception:
                    logger.debug("schedules_changed broadcast failed", exc_info=True)

        task = asyncio.create_task(finish(), name=f"schedule-control-{schedule_id}")
        self._control_tasks.add(task)

        def retired(original: asyncio.Task[None]) -> None:
            if original.cancelled():
                logger.error("Owned schedule control cancelled schedule=%s", schedule_id)
            elif (error := original.exception()) is not None and not isinstance(error, (ScheduleChangedError, RequestingWorkClosedError, ScheduleParseError)):
                logger.error("Schedule control failed schedule=%s", schedule_id, exc_info=error)
            self._control_tasks.discard(original)

        task.add_done_callback(retired)
        await asyncio.shield(task)

    async def reconcile(self, schedule_id: str) -> None:
        """Rebuild only this identity's trigger from its current definition."""
        await self._change(schedule_id)

    async def _register(self, schedule_id: str) -> None:
        # The DB definition is authoritative even when a later edit/deletion
        # overtook this operation. No SQL transaction spans target validation.
        while True:
            current = await self._db.get_schedule_definition(schedule_id)
            self._remove_job(schedule_id)
            if current is None or not current["enabled"]:
                return
            reason = await self._target_unavailable_reason(current)
            if reason is None and current.get("run_at") is not None:
                try:
                    stored_run_at(current["run_at"])
                except (OverflowError, ValueError):
                    reason = "invalid_run_at"
            if reason is None:
                try:
                    self._add_job(current)
                except ScheduleParseError as exc:
                    reason = f"invalid_recurrence: {exc}"
                else:
                    return
            if reason == "agent_archived":
                return
            try:
                # Definitions can be edited outside the registration lock.
                # An old observation cannot disable the replacement definition.
                await self._db.update_schedule(
                    schedule_id, expected=current, enabled=False, invalid_reason=reason,
                )
            except ScheduleChangedError:
                continue
            logger.warning("Schedule %s disabled: %s", schedule_id, reason)
            return

    def _remove_job(self, schedule_id: str) -> None:
        registration = self._registrations.get(schedule_id)
        if registration is None:
            return
        try:
            self._scheduler.remove_job(registration.job_id)
        except JobLookupError:
            # No job registered — a disabled schedule never had one, and
            # removing twice is how delete-after-disable arrives here.
            pass
        self._registrations.pop(schedule_id)

    async def _target_unavailable_reason(self, row: dict) -> str | None:
        """Observe this fire's target without mutating current registration."""
        try:
            agent = await resolve_schedule_target(
                self._db, row["agent_id"], row.get("origin_session_id"),
                allow_missing_origin=True,
            )
            context = decode_context(row.get("knowledge_context"))
            if context is None:
                raise ScheduleTargetError("knowledge_contract_missing", "Schedule needs explicit human authorization")
            if context.origin.user_id != agent.get("user_id"):
                raise ScheduleTargetError("knowledge_owner_mismatch", "Schedule knowledge belongs to another account")
        except ScheduleTargetError as exc:
            return exc.reason
        # A pending hard deletion closes creation at the SessionManager. An
        # unconsumed date can still rearm and periodic jobs keep their cadence.
        return "agent_archived" if row["agent_id"] in self._session_mgr._closed_agents else None

    async def remove_deleted_agent_schedules(self, agent_id: str) -> None:
        """Remove triggers after SQLite has cascaded their definitions."""
        async with self._registration_lock:
            for schedule_id, registration in tuple(self._registrations.items()):
                if registration.agent_id == agent_id:
                    self._remove_job(schedule_id)

    async def suspend_agent(self, agent_id: str) -> None:
        for row in await self._db.load_schedules():
            if row["agent_id"] == agent_id:
                await self.reconcile(row["id"])

    async def restore_agent(self, agent_id: str) -> None:
        for row in await self._db.load_schedules():
            if row["agent_id"] == agent_id:
                await self.reconcile(row["id"])

    def drain_blockers(self) -> tuple[str, ...]:
        callbacks = (*getattr(self._executor, "_pending_futures", ()), *(self._shutdown_tasks or ()))
        controls = tuple(task.get_name() for task in self._control_tasks if not task.done())
        if self._registration_lock.locked() or any(not task.done() for task in callbacks):
            return (*controls, "registration or dispatched callback")
        return controls

    def begin_shutdown(self) -> None:
        """Close admission/fires while admitted controls keep their scheduler."""
        if self._closing:
            return
        self._closing = True
        self._shutdown_controls = tuple(self._control_tasks)
        self._shutdown_tasks = tuple(getattr(self._executor, "_pending_futures", ()))
        if self._scheduler.running:
            self._scheduler.pause()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Join controls and callbacks within one bound; keep uncertain owners."""
        self.begin_shutdown()
        deadline = asyncio.get_running_loop().time() + timeout
        errors: list[Exception] = []
        if self._shutdown_controls:
            controls_done, controls_pending = await asyncio.wait(
                self._shutdown_controls, timeout=max(0, deadline - asyncio.get_running_loop().time()),
            )
            if controls_pending:
                raise TimeoutError(f"scheduler control retirement unconfirmed: {', '.join(task.get_name() for task in controls_pending)}")
            for task in controls_done:
                if task.cancelled():
                    raise RuntimeError(f"scheduler control was cancelled: {task.get_name()}")
                if isinstance(error := task.exception(), Exception) and not isinstance(error, (ScheduleChangedError, RequestingWorkClosedError, ScheduleParseError)):
                    errors.append(error)
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        # APScheduler's posted shutdown also retires its timer and job stores.
        await asyncio.sleep(0)
        tasks = self._shutdown_tasks or ()
        callbacks_done, callbacks_pending = await asyncio.wait(
            tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()),
        ) if tasks else (set(), set())
        if callbacks_pending:
            raise TimeoutError(f"scheduler shutdown retirement unconfirmed: {len(callbacks_pending)} callback(s)")
        for callback in callbacks_done:
            if callback.cancelled():
                continue
            # run_coroutine_job converts callback failures into job events.
            for event in callback.result():
                error = getattr(event, "exception", None)
                if isinstance(error, Exception):
                    errors.append(error)
        if errors:
            raise ExceptionGroup("scheduler callback cleanup failed", errors)
        self._registrations.clear()

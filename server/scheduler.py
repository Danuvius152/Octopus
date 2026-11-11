from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .knowledge import RequestingWorkClosedError, decode_context
from .models import SessionStatus
from .schedule_ai import ScheduleParseError, cron_trigger
from .schedule_targets import ScheduleTargetError, resolve_schedule_target

if TYPE_CHECKING:
    from server.database import Database
    from server.knowledge import AdmittedRequest
    from server.session_manager import SessionManager
    from server.sessions.submission import SubmissionReceipt

from .db.schedules import ScheduleChangedError
from .monitor import Event as _MonEvent
from .monitor import record as _mon_record

logger = logging.getLogger(__name__)


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
        self._control_tasks: set[asyncio.Task[None]] = set()
        self._shutdown_controls: tuple[asyncio.Task[None], ...] = ()
        self._shutdown_tasks: tuple[asyncio.Future, ...] | None = None
        self._session_mgr.set_schedule_runner(self)

    def next_run_at(self, schedule_id: str) -> str | None:
        """ISO timestamp of the next fire, straight from APScheduler's own
        trigger state — the only place that knows it for a cron in an
        arbitrary timezone. None when the schedule is disabled (no job) or the
        scheduler hasn't started yet."""
        try:
            job = self._scheduler.get_job(schedule_id)
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
        # Already admitted controls can finish while new launches are closed.
        # Recurrence priority: run_at (one-time) > cron > interval.
        common = dict(id=row["id"], args=[row["id"]], replace_existing=True)
        if row.get("run_at"):
            run_date = datetime.fromisoformat(row["run_at"])
            if run_date.tzinfo is None:
                run_date = run_date.replace(tzinfo=UTC)
            self._scheduler.add_job(self._fire, DateTrigger(run_date=max(run_date, not_before or datetime.now(UTC))),
                                    misfire_grace_time=None, **common)
        elif row.get("cron"):
            # cron_trigger, not CronTrigger.from_crontab: crontab counts
            # day-of-week from Sunday and APScheduler from Monday
            # (schedule_ai._crontab_dow_field).
            trigger = cron_trigger(row["cron"], row.get("timezone") or "UTC")
            self._scheduler.add_job(self._fire, trigger, **common)
        else:
            self._scheduler.add_job(
                self._fire, "interval", seconds=row["interval_seconds"], **common
            )

    async def _fire(self, schedule_id: str) -> None:
        """Run the schedule's prompt for this fire.

        Two modes (agent-refactor.md §5.3/§5.6):

        * **Append into the origin session** — when the schedule was created from
          a `/schedule` chat command and that session is still live, the run is
          queued into it (`start_message`, so it lands behind any in-flight turn
          instead of being dropped) and the result shows up in the conversation
          the user already has open. The session is *not* archived.
        * **Fresh schedule-origin session** — no origin recorded, or it has since
          been deleted/archived. Materialize a throwaway session under the agent,
          run the prompt, and hide it on idle. Continuity across fires comes from
          agent memory, not a reused session.

        A fire never starts while the previous one is still outstanding
        (§"overlap"): a schedule that runs every 5 minutes and takes 10 would
        otherwise stack runs on top of each other until the box gives out. The
        skip is logged and the schedule keeps its cadence — the next tick tries
        again.

        A one-shot is removed only by the target/input admission transaction.
        Returning or losing this callback cannot consume an unaccepted fire.
        """
        if self._closing:
            return
        now = datetime.now(UTC).isoformat()
        row = await self._schedule_row(schedule_id)
        # Only the current durable row can authorize a fire or supply text.
        if row is None or not row["enabled"]:
            return
        if (reason := await self._target_unavailable_reason(row)) is not None:
            logger.warning("Schedule %s suppressed: %s", schedule_id, reason)
            if not self._closing:
                await self.reconcile(schedule_id)
            return
        agent_id = row["agent_id"]
        origin_session_id = row.get("origin_session_id")
        run_at = row.get("run_at")
        prompt = row["prompt"]
        body = self._compose_prompt(row, prompt)
        context = decode_context(row.get("knowledge_context"))
        assert context is not None  # Target validation checked this exact definition snapshot.

        if run_at is not None:
            target = None
            try:
                target, receipt = await self._session_mgr.start_one_shot(row, body, context)
                if not receipt.accepted:
                    logger.info("Schedule %s retained: session admission is closed", schedule_id)
                    return
                if target != origin_session_id:
                    await self._completion_recorded(schedule_id, target, receipt)
            except Exception:
                logger.exception("One-shot %s was not acknowledged; its durable input determines consumption", schedule_id)
            finally:
                if target is not None and target != origin_session_id:
                    await self._session_mgr.auto_archive_scheduled_session(target)
                if not self._closing and self._scheduler.running:
                    # Admission alone consumes the original source. Reconcile
                    # current state before retrying an unconsumed one-shot.
                    await self.reconcile(schedule_id, not_before=datetime.now(UTC) + timedelta(seconds=5))
            return

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
                    last_run_at=now,
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
            await self._db.update_schedule(schedule_id, last_run_at=now)
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

    async def _schedule_row(self, schedule_id: str) -> dict | None:
        """The schedule's current row, or None if it was deleted between the
        job firing and this lookup."""
        try:
            return await self._db.get_schedule_definition(schedule_id)
        except Exception:
            logger.exception("Schedule %s: could not read its row", schedule_id)
            return None

    @staticmethod
    def _compose_prompt(row: dict | None, prompt: str) -> str:
        """The prompt with a marker saying where it came from.

        Every other machine-injected turn in Octopus announces itself —
        `[bg-task-result]`, `[agent-reply:…]` — and a schedule's didn't, so an
        agent woken at 07:00 could not tell its own daily run from something
        the user had just typed. That matters for what it does next: a
        scheduled run reports, it doesn't ask a follow-up question of someone
        who isn't there.
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

    async def _change(self, schedule_id: str, write: Callable[[], Awaitable[None]] | None = None,
                      *, not_before: datetime | None = None) -> None:
        """Take ownership before the first DB/registration wait, until convergence.

        Each edit is a distinct operation. Cancelling a waiter cannot cancel an
        admitted definition write or lose the subsequent trigger update.
        """
        if self._closing:
            raise RuntimeError("ScheduleRunner is shutting down")

        async def finish() -> None:
            if write is not None:
                await write()
            try:
                async with self._registration_lock:
                    await self._register(schedule_id, not_before=not_before)
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

    async def reconcile(self, schedule_id: str, *, not_before: datetime | None = None) -> None:
        """Rebuild only this identity's trigger from its current definition."""
        await self._change(schedule_id, not_before=not_before)

    async def _register(self, schedule_id: str, *, not_before: datetime | None = None) -> None:
        # The DB definition is authoritative even when a later edit/deletion
        # overtook this operation. No SQL transaction spans target validation.
        while True:
            current = await self._db.get_schedule_definition(schedule_id)
            self._remove_job(schedule_id)
            if current is None or not current["enabled"]:
                return
            reason = await self._target_unavailable_reason(current)
            if reason is None and current.get("run_at"):
                try:
                    datetime.fromisoformat(current["run_at"])
                except ValueError:
                    reason = "invalid_run_at"
            if reason is None:
                self._add_job(current, not_before=not_before)
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
        try:
            self._scheduler.remove_job(schedule_id)
        except JobLookupError:
            # No job registered — a disabled schedule never had one, and
            # removing twice is how delete-after-disable arrives here.
            pass

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
        return "agent_archived" if row["agent_id"] in self._session_mgr._closed_agents else None

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

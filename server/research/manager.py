"""ResearchManager — tracks deep-research jobs as async tasks
(native-deep-research.md §6). Mirrors the bg-task / delegation managers:

  - `start(session_id, question)` persists a `research_jobs` row, returns the
    id immediately, and runs the pipeline as a tracked `asyncio.Task`.
  - Progress is broadcast over the session bus + written to the row's `phase`.
  - On success the report is written to a file AND injected into the session as
    a turn via `admit_return`; completion and delivery
    are tracked separately, injection is idempotent.
  - `cancel(job_id)` cancels the task — leaves re-raise CancelledError and reap
    their process groups, so nothing orphans.
  - A global semaphore bounds concurrent JOBS (per-job leaf concurrency is
    bounded inside the pipeline); a hard per-job timeout backstops everything.
  - A boot sweep marks restart-orphaned `running` rows interrupted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from ..aio import complete_cleanup
from ..config import settings
from ..diagnostics import safe_diagnostic
from ..harness import get_harness, has_backend, model_settings
from ..knowledge import (
    OwnedTaskContext,
    capture,
    decode_owned,
    derive,
    encode,
)
from ..mcp_identity import McpScope, require_live_scope, work_is_open
from ..sessions.base import Session
from ..sessions.configuration import SessionConfigurationError
from ..sessions.owner_runtime import capture_configuration
from ..sessions.publication import WorkPublications
from ..sessions.runtime_types import (
    AdmissionIntegrityError,
    SessionOwnerUnavailable,
)
from ..sessions.submission import (
    RequestCompletion,
    ReturnBody,
    ReturnRefused,
    ReturnSkipped,
    ReturnTarget,
    ReturnUnconfirmed,
    SubmissionReceipt,
    receipt_for_input,
)
from ..workspace import paths_for
from .evidence import MAX_EVIDENCE_BYTES, ResearchEvidence
from .orchestrator import (
    ResearchLimits,
    ResearchProgress,
    ResearchSynthesisError,
    run_research,
)

if TYPE_CHECKING:
    from ..database import Database
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)


class ResearchError(Exception):
    """Surface-level error with an HTTP status for the REST layer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


type _WriterKind = Literal["pipeline", "control", "delivery"]


@dataclass
class _ResearchWriter:
    task: asyncio.Task
    on_retired: Callable[[], None] | None
    recording: Callable[[], Coroutine[Any, Any, Any]] | None = None
    resuming: bool = False


@dataclass
class _ResearchJob:
    job_id: str
    session_id: str | None
    work_id: str | None
    writers: dict[_WriterKind, _ResearchWriter] = field(default_factory=dict)
    retiring_delivery: asyncio.Future[None] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _delivery_result(completion: RequestCompletion) -> tuple[str, str | None]:
    successful = (completion.operation == "message" and completion.state == "completed" and completion.recording_state == "committed"
                  and completion.outcome is not None and completion.outcome.status == "success")
    stage = ("processed" if successful else "unknown" if completion.state == "unavailable" else
             "recording_unconfirmed" if completion.recording_state in {"failed", "unconfirmed"} else "failed")
    return stage, None if successful else completion.reason or completion.state


def _research_dir(user_id: str | None) -> str:
    """Where this account's research scratch and reports live.

    A job's scratch cwd and its report are the user's own data, so they belong
    under the user's own root rather than in one shared directory (§6). The
    pre-accounts answer is the directory Octopus has always used, so an install
    being upgraded keeps the reports it already wrote.
    """
    d = str(paths_for(user_id).research)
    os.makedirs(d, exist_ok=True)
    return d


class ResearchManager:
    """App-lifetime singleton; bound in main.py's lifespan."""

    def __init__(self) -> None:
        self.session_mgr: SessionManager | None = None
        self.db: Database | None = None
        self._jobs: dict[str, _ResearchJob] = {}
        self._job_sem: asyncio.Semaphore | None = None
        self._closing = False
        self._delivery_jobs: dict[str, _ResearchJob] = {}
        self._publications = WorkPublications()

    def bind(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._job_sem = asyncio.Semaphore(max(1, settings.research_max_concurrent_jobs))
        self._closing = False
        session_mgr.on_broadcast("research.delivery", self._on_delivery_progress, required=False)

    def _task(self, job_id: str, kind: _WriterKind = "pipeline") -> asyncio.Task | None:
        job = self._jobs.get(job_id)
        writer = job.writers.get(kind) if job else None
        return writer.task if writer else None

    def _register_task(self, job_id: str, task: asyncio.Task, *, kind: _WriterKind = "pipeline",
                       session_id: str | None = None, work_id: str | None = None,
                       on_retired: Callable[[], None] | None = None, resuming: bool = False) -> None:
        """Retain original work through cleanup, with controls kept unsignalled."""
        job = self._jobs.get(job_id)
        if job is None:
            job = self._jobs[job_id] = _ResearchJob(job_id, session_id, work_id)
        writer = job.writers[kind] = _ResearchWriter(task, on_retired, resuming=resuming)

        def finished(done: asyncio.Task) -> None:
            error = None if done.cancelled() else done.exception()
            if kind == "control" and done.cancelled():
                logger.error("research cancellation control unexpectedly cancelled job=%s", job_id)
            elif error is not None and not (kind == "control" and isinstance(error, ResearchError)):
                logger.error("research %s %s cleanup failed", job_id, kind, exc_info=error)
            elif job.writers.get(kind) is writer:
                del job.writers[kind]
                if on_retired is not None:
                    on_retired()
                if not job.writers:
                    del self._jobs[job_id]
        task.add_done_callback(finished)

    async def _record_owned[T](
        self, job_id: str, write: Callable[[], Coroutine[Any, Any, T]], *, kind: _WriterKind = "pipeline",
        on_recorded: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        """Resume a selected write and hand its confirmation to the next stage."""
        job = self._jobs.get(job_id)
        writer = job.writers.get(kind) if job else None
        if writer is not None:
            writer.recording = lambda: self._record_owned(job_id, write, kind=kind, on_recorded=on_recorded)
        result = await write()
        if writer is not None:
            writer.recording = None
        if on_recorded is not None:
            await on_recorded(result)
        return result

    async def wait_for_idle(self) -> None:
        """Drain current controls, pipelines and their delivery observers.

        This does not close admission. Cancelling a waiter never cancels the
        original research or receipt observation.
        """
        await self._drain()

    async def recover_interrupted(self, *, previous_writers_retired: bool = False) -> int:
        """Before admission opens, observe original identities without dispatch.

        Only managed startup supplies proof that an absent prior producer cannot
        still accept its report. Standalone startup preserves that uncertainty.
        """
        if self.db is None:
            return 0
        n = await self.db.mark_in_flight_research_jobs_interrupted(_now())
        for row in await self.db.recoverable_research_deliveries():
            previous = row["delivery_receipt"]
            if previous and previous["stage"] in {"processed", "failed", "recording_unconfirmed"}:
                await self.db.advance_research_delivery(row["id"], previous["request_id"], previous["stage"])
                continue
            error = "Report admission is unknown after restart; the request was not replayed."
            try:
                # Read the same committed source identity used by source_receipt,
                # reconstructing persisted completion rather than a live waiter.
                source = await self.db.get_source_submission("research", f"research:{row['id']}", session_id=row["session_id"])
            except (SessionOwnerUnavailable, TimeoutError, ConnectionError) as cause:
                self.db.health.require_healthy()
                source = None
                observed_absence = False
                error = f"Report admission could not be read ({cause}); the request was not replayed."
            else:
                observed_absence = source is None
            if source is not None and (receipt := receipt_for_input(source)).accepted:
                stage, detail = _delivery_result(await receipt.wait())
                await self.db.advance_research_delivery(row["id"], receipt.request_id, stage, error=detail)
            elif previous:
                await self.db.advance_research_delivery(row["id"], previous["request_id"], "unknown", error=error)
            else:
                owned = decode_owned(row.get("knowledge_context"))
                binding = await self.db.get_session_binding(row["session_id"])
                reference = owned.return_binding.reference if owned else None
                valid = (reference is not None and binding is not None and reference.user_id is not None
                         and reference.session_id == row["session_id"] and reference.user_id == binding["user_id"])
                failed = observed_absence and previous_writers_retired and valid and row["injection_status"] == "pending"
                if failed:
                    error = "Report was saved but not admitted before restart; the request was not replayed."
                await self.db.reconcile_unaccepted_research(row["id"], failed=failed, error=error)
        if n:
            logger.info("research: marked %d interrupted job(s) on boot", n)
        return n

    # ----------------------------------------------------------------- start

    async def start(
        self, session_id: str, question: str, *, scope: McpScope | None = None,
    ) -> dict[str, Any]:
        """Create + launch a research job for `session_id`. Returns the row."""
        if self._closing:
            raise ResearchError("ResearchManager is shutting down", status_code=503)
        if self.session_mgr is None or self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        question = (question or "").strip()
        if not question:
            raise ResearchError("question must be a non-empty string", status_code=400)

        session = self.session_mgr.get_session(session_id)
        if session is None:
            raise ResearchError(f"session {session_id} not found", status_code=404)
        try:
            # Capture before any await. A model call can never become a fresh
            # human request merely because its original lease has retired.
            parent = capture(session, scope, prompt=question)
            owned = derive(parent, question, sources=("web",))
        except ValueError as error:
            raise ResearchError(str(error), status_code=409) from error
        try:
            configured, agent = await self.session_mgr.capture_request_configuration(capture_configuration(session))
        except SessionConfigurationError as error:
            raise ResearchError(str(error), status_code=error.status_code) from error
        selected = model_settings.effective_settings(configured, agent)
        configured.model = selected["model"]
        configured.reasoning_effort = selected["reasoning_effort"]
        configured.service_tier = selected["service_tier"]
        configured.credential_id = selected["credential_id"]
        # Configuration capture can wait. Admission and registration below are
        # synchronous, so shutdown cannot miss a job admitted after its snapshot.
        if self._closing:
            raise ResearchError("ResearchManager is shutting down", status_code=503)
        if self.session_mgr.get_session(session_id) is not session:
            raise ResearchError(f"session {session_id} not found", status_code=404)
        if session._admission_closed or self.session_mgr.session_deleting(session_id):
            raise ResearchError("Session admission is closed", status_code=409)
        try:
            require_live_scope(scope, session_id)
        except ValueError as error:
            raise ResearchError(str(error), status_code=409) from error
        if not has_backend(configured.backend):
            raise ResearchError(f"unknown backend {configured.backend!r}", status_code=400)
        harness = get_harness(configured.backend)
        if harness.profile.web is None:
            raise ResearchError(
                f"the {session.backend} backend has no web tools, so deep "
                "research isn't available on it",
                status_code=409,
            )

        job_id = uuid.uuid4().hex[:12]
        ready: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        # Own the whole launch before SQLite or subscribers can suspend it.
        task = asyncio.create_task(
            self._start_job(job_id, session_id, question, ready, owned, configured),
            name=f"research-{job_id}",
        )
        self._register_task(job_id, task, session_id=session_id, work_id=owned.return_binding.work_id)

        def finished(done: asyncio.Task) -> None:
            error = None if done.cancelled() else done.exception()
            if not ready.done():
                ready.set_exception(error or ResearchError(
                    "ResearchManager is shutting down" if self._closing else "research launch cancelled",
                    status_code=503,
                ))

        task.add_done_callback(finished)
        # A cancelled HTTP waiter does not surrender ownership of admitted work.
        ready.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        return await asyncio.shield(ready)

    async def _start_job(
        self, job_id: str, session_id: str, question: str,
        ready: asyncio.Future[dict[str, Any]],
        owned: OwnedTaskContext,
        configured: Session,
    ) -> None:
        assert self.db is not None
        try:
            await self.db.create_research_job(
                job_id, session_id, question, _now(), knowledge_context=encode(owned),
            )
            if closed := self._closure_error(self._jobs[job_id]):
                await self._finalize_failed(job_id, "cancelled", closed.message)
                ready.set_exception(closed)
                return
            await self._broadcast({
                "type": "research_started",
                "session_id": session_id,
                "research_id": job_id,
                "question": question,
            })
            row = await self.db.get_research_job(job_id)
            assert row is not None
            ready.set_result(row)
            await self._run_job(job_id, session_id, question, configured, owned)
        except asyncio.CancelledError:
            status = "interrupted" if self._closing else "cancelled"
            reason = "research interrupted by server shutdown" if self._closing else "cancelled by user"
            await self._finalize_failed(job_id, status, reason)
            raise

    # ------------------------------------------------------------------- run

    async def _run_job(self, job_id: str, session_id: str, question: str, session: Session, owned: OwnedTaskContext) -> None:
        assert self.session_mgr is not None and self.db is not None
        assert self._job_sem is not None
        report = None
        completion_progress: dict[str, Any] | None = None
        try:
            async with self._job_sem:  # bound concurrent jobs
                harness = get_harness(session.backend)
                user_id = owned.execution.origin.user_id
                credential_id, backend = session.credential_id, session.backend
                database, manager, job = self.db, self.session_mgr, self._jobs[job_id]

                async def authorize_spawn() -> None:
                    await manager.require_active_agent(session.agent_id)
                    await database.require_run_authority(user_id, credential_id, backend)
                    if closed := self._closure_error(job):
                        raise asyncio.CancelledError(closed.message)

                try:
                    credential = await self.session_mgr.resolve_credential_by_id(
                        credential_id, backend=backend, context=f"research {job_id}", user_id=user_id,
                    )
                    await authorize_spawn()
                except ValueError as error:
                    raise ResearchError(str(error), status_code=403) from error

                async def on_progress(p: ResearchProgress) -> None:
                    nonlocal completion_progress
                    assert self.db is not None
                    await self.db.update_research_job(job_id, phase=p.phase)
                    event = {
                        "type": "research_progress",
                        "session_id": session_id,
                        "research_id": job_id,
                        "phase": p.phase,
                        "detail": p.detail,
                        "counts": p.counts,
                    }
                    if p.phase == "done":
                        completion_progress = event
                    else:
                        await self._broadcast(event)

                # Run leaves in an EMPTY per-job scratch cwd, NOT the session's
                # working_dir — a web/reasoning leaf has no reason to read the
                # user's repo, and this keeps the read-only-sandbox/denylist
                # leaves away from the session's files.
                try:
                    scratch = os.path.join(_research_dir(session.user_id), job_id, "cwd")
                    os.makedirs(scratch, exist_ok=True)
                except OSError as error:
                    raise ResearchError(f"Could not prepare research scratch directory: {error}", status_code=500) from error
                async with asyncio.timeout(settings.research_job_timeout_seconds):
                    report = await run_research(
                        question,
                        harness=harness,
                        credential=credential,
                        model=session.model,
                        reasoning_effort=session.reasoning_effort,
                        service_tier=session.service_tier,
                        working_dir=scratch,
                        limits=ResearchLimits(),
                        on_progress=on_progress,
                        authorize_spawn=authorize_spawn,
                    )

            # Completion is selected before this await. Cancellation retires
            # its original record/delivery; it cannot select interruption.
            async def finish_report() -> None:
                if completion_progress is not None:
                    await self._broadcast(completion_progress)
                await self._complete_report(job_id, session_id, question, report, session.user_id)

            await complete_cleanup(finish_report())
        except asyncio.CancelledError:
            raise
        except ResearchSynthesisError as exc:
            evidence = os.path.join(paths_for(session.user_id).research, job_id, "synthesis-failure.json")
            try:
                payload = json.dumps({"error": str(exc), **asdict(exc.partial), "evidence": exc.partial.evidence.model_dump()},
                                     ensure_ascii=False, indent=2)
            except Exception as material_error:
                logger.exception("research %s: could not serialize synthesis failure material", job_id)
                material_failure = safe_diagnostic(f"{type(material_error).__name__}: {material_error}")
            else:
                material_failure = self._write_material(evidence, payload)
            failure_reason = str(exc)
            if material_failure is not None:
                failure_reason += f"\nSynthesis diagnostic material unavailable: {material_failure}"
            await self._finalize_failed(job_id, "failed", failure_reason)
        except TimeoutError:
            if report is not None or self._closing:
                raise
            await self._finalize_failed(
                job_id, "failed",
                f"research exceeded {settings.research_job_timeout_seconds}s and was stopped",
            )
        except ResearchError as e:
            if report is not None or self._closing:
                raise
            await self._finalize_failed(job_id, "failed", e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("research job %s crashed", job_id)
            current = asyncio.current_task()
            if report is not None or self._closing or (current and current.cancelling()):
                raise
            await self._finalize_failed(job_id, "failed", str(e), retry=False)
            # An unexpected leaf failure can be unconfirmed retirement after
            # user cancellation. Keep its owner and exception for shutdown.
            raise

    async def _complete_report(
        self, job_id: str, session_id: str, question: str, report: Any,
        user_id: str | None,
    ) -> None:
        assert self.db is not None
        report_path = self._write_report(job_id, report.report, user_id)
        summary_saved = self._write_evidence(job_id, session_id, report.evidence, user_id)
        if report_path is None or not summary_saved:
            await self._finalize_failed(job_id, "failed", "Research report and evidence could not both be saved")
            return
        timestamp = _now()
        db = self.db

        async def deliver_if_committed(result: tuple[bool, dict[str, Any] | None]) -> None:
            _, row = result
            # Only this job's original pipeline writes its report. A retry can
            # observe that committed outcome after losing the write acknowledgement.
            if row is not None and row["status"] == "completed" and row["completed_at"] == timestamp:
                await self._deliver_completed_report(row, session_id, question, report, report_path)

        await self._record_owned(
            job_id, lambda: db.confirm_research_outcome(
                job_id, "completed", cost=report.cost, completed_at=timestamp, report_path=report_path,
            ),
            on_recorded=deliver_if_committed,
        )

    async def _deliver_completed_report(
        self, row: dict[str, Any], session_id: str, question: str, report: Any, report_path: str,
    ) -> None:
        assert self.db is not None
        job_id = row["id"]
        projection = await self.public_job(row)
        await self._broadcast({
            "type": "research_completed",
            "session_id": session_id,
            "research_id": job_id,
            "sources": report.sources,
            "verified": sum(getattr(f, "verification", "unverified") == "verified" for f in report.findings),
            "unverified": sum(getattr(f, "verification", "unverified") != "verified" for f in report.findings),
            "injection_status": "pending",
            "report_path": report_path,
            "evidence": projection["evidence"],
            "evidence_state": projection["evidence_state"],
        })
        try:
            await self._inject_report(job_id, session_id, question, report)
        except Exception as error:
            # Keep unexpected errors visible without reclassifying them as a
            # refusal or permission to retry. Storage still owns fail-stop.
            self.db.health.require_healthy()
            current = await self.db.get_research_job(job_id)
            if current is not None and current["injection_status"] != "failed":
                message = f"Report delivery stopped ({type(error).__name__}): {error}"
                if current.get("error"):
                    message = f"{current['error']}\n{message}"
                await self.db.update_research_job(job_id, error=message)
                await self._broadcast({"type": "research_delivery", "session_id": session_id,
                                       "research_id": job_id, "injection_status": current["injection_status"], "error": message})
            raise

    async def _inject_report(
        self, job_id: str, session_id: str, question: str, report: Any
    ) -> None:
        """Own report retries/progress; sessions own the complete return handoff."""
        assert self.db is not None and self.session_mgr is not None
        db = self.db

        async def read_source() -> dict[str, Any] | None:
            try:
                return await db.get_research_job(job_id)
            except (TimeoutError, ConnectionError):
                # Both reads precede submission. The original job can restart
                # receipt-first observation after storage becomes available.
                db.health.require_healthy()
                job = self._jobs.get(job_id)
                if job is not None and (writer := job.writers.get("pipeline")) is not None:
                    writer.recording = lambda: self._inject_report(job_id, session_id, question, report)
                raise

        async def prepare() -> ReturnBody | ReturnSkipped:
            current = await read_source()
            if (self._closing or current is None or current["session_id"] != session_id
                    or current["status"] != "completed" or current["injection_status"] != "pending"):
                return ReturnSkipped("The source no longer permits a new report input")
            return ReturnBody(f"[deep-research:{job_id}] Research complete for: {question}\n\n{report.report}")

        while True:
            row = await read_source()
            if row and row.get("injection_status") in {"delivered", "failed"}:
                return
            owned = decode_owned(row.get("knowledge_context")) if row else None
            work_id = owned.return_binding.work_id if owned else None
            try:
                result = await self.session_mgr.admit_return(
                    ReturnTarget(session_id, "research", f"research:{job_id}", work_id, owned), prepare=prepare,
                )
            except AdmissionIntegrityError as error:
                if row and row["injection_status"] == "unknown":
                    raise
                await self._settle_delivery(job_id, session_id, error=str(error))
                return
            if isinstance(result, SubmissionReceipt):
                receipt = result
                break
            if row and row["injection_status"] == "unknown":
                return  # A later refusal/observation failure cannot erase the original uncertainty.
            if isinstance(result, ReturnUnconfirmed):
                await self._settle_delivery(job_id, session_id,
                    error=f"Report admission is unconfirmed: {result.cause}", unknown=True)
                return
            assert isinstance(result, ReturnRefused)
            if result.reason in {"capacity", "busy"} and not self._closing:
                await asyncio.sleep(1)
                continue
            if result.cause is not None:
                detail = str(result.cause)
            elif self._closing or result.reason == "admission_closed":
                detail = "session admission is closed"
            else:
                detail = "original request contract is missing, closed or mismatched"
            await self._settle_delivery(job_id, session_id, error=f"delivery suppressed: {detail}")
            return
        await self._record_owned(job_id, lambda: self._start_delivery_observer(job_id, session_id, work_id, receipt))

    async def _start_delivery_observer(
        self, job_id: str, session_id: str, work_id: str | None, receipt: SubmissionReceipt,
    ) -> None:
        assert self.session_mgr is not None
        job = self._jobs.get(job_id)
        if job is None:
            job = self._jobs[job_id] = _ResearchJob(job_id, session_id, work_id)
        job.retiring_delivery = asyncio.get_running_loop().create_future()
        self._delivery_jobs[receipt.request_id] = job
        current = self.session_mgr.get_session(session_id)
        processing = current and current._run.request and current._run.request.request_id == receipt.request_id
        def forget_delivery() -> None:
            self._delivery_jobs.pop(receipt.request_id, None)
            job.retiring_delivery = None
        try:
            await self._advance_delivery(job_id, session_id, receipt.request_id, "processing" if processing else "queued",
                                         work_id=work_id)
        except BaseException:
            forget_delivery()
            if not job.writers:
                self._jobs.pop(job_id, None)
            raise
        task = asyncio.create_task(self._observe_delivery(job, receipt), name=f"research-delivery-{job_id}")
        self._register_task(job_id, task, kind="delivery", on_retired=forget_delivery)

    async def _advance_delivery(self, job_id, session_id, request_id, stage, *, error=None, work_id=None):
        receipt = await self.db.advance_research_delivery(job_id, request_id, stage, error=error)
        if receipt:
            await self._broadcast({"type": "research_delivery", "session_id": session_id, "research_id": job_id,
                                   "injection_status": "delivered", "delivery_receipt": receipt},
                                  retiring=not work_is_open(work_id))

    async def _on_delivery_progress(self, event):
        owner = self._delivery_jobs.get(event.get("request_id"))
        if event.get("type") == "dequeued" and owner and owner.session_id == event.get("session_id"):
            await self._advance_delivery(owner.job_id, owner.session_id, event["request_id"], "processing",
                                         work_id=owner.work_id)

    async def _observe_delivery(self, delivery: _ResearchJob, receipt: SubmissionReceipt) -> None:
        job_id, session_id = delivery.job_id, delivery.session_id
        assert delivery.retiring_delivery is not None
        try:
            if self._closing:
                raise asyncio.CancelledError
            milestones: set[asyncio.Future[Any]] = {receipt._completion, delivery.retiring_delivery}
            await asyncio.wait(milestones, return_when=asyncio.FIRST_COMPLETED)
            await self._record_owned(job_id, lambda: self._record_delivery_completion(delivery, receipt), kind="delivery")
        except asyncio.CancelledError:
            await self._record_owned(job_id, lambda: self._advance_delivery(
                job_id, session_id, receipt.request_id, "unknown", error="Delivery observation stopped; the request was not replayed.",
                work_id=delivery.work_id,
            ), kind="delivery")
            raise

    async def _record_delivery_completion(self, delivery: _ResearchJob, receipt: SubmissionReceipt) -> None:
        completion = receipt.completion
        if completion is None:
            # The Owner already retired its execution. Its publisher can
            # still be inside a subscriber that is awaiting this deletion.
            assert self.db is not None
            row = await self.db.get_source_submission("research", f"research:{delivery.job_id}", session_id=delivery.session_id)
            if row is not None and row["request_id"] == receipt.request_id:
                completion = receipt_for_input(row).completion
        stage, error = _delivery_result(completion) if completion is not None else ("unknown", "Original completion is unconfirmed")
        await self._advance_delivery(delivery.job_id, delivery.session_id, receipt.request_id, stage,
                                     error=error, work_id=delivery.work_id)

    async def _settle_delivery(self, job_id: str, session_id: str, *, error: str, unknown: bool = False) -> None:
        """Persist a refused or uncertain return independently of the report."""
        await self._record_owned(job_id, lambda: self._record_undelivered(job_id, session_id, error, unknown))

    async def _record_undelivered(self, job_id: str, session_id: str, error: str, unknown: bool) -> None:
        assert self.db is not None and self.session_mgr is not None
        status = "unknown" if unknown else "failed"
        await self.db.update_research_job(job_id, injection_status=status, error=error, injected_at=None)
        if not unknown:
            row = await self.db.get_research_job(job_id)
            location = (row or {}).get("report_path") or "the research job record"
            try:
                await self.session_mgr.retain_result_notice(
                    session_id, f"research:{job_id}",
                    f"Research {job_id}: report retained as material at {location}.\n{error}. No automatic follow-up was started.",
                )
            except SessionOwnerUnavailable:
                logger.warning("Research %s delivery unavailable; report retained at %s: %s", job_id, location, error)
        await self._broadcast({
            "type": "research_delivery", "session_id": session_id, "research_id": job_id,
            "injection_status": status, "error": error,
        })

    async def _finalize_failed(self, job_id: str, status: str, error: str, *, retry: bool = True) -> None:
        assert self.db is not None
        db = self.db
        timestamp = _now()

        async def write() -> tuple[bool, dict[str, Any] | None]:
            return await db.confirm_research_outcome(job_id, status, error=error, completed_at=timestamp)

        async def publish_if_committed(result: tuple[bool, dict[str, Any] | None]) -> None:
            _, row = result
            if row is not None and (row["status"], row["completed_at"]) == (status, timestamp):
                await self._publish_failed(row)

        if retry:
            await self._record_owned(job_id, write, on_recorded=publish_if_committed)
        else:
            await publish_if_committed(await write())

    async def _publish_failed(self, row: dict[str, Any], *, retiring: bool = False) -> None:
        await self._broadcast({
            "type": "research_failed", "session_id": row["session_id"],
            "research_id": row["id"], "status": row["status"], "error": row["error"],
        }, retiring=retiring)

    # ---------------------------------------------------------------- cancel

    async def retire_session(self, session_id: str) -> None:
        """The Owner has stopped. Join its writers, then any receipts they handed off."""
        def matches(job: _ResearchJob) -> bool:
            return job.session_id == session_id

        errors = await self._retire_writers(matches)
        errors.extend(await self._retire_writers(matches, deliveries=True))
        self._raise_retirement_errors(errors)

    async def cancel_work(self, work_id: str) -> None:
        errors = await self._retire_writers(lambda job: job.work_id == work_id)
        self._raise_retirement_errors(errors)

    async def settle_work_returns(self, work_id: str) -> None:
        """Delegation has now closed the original child execution as well."""
        errors = await self._retire_writers(lambda job: job.work_id == work_id, deliveries=True)
        self._raise_retirement_errors(errors)

    @staticmethod
    def _raise_retirement_errors(errors: list[BaseException]) -> None:
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Original research resource retirement failed", errors)

    async def _retire_writers(self, matches: Callable[[_ResearchJob], bool], *, deliveries: bool = False) -> list[BaseException]:
        tasks: dict[asyncio.Task, _WriterKind] = {}
        for job in self._jobs.values():
            if not matches(job):
                continue
            self._publications.release(job.job_id)
            for kind, writer in job.writers.items():
                if (kind == "delivery") != deliveries:
                    continue
                task = writer.task
                if kind == "delivery" and job.retiring_delivery is not None and not job.retiring_delivery.done():
                    job.retiring_delivery.set_result(None)
                if task.done() and writer.recording is not None:
                    assert self.db is not None
                    self.db.health.require_healthy()
                    task = asyncio.create_task(writer.recording(), name=f"research-settle-{job.job_id}-{kind}")
                    self._register_task(job.job_id, task, kind=kind, on_retired=writer.on_retired, resuming=True)
                elif kind == "pipeline" and not (task.done() or task.cancelling() or writer.recording or writer.resuming):
                    task.cancel()
                tasks[task] = kind
        if tasks:
            await asyncio.wait(tasks)
        return [error for task, kind in tasks.items() if (error := self._writer_error(task, kind)) is not None]

    @staticmethod
    def _writer_error(task: asyncio.Task, kind: _WriterKind) -> BaseException | None:
        if task.cancelled():
            return RuntimeError("Research cancellation control retirement is unconfirmed") if kind == "control" else None
        error = task.exception()
        return None if kind == "control" and isinstance(error, ResearchError) else error

    async def cancel(self, job_id: str, *, row: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        if (control := self._task(job_id, "control")) is not None:
            return await asyncio.shield(control)
        if self._closing:
            raise ResearchError("ResearchManager is shutting down", status_code=503)
        original = self._task(job_id)
        job = self._jobs.get(job_id) or _ResearchJob(job_id, None, None)
        if row is not None and job.session_id is None:
            self._bind_stored_job(job, row)
        if closed := self._closure_error(job):
            raise closed
        timestamp = _now()

        async def cancel_original() -> dict[str, Any]:
            assert self.db is not None
            # A direct call can name a persisted job without a live writer.
            # Register its control before reading and check its binding before mutation.
            if job.session_id is None:
                stored = await self.db.get_research_job(job_id)
                if stored is None:
                    raise ResearchError(f"research job {job_id} not found", status_code=404)
                self._bind_stored_job(job, stored)
                if closed := self._closure_error(job):
                    raise closed
            return await self._record_owned(job_id, lambda: self._cancel_job(job_id, original, timestamp), kind="control")

        self._jobs[job_id] = job
        control = asyncio.create_task(cancel_original(), name=f"research-cancel-{job_id}")
        self._register_task(job_id, control, kind="control")
        return await asyncio.shield(control)

    @staticmethod
    def _bind_stored_job(job: _ResearchJob, row: dict[str, Any]) -> None:
        owned = decode_owned(row.get("knowledge_context"))
        job.session_id = row["session_id"]
        job.work_id = owned.return_binding.work_id if owned else None

    def _closure_error(self, job: _ResearchJob) -> ResearchError | None:
        if job.session_id is not None and self.session_mgr is not None and self.session_mgr.session_deleting(job.session_id):
            return ResearchError("Session admission is closed", status_code=409)
        if not work_is_open(job.work_id):
            return ResearchError("Original research work is closed", status_code=409)
        return None

    async def _cancel_job(self, job_id: str, original: asyncio.Task | None, timestamp: str) -> dict[str, Any]:
        assert self.db is not None
        _, row = await self.db.confirm_research_outcome(
            job_id, "cancelled", completed_at=timestamp, error="cancelled by user",
        )
        if row is None:
            raise ResearchError(f"research job {job_id} not found", status_code=404)
        if row["status"] == "cancelled" and original and not original.done() and not original.cancelling():
            self._publications.release(job_id)
            original.cancel()
        if (row["status"], row["completed_at"]) == ("cancelled", timestamp):
            await self._publish_failed(row, retiring=True)
        return row

    # --------------------------------------------------------------- helpers

    def drain_blockers(self) -> tuple[str, ...]:
        blockers = [f"cancellation control {job.job_id}" for job in self._jobs.values() if "control" in job.writers]
        if self._publications.pending or any(job.writers.keys() - {"control"} for job in self._jobs.values()):
            blockers.append("pipeline, return delivery, publication or cleanup")
        return tuple(blockers)

    def begin_shutdown(self) -> None:
        """Close admission. Existing controls must finish their original transactions."""
        if self._closing:
            return
        self._closing = True
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast("research.delivery")
        for job in self._jobs.values():
            for kind, writer in job.writers.items():
                if kind != "control" and not writer.task.done() and not writer.task.cancelling():
                    writer.task.cancel()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        self.begin_shutdown()
        await self._drain(deadline=asyncio.get_running_loop().time() + timeout)

    async def _drain(self, *, deadline: float | None = None) -> None:
        while True:
            writers = {writer.task: (job.job_id, kind) for job in self._jobs.values() for kind, writer in job.writers.items()}
            observed = writers.keys() | self._publications.pending
            if not observed:
                return
            timeout = max(0, deadline - asyncio.get_running_loop().time()) if deadline is not None else None
            _, pending = await asyncio.wait(observed, timeout=timeout)
            if pending:
                jobs = ", ".join(job_id for task, (job_id, _) in writers.items() if task in pending)
                raise TimeoutError(f"research shutdown retirement unconfirmed: {jobs or 'publication'}")
            for task, (_, kind) in writers.items():
                if (error := self._writer_error(task, kind)) is not None:
                    raise error

    def _write_report(self, job_id: str, report: str, user_id: str | None) -> str | None:
        path = os.path.join(paths_for(user_id).research, f"{job_id}.md")
        return path if self._write_material(path, report) is None else None

    def _write_evidence(self, job_id: str, session_id: str, evidence: ResearchEvidence, user_id: str | None) -> bool:
        path = os.path.join(paths_for(user_id).research, job_id, "evidence.json")
        payload = json.dumps({"job_id": job_id, "session_id": session_id, "user_id": user_id,
                              "evidence": evidence.model_dump()}, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            logger.error("research %s: evidence exceeds its size limit", job_id)
            return False
        return self._write_material(path, payload) is None

    @staticmethod
    def _write_material(path: str, text: str) -> str | None:
        """Return a safe failure reason, or None after the file and directory sync."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return None
        except Exception as exc:
            logger.exception("research: could not write material %s", path)
            return safe_diagnostic(f"{type(exc).__name__}: {exc}")

    async def public_job(self, row: dict[str, Any]) -> dict[str, Any]:
        """Read an original job's bounded material; never infer a new outcome.

        The caller checks access to the session. The persisted session binding,
        fixed job path and envelope independently bind the file to its owner.
        Missing old or damaged material is explicit, clearing cached evidence.
        """
        public = {key: value for key, value in row.items() if key != "knowledge_context"}
        public.update(evidence=None, evidence_state="unavailable")
        if row["status"] != "completed" or self.db is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"]):
            return public
        binding = await self.db.get_session_binding(row["session_id"])
        if binding is None:
            return public
        root = paths_for(binding["user_id"]).research.resolve()
        path = root / row["id"] / "evidence.json"
        try:
            if not path.resolve().is_relative_to(root):
                return public
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return public
                content = handle.read(MAX_EVIDENCE_BYTES + 1)
            if len(content) > MAX_EVIDENCE_BYTES:
                return public
            envelope = json.loads(content)
            if (not isinstance(envelope, dict) or envelope.get("job_id") != row["id"]
                    or envelope.get("session_id") != row["session_id"] or envelope.get("user_id") != binding["user_id"]):
                return public
            evidence = ResearchEvidence.model_validate(envelope.get("evidence"), context="stored")
        except (OSError, ValueError, UnicodeError, ValidationError, RecursionError):
            return public
        public.update(evidence=evidence.model_dump(), evidence_state="available")
        return public

    async def _broadcast(self, msg: dict[str, Any], *, retiring: bool = False) -> None:
        manager = self.session_mgr
        if manager is None:
            return
        job_id = msg["research_id"]
        job = self._jobs.get(job_id)
        retiring = retiring or manager.session_deleting(msg["session_id"]) or (
            job is not None and not work_is_open(job.work_id)
        )

        await self._publications.publish(job_id, lambda: manager._broadcast(msg), retiring=retiring, required=False)


# Module-level singleton (mirrors session_manager / bg_tasks / delegations).
research_manager = ResearchManager()

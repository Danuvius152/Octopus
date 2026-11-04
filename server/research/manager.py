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
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..aio import complete_cleanup
from ..config import settings
from ..harness import get_harness, has_backend, model_settings
from ..knowledge import (
    OwnedTaskContext,
    capture,
    decode_owned,
    derive,
    encode,
)
from ..mcp_identity import McpScope, work_is_open
from ..sessions.base import Session
from ..sessions.configuration import SessionConfigurationError
from ..sessions.owner_runtime import capture_configuration
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _delivery_result(completion: RequestCompletion) -> tuple[str, str | None]:
    successful = (completion.state == "completed" and completion.recording_state == "committed"
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
        self._tasks: dict[str, asyncio.Task] = {}
        # Cancellation controls must survive shutdown's signals to pipelines.
        self._cancel_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._contexts: dict[str, OwnedTaskContext] = {}
        self._job_sem: asyncio.Semaphore | None = None
        self._closing = False
        self._shutdown_tasks: dict[str, asyncio.Task] | None = None
        self._delivery_jobs: dict[str, tuple[str, str]] = {}

    def bind(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._job_sem = asyncio.Semaphore(max(1, settings.research_max_concurrent_jobs))
        self._closing = False
        self._shutdown_tasks = None
        session_mgr.on_broadcast("research.delivery", self._on_delivery_progress, required=False)

    def _register_task(self, key: str, task: asyncio.Task, *, on_retired: Callable[[], None] | None = None,
                       cancellation: bool = False) -> None:
        """Retain original work through cleanup, with controls kept unsignalled."""
        registry = self._cancel_tasks if cancellation else self._tasks
        registry[key] = task
        shutdown_key = f"cancel:{key}" if cancellation else key
        if self._shutdown_tasks is not None:
            # A shielded original pipeline may hand off its accepted receipt
            # after shutdown captured it. This is the same cleanup responsibility.
            self._shutdown_tasks[shutdown_key] = task
        def finished(done: asyncio.Task) -> None:
            error = None if done.cancelled() else done.exception()
            if cancellation and done.cancelled():
                logger.error("research cancellation control unexpectedly cancelled job=%s", key)
            elif error is not None and not (cancellation and isinstance(error, ResearchError)):
                logger.error("research %s cleanup failed", shutdown_key, exc_info=error)
            elif registry.get(key) is done:
                registry.pop(key)
                if on_retired is not None:
                    on_retired()
        task.add_done_callback(finished)

    async def wait_for_idle(self) -> None:
        """Drain current controls, pipelines and their delivery observers.

        This does not close admission. Cancelling a waiter never cancels the
        original research or receipt observation.
        """
        while self._tasks or self._cancel_tasks:
            tasks = (*self._tasks.values(), *self._cancel_tasks.values())
            results = await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
            if any(task.cancelled() for task in self._cancel_tasks.values()):
                raise RuntimeError("Research cancellation control retirement is unconfirmed")
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

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
        configured.credential_id = selected["credential_id"]
        # Configuration capture can wait. Admission and registration below are
        # synchronous, so shutdown cannot miss a job admitted after its snapshot.
        if self._closing:
            raise ResearchError("ResearchManager is shutting down", status_code=503)
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
        self._contexts[job_id] = owned
        def forget_context() -> None:
            self._contexts.pop(job_id, None)

        self._register_task(job_id, task, on_retired=forget_context)

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
            if not work_is_open(owned.return_binding.work_id):
                await self._finalize_failed(job_id, session_id, "cancelled", "original research work is closed")
                ready.set_exception(ResearchError("Original research work is closed", status_code=409))
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
            await self._run_job(job_id, session_id, question, configured)
        except asyncio.CancelledError:
            await self._finalize_failed(
                job_id, session_id,
                "interrupted" if self._closing else "cancelled",
                "research interrupted by server shutdown" if self._closing else "cancelled by user",
            )
            raise

    # ------------------------------------------------------------------- run

    async def _run_job(self, job_id: str, session_id: str, question: str, session: Session) -> None:
        assert self.session_mgr is not None and self.db is not None
        assert self._job_sem is not None
        report = None
        try:
            async with self._job_sem:  # bound concurrent jobs
                harness = get_harness(session.backend)
                user_id = self._contexts[job_id].execution.origin.user_id
                credential_id, backend = session.credential_id, session.backend
                database = self.db

                async def authorize_spawn():
                    await self.session_mgr.require_active_agent(session.agent_id)
                    await database.require_run_authority(user_id, credential_id, backend)

                try:
                    credential = await self.session_mgr.resolve_credential_by_id(
                        credential_id, backend=backend, context=f"research {job_id}", user_id=user_id,
                    )
                    await authorize_spawn()
                except ValueError as error:
                    raise ResearchError(str(error), status_code=403) from error
                if not work_is_open(self._contexts[job_id].return_binding.work_id):
                    raise asyncio.CancelledError("original research work is closed")

                async def on_progress(p: ResearchProgress) -> None:
                    assert self.db is not None
                    await self.db.update_research_job(job_id, phase=p.phase)
                    await self._broadcast({
                        "type": "research_progress",
                        "session_id": session_id,
                        "research_id": job_id,
                        "phase": p.phase,
                        "detail": p.detail,
                        "counts": p.counts,
                    })

                # Run leaves in an EMPTY per-job scratch cwd, NOT the session's
                # working_dir — a web/reasoning leaf has no reason to read the
                # user's repo, and this keeps the read-only-sandbox/denylist
                # leaves from touching real files (Vera review).
                scratch = os.path.join(_research_dir(session.user_id), job_id, "cwd")
                os.makedirs(scratch, exist_ok=True)
                async with asyncio.timeout(settings.research_job_timeout_seconds):
                    report = await run_research(
                        question,
                        harness=harness,
                        credential=credential,
                        model=session.model,
                        working_dir=scratch,
                        limits=ResearchLimits(),
                        on_progress=on_progress,
                        authorize_spawn=authorize_spawn,
                    )

            # Completion is selected before this await. Cancellation retires
            # its original record/delivery; it cannot select interruption.
            await complete_cleanup(self._complete_report(
                job_id, session_id, question, report, session.user_id,
            ))
        except asyncio.CancelledError:
            raise
        except ResearchSynthesisError as exc:
            evidence = os.path.join(_research_dir(session.user_id), job_id, "synthesis-failure.json")
            with open(evidence, "w", encoding="utf-8") as handle:
                json.dump({"error": str(exc), **asdict(exc.partial)}, handle, ensure_ascii=False, indent=2)
            await self._finalize_failed(job_id, session_id, "failed", str(exc))
        except TimeoutError:
            if report is not None or self._closing:
                raise
            await self._finalize_failed(
                job_id, session_id, "failed",
                f"research exceeded {settings.research_job_timeout_seconds}s and was stopped",
            )
        except ResearchError as e:
            if report is not None or self._closing:
                raise
            await self._finalize_failed(job_id, session_id, "failed", e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("research job %s crashed", job_id)
            current = asyncio.current_task()
            if report is not None or self._closing or (current and current.cancelling()):
                raise
            await self._finalize_failed(job_id, session_id, "failed", str(e))
            # An unexpected leaf failure can be unconfirmed retirement after
            # user cancellation. Keep its owner and exception for shutdown.
            raise

    async def _complete_report(
        self, job_id: str, session_id: str, question: str, report: Any,
        user_id: str | None,
    ) -> None:
        assert self.db is not None
        report_path = self._write_report(job_id, report.report, user_id)
        if report_path is None:
            await self._finalize_failed(job_id, session_id, "failed", "Research report could not be saved")
            return
        won, _ = await self.db.confirm_research_outcome(
            job_id, "completed", cost=report.cost,
            completed_at=_now(), report_path=report_path,
        )
        if not won:
            return
        await self._broadcast({
            "type": "research_completed",
            "session_id": session_id,
            "research_id": job_id,
            "sources": report.sources,
            "verified": sum(getattr(f, "verification", "unverified") == "verified" for f in report.findings),
            "unverified": sum(getattr(f, "verification", "unverified") != "verified" for f in report.findings),
            "injection_status": "pending",
            "report_path": report_path,
        })
        try:
            await self._inject_report(job_id, session_id, question, report)
        except Exception as error:
            # Keep unexpected errors visible without reclassifying them as a
            # refusal or permission to retry. Storage still owns fail-stop.
            self.db.health.require_healthy()
            row = await self.db.get_research_job(job_id)
            if row is not None and row["injection_status"] != "failed":
                message = f"Report delivery stopped ({type(error).__name__}): {error}"
                if row.get("error"):
                    message = f"{row['error']}\n{message}"
                await self.db.update_research_job(job_id, error=message)
                await self._broadcast({"type": "research_delivery", "session_id": session_id,
                                       "research_id": job_id, "injection_status": row["injection_status"], "error": message})
            raise

    async def _inject_report(
        self, job_id: str, session_id: str, question: str, report: Any
    ) -> None:
        """Own report retries/progress; sessions own the complete return handoff."""
        assert self.db is not None and self.session_mgr is not None

        async def prepare() -> ReturnBody | ReturnSkipped:
            assert self.db is not None
            current = await self.db.get_research_job(job_id)
            if (self._closing or current is None or current["session_id"] != session_id
                    or current["status"] != "completed" or current["injection_status"] != "pending"):
                return ReturnSkipped("The source no longer permits a new report input")
            return ReturnBody(f"[deep-research:{job_id}] Research complete for: {question}\n\n{report.report}")

        while True:
            row = await self.db.get_research_job(job_id)
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
        self._delivery_jobs[receipt.request_id] = (job_id, session_id)
        current = self.session_mgr.get_session(session_id)
        processing = current and current._run.request and current._run.request.request_id == receipt.request_id
        await self._advance_delivery(job_id, session_id, receipt.request_id, "processing" if processing else "queued")
        task = asyncio.create_task(self._observe_delivery(job_id, session_id, receipt), name=f"research-delivery-{job_id}")
        def forget_delivery() -> None:
            self._delivery_jobs.pop(receipt.request_id, None)

        self._register_task(f"delivery:{job_id}", task, on_retired=forget_delivery)

    async def _advance_delivery(self, job_id, session_id, request_id, stage, *, error=None):
        receipt = await self.db.advance_research_delivery(job_id, request_id, stage, error=error)
        if receipt:
            await self._broadcast({"type": "research_delivery", "session_id": session_id, "research_id": job_id,
                                   "injection_status": "delivered", "delivery_receipt": receipt})

    async def _on_delivery_progress(self, event):
        owner = self._delivery_jobs.get(event.get("request_id"))
        if event.get("type") == "dequeued" and owner and owner[1] == event.get("session_id"):
            await self._advance_delivery(*owner, event["request_id"], "processing")

    async def _observe_delivery(self, job_id, session_id, receipt):
        try:
            if self._closing:
                raise asyncio.CancelledError
            completion = await receipt.wait()
            stage, error = _delivery_result(completion)
            await self._advance_delivery(job_id, session_id, receipt.request_id, stage, error=error)
        except asyncio.CancelledError:
            await self._advance_delivery(job_id, session_id, receipt.request_id, "unknown", error="Delivery observation stopped; the request was not replayed.")
            raise

    async def _settle_delivery(self, job_id: str, session_id: str, *, error: str, unknown: bool = False) -> None:
        """Persist a refused or uncertain return independently of the report."""
        assert self.db is not None and self.session_mgr is not None
        status = "unknown" if unknown else "failed"
        await self.db.update_research_job(job_id, injection_status=status, error=error,
                                          injected_at=None)
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

    async def _finalize_failed(
        self, job_id: str, session_id: str, status: str, error: str
    ) -> tuple[bool, dict[str, Any] | None]:
        assert self.db is not None
        won, row = await self.db.confirm_research_outcome(
            job_id, status, error=error, completed_at=_now(),
        )
        if won:
            assert row is not None
            await self._publish_failed(row)
        return won, row

    async def _publish_failed(self, row: dict[str, Any]) -> None:
        await self._broadcast({
            "type": "research_failed", "session_id": row["session_id"],
            "research_id": row["id"], "status": row["status"], "error": row["error"],
        })

    # ---------------------------------------------------------------- cancel

    async def cancel_work(self, work_id: str) -> None:
        """Join the original launch/pipeline owners after closing their work."""
        tasks = [self._tasks[job_id] for job_id, context in self._contexts.items()
                 if context.return_binding.work_id == work_id and job_id in self._tasks]
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        results = await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def cancel(self, job_id: str) -> dict[str, Any]:
        if self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        control = self._cancel_tasks.get(job_id)
        if control is None:
            if self._closing:
                raise ResearchError("ResearchManager is shutting down", status_code=503)
            # Capture the original pipeline before any DB wait. A late control
            # must never signal a replacement registered under the same key.
            original = self._tasks.get(job_id)
            control = asyncio.create_task(self._cancel_job(job_id, original), name=f"research-cancel-{job_id}")
            self._register_task(job_id, control, cancellation=True)
        return await asyncio.shield(control)

    async def _cancel_job(self, job_id: str, original: asyncio.Task | None) -> dict[str, Any]:
        assert self.db is not None
        won, row = await self.db.confirm_research_outcome(
            job_id, "cancelled", completed_at=_now(), error="cancelled by user",
        )
        if row is None:
            raise ResearchError(f"research job {job_id} not found", status_code=404)
        if row["status"] == "cancelled" and original and not original.done() and not original.cancelling():
            original.cancel()  # reaps in-flight leaves via CancelledError
        if won:
            await self._publish_failed(row)
        return row

    # --------------------------------------------------------------- helpers

    def drain_blockers(self) -> tuple[str, ...]:
        # Failed teardown tasks remain owned until their original cleanup is confirmed.
        return (("pipeline, return delivery or cleanup",) if self._tasks else ()) + tuple(
            f"cancellation control {job_id}" for job_id in self._cancel_tasks
        )

    def begin_shutdown(self) -> None:
        """Close admission and signal the jobs this manager already owns."""
        if self._closing:
            return
        self._closing = True
        self._shutdown_tasks = {**self._tasks, **{f"cancel:{key}": task for key, task in self._cancel_tasks.items()}}
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast("research.delivery")
        for task in self._tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Wait for owned jobs to unwind without cancelling their cleanup again."""
        self.begin_shutdown()
        tasks = self._shutdown_tasks or {}
        if not tasks:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            _, pending = await asyncio.wait(tuple(tasks.values()), timeout=max(0, deadline - asyncio.get_running_loop().time()))
            if pending:
                jobs = ", ".join(job_id for job_id, task in tasks.items() if task in pending)
                raise TimeoutError(f"research shutdown retirement unconfirmed: {jobs}")
            if all(task.done() for task in tasks.values()):
                break
        for key, task in tasks.items():
            if key.startswith("cancel:") and task.cancelled():
                raise RuntimeError(f"Research cancellation control retirement is unconfirmed: {key}")
            if not task.cancelled():
                if key.startswith("cancel:") and isinstance(task.exception(), ResearchError):
                    continue  # A missing job is a domain refusal, not failed cleanup.
                task.result()

    def _write_report(self, job_id: str, report: str, user_id: str | None) -> str | None:
        try:
            path = os.path.join(_research_dir(user_id), f"{job_id}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
                fh.flush()
                os.fsync(fh.fileno())
            directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return path
        except Exception:
            logger.exception("research %s: could not write report file", job_id)
            return None

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        if self.session_mgr is not None:
            try:
                await self.session_mgr._broadcast(msg)
            except Exception:
                logger.exception("research broadcast failed")


# Module-level singleton (mirrors session_manager / bg_tasks / delegations).
research_manager = ResearchManager()

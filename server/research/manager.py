"""ResearchManager — tracks deep-research jobs as async tasks
(native-deep-research.md §6). Mirrors the bg-task / delegation managers:

  - `start(session_id, question)` persists a `research_jobs` row, returns the
    id immediately, and runs the pipeline as a tracked `asyncio.Task`.
  - Progress is broadcast over the session bus + written to the row's `phase`.
  - On success the report is written to a file AND injected into the session as
    a turn via `start_message` (the bg-delivery path); completion and delivery
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
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..aio import complete_cleanup
from ..config import settings
from ..harness import get_harness, has_backend
from ..knowledge import (
    OwnedTaskContext,
    capture,
    decode_owned,
    derive,
    encode,
    receive_return,
)
from ..mcp_identity import McpScope, work_is_open
from ..sessions.runtime_types import SessionOwnerUnavailable
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
        self._contexts: dict[str, OwnedTaskContext] = {}
        self._job_sem: asyncio.Semaphore | None = None
        self._closing = False
        self._shutdown_tasks: dict[str, asyncio.Task] | None = None
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self._delivery_jobs: dict[str, tuple[str, str]] = {}

    def bind(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._job_sem = asyncio.Semaphore(max(1, settings.research_max_concurrent_jobs))
        self._closing = False
        self._shutdown_tasks = None
        session_mgr.on_broadcast("research.delivery", self._on_delivery_progress)

    async def recover_interrupted(self) -> int:
        """Boot sweep — mark prior-process `running` jobs interrupted."""
        if self.db is None:
            return 0
        n = await self.db.mark_in_flight_research_jobs_interrupted(_now())
        for row in await self.db.pending_research_deliveries():
            request_id = row["delivery_receipt"]["request_id"]
            outcome = await self.db.get_committed_terminal(row["session_id"], request_id)
            stage = "processed" if outcome and outcome.get("status") == "success" else "failed" if outcome else "unknown"
            await self.db.advance_research_delivery(row["id"], request_id, stage,
                                                   error=None if outcome else "Delivery observation was interrupted; the request was not replayed.")
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
        if not has_backend(session.backend):
            raise ResearchError(f"unknown backend {session.backend!r}", status_code=400)
        harness = get_harness(session.backend)
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
            self._start_job(job_id, session_id, question, ready, owned),
            name=f"research-{job_id}",
        )
        self._tasks[job_id] = task
        self._contexts[job_id] = owned

        def finished(done: asyncio.Task) -> None:
            error = None if done.cancelled() else done.exception()
            if error is None:
                self._tasks.pop(job_id, None)
                self._contexts.pop(job_id, None)
            if not ready.done():
                ready.set_exception(error or ResearchError(
                    "ResearchManager is shutting down" if self._closing else "research launch cancelled",
                    status_code=503,
                ))
            if error is not None:
                logger.error("research %s cleanup failed", job_id, exc_info=error)

        task.add_done_callback(finished)
        # A cancelled HTTP waiter does not surrender ownership of admitted work.
        ready.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        return await asyncio.shield(ready)

    async def _start_job(
        self, job_id: str, session_id: str, question: str,
        ready: asyncio.Future[dict[str, Any]],
        owned: OwnedTaskContext,
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
            await self._run_job(job_id, session_id, question)
        except asyncio.CancelledError:
            await self._finalize_failed(
                job_id, session_id,
                "interrupted" if self._closing else "cancelled",
                "research interrupted by server shutdown" if self._closing else "cancelled by user",
            )
            raise

    # ------------------------------------------------------------------- run

    async def _run_job(self, job_id: str, session_id: str, question: str) -> None:
        assert self.session_mgr is not None and self.db is not None
        assert self._job_sem is not None
        report = None
        try:
            async with self._job_sem:  # bound concurrent jobs
                session = self.session_mgr.get_session(session_id)
                if session is None:
                    raise ResearchError("session disappeared", status_code=404)
                harness = get_harness(session.backend)
                agent = await self.db.get_agent(session.agent_id) if session.agent_id else None
                model = (agent or {}).get("model")
                user_id = self._contexts[job_id].execution.origin.user_id
                credential_id, backend = session.credential_id or (agent or {}).get("credential_id"), session.backend
                database = self.db

                async def authorize_spawn():
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
                        model=model,
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
        await self.db.update_research_job(
            job_id, status="completed", phase="done", cost=report.cost,
            completed_at=_now(), report_path=report_path,
        )
        await self._broadcast({
            "type": "research_completed",
            "session_id": session_id,
            "research_id": job_id,
            "sources": report.sources,
            "verified": sum(getattr(f, "verification", "unverified") == "verified" for f in report.findings),
            "unverified": sum(getattr(f, "verification", "unverified") != "verified" for f in report.findings),
            "injection_status": "pending",
        })
        await self._inject_report(job_id, session_id, question, report)

    async def _inject_report(
        self, job_id: str, session_id: str, question: str, report: Any
    ) -> None:
        """Deliver the report into the session as a turn (the bg-delivery path).
        Idempotent + tracked separately from completion (Vera review)."""
        assert self.db is not None and self.session_mgr is not None
        prompt = (
            f"[deep-research:{job_id}] Research complete for: {question}\n\n"
            f"{report.report}"
        )
        while True:
            row = await self.db.get_research_job(job_id)
            if row and row.get("injection_status") == "delivered":
                return
            existing = await self.session_mgr.source_receipt("research", f"research:{job_id}", session_id=session_id)
            if existing is not None:
                receipt = existing[1]
                break
            if self.session_mgr.session_owner_failed(session_id):
                await self._settle_delivery(job_id, session_id, error="delivery suppressed: destination owner is unavailable")
                return
            if self._closing or self.session_mgr._admission_closed:
                await self._settle_delivery(job_id, session_id, error="delivery suppressed: session admission is closed")
                return
            session = self.session_mgr.get_session(session_id)
            owned = decode_owned(row.get("knowledge_context")) if row else None
            work_id = owned.return_binding.work_id if owned else None
            context = receive_return(
                owned, session_id=session_id, user_id=session.user_id if session else None,
                work_id=work_id,
            )
            if (row is None or row["session_id"] != session_id or row["status"] != "completed"
                    or session is None or context is None):
                await self._settle_delivery(job_id, session_id,
                    error="delivery suppressed: original request contract is missing, closed or mismatched")
                return
            try:
                receipt = await self.session_mgr.start_message(
                    session_id, prompt, source="research", delivery_id=f"research:{job_id}",
                    work_id=work_id, knowledge_context=context,
                )
                if not receipt.accepted:
                    if self.session_mgr._admission_closed:
                        continue  # Confirm admission before recording a closed destination.
                    await self._settle_delivery(job_id, session_id, error="delivery suppressed: session admission is closed")
                    return
                break
            except Exception:
                logger.exception("research %s: admission acknowledgment pending; retrying the same report", job_id)
            if not self._closing:
                await asyncio.sleep(1)
        self._delivery_jobs[receipt.request_id] = (job_id, session_id)
        current = self.session_mgr.get_session(session_id)
        processing = current and current._run.request and current._run.request.request_id == receipt.request_id
        await self._advance_delivery(job_id, session_id, receipt.request_id, "processing" if processing else "queued")
        await self._settle_delivery(job_id, session_id)
        task = asyncio.create_task(self._observe_delivery(job_id, session_id, receipt), name=f"research-delivery-{job_id}")
        self._delivery_tasks[job_id] = task
        self._tasks[f"delivery:{job_id}"] = task
        def finished(done):
            if self._delivery_tasks.get(job_id) is done:
                self._delivery_tasks.pop(job_id, None)
            self._delivery_jobs.pop(receipt.request_id, None)
            self._tasks.pop(f"delivery:{job_id}", None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Research delivery observation failed", exc_info=done.exception())
        task.add_done_callback(finished)

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
            completion = await receipt.wait()
            successful = (completion.state == "completed" and completion.recording_state == "committed"
                          and completion.outcome is not None and completion.outcome.status == "success")
            stage = ("processed" if successful else "unknown" if completion.state == "unavailable" else
                     "recording_unconfirmed" if completion.recording_state in {"failed", "unconfirmed"} else "failed")
            await self._advance_delivery(job_id, session_id, receipt.request_id, stage, error=None if successful else completion.reason or completion.state)
        except asyncio.CancelledError:
            await self._advance_delivery(job_id, session_id, receipt.request_id, "unknown", error="Delivery observation stopped; the request was not replayed.")
            raise

    async def _settle_delivery(self, job_id: str, session_id: str, *, error: str | None = None) -> None:
        """Persist and publish delivery independently of report completion."""
        assert self.db is not None and self.session_mgr is not None
        status = "failed" if error else "delivered"
        await self.db.update_research_job(job_id, injection_status=status, error=error,
                                          injected_at=None if error else _now())
        if error:
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
    ) -> None:
        assert self.db is not None
        try:
            # Idempotent: if the job is already terminal (e.g. cancel() recorded
            # it before interrupting), don't overwrite or re-broadcast (Vera
            # review — avoids the cancel double-write/double-event).
            row = await self.db.get_research_job(job_id)
            if row is None or row["status"] != "running":
                return
            await self.db.update_research_job(
                job_id, status=status, error=error, completed_at=_now()
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": session_id,
                "research_id": job_id,
                "status": status,
                "error": error,
            })
        except Exception:
            logger.exception("research %s: finalize(%s) failed", job_id, status)
            if self._closing:
                raise

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
        row = await self.db.get_research_job(job_id)
        if row is None:
            raise ResearchError(f"research job {job_id} not found", status_code=404)
        # Record cancelled + broadcast BEFORE interrupting live work, so the
        # state transition is authoritative and the REST caller/UI never see a
        # stale `running` (Vera review — cancel-as-state-transition). The
        # CancelledError path's _finalize_failed is now idempotent, so it won't
        # double-write. No-op if already terminal.
        if row["status"] == "running":
            await self.db.update_research_job(
                job_id, status="cancelled", error="cancelled by user",
                completed_at=_now(),
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": row["session_id"],
                "research_id": job_id,
                "status": "cancelled",
                "error": "cancelled by user",
            })
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()  # reaps in-flight leaves via CancelledError
        row = await self.db.get_research_job(job_id)
        assert row is not None  # written immediately above
        return row

    # --------------------------------------------------------------- helpers

    def begin_shutdown(self) -> None:
        """Close admission and signal the jobs this manager already owns."""
        if self._closing:
            return
        self._closing = True
        self._shutdown_tasks = {**self._tasks, **{f"delivery:{key}": task for key, task in self._delivery_tasks.items()}}
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast("research.delivery")
        for task in self._shutdown_tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Wait for owned jobs to unwind without cancelling their cleanup again."""
        self.begin_shutdown()
        tasks = self._shutdown_tasks or {}
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
        if pending:
            jobs = ", ".join(job_id for job_id, task in tasks.items() if task in pending)
            raise TimeoutError(f"research shutdown retirement unconfirmed: {jobs}")
        for task in done:
            if not task.cancelled():
                task.result()

    def _write_report(self, job_id: str, report: str, user_id: str | None) -> str | None:
        try:
            path = os.path.join(_research_dir(user_id), f"{job_id}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
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

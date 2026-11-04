"""ResearchManager + research_jobs persistence (native-deep-research.md §6).

The initial pipeline tests use a fake report. Shutdown regressions exercise
the actual pipeline, harness and SQLite with a synthetic provider CLI.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from server.database import Database
from server.harness import Harness, get_harness
from server.harness.launch_limits import LaunchConfigurationError
from server.harness.registry import _REGISTRY
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchProgress, ResearchReport
from server.session_manager import SessionManager
from tests.accounts import create_account
from tests.submission import accepted_receipt


@pytest.fixture
async def db(tmp_path):
    d = Database(":memory:")
    await d.initialize()
    await create_account(d, extra_roots=[tmp_path])
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def rm(mgr, db):
    r = ResearchManager()
    r.bind(session_mgr=mgr, db=db)
    yield r


async def _session(mgr, db, *, backend="claude-code"):
    agent = await db.get_system_agent()
    return await mgr.create_session(agent["id"], "S", backend=backend)


@pytest.mark.parametrize("kind", ["conflict", "value", "program"])
async def test_unclassified_delivery_error_preserves_original_exception_and_completed_report(rm, mgr, db, monkeypatch, kind):
    from server.submission_models import SubmissionConflict

    session = await _session(mgr, db)
    failure = SubmissionConflict("Original identity conflict") if kind == "conflict" else ValueError("Original value bug") if kind == "value" else RuntimeError("Original programming bug")
    attempts = []

    async def report(*args, **kwargs):
        return _fake_report()

    async def reject(*args, **kwargs):
        attempts.append(kwargs["delivery_id"])
        raise failure

    monkeypatch.setattr(rm_mod, "run_research", report)
    monkeypatch.setattr(mgr, "start_message", reject)
    job = await rm.start(session.id, "Preserve the original exception")
    with pytest.raises(type(failure)) as caught:
        await asyncio.wait_for(asyncio.shield(rm._tasks[job["id"]]), 2)
    assert caught.value is failure
    row = await db.get_research_job(job["id"])
    assert row["status"] == "completed"
    assert type(failure).__name__ in row["error"] and str(failure) in row["error"]
    assert "Final report" in Path(row["report_path"]).read_text()
    assert attempts == [f"research:{job['id']}"]
    with pytest.raises(type(failure)):
        await rm.shutdown()


async def test_unavailable_binding_finishes_job_and_does_not_poison_shutdown(rm, mgr, db, monkeypatch):
    session = await _session(mgr, db)
    await db.save_credential("revoked", session.backend, "owned", "api_key", "", "now", user_id=session.user_id)
    session.credential_id = "revoked"
    await db.update_credential("revoked", needs_reconnect=True)

    async def forbidden_pipeline(*args, **kwargs):
        pytest.fail("Unavailable authentication must be rejected before the pipeline")

    monkeypatch.setattr(rm_mod, "run_research", forbidden_pipeline)
    job = await rm.start(session.id, "A rejected research job")
    await rm._tasks[job["id"]]
    row = await db.get_research_job(job["id"])
    assert row["status"] == "failed"
    assert "unavailable" in row["error"]
    assert job["id"] not in rm._contexts
    await rm.shutdown()


async def test_oversized_configuration_finishes_research_and_releases_owner(rm, mgr, db, monkeypatch):
    from server.harness.launch_limits import validate_launch

    session = await _session(mgr, db)
    profile = get_harness(session.backend).profile
    spawned = []

    def oversized_turn(ctx):
        return [sys.executable, "x" * 150000], {"cwd": ctx.working_dir}

    def oversized_oneshot(ctx):
        return [sys.executable, "x" * 150000], {"cwd": ctx.working_dir}

    async def forbidden_spawn(*args, **kwargs):
        spawned.append(args)
        pytest.fail("Refused research configuration must never spawn")

    # Native launch refusal is known before a process exists.
    with pytest.raises(LaunchConfigurationError):
        validate_launch([sys.executable, "x" * 150000], {})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    monkeypatch.setitem(_REGISTRY, session.backend, Harness(replace(profile,
        build_turn_argv=oversized_turn, build_oneshot_argv=oversized_oneshot, activity_observer=None)))
    job = await rm.start(session.id, "A refused research configuration")
    await rm._tasks[job["id"]]
    row = await db.get_research_job(job["id"])
    assert row["status"] == "failed" and "bytes" in row["error"]
    assert job["id"] not in rm._contexts
    assert not spawned
    await rm.shutdown()


async def test_closed_work_cancellation_joins_research_cleanup(rm, mgr, db, monkeypatch):
    from server.mcp_identity import close_work

    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_pipeline(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(rm_mod, "run_research", blocked_pipeline)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "Retire this research")
    await asyncio.wait_for(started.wait(), 3)
    work_id = rm._contexts[job["id"]].return_binding.work_id
    close_work(work_id)
    stopping = asyncio.create_task(rm.cancel_work(work_id))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not stopping.done(), "Cancellation acknowledgement must wait for tool cleanup"
        release.set()
        await stopping
        assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        await rm.shutdown()


def _fake_report(question="q"):
    return ResearchReport(
        question=question, report="# Final report\nThe answer [http://a].",
        findings=[], sources=["http://a"], angles=["x"], claims_examined=3, cost=0.05,
    )


@pytest.fixture
async def pending_report(rm, mgr, db, monkeypatch):
    from server.sessions.submission import SubmissionReceipt

    completion = asyncio.get_running_loop().create_future()
    async def pipeline(*args, **kwargs):
        return _fake_report()
    async def accept(*args, **kwargs):
        return SubmissionReceipt("accepted", "observed-report", completion)
    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", accept)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "Wait for report processing")
    await rm._tasks[job["id"]]  # Establish that the primary pipeline has ended.
    yield job, completion


async def test_report_observer_failure_remains_visible_to_shutdown(rm, db, pending_report, monkeypatch):
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion

    job, completion = pending_report
    failed = asyncio.Event()
    advance = db.advance_research_delivery
    async def reject_terminal(job_id, request_id, stage, **kwargs):
        if stage == "processed":
            failed.set()
            raise OSError("report observation recording failed")
        return await advance(job_id, request_id, stage, **kwargs)
    monkeypatch.setattr(db, "advance_research_delivery", reject_terminal)
    completion.set_result(RequestCompletion("observed-report", "observed-report", "completed",
        TurnOutcome(request_id="observed-report", status="success"), "committed"))
    await asyncio.wait_for(failed.wait(), 2)
    await asyncio.sleep(0)
    with pytest.raises(OSError, match="observation recording failed"):
        await rm.shutdown()
    assert (await db.get_research_job(job["id"]))["delivery_receipt"]["stage"] == "queued"


async def test_natural_drain_waits_for_report_processing_without_a_browser(rm, db, pending_report):
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion

    job, completion = pending_report
    draining = asyncio.create_task(rm.wait_for_idle())
    await asyncio.sleep(0)
    assert not draining.done()
    assert (await db.get_research_job(job["id"]))["status"] == "completed"
    completion.set_result(RequestCompletion("observed-report", "observed-report", "completed",
        TurnOutcome(request_id="observed-report", status="success"), "committed"))
    await asyncio.wait_for(draining, 2)
    receipt = (await db.get_research_job(job["id"]))["delivery_receipt"]
    assert receipt["stage"] == "processed"
    assert "browser_confirmed_version" not in receipt


async def test_observer_shutdown_timeout_retains_original_cancellation_cleanup(rm, mgr, db, pending_report, monkeypatch):
    job, _ = pending_report
    cleaning, release = asyncio.Event(), asyncio.Event()
    advance = db.advance_research_delivery
    async def held_unknown(job_id, request_id, stage, **kwargs):
        if stage == "unknown":
            cleaning.set()
            await release.wait()
        return await advance(job_id, request_id, stage, **kwargs)
    monkeypatch.setattr(db, "advance_research_delivery", held_unknown)
    retry = None
    try:
        with pytest.raises(TimeoutError, match=job["id"]):
            await rm.shutdown(timeout=.01)
        assert cleaning.is_set()
        with pytest.raises(rm_mod.ResearchError, match="shutting down"):
            await rm.start((await db.get_research_job(job["id"]))["session_id"], "refused")
        retry = asyncio.create_task(rm.shutdown())
        await asyncio.sleep(0)
        assert not retry.done()
        release.set()
        await asyncio.wait_for(retry, 2)
        row = await db.get_research_job(job["id"])
        assert row["status"] == "completed" and row["delivery_receipt"]["stage"] == "unknown"
    finally:
        release.set()
        if retry is not None:
            await retry


async def test_old_task_callback_cannot_retire_its_replacement(rm):
    first, second = asyncio.Event(), asyncio.Event()
    retired = []
    old = asyncio.create_task(first.wait())
    replacement = asyncio.create_task(second.wait())
    rm._register_task("same-owner", old, on_retired=lambda: retired.append("old"))
    rm._register_task("same-owner", replacement, on_retired=lambda: retired.append("replacement"))
    first.set()
    await old
    draining = asyncio.create_task(rm.wait_for_idle())
    await asyncio.sleep(0)
    assert not draining.done() and retired == []
    second.set()
    await asyncio.wait_for(draining, 2)
    assert retired == ["replacement"]


async def test_shutdown_joins_observer_created_by_original_pipeline_cleanup(rm, mgr, db, monkeypatch):
    from server.sessions.submission import RequestCompletion, SubmissionReceipt

    accepted, release, cleaning, cleaned = (asyncio.Event() for _ in range(4))
    completion = asyncio.get_running_loop().create_future()
    advance = db.advance_research_delivery
    async def pipeline(*args, **kwargs):
        return _fake_report()
    async def accept(*args, **kwargs):
        return SubmissionReceipt("accepted", "late-observer", completion)
    async def held_unknown(job_id, request_id, stage, **kwargs):
        if stage == "queued":
            result = await advance(job_id, request_id, stage, **kwargs)
            accepted.set()
            await release.wait()
            return result
        if stage == "unknown":
            cleaning.set()
            await cleaned.wait()
        return await advance(job_id, request_id, stage, **kwargs)
    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", accept)
    monkeypatch.setattr(db, "advance_research_delivery", held_unknown)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "Admission already committed")
    await asyncio.wait_for(accepted.wait(), 2)
    rm.begin_shutdown()
    release.set()
    try:
        with pytest.raises(TimeoutError, match=job["id"]):
            await rm.shutdown(timeout=.02)
        assert cleaning.is_set()
        cleaned.set()
        await rm.shutdown()
        row = await db.get_research_job(job["id"])
        assert row["status"] == "completed" and row["delivery_receipt"]["stage"] == "unknown"
    finally:
        release.set()
        cleaned.set()
        if not completion.done():
            completion.set_result(RequestCompletion("late-observer", "late-observer", "unavailable"))
        await rm.wait_for_idle()


async def test_cancel_confirmed_before_report_commit_cannot_become_completed(rm, mgr, db, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    complete = rm._complete_report

    async def held_completion(*args, **kwargs):
        entered.set()
        await release.wait()
        await complete(*args, **kwargs)

    async def pipeline(*args, **kwargs):
        return _fake_report()

    injected = []
    async def deliver(*args, **kwargs):
        injected.append(args)
        return accepted_receipt()

    monkeypatch.setattr(rm, "_complete_report", held_completion)
    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", deliver)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "cancel before commit")
    task = rm._tasks[job["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert (await rm.cancel(job["id"]))["status"] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
    assert not injected


async def test_report_write_failure_never_completes_or_delivers(rm, mgr, db, monkeypatch):
    async def pipeline(*args, **kwargs):
        return _fake_report()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    def reject_report_sync(fd):
        raise OSError("Report persistence refused")

    monkeypatch.setattr(os, "fsync", reject_report_sync)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "must save the report")
    await rm._tasks[job["id"]]
    row = await db.get_research_job(job["id"])
    assert row["status"] == "failed"
    assert row["injection_status"] == "pending"


async def test_cancel_after_completion_preserves_pending_delivery(rm, mgr, db, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def pipeline(*args, **kwargs):
        return _fake_report()

    async def deliver(*args, **kwargs):
        entered.set()
        await release.wait()
        delivered.append(args)
        return accepted_receipt()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", deliver)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "completed before cancel")
    task = rm._tasks[job["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    try:
        row = await rm.cancel(job["id"])
        assert row["status"] == "completed"
        assert Path(row["report_path"]).read_text().startswith("# Final report")
    finally:
        release.set()
        await task
    assert delivered
    assert (await db.get_research_job(job["id"]))["injection_status"] == "delivered"


@pytest.mark.parametrize("phase", ["sql", "committed"])
async def test_cancel_waiter_cannot_leave_a_committed_cancellation_without_pipeline_stop(rm, mgr, db, monkeypatch, phase):
    started, stopping, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_cleanup, entered, release_sql = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pipeline(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopping.set()
            await release_cleanup.wait()
            retired.set()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "cancel across the real transaction")
    original = rm._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    execute, commit = db.conn._execute, db.conn.commit
    updating = False

    async def gated_execute(function, *args, **kwargs):
        nonlocal updating
        result = await execute(function, *args, **kwargs)
        if args and isinstance(args[0], str) and args[0].startswith("UPDATE research_jobs SET status=") and not entered.is_set():
            updating = True
            if phase == "sql":
                entered.set()
                await release_sql.wait()
        return result

    async def gated_commit():
        await commit()
        if updating and phase == "committed" and not entered.is_set():
            entered.set()
            await release_sql.wait()

    monkeypatch.setattr(db.conn, "_execute", gated_execute)
    monkeypatch.setattr(db.conn, "commit", gated_commit)
    waiter = asyncio.create_task(rm.cancel(job["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        release_sql.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
        await asyncio.wait_for(stopping.wait(), 1)
        assert not retired.is_set()
        assert rm.drain_blockers(), "A cancelled result is not pipeline retirement"
        release_cleanup.set()
        await rm.wait_for_idle()
        assert retired.is_set() and original.done()
    finally:
        release_sql.set()
        release_cleanup.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await rm.shutdown()


async def test_repeated_cancel_stops_a_previously_cancelled_job_and_joins_original_cleanup(rm, mgr, db, monkeypatch):
    started, stopping, release, retired = (asyncio.Event() for _ in range(4))

    async def pipeline(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopping.set()
            await release.wait()
            retired.set()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "finish the original cancellation")
    original = rm._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    # A prior transaction can have committed without its old waiter signalling.
    won, saved = await db.confirm_research_outcome(job["id"], "cancelled", completed_at="original cancellation")
    assert won
    draining = None
    try:
        first = await rm.cancel(job["id"])
        await asyncio.wait_for(stopping.wait(), 2)
        second = await rm.cancel(job["id"])
        assert first == second == saved
        assert original.cancelling() == 1, "A repeat signal would interrupt original cleanup"
        draining = asyncio.create_task(rm.wait_for_idle())
        with pytest.raises(TimeoutError, match=job["id"]):
            await rm.shutdown(timeout=.01)
        assert not draining.done() and not retired.is_set()
        assert rm.drain_blockers()
        release.set()
        await asyncio.wait_for(draining, 2)
        await rm.shutdown()
        assert retired.is_set() and not rm.drain_blockers()
    finally:
        release.set()
        if draining is not None:
            await draining
        await rm.shutdown()


async def test_shutdown_joins_admitted_cancellation_without_cancelling_its_transaction(rm, mgr, db, monkeypatch):
    started = asyncio.Event()

    async def pipeline(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "cancel before shutdown")
    await asyncio.wait_for(started.wait(), 2)
    async with db._write_lock:
        waiter = asyncio.create_task(rm.cancel(job["id"]))
        async with asyncio.timeout(2):
            while job["id"] not in rm._cancel_tasks:
                await asyncio.sleep(0)
        # Let its original control reach the actual held database lock first.
        await asyncio.sleep(0)
        control = rm._cancel_tasks[job["id"]]
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with pytest.raises(TimeoutError, match=job["id"]):
            await rm.shutdown(timeout=.01)
        assert not control.done() and control.cancelling() == 0
        assert any(job["id"] in blocker for blocker in rm.drain_blockers())
    await rm.shutdown()
    assert control.done() and not control.cancelled()
    assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
    assert not rm.drain_blockers()


async def test_late_cancellation_signals_only_its_captured_pipeline(rm, mgr, db):
    session = await _session(mgr, db)
    await db.create_research_job("original-job", session.id, "q", "now")
    original = asyncio.create_task(asyncio.Event().wait())
    replacement_release = asyncio.Event()
    replacement = asyncio.create_task(replacement_release.wait())
    rm._register_task("original-job", original)
    waiter = None
    try:
        async with db._write_lock:
            waiter = asyncio.create_task(rm.cancel("original-job"))
            async with asyncio.timeout(2):
                while "original-job" not in rm._cancel_tasks:
                    await asyncio.sleep(0)
            rm._register_task("original-job", replacement)
        assert (await waiter)["status"] == "cancelled"
        await asyncio.gather(original, return_exceptions=True)
        assert original.cancelled()
        assert not replacement.done() and replacement.cancelling() == 0
        assert rm._tasks["original-job"] is replacement
    finally:
        replacement_release.set()
        await asyncio.gather(*(task for task in (original, replacement, waiter) if task is not None), return_exceptions=True)
        await rm.shutdown()


async def test_missing_job_cancellation_is_a_domain_error_during_shutdown(rm, db):
    async with db._write_lock:
        waiter = asyncio.create_task(rm.cancel("missing-job"))
        async with asyncio.timeout(2):
            while "missing-job" not in rm._cancel_tasks:
                await asyncio.sleep(0)
        rm.begin_shutdown()
    with pytest.raises(rm_mod.ResearchError) as error:
        await waiter
    assert error.value.status_code == 404
    await rm.shutdown()
    assert not rm.drain_blockers()


@pytest.mark.parametrize("completion_kind", ["delivered", "notification_failed", "unavailable"])
async def test_delivery_receipts_distinguish_queue_processing_and_current_browser_confirmation(
    rm, mgr, db, monkeypatch, completion_kind,
):
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion, SubmissionReceipt
    future = asyncio.get_running_loop().create_future()
    async def fake_start(*args, **kwargs):
        return SubmissionReceipt("accepted", "report-follow-up", future)
    async def fake_run(*args, **kwargs):
        return _fake_report()
    monkeypatch.setattr(mgr, "start_message", fake_start)
    monkeypatch.setattr(rm_mod, "run_research", fake_run)
    session = await _session(mgr, db)
    row = await rm.start(session.id, "question")
    await rm._tasks[row["id"]]
    receipt = (await db.get_research_job(row["id"]))["delivery_receipt"]
    assert receipt["stage"] == "queued" and "processed_at" not in receipt
    assert await db.acknowledge_research_delivery(row["id"], "wrong-request", receipt["version"]) is None
    await mgr._broadcast({"type": "dequeued", "session_id": session.id, "request_id": "report-follow-up"})
    processing = (await db.get_research_job(row["id"]))["delivery_receipt"]
    assert processing["stage"] == "processing"
    if completion_kind == "unavailable":
        future.set_result(RequestCompletion("report-follow-up", "report-follow-up", "unavailable",
            reason="The original writer ended without an acknowledged result", notification_state="unavailable"))
    else:
        future.set_result(RequestCompletion("report-follow-up", "report-follow-up", "completed",
            TurnOutcome(request_id="report-follow-up", status="success"), "committed",
            notification_state="failed" if completion_kind == "notification_failed" else "delivered"))
    await rm.wait_for_idle()
    final = (await db.get_research_job(row["id"]))["delivery_receipt"]
    if completion_kind == "unavailable":
        assert final["stage"] == "unknown" and "processed_at" not in final
        assert final["error"] == "The original writer ended without an acknowledged result"
        assert (await db.get_research_job(row["id"]))["status"] == "completed"
    else:
        assert final["stage"] == "processed" and final["processed_at"]
    assert await db.acknowledge_research_delivery(row["id"], "report-follow-up", receipt["version"]) is None
    acknowledged = await db.acknowledge_research_delivery(row["id"], "report-follow-up", final["version"])
    assert acknowledged["browser_confirmed_version"] == final["version"]
    if completion_kind != "unavailable":
        assert (await db.advance_research_delivery(row["id"], "report-follow-up", "processing"))["stage"] == "processed"
    await rm.shutdown()


@pytest.mark.asyncio
async def test_research_jobs_crud_and_sweep(mgr, db):
    session = await _session(mgr, db)  # research_jobs.session_id FKs to sessions
    await db.create_research_job("j1", session.id, "what?", "2026-06-13T00:00:00Z")
    row = await db.get_research_job("j1")
    assert row["status"] == "running" and row["injection_status"] == "pending"
    await db.update_research_job("j1", phase="verify", cost=0.1)
    assert (await db.get_research_job("j1"))["phase"] == "verify"
    # boot sweep flips running → interrupted
    n = await db.mark_in_flight_research_jobs_interrupted("2026-06-13T01:00:00Z")
    assert n == 1
    assert (await db.get_research_job("j1"))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_start_completes_and_injects_report(rm, mgr, db, monkeypatch):
    injected: list[tuple[str, str]] = []

    async def fake_start_message(session_id, prompt, *, source, delivery_id, work_id, knowledge_context,
                                 question_session_id=None, question_id=None):
        assert source == "research"
        assert delivery_id.startswith("research:")
        assert knowledge_context.trusted_request == "what is X?"
        assert work_id is None
        assert (question_session_id, question_id) == (None, None)
        injected.append((session_id, prompt))
        return accepted_receipt()

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    async def fake_run_research(question, *, on_progress=None, **kw):
        if on_progress:
            await on_progress(ResearchProgress(phase="search", detail="…"))
            await on_progress(ResearchProgress(phase="done", detail="done"))
        return _fake_report(question)

    monkeypatch.setattr(rm_mod, "run_research", fake_run_research)

    session = await _session(mgr, db)
    row = await rm.start(session.id, "what is X?")
    rid = row["id"]
    # let the background task finish
    await asyncio.wait_for(rm._tasks[rid], timeout=5)

    final = await db.get_research_job(rid)
    assert final["status"] == "completed" and final["phase"] == "done"
    assert final["injection_status"] == "delivered"
    assert final["cost"] == pytest.approx(0.05)
    assert injected and injected[0][0] == session.id
    assert injected[0][1].startswith(f"[deep-research:{rid}]")
    assert "Final report" in injected[0][1]


@pytest.mark.asyncio
async def test_start_rejects_backend_without_web(rm, mgr, db, monkeypatch):
    session = await _session(mgr, db)

    class _NoWeb:
        class profile:
            web = None
            credential_style = "env_secret"

    monkeypatch.setattr(rm_mod, "get_harness", lambda _b: _NoWeb())
    with pytest.raises(rm_mod.ResearchError) as ei:
        await rm.start(session.id, "q")
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_marks_cancelled(rm, mgr, db, monkeypatch):
    started = asyncio.Event()

    async def fake_run_research(question, *, on_progress=None, **kw):
        started.set()
        await asyncio.sleep(30)  # block until cancelled
        return _fake_report()

    monkeypatch.setattr(rm_mod, "run_research", fake_run_research)
    monkeypatch.setattr(mgr, "start_message", lambda *a, **k: asyncio.sleep(0))

    session = await _session(mgr, db)
    row = await rm.start(session.id, "q")
    rid = row["id"]
    await asyncio.wait_for(started.wait(), timeout=5)
    # cancel() records the state transition BEFORE interrupting, so the
    # returned row is already cancelled (no stale "running") — Vera review.
    cancelled = await rm.cancel(rid)
    assert cancelled["status"] == "cancelled"
    assert (await db.get_research_job(rid))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_recover_interrupted(rm, mgr, db):
    session = await _session(mgr, db)
    await db.create_research_job("old", session.id, "q", "2026-06-13T00:00:00Z")
    n = await rm.recover_interrupted()
    assert n == 1
    assert (await db.get_research_job("old"))["status"] == "interrupted"


@pytest.mark.parametrize("retired,original,valid,expected", [
    (True, "pending", True, "failed"),
    (False, "pending", True, "unknown"),
    (True, "unknown", True, "unknown"),
    (True, "pending", False, "unknown"),
])
async def test_restart_reconciles_saved_report_without_replaying_input(
    rm, mgr, db, tmp_path, monkeypatch, retired, original, valid, expected,
):
    from server.knowledge import capture, derive, encode

    session = await _session(mgr, db)
    context = encode(derive(capture(session, None, prompt="q"), "q")) if valid else None
    report = tmp_path / "retained-report.md"
    report.write_text("# Already completed")
    await db.create_research_job("crashed-report", session.id, "q", "now", context)
    await db.confirm_research_outcome("crashed-report", "completed", completed_at="then", report_path=str(report))
    await db.update_research_job("crashed-report", injection_status=original)

    async def no_new_work(*args, **kwargs):
        pytest.fail("Restart recovery must only observe the original input")

    monkeypatch.setattr(mgr, "start_message", no_new_work)
    monkeypatch.setattr(mgr, "admit_return", no_new_work)
    monkeypatch.setattr(mgr, "retain_result_notice", no_new_work)
    monkeypatch.setattr(mgr, "_session_owner", no_new_work)
    for _ in range(2):
        assert await rm.recover_interrupted(previous_writers_retired=retired) == 0
        saved = await db.get_research_job("crashed-report")
        assert saved["status"] == "completed" and saved["injection_status"] == expected
        assert saved["delivery_receipt"] is None
        assert Path(saved["report_path"]).read_text() == "# Already completed"
        assert await db.get_source_submission("research", "research:crashed-report", session_id=session.id) is None
    assert "not replayed" in saved["error"]


@pytest.mark.parametrize("state,stage", [("pending", "failed"), ("running", "unknown"), ("success", "processed"), ("failed", "failed")])
@pytest.mark.parametrize("linked", [False, True])
async def test_restart_links_original_persisted_input_and_its_real_completion(
    rm, mgr, db, tmp_path, monkeypatch, state, stage, linked,
):
    session = await _session(mgr, db)
    report = tmp_path / "saved.md"
    report.write_text("# Original report")
    await db.create_research_job("saved", session.id, "q", "now")
    await db.confirm_research_outcome("saved", "completed", completed_at="then", report_path=str(report))
    await db.admit_client_submission(session.id, "research:research:saved", "digest", "original-input",
        payload={"source": "research", "delivery_id": "research:saved", "content": "Original report", "attachment_ids": []},
        receipt={"accepted": True, "disposition": "accepted"})
    if state == "running":
        await db.claim_client_submission(session.id, "original-input", "original-input", "old-service")
    elif state in {"success", "failed"}:
        await db.append_message(session.id, 0, "system", "result", {"request_id": "original-input", "status": state},
                                terminal_request_id="original-input")
        await db.complete_client_submission(session.id, "original-input", owner="original-input", state="done")
    if linked:
        await db.advance_research_delivery("saved", "original-input", "queued")
        await db.update_research_job("saved", injection_status="pending")  # Prior split-write window.
    await db.recover_client_submissions()
    async def no_dispatch(*args, **kwargs):
        pytest.fail("Observing a committed input cannot submit or wake any session")
    monkeypatch.setattr(mgr, "admit_return", no_dispatch)
    monkeypatch.setattr(mgr, "start_message", no_dispatch)
    monkeypatch.setattr(mgr, "_session_owner", no_dispatch)
    for _ in range(2):
        await rm.recover_interrupted(previous_writers_retired=True)
        row = await db.get_research_job("saved")
        assert row["status"] == "completed" and row["injection_status"] == "delivered"
        assert row["delivery_receipt"]["request_id"] == "original-input"
        assert row["delivery_receipt"]["stage"] == stage
        assert Path(row["report_path"]).read_text() == "# Original report"
    async with db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT COUNT(*) FROM client_submissions") == (1,)


async def test_restart_repairs_injection_even_when_confirmed_receipt_is_already_terminal(rm, mgr, db):
    session = await _session(mgr, db)
    await db.create_research_job("done", session.id, "q", "now")
    await db.confirm_research_outcome("done", "completed", completed_at="then", report_path="saved.md")
    receipt = await db.advance_research_delivery("done", "original-input", "processed")
    confirmed = await db.acknowledge_research_delivery("done", "original-input", receipt["version"])
    await db.update_research_job("done", injection_status="pending")
    for _ in range(2):
        await rm.recover_interrupted(previous_writers_retired=True)
        row = await db.get_research_job("done")
        assert row["injection_status"] == "delivered" and row["delivery_receipt"] == confirmed


async def test_failed_source_read_cannot_be_used_as_proof_of_no_admission(rm, mgr, db, monkeypatch):
    from server.knowledge import capture, derive, encode
    session = await _session(mgr, db)
    await db.create_research_job("unreadable", session.id, "q", "now", encode(derive(capture(session, None, prompt="q"), "q")))
    await db.confirm_research_outcome("unreadable", "completed", completed_at="then", report_path="saved.md")
    async def unavailable(*args, **kwargs):
        raise TimeoutError("source unavailable")
    monkeypatch.setattr(db, "get_source_submission", unavailable)
    await rm.recover_interrupted(previous_writers_retired=True)
    row = await db.get_research_job("unreadable")
    assert row["injection_status"] == "unknown" and "source unavailable" in row["error"]


@pytest.mark.asyncio
async def test_shutdown_closes_research_admission_before_awaiting(rm, mgr, db):
    session = await _session(mgr, db)

    rm.begin_shutdown()

    with pytest.raises(rm_mod.ResearchError, match="shutting down") as error:
        await rm.start(session.id, "new research")
    assert error.value.status_code == 503
    await rm.shutdown()


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_queued_research_keeps_admitted_choices_and_rechecks_authority(
    rm, mgr, db, monkeypatch, override, revoke,
):
    from server.agent_manager import AgentManager
    from server.crypto import encrypt
    from server.users import UserManager

    session = await _session(mgr, db)
    key = await UserManager(db).data_key_for(session.user_id)
    for identifier in ("original", "replacement"):
        await db.save_credential(identifier, session.backend, identifier, "api_key",
                                 encrypt(identifier, key), "now", user_id=session.user_id)
    agents = AgentManager(db)
    await agents.update_agent(session.agent_id, model="agent-original", credential_id="original")
    if override:
        await mgr.update_session_configuration(session.id, {"model": "session-original"}, session.user_id)
    launched = []

    async def pipeline(question, *, credential, model, authorize_spawn, **kwargs):
        assert not db.configuration_lock.locked()
        await authorize_spawn()
        launched.append((question, model, credential.secret))
        return _fake_report(question)

    async def accept(*args, **kwargs):
        return accepted_receipt()

    monkeypatch.setattr(rm_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", accept)
    rm._job_sem = asyncio.Semaphore(0)
    first = await rm.start(session.id, "old job")
    await agents.update_agent(session.agent_id, model="agent-replacement", credential_id="replacement")
    if override:
        await mgr.update_session_configuration(session.id, {"model": "session-replacement"}, session.user_id)
    if revoke:
        await mgr.delete_credential_binding("original", session.user_id)
    second = await rm.start(session.id, "new job")
    rm._job_sem.release()
    await asyncio.wait_for(rm.wait_for_idle(), 3)

    expected_new = ("new job", "session-replacement" if override else "agent-replacement", "replacement")
    if revoke:
        assert launched == [expected_new]
        failed = await db.get_research_job(first["id"])
        assert failed["status"] == "failed" and "credential" in failed["error"].lower()
    else:
        assert launched == [
            ("old job", "session-original" if override else "agent-original", "original"), expected_new,
        ]
        assert (await db.get_research_job(first["id"]))["status"] == "completed"
    assert (await db.get_research_job(second["id"]))["status"] == "completed"


async def test_research_waiting_for_configuration_cannot_admit_after_shutdown(rm, mgr, db):
    session = await _session(mgr, db)
    async with db.configuration_lock:
        admission = asyncio.create_task(rm.start(session.id, "waiting configuration"))
        await asyncio.sleep(0)
        rm.begin_shutdown()
    with pytest.raises(rm_mod.ResearchError, match="shutting down"):
        await admission
    await rm.shutdown()
    assert not rm.drain_blockers()
    async with db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT id FROM research_jobs") is None


def _blocking_research_cli(monkeypatch, tmp_path: Path, *, stage: str) -> Path:
    """Install a synthetic provider at the actual subprocess boundary."""
    pid_file = tmp_path / "research-cli.pid"
    block = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    original = get_harness("claude-code").profile
    profile = replace(
        original,
        binary=sys.executable,
        build_oneshot_argv=lambda ctx: (
            [sys.executable, "-c", block, str(pid_file)]
            if stage == "reason"
            else [sys.executable, "-c", 'print(\'["one angle"]\')'],
            {"cwd": ctx.working_dir},
        ),
        parse_oneshot_stdout=lambda output: output,
        build_turn_argv=lambda ctx: (
            [sys.executable, "-c", block, str(pid_file)],
            {"cwd": ctx.working_dir},
        ),
    )
    monkeypatch.setitem(_REGISTRY, "claude-code", Harness(profile))
    return pid_file


def _completed_research_cli(monkeypatch) -> None:
    original = get_harness("claude-code").profile
    profile = replace(
        original, binary=sys.executable,
        build_oneshot_argv=lambda ctx: (
            [sys.executable, "-c", 'print(\'["one angle"]\')'], {"cwd": ctx.working_dir},
        ),
        parse_oneshot_stdout=lambda output: output,
        build_turn_argv=lambda ctx: (
            [sys.executable, "-c", 'print(\'{"type":"result","subtype":"success","result":"[]"}\')'],
            {"cwd": ctx.working_dir},
        ),
    )
    monkeypatch.setitem(_REGISTRY, "claude-code", Harness(profile))


async def _cli_pid(pid_file: Path) -> int:
    async with asyncio.timeout(5):
        while not pid_file.exists():
            await asyncio.sleep(0.01)
    return int(pid_file.read_text())


@pytest.mark.parametrize("stage", ["reason", "web"])
async def test_cancelled_http_waiter_still_retires_original_research_cli(
    rm, mgr, db, monkeypatch, tmp_path, stage, short_process_shutdown,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from server.deps import get_session_manager
    from server.routers import research
    from server.users import UserManager

    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage=stage)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "HTTP cancellation owns the original leaf")
    pid = await _cli_pid(pid_file)
    pipeline = rm._tasks[job["id"]]
    app = FastAPI()
    app.include_router(research.router)
    app.dependency_overrides[get_session_manager] = lambda: mgr
    monkeypatch.setattr(research, "research_manager", rm)
    token = await UserManager(db).issue_token(session.user_id)
    entered, release = asyncio.Event(), asyncio.Event()
    execute = db.conn._execute

    async def after_sql(function, *args, **kwargs):
        result = await execute(function, *args, **kwargs)
        if args and isinstance(args[0], str) and args[0].startswith("UPDATE research_jobs SET status=") and not entered.is_set():
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(db.conn, "_execute", after_sql)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as api:
        url = f"/api/sessions/{session.id}/research/{job['id']}"
        waiter = asyncio.create_task(api.post(f"{url}/cancel"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            waiter.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            repeated = await api.post(f"{url}/cancel")
            assert repeated.status_code == 200 and repeated.json()["status"] == "cancelled"
            await asyncio.wait_for(rm.wait_for_idle(), 5)
            assert pipeline.done() and not rm.drain_blockers()
            assert (await api.get(url)).json()["status"] == "cancelled"
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert int(pid_file.read_text()) == pid, "Cancellation must not start a replacement leaf"
        finally:
            release.set()
            await asyncio.gather(waiter, return_exceptions=True)
            await rm.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reason", "web"])
async def test_research_shutdown_interrupts_job_after_cli_retires(
    rm, mgr, db, monkeypatch, tmp_path, stage, short_process_shutdown
):
    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage=stage)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "research before shutdown")
    pid = await _cli_pid(pid_file)

    try:
        rm.begin_shutdown()
        await rm.shutdown()

        final = await db.get_research_job(job["id"])
        assert final["status"] == "interrupted"
        assert final["injection_status"] == "pending"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        for task in tuple(rm._tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(rm._tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_research_shutdown_owns_job_while_start_notification_is_pending(
    rm, mgr, db, monkeypatch, tmp_path
):
    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage="reason")
    session = await _session(mgr, db)
    notifying = asyncio.Event()
    release = asyncio.Event()
    jobs = []

    async def slow_started(event):
        if event["type"] == "research_started":
            jobs.append(event["research_id"])
            notifying.set()
            await release.wait()

    mgr.on_broadcast("slow-research-start", slow_started)
    admission = asyncio.create_task(rm.start(session.id, "pending notification"))
    try:
        await asyncio.wait_for(notifying.wait(), timeout=5)
        rm.begin_shutdown()
        await rm.shutdown()

        assert (await db.get_research_job(jobs[0]))["status"] == "interrupted"
        with pytest.raises(rm_mod.ResearchError, match="shutting down"):
            await admission
        assert not pid_file.exists()
    finally:
        release.set()
        admission.cancel()
        await asyncio.gather(admission, return_exceptions=True)
        for task in tuple(rm._tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(rm._tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_research_shutdown_reports_selected_result_recording_failure(
    rm, mgr, db, monkeypatch
):
    _completed_research_cli(monkeypatch)
    await db.conn.executescript("""
        CREATE TRIGGER reject_completed_report BEFORE UPDATE OF status ON research_jobs
        WHEN NEW.status = 'completed'
        BEGIN SELECT RAISE(ABORT, 'selected report write rejected'); END;
    """)
    selected = asyncio.Event()

    async def done_progress(event):
        if event["type"] == "research_progress" and event["phase"] == "done":
            selected.set()

    mgr.on_broadcast("selected-research-result", done_progress)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "complete research")
    await asyncio.wait_for(selected.wait(), timeout=5)

    with pytest.raises(Exception, match="selected report write rejected"):
        await rm.shutdown()
    final = await db.get_research_job(job["id"])
    assert final["status"] == "running"
    assert final["injection_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_shutdown", [False, True])
async def test_research_shutdown_reports_web_cli_retirement_failure(
    rm, mgr, db, monkeypatch, tmp_path, cancel_before_shutdown, short_process_shutdown
):
    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage="web")
    session = await _session(mgr, db)
    job = await rm.start(session.id, "web research")
    pid = await _cli_pid(pid_file)
    killpg = os.killpg

    def failing_signal(group, sig):
        if group == pid:
            raise OSError("web CLI retirement unconfirmed")
        return killpg(group, sig)

    monkeypatch.setattr(os, "killpg", failing_signal)
    try:
        if cancel_before_shutdown:
            owner = rm._tasks[job["id"]]
            await rm.cancel(job["id"])
            await asyncio.gather(owner, return_exceptions=True)
        with pytest.raises(Exception) as error:
            await rm.shutdown()
        assert "web CLI retirement unconfirmed" in "".join(traceback.format_exception(error.value))
        assert (await db.get_research_job(job["id"]))["status"] == (
            "cancelled" if cancel_before_shutdown else "running"
        )
    finally:
        monkeypatch.setattr(os, "killpg", killpg)
        try:
            killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        async with asyncio.timeout(5):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_research_shutdown_preserves_selected_completion_with_closed_delivery(
    rm, mgr, db, monkeypatch
):
    _completed_research_cli(monkeypatch)
    selected = asyncio.Event()

    async def done_progress(event):
        if event["type"] == "research_progress" and event["phase"] == "done":
            selected.set()

    mgr.on_broadcast("selected-completion", done_progress)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "selected report")
    await asyncio.wait_for(selected.wait(), timeout=5)
    mgr.close_admission()

    await rm.shutdown()

    final = await db.get_research_job(job["id"])
    assert final["status"] == "completed"
    assert final["injection_status"] == "failed"
    assert final["error"] == "delivery suppressed: session admission is closed"
    assert Path(final["report_path"]).read_text().strip() == '["one angle"]'


@pytest.mark.asyncio
async def test_research_shutdown_preserves_user_cancellation(
    rm, mgr, db, monkeypatch, tmp_path
):
    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage="reason")
    session = await _session(mgr, db)
    job = await rm.start(session.id, "cancelled by user")
    pid = await _cli_pid(pid_file)
    await rm.cancel(job["id"])

    await rm.shutdown()

    final = await db.get_research_job(job["id"])
    assert final["status"] == "cancelled"
    assert final["error"] == "cancelled by user"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_research_shutdown_timeout_keeps_pending_cleanup_for_later_join(
    rm, mgr, db, monkeypatch, tmp_path
):
    pid_file = _blocking_research_cli(monkeypatch, tmp_path, stage="reason")
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def slow_interruption(event):
        if event["type"] == "research_failed" and event["status"] == "interrupted":
            cleaning.set()
            await release.wait()

    mgr.on_broadcast("slow-interruption", slow_interruption)
    session = await _session(mgr, db)
    job = await rm.start(session.id, "slow cleanup")
    await _cli_pid(pid_file)
    retry = None
    try:
        with pytest.raises(TimeoutError, match=job["id"]):
            await rm.shutdown(timeout=0.01)
        assert cleaning.is_set()
        retry = asyncio.create_task(rm.shutdown())
        await asyncio.sleep(0)
        assert not retry.done()

        release.set()
        await retry
        assert (await db.get_research_job(job["id"]))["status"] == "interrupted"
    finally:
        release.set()
        if retry is not None:
            await retry
        else:
            await rm.shutdown()


@pytest.mark.asyncio
async def test_research_shutdown_joins_every_parallel_web_cli_retirement(
    rm, mgr, db, monkeypatch, tmp_path, short_process_shutdown
):
    original = get_harness("claude-code").profile
    script = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); "
        "sys.stdin.read() if sys.argv[1] == 'fast' else time.sleep(60)"
    )

    def web_argv(context):
        angle = "slow" if "ANGLE: slow\n" in context.prompt else "fast"
        return (
            [sys.executable, "-c", script, angle, str(tmp_path / f"{angle}.pid")],
            {"cwd": context.working_dir},
        )

    profile = replace(
        original, binary=sys.executable,
        build_oneshot_argv=lambda context: (
            [sys.executable, "-c", 'print(\'["fast", "slow"]\')'],
            {"cwd": context.working_dir},
        ),
        parse_oneshot_stdout=lambda output: output,
        build_turn_argv=web_argv,
    )
    monkeypatch.setitem(_REGISTRY, "claude-code", Harness(profile))
    session = await _session(mgr, db)
    job = await rm.start(session.id, "parallel research")
    pids = [await _cli_pid(tmp_path / f"{angle}.pid") for angle in ("fast", "slow")]
    try:
        await rm.shutdown()

        assert (await db.get_research_job(job["id"]))["status"] == "interrupted"
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        async with asyncio.timeout(5):
            for pid in pids:
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)


class TestResearchLivesUnderItsOwner:
    """A job's scratch cwd and report are the user's data (multi-tenancy.md §6).

    They used to go to a hardcoded `~/.octopus/research` — which pooled every
    account's research in one directory no confinement check covers, and which
    an ordinary test run wrote into on whatever box it ran on.
    """

    def test_each_account_researches_in_its_own_directory(self, tmp_path, monkeypatch):
        from server.config import settings
        from server.research.manager import _research_dir

        monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
        one, two = _research_dir("u1"), _research_dir("u2")
        assert one != two
        assert not one.startswith(two) and not two.startswith(one)
        assert os.path.isdir(one) and os.path.isdir(two)

    def test_before_accounts_it_is_where_it_has_always_been(self, tmp_path, monkeypatch):
        from server.config import settings
        from server.research.manager import _research_dir

        monkeypatch.setattr(settings, "research_dir", str(tmp_path / "legacy"))
        assert _research_dir(None) == str(tmp_path / "legacy")

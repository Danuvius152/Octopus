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
from server.harness.registry import _REGISTRY
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchProgress, ResearchReport
from server.session_manager import SessionManager
from tests.submission import accepted_receipt


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
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
    return await mgr.create_session(agent["id"], "S", "/tmp", backend=backend)


def _fake_report(question="q"):
    return ResearchReport(
        question=question, report="# Final report\nThe answer [http://a].",
        findings=[], sources=["http://a"], angles=["x"], claims_examined=3, cost=0.05,
    )


async def test_delivery_receipts_distinguish_queue_processing_and_current_browser_confirmation(rm, mgr, db, monkeypatch):
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
    future.set_result(RequestCompletion("report-follow-up", "report-follow-up", "completed",
        TurnOutcome(request_id="report-follow-up", status="success"), "committed"))
    await rm._delivery_tasks[row["id"]]
    final = (await db.get_research_job(row["id"]))["delivery_receipt"]
    assert final["stage"] == "processed" and final["processed_at"]
    assert await db.acknowledge_research_delivery(row["id"], "report-follow-up", receipt["version"]) is None
    acknowledged = await db.acknowledge_research_delivery(row["id"], "report-follow-up", final["version"])
    assert acknowledged["browser_confirmed_version"] == final["version"]
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

    async def fake_start_message(session_id, prompt, *, source, delivery_id, work_id, knowledge_context):
        assert source == "research"
        assert delivery_id.startswith("research:")
        assert knowledge_context.trusted_request == "what is X?"
        assert work_id is None
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


@pytest.mark.asyncio
async def test_shutdown_closes_research_admission_before_awaiting(rm, mgr, db):
    session = await _session(mgr, db)

    rm.begin_shutdown()

    with pytest.raises(rm_mod.ResearchError, match="shutting down") as error:
        await rm.start(session.id, "new research")
    assert error.value.status_code == 503
    await rm.shutdown()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reason", "web"])
async def test_research_shutdown_interrupts_job_after_cli_retires(
    rm, mgr, db, monkeypatch, tmp_path, stage
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
    rm, mgr, db, monkeypatch, tmp_path, cancel_before_shutdown
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
    rm, mgr, db, monkeypatch, tmp_path
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

"""Independent research keeps its caller across session archive boundaries."""

from __future__ import annotations

import asyncio

import pytest

from server.database import Database
from server.harness import HarnessEvent
from server.research import manager as research_module
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchReport
from server.session_manager import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase


@pytest.fixture
async def returning_research(monkeypatch, tmp_path):
    database = Database(":memory:")
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    research = ResearchManager()
    research.bind(manager, database)
    release, started = asyncio.Event(), asyncio.Event()
    processed: asyncio.Queue[str] = asyncio.Queue()

    async def pipeline(question, **kwargs):
        started.set()
        await release.wait()
        return ResearchReport(question, "# Saved report", [], [], [], 0, 0)

    class Run(FakeRunBase):
        def __init__(self):
            self.ready = asyncio.Event()

        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt
            self.ready.set()

        async def stream(self):
            await self.ready.wait()
            processed.put_nowait(self.prompt)
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(research_module, "run_research", pipeline)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    try:
        yield database, manager, research, started, release, processed
    finally:
        release.set()
        await research.shutdown()
        await database.close()


@pytest.mark.parametrize("paused", [False, True])
async def test_new_research_return_restores_original_session_and_stop(returning_research, paused):
    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Original")
    job = await research.start(session.id, "Keep the original caller")
    task = research._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    for _ in range(4):
        await manager.interrupt(session.id)
        await manager.resume_dispatch(session.id)
    if paused:
        await manager.interrupt(session.id)
    previous_revision = manager.dispatch_snapshot(session.id)["revision"]
    await manager.archive_session(session.id, replace=False)
    assert manager.get_session(session.id) is None
    release.set()
    await asyncio.wait_for(task, 3)
    result = await db.get_research_job(job["id"])
    assert result["status"] == "completed"
    assert result["injection_status"] == "delivered"
    restored = manager.get_session(session.id)
    assert restored is not None and restored.user_id == session.user_id
    assert manager.dispatch_snapshot(session.id)["paused"] is paused
    assert manager.dispatch_snapshot(session.id)["revision"] > previous_revision
    if paused:
        assert processed.empty()
        await manager.resume_dispatch(session.id)
    assert f"[deep-research:{job['id']}]" in await asyncio.wait_for(processed.get(), 2)
    await research.wait_for_idle()
    assert (await db.get_research_job(job["id"]))["delivery_receipt"]["stage"] == "processed"


async def test_research_waiting_for_slot_keeps_configuration_after_archive(returning_research):
    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Waiting")
    research._job_sem = asyncio.Semaphore(0)
    job = await research.start(session.id, "Waiting for a slot")
    task = research._tasks[job["id"]]
    await manager.archive_session(session.id, replace=False)
    assert not started.is_set()
    release.set()
    research._job_sem.release()
    await asyncio.wait_for(task, 3)
    assert (await db.get_research_job(job["id"]))["status"] == "completed"
    assert "Waiting for a slot" in await asyncio.wait_for(processed.get(), 2)


async def test_scheduled_session_archives_then_processes_report_and_archives_again(returning_research):
    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Scheduled", origin="schedule")
    archives: asyncio.Queue[str] = asyncio.Queue()

    async def archived(event):
        if event["type"] == "session_archived":
            archives.put_nowait(event["old_session_id"])

    manager.on_broadcast("archive-evidence", archived)
    job = await research.start(session.id, "Scheduled research")
    task = research._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    primary = await manager.start_message(session.id, "Primary turn")
    await primary.wait()
    assert "Primary turn" in await processed.get()
    assert await asyncio.wait_for(archives.get(), 2) == session.id
    release.set()
    await asyncio.wait_for(task, 3)
    assert f"[deep-research:{job['id']}]" in await asyncio.wait_for(processed.get(), 2)
    assert await asyncio.wait_for(archives.get(), 2) == session.id
    assert manager.get_session(session.id) is None
    assert (await db.get_research_job(job["id"]))["delivery_receipt"]["stage"] == "processed"


async def test_archive_revokes_queued_report_and_only_new_report_wakes_session(returning_research, monkeypatch):
    db, manager, research, _, _, processed = returning_research
    gates = {"first": asyncio.Event(), "second": asyncio.Event()}

    async def pipeline(question, **kwargs):
        await gates[question].wait()
        return ResearchReport(question, f"# {question}", [], [], [], 0, 0)

    monkeypatch.setattr(research_module, "run_research", pipeline)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Two reports")
    await manager.interrupt(session.id)
    first = await research.start(session.id, "first")
    first_task = research._tasks[first["id"]]
    second = await research.start(session.id, "second")
    second_task = research._tasks[second["id"]]
    gates["first"].set()
    await first_task
    old_delivery = (await db.get_research_job(first["id"]))["delivery_receipt"]
    await manager.archive_session(session.id, replace=False)
    gates["second"].set()
    await second_task
    pending = manager.dispatch_snapshot(session.id)
    assert pending["paused"] is True
    assert len(pending["items"]) == 1
    assert second["id"] in pending["items"][0]["summary"]
    duplicate = await manager.source_receipt("research", f"research:{first['id']}", session_id=session.id)
    assert duplicate[1].request_id == old_delivery["request_id"]
    assert (await duplicate[1].wait()).reason == "archived"
    await manager.resume_dispatch(session.id)
    assert second["id"] in await asyncio.wait_for(processed.get(), 2)
    assert processed.empty()


async def test_simultaneous_reports_share_one_session_and_duplicate_keeps_receipt(returning_research):
    from server.knowledge import decode_owned

    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Concurrent reports")
    jobs = [await research.start(session.id, question) for question in ("one", "two")]
    tasks = [research._tasks[job["id"]] for job in jobs]
    await asyncio.wait_for(started.wait(), 2)
    await manager.archive_session(session.id, replace=False)
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 3)
    prompts = [await asyncio.wait_for(processed.get(), 2) for _ in jobs]
    assert all(sum(job["id"] in prompt for prompt in prompts) == 1 for job in jobs)
    restored = manager.get_session(session.id)
    assert restored is not None and restored is not session
    row = await db.get_research_job(jobs[0]["id"])
    resolved = await manager.resolve_task_return(decode_owned(row["knowledge_context"]), session_id=session.id, work_id=None)
    assert resolved[0] is restored
    repeated = await manager.start_message(
        session.id, f"[deep-research:{row['id']}] Research complete for: {row['question']}\n\n# Saved report",
        source="research", delivery_id=f"research:{row['id']}",
        knowledge_context=resolved[1],
    )
    assert repeated.disposition == "duplicate"
    assert repeated.request_id == row["delivery_receipt"]["request_id"]
    assert (await repeated.wait()).outcome.status == "success"
    assert processed.empty()


async def test_revoked_account_retains_report_without_waking_session(returning_research):
    from pathlib import Path

    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Revoked")
    job = await research.start(session.id, "Retain evidence")
    task = research._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    await manager.archive_session(session.id, replace=False)
    await db.update_user_field(session.user_id, disabled_at="2026-10-27T00:00:00+00:00")
    release.set()
    await asyncio.wait_for(task, 3)
    row = await db.get_research_job(job["id"])
    assert row["status"] == "completed" and row["injection_status"] == "failed"
    assert Path(row["report_path"]).read_text() == "# Saved report"
    assert manager.get_session(session.id) is None
    assert processed.empty()


async def test_new_return_can_finish_while_old_archive_notice_is_delayed(returning_research):
    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Archive race")
    delayed, publish = asyncio.Event(), asyncio.Event()
    notices = []

    async def slow_archive(event):
        if event["type"] == "session_archived":
            delayed.set()
            await publish.wait()

    async def browser(event):
        if event["type"] in {"session_archived", "session_unarchived"}:
            notices.append(event)

    manager.on_broadcast("delayed-archive-observer", slow_archive)
    manager.on_broadcast("browser-projection", browser, session.user_id)
    job = await research.start(session.id, "Return across publication")
    task = research._tasks[job["id"]]
    await asyncio.wait_for(started.wait(), 2)
    archiving = asyncio.create_task(manager.archive_session(session.id, replace=False))
    try:
        await asyncio.wait_for(delayed.wait(), 2)
        release.set()
        await asyncio.wait_for(task, 3)
        assert job["id"] in await asyncio.wait_for(processed.get(), 2)
        publish.set()
        await archiving
        assert [event["type"] for event in notices] == ["session_unarchived", "session_archived"]
        assert notices[0]["session"]["id"] == session.id
        assert notices[0]["dispatch_state"]["revision"] > notices[1]["dispatch_state"]["revision"]
    finally:
        publish.set()
        await archiving


@pytest.mark.parametrize("unavailable", ["closed_work", "wrong_work", "wrong_session", "deleted", "agent_archived"])
async def test_unavailable_return_cannot_reopen_original_session(returning_research, unavailable):
    import uuid
    from dataclasses import replace

    from server.agent_manager import AgentManager
    from server.knowledge import capture, derive
    from server.mcp_identity import close_work

    db, manager, _, _, _, processed = returning_research
    owner = await db.get_system_agent()
    agent = await AgentManager(db).create_agent(name="Return owner", user_id=owner["user_id"])
    session = await manager.create_session(agent["id"], "Closed destination")
    work_id = uuid.uuid4().hex
    owned = derive(replace(capture(session, None, prompt="Original request"), work_id=work_id), "Independent task")
    await manager.archive_session(session.id, replace=False)
    if unavailable == "closed_work":
        close_work(work_id)
    elif unavailable == "deleted":
        await db.delete_session(session.id)
    elif unavailable == "agent_archived":
        await manager.archive_agent(agent["id"])
    result = await manager.resolve_task_return(
        owned, session_id="another-session" if unavailable == "wrong_session" else session.id,
        work_id="another-work" if unavailable == "wrong_work" else work_id,
    )
    assert result is None and manager.get_session(session.id) is None and processed.empty()


async def test_work_closed_while_return_waits_for_restore_keeps_session_archived(returning_research):
    import uuid
    from dataclasses import replace

    from server.knowledge import capture, derive
    from server.mcp_identity import close_work

    db, manager, _, _, _, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Closing work")
    work_id = uuid.uuid4().hex
    owned = derive(replace(capture(session, None, prompt="Original request"), work_id=work_id), "Independent task")
    await manager.archive_session(session.id, replace=False)
    notices = []
    waiting = asyncio.Event()

    async def record(event):
        notices.append(event)

    manager.on_broadcast("restore-events", record, session.user_id)
    lock = manager._session_lifecycle_lock(session.id)
    async with lock:
        async def resolve():
            waiting.set()
            return await manager.resolve_task_return(owned, session_id=session.id, work_id=work_id)

        returning = asyncio.create_task(resolve())
        await waiting.wait()
        # The resolver must pass its initial checks and actually wait for the
        # held lifecycle lock before work is closed.
        async with asyncio.timeout(2):
            while not lock._waiters:
                await asyncio.sleep(.001)
        close_work(work_id)
    assert await asyncio.wait_for(returning, 2) is None
    assert manager.get_session(session.id) is None and processed.empty()
    archived = await manager.list_archived_sessions(user_id=session.user_id)
    assert session.id in {row["id"] for row in archived}
    assert not any(event["type"] == "session_unarchived" for event in notices)


async def test_permanent_transcript_rejection_retains_report_and_never_retries_after_recovery(returning_research, monkeypatch):
    from pathlib import Path

    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Damaged transcript")
    job = await research.start(session.id, "Keep the completed report")
    task = research._tasks[job["id"]]
    await started.wait()
    await manager.archive_session(session.id, replace=False)
    async with db.transaction(write=True) as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
        for _ in range(2):
            await tx.execute("INSERT INTO messages(session_id,seq,role,type,content,created_at) VALUES(?,0,'user','text','\"duplicate\"','now')", (session.id,))
    attempts = []
    original = manager.start_message

    async def observe(*args, **kwargs):
        attempts.append(kwargs.get("delivery_id"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", observe)
    release.set()
    await asyncio.wait_for(asyncio.shield(task), 2)
    saved = await db.get_research_job(job["id"])
    assert saved["status"] == "completed" and saved["injection_status"] == "failed"
    assert "duplicate sequences" in saved["error"]
    assert Path(saved["report_path"]).read_text() == "# Saved report"
    assert attempts == [f"research:{job['id']}"] and processed.empty()
    async with db.transaction(write=True) as tx:
        await tx.execute("DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages WHERE session_id=?)", (session.id,))
    await manager.archive_session(session.id, replace=False)
    await manager.unarchive_session(session.id)
    await research.recover_interrupted()
    await research._inject_report(job["id"], session.id, saved["question"], ResearchReport(saved["question"], "# Saved report", [], [], [], 0, 0))
    assert attempts == [f"research:{job['id']}"] and processed.empty()


@pytest.mark.parametrize("receipt_temporarily_unavailable", [False, True])
async def test_report_lost_ack_reconciles_original_input_without_repeating_research(
    returning_research, monkeypatch, receipt_temporarily_unavailable,
):
    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Lost report ACK")
    await manager.interrupt(session.id)
    submit, lookup = manager.start_message, manager.source_receipt
    attempts = []
    observation_failed = False

    async def lost_ack(*args, **kwargs):
        attempts.append(kwargs["delivery_id"])
        await submit(*args, **kwargs)
        raise TimeoutError("Original admission ACK was lost")

    async def original_receipt(*args, **kwargs):
        nonlocal observation_failed
        if receipt_temporarily_unavailable and attempts and not observation_failed:
            observation_failed = True
            raise TimeoutError("Original receipt temporarily unreadable")
        return await lookup(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lost_ack)
    monkeypatch.setattr(manager, "source_receipt", original_receipt)
    job = await research.start(session.id, "Reconcile the report")
    task = research._tasks[job["id"]]
    await started.wait()
    release.set()
    await asyncio.wait_for(asyncio.shield(task), 2)
    if receipt_temporarily_unavailable:
        assert (await db.get_research_job(job["id"]))["injection_status"] == "unknown"
        await research.recover_interrupted()
    saved = await db.get_research_job(job["id"])
    assert saved["status"] == "completed" and saved["injection_status"] == "delivered"
    original = await db.get_source_submission("research", f"research:{job['id']}", session_id=session.id)
    assert saved["delivery_receipt"]["request_id"] == original["request_id"]
    assert attempts == [f"research:{job['id']}"]
    assert len(manager.dispatch_snapshot(session.id)["items"]) == 1
    await manager.resume_dispatch(session.id)
    assert job["id"] in await asyncio.wait_for(processed.get(), 2)
    assert processed.empty()


async def test_unconfirmed_report_is_not_overwritten_by_later_refusal_or_restart(returning_research, monkeypatch):
    from pathlib import Path

    from server.sessions.runtime_types import AdmissionIntegrityError

    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Unconfirmed original")
    attempts = []

    async def unknown(*args, **kwargs):
        attempts.append(kwargs["delivery_id"])
        if len(attempts) > 1:
            raise AdmissionIntegrityError("Later permanent refusal cannot erase original uncertainty")
        raise TimeoutError("Original admission could not be confirmed")

    monkeypatch.setattr(manager, "start_message", unknown)
    job = await research.start(session.id, "Preserve uncertainty")
    task = research._tasks[job["id"]]
    await started.wait()
    release.set()
    await asyncio.wait_for(task, 2)
    await research.recover_interrupted()
    await research._inject_report(job["id"], session.id, job["question"], ResearchReport(job["question"], "# Saved report", [], [], [], 0, 0))
    saved = await db.get_research_job(job["id"])
    assert saved["status"] == "completed" and saved["injection_status"] == "unknown"
    assert "Original admission" in saved["error"] and "Later permanent" not in saved["error"]
    assert Path(saved["report_path"]).read_text() == "# Saved report"
    assert attempts == [f"research:{job['id']}"] and processed.empty()


async def test_real_fork_reservation_delays_report_using_its_original_delivery_identity(returning_research, monkeypatch):
    from server.sessions.owner_runtime import ReserveFork
    from server.sessions.runtime_types import SessionAdmissionBusy

    db, manager, research, started, release, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Fork reservation")
    job = await research.start(session.id, "Wait for the original lifecycle")
    task = research._tasks[job["id"]]
    await started.wait()
    owner = manager._session_owner(session)
    await owner.call_runtime(ReserveFork(True))
    refused = asyncio.Event()
    resolve = manager.resolve_task_return

    async def observe(*args, **kwargs):
        try:
            return await resolve(*args, **kwargs)
        except SessionAdmissionBusy:
            refused.set()
            raise

    monkeypatch.setattr(manager, "resolve_task_return", observe)
    release.set()
    await asyncio.wait_for(refused.wait(), 2)
    assert await db.get_source_submission("research", f"research:{job['id']}", session_id=session.id) is None
    await owner.call_runtime(ReserveFork(False))
    await asyncio.wait_for(task, 3)
    assert job["id"] in await asyncio.wait_for(processed.get(), 2)
    await research.wait_for_idle()
    saved = await db.get_research_job(job["id"])
    assert saved["status"] == "completed" and saved["delivery_receipt"]["stage"] == "processed"
    assert processed.empty()

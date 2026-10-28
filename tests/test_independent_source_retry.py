"""Original source Owners retry delivery, never the independent execution."""

import asyncio
import shlex
import sys

import pytest

from server.bg_tasks import BgTaskManager
from server.knowledge import AdmittedRequest, decode_context, derive
from server.research import manager as research_module
from server.research.manager import ResearchManager
from server.sessions.runtime_types import SessionMailboxFull, SessionOwnerUnavailable
from tests.test_research_manager import _fake_report
from tests.test_submission_recovery import runtime as runtime


@pytest.mark.parametrize("failure", ["refused", "lost_ack"])
async def test_background_owner_retries_the_original_result_after_caller_completion(runtime, monkeypatch, tmp_path, failure):
    manager, db, session, _, starts, _ = runtime
    caller = await manager.start_message(session.id, "Original caller ends first")
    original = await asyncio.wait_for(caller.wait(), 5)
    context = decode_context((await db.get_submission_for_request(session.id, caller.request_id))["payload"]["knowledge"])
    owned = derive(AdmittedRequest(context.origin, context), "Independent command")
    await manager.interrupt(session.id)
    start, attempts = manager.start_message, 0
    reached, acknowledged = asyncio.Event(), asyncio.Event()

    async def intercept(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "refused":
            reached.set()
            raise SessionMailboxFull("Temporary input capacity")
        receipt = await start(*args, **kwargs)
        if attempts == 1:
            reached.set()
            raise ConnectionError("Accepted return acknowledgment lost")
        return receipt

    async def deliver(record):
        accepted = await manager.deliver_bg_result(record)
        if accepted:
            acknowledged.set()
        return accepted

    monkeypatch.setattr(manager, "start_message", intercept)
    background = BgTaskManager()
    background.bind(db, deliver, manager._broadcast)
    await background.start()
    effect = tmp_path / "command-effects"
    command = shlex.join([sys.executable, "-c", f"from pathlib import Path; p=Path({str(effect)!r}); p.write_text(p.read_text()+'executed\\n' if p.exists() else 'executed\\n'); print('original result')"])
    try:
        task = await background.start_task(session_id=session.id, command=command, working_dir=session.working_dir,
                                          owned_context=owned, idle_timeout_seconds=0)
        await asyncio.wait_for(reached.wait(), 5)
        await asyncio.wait_for(acknowledged.wait(), 5)
        assert (await background.get_task(task.id)).status == "completed"
        assert effect.read_text().splitlines() == ["executed"]
        queued = manager.dispatch_snapshot(session.id)
        assert queued["paused"] and len(queued["items"]) == 1
        saved = await db.get_submission_for_request(session.id, queued["items"][0]["request_id"])
        assert saved["payload"]["delivery_id"] == f"bg:{task.id}"
        assert saved["receipt"]["accepted"]
        assert (await caller.wait()) is original and starts.qsize() == 1
    finally:
        await background.shutdown()


@pytest.mark.parametrize("failure", ["refused", "lost_ack"])
async def test_research_owner_retries_admission_without_rerunning_the_report_or_reporting_false_failure(runtime, monkeypatch, failure):
    manager, db, session, _, _, events = runtime
    await manager.interrupt(session.id)
    start, attempts, reports = manager.start_message, 0, []

    async def report(*args, **kwargs):
        reports.append("one report")
        return _fake_report()

    async def intercept(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "refused":
            raise SessionMailboxFull("Temporary input capacity")
        receipt = await start(*args, **kwargs)
        if attempts == 1:
            raise SessionOwnerUnavailable("Accepted return acknowledgment lost")
        return receipt

    monkeypatch.setattr(manager, "start_message", intercept)
    monkeypatch.setattr(research_module, "run_research", report)
    research = ResearchManager()
    research.bind(manager, db)
    try:
        job = await research.start(session.id, "Original research")
        await asyncio.wait_for(research._tasks[job["id"]], 5)
        row = await db.get_research_job(job["id"])
        assert row["status"] == "completed" and row["injection_status"] == "delivered"
        assert reports == ["one report"]
        queued = manager.dispatch_snapshot(session.id)
        assert queued["paused"] and len(queued["items"]) == 1
        assert row["delivery_receipt"]["request_id"] == queued["items"][0]["request_id"]
        assert not [event for event in events if event.get("type") == "research_delivery" and event.get("injection_status") == "failed"]
    finally:
        await research.shutdown()


async def test_research_shutdown_confirms_a_committed_report_after_its_acknowledgment_was_lost(runtime, monkeypatch):
    manager, db, session, _, _, events = runtime
    await manager.interrupt(session.id)
    start = manager.start_message
    accepted = asyncio.Event()
    research = ResearchManager()
    research.bind(manager, db)

    async def report(*args, **kwargs):
        return _fake_report()

    async def lose_ack(*args, **kwargs):
        await start(*args, **kwargs)
        manager.close_admission()
        research.begin_shutdown()
        accepted.set()
        raise SessionOwnerUnavailable("Accepted report acknowledgment lost during shutdown")

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(research_module, "run_research", report)
    try:
        job = await research.start(session.id, "Completed report at shutdown")
        await asyncio.wait_for(accepted.wait(), 5)
        await research.shutdown()
        row = await db.get_research_job(job["id"])
        assert row["status"] == "completed" and row["injection_status"] == "delivered"
        queued = manager.dispatch_snapshot(session.id)
        assert len(queued["items"]) == 1
        assert row["delivery_receipt"]["request_id"] == queued["items"][0]["request_id"]
        assert not [event for event in events if event.get("type") == "research_delivery" and event.get("injection_status") == "failed"]
    finally:
        await research.shutdown()

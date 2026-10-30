"""Permanent destination loss ends delivery without changing the source result."""

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from server.bg_tasks import BgTaskManager
from server.knowledge import AdmittedRequest, decode_context, derive
from server.research import manager as research_module
from server.research.manager import ResearchManager
from server.sessions.runtime_types import SessionOwnerUnavailable
from tests.test_delegation_work import wait_state
from tests.test_delegation_work import work_runtime as work_runtime
from tests.test_research_manager import _fake_report
from tests.test_submission_recovery import runtime as runtime


async def fail_destination_owner(manager, session, monkeypatch):
    read = manager.db.has_delivery_notice
    owner = manager._session_owners.get(id(session)) or manager._session_owner(session)

    async def fail_after_read(*args):
        await read(*args)
        raise RuntimeError("Destination owner failed after a confirmed read")

    with monkeypatch.context() as patch:
        patch.setattr(manager.db, "has_delivery_notice", fail_after_read)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "fail-original-owner", "Uncommitted notice")
    assert owner.failed and not manager._admission_closed
    return owner


async def test_research_retains_report_and_reports_failed_delivery_when_its_destination_owner_has_ended(runtime, monkeypatch):
    manager, db, session, _, _, events = runtime
    owner = await fail_destination_owner(manager, session, monkeypatch)
    reports = []

    async def report(*args, **kwargs):
        reports.append("one report")
        return _fake_report()

    monkeypatch.setattr(research_module, "run_research", report)
    research = ResearchManager()
    research.bind(manager, db)
    try:
        job = await research.start(session.id, "Retain this completed report")
        await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 2)
        row = await db.get_research_job(job["id"])
        assert row["status"] == "completed" and row["injection_status"] == "failed"
        assert "owner is unavailable" in row["error"]
        assert "Final report" in Path(row["report_path"]).read_text()
        assert reports == ["one report"]
        assert await db.get_source_submission("research", f"research:{job['id']}", session_id=session.id) is None
        assert any(event.get("type") == "research_delivery" and event.get("injection_status") == "failed" for event in events)
        assert manager._session_owners[id(session)] is owner and owner.failed
    finally:
        await asyncio.gather(research.shutdown(), return_exceptions=True)


@pytest.mark.parametrize("accepted_before_failure", [False, True])
async def test_background_result_is_retained_and_delivery_retires_when_destination_owner_failed(runtime, monkeypatch, caplog, accepted_before_failure):
    manager, db, session, _, _, _ = runtime
    caller = await manager.start_message(session.id, "Original caller")
    await asyncio.wait_for(caller.wait(), 5)
    context = decode_context((await db.get_submission_for_request(session.id, caller.request_id))["payload"]["knowledge"])
    owned = derive(AdmittedRequest(context.origin, context), "Independent command")
    await manager.interrupt(session.id)
    owner = (manager._session_owners[id(session)] if accepted_before_failure
             else await fail_destination_owner(manager, session, monkeypatch))
    acknowledged = asyncio.Event()
    results = []
    attempted = False

    async def deliver(record):
        nonlocal attempted
        if accepted_before_failure and not attempted:
            attempted = True
            assert await manager.deliver_bg_result(record)
            await fail_destination_owner(manager, session, monkeypatch)
            raise SessionOwnerUnavailable("Destination owner is unavailable after accepting the result")
        result = await manager.deliver_bg_result(record)
        results.append(result)
        acknowledged.set()
        return result

    background = BgTaskManager()
    background.bind(db, deliver, manager._broadcast)
    await background.start()
    try:
        task = await background.start_task(session_id=session.id, working_dir=session.working_dir,
            command=shlex.join([sys.executable, "-c", "print('Retained original output')"]), owned_context=owned)
        await asyncio.wait_for(acknowledged.wait(), 2)
        async with asyncio.timeout(2):
            while task.id in background._running:
                await asyncio.sleep(.01)
        row = await background.get_task(task.id)
        assert row.status == "completed" and row.stdout == "Retained original output\n" and results == [accepted_before_failure]
        source = await db.get_source_submission("bg", f"bg:{task.id}", session_id=session.id)
        assert (source is not None) is accepted_before_failure
        assert "owner is unavailable" in caplog.text
        assert manager._session_owners[id(session)] is owner and owner.failed
    finally:
        await background.shutdown()


async def test_completed_delegation_keeps_its_reply_and_finishes_delivery_after_parent_owner_failure(work_runtime, monkeypatch, caplog):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="controlled return")
    await asyncio.wait_for(starts.get(), 5)
    owner = await fail_destination_owner(manager, parent, monkeypatch)
    (Path(parent.working_dir) / "release").write_text("Return the original result")
    joined = False
    try:
        await wait_state(delegations, rec.delegation_id, "completed")
        await asyncio.wait_for(asyncio.shield(delegations.confirm_recording()), 2)
        joined = True
        public = rec.to_public_dict()
        assert rec.state == "completed" and public["terminal_disposition"] == "stopped"
        assert "owner is unavailable" in public["terminal_delivery_error"].casefold()
        assert await manager.db.get_source_submission("delegation", f"agent-terminal:{rec.work_id}", session_id=parent.id) is None
        messages = await manager.db.load_messages(rec.delegation_id)
        assert any(row["role"] == "assistant" and row["content"] == "reply" for row in messages)
        assert "owner is unavailable" in caplog.text
        assert manager._session_owners[id(parent)] is owner and owner.failed
    finally:
        if not joined:
            manager.close_admission()
            await asyncio.wait_for(delegations.confirm_recording(), 3)
        await manager.archive_session(parent.id, replace=False)


async def test_delegation_question_delivery_ends_when_parent_owner_has_permanently_failed(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held while asking")
    await asyncio.wait_for(starts.get(), 5)
    owner = await fail_destination_owner(manager, parent, monkeypatch)
    published = asyncio.Event()

    async def after_delegation(event):
        if event.get("type") == "question_request" and event.get("session_id") == rec.delegation_id:
            published.set()

    manager.on_broadcast("question-delivery-retired", after_delegation)
    question = await manager.create_pending_question(rec.delegation_id, [{"question": "Keep this original question"}])
    try:
        await asyncio.wait_for(published.wait(), 2)
        await delegations.confirm_recording()
        assert manager.has_pending_question(rec.delegation_id, question)
        assert await manager.db.get_source_submission("delegation", f"agent-question:{rec.work_id}:{question}", session_id=parent.id) is None
        notices = [row for row in await manager.db.load_messages(rec.delegation_id) if row["type"] == "delivery_notice"]
        assert len(notices) == 1 and question in notices[0]["content"]
        assert "Automatic return stopped" in notices[0]["content"]
        assert "owner is unavailable" in notices[0]["content"].casefold()
        assert manager._session_owners[id(parent)] is owner and owner.failed
        assert await manager.answer_question(rec.delegation_id, question, [{"text": "Manual answer"}])
        assert await manager.wait_for_question_answer(rec.delegation_id, question) == "Q: Keep this original question\nA: Manual answer"
    finally:
        await manager.close_pending_question(rec.delegation_id, question)
        await manager.archive_session(parent.id, replace=False)
        manager.remove_broadcast("question-delivery-retired")

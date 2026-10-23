"""A completed round owns its original return until admission is confirmed."""

import asyncio

import pytest

from server.sessions.runtime_types import SessionMailboxFull
from tests.test_delegation_work import wait_state
from tests.test_delegation_work import work_runtime as work_runtime


@pytest.mark.parametrize("failure", ["refused", "lost_ack", "shutdown_after_ack"])
async def test_completed_round_retries_its_original_parent_return(work_runtime, monkeypatch, failure):
    manager, delegations, parent, starts = work_runtime
    start, attempted = manager.start_message, False
    reached = asyncio.Event()

    async def intercept(session_id, prompt, **kwargs):
        nonlocal attempted
        returning = session_id == parent.id and kwargs.get("delivery_id", "").startswith("agent-terminal:")
        if returning and not attempted:
            attempted = True
            reached.set()
            if failure == "refused":
                raise SessionMailboxFull("Temporary parent input capacity")
            await start(session_id, prompt, **kwargs)
            if failure == "shutdown_after_ack":
                manager.close_admission()
            raise ConnectionError("Parent acceptance acknowledgment lost")
        return await start(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, "start_message", intercept)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Finish this round once")
    await asyncio.wait_for(reached.wait(), 5)
    await wait_state(delegations, rec.delegation_id, "completed")
    await delegations.confirm_recording()
    pending = manager.dispatch_snapshot(parent.id)
    assert pending["paused"] and len(pending["items"]) == 1
    row = await manager.db.get_submission_for_request(parent.id, pending["items"][0]["request_id"])
    assert row["payload"]["delivery_id"] == f"agent-terminal:{rec.work_id}"
    assert row["receipt"]["accepted"]
    assert starts.qsize() == 1


@pytest.mark.parametrize("failure", ["refused", "lost_ack", "closed_before_retry"])
async def test_pending_delegation_question_retries_under_its_original_identity(work_runtime, monkeypatch, failure):
    manager, delegations, parent, starts = work_runtime
    start, attempted = manager.start_message, False
    reached = asyncio.Event()

    async def intercept(session_id, prompt, **kwargs):
        nonlocal attempted
        returning = session_id == parent.id and kwargs.get("delivery_id", "").startswith("agent-question:")
        if returning and not attempted:
            attempted = True
            reached.set()
            if failure in {"refused", "closed_before_retry"}:
                raise SessionMailboxFull("Temporary parent input capacity")
            await start(session_id, prompt, **kwargs)
            raise ConnectionError("Question acceptance acknowledgment lost")
        return await start(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, "start_message", intercept)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held while asking")
    await asyncio.wait_for(starts.get(), 5)
    question = await manager.create_pending_question(rec.delegation_id, [{"question": "Which option?"}])
    await asyncio.wait_for(reached.wait(), 5)
    if failure == "closed_before_retry":
        await manager.close_pending_question(rec.delegation_id, question)
        await manager.interrupt(rec.delegation_id)
        await manager.await_session_execution(rec.delegation_id)
        await delegations.confirm_recording()
        for item in manager.dispatch_snapshot(parent.id)["items"]:
            row = await manager.db.get_submission_for_request(parent.id, item["request_id"])
            assert row["payload"]["delivery_id"] != f"agent-question:{rec.work_id}:{question}"
        return
    async with asyncio.timeout(4):
        while not manager.dispatch_snapshot(parent.id)["items"]:
            await asyncio.sleep(.01)
    queued = manager.dispatch_snapshot(parent.id)["items"]
    assert len(queued) == 1 and manager.has_pending_question(rec.delegation_id, question)
    row = await manager.db.get_submission_for_request(parent.id, queued[0]["request_id"])
    assert row["payload"]["delivery_id"] == f"agent-question:{rec.work_id}:{question}"
    assert row["payload"]["question_id"] == question
    await manager.close_pending_question(rec.delegation_id, question)

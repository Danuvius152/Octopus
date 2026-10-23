"""Independent returns are new inputs, even after the original caller ends."""

import asyncio

import pytest

from tests.test_submission_recovery import runtime as runtime


@pytest.mark.parametrize("source,delivery_id", [
    ("bg", "bg:late-task"),
    ("research", "research:late-report"),
    ("delegation", "agent-terminal:original-round"),
])
async def test_lost_return_acknowledgment_does_not_reopen_the_caller_or_duplicate_input(runtime, source, delivery_id):
    manager, db, session, _, starts, _ = runtime
    original = await manager.start_message(session.id, "Finish the original caller")
    completion = await asyncio.wait_for(original.wait(), 5)
    assert completion.outcome.status == "success"
    await manager.interrupt(session.id)
    first = await manager.start_message(session.id, "Independent result", source=source, delivery_id=delivery_id)
    # Discard the first acknowledgment as a transport would; the source resends.
    duplicate = await manager.start_message(session.id, "Independent result", source=source, delivery_id=delivery_id)
    assert duplicate.disposition == "duplicate"
    assert duplicate.request_id == first.request_id != original.request_id
    assert (await original.wait()) is completion
    pending = manager.dispatch_snapshot(session.id)
    assert pending["paused"] and [item["request_id"] for item in pending["items"]] == [first.request_id]
    saved = await db.get_submission_for_request(session.id, first.request_id)
    assert saved["receipt"]["accepted"] and saved["payload"]["delivery_id"] == delivery_id
    assert saved["payload"]["source"] == source
    await manager.resume_dispatch(session.id)
    assert (await asyncio.wait_for(duplicate.wait(), 5)).outcome.status == "success"
    assert starts.qsize() == 2

"""Source confirmation observes the original target after the fire is consumed."""

import asyncio

import pytest

from server.knowledge import decode_context
from tests.test_scheduler import capture_model_runs, save_schedule
from tests.test_scheduler import setup as setup


@pytest.mark.parametrize("concurrent", [False, True])
async def test_confirming_the_same_originless_fire_returns_the_original_target_and_receipt(setup, monkeypatch, concurrent):
    manager, db, _runner = setup
    runs = capture_model_runs(manager, monkeypatch, hold=True)
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="confirm-once", agent_id=agent["id"], name="Confirm me", prompt="Original one-shot",
        created_at="2026-10-23T00:00:00Z", run_at="2026-10-23T01:00:00Z",
    )
    snapshot = await db.get_schedule_definition("confirm-once")
    context = decode_context(snapshot["knowledge_context"])
    try:
        if concurrent:
            (first_target, first), (repeated_target, repeated) = await asyncio.gather(*(
                manager.start_one_shot(snapshot, "Original one-shot", context) for _ in range(2)
            ))
        else:
            first_target, first = await manager.start_one_shot(snapshot, "Original one-shot", context)
            repeated_target, repeated = await manager.start_one_shot(snapshot, "Original one-shot", context)
        assert repeated_target == first_target
        assert repeated.request_id == first.request_id and repeated.disposition == "duplicate"
        assert await db.get_schedule_definition("confirm-once") is None
        async with asyncio.timeout(5):
            while not runs:
                await asyncio.sleep(.01)
        assert len(runs) == 1
        assert len([row for row in await db.load_sessions() if row["origin"] == "schedule"]) == 1
        runs[0].release.set()
        assert (await asyncio.wait_for(repeated.wait(), 5)).outcome.status == "success"
    finally:
        for run in runs:
            run.release.set()


async def test_confirmation_after_automatic_archive_observes_the_same_fire_without_reopening_its_target(setup, monkeypatch):
    manager, db, _runner = setup
    runs = capture_model_runs(manager, monkeypatch)
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="archived-once", agent_id=agent["id"], name="Archived fire", prompt="Original fire",
        created_at="2026-10-23T00:00:00Z", run_at="2026-10-23T01:00:00Z",
    )
    snapshot = await db.get_schedule_definition("archived-once")
    context = decode_context(snapshot["knowledge_context"])
    target, receipt = await manager.start_one_shot(snapshot, "Original fire", context)
    await asyncio.wait_for(receipt.wait(), 5)
    await manager.await_session_execution(target)
    await manager.auto_archive_scheduled_session(target)
    async with asyncio.timeout(5):
        while manager.get_session(target) is not None:
            await asyncio.sleep(.01)
    confirmed_target, confirmed = await manager.start_one_shot(snapshot, "Original fire", context)
    assert confirmed_target == target and confirmed.request_id == receipt.request_id
    assert (await confirmed.wait()).outcome.status == "success"
    assert manager.get_session(target) is None and len(runs) == 1

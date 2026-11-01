"""An original receipt distinguishes its driver from a reusable resource."""

import asyncio
import sys

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.harness_profiles import process_profile


@pytest.fixture
async def held(tmp_path, monkeypatch, short_process_shutdown):
    release = tmp_path / "release"
    script = tmp_path / "model.py"
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "for line in sys.stdin:\n"
        " prompt=json.loads(line)['message']['content']\n"
        " print(json.dumps({'type':'system','subtype':'init','session_id':'held-thread'}),flush=True)\n"
        " if prompt=='B':\n"
        f"  while not Path({str(release)!r}).exists(): time.sleep(.005)\n"
        " print(json.dumps({'type':'result','subtype':'success','session_id':'held-thread','is_error':False}),flush=True)\n"
    )
    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile,
        build_turn_argv=lambda ctx: ([sys.executable, "-u", str(script)], {"cwd": ctx.working_dir})))
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Original resources", str(tmp_path), backend="claude-code")
    try:
        yield manager, session, release
    finally:
        release.touch()
        await manager.drain_turns(timeout=3)
        await manager.stop_all_held_processes()
        await db.close()


async def test_original_driver_can_end_while_host_serves_next_request(held, monkeypatch):
    manager, session, release = held
    first = await manager.start_message(session.id, "A")
    assert (await asyncio.wait_for(first.wait(), 2)).outcome.status == "success"
    observation = first.execution
    assert observation.driver_state == "proven"
    original = observation.attempts[0]
    assert original.request_id == first.request_id and original.attempt_id
    assert original.run_lease_id and original.state == "pending"
    second = await manager.start_message(session.id, "B")
    entered = asyncio.Event()
    wait_retirement = original._backend.wait_retirement

    async def observe_retirement():
        entered.set()
        await wait_retirement()

    monkeypatch.setattr(original._backend, "wait_retirement", observe_retirement)
    waiting = asyncio.create_task(observation.wait_resources())
    await entered.wait()
    # Waiting and cancelling A is observation only, including while B is live.
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert original.state == "pending" and not second._completion.done()
    release.touch()
    assert (await asyncio.wait_for(second.wait(), 2)).outcome.status == "success"
    assert second.execution.attempts[0].run_lease_id == original.run_lease_id
    await manager.archive_session(session.id, replace=False)
    assert await asyncio.wait_for(observation.wait_resources(), 2) == ("proven",)
    assert manager.get_session(session.id) is None


async def test_unstarted_withdrawal_finishes_original_driver_observation(held):
    manager, session, _ = held
    await manager.interrupt(session.id)
    receipt = await manager.start_message(session.id, "queued", source="bg", work_id="original-work")
    assert receipt.execution.driver_state == "pending"
    affected = await manager.close_work("original-work")
    await manager.finish_work_cancellation([affected])
    assert (await receipt.wait()).state == "withdrawn"
    await asyncio.wait_for(receipt.execution.wait_driver(), .2)
    assert receipt.execution.attempts == ()


async def test_stopping_an_unstarted_claim_does_not_finish_retained_input_observation(held):
    manager, session, _ = held
    stopped = asyncio.Event()

    async def stop_before_claim(event):
        if event.get("type") == "dequeued" and not stopped.is_set():
            await manager.interrupt(session.id)
            stopped.set()

    manager.on_broadcast("stop-before-claim", stop_before_claim)
    receipt = await manager.start_message(session.id, "A")
    await asyncio.wait_for(stopped.wait(), 1)
    await manager.await_session_execution(session.id, timeout=1)
    assert receipt.execution.driver_state == "pending"
    assert receipt.execution.attempts == ()
    manager.remove_broadcast("stop-before-claim")
    await manager.resume_dispatch(session.id)
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
    await asyncio.wait_for(receipt.execution.wait_driver(), 1)
    assert len(receipt.execution.attempts) == 1


async def test_failed_resource_exit_is_not_successful_retirement(held, monkeypatch):
    manager, session, _ = held
    receipt = await manager.start_message(session.id, "A")
    await asyncio.wait_for(receipt.wait(), 2)
    original = receipt.execution.attempts[0]
    backend = original._backend
    stop = backend._stop

    async def fail():
        await stop()
        raise OSError("exit proof failed")

    monkeypatch.setattr(backend, "_stop", fail)
    with pytest.raises(OSError, match="exit proof failed"):
        await backend.stop()
    assert original.state == "failed"
    with pytest.raises(OSError, match="exit proof failed"):
        await receipt.execution.wait_resources()
    # The injected failure is sticky; dispose it before fixture shutdown.
    session._run.backend = None

"""Original machine receipts retain exit proof without retaining executions."""

import asyncio
import gc
import weakref

from tests.claude_runtime import claude_runtime as claude_runtime
from tests.native_app_server import inject, terminal, until
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import fail_destination_owner, wait_dispatch_idle


async def retained_receipts(manager, session, starts, monkeypatch):
    runs, executions, receipts, handles = [], [], [], []
    make_run = manager._make_run

    def observe_run(*args, **kwargs):
        run = make_run(*args, **kwargs)
        runs.append(weakref.ref(run))
        return run

    monkeypatch.setattr(manager, "_make_run", observe_run)

    async def observe_attempt(event):
        if event.get("type") == "attempt_started":
            original = manager._session_owners[id(session)].runtime.current
            assert len(original.request.prompt) >= 256 * 1024
            executions.append(weakref.ref(original))

    manager.on_broadcast("retained-receipts", observe_attempt)
    try:
        for index in range(8):
            prompt = f"Return {index}: " + "x" * (256 * 1024)
            receipt = await manager.start_message(
                session.id, prompt, source="bg", delivery_id=f"bg:retention:{index}",
            )
            assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
            await wait_dispatch_idle(manager, session.id)
            receipts.append(receipt)
            handles.extend(receipt.execution.attempts)
            await manager.stop_all_held_processes()
            assert await receipt.execution.wait_resources() == ("proven",)
            duplicate = await manager.start_message(
                session.id, prompt, source="bg", delivery_id=f"bg:retention:{index}",
            )
            assert duplicate.request_id == receipt.request_id
            assert duplicate.execution is receipt.execution
            assert await duplicate.wait() == await receipt.wait()
            # The fixture records full launch contexts for protocol assertions;
            # it must not itself own the large inputs in this lifetime probe.
            starts.clear()
        del receipt, duplicate, prompt
        await asyncio.sleep(0)
        gc.collect()
        assert len(receipts) == len(handles) == len(executions) == 8
        assert sum(reference() is not None for reference in runs) <= 1
        assert sum(reference() is not None for reference in executions) <= 1
        assert all(handle.state == "proven" for handle in handles)
        assert all(receipt.execution.driver_state == "proven" for receipt in receipts)
        assert [await handle.wait() for handle in handles] == ["proven"] * 8
    finally:
        manager.remove_broadcast("retained-receipts")


async def test_codex_machine_receipts_release_retired_resources(native_runtime, monkeypatch):
    manager, _, session, _, starts, _ = native_runtime
    await retained_receipts(manager, session, starts, monkeypatch)


async def test_claude_machine_receipts_release_retired_resources(claude_runtime, monkeypatch):
    manager, _, session, starts, _ = claude_runtime
    await retained_receipts(manager, session, starts, monkeypatch)


async def test_early_resource_handle_survives_a_real_retry(native_runtime, tmp_path, monkeypatch):
    manager, _, session, mode, _, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "Original machine return", source="bg", delivery_id="bg:retry")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    first, = receipt.execution.attempts
    assert first.state == "pending"
    mode[0] = "normal"
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Provider at capacity"}
    inject(tmp_path, failure)
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
    assert receipt.execution.attempts[0] is first
    assert len(receipt.execution.attempts) == 2
    assert await receipt.execution.wait_resources() == ("proven", "proven")


async def test_inline_receipt_keeps_the_captured_carrier_resource(native_runtime, tmp_path):
    manager, _, session, mode, _, events = native_runtime
    mode[0] = "steer_wait"
    carrier = await manager.start_message(session.id, "Wait for the helper")
    await until(lambda: any(event.get("type") == "tool_use" for event in events))
    original, = carrier.execution.attempts
    inline = await manager.start_message(session.id, "Use another file", steerable=True)
    assert inline.disposition == "steered"
    await until(lambda: bool(inline.execution.attempts))
    assert inline.execution.attempts[0] is original
    assert original.state == "pending"
    (tmp_path / "native-finish").touch()
    assert (await asyncio.wait_for(carrier.wait(), 5)).outcome.status == "success"
    await asyncio.wait_for(inline.wait(), 5)
    assert await carrier.execution.wait_resources() == await inline.execution.wait_resources() == ("proven",)


async def test_owner_failure_waits_for_original_resource_retirement(native_runtime, monkeypatch):
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    receipt = await manager.start_message(session.id, "Original failed-owner input", source="bg", delivery_id="bg:failure")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    original, = receipt.execution.attempts
    backend = session._run.backend
    stop = backend._stop
    entered, release = asyncio.Event(), asyncio.Event()

    async def retire():
        entered.set()
        await release.wait()
        await stop()

    monkeypatch.setattr(backend, "_stop", retire)
    try:
        owner = await fail_destination_owner(manager, session, monkeypatch)
        await asyncio.wait_for(entered.wait(), 3)
        assert (await receipt.wait()).state == "unavailable"
        assert original.state == receipt.execution.driver_state == "pending"
        release.set()
        await asyncio.wait_for(asyncio.shield(owner.resource_cleanup), 3)
        assert await receipt.execution.wait_resources() == ("proven",)
        assert receipt.execution.driver_state == "proven"
        assert (await receipt.wait()).outcome is None
    finally:
        release.set()

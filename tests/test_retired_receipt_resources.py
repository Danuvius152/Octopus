"""Original machine receipts retain exit proof without retaining executions."""

import asyncio
import gc
import json
import weakref
from pathlib import Path

from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inputs
from tests.fake_run import capture_model_runs
from tests.native_app_server import inject, terminal, until
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import fail_destination_owner, wait_dispatch_idle


async def assert_resume_after_exit(manager, database, session, starts, original, monkeypatch, prepare_replacement):
    run = session._run.backend
    resource, = original.execution.attempts
    pid = run.process_group_id
    native_id = await database.get_session_resume_id(session.id)
    assert native_id and pid and Path(f"/proc/{pid}").exists()
    entered, release = asyncio.Event(), asyncio.Event()
    stop = run._stop_process
    read_resume = database.get_session_resume_id
    exit_at_read = []

    async def stop_at_barrier():
        entered.set()
        await release.wait()
        await stop()

    async def observe_resume(session_id):
        exit_at_read.append((resource.state, Path(f"/proc/{pid}").exists()))
        return await read_resume(session_id)

    monkeypatch.setattr(run, "_stop_process", stop_at_barrier)
    monkeypatch.setattr(database, "get_session_resume_id", observe_resume)
    launched = len(starts)
    try:
        await manager.interrupt(session.id)
        await asyncio.wait_for(entered.wait(), 3)
        prepare_replacement()
        session.claude_session_id = None
        replacement = await manager.start_message(session.id, "Resume the saved history on a new host")
        assert resource.state == "pending"
        assert replacement.execution.attempts == ()
        assert not exit_at_read and len(starts) == launched
    finally:
        release.set()
    assert (await asyncio.wait_for(original.wait(), 5)).outcome.status == "interrupted"
    assert (await asyncio.wait_for(replacement.wait(), 5)).outcome.status == "success"
    assert exit_at_read == [("proven", False)]
    assert starts[-1].resume_id == native_id
    assert replacement.execution.attempts[0].run_lease_id != resource.run_lease_id


async def test_claude_reuses_compatible_host_then_resumes_after_exit(claude_runtime, tmp_path, monkeypatch):
    manager, database, session, starts, _ = claude_runtime
    first = await manager.start_message(session.id, "Establish durable history")
    assert (await asyncio.wait_for(first.wait(), 5)).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    resource, = first.execution.attempts

    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    second = await manager.start_message(session.id, "Continue on the compatible host")
    initial, reused = await inputs(tmp_path, 2)
    assert initial["pid"] == reused["pid"]
    assert second.execution.attempts[0].run_lease_id == resource.run_lease_id
    assert resource.state == "pending"
    await assert_resume_after_exit(manager, database, session, starts, second, monkeypatch, plan.unlink)


async def test_codex_resumes_after_original_host_exit(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "held"
    original = await manager.start_message(session.id, "Establish durable history")
    await until(lambda: (tmp_path / "native-business.jsonl").exists())

    def prepare_replacement():
        mode[0] = "normal"

    await assert_resume_after_exit(manager, database, session, starts, original, monkeypatch, prepare_replacement)


async def test_claude_configuration_replacement_reads_history_committed_during_exit(claude_runtime, tmp_path, monkeypatch):
    manager, database, session, starts, _ = claude_runtime
    original = await manager.start_message(session.id, "Establish durable history")
    assert (await asyncio.wait_for(original.wait(), 5)).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    old_run = session._run.backend
    resource, = original.execution.attempts
    first, = await inputs(tmp_path, 1)
    (tmp_path / "claude-fresh-ids.json").write_text("[]")
    latest_id = "history-committed-during-exit"
    entered, release = asyncio.Event(), asyncio.Event()
    stop = old_run._stop_process

    async def stop_with_late_history():
        entered.set()
        await release.wait()
        await stop()
        await database.update_session_field(session.id, claude_session_id=latest_id)

    monkeypatch.setattr(old_run, "_stop_process", stop_with_late_history)
    try:
        await manager.update_session_configuration(session.id, {"model": "sonnet"}, session.user_id)
        replacement = await manager.start_message(session.id, "Continue with the new model")
        await asyncio.wait_for(entered.wait(), 3)
        assert resource.state == "pending" and replacement.execution.attempts == ()
        assert (await database.get_session_resume_id(session.id)) != latest_id
        assert len(starts) == 1
    finally:
        release.set()

    assert (await asyncio.wait_for(replacement.wait(), 5)).outcome.status == "success"
    assert await original.execution.wait_resources() == ("proven",)
    assert starts[-1].resume_id == latest_id
    _, second = await inputs(tmp_path, 2)
    assert second["pid"] != first["pid"] and not Path(f"/proc/{first['pid']}").exists()
    assert second["argv"][second["argv"].index("--resume") + 1] == latest_id


async def test_business_peer_reports_retirement_through_original_receipt(claude_runtime, monkeypatch):
    manager, _, session, _, _ = claude_runtime
    started = capture_model_runs(manager, monkeypatch, hold=True)
    original = await manager.start_message(session.id, "Original business input")
    await until(lambda: bool(started))
    resource, = original.execution.attempts
    assert resource.state == "pending"
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(original.wait(), 5)).outcome.status == "interrupted"
    assert await original.execution.wait_resources() == ("proven",)


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

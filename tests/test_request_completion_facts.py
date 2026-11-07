"""A request's committed outcome and its notification are independent facts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agent_manager import AgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.harness.run import HarnessRun
from server.sessions.owner import SessionOwnerUnavailable
from server.wiki import job_execution
from server.wiki.store import WikiError
from tests.test_delegation_notification_failure import (
    notification_runtime as notification_runtime,
)
from tests.test_delegation_notification_failure import (
    release_model,
)


async def test_failed_notification_preserves_the_exact_committed_completion(notification_runtime):
    runtime = notification_runtime
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker",
        request="Keep the result when the original question notification fails",
    )
    await asyncio.wait_for(runtime.entered.wait(), 2)
    receipt = next(receipt for sid, work, receipt in runtime.receipts
                   if sid == rec.delegation_id and work == rec.work_id)
    await release_model(runtime, rec)
    async with asyncio.timeout(2):
        while await runtime.manager.db.get_committed_terminal(rec.delegation_id, receipt.request_id) is None:
            await asyncio.sleep(.005)
    runtime.release.set()
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert receipt.operation == completion.operation == completion.outcome.operation == "message"
    assert completion.input_request_id == completion.request_id == receipt.request_id
    assert completion.state == "completed" and completion.recording_state == "committed"
    assert completion.outcome.status == "success"
    assert completion.notification_state == "failed"
    assert not [event for event in runtime.frames
                if event.get("request_id") == receipt.request_id and event.get("type") == "result"]


async def test_owner_failure_after_terminal_commit_does_not_lose_the_acknowledgement(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    database = runtime.manager.db
    sequence = database.message_sequence_state
    target = {}

    async def fail_projection_after_actual_commit(session_id):
        projection = await sequence(session_id)
        receipt = target.get("receipt")
        if receipt is not None and session_id == target["session_id"]:
            terminal = await database.get_committed_terminal(session_id, receipt.request_id)
            if terminal is not None:
                assert terminal["status"] == "success"
                raise RuntimeError("Projection failed after the original terminal transaction returned")
        return projection

    monkeypatch.setattr(database, "message_sequence_state", fail_projection_after_actual_commit)
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker",
        request="Keep the acknowledgement before the projection await",
    )
    receipt = next(receipt for sid, work, receipt in runtime.receipts
                   if sid == rec.delegation_id and work == rec.work_id)
    target.update(receipt=receipt, session_id=rec.delegation_id)
    await release_model(runtime, rec)
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.input_request_id == completion.request_id == receipt.request_id
    assert completion.state == "completed" and completion.recording_state == "committed"
    assert completion.outcome.status == "success"
    assert completion.notification_state == "unavailable"
    assert (await database.get_committed_terminal(rec.delegation_id, receipt.request_id)) == completion.outcome.model_dump()
    assert len(runtime.starts) == 1


async def test_wiki_work_guard_releases_failed_owner_only_after_confirmed_resource_cleanup(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    database = runtime.manager.db
    sequence, stop = database.message_sequence_state, HarnessRun.stop
    model = runtime.root / "notification_model.py"
    model.write_text(model.read_text() + "sys.stdin.read()\n")
    retiring, release = asyncio.Event(), asyncio.Event()
    target = {}

    async def fail_projection(session_id):
        projection = await sequence(session_id)
        if target.get("session_id") == session_id and target.get("fail"):
            if await database.get_committed_terminal(session_id, target["receipt"].request_id):
                raise RuntimeError("Projection failed after terminal commit")
        return projection

    async def retire_original(run):
        if run.work_id == target.get("work_id"):
            retiring.set()
            await release.wait()
        await stop(run)

    monkeypatch.setattr(database, "message_sequence_state", fail_projection)
    monkeypatch.setattr(HarnessRun, "stop", retire_original)
    rec = await runtime.delegations.start_delegation(parent_session_id=runtime.parent.id,
        agent_name="Notification worker", request="Retain resource proof after Owner failure")
    receipt = next(receipt for sid, work, receipt in runtime.receipts
                   if sid == rec.delegation_id and work == rec.work_id)
    target.update(session_id=rec.delegation_id, work_id=rec.work_id, receipt=receipt, fail=True)
    reference = {"request_id": receipt.request_id, "session_id": rec.delegation_id,
                 "work_id": rec.work_id, "phase": "settled", "retired": True}
    job = {"id": receipt.request_id, "session_id": rec.delegation_id, "items": [{"worker_request": reference}]}
    store = SimpleNamespace(state=runtime.root)
    try:
        await release_model(runtime, rec)
        assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
        await asyncio.wait_for(retiring.wait(), 2)
        assert runtime.manager.session_owner_failed(rec.delegation_id)
        assert runtime.manager.work_has_resources(rec.work_id)
        with pytest.raises(WikiError, match="previous maintenance execution has not retired"):
            job_execution.require_retired(store, job, manager=runtime.manager)
        target["fail"] = False
        release.set()
        await asyncio.wait_for(job_execution.retire(runtime.manager, reference), 3)
        assert not runtime.manager.work_has_resources(rec.work_id)
        job_execution.require_retired(store, job, manager=runtime.manager)
        await runtime.manager.archive_session(rec.delegation_id, replace=False)
        assert runtime.manager.get_session(rec.delegation_id) is None
        assert runtime.manager.session_owner_failed(rec.delegation_id)
        job_execution.require_retired(store, job, manager=runtime.manager)
    finally:
        target["fail"] = False
        release.set()


async def test_slow_initial_text_cannot_overtake_the_nested_relay(notification_runtime, monkeypatch):
    runtime = notification_runtime
    model = runtime.root / "notification_model.py"
    model.write_text(model.read_text().replace(
        "gate.with_suffix('.input').write_text(sys.stdin.readline())",
        "input_frame=sys.stdin.readline()\ngate.with_suffix('.input').write_text(input_frame)",
    ).replace("'Original body from '+sid", "('FINAL RELAY ANSWER' if '[agent-reply:' in input_frame else 'INITIAL WAITING TEXT')"))
    await AgentManager(runtime.manager.db).create_agent(name="Leaf worker", backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Delegate and relay",
    )
    await asyncio.wait_for(runtime.entered.wait(), 2)
    scope = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    leaf = await runtime.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf worker", request="Leaf answer", scope=scope,
    )
    first = next(receipt for sid, work, receipt in runtime.receipts
                 if sid == outer.delegation_id and work == outer.work_id)
    load = runtime.manager.db.read_session_result
    release_text = asyncio.Event()

    async def delayed_initial_text(session_id, request_id):
        if session_id == outer.delegation_id and request_id == first.request_id:
            await release_text.wait()
        return await load(session_id, request_id)

    monkeypatch.setattr(runtime.manager.db, "read_session_result", delayed_initial_text)
    try:
        await release_model(runtime, outer)
        runtime.release.set()
        await asyncio.wait_for(first.wait(), 2)
        await release_model(runtime, leaf)
        async with asyncio.timeout(3):
            while sum(scope.session_id == outer.delegation_id for scope, _ in runtime.starts) < 2:
                await asyncio.sleep(.005)
        relay = next(receipt for sid, work, receipt in reversed(runtime.receipts)
                     if sid == outer.delegation_id and work == outer.work_id and receipt is not first)
        await release_model(runtime, outer)
        await asyncio.wait_for(relay.wait(), 2)
        release_text.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        assert outer.state == leaf.state == "completed"
        replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
        assert len(replies) == 1
        assert "FINAL RELAY ANSWER" in replies[0]["summary"]
        assert "INITIAL WAITING TEXT" not in replies[0]["summary"]
        assert len(runtime.starts) == 3
    finally:
        release_text.set()


@pytest.mark.parametrize("direct_chat", [False, True], ids=["initial-build", "direct-build-chat"])
async def test_application_build_finishes_from_its_receipt_when_the_prefix_fails(
    notification_runtime, monkeypatch, direct_chat,
):
    runtime = notification_runtime
    monkeypatch.setattr(settings, "applications_dir", str(runtime.root / "apps"))
    applications = ApplicationManager()
    applications.bind(runtime.manager, runtime.manager.db)
    runtime.controls.enabled = not direct_chat
    agent = await AgentManager(runtime.manager.db).create_agent(name="Original builder", backend="codex", user_id=runtime.parent.user_id)
    try:
        application = await applications.create_application(
            name="Original build", description="Keep the original application output.", agent_id=agent["id"],
        )
        session_id = application["session_id"]
        index = Path(application["app_dir"]) / "index.html"
        index.write_text("<p>Original application output.</p>")
        receipt = next(receipt for sid, _work, receipt in runtime.receipts if sid == session_id)
        if direct_chat:
            async with asyncio.timeout(2):
                while not any(scope.session_id == session_id for scope, _ in runtime.starts):
                    await asyncio.sleep(.005)
            scope = next(scope for scope, _ in reversed(runtime.starts) if scope.session_id == session_id)
            (runtime.root / f"{scope.run_lease_id}.result").touch()
            assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
            await runtime.manager.await_session_execution(session_id)
            async with asyncio.timeout(2):
                while (await applications.get_application(application["id"]))["status"] != "ready":
                    await asyncio.sleep(.005)
            runtime.controls.enabled = True
            receipt = await runtime.manager.start_message(session_id, "Change this build through direct chat")
        await asyncio.wait_for(runtime.entered.wait(), 2)
        scope = next(scope for scope, _ in reversed(runtime.starts) if scope.session_id == session_id)
        (runtime.root / f"{scope.run_lease_id}.result").touch()
        runtime.release.set()
        completion = await asyncio.wait_for(receipt.wait(), 2)
        assert completion.state == "completed" and completion.outcome.status == "success"
        assert completion.notification_state == "failed"
        async with asyncio.timeout(2):
            while (updated := await applications.get_application(application["id"]))["status"] == "building":
                await asyncio.sleep(.005)
        assert updated["status"] == "ready" and updated["error"] is None
        assert index.read_text() == "<p>Original application output.</p>"
        assert not [event for event in runtime.frames
                    if event.get("type") == "result" and event.get("request_id") == receipt.request_id]
        assert len(runtime.starts) == (2 if direct_chat else 1)
    finally:
        applications.shutdown()


async def test_an_unavailable_build_is_not_completed_just_because_its_file_exists(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr(settings, "applications_dir", str(runtime.root / "apps"))
    applications = ApplicationManager()
    applications.bind(runtime.manager, runtime.manager.db)
    agent = await AgentManager(runtime.manager.db).create_agent(name="Interrupted builder", backend="codex", user_id=runtime.parent.user_id)
    read = runtime.manager.db.has_delivery_notice

    async def fail_original_owner(session_id, delivery_id):
        result = await read(session_id, delivery_id)
        if delivery_id == "fail-before-build-terminal":
            raise RuntimeError("Build writer ended without a committed terminal")
        return result

    try:
        application = await applications.create_application(
            name="Uncertain build", description="Keep uncertain build evidence.", agent_id=agent["id"],
        )
        session_id = application["session_id"]
        index = Path(application["app_dir"]) / "index.html"
        index.write_text("<p>Existing file is not a terminal.</p>")
        async with asyncio.timeout(2):
            while not any(event.get("session_id") == session_id and event.get("type") == "assistant_text"
                          for event in runtime.frames):
                await asyncio.sleep(.005)
        monkeypatch.setattr(runtime.manager.db, "has_delivery_notice", fail_original_owner)
        with pytest.raises(SessionOwnerUnavailable):
            await runtime.manager.retain_result_notice(session_id, "fail-before-build-terminal", "Do not retry")
        receipt = next(receipt for sid, _work, receipt in runtime.receipts if sid == session_id)
        completion = await asyncio.wait_for(receipt.wait(), 2)
        assert completion.state == "unavailable" and completion.outcome is None
        async with asyncio.timeout(2):
            while (updated := await applications.get_application(application["id"]))["status"] == "building":
                await asyncio.sleep(.005)
        assert updated["status"] == "failed" and "owner" in updated["error"].lower()
        assert await runtime.manager.db.get_committed_terminal(session_id, receipt.request_id) is None
        assert index.read_text() == "<p>Existing file is not a terminal.</p>"
        assert len(runtime.starts) == 1
    finally:
        applications.shutdown()


async def test_owner_failure_does_not_assign_a_terminal_to_an_unconfirmed_steer(
    notification_runtime, monkeypatch, short_process_shutdown,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    database = runtime.manager.db
    agent = await database.get_system_agent()
    session = await runtime.manager.create_session(agent["id"], "Unconfirmed correction", str(runtime.root), backend="codex")
    correction = "Keep this correction unknown while its original write has no result."
    sent, cancelled, release_write = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = HarnessRun.send_input
    original_writes = []

    async def hold_original_write(backend, text, **kwargs):
        result = await send(backend, text, **kwargs)
        original_writes.append(text)
        if text == correction:
            sent.set()
            try:
                await release_write.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release_write.wait()
                raise
        return result

    read = database.has_delivery_notice

    async def fail_original_writer(session_id, delivery_id):
        exists = await read(session_id, delivery_id)
        if delivery_id == "fail-unconfirmed-correction-owner":
            raise RuntimeError("Original correction writer ended before its write result")
        return exists

    monkeypatch.setattr(HarnessRun, "send_input", hold_original_write)
    try:
        initial = await runtime.manager.start_message(session.id, "Continue until interrupted.")
        async with asyncio.timeout(2):
            while not any(event.get("session_id") == session.id and event.get("type") == "assistant_text"
                          for event in runtime.frames):
                await asyncio.sleep(.005)
        inline = await runtime.manager.start_message(session.id, correction, steerable=True)
        assert inline.disposition == "steered"
        await asyncio.wait_for(sent.wait(), 2)
        await runtime.manager.interrupt(session.id)
        await asyncio.wait_for(cancelled.wait(), 2)
        terminal = await database.get_committed_terminal(session.id, initial.request_id)
        assert terminal["status"] == "interrupted"
        monkeypatch.setattr(database, "has_delivery_notice", fail_original_writer)
        with pytest.raises(SessionOwnerUnavailable):
            await runtime.manager.retain_result_notice(session.id, "fail-unconfirmed-correction-owner", "Retain original evidence")
        completion = await asyncio.wait_for(inline.wait(), 2)
        assert completion.input_request_id == inline.request_id
        assert completion.request_id == initial.request_id
        assert completion.state == "unavailable" and completion.outcome is None
        assert completion.recording_state == "none" and completion.notification_state == "unavailable"
        initial_result = await asyncio.wait_for(initial.wait(), 2)
        assert initial_result.state == "completed" and initial_result.outcome.status == "interrupted"
        assert await database.get_committed_terminal(session.id, inline.request_id) is None
        rows = await database.load_messages(session.id)
        assert [row["content"] for row in rows if row["role"] == "user"] == ["Continue until interrupted.", correction]
        assert original_writes == ["Continue until interrupted.", correction]
        assert len(runtime.starts) == 1
    finally:
        release_write.set()
        # This already-failed writer owns its original retirement. Join that
        # resource deadline rather than imposing the shorter idle-sweep wait.
        await runtime.manager.await_session_execution(session.id, timeout=5)


async def test_late_running_notification_cannot_reopen_a_completed_build(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr(settings, "applications_dir", str(runtime.root / "apps"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_original_input(event):
        if event.get("type") == "user_message":
            entered.set()
            await release.wait()

    runtime.manager.on_broadcast("hold-original-build-prefix", hold_original_input)
    applications = ApplicationManager()
    applications.bind(runtime.manager, runtime.manager.db)
    agent = await AgentManager(runtime.manager.db).create_agent(name="Late build prefix", backend="codex", user_id=runtime.parent.user_id)
    sequence = runtime.manager.db.message_sequence_state
    target = {}

    async def fail_projection(session_id):
        projection = await sequence(session_id)
        if session_id == target.get("session_id") and await runtime.manager.db.get_committed_terminal(session_id, target["request_id"]):
            raise RuntimeError("Projection ended after committed build, before original notifications")
        return projection

    try:
        application = await applications.create_application(name="Late prefix", description="Preserve the completed build.", agent_id=agent["id"])
        receipt = next(receipt for sid, _work, receipt in runtime.receipts if sid == application["session_id"])
        target.update(session_id=application["session_id"], request_id=receipt.request_id)
        Path(application["app_dir"], "index.html").write_text("<p>Committed build.</p>")
        monkeypatch.setattr(runtime.manager.db, "message_sequence_state", fail_projection)
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(2):
            while not any(scope.session_id == application["session_id"] for scope, _ in runtime.starts):
                await asyncio.sleep(.005)
        scope = next(scope for scope, _ in runtime.starts if scope.session_id == application["session_id"])
        (runtime.root / f"{scope.run_lease_id}.result").touch()
        completion = await asyncio.wait_for(receipt.wait(), 2)
        assert completion.outcome.status == "success" and completion.notification_state == "unavailable"
        async with asyncio.timeout(2):
            while (await applications.get_application(application["id"]))["status"] != "ready":
                await asyncio.sleep(.005)
        release.set()
        await runtime.manager.close_session_owners()
        assert (await applications.get_application(application["id"]))["status"] == "ready"
        assert any(event.get("type") == "status" and event.get("status") == "running"
                   and event.get("session_id") == application["session_id"] for event in runtime.frames)
        assert len(runtime.starts) == 1
    finally:
        release.set()
        applications.shutdown()


async def test_slow_old_build_completion_cannot_finish_the_next_build(notification_runtime, monkeypatch):
    from server.app_backends import backend_supervisor

    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr(settings, "applications_dir", str(runtime.root / "apps"))
    entered, release, old_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rebuild = backend_supervisor.on_rebuild
    rebuilds = []

    async def hold_first_rebuild(app_id, app_dir):
        await rebuild(app_id, app_dir)
        rebuilds.append(app_id)
        if len(rebuilds) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(backend_supervisor, "on_rebuild", hold_first_rebuild)
    register = runtime.manager.on_request_complete

    def observe_completion(key, callback):
        async def consumed(session_id, completion):
            await callback(session_id, completion)
            if len(rebuilds) == 1:
                old_finished.set()
        register(key, consumed)

    monkeypatch.setattr(runtime.manager, "on_request_complete", observe_completion)
    applications = ApplicationManager()
    applications.bind(runtime.manager, runtime.manager.db)
    agent = await AgentManager(runtime.manager.db).create_agent(name="Build order", backend="codex", user_id=runtime.parent.user_id)
    try:
        application = await applications.create_application(name="Build order", description="Keep the second build active.", agent_id=agent["id"])
        sid = application["session_id"]
        Path(application["app_dir"], "index.html").write_text("<p>First build.</p>")
        async with asyncio.timeout(2):
            while not any(scope.session_id == sid for scope, _ in runtime.starts):
                await asyncio.sleep(.005)
        first_scope = next(scope for scope, _ in runtime.starts if scope.session_id == sid)
        (runtime.root / f"{first_scope.run_lease_id}.result").touch()
        await asyncio.wait_for(entered.wait(), 2)
        second = await runtime.manager.start_message(sid, "Keep the second build running.")
        async with asyncio.timeout(2):
            while len(runtime.starts) < 2:
                await asyncio.sleep(.005)
        assert runtime.manager.dispatch_snapshot(sid)["active_request_id"] == second.request_id
        release.set()
        await asyncio.wait_for(old_finished.wait(), 2)
        assert (await applications.get_application(application["id"]))["status"] == "building"
        assert not second._completion.done()
        second_scope = next(scope for scope, _ in reversed(runtime.starts) if scope.session_id == sid)
        (runtime.root / f"{second_scope.run_lease_id}.result").touch()
        assert (await asyncio.wait_for(second.wait(), 2)).outcome.status == "success"
    finally:
        release.set()
        applications.shutdown()


async def test_failed_owner_keeps_an_unsent_correction_on_its_own_request(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    database = runtime.manager.db
    monkeypatch.setattr(settings, "users_root", str(runtime.root / "accounts"))
    owner = "original-steer-owner"
    await database.create_user(user_id=owner, username="original-owner", password_hash="unused-test-login", created_at="2026-10-21")
    agent = await AgentManager(database).create_agent(name="Owned correction", user_id=owner, backend="codex")
    session = await runtime.manager.create_session(agent["id"], "Unsent correction", backend="codex")
    availability_entered, release_availability, availability_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fault_entered, release_fault = asyncio.Event(), asyncio.Event()
    availability = database.set_steer_delivery
    read = database.has_delivery_notice
    gate = False

    async def hold_original_authority(session_id, request_id, *, confirmed):
        checking = gate and session_id == session.id and not confirmed
        if checking:
            availability_entered.set()
            await release_availability.wait()
        available = await availability(session_id, request_id, confirmed=confirmed)
        if checking:
            assert not available
            # No await follows a rejected final authority read in _write_steer.
            # Its False result exists before this waiter resumes.
            availability_returned.set()
        return available

    async def fail_after_original_notice_read(session_id, delivery_id):
        exists = await read(session_id, delivery_id)
        if delivery_id == "fail-after-unsent-write":
            fault_entered.set()
            await release_fault.wait()
            raise RuntimeError("Original notice failed after the correction was rejected")
        return exists

    monkeypatch.setattr(database, "set_steer_delivery", hold_original_authority)
    monkeypatch.setattr(database, "has_delivery_notice", fail_after_original_notice_read)
    fault = None
    try:
        initial = await runtime.manager.start_message(session.id, "Continue the original work.")
        async with asyncio.timeout(2):
            while not any(event.get("type") == "assistant_text" and event.get("session_id") == session.id
                          for event in runtime.frames):
                await asyncio.sleep(.005)
        gate = True
        correction = await runtime.manager.start_message(session.id, "Never send this rejected correction.", steerable=True)
        assert correction.disposition == "steered"
        await asyncio.wait_for(availability_entered.wait(), 2)
        await database.update_user_field(owner, disabled_at="2026-10-21")
        fault = asyncio.create_task(runtime.manager.retain_result_notice(session.id, "fail-after-unsent-write", "Original material"))
        await asyncio.wait_for(fault_entered.wait(), 2)
        release_availability.set()
        await asyncio.wait_for(availability_returned.wait(), 2)
        release_fault.set()
        with pytest.raises(SessionOwnerUnavailable):
            await fault
        completion = await asyncio.wait_for(correction.wait(), 2)
        assert completion.state == "withdrawn" and completion.outcome is None
        assert completion.input_request_id == completion.request_id == correction.request_id
        assert completion.request_id != initial.request_id
        assert not [event for event in runtime.frames if event.get("type") == "steered"]
        assert len(runtime.starts) == 1
        assert await database.get_committed_terminal(session.id, correction.request_id) is None
    finally:
        release_availability.set()
        release_fault.set()
        if fault is not None:
            await asyncio.gather(fault, return_exceptions=True)
        for scope, _context in runtime.starts:
            (runtime.root / f"{scope.run_lease_id}.result").touch()
        await runtime.manager.await_session_execution(session.id, timeout=8)
        await runtime.manager.stop_all_held_processes()

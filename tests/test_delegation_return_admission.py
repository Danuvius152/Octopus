"""Delegated return material is prepared before the original caller is restored."""

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.mcp_identity import close_work
from server.sessions.runtime_types import SessionOwnerUnavailable
from tests.test_delegation_restart_security import wait_live_scope
from tests.test_delegation_work import work_runtime as work_runtime
from tests.test_source_return_owner_failure import fail_destination_owner


async def test_terminal_material_preparation_does_not_wake_archived_parent(work_runtime, monkeypatch, tmp_path):
    manager, delegations, parent, starts = work_runtime
    reading, release_material = asyncio.Event(), asyncio.Event()
    read = manager.db.read_session_result
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled original return material",
    )
    await asyncio.wait_for(starts.get(), 3)

    async def prepare_original_material(session_id, request_id):
        if session_id == rec.delegation_id:
            reading.set()
            await release_material.wait()
        return await read(session_id, request_id)

    monkeypatch.setattr(manager.db, "read_session_result", prepare_original_material)
    await manager.archive_session(parent.id, replace=False)
    try:
        (tmp_path / "release").touch()
        await asyncio.wait_for(reading.wait(), 3)
        assert manager.get_session(parent.id) is None, "Pure return preparation must precede lifecycle wake-up"
        release_material.set()
        await asyncio.wait_for(delegations.confirm_recording(), 3)
        assert rec.state == "completed" and rec.error is None
        original = await manager.source_receipt("delegation", f"agent-terminal:{rec.work_id}", session_id=parent.id)
        assert original is not None and original[1].accepted
        snapshot = manager.dispatch_snapshot(parent.id)
        assert snapshot["paused"] is True
        assert [item["request_id"] for item in snapshot["items"]] == [original[1].request_id]
        assert starts.empty()
    finally:
        release_material.set()
        (tmp_path / "release").touch()
        await delegations.confirm_recording()


async def test_question_material_is_prepared_before_archived_parent_wakes(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held while asking",
    )
    await asyncio.wait_for(starts.get(), 3)
    await manager.archive_session(parent.id, replace=False)
    render = delegations._render_question_body
    preparation_membership = []

    def read_question_material(questions):
        preparation_membership.append(manager.get_session(parent.id))
        return render(questions)

    monkeypatch.setattr(delegations, "_render_question_body", read_question_material)
    question_id = await manager.create_pending_question(rec.delegation_id, [{"question": "Original question"}])
    await asyncio.wait_for(delegations.confirm_recording(), 3)
    assert preparation_membership == [None]
    original = await manager.source_receipt(
        "delegation", f"agent-question:{rec.work_id}:{question_id}", session_id=parent.id,
    )
    assert original is not None and original[1].accepted
    assert manager.has_pending_question(rec.delegation_id, question_id)
    assert rec.state == "running"
    snapshot = manager.dispatch_snapshot(parent.id)
    assert snapshot["paused"] and [item["request_id"] for item in snapshot["items"]] == [original[1].request_id]
    assert starts.empty()


async def test_question_closed_during_real_parent_archive_never_starts_an_answer(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held while asking",
    )
    await asyncio.wait_for(starts.get(), 3)
    retiring, release_archive = asyncio.Event(), asyncio.Event()
    retire = manager._retire_application_history

    async def hold_retirement(session):
        if session.id == parent.id:
            retiring.set()
            await release_archive.wait()
        await retire(session)

    monkeypatch.setattr(manager, "_retire_application_history", hold_retirement)
    archive = asyncio.create_task(manager.archive_session(parent.id, replace=False))
    try:
        await asyncio.wait_for(retiring.wait(), 3)
        question_id = await manager.create_pending_question(rec.delegation_id, [{"question": "Original question"}])
        lock = manager._session_lifecycle_lock(parent.id)
        async with asyncio.timeout(3):
            while not lock._waiters:
                await asyncio.sleep(.001)
        await manager.close_pending_question(rec.delegation_id, question_id)
        release_archive.set()
        await asyncio.wait_for(archive, 3)
        await asyncio.wait_for(delegations.confirm_recording(), 3)
        original = await manager.source_receipt(
            "delegation", f"agent-question:{rec.work_id}:{question_id}", session_id=parent.id,
        )
        assert original is not None and original[1].accepted
        assert manager.dispatch_snapshot(parent.id)["paused"]
        await manager.resume_dispatch(parent.id)
        completion = await asyncio.wait_for(original[1].wait(), 3)
        assert completion.state == "expired" and completion.reason == "expired"
        assert starts.empty() and rec.state == "running"
        assert not manager.has_pending_question(rec.delegation_id, question_id)
    finally:
        release_archive.set()
        await archive


async def test_parent_work_closed_during_terminal_preparation_cannot_wake_or_change_child_outcome(
    work_runtime, monkeypatch, tmp_path,
):
    manager, delegations, parent, starts = work_runtime
    await AgentManager(manager.db).create_agent(name="Leaf", backend="codex", user_id=parent.user_id)
    caller = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held original caller",
    )
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    rec = await delegations.start_delegation(
        parent_session_id=caller.delegation_id, agent_name="Leaf", request="controlled original leaf", scope=scope,
    )
    await asyncio.wait_for(starts.get(), 3)
    reading, release_material = asyncio.Event(), asyncio.Event()
    read = manager.db.read_session_result

    async def hold_original_material(session_id, request_id):
        if session_id == rec.delegation_id:
            reading.set()
            await release_material.wait()
        return await read(session_id, request_id)

    monkeypatch.setattr(manager.db, "read_session_result", hold_original_material)
    try:
        (tmp_path / "release").touch()
        await asyncio.wait_for(reading.wait(), 3)
        await manager.archive_session(caller.delegation_id, replace=False)
        close_work(caller.work_id)
        release_material.set()
        await asyncio.wait_for(delegations.confirm_recording(), 3)
        assert rec.parent_work_id == caller.work_id
        assert rec.state == "completed" and rec.error is None and rec._terminal_disposition == "closed"
        assert manager.get_session(caller.delegation_id) is None and starts.empty()
        assert await manager.source_receipt(
            "delegation", f"agent-terminal:{rec.work_id}", session_id=caller.delegation_id,
        ) is None
        rows = await manager.db.load_messages(rec.delegation_id)
        assert any(row["role"] == "assistant" and row["content"] == "reply" for row in rows)
    finally:
        release_material.set()
        (tmp_path / "release").touch()
        await delegations.confirm_recording()


@pytest.mark.parametrize("source", ["terminal", "question"])
async def test_unconfirmed_return_recovers_original_input_after_source_loss_and_parent_owner_failure(
    work_runtime, monkeypatch, source,
):
    manager, delegations, parent, starts = work_runtime
    submit, lookup = manager.start_message, manager.source_receipt
    submissions = []
    readable = False

    async def lose_ack(session_id, prompt, **kwargs):
        receipt = await submit(session_id, prompt, **kwargs)
        if session_id == parent.id and kwargs.get("delivery_id", "").startswith(f"agent-{source}:"):
            submissions.append(receipt.request_id)
            raise TimeoutError("Original delegated return acknowledgment lost")
        return receipt

    async def observe(*args, **kwargs):
        if submissions and not readable and args[1].startswith(f"agent-{source}:"):
            raise SessionOwnerUnavailable("Original delegated return receipt is temporarily unavailable")
        return await lookup(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", observe)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker",
        request="held while asking" if source == "question" else "One original child result",
    )
    await asyncio.wait_for(starts.get(), 3)
    question_id = None
    if source == "question":
        question_id = await manager.create_pending_question(rec.delegation_id, [{"question": "Original question"}])
    await asyncio.wait_for(delegations.confirm_recording(), 3)
    if source == "terminal":
        # Completion owns the terminal relay and may enter after start returns.
        async with asyncio.timeout(3):
            while rec._terminal_disposition is None:
                await asyncio.sleep(.01)
        await delegations.confirm_recording()
        assert rec.state == "completed" and rec.error is None
        assert rec._terminal_disposition == "unknown"
    else:
        assert rec._question_deliveries[question_id].disposition == "unknown"
    assert len(submissions) == 1
    await fail_destination_owner(manager, parent, monkeypatch)
    try:
        if source == "terminal":
            assert await manager.delete_session(rec.delegation_id)
        else:
            await manager.close_pending_question(rec.delegation_id, question_id)
        readable = True

        async def deleted_material(*args, **kwargs):
            pytest.fail("An admitted delegated return must not read deleted child material")

        def closed_question_material(*args, **kwargs):
            pytest.fail("An admitted question must not prepare a closed question again")

        monkeypatch.setattr(manager.db, "read_session_result", deleted_material)
        monkeypatch.setattr(delegations, "_render_question_body", closed_question_material)
        if source == "terminal":
            await delegations._command(delegations._inject_terminal(rec))
            delivery_id = f"agent-terminal:{rec.work_id}"
            assert rec.state == "completed" and rec.error is None and rec._terminal_disposition == "duplicate"
        else:
            await manager._broadcast({
                "type": "question_request", "session_id": rec.delegation_id, "work_id": rec.work_id,
                "question_id": question_id, "questions": [{"question": "Stale question event"}],
            })
            delivery_id = f"agent-question:{rec.work_id}:{question_id}"
            assert not manager.has_pending_question(rec.delegation_id, question_id)
        await delegations.confirm_recording()
        original = await manager.source_receipt("delegation", delivery_id, session_id=parent.id)
        assert original is not None and original[1].request_id == submissions[0]
        assert len(submissions) == 1 and starts.empty()
        queued = manager.dispatch_snapshot(parent.id)
        assert queued["paused"] and [item["request_id"] for item in queued["items"]] == submissions
    finally:
        # Retire the deliberately failed Owner through its real lifecycle
        # before the fixture's ordinary Stop cleanup reaches it.
        await manager.archive_session(parent.id, replace=False)

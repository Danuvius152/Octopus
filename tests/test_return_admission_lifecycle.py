"""Independent returns preserve the original lifecycle's wait-and-restore contract."""

import asyncio
import json

import pytest

from server import bg_tasks
from server import delegations as delegation_module
from server.sessions.runtime_types import SessionAdmissionClosed
from server.sessions.submission import ReturnRefused
from tests.test_delegation_work import work_runtime as work_runtime


async def test_archived_deletion_retries_after_a_postcommit_hook_fails(work_runtime):
    manager, _, parent, _ = work_runtime
    await manager.db.create_fork_session(
        fork_id="retainedfork", name="Retained fork", working_dir=parent.working_dir, created_at=parent.created_at,
        parent_id=parent.id, backend=parent.backend, user_id=parent.user_id, agent_id=parent.agent_id,
        credential_id=None, resume_id=None, fork_after_seq=0,
    )
    await manager.archive_session(parent.id, replace=False)
    calls = []

    def fail_once(session_id):
        calls.append(session_id)
        if len(calls) == 1:
            raise OSError("deletion hook failed")

    manager.db.add_session_delete_hook(fail_once)
    with pytest.raises(OSError, match="deletion hook failed"):
        await manager.delete_session(parent.id)
    assert await manager.db.get_session_binding(parent.id) is None
    assert await manager.delete_session(parent.id)
    assert calls == [parent.id, parent.id]
    assert await manager.db.get_session_binding("retainedfork") is not None
    assert not await manager.delete_session(parent.id)


async def test_background_restore_subscriber_can_permanently_delete_the_session(work_runtime, monkeypatch, tmp_path):
    from server.routers import sessions as session_routes

    manager, _, parent, _ = work_runtime
    bg = manager.work_test_bg
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr(bg_tasks, "bg_task_manager", bg)
    archived = asyncio.Event()
    deleted = asyncio.get_running_loop().create_future()

    async def deliver(record):
        await archived.wait()
        return await manager.deliver_bg_result(record)

    async def delete_restored(event):
        if event["type"] == "session_unarchived" and event["session_id"] == parent.id:
            try:
                async with asyncio.timeout(3):
                    response = await manager.work_test_client.delete(f"/api/sessions/{parent.id}")
            except TimeoutError as error:
                deleted.set_exception(error)
            else:
                deleted.set_result(response.status_code)

    bg.bind(manager.db, deliver, manager._broadcast)
    manager.on_broadcast("delete restored background owner", delete_restored)
    try:
        await bg.start_task(session_id=parent.id, command="true", working_dir=str(tmp_path))
        await manager.archive_session(parent.id, replace=False)
        archived.set()
        assert await asyncio.wait_for(deleted, 5) == 204
        assert await manager.db.get_session_binding(parent.id) is None
    finally:
        archived.set()
        manager.remove_broadcast("delete restored background owner")


async def test_delegation_terminal_waits_for_real_parent_archive_and_admits_once(work_runtime, monkeypatch, tmp_path):
    manager, delegations, parent, starts = work_runtime
    retiring, release_archive, returning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retire = manager._retire_application_history
    admit = manager.admit_return
    archive = recording = None

    async def hold_retirement(session):
        if session.id == parent.id:
            retiring.set()
            await release_archive.wait()
        await retire(session)

    async def observe_return(target, **kwargs):
        if target.session_id == parent.id:
            returning.set()
        return await admit(target, **kwargs)

    monkeypatch.setattr(manager, "_retire_application_history", hold_retirement)
    monkeypatch.setattr(manager, "admit_return", observe_return)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled archive race",
    )
    await asyncio.wait_for(starts.get(), 3)
    try:
        archive = asyncio.create_task(manager.archive_session(parent.id, replace=False))
        await asyncio.wait_for(retiring.wait(), 3)
        (tmp_path / "release").touch()
        await asyncio.wait_for(returning.wait(), 3)
        recording = asyncio.create_task(delegations.confirm_recording())
        _, pending = await asyncio.wait({recording}, timeout=0.1)
        assert pending, "The return must wait for archive, without failing or declaring delivery stopped"
        release_archive.set()
        await asyncio.wait_for(archive, 3)
        await asyncio.wait_for(recording, 3)
        assert rec.state == "completed" and rec.error is None
        assert rec.to_public_dict()["terminal_disposition"] == "accepted"
        original = await manager.source_receipt("delegation", f"agent-terminal:{rec.work_id}", session_id=parent.id)
        assert original is not None and original[1].accepted
        snapshot = manager.dispatch_snapshot(parent.id)
        assert snapshot["paused"] is True
        assert [item["request_id"] for item in snapshot["items"]] == [original[1].request_id]
        await delegations.confirm_recording()
        assert starts.empty()
    finally:
        release_archive.set()
        (tmp_path / "release").touch()
        await asyncio.gather(*(task for task in (archive, recording) if task is not None), return_exceptions=True)


async def test_archived_deletion_and_original_background_return_can_both_finish(work_runtime, monkeypatch, tmp_path):
    manager, _, parent, _ = work_runtime
    bg = manager.work_test_bg
    monkeypatch.setattr(bg_tasks, "bg_task_manager", bg)
    ready_to_return, retiring = asyncio.Event(), asyncio.Event()
    observed = []
    original_retire = bg.retire_session

    async def deliver(record):
        ready_to_return.set()
        await retiring.wait()
        try:
            result = await asyncio.wait_for(manager.deliver_bg_result(record), 1)
        except TimeoutError as error:
            observed.append(error)
            return ReturnRefused("source_withdrawn")
        observed.append(result)
        return result

    async def retire(session_id):
        retiring.set()
        await original_retire(session_id)

    bg.bind(manager.db, deliver, manager._broadcast)
    monkeypatch.setattr(bg, "retire_session", retire)
    try:
        await bg.start_task(session_id=parent.id, command="true", working_dir=str(tmp_path))
        await asyncio.wait_for(ready_to_return.wait(), 3)
        await manager.archive_session(parent.id, replace=False)
        assert await asyncio.wait_for(manager.delete_session(parent.id), 3)
        assert len(observed) == 1 and isinstance(observed[0], ReturnRefused), observed
        assert observed[0].reason in {"admission_closed", "destination_unavailable"}
        assert manager.get_session(parent.id) is None
        assert not await manager.delete_session(parent.id)
    finally:
        retiring.set()


@pytest.mark.parametrize("acknowledgment", ["accepted", "unknown"])
async def test_parent_deletion_settles_the_original_delegation_return_before_deleting(
    work_runtime, monkeypatch, tmp_path, acknowledgment,
):
    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr(delegation_module, "delegation_manager", delegations)
    db = manager.db
    entered, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    submit, admit, delete = manager.start_message, manager.admit_return, db.delete_session
    retire = delegations.retire_session
    attempts = []

    async def observe_submit(session_id, prompt, **kwargs):
        if session_id == parent.id and kwargs.get("delivery_id", "").startswith("agent-terminal:"):
            attempts.append(kwargs["delivery_id"])
            if acknowledgment == "unknown":
                raise ConnectionError("Lost original handoff confirmation")
        return await submit(session_id, prompt, **kwargs)

    async def hold_return(target, **kwargs):
        result = await admit(target, **kwargs)
        if target.session_id == parent.id:
            entered.set()
            await release.wait()
        return result

    async def refuse_delete(session_id):
        assert session_id == parent.id
        stored = await db.load_delegation_state(rec.delegation_id, parent_session_id=parent.id, user_id=parent.user_id)
        assert stored is not None
        assert json.loads(stored)["terminal_disposition"] == acknowledgment
        raise OSError("Transient deletion write failure")

    async def observe_retirement(session_id, *, permanent=False):
        if session_id == parent.id and permanent:
            retiring.set()
        await retire(session_id, permanent=permanent)

    monkeypatch.setattr(manager, "start_message", observe_submit)
    monkeypatch.setattr(manager, "admit_return", hold_return)
    monkeypatch.setattr(db, "delete_session", refuse_delete)
    monkeypatch.setattr(delegations, "retire_session", observe_retirement)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="controlled deletion race")
    await asyncio.wait_for(starts.get(), 3)
    deleting = None
    try:
        (tmp_path / "release").touch()
        await asyncio.wait_for(entered.wait(), 3)
        deleting = asyncio.create_task(manager.delete_session(parent.id))
        await asyncio.wait_for(retiring.wait(), 3)
        _, pending = await asyncio.wait({deleting}, timeout=0.1)
        assert pending, "Deletion must wait for the original delegation return"
        assert await db.get_session_binding(parent.id) is not None
        release.set()
        with pytest.raises(OSError, match="Transient deletion write failure"):
            await asyncio.wait_for(deleting, 3)
        assert rec.state == "completed"
        assert rec.to_public_dict()["terminal_disposition"] == acknowledgment
        assert attempts == [f"agent-terminal:{rec.work_id}"]
        original = await manager.source_receipt("delegation", attempts[0], session_id=parent.id)
        if acknowledgment == "accepted":
            assert original is not None and original[1].accepted
        else:
            assert original is None
        with pytest.raises(SessionAdmissionClosed):
            await manager.unarchive_session(parent.id)
        monkeypatch.setattr(db, "delete_session", delete)
        assert await asyncio.wait_for(manager.delete_session(parent.id), 3)
        assert rec.to_public_dict()["terminal_disposition"] == acknowledgment
        assert len(attempts) == 1
    finally:
        release.set()
        if deleting is not None:
            await asyncio.gather(deleting, return_exceptions=True)


async def test_delegation_delivery_subscriber_can_delete_the_parent(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr(delegation_module, "delegation_manager", delegations)
    completed = asyncio.get_running_loop().create_future()

    async def subscriber(event):
        if event["type"] == "delegation_delivery" and event["session_id"] == parent.id:
            try:
                completed.set_result(await asyncio.wait_for(manager.delete_session(parent.id), 3))
            except Exception as error:
                completed.set_exception(error)

    manager.on_broadcast("delete from delegation delivery", subscriber)
    try:
        rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Review and reply")
        await asyncio.wait_for(starts.get(), 3)
        assert await asyncio.wait_for(completed, 4)
        assert rec.state == "completed" and rec.to_public_dict()["terminal_disposition"] == "accepted"
        assert await manager.db.get_session_binding(parent.id) is None
    finally:
        manager.remove_broadcast("delete from delegation delivery")
        await delegations.confirm_recording()

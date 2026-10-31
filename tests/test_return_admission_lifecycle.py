"""Independent returns preserve the original lifecycle's wait-and-restore contract."""

import asyncio

from tests.test_delegation_work import work_runtime as work_runtime


async def test_delegation_terminal_waits_for_real_parent_archive_and_admits_once(work_runtime, monkeypatch, tmp_path):
    manager, delegations, parent, starts = work_runtime
    retiring, release_archive, returning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retire = manager._retire_application_history
    resolve = manager.resolve_task_return
    archive = recording = None

    async def hold_retirement(session):
        if session.id == parent.id:
            retiring.set()
            await release_archive.wait()
        await retire(session)

    async def observe_return(*args, **kwargs):
        if kwargs.get("session_id") == parent.id:
            returning.set()
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(manager, "_retire_application_history", hold_retirement)
    monkeypatch.setattr(manager, "resolve_task_return", observe_return)
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

"""Failed writers hand original resources and lifecycle results to Manager."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from server.sessions.owner import SessionOwnerUnavailable
from tests import test_session_fork_installation as fork_tests
from tests import test_session_owner_rewind_retirement as rewind_tests
from tests.accounts import create_account

installation = fork_tests.installation
held_rewind = rewind_tests.held_rewind


@pytest.mark.parametrize("held_rewind", [1.0], indirect=True)
@pytest.mark.parametrize("fail_during_close", [False, True], ids=["failed-before-close", "failed-during-close"])
async def test_work_cancellation_joins_the_failed_writers_original_process(
    held_rewind, monkeypatch, fail_during_close,
):
    env = held_rewind
    read = env.database.has_delivery_notice
    read_entered, release_failure = asyncio.Event(), asyncio.Event()

    async def fail_after_original_read(session_id, delivery_id):
        await read(session_id, delivery_id)
        read_entered.set()
        await release_failure.wait()
        raise RuntimeError("Original writer failed before its held process retired")

    monkeypatch.setattr(env.database, "has_delivery_notice", fail_after_original_read)
    failing = asyncio.create_task(env.manager.retain_result_notice(env.parent.id, "fail-work-owner", "Unconfirmed"))
    closing = finishing = None
    try:
        await asyncio.wait_for(read_entered.wait(), 2)
        if fail_during_close:
            closing = asyncio.create_task(env.manager.close_work(f"original-work-{env.parent.id}"))
            done, _ = await asyncio.wait({closing}, timeout=.05)
            assert not done, "The original writer must still own its suspended read"
        release_failure.set()
        with pytest.raises(SessionOwnerUnavailable):
            await asyncio.wait_for(failing, 2)
        assert env.processes[0].returncode is None
        assert Path(f"/proc/{env.identity['pid']}").exists()
        original = (await asyncio.wait_for(closing, 2) if closing is not None
                    else await env.manager.close_work(f"original-work-{env.parent.id}"))
        finishing = asyncio.create_task(env.manager.finish_work_cancellation([original]))
        done, _ = await asyncio.wait({finishing}, timeout=.1)
        assert not done, "Work cancellation returned while its failed writer's original PID was live"
        assert env.processes[0].returncode is None
        await asyncio.wait_for(finishing, 3)
        assert env.processes[0].returncode == 0
        assert not Path(f"/proc/{env.identity['pid']}").exists()
        assert all(task.done() for task in env.readers | env.stdout)
        assert len(env.processes) == 1
        terminal = await env.database.get_committed_terminal(env.parent.id, env.receipt.request_id)
        assert terminal["status"] == "success"
    finally:
        release_failure.set()
        await asyncio.gather(failing, *([closing] if closing is not None else []), return_exceptions=True)
        if finishing is not None:
            await asyncio.gather(finishing, return_exceptions=True)


@pytest.mark.parametrize("full_copy", [False, True], ids=["rewind", "duplicate"])
async def test_parent_writer_failure_during_preparation_preserves_the_ready_child(
    installation, monkeypatch, full_copy,
):
    manager, parent_id, client, directory = installation
    entered, release = asyncio.Event(), asyncio.Event()
    harness = get_harness("codex")
    preparation = "prepare_fork_copy" if full_copy else "prepare_fork"
    prepare = getattr(harness, preparation)
    read = manager.db.has_delivery_notice
    captured = []

    async def hold_original_artifact(*args, **kwargs):
        artifact = await prepare(*args, **kwargs)
        captured.extend(row["id"] for row in await manager.db.load_incomplete_forks())
        entered.set()
        await release.wait()
        return artifact

    async def fail_after_original_read(session_id, delivery_id):
        result = await read(session_id, delivery_id)
        if session_id == parent_id and delivery_id == "fail-parent-owner":
            raise RuntimeError("Original parent writer failed while its prepared fork was owned")
        return result

    monkeypatch.setattr(harness, preparation, hold_original_artifact)
    creating = asyncio.create_task(
        manager.duplicate_session(parent_id) if full_copy else manager.fork_session(parent_id, 0)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert len(captured) == 1
        monkeypatch.setattr(manager.db, "has_delivery_notice", fail_after_original_read)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(parent_id, "fail-parent-owner", "Unconfirmed")
        release.set()
        child = await asyncio.wait_for(creating, 3)
        assert child.id == captured[0] and child.fork_status == "ready"
        detail = await client.get(f"/api/sessions/{child.id}")
        assert detail.status_code == 200 and detail.json()["forked_from_session_id"] == parent_id
        parent = await client.get(f"/api/sessions/{parent_id}")
        assert parent.status_code == 200
        assert [row["content"] for row in parent.json()["messages"]] == ["Original request."]
        assert (directory / "original.txt").read_text() == "Source bytes remain unchanged.\n"
        if full_copy:
            assert (Path(child.working_dir) / "original.txt").read_text() == "Source bytes remain unchanged.\n"
        assert await manager.retain_result_notice(child.id, "child-still-available", "Ready child remains usable")
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)


async def test_failed_auto_archive_drains_original_publication_before_reporting_its_error(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "failed-auto-archive.sqlite3"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], origin="schedule")
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    update = database.update_session_field
    original_tasks = set()
    closing = None

    async def hold_original_notice(event):
        if event.get("session_id") == session.id and event.get("type") == "delivery_notice":
            entered.set()
            await release.wait()

    async def receive_original_notice(event):
        if event.get("session_id") == session.id and event.get("type") == "delivery_notice":
            received.set()

    async def refuse_before_archive_effect(session_id, **fields):
        if session_id == session.id and fields.get("archived"):
            row = await database.get_session_binding(session_id)
            assert row is not None
            raise ValueError("Original automatic archive was refused before its effect")
        await update(session_id, **fields)

    manager.on_broadcast("hold-auto-archive-prefix", hold_original_notice)
    manager.on_broadcast("receive-auto-archive-prefix", receive_original_notice)
    try:
        assert await manager.retain_result_notice(session.id, "kept-auto-archive-material", "Original committed material")
        await asyncio.wait_for(entered.wait(), 2)
        original_tasks = {
            task for task in asyncio.all_tasks()
            if task.get_name() in {f"session-owner-{session.id}", "session-publication"}
        }
        assert len(original_tasks) == 2
        monkeypatch.setattr(database, "update_session_field", refuse_before_archive_effect)
        with pytest.raises(ValueError, match="refused before its effect"):
            await asyncio.wait_for(manager.auto_archive_scheduled_session(session.id), 3)
        owner_task = next(task for task in original_tasks if task.get_name() != "session-publication")
        await asyncio.wait_for(asyncio.gather(owner_task, return_exceptions=True), 2)
        closing = asyncio.create_task(manager.close_session_owners())
        done, _ = await asyncio.wait({closing}, timeout=.1)
        assert not done, "The automatic archive error escaped before its original publisher drained"
        assert not received.is_set()
        release.set()
        with pytest.raises(ValueError, match="refused before its effect"):
            await asyncio.wait_for(closing, 2)
        assert received.is_set()
        assert all(task.done() for task in original_tasks)
        assert [row["content"] for row in await database.load_messages(session.id)] == ["Original committed material"]
        row = await database.get_session_binding(session.id)
        assert row is not None and not row["archived"]
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        # The original archive error remains observable to Manager and must
        # not be erased to make Database.close pass. Only the test's final
        # resource cleanup seals a publisher left behind by the red version.
        # This happens after every product assertion, never as its evidence.
        owner = manager._session_owners.get(id(session))
        if owner is not None:
            await asyncio.wait_for(owner.publisher.close(), 2)
        await asyncio.gather(*original_tasks, return_exceptions=True)
        await database.conn.close()


async def test_archive_release_hands_off_when_the_original_deadline_writer_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .2)
    database = Database(str(tmp_path / "archive-release-death.sqlite3"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], origin="schedule")
    question_published = asyncio.Event()
    deadline_entered, deadline_release, domain_refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    append, update = database.append_message, database.update_session_field
    archiving = None
    original_error = ValueError("Original archive refusal survives its writer's death")
    faulted = False

    async def question_observer(event):
        if event.get("session_id") == session.id and event.get("type") == "question_request":
            question_published.set()

    async def fail_deadline_after_real_read(*args, **fields):
        if fields.get("session_id") == session.id and fields.get("type") == "question_answer":
            row = await database.get_session_binding(session.id)
            assert row is not None
            deadline_entered.set()
            await deadline_release.wait()
            raise RuntimeError("Original deadline writer failed before writing its answer")
        return await append(*args, **fields)

    async def refuse_archive_after_the_deadline_started(session_id, **fields):
        nonlocal faulted
        if session_id == session.id and fields.get("archived") and not faulted:
            row = await database.get_session_binding(session_id)
            assert row is not None and not row["archived"]
            await asyncio.wait_for(deadline_entered.wait(), 2)
            faulted = True
            domain_refused.set()
            raise original_error
        return await update(session_id, **fields)

    manager.on_broadcast("observe-original-deadline-question", question_observer)
    try:
        assert await manager.retain_result_notice(session.id, "original-before-release", "Original committed material")
        question_id = await manager.create_pending_question(session.id, [{"question": "Original timed question"}])
        assert question_id is not None
        await asyncio.wait_for(question_published.wait(), 2)
        original_history = await database.load_messages(session.id)
        monkeypatch.setattr(database, "append_message", fail_deadline_after_real_read)
        monkeypatch.setattr(database, "update_session_field", refuse_archive_after_the_deadline_started)
        archiving = asyncio.create_task(manager.auto_archive_scheduled_session(session.id))
        await asyncio.wait_for(domain_refused.wait(), 3)
        assert not archiving.done(), "Archive release must still await the original live writer"
        deadline_release.set()
        with pytest.raises(ValueError, match="Original archive refusal") as failure:
            await asyncio.wait_for(archiving, 3)
        assert failure.value is original_error
        assert await database.load_messages(session.id) == original_history
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "failed-admission-stays-closed", "Never admitted")
        assert await manager.archive_session(session.id, replace=False) is None
        assert manager.get_session(session.id) is None
        detail = await manager.load_archived_session_detail(session.id)
        assert detail is not None
        assert [message.content for message in detail.messages] == [row["content"] for row in original_history]
    finally:
        deadline_release.set()
        if archiving is not None:
            await asyncio.gather(archiving, return_exceptions=True)
        owner = manager._session_owners.get(id(session))
        if owner is not None:
            await asyncio.wait_for(owner.publisher.close(), 2)
            await asyncio.gather(
                owner.task, *([owner.cleanup] if owner.cleanup is not None else []),
                return_exceptions=True,
            )
        await database.conn.close()

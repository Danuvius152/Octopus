"""Rewind retires its original material writer without losing its notices."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import get_session_manager
from server.routers.sessions import router
from server.sessions import SessionManager
from server.sessions.owner import SessionOwnerUnavailable
from tests.accounts import create_account


@pytest.fixture
async def rewind(tmp_path):
    database = Database(str(tmp_path / "rewind.sqlite3"))
    await database.initialize()
    _, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    parent = await manager.create_session(
        agent["id"], "Original parent", str(tmp_path), backend="codex",
    )
    await database.append_message(
        session_id=parent.id, seq=0, role="user", type="text",
        content="The original question remains in the archived parent.",
    )
    # Reopen the original persisted transcript through the public startup
    # boundary, so the test does not assign a live sequence or message count.
    await manager.initialize(database)
    parent = manager.get_session(parent.id)
    assert parent is not None
    source = tmp_path / "original.txt"
    source.write_text("Original file bytes survive the rewind.")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + bearer},
        ) as client:
            yield manager, parent, client, source
    finally:
        await database.close()


async def test_rewind_joins_original_notice_effects_and_retires_its_publisher(rewind, monkeypatch):
    manager, parent, client, source = rewind
    read, release_read, child_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    publishing, release_publication = asyncio.Event(), asyncio.Event()
    archiving, release_archive = asyncio.Event(), asyncio.Event()
    has_notice = manager.db.has_delivery_notice
    update = manager.db.update_session_field
    delivered = []

    async def hold_completed_original_read(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if session_id == parent.id and delivery_id == "original-material":
            read.set()
            # The real read transaction has ended. Rewind can do its own SQL,
            # but this original command still owns its subsequent append.
            await release_read.wait()
        return result

    async def observe_ready_child(session_id, **fields):
        if session_id == parent.id and fields.get("archived"):
            archiving.set()
            await release_archive.wait()
        await update(session_id, **fields)
        if session_id != parent.id and fields.get("fork_status") == "ready":
            child_ready.set()

    async def hold_original_publication(event):
        if event.get("type") == "delivery_notice" and event.get("session_id") == parent.id:
            publishing.set()
            await release_publication.wait()

    async def receive_original_publication(event):
        if event.get("type") == "delivery_notice" and event.get("session_id") == parent.id:
            delivered.append(event["content"])

    monkeypatch.setattr(manager.db, "has_delivery_notice", hold_completed_original_read)
    monkeypatch.setattr(manager.db, "update_session_field", observe_ready_child)
    manager.on_broadcast("hold-original-rewind-notice", hold_original_publication)
    manager.on_broadcast("receive-original-rewind-notice", receive_original_publication)
    notice = asyncio.create_task(manager.retain_result_notice(
        parent.id, "original-material", "The original retained material must be delivered exactly once.",
    ))
    rewinding = None
    try:
        await asyncio.wait_for(read.wait(), 3)
        # Capture original resource tasks, without creating substitutes or
        # invoking their private shutdown hooks. Retirement is checked before
        # the fixture's public database close can clean up leaked resources.
        owner_tasks = {task for task in asyncio.all_tasks()
                       if task.get_name() == f"session-owner-{parent.id}"}
        publisher_tasks = {task for task in asyncio.all_tasks()
                           if task.get_name() == "session-publication"}
        assert len(owner_tasks) == len(publisher_tasks) == 1
        original_tasks = owner_tasks | publisher_tasks
        rewinding = asyncio.create_task(client.post(
            f"/api/sessions/{parent.id}/fork", json={"rewind_to_msg_seq": 0},
        ))
        done, _ = await asyncio.wait({rewinding}, timeout=.1)
        assert not done, "Rewind retired its parent before the original notice command finished."
        live = await client.get(f"/api/sessions/{parent.id}")
        assert live.status_code == 200 and live.json()["archived"] is False
        assert [row["content"] for row in live.json()["messages"]] == [
            "The original question remains in the archived parent.",
        ]
        assert not archiving.is_set(), "Rewind crossed the original material append before its effect completed."

        release_read.set()
        assert await asyncio.wait_for(notice, 3) is True
        await asyncio.wait_for(publishing.wait(), 3)
        await asyncio.wait_for(child_ready.wait(), 3)
        await asyncio.wait_for(archiving.wait(), 3)
        with pytest.raises(SessionOwnerUnavailable, match="archive is in progress"):
            await manager.retain_result_notice(parent.id, "too-late", "This notice was never accepted.")
        live = await client.get(f"/api/sessions/{parent.id}")
        assert live.status_code == 200 and live.json()["archived"] is False
        assert live.json()["messages"][-1]["content"] == "The original retained material must be delivered exactly once."
        release_archive.set()
        reply = await asyncio.wait_for(rewinding, 3)
        assert reply.status_code == 201, reply.text
        assert reply.json()["forked_from_session_id"] == parent.id
        assert delivered == []
        assert all(not task.done() for task in original_tasks)
        assert await manager.retain_result_notice(parent.id, "after-rewind", "Never accepted after rewind.") is False

        archived = await client.get(f"/api/sessions/{parent.id}")
        assert archived.status_code == 200 and archived.json()["archived"] is True
        assert [row["content"] for row in archived.json()["messages"]] == [
            "The original question remains in the archived parent.",
            "The original retained material must be delivered exactly once.",
        ]
        assert source.read_text() == "Original file bytes survive the rewind."
        assert manager.get_session(parent.id) is None

        release_publication.set()
        done, pending = await asyncio.wait(original_tasks, timeout=3)
        assert not pending, "Archived parent retained its original owner or publisher task."
        assert all(not task.cancelled() and task.exception() is None for task in done)
        assert delivered == ["The original retained material must be delivered exactly once."]
        final = await client.get(f"/api/sessions/{parent.id}")
        assert final.status_code == 200 and final.json()["messages"] == archived.json()["messages"]
    finally:
        release_read.set()
        release_archive.set()
        release_publication.set()
        await asyncio.gather(*(task for task in (notice, rewinding) if task is not None), return_exceptions=True)
        await manager.close_session_owners()

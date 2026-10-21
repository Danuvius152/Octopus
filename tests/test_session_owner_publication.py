"""Committed material notices keep one ordered subscriber prefix."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.routers.sessions import router
from server.sessions import SessionManager


@pytest.fixture
async def notices(tmp_path):
    database = Database(str(tmp_path / "notices.sqlite3"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Material", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {settings.auth_token}"},
        ) as client:
            yield manager, session, client
    finally:
        await database.close()


async def test_reentrant_material_notice_finishes_original_subscriber_prefix(notices):
    manager, session, client = notices
    observed = []
    finished = asyncio.Event()

    async def submit_later_notice(event):
        if event.get("type") != "delivery_notice":
            return
        delivery_id = event["tool_input"]["delivery_id"]
        observed.append(("first", delivery_id))
        if delivery_id == "original":
            assert await manager.retain_result_notice(session.id, "later", "Later material")

    async def receive(event):
        if event.get("type") != "delivery_notice":
            return
        delivery_id = event["tool_input"]["delivery_id"]
        observed.append(("second", delivery_id))
        if delivery_id == "later":
            finished.set()

    manager.on_broadcast("submit-later", submit_later_notice)
    manager.on_broadcast("receive-notices", receive)
    assert await manager.retain_result_notice(session.id, "original", "Original material")
    await asyncio.wait_for(finished.wait(), 3)

    assert observed == [
        ("first", "original"), ("second", "original"),
        ("first", "later"), ("second", "later"),
    ]
    response = await client.get(f"/api/sessions/{session.id}")
    assert response.status_code == 200
    assert [message["content"] for message in response.json()["messages"]] == [
        "Original material", "Later material",
    ]


async def test_cancelled_notice_waiter_does_not_cancel_committed_publication(notices):
    manager, session, client = notices
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def hold_original_publication(event):
        if event.get("type") == "delivery_notice":
            entered.set()
            await release.wait()

    async def receive(event):
        if event.get("type") == "delivery_notice":
            observed.append(event["content"])
            received.set()

    manager.on_broadcast("hold-notice", hold_original_publication)
    manager.on_broadcast("receive-notice", receive)
    waiter = asyncio.create_task(
        manager.retain_result_notice(session.id, "kept", "Keep committed material"),
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        response = await client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200
        assert [message["content"] for message in response.json()["messages"]] == [
            "Keep committed material",
        ]
        release.set()
        await asyncio.wait_for(received.wait(), 3)
        assert observed == ["Keep committed material"]
        assert await manager.retain_result_notice(session.id, "kept", "No duplicate")
        assert observed == ["Keep committed material"]
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("pressure", ["bytes", "count"])
async def test_material_notice_capacity_refuses_before_commit_then_allows_retry(notices, pressure):
    manager, session, client = notices
    entered, release, drained, retried = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    expected_count = 1 if pressure == "bytes" else 256
    original = "L" * (1024 * 1024 + 32) if pressure == "bytes" else "Original material"
    delivered = []

    async def hold_first(event):
        if (event.get("type") == "delivery_notice"
                and event["tool_input"]["delivery_id"] == "pending-0"):
            entered.set()
            await release.wait()

    async def receive(event):
        if event.get("type") != "delivery_notice":
            return
        delivery_id = event["tool_input"]["delivery_id"]
        delivered.append(delivery_id)
        if delivery_id == "retry":
            retried.set()
        elif len(delivered) == expected_count:
            drained.set()

    manager.on_broadcast("hold-capacity", hold_first)
    manager.on_broadcast("receive-capacity", receive)
    try:
        # A single legal large notice may occupy an empty publisher alone.
        assert await manager.retain_result_notice(session.id, "pending-0", original)
        await asyncio.wait_for(entered.wait(), 3)
        for index in range(1, expected_count):
            assert await manager.retain_result_notice(session.id, f"pending-{index}", "Queued material")

        with pytest.raises(ValueError, match="notification capacity is full"):
            await manager.retain_result_notice(session.id, "retry", "Retry after capacity")
        response = await client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["message_count"] == expected_count
        assert all(message["tool_input"]["delivery_id"] != "retry" for message in detail["messages"])
        assert delivered == []

        release.set()
        await asyncio.wait_for(drained.wait(), 3)
        assert delivered == [f"pending-{index}" for index in range(expected_count)]
        assert await manager.retain_result_notice(session.id, "retry", "Retry after capacity")
        await asyncio.wait_for(retried.wait(), 3)
        assert delivered[-1] == "retry"
        response = await client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["message_count"] == expected_count + 1
        assert detail["messages"][-1]["content"] == "Retry after capacity"
    finally:
        release.set()

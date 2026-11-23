"""Notifier HTTP receipts belong to the original committed operation."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.main import app
from server.notifiers import NotifierEvent, NotifierManager, WebhookNotifier
from server.routers import notifiers
from server.users import UserManager

IDLE_EVENT = NotifierEvent(type="session_idle", title="Idle", message="Done")
TARGET_URL = "https://local.test/hook"
OTHER_URL = "https://local.test/other"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "main.db"))
    await db.initialize()
    users = UserManager(db)
    monkeypatch.setattr(deps, "_user_manager", users)
    owner = await users.create_user(username="notifier-owner", password="password1")
    token = await users.issue_token(owner["id"])
    await db.save_notifier(
        "target", "webhook", "Original", {"url": TARGET_URL},
        "2026-11-13", user_id=owner["id"],
    )
    manager = NotifierManager()
    manager.set_db(db)
    monkeypatch.setattr(notifiers, "_db", db)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c, db, manager, users, owner
    await db.close()


@pytest.mark.parametrize(
    ("stored_enabled", "enabled"),
    [(0, False), ("", False), (b"", False), (2, True)],
    ids=["zero", "empty-text", "empty-blob", "nonzero-integer"],
)
async def test_dispatch_respects_listed_enabled_status(client, monkeypatch, stored_enabled, enabled):
    c, db, manager, _users, owner = client
    sent = []

    async def record_delivery(self, event):
        sent.append(self.id)

    monkeypatch.setattr(WebhookNotifier, "send", record_delivery)
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE notifiers SET enabled = ? WHERE id = ?", (stored_enabled, "target"))
    response = await c.get("/api/notifiers")
    assert response.status_code == 200
    assert response.json()[0]["enabled"] is enabled
    await manager.fire(IDLE_EVENT, owner["id"])
    assert sent == (["target"] if enabled else [])


async def test_cancelled_patch_waiter_leaves_committed_target_revoked(client, monkeypatch):
    c, db, manager, _users, owner = client
    committed = asyncio.Event()
    hold_receipt = asyncio.Event()
    update = db.update_notifier
    sent = []

    async def held_update(*args, **kwargs):
        receipt = await update(*args, **kwargs)
        committed.set()
        await hold_receipt.wait()
        return receipt

    async def record_delivery(self, event):
        sent.append(self.id)

    monkeypatch.setattr(db, "update_notifier", held_update)
    monkeypatch.setattr(WebhookNotifier, "send", record_delivery)
    patch = asyncio.create_task(c.patch("/api/notifiers/target", json={"enabled": False}))
    try:
        await asyncio.wait_for(committed.wait(), 5)
    finally:
        patch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await patch

    response = await c.get("/api/notifiers")
    assert response.status_code == 200
    assert response.json()[0]["enabled"] is False
    await manager.fire(IDLE_EVENT, owner["id"])
    assert sent == []


@pytest.mark.parametrize(
    ("method", "body", "status", "expected_urls"),
    [
        ("DELETE", None, 204, [OTHER_URL]),
        ("PATCH", {"enabled": False}, 200, [OTHER_URL]),
        ("PATCH", {"config": {"url": "https://local.test/new"}}, 200,
         ["https://local.test/new", OTHER_URL]),
    ],
    ids=["delete", "disable", "change-url"],
)
async def test_fire_queued_before_commit_selects_after_commit(
    client, monkeypatch, method, body, status, expected_urls,
):
    c, db, manager, users, owner = client
    other = await users.create_user(username="foreign-owner", password="password2")
    await db.save_notifier(
        "foreign", "webhook", "Foreign", {"url": "https://local.test/foreign"},
        "2026-11-13", user_id=other["id"],
    )
    created = await c.post("/api/notifiers", json={
        "type": "webhook", "label": "Other", "config": {"url": OTHER_URL},
    })
    assert created.status_code == 201
    assert created.json() in (await c.get("/api/notifiers")).json()

    writing, release_commit, entered_fire = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = db.conn.execute
    sent_urls = []

    async def hold_notifier_write_before_commit(sql, params=()):
        cursor = await execute(sql, params)
        if sql.startswith(("UPDATE notifiers ", "DELETE FROM notifiers ")):
            writing.set()
            await release_commit.wait()
        return cursor

    async def record_delivery(self, event):
        sent_urls.append(self.config["url"])

    async def fire():
        entered_fire.set()
        await manager.fire(IDLE_EVENT, owner["id"])

    monkeypatch.setattr(db.conn, "execute", hold_notifier_write_before_commit)
    monkeypatch.setattr(WebhookNotifier, "send", record_delivery)
    async with asyncio.TaskGroup() as tasks:
        write = tasks.create_task(c.request(method, "/api/notifiers/target", json=body))
        try:
            await asyncio.wait_for(writing.wait(), 5)
            dispatch = tasks.create_task(fire())
            await asyncio.wait_for(entered_fire.wait(), 5)
            assert not dispatch.done()
        finally:
            release_commit.set()
    assert write.result().status_code == status, write.result().text
    assert sorted(sent_urls) == sorted(expected_urls)


async def test_selected_delivery_can_finish_after_the_target_is_deleted(client, monkeypatch):
    c, _db, manager, _users, owner = client
    sending, release_delivery = asyncio.Event(), asyncio.Event()
    delivered = []

    async def hold_delivery(self, event):
        sending.set()
        await release_delivery.wait()
        delivered.append(self.config["url"])

    monkeypatch.setattr(WebhookNotifier, "send", hold_delivery)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(manager.fire(IDLE_EVENT, owner["id"]))
        try:
            await asyncio.wait_for(sending.wait(), 5)
            deleted = await asyncio.wait_for(c.delete("/api/notifiers/target"), 2)
            assert deleted.status_code == 204
            assert delivered == []
        finally:
            release_delivery.set()
    await manager.fire(IDLE_EVENT, owner["id"])
    assert delivered == [TARGET_URL]


async def test_next_event_recovers_after_invalid_target_data(client, monkeypatch, caplog):
    c, db, manager, _users, owner = client
    sent = []

    async def record_delivery(self, event):
        sent.append(self.config["url"])

    monkeypatch.setattr(WebhookNotifier, "send", record_delivery)
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE notifiers SET config = ? WHERE id = ?", ("sensitive-config", "target"))
    await manager.fire(IDLE_EVENT, owner["id"])
    assert sent == []
    assert "Notifier target selection failed for event=session_idle (JSONDecodeError)" in caplog.text
    assert "sensitive-config" not in caplog.text

    response = await c.patch("/api/notifiers/target", json={"config": {"url": OTHER_URL}})
    assert response.status_code == 200, response.text
    await manager.fire(IDLE_EVENT, owner["id"])
    assert sent == [OTHER_URL]


async def test_cancelled_selection_propagates_before_decoding_invalid_target(client, monkeypatch):
    _c, db, manager, _users, owner = client
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE notifiers SET config = ? WHERE id = ?", ("invalid", "target"))
    selected, release_read = asyncio.Event(), asyncio.Event()
    execute = db.conn.execute

    async def hold_driver_read(sql, params=()):
        cursor = await execute(sql, params)
        if "FROM notifiers" in sql:
            selected.set()
            await release_read.wait()
        return cursor

    monkeypatch.setattr(db.conn, "execute", hold_driver_read)
    dispatch = asyncio.create_task(manager.fire(IDLE_EVENT, owner["id"]))
    try:
        await asyncio.wait_for(selected.wait(), 5)
    finally:
        dispatch.cancel()
        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await dispatch


async def test_patch_keeps_its_committed_receipt_after_delete(client, monkeypatch):
    c, db, _manager, _users, _owner = client
    committed, release = asyncio.Event(), asyncio.Event()
    update = db.update_notifier

    async def held_update(*args, **kwargs):
        receipt = await update(*args, **kwargs)
        committed.set()
        await release.wait()
        return receipt

    monkeypatch.setattr(db, "update_notifier", held_update)
    patch = asyncio.create_task(c.patch("/api/notifiers/target", json={"label": "Saved"}))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        deleted = await c.delete("/api/notifiers/target")
        assert deleted.status_code == 204
    finally:
        release.set()
    response = await patch
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": "target", "type": "webhook", "label": "Saved",
        "config": {"url": "https://local.test/hook"}, "enabled": True,
        "created_at": "2026-11-13",
    }
    assert (await c.get("/api/notifiers")).json() == []


async def test_delete_wins_before_patch(client):
    c, _db, _manager, _users, _owner = client
    assert (await c.delete("/api/notifiers/target")).status_code == 204
    assert (await c.delete("/api/notifiers/target")).status_code == 404
    for body in ({"label": "Too late"}, {}):
        response = await c.patch("/api/notifiers/target", json=body)
        assert response.status_code == 404
        assert response.json() == {"detail": "notifier not found"}


async def test_two_patches_return_their_own_committed_rows(client, monkeypatch):
    c, db, _manager, _users, _owner = client
    committed, release = asyncio.Event(), asyncio.Event()
    update = db.update_notifier

    async def held_first(*args, **kwargs):
        receipt = await update(*args, **kwargs)
        if kwargs.get("label") == "First":
            committed.set()
            await release.wait()
        return receipt

    monkeypatch.setattr(db, "update_notifier", held_first)
    first = asyncio.create_task(c.patch("/api/notifiers/target", json={"label": "First", "enabled": False}))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        second = await c.patch("/api/notifiers/target", json={"label": "Second", "config": {}})
    finally:
        release.set()
    original = await first
    assert original.status_code == second.status_code == 200
    assert original.json()["label"] == "First"
    assert original.json()["config"] == {"url": "https://local.test/hook"}
    assert original.json()["enabled"] is False
    assert second.json()["label"] == "Second"
    assert second.json()["config"] == {}
    assert second.json()["enabled"] is False
    assert (await c.get("/api/notifiers")).json() == [second.json()]


@pytest.mark.parametrize("body", [{}, {"label": None, "config": None, "enabled": None}])
async def test_empty_patch_only_reads_the_owned_row(client, body):
    c, db, _manager, _users, _owner = client
    statements: list[str] = []
    await db.conn.set_trace_callback(statements.append)
    try:
        response = await c.patch("/api/notifiers/target", json=body)
    finally:
        await db.conn.set_trace_callback(None)
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Original"
    target_sql = [sql for sql in statements if "notifiers" in sql.lower()]
    assert len(target_sql) == 1
    assert target_sql[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("body", [{}, {"label": "Stolen", "config": {}, "enabled": False}])
async def test_patch_never_updates_or_returns_another_accounts_target(client, body):
    c, _db, _manager, users, _owner = client
    other = await users.create_user(username="other-owner", password="password2")
    token = await users.issue_token(other["id"])
    response = await c.patch("/api/notifiers/target", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert response.status_code == 404
    original = (await c.get("/api/notifiers")).json()
    assert len(original) == 1 and original[0]["label"] == "Original"
    assert original[0]["config"] == {"url": "https://local.test/hook"}
    assert original[0]["enabled"] is True


async def test_delete_never_removes_another_accounts_target(client):
    c, _db, _manager, users, _owner = client
    foreign = await users.create_user(username="foreign", password="password2")
    token = await users.issue_token(foreign["id"])
    response = await c.delete("/api/notifiers/target", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
    assert [row["id"] for row in (await c.get("/api/notifiers")).json()] == ["target"]

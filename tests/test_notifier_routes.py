"""Notifier HTTP receipts belong to the original committed operation."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.main import app
from server.notifiers import NotifierManager
from server.routers import notifiers
from server.users import UserManager


@pytest.fixture
async def client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "main.db"))
    await db.initialize()
    users = UserManager(db)
    monkeypatch.setattr(deps, "_user_manager", users)
    owner = await users.create_user(username="notifier-owner", password="password1")
    token = await users.issue_token(owner["id"])
    await db.save_notifier(
        "target", "webhook", "Original", {"url": "https://local.test/hook"},
        "2026-11-13", user_id=owner["id"],
    )
    manager = NotifierManager()
    manager.set_db(db)
    await manager.load()
    monkeypatch.setattr(notifiers, "_db", db)
    monkeypatch.setattr(notifiers, "notifier_manager", manager)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c, db, manager, users, owner
    await db.close()


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


async def test_refresh_failure_reports_saved_without_replaying_the_update(client, monkeypatch, caplog):
    c, _db, manager, _users, _owner = client

    async def unavailable():
        raise RuntimeError("runtime refresh unavailable")

    monkeypatch.setattr(manager, "load", unavailable)
    response = await c.patch("/api/notifiers/target", json={"label": "Saved"})
    assert response.status_code == 503, response.text
    assert response.json() == {
        "code": "notifier_refresh_unconfirmed", "notifier_id": "target", "committed": True,
        "detail": "Notifier was saved, but runtime refresh is unconfirmed.",
    }
    assert (await c.get("/api/notifiers")).json()[0]["label"] == "Saved"
    assert manager.list()[0].label == "Original"
    assert "runtime refresh unavailable" in caplog.text


async def test_delete_wins_before_patch(client):
    c, _db, _manager, _users, _owner = client
    assert (await c.delete("/api/notifiers/target")).status_code == 204
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
async def test_empty_patch_only_reads_the_owned_row_without_runtime_reload(client, monkeypatch, body):
    c, db, manager, _users, _owner = client
    statements: list[str] = []

    async def unexpected_reload():
        raise AssertionError("empty PATCH must not reload runtime")

    monkeypatch.setattr(manager, "load", unexpected_reload)
    await db.conn.set_trace_callback(statements.append)
    try:
        response = await c.patch("/api/notifiers/target", json=body)
    finally:
        await db.conn.set_trace_callback(None)
    assert response.status_code == 200, response.text
    assert response.json()["label"] == manager.list()[0].label == "Original"
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

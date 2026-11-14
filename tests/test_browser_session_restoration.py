"""Browser restore identities at the real HTTP and session lifecycle boundary."""

import asyncio
import gc
import uuid
import weakref

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from server import deps
from server.auth import verify_token
from server.database import Database
from server.deps import AuthenticatedAccount, get_session_manager
from server.routers.sessions import router
from server.session_manager import SessionManager
from server.users import UserManager


@pytest.fixture
async def restoration():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="restore-owner", password="restore-password")
    token = await users.issue_token(user["id"])
    deps.set_user_manager(users)
    manager = SessionManager()
    await manager.initialize(db)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as client:
        agent = await db.get_system_agent(user["id"])
        session = await manager.create_session(agent["id"], "Restore target")
        await manager.archive_session(session.id, replace=False)
        yield client, manager, users, user["id"], session.id
    await db.close()
    deps.set_user_manager(None)


async def test_seal_before_a_delayed_restore_permanently_closes_that_identity(restoration):
    client, _manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    initial = await client.get(path + "/restoration")
    assert initial.status_code == 200
    assert initial.json()["state"] == "idle"
    identity = {"operation_id": uuid.uuid4().hex, "service_generation": initial.json()["service_generation"]}
    missing = await client.get(path + "/restoration", params=identity)
    assert missing.json()["state"] == "not_registered"
    sealed = await client.post(path + "/restoration/seal", json=identity)
    assert sealed.json()["state"] == "sealed"
    late = await client.post(path + "/unarchive", json=identity)
    assert late.status_code == 409
    assert late.json()["state"] == "sealed"
    assert (await client.get(path)).json()["archived"] is True
    assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "sealed"


async def test_registered_restore_survives_its_http_waiter_and_seal_only_observes_the_original(restoration):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def observe(event):
        if event.get("type") == "session_unarchived":
            effects.append(event["session_id"])
            entered.set()
            await release.wait()

    manager.on_broadcast("hold-restoration", observe)
    original = asyncio.create_task(client.post(path + "/unarchive", json=identity))
    duplicate = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        original.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original
        assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "running"
        assert (await client.post(path + "/restoration/seal", json=identity)).json()["state"] == "running"
        assert (await client.get(path + "/restoration")).json()["operation_id"] == identity["operation_id"]
        duplicate = asyncio.create_task(client.post(path + "/unarchive", json=identity))
        other = await client.post(path + "/unarchive", json={**identity, "operation_id": uuid.uuid4().hex})
        assert other.json()["state"] == "finished" and other.json()["status_code"] == 409
        assert not duplicate.done()
    finally:
        release.set()
        if duplicate is not None:
            assert (await duplicate).json()["state"] == "finished"
        manager.remove_broadcast("hold-restoration")
    assert effects == [sid]
    assert (await client.get(path)).json()["archived"] is False
    await manager.archive_session(sid, replace=False)
    # An old duplicate is historical proof, even after a later archive.
    assert (await client.post(path + "/unarchive", json=identity)).json()["state"] == "finished"
    assert (await client.get(path)).json()["archived"] is True


async def test_shutdown_after_the_last_target_read_refuses_new_browser_registration(restoration, monkeypatch):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    entered, release = asyncio.Event(), asyncio.Event()
    execute = manager.db.conn.execute

    async def held_execute(sql, *args, **kwargs):
        if str(sql).startswith("SELECT id, agent_id, user_id, archived FROM sessions"):
            entered.set()
            await release.wait()
        return await execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(manager.db.conn, "execute", held_execute)
        pending = asyncio.create_task(client.post(path + "/unarchive", json=identity))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            manager.close_admission()
            await manager.drain_turns()
        finally:
            release.set()
        refused = await pending
    assert refused.status_code == 503
    assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "not_registered"
    assert (await client.post(path + "/restoration/seal", json=identity)).json()["state"] == "sealed"
    assert (await client.get(path)).json()["archived"] is True


async def test_registered_commit_blocks_database_close_until_the_original_effect_finishes(restoration, monkeypatch):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = manager.db.conn.execute, manager.db.conn.commit
    restoring = False

    async def marked_execute(sql, *args, **kwargs):
        nonlocal restoring
        if str(sql).startswith("UPDATE sessions SET archived"):
            restoring = True
        return await execute(sql, *args, **kwargs)

    async def held_commit():
        if restoring:
            entered.set()
            await release.wait()
        return await commit()

    closing = None
    with monkeypatch.context() as patch:
        patch.setattr(manager.db.conn, "execute", marked_execute)
        patch.setattr(manager.db.conn, "commit", held_commit)
        pending = asyncio.create_task(client.post(path + "/unarchive", json=identity))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            closing = asyncio.create_task(manager.db.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 3)


@pytest.mark.parametrize("managed", [False, True])
async def test_new_service_never_replays_an_old_generation_and_only_managed_proof_closes_it(restoration, managed):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    await manager.initialize(manager.db, previous_writers_retired=managed)
    proof = await client.post(path + "/restoration/seal", json=identity)
    assert proof.json()["state"] == ("generation_retired" if managed else "generation_unknown")
    assert (await client.post(path + "/unarchive", json=identity)).status_code == 409
    assert (await client.get(path)).json()["archived"] is True


async def test_current_token_seals_a_previous_tokens_authenticated_but_not_registered_write(restoration):
    client, _manager, users, user_id, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    old_bearer = client.headers["Authorization"]
    new_token = await users.issue_token(user_id)  # Issuing T2 does not revoke T1.
    new_headers = {"Authorization": f"Bearer {new_token}"}
    entered, release = asyncio.Event(), asyncio.Event()
    # Keep real authentication and its original resolved account. The second
    # auth dependency provides the HTTP scheduling barrier after that proof.
    async def held_auth(request: Request, authentication: AuthenticatedAccount):
        if request.method == "POST" and request.url.path.endswith("/unarchive"):
            assert authentication.user["id"] == user_id
            entered.set()
            await release.wait()
        return request.headers["Authorization"]

    app = client._transport.app
    app.dependency_overrides[verify_token] = held_auth
    pending = asyncio.create_task(client.post(path + "/unarchive", json=identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.get(path + "/restoration", params=identity, headers=new_headers)).json()["state"] == "not_registered"
        await users.revoke_token(old_bearer.removeprefix("Bearer "))
        sealed = await client.post(path + "/restoration/seal", json=identity, headers=new_headers)
        assert sealed.json()["state"] == "sealed"
    finally:
        release.set()
    assert (await pending).status_code == 409
    assert (await client.get(path, headers=new_headers)).json()["archived"] is True


async def test_confirmed_deletion_refuses_new_or_delayed_restore(restoration):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    assert await manager.delete_session(sid)
    assert (await client.post(path + "/unarchive", json=identity)).status_code == 404
    assert (await client.get(path + "/restoration")).status_code == 404
    assert manager.list_sessions() == []


async def test_missing_identity_and_foreign_target_cannot_bypass_restore_admission(restoration):
    client, _manager, users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    assert (await client.post(path + "/unarchive")).status_code == 422
    user = await users.create_user(username="other-restore", password="other-password")
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    identity = {"operation_id": uuid.uuid4().hex, "service_generation": "unknown"}
    assert (await client.post(path + "/unarchive", json=identity, headers=headers)).status_code == 404
    assert (await client.post(path + "/restoration/seal", json=identity, headers=headers)).status_code == 404


async def test_initialization_joins_original_restoration_before_replacing_its_owners_and_generation(restoration):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") == "session_unarchived":
            entered.set()
            await release.wait()

    manager.on_broadcast("hold-initialize", observe)
    pending = asyncio.create_task(client.post(path + "/unarchive", json=identity))
    initializing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        initializing = asyncio.create_task(manager.initialize(manager.db))
        await asyncio.sleep(0)
        assert not initializing.done()
        assert manager.get_session(sid) is not None
        assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "running"
    finally:
        release.set()
        result = await pending
        if initializing is not None:
            await initializing
        manager.remove_broadcast("hold-initialize")
    assert result.json()["state"] == "finished"
    assert result.json()["service_generation"] == identity["service_generation"]
    assert (await client.get(path)).json()["archived"] is False


async def test_finished_identity_keeps_proof_without_retaining_the_restored_session(restoration):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    assert (await client.post(path + "/unarchive", json=identity)).json()["state"] == "finished"
    restored = weakref.ref(manager.get_session(sid))
    await manager.archive_session(sid, replace=False)
    await manager.close_session_owners()
    await asyncio.sleep(0)
    gc.collect()
    assert restored() is None
    assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "finished"
    assert (await client.get(path)).json()["archived"] is True


async def test_deletion_does_not_finish_an_original_restore_that_is_still_publishing(restoration):
    client, manager, _users, _user, sid = restoration
    path = f"/api/sessions/{sid}"
    identity = {"operation_id": uuid.uuid4().hex,
                "service_generation": (await client.get(path + "/restoration")).json()["service_generation"]}
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") == "session_unarchived":
            entered.set()
            await release.wait()

    manager.on_broadcast("hold-delete", observe)
    pending = asyncio.create_task(client.post(path + "/unarchive", json=identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await manager.delete_session(sid)
        assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "running"
        assert (await client.post(path + "/restoration/seal", json=identity)).json()["state"] == "running"
        fresh = {**identity, "operation_id": uuid.uuid4().hex}
        assert (await client.post(path + "/unarchive", json=fresh)).status_code == 404
    finally:
        release.set()
        assert (await pending).json()["state"] == "finished"
        manager.remove_broadcast("hold-delete")
    assert (await client.get(path + "/restoration", params=identity)).json()["state"] == "finished"
    assert (await client.get(path)).status_code == 404

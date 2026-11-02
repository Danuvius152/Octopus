"""OAuth completion owns provider work and the actual credential commit."""

import asyncio
import json

import pytest

from server import deps, oauth_login, oauth_providers
from server.crypto import decrypt
from server.oauth_errors import ScopeMissingError
from server.oauth_providers import OAuthTokenSet
from tests.oauth_provider import patch_provider
from tests.test_credentials_api import client as client


@pytest.fixture
async def oauth_runtime(client, monkeypatch):
    manager = oauth_login.OAuthLoginManager()
    monkeypatch.setattr(oauth_login, "oauth_login_manager", manager)
    try:
        yield *client, manager
    finally:
        await manager.shutdown()


async def start(connection, **extra):
    response = await connection.post("/api/credentials/oauth/start", json={})
    assert response.status_code == 201, response.text
    return {"login_id": response.json()["login_id"], "code": "original-code", "label": "Personal", **extra}


async def test_lost_http_waiter_can_wait_again_for_one_complete_bundle_commit(oauth_runtime, monkeypatch):
    connection, database, manager = oauth_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def exchange(**kwargs):
        calls.append("exchange")
        entered.set()
        await release.wait()
        return OAuthTokenSet("access", "refresh", 2000000000, ["user:inference"])

    async def mint(_):
        calls.append("mint")
        raise ScopeMissingError("no organization", missing_scope="org:create_api_key")

    provider = oauth_providers.get_provider("claude-code")
    monkeypatch.setattr(provider, "exchange_code", exchange)
    monkeypatch.setattr(provider, "mint_api_key", mint)
    request = await start(connection)
    waiter = asyncio.create_task(connection.post("/api/credentials/oauth/complete", json=request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert manager.drain_blockers()
        release.set()
        response = await connection.post("/api/credentials/oauth/complete", json=request)
        assert response.status_code == 201, response.text
        assert calls == ["exchange", "mint"]
        rows = await database.load_credentials()
        assert len(rows) == 1 and rows[0]["id"] == response.json()["id"]
        assert rows[0]["token_expires_at"] == "2033-05-18T03:33:20+00:00"
        key = await deps.get_user_manager().data_key_for(rows[0]["user_id"])
        assert json.loads(decrypt(rows[0]["secret_encrypted"], key))["refresh_token"] == "refresh"
        assert not manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("stage", ["exchange", "mint"])
async def test_explicit_cancel_revokes_next_stage_but_waits_for_provider_retirement(
    oauth_runtime, monkeypatch, stage
):
    connection, database, manager = oauth_runtime
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def pause():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()

    async def exchange(**kwargs):
        calls.append("exchange")
        if stage == "exchange":
            await pause()
        return OAuthTokenSet("access", "refresh", 2000000000, [])

    async def mint(_):
        calls.append("mint")
        if stage == "mint":
            await pause()
        return "new-key"

    provider = oauth_providers.get_provider("claude-code")
    monkeypatch.setattr(provider, "exchange_code", exchange)
    monkeypatch.setattr(provider, "mint_api_key", mint)
    request = await start(connection)
    waiter = asyncio.create_task(connection.post("/api/credentials/oauth/complete", json=request))
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cancelling = asyncio.create_task(connection.post("/api/credentials/oauth/cancel", json={"login_id": request["login_id"]}))
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not cancelling.done()
        assert manager.drain_blockers()
        session = manager.get(request["login_id"])
        session._created_at -= oauth_login._LOGIN_TTL_SECONDS + 1
        manager._gc()
        assert manager.get(session.id) is session
        release.set()
        cancelled, completed = await asyncio.gather(cancelling, waiter)
        assert cancelled.status_code == 200 and cancelled.json() == {"status": "cancelled"}
        assert completed.status_code == 409
        assert calls == (["exchange"] if stage == "exchange" else ["exchange", "mint"])
        assert await database.load_credentials() == []
        assert not manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(waiter, *([cancelling] if cancelling else []), return_exceptions=True)


async def test_committing_login_survives_cancelled_complete_and_cancel_waiters(oauth_runtime, monkeypatch):
    connection, database, manager = oauth_runtime
    patch_provider(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    save = database.save_credential
    commits = []

    async def paused(*args, **kwargs):
        commits.append(args[0])
        entered.set()
        await release.wait()
        await save(*args, **kwargs)

    monkeypatch.setattr(database, "save_credential", paused)
    request = await start(connection)
    waiter = asyncio.create_task(connection.post("/api/credentials/oauth/complete", json=request))
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.get(request["login_id"]).state == oauth_login.LoginState.committing
        assert await database.load_credentials() == []
        cancelling = asyncio.create_task(manager.cancel(request["login_id"]))
        await asyncio.sleep(0)
        assert not cancelling.done()
        waiter.cancel()
        cancelling.cancel()
        for task in (waiter, cancelling):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert manager.drain_blockers()
        release.set()
        completed = await connection.post("/api/credentials/oauth/complete", json=request)
        cancelled = await connection.post("/api/credentials/oauth/cancel", json={"login_id": request["login_id"]})
        assert completed.status_code == 201, completed.text
        assert cancelled.json() == {"status": "already_completed"}
        assert len(commits) == len(await database.load_credentials()) == 1
        assert not manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(waiter, *([cancelling] if cancelling else []), return_exceptions=True)


async def test_completed_result_is_immutable_after_credential_deletion(oauth_runtime, monkeypatch):
    connection, database, _ = oauth_runtime
    provider = patch_provider(monkeypatch)
    from unittest.mock import AsyncMock

    exchange = AsyncMock(wraps=provider.exchange_code)
    monkeypatch.setattr(provider, "exchange_code", exchange)
    request = await start(connection)
    original = await connection.post("/api/credentials/oauth/complete", json=request)
    assert original.status_code == 201, original.text
    identity = original.json()["id"]
    await connection.delete(f"/api/credentials/{identity}")
    repeated = await connection.post("/api/credentials/oauth/complete", json=request)
    assert repeated.json() == original.json()
    assert exchange.await_count == 1
    assert await database.load_credentials() == []
    for changed in ({"code": "other-code"}, {"label": "other-label"}, {"credential_id": "other-target"}):
        refused = await connection.post("/api/credentials/oauth/complete", json={**request, **changed})
        assert refused.status_code == 409


async def test_cancel_timeout_keeps_real_blocker_and_shutdown_waits_for_same_task(oauth_runtime, monkeypatch):
    connection, database, manager = oauth_runtime
    provider = patch_provider(monkeypatch)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def exchange(**kwargs):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
        return OAuthTokenSet("access", "refresh", 2000000000, [])

    monkeypatch.setattr(provider, "exchange_code", exchange)
    monkeypatch.setattr(oauth_login, "_CANCEL_TIMEOUT_SECONDS", .02)
    request = await start(connection)
    waiter = asyncio.create_task(connection.post("/api/credentials/oauth/complete", json=request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cancelled = await connection.post("/api/credentials/oauth/cancel", json={"login_id": request["login_id"]})
        assert cancelled.status_code == 503
        assert cleaning.is_set() and manager.drain_blockers()
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await manager.shutdown()
        assert manager.get(request["login_id"]) is not None and manager.drain_blockers()
        with pytest.raises(RuntimeError, match="admission is closed"):
            await manager.start()
        release.set()
        assert (await waiter).status_code == 409
        await manager.shutdown()
        assert not manager.drain_blockers()
        assert await database.load_credentials() == []
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)

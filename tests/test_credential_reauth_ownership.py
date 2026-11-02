"""Reauthorization validates both the login owner and the mutable target."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from server import deps, oauth_login
from server.crypto import decrypt, encrypt
from server.oauth_providers import OAuthTokenSet
from server.users import UserManager
from tests.oauth_provider import patch_provider
from tests.test_credentials_api import client as client


@pytest.fixture
async def accounts(client, monkeypatch):
    connection, db = client
    users = UserManager(db)
    monkeypatch.setattr(deps, "_user_manager", users)
    actor = await users.create_user(username="actor", password="actor-password")
    other = await users.create_user(username="other", password="other-password")
    bearer = await users.issue_token(actor["id"])
    login = await oauth_login.oauth_login_manager.start(user_id=actor["id"])
    for user in (actor, other):
        await db.save_credential(user["id"], "claude-code", "original", "oauth",
                                 encrypt("old-secret", users.data_key(user)), "now", user_id=user["id"])
    yield connection, db, users, actor, other, {"Authorization": f"Bearer {bearer}"}, login.id


async def complete(connection, headers, target, login_id):
    return await connection.post("/api/credentials/oauth/complete", headers=headers, json={
        "login_id": login_id, "code": "synthetic-code", "label": "new label", "credential_id": target,
    })


@pytest.mark.parametrize("target", ["other", "missing"])
async def test_wrong_target_is_rejected_before_consuming_login(accounts, monkeypatch, target):
    connection, db, users, actor, other, headers, login_id = accounts
    before = await db.get_credential(other["id"])
    submit = AsyncMock(side_effect=AssertionError("must reject before exchange"))
    provider = patch_provider(monkeypatch)
    monkeypatch.setattr(provider, "exchange_code", submit)
    response = await complete(connection, headers, other["id"] if target == "other" else "missing", login_id)
    assert response.status_code == 404
    submit.assert_not_awaited()
    assert await db.get_credential(other["id"]) == before


@pytest.mark.parametrize("change", ["delete", "owner"])
async def test_target_changed_during_exchange_is_not_written(accounts, monkeypatch, change):
    connection, db, users, actor, other, headers, login_id = accounts
    changed = None

    async def exchange(**kwargs):
        nonlocal changed
        if change == "delete":
            await db.delete_credential(actor["id"])
        else:
            async with db.transaction() as tx:
                await tx.execute("UPDATE backend_credentials SET user_id=? WHERE id=?", (other["id"], actor["id"]))
        changed = await db.get_credential(actor["id"])
        return OAuthTokenSet("access", "refresh", 2000000000, ["scope"])

    provider = patch_provider(monkeypatch, api_key="new-secret")
    monkeypatch.setattr(provider, "exchange_code", exchange)
    response = await complete(connection, headers, actor["id"], login_id)
    assert response.status_code == 404
    assert await db.get_credential(actor["id"]) == changed
    if change == "delete":
        cursor = await db.conn.execute("SELECT 1 FROM credential_secrets WHERE credential_id=?", (actor["id"],))
        assert await cursor.fetchone() is None


@pytest.mark.parametrize("bundle", [False, True])
async def test_owned_reauth_keeps_binding_and_decryptable_secret(accounts, monkeypatch, bundle):
    connection, db, users, actor, other, headers, login_id = accounts
    await db.update_credential(actor["id"], status="needs_reconnect", needs_reconnect=True,
                               last_refresh_error_code="invalid_grant")
    agent = await db.get_system_agent(actor["id"])
    await db.update_agent(agent["id"], credential_id=actor["id"])

    patch_provider(monkeypatch, api_key=None if bundle else "new-secret")
    response = await complete(connection, headers, actor["id"], login_id)
    assert response.status_code == 201, response.text
    assert response.json()["id"] == actor["id"]
    row = await db.get_credential(actor["id"], actor["id"])
    assert row["status"] == "active" and not row["needs_reconnect"]
    assert row["last_refresh_error_code"] is None
    secret = decrypt(row["secret_encrypted"], users.data_key(actor))
    assert "new-access" in secret if bundle else secret == "new-secret"
    assert (await db.get_agent(agent["id"]))["credential_id"] == actor["id"]
    # Historical completion remains readable after its mutable target is gone.
    await db.delete_credential(actor["id"])
    repeated = await complete(connection, headers, actor["id"], login_id)
    assert repeated.status_code == 201 and repeated.json() == response.json()
    assert await db.get_credential(actor["id"]) is None


async def test_reauth_rechecks_owner_after_waiting_for_the_storage_writer(accounts):
    connection, db, users, actor, other, headers, login_id = accounts
    before = await db.get_credential(actor["id"])
    async with db.transaction() as tx:
        updating = asyncio.create_task(db.reauthorize_credential(actor["id"], user_id=actor["id"],
            backend="claude-code", label="must not write", secret_encrypted="must not write"))
        await asyncio.sleep(0)
        assert not updating.done()
        await tx.execute("UPDATE backend_credentials SET user_id=? WHERE id=?", (other["id"], actor["id"]))
    assert await updating is None
    assert await db.get_credential(actor["id"]) == {**before, "user_id": other["id"]}
    assert await db.reauthorize_credential(other["id"], user_id=None, backend="claude-code", label="install") is None

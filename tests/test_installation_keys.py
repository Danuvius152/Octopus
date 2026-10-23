"""Legacy secret consumers retire before the installation key changes."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.bootstrap import bootstrap_first_account
from server.connector_manager import ConnectorManager
from server.connectors.registry import register
from server.crypto import decrypt
from server.installation_keys import InstallationKeyRetired
from server.oauth_providers import OAuthTokenSet
from server.session_manager import session_manager
from server.users import UserManager
from tests.test_bootstrap import legacy as legacy
from tests.test_connector_manager import FakeConnector, FakeProvider
from tests.test_connector_manager import clean_registry as clean_registry
from tests.test_service_shutdown import synthetic_cli


async def test_bootstrap_waits_for_a_captured_connector_refresh_before_adopting_its_secret(
    legacy, clean_registry, monkeypatch, tmp_path,
):
    database, agent = legacy
    synthetic_cli(monkeypatch, [], (
        "import json\nprint(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    ))
    session = await session_manager.create_session(agent["id"], "Admission during handoff", str(tmp_path), backend="codex")
    provider = FakeProvider()
    register(FakeConnector(provider))
    connectors = ConnectorManager(database)
    await connectors.set_client_creds("fakekind", "client", "secret")
    installation = await connectors.complete_install(
        kind="fakekind", token_set=OAuthTokenSet("old-access", "refresh", time.time() + 1),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(**_):
        entered.set()
        await release.wait()
        return OAuthTokenSet("new-access", "new-refresh", time.time() + 3600)

    monkeypatch.setattr(provider, "refresh", refresh)
    reading = asyncio.create_task(connectors.get_access_token(installation["id"]))
    handoff = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        handoff = asyncio.create_task(bootstrap_first_account(
            database, username="owner", password="password1", session_manager=session_manager,
        ))
        done, _ = await asyncio.wait({handoff}, timeout=.05)
        assert not done, "the account cannot commit while a refresh can still write the retired key"
        late = await session_manager.start_message(session.id, "Admitted after the drain")
        assert late.disposition == "closed", "legacy execution must stay closed while waiting for key consumers"
        release.set()
        assert (await asyncio.wait_for(reading, 3))["access_token"] == "new-access"
        summary = await asyncio.wait_for(handoff, 3)
        user = await database.get_user(summary["user_id"])
        retained = await database.get_connector_secret(installation["id"])
        assert '"access_token":"new-access"' in decrypt(retained, UserManager(database).data_key(user))
        assert (await database.get_connector_installation(installation["id"]))["user_id"] == user["id"]
    finally:
        release.set()
        await asyncio.gather(reading, *([handoff] if handoff is not None else []), return_exceptions=True)


async def test_bootstrap_waits_for_a_captured_credential_refresh_before_adopting_its_secret(
    legacy, monkeypatch,
):
    from server.crypto import encrypt
    from server.oauth_providers import get_provider

    database, _ = legacy
    await database.save_credential(
        "refresh-credential", "claude-code", "OAuth", "oauth", encrypt(
            '{"access_token":"expired","refresh_token":"refresh","expires_at_epoch":1}', "the-old-token",
        ), "2026-10-20",
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_):
        entered.set()
        await release.wait()
        return OAuthTokenSet("new-access", "new-refresh", time.time() + 3600)

    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", refresh)
    reading = asyncio.create_task(session_manager.resolve_credential_by_id("refresh-credential"))
    handoff = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        handoff = asyncio.create_task(bootstrap_first_account(database, username="owner", password="password1"))
        done, _ = await asyncio.wait({handoff}, timeout=.05)
        assert not done, "the account cannot commit while an OAuth refresh can still write the retired key"
        release.set()
        assert (await asyncio.wait_for(reading, 3)).secret == "new-access"
        summary = await asyncio.wait_for(handoff, 3)
        user = await database.get_user(summary["user_id"])
        retained = await database.get_credential("refresh-credential", user["id"])
        assert '"access_token":"new-access"' in decrypt(retained["secret_encrypted"], UserManager(database).data_key(user))
    finally:
        release.set()
        await asyncio.gather(reading, *([handoff] if handoff is not None else []), return_exceptions=True)


async def test_legacy_secret_creation_waiting_across_bootstrap_cannot_create_an_orphan(
    legacy, clean_registry, monkeypatch,
):
    from server.main import app
    from server.routers import credentials

    database, _ = legacy
    monkeypatch.setattr(credentials, "_db", database)
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    monkeypatch.setattr(deps, "_accounts_exist", None)
    provider = FakeProvider()
    register(FakeConnector(provider))
    connectors = ConnectorManager(database)
    await connectors.set_client_creds("fakekind", "client", "secret")
    installation = await connectors.complete_install(
        kind="fakekind", token_set=OAuthTokenSet("expired", "refresh", time.time() + 1),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(**_):
        entered.set()
        await release.wait()
        return OAuthTokenSet("new-access", "new-refresh", time.time() + 3600)

    monkeypatch.setattr(provider, "refresh", refresh)
    reading = asyncio.create_task(connectors.get_access_token(installation["id"]))
    handoff = creating = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        try:
            await asyncio.wait_for(entered.wait(), 3)
            handoff = asyncio.create_task(bootstrap_first_account(database, username="owner", password="password1"))
            assert not (await asyncio.wait({handoff}, timeout=.05))[0]
            creating = asyncio.create_task(client.post(
                "/api/credentials", headers={"Authorization": "Bearer the-old-token"},
                json={"backend": "claude-code", "label": "Late legacy", "secret": "late-secret"},
            ))
            assert not (await asyncio.wait({creating}, timeout=.03))[0]
            release.set()
            await asyncio.wait_for(reading, 3)
            await asyncio.wait_for(handoff, 3)
            response = await asyncio.wait_for(creating, 3)
            assert response.status_code == 401, response.text
            assert all(row["user_id"] is not None for row in await database.load_credentials())
            assert all(row["label"] != "Late legacy" for row in await database.load_credentials())
        finally:
            release.set()
            await asyncio.gather(reading, *([handoff] if handoff is not None else []),
                                 *([creating] if creating is not None else []), return_exceptions=True)


async def test_account_connector_refresh_does_not_wait_for_the_retired_installation_key(
    legacy, clean_registry, monkeypatch,
):
    database, _ = legacy
    summary = await bootstrap_first_account(database, username="owner", password="password1")
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    register(FakeConnector(FakeProvider()))
    connectors = ConnectorManager(database)
    await connectors.set_client_creds("fakekind", "client", "secret")
    installation = await connectors.complete_install(
        kind="fakekind", token_set=OAuthTokenSet("expired", "refresh", time.time() + 1),
        user_id=summary["user_id"],
    )
    async with database.installation_key_lock:
        # This key has retired; an account-owned refresh uses its stable DEK.
        refreshed = await asyncio.wait_for(connectors.get_access_token(installation["id"], summary["user_id"]), 1)
        assert refreshed["access_token"] == "refreshed"
        await database.append_message("s-old", 0, "assistant", "text", "Unrelated short transaction")
        assert (await database.load_messages("s-old"))[0]["content"] == "Unrelated short transaction"


async def test_captured_legacy_connector_reader_cannot_resolve_the_new_account_key(
    legacy, clean_registry, monkeypatch,
):
    database, _ = legacy
    register(FakeConnector(FakeProvider()))
    connectors = ConnectorManager(database)
    installation = await connectors.complete_install(
        kind="fakekind", token_set=OAuthTokenSet("private-access", "refresh", time.time() + 3600),
    )
    await bootstrap_first_account(database, username="owner", password="password1")
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    # None is the legacy caller captured before adoption, not an unscoped
    # permission to resolve the new owner's DEK from the installation id.
    with pytest.raises(InstallationKeyRetired):
        await connectors.get_access_token(installation["id"], None)


async def test_captured_legacy_credential_reader_cannot_resolve_the_new_account_key(legacy, monkeypatch):
    database, _ = legacy
    await bootstrap_first_account(database, username="owner", password="password1")
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    with pytest.raises(InstallationKeyRetired):
        await session_manager.resolve_credential_by_id("cred-1")

"""Custom definition deletion orders real OAuth bindings and all-account cleanup."""

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.connector_manager import ConnectorError, ConnectorManager
from server.connectors.custom import GenericOAuthProvider
from server.connectors.oauth import ConnectorLoginManager
from server.database import Database
from server.oauth_providers import OAuthTokenSet
from server.routers import connectors
from tests.accounts import create_account
from tests.test_router_connectors import _create_custom


@pytest.fixture
async def custom_runtime(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "custom.db"))
    await db.initialize()
    first, bearer = await create_account(db)
    second, second_bearer = await create_account(db, username="second-owner")
    manager = ConnectorManager(db)
    monkeypatch.setattr(connectors, "_manager", manager)
    monkeypatch.setattr(connectors, "_login_mgr", ConnectorLoginManager())

    async def exchange(self, **kwargs):
        return OAuthTokenSet("access-" + kwargs["code"], "refresh", time.time() + 3600)

    monkeypatch.setattr(GenericOAuthProvider, "exchange_code", exchange)
    app = FastAPI()
    app.include_router(connectors.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
                               headers={"Authorization": "Bearer " + bearer}) as client:
            yield client, manager, [(first, client.headers), (second, {"Authorization": "Bearer " + second_bearer})]
    finally:
        await db.close()


async def begin(client, kind="linear", headers=None, *, installation_id=None):
    body = {"kind": kind}
    if installation_id is not None:
        body["installation_id"] = installation_id
    response = await client.post("/api/connectors/oauth/start", json=body, headers=headers)
    assert response.status_code == 201, response.text
    login_id = response.json()["login_id"]
    login = connectors._login_mgr.get(login_id)
    return login_id, {"code": "original", "state": f"{login_id}:{login.state}"}


async def install(client, kind="linear", headers=None):
    login_id, params = await begin(client, kind, headers)
    assert "Connected" in (await client.get("/api/connectors/oauth/callback", params=params)).text
    receipt = await client.get(f"/api/connectors/oauth/status/{login_id}", headers=headers)
    assert receipt.json()["status"] == "success"
    return login_id, receipt.json()["installation_id"]


async def test_reconnect_custom_updates_selected_installation_and_enabled_agent(custom_runtime):
    client, manager, accounts = custom_runtime
    await _create_custom(client)
    _, selected = await install(client)
    _, other = await install(client)
    assert selected != other
    owner = accounts[0][0]["id"]
    agent = await manager.db.get_system_agent(owner)
    await manager.set_agent_connector(agent["id"], selected, True, owner)
    assert (await client.patch(f"/api/connectors/{selected}", json={"enable_by_default": True})).status_code == 200
    original_grant = (await client.get(f"/api/connectors/{selected}/token")).json()
    rejected = await client.post(f"/api/connectors/{selected}/mark-needs-reconnect",
                                 json={"grant_stamp": original_grant["grant_stamp"]})
    assert rejected.json()["disposition"] == "applied"
    before = {row["id"]: row for row in (await client.get("/api/connectors")).json()}

    login_id, params = await begin(client, installation_id=selected)
    params["code"] = "replacement"
    assert "Connected" in (await client.get("/api/connectors/oauth/callback", params=params)).text
    receipt = (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()
    assert receipt["status"] == "success" and receipt["installation_id"] == selected
    after = {row["id"]: row for row in (await client.get("/api/connectors")).json()}
    assert after.keys() == before.keys()
    assert after[other] == before[other]
    assert after[selected]["created_at"] == before[selected]["created_at"]
    assert after[selected]["enable_by_default"] is True
    assert after[selected]["needs_reconnect"] is False and after[selected]["last_refresh_error_code"] is None
    enabled = await manager.enabled_installations_for_agent(agent["id"])
    assert [installation.id for installation in enabled] == [selected]
    grant = await client.get(f"/api/connectors/{enabled[0].id}/token")
    assert grant.status_code == 200 and grant.json()["access_token"] == "access-replacement"
    assert (await client.get(f"/api/connectors/{other}/token")).json()["access_token"] == "access-original"


@pytest.mark.parametrize("target", ["other_owner", "other_kind", "missing"])
async def test_reconnect_start_refuses_wrong_target(custom_runtime, target):
    client, manager, accounts = custom_runtime
    await _create_custom(client)
    if target == "other_owner":
        _, iid = await install(client, headers=accounts[1][1])
    elif target == "other_kind":
        await _create_custom(client, "other-kind")
        _, iid = await install(client, "other-kind")
    else:
        iid = "missing-installation"
    before = await manager.db.load_connector_installations()
    response = await client.post("/api/connectors/oauth/start", json={"kind": "linear", "installation_id": iid})
    assert response.status_code == 404
    assert await manager.db.load_connector_installations() == before


@pytest.mark.parametrize("phase", ["before_exchange", "during_exchange"])
@pytest.mark.parametrize("removed", ["installation", "definition"])
async def test_reconnect_cannot_recreate_deleted_target(custom_runtime, monkeypatch, phase, removed):
    client, manager, _ = custom_runtime
    await _create_custom(client)
    _, iid = await install(client)
    _, other = await install(client)
    login_id, params = await begin(client, installation_id=iid)
    entered, release = asyncio.Event(), asyncio.Event()
    exchanges = []

    async def exchange(self, **kwargs):
        exchanges.append(kwargs["code"])
        entered.set()
        await release.wait()
        return OAuthTokenSet("replacement", "refresh", time.time() + 3600)

    async def remove():
        if removed == "installation":
            assert (await client.delete(f"/api/connectors/{iid}")).status_code == 204
        else:
            assert (await client.delete("/api/connectors/custom/linear")).status_code == 204
            await _create_custom(client)

    monkeypatch.setattr(GenericOAuthProvider, "exchange_code", exchange)
    callback = None
    try:
        if phase == "before_exchange":
            await remove()
            release.set()
        callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
        if phase == "during_exchange":
            await asyncio.wait_for(entered.wait(), 2)
            await remove()
            release.set()
        page = await asyncio.wait_for(callback, 3)
        assert "Connection failed" in page.text
        status = (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()
        assert status["status"] == "error" and status["installation_id"] is None
        assert [row["id"] for row in (await client.get("/api/connectors")).json()] == (
            [other] if removed == "installation" else [])
        assert await manager.db.get_connector_secret(iid) is None
        assert len(exchanges) == (1 if phase == "during_exchange" else 0)
    finally:
        release.set()
        if callback is not None:
            await asyncio.gather(callback, return_exceptions=True)


async def test_reconnect_secret_failure_rolls_back_metadata_and_keeps_agent_grant(custom_runtime):
    client, manager, accounts = custom_runtime
    await _create_custom(client)
    _, iid = await install(client)
    owner = accounts[0][0]["id"]
    agent = await manager.db.get_system_agent(owner)
    await manager.set_agent_connector(agent["id"], iid, True, owner)
    before = (await client.get("/api/connectors")).json()
    secret = await manager.db.get_connector_secret(iid)
    login_id, params = await begin(client, installation_id=iid)
    params["code"] = "replacement"
    await manager.db.conn.execute(
        "CREATE TRIGGER refuse_reconnect_secret BEFORE UPDATE ON connector_installation_secrets "
        "BEGIN SELECT RAISE(ABORT, 'secret write rejected'); END")
    await manager.db.conn.commit()
    page = await client.get("/api/connectors/oauth/callback", params=params)
    assert "Connection failed" in page.text
    assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == "error"
    assert (await client.get("/api/connectors")).json() == before
    assert await manager.db.get_connector_secret(iid) == secret
    assert [item.id for item in await manager.enabled_installations_for_agent(agent["id"])] == [iid]
    assert (await client.get(f"/api/connectors/{iid}/token")).json()["access_token"] == "access-original"


async def test_cancel_reconnect_during_exchange_keeps_original_installation(custom_runtime, monkeypatch):
    client, manager, _ = custom_runtime
    await _create_custom(client)
    _, iid = await install(client)
    login_id, params = await begin(client, installation_id=iid)
    before = (await client.get("/api/connectors")).json()
    secret = await manager.db.get_connector_secret(iid)
    entered, release = asyncio.Event(), asyncio.Event()

    async def exchange(self, **kwargs):
        entered.set()
        await release.wait()
        return OAuthTokenSet("discarded-replacement", None, time.time() + 3600)

    monkeypatch.setattr(GenericOAuthProvider, "exchange_code", exchange)
    callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cancelled = await client.post("/api/connectors/oauth/cancel", json={"login_id": login_id})
        assert cancelled.json()["status"] == "cancelled" and cancelled.json()["installation_id"] is None
        release.set()
        assert "Connection cancelled" in (await asyncio.wait_for(callback, 3)).text
        assert (await client.get("/api/connectors")).json() == before
        assert await manager.db.get_connector_secret(iid) == secret
        assert (await client.get(f"/api/connectors/{iid}/token")).json()["access_token"] == "access-original"
    finally:
        release.set()
        await asyncio.gather(callback, return_exceptions=True)


async def test_reconnect_waits_for_original_refresh_and_cancel_observes_committed_target(custom_runtime, monkeypatch):
    client, manager, _ = custom_runtime
    await _create_custom(client)

    async def exchange(self, **kwargs):
        return OAuthTokenSet(kwargs["code"], "original-refresh", time.time() + (1 if kwargs["code"] == "original" else 3600))

    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(self, **kwargs):
        entered.set()
        await release.wait()
        return OAuthTokenSet("rotated-original", "rotated-refresh", time.time() + 3600)

    monkeypatch.setattr(GenericOAuthProvider, "exchange_code", exchange)
    monkeypatch.setattr(GenericOAuthProvider, "refresh", refresh)
    _, iid = await install(client)
    login_id, params = await begin(client, installation_id=iid)
    params["code"] = "replacement"
    refreshing = asyncio.create_task(client.get(f"/api/connectors/{iid}/token"))
    callback = cancel = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
        login = connectors._login_mgr.get(login_id)
        # The original binding has claimed cancellation ownership and is
        # waiting behind the actual refresh's installation lock.
        async with asyncio.timeout(2):
            while not login.binding_lock.locked():
                await asyncio.sleep(0)
        cancel = asyncio.create_task(client.post("/api/connectors/oauth/cancel", json={"login_id": login_id}))
        release.set()
        rotated, page, cancelled = await asyncio.wait_for(asyncio.gather(refreshing, callback, cancel), 3)
        assert rotated.json()["access_token"] == "rotated-original"
        assert "Connected" in page.text
        assert cancelled.json()["status"] == "success" and cancelled.json()["installation_id"] == iid
        assert (await client.get(f"/api/connectors/{iid}/token")).json()["access_token"] == "replacement"
        assert [row["id"] for row in (await client.get("/api/connectors")).json()] == [iid]
    finally:
        release.set()
        await asyncio.gather(refreshing, *([callback] if callback else []), *([cancel] if cancel else []), return_exceptions=True)


async def test_enabled_custom_mcp_uses_replacement_grant_and_old_401_is_obsolete(custom_runtime, monkeypatch):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from server.config import settings
    from server.harness.assembly import _http_entries
    from server.mcp_identity import mint
    from tests.callback_api import start_callback_api

    client, manager, accounts = custom_runtime
    await _create_custom(client)
    _, iid = await install(client)
    owner = accounts[0][0]["id"]
    agent = await manager.db.get_system_agent(owner)
    await manager.set_agent_connector(agent["id"], iid, True, owner)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    send = httpx.AsyncHTTPTransport.handle_async_request

    async def upstream(transport, request):
        if request.url.host == "provider.test":
            sent.append((str(request.url), request.headers["Authorization"]))
            if request.headers["Authorization"] == "Bearer access-original":
                entered.set()
                await release.wait()
                return httpx.Response(401, json={"error": "old token rejected"}, request=request)
            return httpx.Response(200, json={"authorization": "replacement"}, request=request)
        return await send(transport, request)

    monkeypatch.delenv("OCTOPUS_CONNECTOR_API_BASE", raising=False)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", upstream)
    api = await start_callback_api(connectors.router, readiness_token=mint("readiness", user_id=owner))
    monkeypatch.setattr(settings, "port", api.port)
    try:
        enabled = await manager.enabled_installations_for_agent(agent["id"])
        entry, = _http_entries([], [(await manager.get(item.kind), item) for item in enabled], "agent-request", owner)
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {entry.credential}"}, timeout=10) as http_client:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/custom/mcp", http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    old = asyncio.create_task(session.call_tool("request", {"method": "GET", "path": "/items"}))
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        login_id, params = await begin(client, installation_id=iid)
                        params["code"] = "replacement"
                        assert "Connected" in (await client.get("/api/connectors/oauth/callback", params=params)).text
                        assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["installation_id"] == iid
                        release.set()
                        rejected = await asyncio.wait_for(old, 3)
                        assert "authorization has since changed; its current sign-in was left unchanged" in rejected.content[0].text
                        result = await session.call_tool("request", {"method": "GET", "path": "/items"})
                        assert '"authorization": "replacement"' in result.content[0].text
                        row, = (await client.get("/api/connectors")).json()
                        assert row["id"] == iid and not row["needs_reconnect"] and row["last_refresh_error_code"] is None
                        assert sent == [("https://provider.test/api/items", "Bearer access-original"),
                                        ("https://provider.test/api/items", "Bearer access-replacement")]
                    finally:
                        release.set()
                        await asyncio.gather(old, return_exceptions=True)
    finally:
        release.set()
        await api.stop()


async def test_delete_cleans_every_account_and_keeps_historical_success(custom_runtime):
    client, manager, accounts = custom_runtime
    await _create_custom(client)
    await _create_custom(client, "other-kind")
    installed = []
    for account, headers in accounts:
        login_id, iid = await install(client, headers=headers)
        agent = await manager.db.get_system_agent(account["id"])
        await manager.set_agent_connector(agent["id"], iid, True, account["id"])
        installed.append((login_id, iid, agent["id"], headers))
    _, kept = await install(client, "other-kind")
    await manager.set_agent_connector(installed[0][2], kept, True, accounts[0][0]["id"])
    assert (await client.delete("/api/connectors/custom/linear")).status_code == 204
    for login_id, iid, agent_id, headers in installed:
        assert await manager.get_installation(iid) is None
        assert await manager.db.get_connector_secret(iid) is None
        assert await manager.get_agent_connector_ids(agent_id) == ([kept] if agent_id == installed[0][2] else [])
        assert (await client.get(f"/api/connectors/oauth/status/{login_id}", headers=headers)).json() == {
            "status": "success", "installation_id": iid, "message": None,
        }
        assert iid not in {row["id"] for row in (await client.get("/api/connectors", headers=headers)).json()}
    assert await manager.resolve_client_creds("linear") is None
    assert await manager.get("linear") is None
    assert (await manager.get_access_token(kept, accounts[0][0]["id"]))["access_token"] == "access-original"
    assert (await client.get("/api/connectors")).json()[0]["id"] == kept


@pytest.mark.parametrize("phase", ["before", "installations", "commit", "rollback"])
async def test_definition_delete_cancellation_and_failure_keep_one_complete_state(custom_runtime, monkeypatch, phase):
    client, manager, accounts = custom_runtime
    db = manager.db
    await _create_custom(client)
    _, iid = await install(client)
    agent = await db.get_system_agent(accounts[0][0]["id"])
    await manager.set_agent_connector(agent["id"], iid, True, accounts[0][0]["id"])
    original_secret = await db.get_connector_secret(iid)
    login_id, params = await begin(client)
    if phase == "rollback":
        await db.conn.execute(
            "CREATE TRIGGER refuse_definition_delete BEFORE DELETE ON custom_connectors "
            "BEGIN SELECT RAISE(ABORT, 'definition delete rejected'); END"
        )
        await db.conn.commit()
        assert (await client.delete("/api/connectors/custom/linear")).status_code == 500
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        execute, commit = db.conn.execute, db.conn.commit
        removed = False

        async def gated_execute(sql, parameters=()):
            nonlocal removed
            cursor = await execute(sql, parameters)
            if sql.startswith("DELETE FROM connector_installations WHERE kind"):
                removed = True
                if phase == "installations":
                    entered.set()
                    await release.wait()
            return cursor

        async def gated_commit():
            if removed and phase == "commit":
                entered.set()
                await release.wait()
            await commit()

        with monkeypatch.context() as patch:
            patch.setattr(db.conn, "execute", gated_execute)
            patch.setattr(db.conn, "commit", gated_commit)
            if phase == "before":
                await manager._definition_lock.acquire()
            deleting = asyncio.create_task(manager.delete_custom_connector("linear"))
            try:
                if phase == "before":
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait_for(entered.wait(), 2)
                deleting.cancel()
                await asyncio.sleep(0)
                deleting.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await deleting
            finally:
                release.set()
                if phase == "before":
                    manager._definition_lock.release()
                await asyncio.gather(deleting, return_exceptions=True)
    changed = phase in {"installations", "commit"}
    assert (await manager.get("linear") is None) == changed
    assert (await manager.resolve_client_creds("linear") is None) == changed
    assert (await manager.get_installation(iid) is None) == changed
    assert await db.get_connector_secret(iid) == (None if changed else original_secret)
    assert await manager.get_agent_connector_ids(agent["id"]) == ([] if changed else [iid])
    if changed:
        await _create_custom(client)
    page = await client.get("/api/connectors/oauth/callback", params=params)
    assert ("Connection failed" in page.text) == changed
    status = (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()
    assert status["status"] == ("error" if changed else "success")


async def test_binding_commit_before_delete_keeps_success_receipt_and_cleans_installation(custom_runtime, monkeypatch):
    client, manager, _ = custom_runtime
    db = manager.db
    await _create_custom(client)
    login_id, params = await begin(client)
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    binding = False

    async def observed_execute(sql, parameters=()):
        nonlocal binding
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO connector_installations"):
            binding = True
        return cursor

    async def gated_commit():
        if binding and not entered.is_set():
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
        deleting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deleting = asyncio.create_task(manager.delete_custom_connector("linear"))
            await asyncio.sleep(0)
            release.set()
            page, _ = await asyncio.gather(callback, deleting)
        finally:
            release.set()
            await asyncio.gather(callback, *([deleting] if deleting else []), return_exceptions=True)
    assert "Connected" in page.text
    status = (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()
    assert status["status"] == "success"
    assert await manager.get_installation(status["installation_id"]) is None
    assert await db.get_connector_secret(status["installation_id"]) is None
    assert (await client.get("/api/connectors")).json() == []


async def test_late_oauth_client_put_cannot_recreate_deleted_definition_client(custom_runtime, monkeypatch):
    client, manager, _ = custom_runtime
    await _create_custom(client)
    db = manager.db
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    captured = False

    async def observed_execute(sql, parameters=()):
        nonlocal captured
        cursor = await execute(sql, parameters)
        if "FROM custom_connectors WHERE kind = ?" in sql and not entered.is_set():
            captured = True
        return cursor

    async def gated_commit():
        if captured and not entered.is_set():
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        setting = asyncio.create_task(client.put("/api/connectors/linear/oauth-client", json={
            "client_id": "late-id", "client_secret": "late-secret",
        }))
        deleting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deleting = asyncio.create_task(manager.delete_custom_connector("linear"))
            await asyncio.sleep(0)
            release.set()
            response, _ = await asyncio.gather(setting, deleting)
        finally:
            release.set()
            await asyncio.gather(setting, *([deleting] if deleting else []), return_exceptions=True)
    assert await db.get_custom_connector("linear") is None
    assert await db.get_connector_oauth_client("linear") is None
    assert response.status_code != 200


async def test_refresh_captures_original_endpoint_and_client_before_definition_replacement(custom_runtime, monkeypatch):
    client, manager, accounts = custom_runtime
    await _create_custom(client)
    installation = await manager.complete_install(kind="linear", user_id=accounts[0][0]["id"],
        token_set=OAuthTokenSet("old-access", "original-refresh", time.time() + 1))
    db = manager.db
    snapshot, release_snapshot, provider_entered, release_provider = (asyncio.Event() for _ in range(4))
    execute, commit = db.conn.execute, db.conn.commit
    captured = False
    sent = []

    async def observed_execute(sql, parameters=()):
        nonlocal captured
        cursor = await execute(sql, parameters)
        if "FROM custom_connectors WHERE kind = ?" in sql and not snapshot.is_set():
            captured = True
        return cursor

    async def gated_commit():
        if captured and not snapshot.is_set():
            snapshot.set()
            await release_snapshot.wait()
        await commit()

    async def refresh(self, **kwargs):
        sent.append((self.token_url, kwargs["client_id"], kwargs["client_secret"], kwargs["refresh_token"]))
        provider_entered.set()
        await release_provider.wait()
        return OAuthTokenSet("rotated", "rotated-refresh", time.time() + 3600)

    async def replace():
        await manager.delete_custom_connector("linear")
        await _create_custom(client, token_url="https://replacement.test/token", client_id="new-id", client_secret="new-secret")

    monkeypatch.setattr(GenericOAuthProvider, "refresh", refresh)
    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        refreshing = asyncio.create_task(manager.get_access_token(installation["id"], accounts[0][0]["id"]))
        replacement = provider_wait = None
        try:
            await asyncio.wait_for(snapshot.wait(), 2)
            replacement = asyncio.create_task(replace())
            await asyncio.sleep(0)
            release_snapshot.set()
            provider_wait = asyncio.create_task(provider_entered.wait())
            await asyncio.wait_for(asyncio.wait({refreshing, provider_wait}, return_when=asyncio.FIRST_COMPLETED), 3)
            assert provider_entered.is_set(), "Definition/client reads did not preserve the original refresh context"
            await asyncio.wait_for(replacement, 3)
            assert sent == [("https://provider.test/token", "cid", "csec", "original-refresh")]
            release_provider.set()
            with pytest.raises(ConnectorError):
                await refreshing
            assert await manager.db.get_connector_secret(installation["id"]) is None
        finally:
            release_snapshot.set()
            release_provider.set()
            if provider_wait is not None:
                provider_wait.cancel()
            await asyncio.gather(refreshing, *([replacement] if replacement else []), *([provider_wait] if provider_wait else []), return_exceptions=True)


@pytest.mark.parametrize("operation", ["put", "delete"])
async def test_old_client_configuration_writer_cannot_affect_recreated_definition(custom_runtime, monkeypatch, operation):
    client, manager, _ = custom_runtime
    await _create_custom(client)
    db = manager.db
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    captured = False

    async def observed_execute(sql, parameters=()):
        nonlocal captured
        cursor = await execute(sql, parameters)
        if "FROM custom_connectors WHERE kind = ?" in sql and not entered.is_set():
            captured = True
        return cursor

    async def gated_commit():
        if captured and not entered.is_set():
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        writer = asyncio.create_task(getattr(client, operation)("/api/connectors/linear/oauth-client", **(
            {"json": {"client_id": "old-id", "client_secret": "old-secret"}} if operation == "put" else {}
        )))
        deleting = recreating = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deleting = asyncio.create_task(manager.delete_custom_connector("linear"))
            recreating = asyncio.create_task(manager.create_custom_connector(
                kind="linear", display_name="Replacement", authorize_url="https://replacement.test/auth",
                token_url="https://replacement.test/token", scopes=[], pkce=True, api_base="https://replacement.test/api",
                client_id="replacement-id", client_secret="replacement-secret",
            ))
            await asyncio.sleep(0)
            release.set()
            response, _, _ = await asyncio.gather(writer, deleting, recreating)
        finally:
            release.set()
            await asyncio.gather(writer, *([deleting] if deleting else []), *([recreating] if recreating else []), return_exceptions=True)
    assert response.status_code == 400
    assert await manager.resolve_client_creds("linear") == ("replacement-id", "replacement-secret")

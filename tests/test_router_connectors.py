"""Tests for the /api/connectors routes (connectors.md §5.5 / §8): auth,
in-app OAuth-client config, catalog availability, the OAuth install flow,
installation CRUD, the internal token route, and agent-scoped enable routes."""

from __future__ import annotations

import asyncio
import time
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.connector_manager import ConnectorManager
from server.connectors.base import ConnectorBase
from server.connectors.registry import KIND_REGISTRY, register
from server.database import Database
from server.main import app
from server.oauth_providers import OAuthTokenSet
from server.routers import agents as agents_mod
from server.routers import connectors as connectors_mod
from server.session_manager import session_manager
from tests.accounts import create_account


class FakeProvider:
    kind = "fakekind"
    authorize_url = "https://fake/auth"
    token_url = "https://fake/token"
    default_scopes = ["s"]
    pkce = True

    def build_authorize_url(self, *, client_id, redirect_uri, code_challenge, state):
        return f"{self.authorize_url}?client_id={client_id}&state={state}"

    async def exchange_code(
        self, *, client_id, client_secret, code, redirect_uri, code_verifier, state
    ):
        return OAuthTokenSet("at-" + code, "rt", time.time() + 3600, scopes=["s"])

    async def refresh(self, *, client_id, client_secret, refresh_token):
        return OAuthTokenSet("at2", refresh_token, time.time() + 3600)


class FakeConnector(ConnectorBase):
    kind = "fakekind"
    display_name = "Fake"
    category = "test"
    allows_multiple = True
    oauth = FakeProvider()
    tools = ("search",)

    async def fetch_external_identity(self, token_set):
        return ("ext-1", "me@example.com")


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db)
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))
    connectors_mod.set_manager(ConnectorManager(db))

    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    register(FakeConnector())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as c:
        yield c

    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)
    await db.close()


async def _configure(client, kind="fakekind"):
    r = await client.put(
        f"/api/connectors/{kind}/oauth-client",
        json={"client_id": "cid", "client_secret": "csec"},
        headers=client.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _start(client, kind="fakekind", **body):
    return await client.post(
        "/api/connectors/oauth/start", json={"kind": kind, **body}, headers=client.headers
    )


async def _install(client, code="abc"):
    """Configure the OAuth client, then run the full OAuth flow."""
    await _configure(client)
    r = await _start(client)
    assert r.status_code == 201, r.text
    login_id = r.json()["login_id"]
    raw_state = connectors_mod._login_mgr.get(login_id).state
    cb = await client.get(
        "/api/connectors/oauth/callback",
        params={"code": code, "state": f"{login_id}:{raw_state}"},
    )
    assert cb.status_code == 200
    insts = (await client.get("/api/connectors", headers=client.headers)).json()
    return insts[-1], login_id


async def test_two_first_oauth_callbacks_commit_one_installation_and_both_succeed(client, monkeypatch):
    await _configure(client)
    started = [await _start(client) for _ in range(2)]
    login_ids = [response.json()["login_id"] for response in started]
    logins = [connectors_mod._login_mgr.get(login_id) for login_id in login_ids]
    both = asyncio.Event()
    calls = 0

    async def identity(connector, token_set):
        nonlocal calls
        calls += 1
        if calls == 2:
            both.set()
        await both.wait()
        return "same-provider-account", "Shared account"

    monkeypatch.setattr(FakeConnector, "fetch_external_identity", identity)
    pages = await asyncio.wait_for(asyncio.gather(*(
        client.get("/api/connectors/oauth/callback", params={"code": str(index), "state": f"{login_ids[index]}:{login.state}"})
        for index, login in enumerate(logins)
    )), 3)
    assert all(page.status_code == 200 and "is connected" in page.text for page in pages)
    statuses = [(await client.get(f"/api/connectors/oauth/status/{login_id}")).json() for login_id in login_ids]
    assert all(status["status"] == "success" for status in statuses)
    assert statuses[0]["installation_id"] == statuses[1]["installation_id"]
    installations = (await client.get("/api/connectors")).json()
    assert len(installations) == 1
    assert installations[0]["id"] == statuses[0]["installation_id"]
    assert "secret_encrypted" not in installations[0] and "access_token" not in installations[0]


async def _new_agent(client, name="Researcher"):
    r = await client.post("/api/agents", json={"name": name}, headers=client.headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_oauth_callback_displays_external_errors_as_text(client):
    class Page(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.scripts = 0
            self.text = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts += 1

        def handle_data(self, data):
            self.text.append(data)

    await _configure(client)
    started = await _start(client)
    login_id = started.json()["login_id"]
    state = connectors_mod._login_mgr.get(login_id).state
    payload = '</p><script>window.leaked=localStorage.octopus_token</script><img src=x onerror="alert(1)">'
    response = await client.get("/api/connectors/oauth/callback", params={
        "state": f"{login_id}:{state}", "error": payload,
    }, headers={"Authorization": ""})
    assert response.status_code == 200
    page = Page()
    page.feed(response.text)
    assert page.scripts == 1  # only the fixed window.close script
    assert payload in "".join(page.text)
    assert payload not in response.text
    assert connectors_mod._login_mgr.get(login_id).message == payload
    # Invalid state must still be refused before accepting provider errors.
    rejected = await client.get("/api/connectors/oauth/callback", params={"state": "invalid", "error": payload})
    assert payload not in rejected.text


async def test_custom_http_mcp_uses_each_owned_installations_definition_concurrently(client, monkeypatch):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from server.config import settings
    from server.connectors.base import ConnectorInstallation
    from server.harness.assembly import _http_entries
    from server.mcp_identity import mint
    from tests.callback_api import start_callback_api

    manager = connectors_mod._require_manager()
    owner = (await manager.db.get_system_agent())["user_id"]
    other, _ = await create_account(manager.db, username="other-connector-owner")
    entries = []
    expected = []
    installations = []
    for kind, host in [("custom-a", "a"), ("custom-b", "b")]:
        base = f"https://{host}.example.invalid/v1"
        await manager.create_custom_connector(
            kind=kind, display_name=kind, authorize_url="https://oauth.invalid/auth",
            token_url="https://oauth.invalid/token", scopes=[], pkce=True,
            api_base=base, client_id="id", client_secret="secret",
        )
        installation = await manager.complete_install(
            kind=kind, token_set=OAuthTokenSet(f"token-{host}", None, 0), user_id=owner,
        )
        connector = await manager.get(kind)
        entry = _http_entries([], [(connector, ConnectorInstallation(
            id=installation["id"], kind=kind, label=kind,
        ))], "mcp-session", owner)[0]
        entries.append(entry)
        installations.append(installation)
        expected.append((f"{base}/items", f"Bearer token-{host}"))

    destinations = []

    def upstream(method, url, *, headers, **kwargs):
        destinations.append((url, headers["Authorization"]))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.delenv("OCTOPUS_CONNECTOR_API_BASE", raising=False)
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "wrong-process-installation")
    monkeypatch.setattr(httpx, "request", upstream)
    api = await start_callback_api(
        connectors_mod.router, readiness_token=mint("readiness", user_id=owner),
    )
    monkeypatch.setattr(settings, "port", api.port)

    async def call(entry):
        http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {entry.credential}"}, timeout=10,
        )
        async with streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/custom/mcp", http_client=http_client,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("request", {"method": "GET", "path": "/items"})
                return result.content[0].text

    try:
        outputs = await asyncio.gather(*(call(entry) for entry in entries))
        assert all('"ok": true' in output for output in outputs)
        assert sorted(destinations) == sorted(expected)
        # A valid different-account scope cannot read token or destination even
        # when it names the victim installation.
        stolen = _http_entries([], [(await manager.get(installations[0]["kind"]), ConnectorInstallation(
            id=installations[0]["id"], kind=installations[0]["kind"], label="other",
        ))], "other-session", other["id"])[0]
        assert "unavailable" in await call(stolen)
        assert len(destinations) == 2
    finally:
        await api.stop()


# --- auth + catalog --------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_auth(client):
    client.headers.clear()
    assert (await client.get("/api/connectors")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_catalog_lists_registered_kinds(client):
    cat = {
        c["kind"]: c
        for c in (await client.get("/api/connectors/catalog", headers=client.headers)).json()
    }
    assert cat["fakekind"]["display_name"] == "Fake"
    assert cat["fakekind"]["available"] is False  # not configured yet


# --- in-app OAuth client config -------------------------------------------


@pytest.mark.asyncio
async def test_oauth_client_set_get_delete(client):
    g = await client.get("/api/connectors/fakekind/oauth-client", headers=client.headers)
    assert g.status_code == 200
    assert g.json()["configured"] is False
    assert g.json()["redirect_uri"].endswith("/api/connectors/oauth/callback")
    assert "client_secret" not in g.json()  # secret never returned

    p = await _configure(client)
    assert p["configured"] is True and p["client_id"] == "cid" and p["source"] == "db"

    # Catalog availability flips on.
    cat = {
        c["kind"]: c
        for c in (await client.get("/api/connectors/catalog", headers=client.headers)).json()
    }
    assert cat["fakekind"]["available"] is True

    d = await client.delete(
        "/api/connectors/fakekind/oauth-client", headers=client.headers
    )
    assert d.status_code == 204
    g2 = await client.get("/api/connectors/fakekind/oauth-client", headers=client.headers)
    assert g2.json()["configured"] is False

    # Unknown kind.
    assert (
        await client.get("/api/connectors/ghost/oauth-client", headers=client.headers)
    ).status_code == 404


# --- custom connectors -----------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_delete_custom_connector(client):
    r = await client.post(
        "/api/connectors/custom",
        json={
            "kind": "linear",
            "display_name": "Linear",
            "authorize_url": "https://l/auth",
            "token_url": "https://l/tok",
            "scopes": ["read"],
            "pkce": True,
            "api_base": "https://api.linear.app",
            "client_id": "cid",
            "client_secret": "csec",
        },
        headers=client.headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "linear" and body["custom"] is True
    assert body["available"] is True  # creds set during create

    cat = {
        c["kind"]: c
        for c in (await client.get("/api/connectors/catalog", headers=client.headers)).json()
    }
    assert cat["linear"]["custom"] is True

    assert (
        await client.delete("/api/connectors/custom/linear", headers=client.headers)
    ).status_code == 204
    cat = {
        c["kind"]: c
        for c in (await client.get("/api/connectors/catalog", headers=client.headers)).json()
    }
    assert "linear" not in cat
    assert (
        await client.delete("/api/connectors/custom/ghost", headers=client.headers)
    ).status_code == 404


# --- OAuth install flow ----------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_start_unknown_kind(client):
    assert (await _start(client, kind="ghost")).status_code == 404


@pytest.mark.asyncio
async def test_oauth_start_unconfigured_kind(client):
    # Registered but no OAuth client set → cannot start.
    assert (await _start(client)).status_code == 400


@pytest.mark.asyncio
async def test_oauth_flow_installs_and_reports_success(client):
    inst, login_id = await _install(client)
    assert inst["kind"] == "fakekind"
    assert inst["label"] == "me@example.com"
    assert inst["external_account_id"] == "ext-1"
    st = await client.get(
        f"/api/connectors/oauth/status/{login_id}", headers=client.headers
    )
    assert st.status_code == 200
    assert st.json()["status"] == "success"
    assert st.json()["installation_id"] == inst["id"]


@pytest.mark.parametrize("prior", ["cancelled", "expired", "consumed"])
async def test_oauth_callback_cannot_exchange_after_flow_closed(client, monkeypatch, prior):
    await _configure(client)
    started = await _start(client)
    login_id = started.json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    params = {"code": "abc", "state": f"{login_id}:{login.state}"}
    calls = []
    exchange = FakeProvider.exchange_code

    async def counted(provider, **kwargs):
        calls.append(kwargs["code"])
        return await exchange(provider, **kwargs)

    monkeypatch.setattr(FakeProvider, "exchange_code", counted)
    if prior == "cancelled":
        await client.post("/api/connectors/oauth/cancel", json={"login_id": login_id})
    elif prior == "expired":
        monkeypatch.setattr(connectors_mod._login_mgr, "_now", lambda: login.created_at + 901)
    else:
        await client.get("/api/connectors/oauth/callback", params=params)
    before = len(calls)
    await client.get("/api/connectors/oauth/callback", params=params)
    assert len(calls) == before
    rows = (await client.get("/api/connectors")).json()
    assert len(rows) == (1 if prior == "consumed" else 0)


@pytest.mark.parametrize("stage", ["exchange", "identity", "commit", "commit_disconnected"])
async def test_oauth_cancel_and_binding_choose_one_result(client, monkeypatch, stage):
    await _configure(client)
    login_id = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "exchange":
        host, name = FakeProvider, "exchange_code"
    elif stage == "identity":
        host, name = FakeConnector, "fetch_external_identity"
    else:
        host, name = connectors_mod._require_manager().db, "authorize_connector_installation"
    original = getattr(host, name)

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, name, held)
    callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params={
        "code": "abc", "state": f"{login_id}:{login.state}",
    }))
    cancel = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        cancel = asyncio.create_task(client.post("/api/connectors/oauth/cancel", json={"login_id": login_id}))
        if stage.startswith("commit"):
            if stage == "commit_disconnected":
                callback.cancel()
            await asyncio.wait({cancel}, timeout=0.02)
            assert not cancel.done(), "Cancellation cannot precede the binding commit acknowledgment"
        else:
            response = await asyncio.wait_for(cancel, 2)
            assert response.status_code == 200 and response.json()["status"] == "cancelled"
        release.set()
        pages = await asyncio.wait_for(asyncio.gather(callback, return_exceptions=True), 3)
        response = await cancel
        expected = "success" if stage.startswith("commit") else "cancelled"
        assert response.json()["status"] == expected
        assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == expected
        assert len((await client.get("/api/connectors")).json()) == (1 if stage.startswith("commit") else 0)
        if stage == "commit_disconnected":
            assert isinstance(pages[0], asyncio.CancelledError)
        else:
            assert ("is connected" in pages[0].text) is (stage == "commit")
    finally:
        release.set()
        await asyncio.gather(callback, *([cancel] if cancel else []), return_exceptions=True)


async def test_claimed_callback_crosses_ttl_and_concurrent_repeat_never_exchanges(client, monkeypatch):
    await _configure(client)
    login_id = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    params = {"code": "abc", "state": f"{login_id}:{login.state}"}
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    exchange = FakeProvider.exchange_code

    async def held(provider, **kwargs):
        calls.append(kwargs["code"])
        entered.set()
        await release.wait()
        return await exchange(provider, **kwargs)

    monkeypatch.setattr(FakeProvider, "exchange_code", held)
    callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        monkeypatch.setattr(connectors_mod._login_mgr, "_now", lambda: login.created_at + 901)
        await _start(client)  # GC must not retire a timely claimed callback.
        duplicate = await asyncio.wait_for(client.get("/api/connectors/oauth/callback", params=params), 2)
        assert "processing" in duplicate.text
        release.set()
        assert "is connected" in (await callback).text
        assert calls == ["abc"]
        assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == "success"
    finally:
        release.set()
        await callback


async def test_oauth_binding_rechecks_account_inside_persistence(client, monkeypatch):
    await _configure(client)
    login_id = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    manager = connectors_mod._require_manager()
    persist = manager.db.authorize_connector_installation

    async def revoke_before_commit(**kwargs):
        await manager.db.update_user_field(login.user_id, disabled_at="2026-10-27T00:00:00Z")
        return await persist(**kwargs)

    monkeypatch.setattr(manager.db, "authorize_connector_installation", revoke_before_commit)
    await client.get("/api/connectors/oauth/callback", params={"code": "abc", "state": f"{login_id}:{login.state}"})
    assert await manager.list_installations(user_id=login.user_id) == []
    assert login.status.value == "error"


@pytest.mark.parametrize("revoke_at", ["before_callback", "after_exchange"])
async def test_revoked_oauth_account_cannot_start_next_remote_operation(client, monkeypatch, revoke_at):
    await _configure(client)
    login_id = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    manager = connectors_mod._require_manager()
    exchanges, identities = [], []
    exchange = FakeProvider.exchange_code
    identity = FakeConnector.fetch_external_identity

    async def revoke():
        await manager.db.update_user_field(login.user_id, disabled_at="2026-10-27T00:00:00Z")

    async def counted_exchange(provider, **kwargs):
        exchanges.append(kwargs["code"])
        token_set = await exchange(provider, **kwargs)
        if revoke_at == "after_exchange":
            await revoke()
        return token_set

    async def counted_identity(connector, token_set):
        identities.append(token_set.access_token)
        return await identity(connector, token_set)

    monkeypatch.setattr(FakeProvider, "exchange_code", counted_exchange)
    monkeypatch.setattr(FakeConnector, "fetch_external_identity", counted_identity)
    if revoke_at == "before_callback":
        await revoke()
    await client.get("/api/connectors/oauth/callback", params={
        "code": "abc", "state": f"{login_id}:{login.state}",
    })
    assert exchanges == ([] if revoke_at == "before_callback" else ["abc"])
    assert identities == []
    assert login.status.value == "error"
    assert await manager.list_installations(user_id=login.user_id) == []


@pytest.mark.parametrize("stage", ["exchange", "identity"])
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_interrupted_callback_before_binding_finishes_login(client, monkeypatch, stage, cancel_first):
    await _configure(client)
    login_id = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(login_id)
    entered, release = asyncio.Event(), asyncio.Event()
    host, name = ((FakeProvider, "exchange_code") if stage == "exchange"
                  else (FakeConnector, "fetch_external_identity"))
    original = getattr(host, name)
    calls = []

    async def held(*args, **kwargs):
        calls.append(stage)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, name, held)
    params = {"code": "abc", "state": f"{login_id}:{login.state}"}
    callback = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel_first:
            cancelled = await client.post("/api/connectors/oauth/cancel", json={"login_id": login_id})
            assert cancelled.json()["status"] == "cancelled"
        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(callback, 2)
        expected = "cancelled" if cancel_first else "error"
        assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == expected
        assert (await client.get("/api/connectors")).json() == []
        await client.get("/api/connectors/oauth/callback", params=params)
        assert calls == [stage], "Interrupted callbacks remain consumed"
        assert login.finished_at is not None
        monkeypatch.setattr(connectors_mod._login_mgr, "_now", lambda: login.finished_at + 901)
        await _start(client)
        assert connectors_mod._login_mgr.get(login_id) is None
    finally:
        release.set()
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


async def test_failed_callback_is_consumed_and_new_flow_can_connect(client, monkeypatch):
    await _configure(client)
    exchange = FakeProvider.exchange_code
    calls = []

    async def fail_once(provider, **kwargs):
        calls.append(kwargs["code"])
        if len(calls) == 1:
            raise ValueError("Remote exchange failed")
        return await exchange(provider, **kwargs)

    monkeypatch.setattr(FakeProvider, "exchange_code", fail_once)
    first = (await _start(client)).json()["login_id"]
    login = connectors_mod._login_mgr.get(first)
    params = {"code": "original", "state": f"{first}:{login.state}"}
    await client.get("/api/connectors/oauth/callback", params=params)
    await client.get("/api/connectors/oauth/callback", params=params)
    assert calls == ["original"] and login.status.value == "error"
    second = (await _start(client)).json()["login_id"]
    next_login = connectors_mod._login_mgr.get(second)
    await client.get("/api/connectors/oauth/callback", params={"code": "new", "state": f"{second}:{next_login.state}"})
    assert calls == ["original", "new"] and next_login.status.value == "success"
    assert len((await client.get("/api/connectors")).json()) == 1


@pytest.mark.asyncio
async def test_oauth_callback_rejects_bad_state(client):
    await _configure(client)
    r = await _start(client)
    login_id = r.json()["login_id"]
    cb = await client.get(
        "/api/connectors/oauth/callback",
        params={"code": "abc", "state": f"{login_id}:tampered"},
    )
    assert cb.status_code == 200  # HTML page either way
    assert "failed" in cb.text.lower()
    assert (await client.get("/api/connectors", headers=client.headers)).json() == []


@pytest.mark.asyncio
async def test_oauth_status_unknown_login(client):
    r = await client.get("/api/connectors/oauth/status/nope", headers=client.headers)
    assert r.status_code == 404


# --- installation management ----------------------------------------------


@pytest.mark.asyncio
async def test_patch_and_delete_installation(client):
    inst, _ = await _install(client)
    iid = inst["id"]
    p = await client.patch(
        f"/api/connectors/{iid}",
        json={"label": "Work", "enable_by_default": True},
        headers=client.headers,
    )
    assert p.status_code == 200
    assert p.json()["label"] == "Work"
    assert p.json()["enable_by_default"] is True

    assert (await client.delete(f"/api/connectors/{iid}", headers=client.headers)).status_code == 204
    assert (await client.delete(f"/api/connectors/{iid}", headers=client.headers)).status_code == 404


@pytest.mark.asyncio
async def test_token_route(client):
    inst, _ = await _install(client, code="xyz")
    r = await client.get(f"/api/connectors/{inst['id']}/token", headers=client.headers)
    assert r.status_code == 200
    assert r.json()["access_token"] == "at-xyz"
    assert (
        await client.get("/api/connectors/ghost/token", headers=client.headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_mark_needs_reconnect_route(client):
    inst, _ = await _install(client)
    r = await client.post(
        f"/api/connectors/{inst['id']}/mark-needs-reconnect",
        params={"error_code": "invalid_grant"},
        headers=client.headers,
    )
    assert r.status_code == 204
    rows = (await client.get("/api/connectors", headers=client.headers)).json()
    assert rows[0]["needs_reconnect"] is True
    assert rows[0]["last_refresh_error_code"] == "invalid_grant"


# --- agent-scoped enablement ----------------------------------------------


@pytest.mark.asyncio
async def test_agent_enable_disable_and_replace(client):
    inst, _ = await _install(client)
    agent_id = await _new_agent(client)

    g = await client.get(f"/api/agents/{agent_id}/connectors", headers=client.headers)
    assert g.json()["installation_ids"] == []

    t = await client.patch(
        f"/api/agents/{agent_id}/connectors/{inst['id']}",
        json={"enabled": True},
        headers=client.headers,
    )
    assert t.status_code == 204
    g = await client.get(f"/api/agents/{agent_id}/connectors", headers=client.headers)
    assert g.json()["installation_ids"] == [inst["id"]]

    p = await client.put(
        f"/api/agents/{agent_id}/connectors",
        json={"installation_ids": []},
        headers=client.headers,
    )
    assert p.status_code == 200
    assert p.json()["installation_ids"] == []


@pytest.mark.asyncio
async def test_agent_enable_unknown_installation(client):
    agent_id = await _new_agent(client)
    t = await client.patch(
        f"/api/agents/{agent_id}/connectors/ghost",
        json={"enabled": True},
        headers=client.headers,
    )
    assert t.status_code == 404

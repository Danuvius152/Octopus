"""Connector token facts survive provider, callback and MCP consumption."""

import asyncio
import time

import httpx
import pytest

from server.connector_manager import ConnectorError
from server.connectors.gmail import GmailOAuthProvider
from server.mcp_servers.connectors import custom, github, gmail
from server.oauth_providers import OAuthTokenSet
from server.routers import connectors as routes
from tests.test_connector_manager import (
    clean_registry as clean_registry,
)
from tests.test_connector_manager import (
    db as db,
)
from tests.test_connector_manager import (
    mgr as mgr,
)
from tests.test_connector_manager import (
    owner as owner,
)
from tests.test_connector_manager import (
    provider as provider,
)
from tests.test_router_connectors import client as client


@pytest.mark.parametrize("status,body,category", [
    (502, None, "temporary"), (429, {"error": "invalid_grant"}, "temporary"),
    (503, {"error": "invalid_grant"}, "temporary"),
    (400, {"error": "invalid_grant"}, "reconnect"),
    (400, {"error": "invalid_client"}, "configuration"),
    (403, {"error": "access_denied"}, "permission"),
])
async def test_refresh_preserves_http_facts_before_classification(mgr, owner, monkeypatch, status, body, category):
    original_client = httpx.AsyncClient
    response = httpx.Response(status, json=body) if body else httpx.Response(status, text="<html>gateway failure</html>")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(lambda req: response)))
    (await mgr.get("fakekind")).oauth.refresh = GmailOAuthProvider().refresh
    installation = await mgr.complete_install(kind="fakekind", user_id=owner["id"],
        token_set=OAuthTokenSet("old", "refresh", time.time() - 1))
    with pytest.raises(ConnectorError) as failed:
        await mgr.get_access_token(installation["id"], owner["id"])
    current = await mgr.db.get_connector_installation(installation["id"])
    assert current["needs_reconnect"] == (category == "reconnect")
    assert getattr(failed.value, "kind", None) == category


@pytest.mark.parametrize("consumer", [github, gmail, custom], ids=["github", "gmail", "custom"])
@pytest.mark.parametrize("change", ["replace", "none", "delete", "disable", "concurrent"])
async def test_each_consumer_reports_only_its_own_grant(client, monkeypatch, consumer, change):
    manager = routes._require_manager()
    owner = (await manager.db.get_system_agent())["user_id"]
    from server.connectors.custom import CustomConnector

    async def identity(self, tokens):
        return "stable-external-account", "grant test"

    monkeypatch.setattr(CustomConnector, "fetch_external_identity", identity)
    await manager.create_custom_connector(kind="grant-test", display_name="grant test", api_base="https://resource.invalid/v1",
        authorize_url="https://oauth.invalid/auth", token_url="https://oauth.invalid/token", scopes=[], pkce=False,
        client_id="client", client_secret="secret")
    inst = await manager.complete_install(kind="grant-test", token_set=OAuthTokenSet("A", None, time.time() + 3600), user_id=owner)
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://test")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", client.headers["Authorization"].removeprefix("Bearer "))
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", inst["id"])
    before = None
    calls = []

    def callback(method, url, **kwargs):
        return asyncio.run_coroutine_threadsafe(client.request(method, url, **kwargs), loop).result(3)

    async def change_authority():
        nonlocal before
        if change in {"replace", "concurrent"}:
            updated = await manager.complete_install(kind="grant-test",
                token_set=OAuthTokenSet("B", None, time.time() + 3600), user_id=owner)
            assert updated["id"] == inst["id"]
            if change == "concurrent":
                result = await asyncio.to_thread(tool)
                assert not result.startswith("Error:")
        elif change == "delete":
            await manager.delete_installation(inst["id"], owner)
        elif change == "disable":
            await manager.db.update_user_field(owner, disabled_at="now")
        before = await manager.db.get_connector_installation(inst["id"])

    def resource(method, url, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        if kwargs["headers"]["Authorization"] == "Bearer B":
            return httpx.Response(200, json=[] if consumer is github else {"labels": [], "ok": True})
        asyncio.run_coroutine_threadsafe(change_authority(), loop).result(3)
        return httpx.Response(401)

    monkeypatch.setattr(httpx, "get", lambda url, **kw: callback("GET", url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: callback("POST", url, **kw))
    monkeypatch.setattr(httpx, "request", resource)
    tool = github.list_repos if consumer is github else gmail.list_labels if consumer is gmail else lambda: custom.request("GET", "/items")
    output = await asyncio.to_thread(tool)
    assert calls == (["Bearer A", "Bearer B"] if change == "concurrent" else ["Bearer A"])
    after = await manager.db.get_connector_installation(inst["id"])
    if change == "none":
        assert after["needs_reconnect"] and "reconnect" in output.lower()
    else:
        assert after == before and "reconnect" not in output.lower()
    if change in {"replace", "concurrent"}:
        assert (await manager.get_access_token(inst["id"], owner))["access_token"] == "B"

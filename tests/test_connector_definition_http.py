"""Two real loopback providers observe which generation's credentials are sent."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.connector_manager import ConnectorError, ConnectorManager
from server.connectors.oauth import ConnectorLoginManager
from server.database import Database
from server.oauth_providers import OAuthTokenSet
from server.routers import connectors
from tests.accounts import create_account


class ProviderPeer:
    def __init__(self, loop):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.release.set()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                peer.requests.append((self.path, fields))
                loop.call_soon_threadsafe(peer.entered.set)
                if not peer.release.wait(5):
                    self.send_error(504)
                    return
                body = json.dumps({"access_token": "rotated", "refresh_token": "rotated-refresh", "expires_in": 3600}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    async def close(self):
        self.release.set()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()


@pytest.fixture
async def http_definitions(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    db = Database(str(tmp_path / "definitions.db"))
    await db.initialize()
    owner, bearer = await create_account(db)
    manager = ConnectorManager(db)
    monkeypatch.setattr(connectors, "_manager", manager)
    monkeypatch.setattr(connectors, "_login_mgr", ConnectorLoginManager())
    old, replacement = (ProviderPeer(asyncio.get_running_loop()) for _ in range(2))
    app = FastAPI()
    app.include_router(connectors.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Authorization": "Bearer " + bearer}) as client:
            yield client, manager, owner, old, replacement
    finally:
        await asyncio.gather(old.close(), replacement.close())
        await db.close()


async def create(client, peer, generation):
    response = await client.post("/api/connectors/custom", json={
        "kind": "http-kind", "display_name": "HTTP provider", "authorize_url": peer.url + "/authorize",
        "token_url": peer.url + "/token", "api_base": peer.url + "/api", "scopes": [], "pkce": False,
        "client_id": generation + "-id", "client_secret": generation + "-secret",
    })
    assert response.status_code == 201, response.text


async def begin(client):
    response = await client.post("/api/connectors/oauth/start", json={"kind": "http-kind"})
    assert response.status_code == 201, response.text
    login_id = response.json()["login_id"]
    login = connectors._login_mgr.get(login_id)
    return login_id, {"code": "original", "state": f"{login_id}:{login.state}"}


async def test_deleted_pending_never_posts_new_secret_to_old_http_provider(http_definitions):
    client, _, _, old, replacement = http_definitions
    await create(client, old, "old")
    login_id, params = await begin(client)
    assert (await client.delete("/api/connectors/custom/http-kind")).status_code == 204
    await create(client, replacement, "new")
    assert "Connection failed" in (await client.get("/api/connectors/oauth/callback", params=params)).text
    assert old.requests == replacement.requests == []
    assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == "error"
    _, current = await begin(client)
    assert "Connected" in (await client.get("/api/connectors/oauth/callback", params=current)).text
    assert old.requests == []
    assert len(replacement.requests) == 1
    path, fields = replacement.requests[0]
    assert path == "/token" and fields["client_id"] == ["new-id"] and fields["client_secret"] == ["new-secret"]


@pytest.mark.parametrize("operation", ["callback", "refresh"])
async def test_inflight_http_request_keeps_old_credentials_and_cannot_bind_replacement(http_definitions, operation):
    client, manager, owner, old, replacement = http_definitions
    await create(client, old, "old")
    old.release.clear()
    if operation == "callback":
        login_id, params = await begin(client)
        work = asyncio.create_task(client.get("/api/connectors/oauth/callback", params=params))
    else:
        installation = await manager.complete_install(kind="http-kind", user_id=owner["id"],
            token_set=OAuthTokenSet("old-access", "old-refresh", time.time() + 1))
        work = asyncio.create_task(manager.get_access_token(installation["id"], owner["id"]))
    try:
        await asyncio.wait_for(old.entered.wait(), 3)
        assert (await client.delete("/api/connectors/custom/http-kind")).status_code == 204
        await create(client, replacement, "new")
        assert len(old.requests) == 1 and replacement.requests == []
        path, fields = old.requests[0]
        assert path == "/token" and fields["client_id"] == ["old-id"] and fields["client_secret"] == ["old-secret"]
        if operation == "refresh":
            assert fields["refresh_token"] == ["old-refresh"]
        old.release.set()
        if operation == "callback":
            assert "Connection failed" in (await work).text
            assert (await client.get(f"/api/connectors/oauth/status/{login_id}")).json()["status"] == "error"
        else:
            with pytest.raises(ConnectorError):
                await work
            assert await manager.db.get_connector_secret(installation["id"]) is None
        assert (await client.get("/api/connectors")).json() == []
        assert replacement.requests == []
    finally:
        old.release.set()
        await asyncio.gather(work, return_exceptions=True)

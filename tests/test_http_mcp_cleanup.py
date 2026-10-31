"""HTTP wrappers must not break MCP transport retirement or client disconnects."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from server import deps, mcp_http
from server.database import Database
from server.main import _HTTPResponses
from server.main import app as main_app
from server.mcp_identity import mint
from server.users import UserManager
from tests.callback_api import CallbackApi, _own_namespaces, _seize_sse_shutdown_latch


@pytest.fixture
async def mcp_http_app():
    database = Database(":memory:")
    await database.initialize()
    users = UserManager(database)
    owner = await users.create_user(username="http-cleanup", password="test-password")
    deps.set_user_manager(users)
    app = FastAPI()
    for middleware in reversed(main_app.user_middleware):
        app.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)

    @app.get("/api/large")
    @app.get("/mcpish/large")
    async def large():
        return {"body": "preserved response é\n" * 512}

    @app.get("/api/broken")
    async def broken():
        raise ValueError("observable HTTP failure")

    @app.get("/api/metric/{status_code}")
    async def metric(request: Request, status_code: int, long_poll: bool = False):
        request.state.long_poll = long_poll
        return JSONResponse({"status": status_code}, status_code=status_code,
                            headers={"Cache-Control": "public, max-age=3600"})

    servers = {"cleanup": FastMCP("HTTP cleanup")}
    mcp_http.mount_all(app, servers)
    headers = {
        "Authorization": "Bearer " + mint("cleanup", user_id=owner["id"]),
        "Accept": "application/json, text/event-stream",
    }
    ready, release = asyncio.Event(), asyncio.Event()
    owner_task = asyncio.create_task(_own_namespaces(app, servers, ready, release))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765", headers=headers) as client:
            reply = await client.post("/mcp/cleanup/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "cleanup-regression", "version": "1"}},
            })
            assert reply.status_code == 200
            client.headers["mcp-session-id"] = reply.headers["mcp-session-id"]
            yield app, client, servers["cleanup"].session_manager
    finally:
        release.set()
        try:
            await asyncio.wait_for(owner_task, 3)
        finally:
            await database.close()


async def test_mcp_delete_returns_a_response_and_retires_the_transport(mcp_http_app):
    _, client, manager = mcp_http_app
    identity = client.headers["mcp-session-id"]
    reply = await client.delete("/mcp/cleanup/mcp")
    assert reply.status_code == 200
    assert identity not in manager._server_instances
    assert (await client.delete("/mcp/cleanup/mcp")).status_code == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("ending", ["disconnect", "shutdown"])
@pytest.mark.parametrize("root_path", ["", "/octopus"])
@pytest.mark.parametrize("encoding", ["gzip", "identity"])
async def test_mcp_sse_early_disconnect_does_not_raise_missing_response(mcp_http_app, method, ending, root_path, encoding, monkeypatch):
    app, client, _ = mcp_http_app
    messages = []
    incoming = asyncio.Queue()
    body = b"" if method == "GET" else json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "aborted-opening", "version": "1"}},
    }).encode()
    await incoming.put({"type": "http.request", "body": body, "more_body": False})
    if ending == "disconnect":
        await incoming.put({"type": "http.disconnect"})
    else:
        from sse_starlette.sse import AppStatus
        monkeypatch.setattr(AppStatus, "should_exit", True)

    async def send(message):
        messages.append(message)

    scope = _sse_scope(client, method, root_path, encoding)
    await asyncio.wait_for(app(scope, incoming.get, send), timeout=3)
    assert messages[0]["status"] == 200
    assert dict(messages[0]["headers"])[b"content-type"].startswith(b"text/event-stream")
    assert b"content-encoding" not in dict(messages[0]["headers"])


def _sse_scope(client, method, root_path, encoding):
    path = root_path + "/mcp/cleanup/mcp"
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": root_path,
        "headers": [
            (key.lower(), value) for key, value in client.headers.raw
            if key.lower() != b"accept-encoding" and (method == "GET" or key.lower() != b"mcp-session-id")
        ] + [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8765"),
             (b"accept-encoding", encoding.encode())],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
    }


@pytest.mark.parametrize("root_path", ["", "/octopus"])
async def test_idle_mcp_get_sends_headers_before_any_event(mcp_http_app, root_path):
    app, client, manager = mcp_http_app
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    headers_sent = asyncio.Event()
    messages = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            headers_sent.set()

    request = asyncio.create_task(app(_sse_scope(client, "GET", root_path, "gzip"), incoming.get, send))
    try:
        await asyncio.wait_for(headers_sent.wait(), 3)
        assert messages[0]["status"] == 200
        assert not [message for message in messages if message.get("body")]
    finally:
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(request, 3)
    assert (await client.delete("/mcp/cleanup/mcp")).status_code == 200
    assert not manager._server_instances


@pytest.mark.parametrize("path", ["/api/large", "/mcpish/large"])
async def test_non_mcp_responses_keep_real_compression(mcp_http_app, path):
    _, client, _ = mcp_http_app
    compressed = await client.get(path, headers={"Accept-Encoding": "gzip"})
    plain = await client.get(path, headers={"Accept-Encoding": "identity"})
    assert compressed.status_code == plain.status_code == 200
    assert compressed.json() == plain.json()
    assert compressed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert compressed.num_bytes_downloaded < len(plain.content) // 5


async def test_actual_http_errors_still_propagate(mcp_http_app):
    _, client, _ = mcp_http_app
    with pytest.raises(ValueError, match="observable HTTP failure"):
        await client.get("/api/broken")


@pytest.mark.parametrize("status_code,long_poll,error_code,ok", [
    (200, False, None, True), (404, False, "404", True),
    (408, False, "408", True), (408, True, None, True),
    (500, False, "500", False),
])
async def test_http_response_policy_retains_metrics_and_cache_rules(mcp_http_app, monkeypatch, status_code, long_poll, error_code, ok):
    _, client, _ = mcp_http_app
    events = []
    monkeypatch.setattr("server.main._mon_record", events.append)
    response = await client.get(f"/api/metric/{status_code}", params={"long_poll": str(long_poll).lower()})
    assert response.status_code == status_code
    assert response.headers["cache-control"] == ("no-store" if status_code == 404 else "public, max-age=3600")
    assert len(events) == 1
    event = events[0]
    assert event.kind == "http" and event.duration_ms >= 0
    assert event.ok is ok and event.error_code == error_code
    assert event.detail == {"route": "/api/metric/{status_code}", "method": "GET", **({"long_poll": True} if long_poll else {})}


async def test_metric_is_recorded_at_the_first_header_before_the_body(monkeypatch):
    headers_sent, release = asyncio.Event(), asyncio.Event()
    events, messages = [], []
    monkeypatch.setattr("server.main._mon_record", events.append)

    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await release.wait()
        await send({"type": "http.response.body", "body": b"retained"})

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            headers_sent.set()

    async def receive():
        raise AssertionError("This response policy must not consume the request")

    task = asyncio.create_task(_HTTPResponses(application)(
        {"type": "http", "path": "/untemplated", "method": "POST"}, receive, send,
    ))
    try:
        await asyncio.wait_for(headers_sent.wait(), 3)
        assert not task.done() and len(messages) == len(events) == 1
        assert events[0].detail == {"route": "/untemplated", "method": "POST"}
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert messages[1]["body"] == b"retained" and len(events) == 1


@pytest.mark.parametrize("after_headers", [False, True])
async def test_response_policy_preserves_failures_before_and_after_headers(monkeypatch, after_headers):
    events, messages = [], []
    monkeypatch.setattr("server.main._mon_record", events.append)
    error = ValueError("original application failure")

    async def application(scope, receive, send):
        if after_headers:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise error

    async def receive():
        raise AssertionError("This response policy must not consume the request")

    async def send(message):
        messages.append(message)

    with pytest.raises(ValueError) as failure:
        await _HTTPResponses(application)({"type": "http", "path": "/fault", "method": "GET"}, receive, send)
    assert failure.value is error
    assert len(messages) == len(events) == int(after_headers)
    if events:
        assert events[0].ok and events[0].error_code is None


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
async def test_non_http_messages_are_transparent(monkeypatch, kind):
    events, messages = [], []
    monkeypatch.setattr("server.main._mon_record", events.append)
    incoming = {"type": kind + (".receive" if kind == "websocket" else ".startup")}
    outgoing = {"type": kind + (".close" if kind == "websocket" else ".startup.complete")}

    async def application(scope, receive, send):
        assert scope == {"type": kind}
        assert await receive() is incoming
        await send(outgoing)

    async def receive():
        return incoming

    async def send(message):
        messages.append(message)

    await _HTTPResponses(application)({"type": kind}, receive, send)
    assert messages == [outgoing] and not events


async def test_real_mcp_client_cleanup_has_no_http_application_errors(mcp_http_app, caplog):
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app, bootstrap, _ = mcp_http_app
    _seize_sse_shutdown_latch()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning", ws="none"))
    serving = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(.01)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    api = CallbackApi(port, app, server, serving, None, None)

    async def connect_and_close():
        async with httpx.AsyncClient(headers={"Authorization": bootstrap.headers["authorization"]}, timeout=3) as client:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp/cleanup/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()

    try:
        await asyncio.wait_for(asyncio.gather(*(connect_and_close() for _ in range(5))), 10)
    finally:
        await api.stop()
    assert not [r for r in caplog.records if r.name == "uvicorn.error" and r.exc_info]

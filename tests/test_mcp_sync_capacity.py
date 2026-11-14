"""Sync tool workers must leave capacity for their real HTTP callbacks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from dataclasses import asdict

import anyio.to_thread
import httpx
import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from server import deps, mcp_http
from server.config import settings
from server.database import Database
from server.mcp_identity import current_scope, mint
from server.mcp_servers import wiki as wiki_tools
from server.routers import wiki as wiki_routes
from server.users import UserManager
from server.wiki.store import WikiError
from tests.callback_api import start_callback_api


@contextlib.asynccontextmanager
async def capacity_host(monkeypatch, *, worker_fails=False):
    database = Database(":memory:")
    await database.initialize()
    users = UserManager(database)
    user = await users.create_user(username="capacity-owner", password="test-password")
    deps.set_user_manager(users)
    loop = asyncio.get_running_loop()
    full = asyncio.Event()
    release = threading.Event()
    lock = threading.Lock()
    started = []
    finished = []

    def probe(marker: str) -> dict:
        scope = current_scope()
        with lock:
            started.append(marker)
            if len(started) == 40:
                loop.call_soon_threadsafe(full.set)
        release.wait()
        try:
            # The production blocking adapter, authenticated HTTP and sync
            # Wiki route all run. This diagnostic scope has no Wiki task, so
            # the real route must return its ordinary authorization refusal.
            reply = wiki_tools.call("GET", "config")
            if worker_fails and marker == "0":
                raise RuntimeError("original worker failed after its callback")
            return {"marker": marker, "scope": asdict(scope), "reply": reply}
        finally:
            with lock:
                finished.append(marker)

    servers = {name: FastMCP(name) for name in ("builtin", "connector")}
    for server in servers.values():
        server.add_tool(probe, name="probe")
    monkeypatch.setattr(mcp_http, "_BUILTIN_MODULES", {"builtin": "builtin"})
    monkeypatch.setattr(mcp_http, "_CONNECTOR_MODULES", {"connector": "connector"})
    monkeypatch.setattr(mcp_http, "_load", lambda path, **kwargs: servers[path])
    ordinary = APIRouter()

    @ordinary.get("/ordinary")
    def ordinary_sync_route():
        return {"ok": True}

    async def wiki_error(request: Request, exc: WikiError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    token = mint("capacity", user_id=user["id"])
    api = await start_callback_api(
        ordinary, wiki_routes.router, readiness_token=token,
        exception_handlers={WikiError: wiki_error},
    )
    monkeypatch.setattr(settings, "port", api.port)
    default = anyio.to_thread.current_default_thread_limiter()
    original_capacity = default.total_tokens
    assert original_capacity == 40
    try:
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for name in servers:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}, timeout=10,
                ))
                read, write, _ = await stack.enter_async_context(streamable_http_client(
                    f"http://127.0.0.1:{api.port}/mcp/{name}/mcp", http_client=client,
                ))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions.append(session)
            try:
                yield api, sessions, full, release, started, finished, user["id"]
            finally:
                release.set()
                # Only failed-test teardown breaks a baseline deadlock. No
                # success assertion runs with this extra callback capacity.
                if len(finished) != len(started):
                    default.total_tokens = original_capacity + 40
                async with asyncio.timeout(10):
                    while len(finished) != len(started):
                        await asyncio.sleep(.01)
    finally:
        default.total_tokens = original_capacity
        await api.stop()
        await database.close()


async def test_full_sync_mcp_capacity_can_call_sync_routes_and_ordinary_api(monkeypatch):
    async with capacity_host(monkeypatch) as host:
        await full_capacity_callbacks(host)


async def full_capacity_callbacks(capacity_host):
    api, sessions, full, release, started, _, user_id = capacity_host
    calls = [asyncio.create_task(sessions[i % 2].call_tool("probe", {"marker": str(i)}))
             for i in range(40)]
    try:
        await asyncio.wait_for(full.wait(), 5)
        assert len(started) == 40
        # Ordinary sync work can progress even while every outer worker waits.
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"http://127.0.0.1:{api.port}/ordinary")
            assert response.json() == {"ok": True}
        release.set()
        async with asyncio.timeout(10):
            results = await asyncio.gather(*calls)
        for i, result in enumerate(results):
            assert not result.isError
            value = json.loads(result.content[0].text)
            assert value["marker"] == str(i)
            assert value["scope"]["session_id"] == "capacity"
            assert value["scope"]["user_id"] == user_id
            assert value["reply"]["status"] == 403
    finally:
        # The fixture releases the real workers before closing their transports.
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_namespaces_share_one_bound_and_cancelled_queue_never_starts(monkeypatch, worker_fails):
    async with capacity_host(monkeypatch, worker_fails=worker_fails) as host:
        await cancelled_queue(host)


async def cancelled_queue(capacity_host):
    _, sessions, full, release, started, finished, _ = capacity_host
    first_rpc_id = sessions[0]._request_id
    calls = [asyncio.create_task(sessions[0].call_tool("probe", {"marker": str(i)}))
             for i in range(40)]
    queued = replacement = None
    try:
        await asyncio.wait_for(full.wait(), 5)
        rpc_id = sessions[1]._request_id
        queued = asyncio.create_task(sessions[1].call_tool("probe", {"marker": "cancelled"}))
        async with asyncio.timeout(3):
            while sessions[1]._request_id == rpc_id:
                await asyncio.sleep(0)
        # A later request on the same transport confirms the server is serving
        # while the extra call waits for shared worker capacity.
        await sessions[1].list_tools()
        assert len(started) == 40
        await sessions[1].send_notification(types.ClientNotification(types.CancelledNotification(
            method="notifications/cancelled",
            params=types.CancelledNotificationParams(requestId=rpc_id),
        )))
        with pytest.raises(McpError, match="Request cancelled"):
            await asyncio.wait_for(queued, 3)

        # Cancelling a running call replies promptly but must not abandon its
        # original effect or let another worker consume the occupied slot.
        await sessions[0].send_notification(types.ClientNotification(types.CancelledNotification(
            method="notifications/cancelled",
            params=types.CancelledNotificationParams(requestId=first_rpc_id),
        )))
        with pytest.raises(McpError, match="Request cancelled"):
            await asyncio.wait_for(calls[0], 3)
        assert not finished
        replacement_id = sessions[1]._request_id
        replacement = asyncio.create_task(sessions[1].call_tool("probe", {"marker": "after-cancel"}))
        async with asyncio.timeout(3):
            while sessions[1]._request_id == replacement_id:
                await asyncio.sleep(0)
        await sessions[1].list_tools()
        assert len(started) == 40
        release.set()
        await asyncio.gather(*calls[1:], replacement)
        assert len(started) == len(finished) == 41
        assert "cancelled" not in started
    finally:
        pending = [*calls, *(call for call in (queued, replacement) if call is not None)]
        for call in pending:
            call.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

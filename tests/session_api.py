"""Authenticated HTTP and ASGI socket clients for public session scenarios."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from server import deps
from server.deps import get_session_manager
from server.routers.sessions import router as sessions_router
from server.routers.ws import router as ws_router


@asynccontextmanager
async def session_client(manager, session):
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {bearer}"}) as client:
        yield client


@asynccontextmanager
async def session_socket(manager, session):
    app = FastAPI()
    app.include_router(ws_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws", "raw_path": b"/ws",
        "query_string": f"token={bearer}".encode(), "headers": [], "subprotocols": [],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"

    async def submit(payload):
        await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "send_message", **payload})})
        async with asyncio.timeout(3):
            while True:
                frame = await outgoing.get()
                assert frame["type"] == "websocket.send"
                value = json.loads(frame["text"])
                if value.get("type") == "submission":
                    return value

    try:
        yield submit
    finally:
        if not task.done():
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 3)

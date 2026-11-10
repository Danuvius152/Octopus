"""An ASGI browser socket whose transport can be held at the real send seam."""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.deps import get_session_manager
from server.routers.ws import router


@asynccontextmanager
async def connection(manager, token, *, release=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    entered = asyncio.Event()

    async def send(message):
        if message["type"] == "websocket.send" and release is not None:
            entered.set()
            await release.wait()
        await outgoing.put(message)

    await incoming.put({"type": "websocket.connect"})
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws", "raw_path": b"/ws", "headers": [],
        "query_string": f"token={token}".encode(), "subprotocols": [],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    assert (await asyncio.wait_for(outgoing.get(), 1))["type"] == "websocket.accept"
    try:
        yield incoming, outgoing, task, entered
    finally:
        if release is not None:
            release.set()
        if not task.done():
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 2)


async def frame(outgoing):
    message = await asyncio.wait_for(outgoing.get(), 1)
    assert message["type"] == "websocket.send"
    return json.loads(message["text"])


async def receipt_frame(outgoing, kind):
    while True:
        result = await frame(outgoing)
        if result.get("type") == kind:
            return result

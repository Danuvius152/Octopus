"""A callback host that fails startup must not outlive its missing handle."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import uvicorn
from sse_starlette.sse import AppStatus

from server import mcp_http
from tests import callback_api


@pytest.fixture
def startup_owners(monkeypatch):
    servers, owners = [], []
    original_server = uvicorn.Server
    original_owner = callback_api._own_namespaces

    class TrackedServer(original_server):
        serve_task = None

        def __init__(self, config):
            super().__init__(config)
            servers.append(self)

        async def serve(self, *args, **kwargs):
            self.serve_task = asyncio.current_task()
            await super().serve(*args, **kwargs)

    async def track_owner(*args):
        owners.append(asyncio.current_task())
        await original_owner(*args)

    monkeypatch.setattr(uvicorn, "Server", TrackedServer)
    monkeypatch.setattr(callback_api, "_own_namespaces", track_owner)
    return servers, owners


@pytest.mark.parametrize("websocket", [False, True])
async def test_namespace_readiness_failure_closes_listener_and_owner(
    monkeypatch, startup_owners, websocket,
):
    servers, owners = startup_owners
    tasks_before = set(asyncio.all_tasks())
    failure = RuntimeError("controlled namespace readiness failure")
    port = None

    async def fail_readiness(listening_port, namespaces):
        nonlocal port
        port = listening_port
        assert namespaces and owners and not owners[0].done()
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        raise failure

    monkeypatch.setattr(callback_api, "_await_namespaces", fail_readiness)
    with pytest.raises(RuntimeError) as caught:
        await callback_api.start_callback_api(websocket=websocket)

    assert caught.value is failure
    assert len(servers) == len(owners) == 1
    assert servers[0].serve_task.done() and owners[0].done()
    assert all(not listener.is_serving() for listener in servers[0].servers)
    assert servers[0].config.ws == ("auto" if websocket else "none")
    assert AppStatus.should_exit is False
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    assert not (set(asyncio.all_tasks()) - tasks_before)


async def test_namespace_owner_startup_failure_preserves_cause_and_unwinds(
    monkeypatch, startup_owners,
):
    servers, owners = startup_owners
    tasks_before = set(asyncio.all_tasks())
    failure = RuntimeError("controlled namespace owner failure")
    released = asyncio.Event()

    @asynccontextmanager
    async def fail_lifespan(app, namespaces):
        try:
            raise failure
            yield  # pragma: no cover - contextmanager protocol only
        finally:
            released.set()

    monkeypatch.setattr(mcp_http, "lifespan", fail_lifespan)
    with pytest.raises(RuntimeError) as caught:
        await asyncio.wait_for(callback_api.start_callback_api(), timeout=2)

    assert caught.value is failure
    assert released.is_set()
    assert len(servers) == len(owners) == 1
    assert servers[0].serve_task is None and owners[0].done()
    assert AppStatus.should_exit is False
    assert not (set(asyncio.all_tasks()) - tasks_before)

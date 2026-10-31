"""ASGI ownership ends at application completion or the next WS input wait."""

import asyncio
from types import SimpleNamespace

from server.installation_admission import InstallationAdmission
from server.service_health import ServiceHealth


async def test_http_headers_do_not_release_the_original_application():
    entered, release = asyncio.Event(), asyncio.Event()
    health = ServiceHealth()
    health.open_admission()
    state = SimpleNamespace(service_health=health)

    async def response(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        entered.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"final"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(_message):
        pass

    gate = InstallationAdmission(response)
    request = asyncio.create_task(gate({"type": "http", "method": "GET", "path": "/stream", "app": SimpleNamespace(state=state)}, receive, send))
    try:
        await entered.wait()
        assert not gate.idle
    finally:
        release.set()
        await request
    assert gate.idle


async def test_idle_websocket_does_not_block_handoff_but_processing_its_input_does():
    messages = asyncio.Queue()
    waiting, handling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(service_health=ServiceHealth())
    state.service_health.open_admission()

    async def socket(scope, receive, send):
        waiting.set()
        message = await receive()
        assert message["type"] == "websocket.receive"
        handling.set()
        await release.wait()

    async def send(_message):
        pass

    gate = InstallationAdmission(socket)
    connection = asyncio.create_task(gate({"type": "websocket", "path": "/ws", "app": SimpleNamespace(state=state)}, messages.get, send))
    try:
        await waiting.wait()
        assert gate.idle
        messages.put_nowait({"type": "websocket.receive", "text": "command"})
        await handling.wait()
        assert not gate.idle
    finally:
        release.set()
        await connection
    assert gate.idle

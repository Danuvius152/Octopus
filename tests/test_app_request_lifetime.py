"""Real proxy/process boundaries for original-target admission and retirement."""

import asyncio
import os
import signal

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request

from server import app_backends
from server.app_backends import BackendCapacityError, BackendSupervisor
from server.routers import applications as route
from tests.app_proxy import application


def request_for(body_ready: asyncio.Event, release: asyncio.Event) -> Request:
    async def receive():
        body_ready.set()
        await release.wait()
        return {"type": "http.request", "body": b"original effect", "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/apps/a/api/effect",
        "headers": [(b"content-length", b"15")], "query_string": b"",
        "asgi": {"spec_version": "2.4"},
    }, receive)


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    supervisor = BackendSupervisor()
    directory = application(tmp_path, "a")

    async def authorized(*args):
        return {"id": "a", "app_dir": str(directory)}

    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .2)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    return supervisor, directory


async def test_original_body_is_not_sent_to_a_reused_port(proxy):
    supervisor, _ = proxy
    ready, release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(route.proxy_application_api(request_for(ready, release), "a", "effect"))
    replacement = None
    received = []

    async def stranger(reader, writer):
        received.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    try:
        await asyncio.wait_for(ready.wait(), 3)
        state = supervisor.get("a")
        assert state is not None and state.process is not None and state.port is not None
        port = state.port
        os.killpg(state.process.pid, signal.SIGKILL)
        await state.process.wait()
        replacement = await asyncio.start_server(stranger, "127.0.0.1", port)
        release.set()
        with pytest.raises(HTTPException) as refused:
            await pending
        assert refused.value.status_code == 503
        assert received == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if replacement is not None:
            replacement.close()
            await replacement.wait_closed()
        await supervisor.shutdown()


async def test_active_proxy_protects_capacity_and_drain_until_response_cleanup(proxy, tmp_path):
    supervisor, directory = proxy
    other = application(tmp_path, "b")
    ready, release = asyncio.Event(), asyncio.Event()
    request = request_for(ready, release)
    pending = asyncio.create_task(route.proxy_application_api(request, "a", "effect"))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        assert supervisor.drain_blockers()
        with pytest.raises(BackendCapacityError):
            await supervisor.ensure_running("b", str(other))
        release.set()
        response = await pending
        assert directory.joinpath("received").read_bytes() == b"original effect"
        assert supervisor.drain_blockers(), "Returning headers must retain the original use"
        with pytest.raises(BackendCapacityError):
            await supervisor.ensure_running("b", str(other))
        messages = []

        async def send(message):
            messages.append(message)

        await response(request.scope, request.receive, send)
        assert b"".join(message.get("body", b"") for message in messages) == b"ok"
        assert not supervisor.drain_blockers()
        assert (await supervisor.ensure_running("b", str(other))).process is not None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await supervisor.shutdown()


@pytest.mark.parametrize("phase", ["body", "unstarted", "streaming"])
@pytest.mark.parametrize("control", ["stop", "rebuild", "delete", "shutdown"])
async def test_control_closes_admission_and_retires_original_request(proxy, phase, control):
    supervisor, directory = proxy
    ready, release = asyncio.Event(), asyncio.Event()
    request = request_for(ready, release)
    pending = asyncio.create_task(route.proxy_application_api(request, "a", "effect"))
    serving = None
    try:
        await asyncio.wait_for(ready.wait(), 3)
        original = supervisor.get("a").process
        if phase != "body":
            release.set()
            response = await pending
            if phase == "streaming":
                sending = asyncio.Event()

                async def send(message):
                    sending.set()
                    await asyncio.Event().wait()

                serving = asyncio.create_task(response(request.scope, request.receive, send))
                await sending.wait()
        if control == "delete":
            supervisor.close_application("a")
        if control == "rebuild":
            stopping = asyncio.create_task(supervisor.on_rebuild("a", str(directory)))
        elif control == "shutdown":
            stopping = asyncio.create_task(supervisor.shutdown())
        else:
            stopping = asyncio.create_task(supervisor.stop("a"))
        await asyncio.wait_for(stopping, 3)
        assert original.returncode is not None
        assert not supervisor.drain_blockers()
        assert supervisor.get("a").process is None
        if phase == "body":
            assert pending.cancelled()
            assert not (directory / "received").exists()
        elif phase == "streaming":
            assert serving.cancelled()
        if control in {"stop", "rebuild"}:
            assert (await supervisor.ensure_running("a", str(directory))).process is not original
        else:
            with pytest.raises(RuntimeError):
                await supervisor.ensure_running("a", str(directory))
    finally:
        release.set()
        await asyncio.gather(pending, *(task for task in [serving] if task is not None), return_exceptions=True)
        await supervisor.shutdown()


async def test_header_send_failure_closes_uniterated_response_and_idle_becomes_reclaimable(proxy, monkeypatch):
    supervisor, _ = proxy
    ready, release = asyncio.Event(), asyncio.Event()
    release.set()
    request = request_for(ready, release)
    response = await route.proxy_application_api(request, "a", "effect")
    monkeypatch.setattr(app_backends, "IDLE_TIMEOUT_S", -1)

    async def send(message):
        raise OSError("downstream closed before headers")

    try:
        assert await supervisor.reap_idle() == 0
        with pytest.raises(ClientDisconnect):
            await response(request.scope, request.receive, send)
        assert not supervisor.drain_blockers()
        assert await supervisor.reap_idle() == 1
    finally:
        await supervisor.shutdown()


async def test_unstarted_transport_cleanup_timeout_keeps_original_slot_until_confirmed(proxy, monkeypatch):
    supervisor, directory = proxy
    closing, release = asyncio.Event(), asyncio.Event()
    real_client = httpx.AsyncClient

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, stream=Stream())

        async def aclose(self):
            closing.set()
            await release.wait()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=Transport(), **kwargs))
    ready, body = asyncio.Event(), asyncio.Event()
    body.set()
    await route.proxy_application_api(request_for(ready, body), "a", "effect")
    state = supervisor.get("a")
    original = state.process
    try:
        with pytest.raises(RuntimeError, match="request cleanup unconfirmed"):
            await asyncio.wait_for(supervisor.stop("a"), 2)
        assert closing.is_set() and original.returncode is not None
        assert state.process is original and supervisor.drain_blockers()
        with pytest.raises(BackendCapacityError):
            await supervisor.ensure_running("a", str(directory))
    finally:
        release.set()
    await supervisor.stop("a")
    assert state.process is None and not supervisor.drain_blockers()


@pytest.mark.parametrize("disconnect_before_headers", [True, False])
async def test_failed_response_close_still_closes_client_and_keeps_unconfirmed_retirement(tmp_path, monkeypatch, disconnect_before_headers):
    supervisor = BackendSupervisor()
    directory = application(tmp_path, "a")
    real_client = httpx.AsyncClient
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

        async def aclose(self):
            closed.append("response")
            raise OSError("upstream response cleanup unconfirmed")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, stream=Stream())

        async def aclose(self):
            closed.append("client")

    async def authorized(*args):
        return {"id": "a", "app_dir": str(directory)}

    async def send(message):
        if disconnect_before_headers:
            raise OSError("downstream disconnected before headers")

    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=Transport(), **kwargs))
    ready, release = asyncio.Event(), asyncio.Event()
    release.set()
    request = request_for(ready, release)
    response = await route.proxy_application_api(request, "a", "effect")
    with pytest.raises(OSError, match="response cleanup unconfirmed"):
        await response(request.scope, request.receive, send)
    assert closed == ["response", "client"]
    original = supervisor.get("a").process
    with pytest.raises(RuntimeError, match="request cleanup unconfirmed"):
        await supervisor.shutdown()
    assert original.returncode is not None
    assert supervisor.get("a").process is original
    assert supervisor.drain_blockers()


async def test_original_response_can_finish_after_backend_exit_without_another_port_send(proxy):
    supervisor, _ = proxy
    ready, release = asyncio.Event(), asyncio.Event()
    release.set()
    request = request_for(ready, release)
    response = await route.proxy_application_api(request, "a", "effect")
    process = supervisor.get("a").process
    os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
    messages = []

    async def send(message):
        messages.append(message)

    try:
        await response(request.scope, request.receive, send)
        assert b"".join(message.get("body", b"") for message in messages) == b"ok"
        assert not supervisor.drain_blockers()
    finally:
        await supervisor.shutdown()

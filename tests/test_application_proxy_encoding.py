"""The proxy's bytes and encoding headers describe the same representation."""

import asyncio
import gzip

import httpx
import pytest
from starlette.requests import Request

from server.app_backends import BackendSupervisor
from server.routers import applications as route
from server.wiki import app_bridge
from tests.app_proxy import application


@pytest.fixture
async def backend(tmp_path, monkeypatch):
    directory = application(tmp_path, "app")
    supervisor = BackendSupervisor()

    async def authorized(*args):
        return {"id": "app", "app_dir": str(directory)}

    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr("server.app_backends.backend_supervisor", supervisor)
    try:
        yield supervisor
    finally:
        await supervisor.shutdown()


async def test_ordinary_proxy_retains_transport_cleanup_through_repeated_cancellation(backend, monkeypatch):
    sending, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retired = []
    real_client = httpx.AsyncClient

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sending.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()
            retired.append(True)

    async def unmanaged(*args):
        return None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/apps/app/api/data",
                       "headers": [], "query_string": b""}, receive)
    monkeypatch.setattr(app_bridge, "begin", unmanaged)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=Transport(), **kwargs))
    calling = asyncio.create_task(route.proxy_application_api(request, "app", "data"))
    try:
        await asyncio.wait_for(sending.wait(), 2)
        calling.cancel("disconnected")
        await asyncio.wait_for(closing.wait(), 2)
        calling.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not calling.done(), "the original transport cleanup must still have an owner"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await calling
        assert retired == [True]
    finally:
        release.set()
        await asyncio.gather(calling, return_exceptions=True)


@pytest.mark.parametrize("encoded,mutation", [(True, False), (False, False), (True, True)])
async def test_proxy_encoding_matches_raw_stream_or_decoded_mutation(backend, monkeypatch, encoded, mutation):
    original = b'{"ok":true}'
    wire = gzip.compress(original) if encoded else original

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Preserve streaming boundaries as well as the final body.
            yield wire[:5]
            yield wire[5:]

    real_client = httpx.AsyncClient
    upstream_closed = []

    def upstream(request):
        headers = {"content-type": "application/json", "content-length": str(len(wire))}
        if encoded:
            headers["content-encoding"] = "gzip"
            headers["etag"] = '"compressed-representation"'
            headers["content-digest"] = "sha-256=:compressed-bytes:"
        return httpx.Response(200, headers=headers, stream=Stream())

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    class Mutation(app_bridge.AppMutation):
        def __init__(self):
            self.body = {}

        async def finish(self):
            upstream_closed.append("finished")

    async def begin(*args):
        return Mutation() if mutation else None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "POST" if mutation else "GET",
        "path": "/apps/app/api/data", "headers": [], "query_string": b"",
        "asgi": {"spec_version": "2.4"},
    }, receive)
    monkeypatch.setattr(app_bridge, "begin", begin)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    response = await route.proxy_application_api(request, "app", "data")
    if mutation:
        assert response.body == original
        assert response.headers.get("content-encoding") is None
        assert "etag" not in response.headers
        assert "content-digest" not in response.headers
        assert upstream_closed == ["finished"]
        assert response.headers["content-length"] == str(len(original))
    else:
        messages = []

        async def send(message):
            messages.append(message)

        await response(request.scope, request.receive, send)
        body = b"".join(message.get("body", b"") for message in messages)
        assert body == wire
        assert response.headers.get("content-encoding") == ("gzip" if encoded else None)
        if encoded:
            assert response.headers["etag"] == '"compressed-representation"'
            assert response.headers["content-digest"] == "sha-256=:compressed-bytes:"
        assert "content-length" not in response.headers
        assert (gzip.decompress(body) if encoded else body) == original

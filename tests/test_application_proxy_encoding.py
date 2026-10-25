"""The proxy's bytes and encoding headers describe the same representation."""

import gzip
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from server.routers import applications as route
from server.wiki import app_bridge


@pytest.mark.parametrize("encoded,mutation", [(True, False), (False, False), (True, True)])
async def test_proxy_encoding_matches_raw_stream_or_decoded_mutation(tmp_path, monkeypatch, encoded, mutation):
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

    async def authorized(*args):
        return {"app_dir": str(tmp_path), "id": "app"}

    async def running(*args):
        return SimpleNamespace(state=route.RUNNING, port=12345)

    class Mutation:
        async def finish(self):
            upstream_closed.append("finished")

    async def begin(*args):
        return Mutation() if mutation else None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "POST" if mutation else "GET",
        "path": "/apps/app/api/data", "headers": [], "query_string": b"",
    }, receive)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(route.backend_supervisor, "ensure_running", running)
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
        body = b"".join([chunk async for chunk in response.body_iterator])
        assert body == wire
        assert response.headers.get("content-encoding") == ("gzip" if encoded else None)
        if encoded:
            assert response.headers["etag"] == '"compressed-representation"'
            assert response.headers["content-digest"] == "sha-256=:compressed-bytes:"
        assert "content-length" not in response.headers
        assert (gzip.decompress(body) if encoded else body) == original

"""Controlled host/provider HTTP and clocks for real connector wrapper tests."""

import asyncio
from types import SimpleNamespace

import httpx
import httpx._api
import pytest

from server.mcp_servers.connectors import _shared, github


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def __init__(self):
            self.waits = []

        def monotonic(self):
            return self.now

        def time(self):
            return 1_700_000_000 + self.now

        async def sleep(self, delay):
            self.waits.append(delay)
            self.now += delay

    clock = Clock()
    monkeypatch.setattr(_shared, "time", clock)
    monkeypatch.setattr(github, "time", clock)
    monkeypatch.setattr(_shared, "asyncio", SimpleNamespace(sleep=clock.sleep, timeout=asyncio.timeout))
    return clock


@pytest.fixture
def provider_transport(monkeypatch):
    """Only replace host/provider HTTP; token resolution and policy stay real."""
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://host.invalid")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "host-token")
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "installation-original")
    original_sync, original_async = httpx.Client, httpx.AsyncClient

    def install(handler, *, token_handler=None):
        def route(request):
            if request.url.host == "host.invalid":
                if request.url.path.endswith("/token"):
                    if token_handler is not None:
                        return token_handler(request)
                    return httpx.Response(200, json={
                        "access_token": "provider-token", "grant_stamp": "original-grant",
                        "api_base": "https://custom.invalid/v1",
                    })
                assert request.url.path.endswith("/mark-needs-reconnect")
                assert request.content == b'{"grant_stamp":"original-grant"}'
                return httpx.Response(200, json={"disposition": "applied"})
            assert request.headers["Authorization"] == "Bearer provider-token"
            return handler(request)

        transport = httpx.MockTransport(route)
        monkeypatch.setattr(httpx, "Client", lambda **kw: original_sync(**{**kw, "transport": transport}))
        monkeypatch.setattr(httpx._api, "Client", httpx.Client)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_async(**{**kw, "transport": transport}))

    return install

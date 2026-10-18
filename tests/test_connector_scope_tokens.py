"""A shared MCP namespace must ask the host for each call's identity."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from server.mcp_identity import (
    McpScope,
    current_scope,
    reset_current_scope,
    set_current_scope,
)
from server.mcp_servers import _host
from server.mcp_servers.connectors._shared import ConnectorContext


@pytest.fixture
def scoped_host(monkeypatch):
    monkeypatch.delenv("OCTOPUS_INSTALLATION_ID", raising=False)
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://synthetic-host")
    monkeypatch.setattr(_host, "bearer", lambda: "synthetic-bearer")
    context = ConnectorContext()
    return context


def call(context, owner, installation):
    marker = set_current_scope(McpScope("synthetic-session", installation, owner))
    try:
        return context.access_token()
    finally:
        reset_current_scope(marker)


@pytest.mark.parametrize("second_owner", ["owner-A", "owner-B"])
def test_sequential_installations_and_secret_changes(scoped_host, monkeypatch, second_owner):
    requests = []
    tokens = {"install-A": "token-A", "install-B": "token-B"}

    def get(url, **kwargs):
        installation = url.split("/")[-2]
        requests.append((current_scope().user_id, installation))
        value = tokens.get(installation)
        return httpx.Response(200, json={"access_token": value}) if value else httpx.Response(404)

    monkeypatch.setattr(httpx, "get", get)
    assert call(scoped_host, "owner-A", "install-A") == "token-A"
    assert call(scoped_host, second_owner, "install-B") == "token-B"
    tokens["install-A"] = "replacement-A"
    assert call(scoped_host, "owner-A", "install-A") == "replacement-A"
    del tokens["install-A"]
    assert call(scoped_host, "owner-A", "install-A") is None
    assert scoped_host.access_token() is None  # No scope or diagnostic installation.
    assert requests == [("owner-A", "install-A"), (second_owner, "install-B"),
                        ("owner-A", "install-A"), ("owner-A", "install-A")]


def test_warmed_context_does_not_bypass_owner_denial(scoped_host, monkeypatch):
    def get(url, **kwargs):
        return (httpx.Response(200, json={"access_token": "token-A"})
                if current_scope().user_id == "owner-A" else httpx.Response(403))

    monkeypatch.setattr(httpx, "get", get)
    assert call(scoped_host, "owner-A", "install-A") == "token-A"
    assert call(scoped_host, "owner-B", "install-A") is None


def test_concurrent_calls_keep_their_request_scopes(scoped_host, monkeypatch):
    barrier = Barrier(4)

    def get(url, **kwargs):
        installation = url.split("/")[-2]
        owner = current_scope().user_id
        barrier.wait(timeout=3)
        return httpx.Response(200, json={"access_token": f"{owner}:{installation}"})

    monkeypatch.setattr(httpx, "get", get)
    identities = [("A", "one"), ("B", "two"), ("A", "three"), ("B", "four")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: call(scoped_host, *pair), identities))
    assert results == [f"{owner}:{installation}" for owner, installation in identities]

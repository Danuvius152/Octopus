"""Generic custom-connector MCP server (connectors.md custom-connectors): the
authenticated `request` tool. HTTP mocked by monkeypatching the module httpx;
the tool is a plain callable in this FastMCP version."""

from __future__ import annotations

import httpx
import pytest

from server.mcp_servers.connectors import custom as cu


class FakeResp:
    def __init__(self, status=200, json_body=None, text="", headers=None):
        self.status_code = status
        self._json = json_body
        self.text = text
        self.headers = headers or {}
        self.content = b"x" if (json_body is not None or text) else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(cu.ctx, "token_info", lambda: {
        "access_token": "tok", "api_base": "https://api.x.com/v1", "grant_stamp": "stamp",
    })
    recon: list[str] = []
    monkeypatch.setattr(
        cu.ctx, "mark_needs_reconnect", lambda stamp: recon.append(stamp) or "applied"
    )
    return recon


def _patch(monkeypatch, handler):
    async def send(self, method, url, **kwargs):
        return handler(method, url, follow_redirects=self.follow_redirects, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "request", send)


async def test_request_get_projects_url_and_auth(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, headers=None, params=None, json=None, timeout=None, follow_redirects=None):
        cap.update(method=method, url=url, headers=headers, params=params, json=json)
        assert follow_redirects is False
        return FakeResp(json_body={"ok": True})

    _patch(monkeypatch, handler)
    out = await cu.request(method="get", path="/issues", query={"q": "open"})
    assert cap["method"] == "GET"
    assert cap["url"] == "https://api.x.com/v1/issues"
    assert cap["params"] == {"q": "open"}
    assert cap["headers"]["Authorization"] == "Bearer tok"
    assert '"ok": true' in out


async def test_request_post_sends_body(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, **k):
        cap["method"], cap["json"] = method, k.get("json")
        return FakeResp(json_body={"id": 1})

    _patch(monkeypatch, handler)
    await cu.request(method="post", path="issues", body={"title": "hi"})
    assert cap["method"] == "POST" and cap["json"] == {"title": "hi"}


async def test_request_401_marks_reconnect(monkeypatch, authed):
    _patch(monkeypatch, lambda *a, **k: FakeResp(status=401))
    out = await cu.request(method="get", path="/x")
    assert "reconnect" in out.lower()
    assert authed == ["stamp"]


async def test_request_no_api_base(monkeypatch):
    monkeypatch.setattr(cu.ctx, "token_info", lambda: {"access_token": "tok"})
    assert "misconfigured" in (await cu.request(method="get", path="/x")).lower()


async def test_request_no_token(monkeypatch):
    def unavailable():
        raise cu.ConnectorTokenError("unavailable", kind="temporary", code="transport")
    monkeypatch.setattr(cu.ctx, "token_info", unavailable)
    out = (await cu.request(method="get", path="/x")).lower()
    assert "unavailable" in out or "reconnect" in out


@pytest.mark.parametrize("path", [
    "https://unrelated.invalid/collect", "http://api.x.com/v1/issues",
    "//unrelated.invalid/collect", "../outside", "/../outside",
    "/%2e%2e/outside", "/%252e%252e/outside", "/%2foutside",
    "\\\\unrelated.invalid/collect", "/safe/../../outside", "\n//unrelated.invalid",
])
async def test_request_rejects_origin_and_prefix_escapes(monkeypatch, authed, path):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid path must not send the OAuth token")

    _patch(monkeypatch, unexpected)
    assert "Error:" in await cu.request("GET", path)


async def test_request_does_not_follow_an_upstream_redirect(monkeypatch, authed):
    destinations = []

    def send(method, url, **kwargs):
        assert kwargs["follow_redirects"] is False
        destinations.append(url)
        return FakeResp(status=302, headers={"Location": "https://unrelated.invalid/collect"})

    _patch(monkeypatch, send)
    assert "redirect refused" in await cu.request("GET", "/issues")
    assert destinations == ["https://api.x.com/v1/issues"]

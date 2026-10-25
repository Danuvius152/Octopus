"""Generic custom-connector MCP server (connectors.md custom-connectors): the
authenticated `request` tool. HTTP mocked by monkeypatching the module httpx;
the tool is a plain callable in this FastMCP version."""

from __future__ import annotations

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
        "access_token": "tok", "api_base": "https://api.x.com/v1",
    })
    recon: list[str] = []
    monkeypatch.setattr(
        cu.ctx, "mark_needs_reconnect", lambda code="invalid_grant": recon.append(code)
    )
    return recon


def test_request_get_projects_url_and_auth(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, headers=None, params=None, json=None, timeout=None, follow_redirects=None):
        cap.update(method=method, url=url, headers=headers, params=params, json=json)
        assert follow_redirects is False
        return FakeResp(json_body={"ok": True})

    monkeypatch.setattr(cu.httpx, "request", handler)
    out = cu.request(method="get", path="/issues", query={"q": "open"})
    assert cap["method"] == "GET"
    assert cap["url"] == "https://api.x.com/v1/issues"
    assert cap["params"] == {"q": "open"}
    assert cap["headers"]["Authorization"] == "Bearer tok"
    assert '"ok": true' in out


def test_request_post_sends_body(monkeypatch, authed):
    cap: dict = {}

    def handler(method, url, **k):
        cap["method"], cap["json"] = method, k.get("json")
        return FakeResp(json_body={"id": 1})

    monkeypatch.setattr(cu.httpx, "request", handler)
    cu.request(method="post", path="issues", body={"title": "hi"})
    assert cap["method"] == "POST" and cap["json"] == {"title": "hi"}


def test_request_401_marks_reconnect(monkeypatch, authed):
    monkeypatch.setattr(cu.httpx, "request", lambda *a, **k: FakeResp(status=401))
    out = cu.request(method="get", path="/x")
    assert "reconnect" in out.lower()
    assert authed == ["invalid_grant"]


def test_request_no_api_base(monkeypatch):
    monkeypatch.setattr(cu.ctx, "token_info", lambda: {"access_token": "tok"})
    assert "misconfigured" in cu.request(method="get", path="/x").lower()


def test_request_no_token(monkeypatch):
    monkeypatch.setattr(cu.ctx, "token_info", lambda: None)
    out = cu.request(method="get", path="/x").lower()
    assert "unavailable" in out or "reconnect" in out


@pytest.mark.parametrize("path", [
    "https://unrelated.invalid/collect", "http://api.x.com/v1/issues",
    "//unrelated.invalid/collect", "../outside", "/../outside",
    "/%2e%2e/outside", "/%252e%252e/outside", "/%2foutside",
    "\\\\unrelated.invalid/collect", "/safe/../../outside", "\n//unrelated.invalid",
])
def test_request_rejects_origin_and_prefix_escapes(monkeypatch, authed, path):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid path must not send the OAuth token")

    monkeypatch.setattr(cu.httpx, "request", unexpected)
    assert "Error:" in cu.request("GET", path)


def test_request_does_not_follow_an_upstream_redirect(monkeypatch, authed):
    destinations = []

    def send(method, url, **kwargs):
        assert kwargs["follow_redirects"] is False
        destinations.append(url)
        return FakeResp(status=302, headers={"Location": "https://unrelated.invalid/collect"})

    monkeypatch.setattr(cu.httpx, "request", send)
    assert "redirect refused" in cu.request("GET", "/issues")
    assert destinations == ["https://api.x.com/v1/issues"]

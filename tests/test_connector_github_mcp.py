"""GitHub connector MCP server + _shared helpers (connectors.md §5.3 / §6.
HTTP mocked by monkeypatching the modules' httpx. Tools are plain callables in
this FastMCP version, so we call them directly."""

from __future__ import annotations

import base64

import pytest

from server.mcp_servers.connectors import _shared
from server.mcp_servers.connectors import github as gh


class FakeResp:
    def __init__(self, status=200, json_body=None, text="", headers=None):
        self.status_code = status
        self._json = json_body
        self.text = text
        self.headers = headers or {}
        self.content = b"x" if (json_body is not None or text) else b""

    def json(self):
        return self._json


# --- _shared ---------------------------------------------------------------


def test_truncate_caps_and_marks():
    assert _shared.truncate("short") == "short"
    big = "A" * (_shared.MAX_RESULT_BYTES + 100)
    out = _shared.truncate(big)
    assert len(out.encode()) < len(big.encode())
    assert "truncated" in out


def test_context_reads_authoritative_token_on_every_call(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://host")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "i-1")
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        assert url.endswith("/api/connectors/i-1/token")
        return FakeResp(json_body={"access_token": "AT", "expires_at_epoch": 0, "grant_stamp": "stamp"})

    monkeypatch.setattr(_shared.httpx, "get", fake_get)
    ctx = _shared.ConnectorContext()
    assert ctx.token_info()["access_token"] == "AT"
    assert ctx.token_info()["access_token"] == "AT"
    assert calls["n"] == 2


def test_context_token_401_returns_none(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://host")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "i-1")
    monkeypatch.setattr(_shared.httpx, "get", lambda *a, **k: FakeResp(status=401))
    with pytest.raises(_shared.ConnectorTokenError) as failed:
        _shared.ConnectorContext().token_info()
    assert failed.value.kind == "unavailable"


def test_context_not_ready_returns_none(monkeypatch):
    monkeypatch.delenv("OCTOPUS_INSTALLATION_ID", raising=False)
    with pytest.raises(_shared.ConnectorTokenError) as failed:
        _shared.ConnectorContext().token_info()
    assert failed.value.kind == "configuration"


def test_mark_needs_reconnect_posts_for_current_installation(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://host")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OCTOPUS_INSTALLATION_ID", "i-1")
    cap = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        cap["url"], cap["json"] = url, json
        return FakeResp(json_body={"disposition": "obsolete"})

    monkeypatch.setattr(_shared.httpx, "post", fake_post)
    ctx = _shared.ConnectorContext()
    assert ctx.mark_needs_reconnect("stamp") == "obsolete"
    assert cap["url"].endswith("/api/connectors/i-1/mark-needs-reconnect")
    assert cap["json"] == {"grant_stamp": "stamp"}


# --- GitHub tools ----------------------------------------------------------


@pytest.fixture
def authed(monkeypatch):
    """ctx with a token; reconnect calls recorded."""
    monkeypatch.setattr(gh.ctx, "token_info", lambda: {"access_token": "tok", "grant_stamp": "stamp"})
    recon: list[str] = []
    monkeypatch.setattr(
        gh.ctx, "mark_needs_reconnect", lambda stamp: recon.append(stamp) or "applied"
    )
    return recon


def _patch_request(monkeypatch, handler):
    monkeypatch.setattr(gh.httpx, "request", handler)


def test_search_issues_projects(monkeypatch, authed):
    def handler(method, url, **kw):
        assert method == "GET" and url.endswith("/search/issues")
        return FakeResp(
            json_body={
                "total_count": 1,
                "items": [
                    {
                        "repository_url": "https://api.github.com/repos/o/n",
                        "number": 5,
                        "title": "Bug",
                        "state": "open",
                        "html_url": "https://github.com/o/n/issues/5",
                    }
                ],
            }
        )

    _patch_request(monkeypatch, handler)
    out = gh.search_issues(query="is:open", limit=10)
    assert '"repo": "o/n"' in out
    assert '"number": 5' in out
    assert '"is_pr": false' in out


def test_get_file_decodes_base64(monkeypatch, authed):
    content = base64.b64encode(b"hello world").decode()
    _patch_request(
        monkeypatch,
        lambda m, u, **k: FakeResp(json_body={"encoding": "base64", "content": content}),
    )
    assert gh.get_file(repo="o/n", path="README.md") == "hello world"


def test_get_file_directory_listing(monkeypatch, authed):
    _patch_request(
        monkeypatch,
        lambda m, u, **k: FakeResp(
            json_body=[{"name": "a.py", "type": "file"}, {"name": "sub", "type": "dir"}]
        ),
    )
    out = gh.get_file(repo="o/n", path="src")
    assert "a.py" in out and "sub" in out


def test_get_file_truncates_large(monkeypatch, authed):
    content = base64.b64encode(b"A" * (_shared.MAX_RESULT_BYTES + 500)).decode()
    _patch_request(
        monkeypatch,
        lambda m, u, **k: FakeResp(json_body={"encoding": "base64", "content": content}),
    )
    out = gh.get_file(repo="o/n", path="big.txt")
    assert "truncated" in out


def test_401_marks_reconnect(monkeypatch, authed):
    _patch_request(monkeypatch, lambda m, u, **k: FakeResp(status=401))
    out = gh.get_issue(repo="o/n", number=1)
    assert "reconnect" in out.lower()
    assert authed == ["stamp"]


def test_retry_on_429_then_success(monkeypatch, authed):
    monkeypatch.setattr(gh.time, "sleep", lambda *_: None)
    seq = [FakeResp(status=429, headers={"Retry-After": "0"}), FakeResp(json_body=[])]
    _patch_request(monkeypatch, lambda m, u, **k: seq.pop(0))
    out = gh.list_repos()
    assert out == "[]"  # second attempt succeeded with an empty repo list


def test_create_issue_writes(monkeypatch, authed):
    cap = {}

    def handler(method, url, **kw):
        cap["method"], cap["url"], cap["json"] = method, url, kw.get("json")
        return FakeResp(json_body={"number": 7, "html_url": "https://github.com/o/n/issues/7"})

    _patch_request(monkeypatch, handler)
    out = gh.create_issue(repo="o/n", title="T", body="B")
    assert cap["method"] == "POST"
    assert cap["json"] == {"title": "T", "body": "B"}
    assert '"number": 7' in out


def test_token_unavailable_returns_error(monkeypatch):
    def unavailable():
        raise _shared.ConnectorTokenError("unavailable", kind="temporary", code="transport")
    monkeypatch.setattr(gh.ctx, "token_info", unavailable)
    out = gh.search_issues(query="x")
    assert "reconnect" in out.lower() or "unavailable" in out.lower()

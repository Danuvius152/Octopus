"""Sapphire query selection and Wiki protection at the application HTTP boundary."""

import asyncio
import fcntl
import json
import shlex
import sys
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.app_backends import backend_supervisor
from server.applications import ApplicationManager
from server.database import Database
from server.main import app
from server.routers import applications as app_router
from server.routers import wiki as wiki_router
from server.session_manager import SessionManager
from server.users import UserManager
from server.wiki import app_bridge
from server.wiki.registry import WikiRegistry

# An isolated, real HTTP backend. Its query and revision selection match the
# supported Sapphire server; it also reports the protection present at dispatch.
_BACKEND = r'''
import fcntl
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

data = Path(os.environ["APP_DATA_DIR"])
state = Path((data / "test-state").read_text())
(data / "started").write_text("started")

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        query = parse_qs(urlsplit(self.path).query)
        vault = query.get("vault", [""])[0]
        journal = state / "external-change.json"
        with (state / "vault.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = False
            except BlockingIOError:
                locked = True
        observed = {
            "vault": vault, "locked": locked,
            "journal": json.loads(journal.read_text()) if journal.exists() else None,
            "path": self.path, "body": raw.hex(),
            "authorization": self.headers.get("authorization"),
            "cookie": self.headers.get("cookie"),
        }
        (data / "dispatched").write_text(json.dumps(observed))
        vaults = json.loads((data / "registry.json").read_text())["vaults"]
        if not any(v["id"] == vault for v in vaults):
            return self.respond(404, {"observed": observed})
        body = json.loads(raw)
        path = data / "vaults" / vault / "repo" / body["path"]
        expected = body.get("revision")
        if expected is None:
            return self.respond(400, {"observed": observed})
        if (path.exists() and expected != hashlib.sha256(path.read_bytes()).hexdigest()) or (not path.exists() and expected):
            return self.respond(409, {"observed": observed})
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".sapphire-tmp")
        temporary.write_bytes(body["content"].encode())
        temporary.replace(path)
        self.respond(200, {"revision": hashlib.sha256(path.read_bytes()).hexdigest(), "observed": observed})

    def respond(self, status, payload):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''


@pytest.fixture
async def sapphire_proxy(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "octopus.db"))
    await db.initialize()
    users = UserManager(db)
    deps.set_user_manager(users)
    owner = await users.create_user(username="owner", password="test-password")
    token = await users.issue_token(owner["id"])
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    manager = ApplicationManager()
    manager.bind(SessionManager(), db)
    monkeypatch.setattr(app_router, "_manager", manager)
    registry = WikiRegistry()
    monkeypatch.setattr(app_bridge, "registry", registry)
    monkeypatch.setattr(wiki_router, "registry", registry)
    app_id = uuid.uuid4().hex
    app_dir = tmp_path / "sapphire"
    app_dir.mkdir()
    data = tmp_path / "sapphire.data"
    root = data / "vaults" / "obv" / "repo"
    root.mkdir(parents=True)
    original = "# 原文\r\nExact source bytes.\r\n".encode()
    (root / "note.md").write_bytes(original)
    (data / "registry.json").write_text(json.dumps({"vaults": [{"id": "obv"}]}))
    now = datetime.now(UTC).isoformat()
    await db.save_application(
        app_id=app_id, name="Sapphire", app_dir=str(app_dir),
        user_id=owner["id"], created_at=now, updated_at=now,
    )
    registry.configure(owner["id"], root, "curator", app_id)
    state = registry.state(owner["id"])
    (data / "test-state").write_text(str(state))
    (app_dir / "server.py").write_text(_BACKEND)
    start = app_dir / "start.sh"
    start.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} server.py\n")
    start.chmod(0o700)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield {
                "client": client, "headers": headers, "token": token, "db": db,
                "users": users, "owner": owner["id"], "id": app_id,
                "root": root, "data": data, "state": state, "original": original,
                "base": f"/apps/{app_id}",
            }
    finally:
        await backend_supervisor.stop(app_id)
        registry.close()
        manager.shutdown()
        await db.close()


@pytest.mark.parametrize("vaults", [
    ["obv", "invalid"], ["", "obv", "invalid"],
    ["obv", "", "invalid", ""], ["obv", "obv"], ["", "obv", ""],
])
async def test_repeated_vault_cannot_bypass_wiki_lock_or_journal(sapphire_proxy, vaults):
    ctx = sapphire_proxy
    client, headers = ctx["client"], ctx["headers"]
    original = (await client.get(
        "/api/wiki/read", headers=headers, params={"doc": "note.md"}
    )).json()
    pairs = [
        ("vault", vaults[0]), ("tag", "first"), ("token", "discarded-first"),
        *[("vault", value) for value in vaults[1:]],
        ("tag", ""), ("token", "discarded-last"),
        ("Token", "ordinary-value"), ("path", "unrelated.md"),
    ]
    raw = json.dumps({
        "path": "note.md", "content": "# 修改\nExact new bytes.\n",
        "revision": original["revision"],
    }, ensure_ascii=False, indent=2).encode()
    response = await client.post(
        ctx["base"] + "/api/save?" + urlencode(pairs), headers=headers, content=raw,
    )
    assert response.status_code == 200, response.text
    observed = response.json()["observed"]
    assert observed["vault"] == "obv"
    assert observed["locked"] is True
    assert observed["journal"]["paths"] == ["note.md"]
    assert observed["journal"]["reason"] == "save"
    assert observed["body"] == raw.hex()
    assert observed["authorization"] is None and observed["cookie"] is None
    assert parse_qsl(urlsplit(observed["path"]).query, keep_blank_values=True) == [
        (key, value) for key, value in pairs if key != "token"
    ]
    current = (await client.get(
        "/api/wiki/read", headers=headers, params={"doc": original["id"]}
    )).json()
    assert current["id"] == original["id"]
    assert current["content"] == "# 修改\nExact new bytes.\n"
    assert (ctx["root"] / "note.md").read_bytes() == "# 修改\nExact new bytes.\n".encode()
    changes = (await client.get(
        "/api/wiki/history", headers=headers, params={"path": "note.md"}
    )).json()["items"]
    assert len(changes) == 1
    assert changes[0]["before_hash"] == original["revision"]
    assert changes[0]["after_hash"] == current["revision"]
    assert (ctx["state"] / "blobs" / original["revision"]).read_bytes() == ctx["original"]
    assert not (ctx["state"] / "external-change.json").exists()

    # A stale save still rejects the overwrite and retains the proposed draft.
    draft = "# 过期草稿\nDo not overwrite the new version.\n"
    stale = await client.post(
        ctx["base"] + "/api/save?" + urlencode(pairs), headers=headers,
        json={"path": "note.md", "content": draft, "revision": original["revision"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["observed"]["locked"] is True
    assert stale.json()["observed"]["journal"]["paths"] == ["note.md"]
    assert (ctx["root"] / "note.md").read_bytes() == "# 修改\nExact new bytes.\n".encode()
    changes = (await client.get(
        "/api/wiki/history", headers=headers, params={"path": "note.md"}
    )).json()["items"]
    assert len(changes) == 2
    conflict = next(change for change in changes if change["status"] == "conflict")
    assert conflict["before_hash"] == current["revision"]
    assert (ctx["state"] / "blobs" / conflict["after_hash"]).read_bytes() == draft.encode()
    assert not (ctx["state"] / "external-change.json").exists()


@pytest.mark.parametrize("query", [
    "", "vault", "vault=", "vault=&vault=", "Vault=obv&vault=",
    "vault=invalid&vault=obv", "vault=&vault=invalid&vault=obv",
    "vault=%20&vault=obv",
])
async def test_missing_blank_or_invalid_first_vault_does_not_write(sapphire_proxy, query):
    ctx = sapphire_proxy
    response = await ctx["client"].post(
        ctx["base"] + "/api/save?" + query, headers=ctx["headers"],
        json={"path": "note.md", "content": "must not write", "revision": ""},
    )
    assert response.status_code == 404, response.text
    observed = response.json()["observed"]
    assert observed["locked"] is False
    assert observed["journal"] is None
    assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]
    changes = await ctx["client"].get(
        "/api/wiki/history", headers=ctx["headers"], params={"path": "note.md"}
    )
    assert changes.status_code == 200, changes.text
    assert changes.json()["items"] == []
    assert not (ctx["state"] / "external-change.json").exists()


@pytest.mark.parametrize("query", ["vault=other&vault=obv", "vault=&vault=other&vault=obv"])
async def test_other_vault_first_keeps_its_own_target(sapphire_proxy, query):
    ctx = sapphire_proxy
    other = ctx["data"] / "vaults" / "other" / "repo"
    other.mkdir(parents=True)
    (ctx["data"] / "registry.json").write_text(json.dumps({
        "vaults": [{"id": "obv"}, {"id": "other"}],
    }))
    response = await ctx["client"].post(
        ctx["base"] + "/api/save?" + query, headers=ctx["headers"],
        json={"path": "note.md", "content": "other vault", "revision": ""},
    )
    assert response.status_code == 200, response.text
    observed = response.json()["observed"]
    assert observed["vault"] == "other"
    assert observed["locked"] is False
    assert observed["journal"] is None
    assert (other / "note.md").read_bytes() == b"other vault"
    assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]
    changes = await ctx["client"].get(
        "/api/wiki/history", headers=ctx["headers"], params={"path": "note.md"}
    )
    assert changes.json()["items"] == []


async def test_authentication_and_account_scope_precede_backend_start_and_dispatch(sapphire_proxy):
    ctx = sapphire_proxy
    other = await ctx["users"].create_user(username="other", password="test-password")
    token = await ctx["users"].issue_token(other["id"])
    url = ctx["base"] + "/api/save?vault=obv&vault=invalid"
    body = {"path": "note.md", "content": "forbidden", "revision": ""}
    for headers, expected in [
        ({}, 401),
        ({"Authorization": "Bearer invalid"}, 401),
        ({"Authorization": "Bearer " + token}, 404),
    ]:
        denied = await ctx["client"].post(url, headers=headers, json=body)
        assert denied.status_code == expected, denied.text
    assert not (ctx["data"] / "started").exists()
    assert not (ctx["data"] / "dispatched").exists()
    assert not (ctx["state"] / "external-change.json").exists()

    # The same ownership check also protects an already-running backend.
    allowed = await ctx["client"].post(url, headers=ctx["headers"], json=body)
    assert allowed.status_code == 409, allowed.text
    dispatched = (ctx["data"] / "dispatched").read_bytes()
    denied = await ctx["client"].post(
        url, headers={"Authorization": "Bearer " + token}, json=body,
    )
    assert denied.status_code == 404, denied.text
    assert (ctx["data"] / "dispatched").read_bytes() == dispatched
    assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]


async def test_waiting_mutation_refuses_a_backend_stopped_before_its_lock(sapphire_proxy):
    ctx = sapphire_proxy
    state = await backend_supervisor.ensure_running(ctx["id"], str(ctx["data"]).removesuffix(".data"))
    started = (ctx["data"] / "started").stat().st_mtime_ns
    with (ctx["state"] / "vault.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = asyncio.create_task(ctx["client"].post(
            ctx["base"] + "/api/save?vault=obv", headers=ctx["headers"],
            json={"path": "note.md", "content": "must not send", "revision": ""},
        ))
        try:
            async with asyncio.timeout(2):
                while not app_bridge._preparations:
                    await asyncio.sleep(0)
            await backend_supervisor.stop(ctx["id"])
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        refused = await asyncio.wait_for(pending, 3)
    assert refused.status_code == 503, refused.text
    assert state.state == "stopped"
    assert (ctx["data"] / "started").stat().st_mtime_ns == started
    assert not (ctx["data"] / "dispatched").exists()
    assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]
    assert not (ctx["state"] / "external-change.json").exists()


async def test_body_wait_cannot_rebind_original_mutation_to_a_restarted_backend(sapphire_proxy):
    ctx = sapphire_proxy
    entered, release = asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await release.wait()
            yield json.dumps({"path": "note.md", "content": "must not send", "revision": ""}).encode()

    waiting = asyncio.create_task(ctx["client"].post(ctx["base"] + "/api/save?vault=obv",
        headers=ctx["headers"], content=Body()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        original = backend_supervisor.get(ctx["id"])
        old_process = original.process
        await backend_supervisor.stop(ctx["id"])
        replacement = await backend_supervisor.ensure_running(ctx["id"], original.app_dir)
        assert replacement is original and replacement.process is not old_process
        release.set()
        refused = await asyncio.wait_for(waiting, 3)
        assert refused.status_code == 503, refused.text
        assert replacement.state == "running", "refusal must not abort the replacement writer"
        assert not (ctx["data"] / "dispatched").exists()
        assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_waiting_mutation_refuses_an_exited_backend_even_if_its_port_is_reused(sapphire_proxy):
    ctx = sapphire_proxy
    state = await backend_supervisor.ensure_running(ctx["id"], str(ctx["data"]).removesuffix(".data"))
    process = state.process
    assert process is not None
    bodies = []

    async def other_service(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            bodies.append(await reader.readexactly(length))
            writer.write(b"HTTP/1.1 409 Conflict\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    trap = None
    with (ctx["state"] / "vault.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = asyncio.create_task(ctx["client"].post(ctx["base"] + "/api/save?vault=obv",
            headers=ctx["headers"], json={"path": "note.md", "content": "private body", "revision": ""}))
        try:
            async with asyncio.timeout(3):
                while not app_bridge._preparations:
                    await asyncio.sleep(0)
            process.terminate()
            await asyncio.wait_for(process.wait(), 3)
            assert state.state == "running" and state.process is process
            trap = await asyncio.start_server(other_service, "127.0.0.1", state.port)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            refused = await asyncio.wait_for(pending, 3)
            assert refused.status_code == 503, refused.text
            assert bodies == [], "the retired backend's port now belongs to another service"
            assert not (ctx["data"] / "dispatched").exists()
            assert (ctx["root"] / "note.md").read_bytes() == ctx["original"]
            assert not (ctx["state"] / "external-change.json").exists()
        finally:
            if trap is not None:
                trap.close()
                await trap.wait_closed()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("terminal", ["done", "unrecognized"])
async def test_async_mutation_keeps_its_original_job_after_http_waiter_cancellation(sapphire_proxy, monkeypatch, terminal):
    ctx = sapphire_proxy
    polled, release = asyncio.Event(), asyncio.Event()
    forwarded = app_bridge.forward
    clients, closed = [], []
    real_client = httpx.AsyncClient

    async def backend(request):
        if request.url.path == "/api/git":
            return httpx.Response(202, json={"id": "original-job"})
        assert request.url.path == "/api/jobs/original-job"
        polled.set()
        await release.wait()
        (ctx["root"] / "note.md").write_text("# Original async effect\n")
        return httpx.Response(200, json={"id": "original-job", "status": terminal})

    class Client(real_client):
        async def aclose(self):
            closed.append(self)
            await super().aclose()

    def client(**kwargs):
        owned = Client(transport=httpx.MockTransport(backend), **kwargs)
        clients.append(owned)
        return owned

    async def disconnect_before_receipt_delivery(*args):
        receipt = await forwarded(*args)
        assert receipt.status_code == 202
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(app_bridge, "forward", disconnect_before_receipt_delivery)
    waiting = asyncio.create_task(ctx["client"].post(ctx["base"] + "/api/git?vault=obv",
        headers=ctx["headers"], json={"action": "sync"}))
    try:
        await asyncio.wait_for(polled.wait(), 3)
        waiting.cancel("client disconnected after job transfer")
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert app_bridge._pending and not clients[0].is_closed
        with (ctx["state"] / "vault.lock").open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.set()
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in tuple(app_bridge._pending))), 5)
        assert closed == clients and len(clients) == 1
        assert not app_bridge._pending
        assert not (ctx["state"] / "external-change.json").exists()
        assert (ctx["root"] / "note.md").read_text() == "# Original async effect\n"
        assert backend_supervisor.get(ctx["id"]).state == ("running" if terminal == "done" else "stopped")
    finally:
        release.set()
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        if app_bridge._pending:
            await app_bridge.shutdown()


@pytest.mark.parametrize("receipt", [{}, {"id": "../another-job"}, []])
async def test_invalid_async_receipt_stops_original_writer_and_keeps_its_effects(sapphire_proxy, monkeypatch, receipt):
    ctx = sapphire_proxy
    real_client = httpx.AsyncClient

    async def backend(request):
        (ctx["root"] / "note.md").write_text("# Effect before invalid receipt\n")
        return httpx.Response(202, json=receipt)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(backend), **kwargs))
    result = await ctx["client"].post(ctx["base"] + "/api/git?vault=obv", headers=ctx["headers"], json={"action": "sync"})
    assert result.status_code == 502, result.text
    assert backend_supervisor.get(ctx["id"]).state == "stopped"
    assert (ctx["root"] / "note.md").read_text() == "# Effect before invalid receipt\n"
    assert not (ctx["state"] / "external-change.json").exists()
    assert not app_bridge._pending

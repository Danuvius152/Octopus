"""A signed capability never outlives its account's current authority."""

import asyncio
import shlex
from datetime import UTC, datetime

import httpx
import pytest

from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.app_backends import backend_supervisor
from server.applications import ApplicationManager, app_scope_token
from server.mcp_identity import mint
from server.routers import applications as app_router
from server.session_manager import session_manager
from server.workspace import paths_for
from tests import test_auth_routes as auth_api
from tests.callback_api import start_callback_api
from tests.test_service_shutdown import synthetic_cli

client = auth_api.client


@pytest.fixture
async def accounts(client):
    http, users, db = client
    admin = await users.create_user(username="archer", password="test-password", is_admin=True)
    owner = await users.create_user(username="owner", password="test-password")
    other = await users.create_user(username="other", password="test-password")
    return {
        "http": http, "users": users, "db": db, "owner": owner, "other": other,
        "admin_headers": {"Authorization": "Bearer " + await users.issue_token(admin["id"])},
        "owner_headers": {"Authorization": "Bearer " + await users.issue_token(owner["id"])},
        "other_headers": {"Authorization": "Bearer " + await users.issue_token(other["id"])},
    }


async def test_disabling_owner_revokes_signed_mcp_at_the_public_authentication_boundary(accounts):
    http = accounts["http"]
    owner = accounts["owner"]["id"]
    scoped = {"Authorization": "Bearer " + mint("existing-execution", user_id=owner)}
    peer = {"Authorization": "Bearer " + mint("peer-execution", user_id=accounts["other"]["id"])}
    assert (await http.get("/api/agents", headers=scoped)).status_code == 200
    disabled = await http.post(
        f"/api/auth/users/{owner}/disabled", json={"disabled": True}, headers=accounts["admin_headers"],
    )
    assert disabled.status_code == 200, disabled.text
    assert (await http.get("/api/agents", headers=accounts["owner_headers"])).status_code == 401
    assert (await http.get("/api/agents", headers=scoped)).status_code == 401
    assert (await http.get("/api/agents", headers=peer)).status_code == 200
    assert (await http.get("/api/agents", headers=accounts["other_headers"])).status_code == 200


async def test_signed_mcp_claim_needs_an_existing_owner(accounts):
    headers = {"Authorization": "Bearer " + mint("removed-owner-execution", user_id="no-such-account")}
    assert (await accounts["http"].get("/api/agents", headers=headers)).status_code == 401


@pytest.fixture
async def owned_apps(accounts, tmp_path, monkeypatch):
    db = accounts["db"]
    manager = ApplicationManager()
    manager.bind(session_manager, db)
    agent_api = AppAgentManager()
    agent_api.bind(session_mgr=session_manager, db=db, app_mgr=manager)
    monkeypatch.setattr(app_router, "_manager", manager)
    monkeypatch.setattr(app_router, "app_agent_manager", agent_api)
    apps = {}
    for label in ("owner", "other"):
        app_id = label + "-application"
        directory = tmp_path / app_id
        directory.mkdir()
        (directory / "index.html").write_text(label + " private application")
        now = datetime.now(UTC).isoformat()
        await db.save_application(
            app_id=app_id, name=label, app_dir=str(directory), user_id=accounts[label]["id"],
            created_at=now, updated_at=now,
        )
        apps[label] = {"id": app_id, "directory": directory}
    try:
        yield {**accounts, "apps": apps}
    finally:
        for row in apps.values():
            await backend_supervisor.stop(row["id"])
        agent_api.shutdown()
        manager.shutdown()


@pytest.mark.parametrize("path,initial_status", [("index.html", 200), ("agent/agents", 200), ("api/ping", 404), ("wiki/status", 404)])
@pytest.mark.parametrize("credential", ["app-header", "bearer", "query", "cookie"])
async def test_disabled_owner_cannot_use_an_existing_app_capability(owned_apps, path, initial_status, credential):
    ctx = owned_apps
    http = ctx["http"]
    owned = ctx["apps"]["owner"]
    peer = ctx["apps"]["other"]
    def presented(app_id):
        token = app_scope_token(app_id)
        if credential == "query":
            return {"params": {"token": token}}
        key, value = {
            "app-header": ("X-Octopus-App-Token", token),
            "bearer": ("Authorization", "Bearer " + token),
            "cookie": ("Cookie", "octopus_app_token=" + token),
        }[credential]
        return {"headers": {key: value}}

    assert (await http.get(f"/apps/{owned['id']}/{path}", **presented(owned["id"]))).status_code == initial_status
    disabled = await http.post(
        f"/api/auth/users/{ctx['owner']['id']}/disabled", json={"disabled": True}, headers=ctx["admin_headers"],
    )
    assert disabled.status_code == 200, disabled.text
    started = owned["directory"] / "must-not-start"
    startup = owned["directory"] / "start.sh"
    startup.write_text(f"#!/bin/sh\nprintf started > {shlex.quote(str(started))}\nexit 13\n")
    startup.chmod(0o700)
    denied = await http.get(f"/apps/{owned['id']}/{path}", **presented(owned["id"]))
    assert denied.status_code == 401, denied.text
    assert not started.exists()
    assert (await http.get(f"/apps/{peer['id']}/{path}", **presented(peer["id"]))).status_code == initial_status


async def test_real_mcp_transport_rejects_disabled_owner_before_tool_dispatch(accounts):
    owner = accounts["owner"]["id"]
    token = mint("real-mcp-owner", user_id=owner)
    api = await start_callback_api(readiness_token=token)
    scoped = {"Authorization": "Bearer " + token,
              "Accept": "application/json, text/event-stream"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "authority", "version": "1"},
    }}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api.port}", timeout=5) as client:
            ready = await client.post("/mcp/bg/mcp", headers=scoped, json=initialize)
            assert ready.status_code == 200, ready.text
            transport_id = ready.headers["mcp-session-id"]
            disabled = await accounts["http"].post(
                f"/api/auth/users/{owner}/disabled", json={"disabled": True}, headers=accounts["admin_headers"],
            )
            assert disabled.status_code == 200, disabled.text
            blocked = await client.post(
                "/mcp/bg/mcp", headers={**scoped, "Mcp-Session-Id": transport_id},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert blocked.status_code == 401, blocked.text
            peer = {**scoped, "Authorization": "Bearer " + mint("real-mcp-peer", user_id=accounts["other"]["id"])}
            assert (await client.post("/mcp/bg/mcp", headers=peer, json=initialize)).status_code == 200
    finally:
        await api.stop()


async def test_accepted_input_is_not_dispatched_after_the_account_is_disabled(accounts, tmp_path, monkeypatch):
    prepared, processes = [], []
    started = tmp_path / "disabled-model-started"
    synthetic_cli(monkeypatch, prepared, (
        "import json\n"
        f"from pathlib import Path\nPath({str(started)!r}).touch()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'auth-test'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    ))
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if any(str(started) in str(arg) for arg in args):
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    owner = accounts["owner"]["id"]
    agent = await AgentManager(accounts["db"]).create_agent(name="Before disable", user_id=owner, backend="codex")
    session = await session_manager.create_session(
        agent["id"], "Queued before disable", str(paths_for(owner).workspace), backend="codex",
    )
    try:
        await session_manager.interrupt(session.id)
        receipt = await session_manager.start_message(session.id, "Keep this exact original input", source="bg")
        assert receipt.accepted
        assert processes == [] and not started.exists()
        disabled = await accounts["http"].post(
            f"/api/auth/users/{owner}/disabled", json={"disabled": True}, headers=accounts["admin_headers"],
        )
        assert disabled.status_code == 200, disabled.text
        await session_manager.resume_dispatch(session.id)
        completion = await asyncio.wait_for(receipt.wait(), 5)
        assert processes == [] and not started.exists()
        assert completion.outcome.status == "failed"
        with pytest.raises(ValueError, match="Account is unavailable"):
            await session_manager.start_message(session.id, "A new background input")
        enabled = await accounts["http"].post(
            f"/api/auth/users/{owner}/disabled", json={"disabled": False}, headers=accounts["admin_headers"],
        )
        assert enabled.status_code == 200, enabled.text
        login = await accounts["http"].post("/api/auth/login", json={"username": "owner", "password": "test-password"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        detail = await accounts["http"].get(f"/api/sessions/{session.id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert any(message["role"] == "user" and message["content"] == "Keep this exact original input"
                   for message in detail.json()["messages"])
    finally:
        await session_manager.drain_turns()

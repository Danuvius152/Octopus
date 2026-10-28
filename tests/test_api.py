"""End-to-end tests for REST API using FastAPI TestClient."""

import asyncio
from datetime import UTC

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.main import app
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import paths_for


@pytest.fixture
async def client():
    # Initialize session_manager with in-memory DB before each test
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="api-owner", password="password-for-api")
    deps.set_user_manager(users)
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/sessions", headers={"Authorization": ""})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_auth_bad_token(client):
    resp = await client.get(
        "/api/sessions", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_sessions_empty(client):
    resp = await client.get("/api/sessions", headers=client.headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_http_submission_is_explicitly_refused_while_recovery_is_incomplete(client, monkeypatch):
    db = session_manager.db
    agent = (await db.load_agents())[0]
    session = await session_manager.create_session(agent["id"], "Readiness")
    entered, release = asyncio.Event(), asyncio.Event()
    load = db.load_sessions
    async def held_load(*args, **kwargs):
        rows = await load(*args, **kwargs)
        entered.set()
        await release.wait()
        return rows
    monkeypatch.setattr(db, "load_sessions", held_load)
    initializing = asyncio.create_task(session_manager.initialize(db))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        reply = await client.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": "not-yet", "content": "Keep this draft",
        })
        assert reply.status_code == 200
        assert reply.json()["accepted"] is False and reply.json()["code"] == "service_not_ready"
        assert "state" not in reply.json()
        status = await client.get(f"/api/sessions/{session.id}/submissions/not-yet")
        assert status.status_code == 503 and status.json()["code"] == "service_not_ready"
        assert await db.get_client_submission(session.id, "not-yet") is None
    finally:
        release.set()
        await initializing


@pytest.mark.asyncio
async def test_create_session(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Test Session", "working_dir": str(paths_for(agent["user_id"]).workspace)},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Session"
    assert data["working_dir"] == str(paths_for(agent["user_id"]).workspace)
    assert data["status"] == "idle"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_session(client):
    # Create first
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Get Me"},
    )
    sid = create_resp.json()["id"]

    # Get it
    resp = await client.get(f"/api/sessions/{sid}", headers=client.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sid
    assert data["name"] == "Get Me"
    assert "messages" in data


@pytest.mark.asyncio
async def test_get_session_not_found(client):
    resp = await client.get("/api/sessions/nonexistent", headers=client.headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Delete Me"},
    )
    sid = create_resp.json()["id"]

    resp = await client.delete(f"/api/sessions/{sid}", headers=client.headers)
    assert resp.status_code == 204

    # Verify gone
    resp = await client.get(f"/api/sessions/{sid}", headers=client.headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_not_found(client):
    resp = await client.delete("/api/sessions/nonexistent", headers=client.headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session(client):
    """POST /api/sessions/{id}/archive returns a fresh SessionInfo with the
    same name/working_dir, the old session disappears from the list,
    and the new id is different."""
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Archive Me", "working_dir": str(paths_for(agent["user_id"]).workspace / "archived")},
    )
    old_id = create_resp.json()["id"]

    arc = await client.post(
        f"/api/sessions/{old_id}/archive", headers=client.headers
    )
    assert arc.status_code == 201
    body = arc.json()
    new_id = body["id"]
    assert new_id != old_id
    assert body["name"] == "Archive Me"
    assert body["working_dir"] == str(paths_for(agent["user_id"]).workspace / "archived")

    # Old session is hidden from the list; new one appears.
    list_resp = await client.get("/api/sessions", headers=client.headers)
    ids = [s["id"] for s in list_resp.json()]
    assert old_id not in ids
    assert new_id in ids

    # GET on the old id still works — it returns the archived row's
    # detail (so the UI's "view archived" can read history).
    archived = await client.get(f"/api/sessions/{old_id}", headers=client.headers)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    # GET on the list with ?include_archived=true surfaces both.
    inc = await client.get(
        "/api/sessions?include_archived=true", headers=client.headers
    )
    ids = [s["id"] for s in inc.json()]
    assert old_id in ids
    assert new_id in ids

    # Unarchive brings the old id back; it returns to the default list.
    un = await client.post(
        f"/api/sessions/{old_id}/unarchive", headers=client.headers
    )
    assert un.status_code == 200
    assert un.json()["id"] == old_id
    assert un.json()["archived"] is False
    list_after = await client.get("/api/sessions", headers=client.headers)
    assert old_id in [s["id"] for s in list_after.json()]


@pytest.mark.asyncio
async def test_archive_session_not_found(client):
    resp = await client.post(
        "/api/sessions/nonexistent/archive", headers=client.headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_session_defaults_backend_to_claude_code(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Default Backend"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_create_session_with_codex_backend(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Cx", "backend": "codex"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "codex"


@pytest.mark.asyncio
async def test_create_session_rejects_credential_backend_mismatch(client):
    """A Codex session must not run a claude-code credential (codex-backend.md
    §4.2) — the route returns 400."""
    from datetime import datetime

    from server.config import settings
    from server.crypto import encrypt

    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    enc = encrypt("sk-x", settings.auth_token)
    await session_manager.db.save_credential(
        credential_id="c-cc",
        backend="claude-code",
        label="L",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=datetime.now(UTC).isoformat(),
        user_id=agent["user_id"],
    )
    resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Bad", "backend": "codex", "credential_id": "c-cc"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_backends_includes_claude_code(client):
    resp = await client.get("/api/backends", headers=client.headers)
    assert resp.status_code == 200
    assert "claude-code" in resp.json()["available"]


@pytest.mark.asyncio
async def test_session_info_reports_the_steering_capability(client):
    """`can_steer` is a harness capability, derived like `can_fork` — the
    composer reads it to say what the send button will actually do. Claiming
    "Queue message" when the message will steer is worse than saying nothing.
    """
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    res = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Steerable"}
    )
    assert res.status_code == 201
    assert res.json()["can_steer"] is True

    cx = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Cx", "backend": "codex"}
    )
    assert cx.status_code == 201
    assert cx.json()["can_steer"] is False


@pytest.mark.asyncio
async def test_a_404_is_never_cacheable(client):
    """We send no Cache-Control on a 404, so a CDN invents one — Cloudflare
    caches error responses for static-looking paths for four hours. The first
    request for an asset that doesn't exist YET then poisons that URL long
    after the file lands: the origin serves it, everyone behind the edge sees
    a 404. That cost real debugging time on an apple-touch-icon.
    """
    # Static-looking extensions specifically: those are what a CDN caches.
    for path in ("/not-here.png", "/nope.js", "/api/definitely-not-a-route"):
        resp = await client.get(path, headers=client.headers)
        assert resp.status_code == 404, path
        assert resp.headers.get("cache-control") == "no-store", path


@pytest.mark.asyncio
async def test_a_successful_response_keeps_its_own_cache_headers(client):
    """The 404 rule must not reach into anything else."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


@pytest.mark.asyncio
async def test_archive_session_without_replacement(client):
    """`?replace=false` — the sidebar's archive button (sidebar-pins.md §6):
    the session is put away, nothing takes its place, and its history can
    still be read and restored. A soft delete, not a restart."""
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/sessions",
        headers=client.headers,
        json={"agent_id": agent["id"], "name": "Put Away", "working_dir": str(paths_for(agent["user_id"]).workspace / "put-away")},
    )
    old_id = create_resp.json()["id"]
    before = {s["id"] for s in (await client.get("/api/sessions", headers=client.headers)).json()}

    arc = await client.post(
        f"/api/sessions/{old_id}/archive",
        params={"replace": "false"},
        headers=client.headers,
    )
    assert arc.status_code == 204
    after = {s["id"] for s in (await client.get("/api/sessions", headers=client.headers)).json()}
    # Gone from the list, and no successor appeared.
    assert after == before - {old_id}

    archived = await client.get(f"/api/sessions/{old_id}", headers=client.headers)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    un = await client.post(f"/api/sessions/{old_id}/unarchive", headers=client.headers)
    assert un.status_code == 200
    assert old_id in [
        s["id"] for s in (await client.get("/api/sessions", headers=client.headers)).json()
    ]


@pytest.mark.asyncio
async def test_answer_long_poll_timeout_is_not_recorded_as_an_error(client, monkeypatch):
    """The answer long-poll's 408 is "no answer yet", and its duration is the
    user's think time — the monitor tags it rather than counting an error."""
    import server.main as main

    events = []
    monkeypatch.setattr(main, "_mon_record", events.append)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    created = await client.post("/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Asker"})
    sid = created.json()["id"]
    question = await client.post(
        f"/api/sessions/{sid}/questions", headers=client.headers,
        json={"questions": [{"question": "Choose", "options": [{"label": "A"}]}]},
    )
    qid = question.json()["question_id"]
    resp = await client.get(
        f"/api/sessions/{sid}/questions/{qid}/answer?timeout=1", headers=client.headers
    )
    assert resp.status_code == 408
    poll = [e for e in events if e.detail["route"].endswith("/answer")]
    assert len(poll) == 1
    assert poll[0].error_code is None
    assert poll[0].detail["long_poll"] is True

    missing = await client.get("/api/sessions/nonexistent", headers=client.headers)
    assert missing.status_code == 404
    assert events[-1].error_code == "404"
    assert "long_poll" not in events[-1].detail

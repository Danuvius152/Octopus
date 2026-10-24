"""Signed rejection diagnoses distinguish lifecycle reasons without payloads."""

import httpx
import pytest

from server import deps, mcp_identity
from server.database import Database
from server.mcp_http import _ScopeMiddleware
from server.users import UserManager


@pytest.fixture
async def active_account():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    deps.set_user_manager(users)
    try:
        yield await users.create_user(username="owner", password="test-password")
    finally:
        await db.close()


@pytest.mark.parametrize("reason", ["missing_bearer", "invalid_signature", "closed_work", "missing_run_lease", "retired_run_lease", "mismatched_run_lease", "owner_unavailable", "live"])
async def test_rejection_reason_keeps_authority_and_logs_only_verified_correlation(monkeypatch, caplog, reason, active_account):
    monkeypatch.setattr(mcp_identity, "_run_leases", {})
    monkeypatch.setattr(mcp_identity, "_closed_works", set())
    session, lease, work, owner = "signed-session", "signed-run", "signed-work", active_account["id"]
    token = mcp_identity.mint(session, user_id=owner, run_lease_id=lease, work_id=work)
    if reason == "missing_bearer":
        token = None
    elif reason == "invalid_signature":
        token = "unverified-caller.unverified-install.unverified-user." + "0" * 64
    elif reason == "closed_work":
        mcp_identity.close_work(work)
    elif reason == "missing_run_lease":
        token = mcp_identity.mint(session, user_id=owner, work_id=work)
    elif reason == "mismatched_run_lease":
        mcp_identity.register_run(lease, "another-session", "another-owner", work)
    elif reason == "owner_unavailable":
        token = mcp_identity.mint(session)
    elif reason == "live":
        mcp_identity.register_run(lease, session, owner, work)
    reached = []

    async def downstream(scope, receive, send):
        reached.append(mcp_identity.current_scope())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = _ScopeMiddleware(downstream, "test")
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/mcp", headers=headers, content="SECRET-TOOL-ARGUMENTS")
    assert response.status_code == (200 if reason == "live" else 401)
    assert len(reached) == (1 if reason == "live" else 0)
    assert mcp_identity.current_scope() is None
    if reason != "live":
        assert "reason=" + reason in caplog.text
        if reason not in {"missing_bearer", "invalid_signature"}:
            assert "session=" + session in caplog.text
    assert "SECRET-TOOL-ARGUMENTS" not in caplog.text
    assert "unverified-caller" not in caplog.text
    if token:
        assert token not in caplog.text

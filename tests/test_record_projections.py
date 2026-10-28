"""Complete records keep their meaning when their single projection is reordered."""

import json

import pytest

from server.database import Database
from server.db.projection import RecordProjection


@pytest.fixture
async def db():
    db = Database(":memory:")
    await db.initialize()
    await db.create_user(user_id="owner", username="owner", password_hash="hash", created_at="created")
    await db.save_credential("credential", "codex", "Login", "oauth", "encrypted-only", "credential-time", user_id="owner")
    await db.save_agent(agent_id="agent", name="Reader", created_at="agent-time", updated_at="updated",
                        user_id="owner", backend="codex", model="chosen-model", credential_id="credential",
                        reasoning_effort="high", service_tier="fast", mcp_servers=[{"name": "local"}], subagents=[{"name": "leaf"}])
    await db.save_session("session", "Chat", "/work", "session-time", agent_id="agent", user_id="owner")
    await db.save_application(app_id="application", name="Report", app_dir="/apps/report", created_at="app-time",
                              updated_at="app-updated", user_id="owner", agent_id="agent", session_id="session", icon="📄")
    await db.create_bg_task("bg", "session", "printf hello", "Work", "/work", "bg-time",
                           idle_timeout_seconds=20, timeout_seconds=40, work_id="round", knowledge_context="bg-context")
    await db.update_bg_task("bg", status="completed", exit_code=7, stdout="output", stderr="error", truncated=True, stop_reason="requested")
    await db.create_research_job("research", "session", "Question", "research-time", knowledge_context="research-context")
    await db.advance_research_delivery("research", "request", "queued")
    await db.advance_research_delivery("research", "request", "processed")
    receipt = (await db.get_research_job("research"))["delivery_receipt"]
    await db.acknowledge_research_delivery("research", "request", receipt["version"])
    await db.save_connector_installation(installation_id="connector", kind="service", label="Account", auth_type="oauth",
                                         external_account_id="external", scopes=["read"], enable_by_default=True,
                                         token_expires_at="expires", secret_encrypted="connector-secret", created_at="connector-time", user_id="owner")
    await db.save_custom_connector(kind="custom", display_name="Custom", authorize_url="https://example.test/auth",
                                   token_url="https://example.test/token", scopes=["write"], pkce=True, api_base="https://example.test", now="custom-time")
    yield db
    await db.close()


@pytest.mark.parametrize(("projection", "reader", "identity", "expected"), [
    ("_AGENT_COLS", "get_agent", "agent", {"name": "Reader", "model": "chosen-model", "backend": "codex", "credential_id": "credential",
      "reasoning_effort": "high", "service_tier": "fast", "active_session_count": 1, "user_id": "owner", "mcp_servers": [{"name": "local"}], "subagents": [{"name": "leaf"}]}),
    ("_APPLICATION_COLS", "get_application", "application", {"name": "Report", "icon": "📄", "archived": False, "pinned": True,
      "session_id": "session", "agent_id": "agent", "user_id": "owner", "updated_at": "app-updated"}),
    ("_BG_TASK_COLS", "get_bg_task", "bg", {"stdout": "output", "stderr": "error", "exit_code": 7, "truncated": True,
      "work_id": "round", "knowledge_context": "bg-context", "idle_timeout_seconds": 20, "timeout_seconds": 40}),
    ("_RESEARCH_COLS", "get_research_job", "research", {"question": "Question", "knowledge_context": "research-context", "injection_status": "pending"}),
    ("_CONNECTOR_COLS", "get_connector_installation", "connector", {"label": "Account", "scopes": ["read"], "user_id": "owner", "enable_by_default": True, "token_expires_at": "expires"}),
    ("_CUSTOM_COLS", "get_custom_connector", "custom", {"display_name": "Custom", "pkce": True, "scopes": ["write"], "api_base": "https://example.test"}),
    ("_CREDENTIAL_COLS", "get_credential", "credential", {"backend": "codex", "label": "Login", "secret_encrypted": "encrypted-only", "user_id": "owner", "needs_reconnect": False}),
])
async def test_public_records_follow_their_declared_projection(db, monkeypatch, projection, reader, identity, expected):
    declared = getattr(db, projection)
    assert isinstance(declared, RecordProjection)
    monkeypatch.setattr(db, projection, RecordProjection(*reversed(declared.fields)))
    record = await getattr(db, reader)(identity)
    assert {key: record[key] for key in expected} == expected
    if reader == "get_research_job":
        assert record["delivery_receipt"]["stage"] == "processed"
        assert record["delivery_receipt"]["browser_confirmed_version"] == record["delivery_receipt"]["version"]
    if reader != "get_credential":
        assert "secret_encrypted" not in record


@pytest.mark.parametrize("raw", ["", "null", "broken json", '{"not":"a list"}', '["entry"]'])
async def test_domain_json_policies_are_preserved(db, raw):
    async with db.transaction() as tx:
        await tx.execute("UPDATE agents SET mcp_servers=?, subagents=? WHERE id='agent'", (raw, raw))
        await tx.execute("UPDATE connector_installations SET scopes=?", (raw,))
        await tx.execute("UPDATE custom_connectors SET scopes=?", (raw,))
    decoded = json.loads(raw) if raw and raw != "broken json" else []
    agent = await db.get_agent("agent")
    assert agent["mcp_servers"] == decoded
    assert agent["subagents"] == (decoded if isinstance(decoded, list) else [])
    assert (await db.get_connector_installation("connector"))["scopes"] == decoded
    assert (await db.get_custom_connector("custom"))["scopes"] == decoded


def test_projection_rejects_incomplete_or_excess_columns():
    projection = RecordProjection("first", ("derived", "COUNT(*)"))
    for row in [(1,), (1, 2, "secret")]:
        with pytest.raises(ValueError, match="record columns"):
            projection.read(row)

"""Saved agent choices survive current-format startup."""


import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.users import UserManager


@pytest.mark.parametrize("name", ["Default", "My assistant"])
@pytest.mark.parametrize("tools", [[], ["bg", "ask"]])
async def test_startup_preserves_saved_agent_configuration(tmp_path, name, tools):
    path = str(tmp_path / "agents.db")
    db = Database(path)
    await db.initialize()
    try:
        owner = await UserManager(db).create_user(username="owner", password="password1")
        system = await db.get_system_agent()
        assert system["user_id"] == owner["id"]
        await db.save_credential(
            credential_id="saved-credential", backend="codex", label="Saved API",
            auth_type="api_key", secret_encrypted="test-ciphertext",
            created_at="2026-10-01T00:00:00Z", user_id=owner["id"],
        )
        await db.update_agent(
            system["id"], name=name, description="Keep description", avatar="saved-icon",
            system_prompt="Use my instructions verbatim: /old/root is prose.",
            backend="codex", model="saved-model", reasoning_effort="high",
            service_tier="fast", credential_id="saved-credential", mcp_servers=tools,
            tool_allow="Read", tool_deny="Write", subagents=[{"name": "helper"}],
        )
        other = await AgentManager(db).create_agent(
            name="Specialist", user_id=owner["id"], mcp_servers=[],
            system_prompt="Keep specialist instructions", backend="claude-code",
        )
        await db.update_agent(other["id"], archived=True)
        expected = await db.load_agents(include_archived=True, user_id=owner["id"])
    finally:
        await db.close()


    for _ in range(2):
        db = Database(path)
        await db.initialize()
        try:
            assert await db.load_agents(include_archived=True, user_id=owner["id"]) == expected
        finally:
            await db.close()


async def test_registered_account_agent_uses_defaults_only_at_creation(tmp_path):
    path = str(tmp_path / "registered.db")
    db = Database(path)
    await db.initialize()
    try:
        users = UserManager(db)
        admin = await users.create_user(username="owner", password="password1", is_admin=True)
        invite = await users.create_invite(created_by=admin["id"])
        account = await users.register(
            invite_code=invite["code"], username="invited", password="password2",
        )
        agent = await AgentManager(db).create_agent(name="New assistant", user_id=account["id"])
        assert agent["mcp_servers"] == ["ask", "bg", "ask_agent", "research", "schedule"]
        await db.update_agent(agent["id"], name="Chosen name", mcp_servers=["ask"])
        expected = await db.get_agent(agent["id"], user_id=account["id"])
    finally:
        await db.close()

    db = Database(path)
    await db.initialize()
    try:
        assert await db.get_agent(agent["id"], user_id=account["id"]) == expected
    finally:
        await db.close()

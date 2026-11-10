"""Shared schedule API runtime and MCP invocation; assertions stay in case files."""

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="tool-owner", password="password-for-tools")
    deps.set_user_manager(users)
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c

    await runner.shutdown()
    await db.close()


async def create_session(client) -> str:
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post("/api/sessions", json={"agent_id": agent["id"], "name": "s"}, headers=client.headers)
    assert resp.status_code == 201
    return resp.json()["id"]


def call_tool(tool: str, **kwargs):
    """Invoke the FastMCP-wrapped schedule tool through its exposed callable."""
    from server.mcp_servers import schedule as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]

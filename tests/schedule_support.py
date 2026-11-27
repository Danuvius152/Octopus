"""Shared schedule API, Runner dispatch and MCP invocation."""

import pytest
from apscheduler.triggers.date import DateTrigger
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.knowledge import encode, for_account
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager
from server.users import UserManager


async def save_schedule(db, **fields):
    """Seed a schedule with the original account's knowledge authority."""
    agent = await db.get_agent(fields["agent_id"])
    context = for_account(session_id=fields.get("origin_session_id") or "schedule-entry",
                          request_id=fields["schedule_id"], user_id=agent["user_id"],
                          prompt=fields["prompt"], selected=fields.get("wiki_mode"))
    await db.save_schedule(**fields, knowledge_context=encode(context))


def schedule_job(runner, schedule_id):
    registration = runner._registrations.get(schedule_id)
    return runner._scheduler.get_job(registration.job_id) if registration is not None else None


async def dispatch_schedule(runner, schedule_id):
    """Dispatch through the registered APScheduler job with a controlled tick."""
    if schedule_id not in runner._registrations:
        await runner.reconcile(schedule_id)
    job = schedule_job(runner, schedule_id)
    if job is not None:
        if isinstance(job.trigger, DateTrigger):
            job.remove()
        await job.func(*job.args)


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

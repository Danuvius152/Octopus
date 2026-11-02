"""Function-scoped authenticated Wiki HTTP runtime; no case-module dependencies."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.main import app
from server.session_manager import SessionManager
from tests.accounts import create_account

session_manager = SessionManager()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import sys

    from server.deps import get_session_manager

    manager = SessionManager()
    monkeypatch.setattr(sys.modules[__name__], "session_manager", manager)
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    monkeypatch.setitem(app.dependency_overrides, get_session_manager, lambda: manager)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as c:
            yield c
    finally:
        # Registered hooks drain turns before joining Owners, while storage
        # can still record their terminal and retirement facts.
        await db.close()


async def configure(client, vault):
    agent_id = (await session_manager.db.load_agents())[0]["id"]
    response = await client.put(
        "/api/wiki/config",
        headers=client.headers,
        json={"root": str(vault), "curator_agent_id": agent_id},
    )
    assert response.status_code == 200, response.text
    return response.json()

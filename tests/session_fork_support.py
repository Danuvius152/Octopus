"""Shared SQLite and HTTP setup for preparing an original fork."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import get_session_manager
from server.routers.sessions import router
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.fixture
async def installation(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "installation.sqlite3"))
    await database.initialize()
    _, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "original.txt").write_text("Source bytes remain unchanged.\n")
    monkeypatch.setattr(manager, "_fork_copy_base", lambda: str(tmp_path / "copies"))
    agent = await database.get_system_agent()
    parent = await manager.create_session(agent["id"], "Source", str(directory), backend="codex")
    await database.append_message(
        session_id=parent.id, seq=0, role="user", type="text", content="Original request.",
    )
    await manager.initialize(database)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + bearer},
        ) as client:
            yield manager, parent.id, client, directory
    finally:
        await database.close()

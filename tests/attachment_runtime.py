"""Isolated storage accounts and real HTTP clients for attachment consumers."""

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.config import settings
from server.database import Database
from server.main import app
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import paths_for


@pytest.fixture
def users_root(tmp_path, monkeypatch):
    """Keep both storage helpers and real accounts under a per-test root."""
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))


@pytest.fixture
def storage_user_id(users_root):
    """Pure file helpers receive an explicit, isolated account owner."""
    return "attachment-storage-owner"


@pytest.fixture
def attachments_root(storage_user_id):
    root = paths_for(storage_user_id).attachments
    root.mkdir(parents=True)
    return root


@pytest.fixture
async def account_user_id(users_root, monkeypatch):
    """Create the real account aggregate used by authenticated HTTP tests."""
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="attachment-owner", password="password-for-files")
    monkeypatch.setattr(deps, "_user_manager", users)
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    try:
        yield user["id"]
    finally:
        await db.close()


@pytest.fixture
async def client(account_user_id):
    """In-memory DB + ASGI client with a real account bearer."""
    headers = {"Authorization": "Bearer " + await deps.get_user_manager().issue_token(account_user_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        yield client

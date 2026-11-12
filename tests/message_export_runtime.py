"""Authenticated message/export HTTP runtime and original CLI adapter."""

import asyncio
import io
import urllib.error
import urllib.request

import pytest
from httpx import ASGITransport, AsyncClient

from server.cli import build_parser
from server.database import Database
from server.deps import get_session_manager
from server.main import app
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.fixture
async def export_runtime(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "export.sqlite3"))
    await database.initialize()
    _, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Export", str(tmp_path), backend="claude-code")
    session.claude_session_id = "export-native"
    await database.update_session_field(session.id, claude_session_id="export-native")
    app.dependency_overrides[get_session_manager] = lambda: manager
    loop = asyncio.get_running_loop()
    requests = []
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                               headers={"Authorization": f"Bearer {bearer}"}) as client:
            def urlopen(request, **kwargs):
                requests.append(request.full_url)
                response = asyncio.run_coroutine_threadsafe(
                    client.request(request.method, request.full_url, headers=dict(request.header_items()), content=request.data), loop,
                ).result(timeout=5)
                if response.is_error:
                    raise urllib.error.HTTPError(request.full_url, response.status_code, response.reason_phrase,
                                                 response.headers, io.BytesIO(response.content))
                return io.BytesIO(response.content)

            monkeypatch.setattr(urllib.request, "urlopen", urlopen)
            args = build_parser().parse_args([
                "pull", session.id, "--server", "http://test", f"--token={bearer}",
                "--project-dir", str(tmp_path / "output"),
            ])
            yield database, manager, session, client, args, requests
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        await database.close()

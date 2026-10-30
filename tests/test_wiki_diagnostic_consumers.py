"""All real compact entry points keep diagnostic samples bounded upstream."""

import json
import sqlite3
from contextlib import contextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.config import settings
from server.deps import get_session_manager
from server.main import _wiki_error
from server.mcp_identity import mint
from server.routers.wiki import router
from server.wiki import WikiError
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.callback_api import start_callback_api

client = wiki_api.client
ENTRIES = ("search", "documents", "read", "read-many", "info", "graph")


@pytest.fixture
async def diagnostic_client(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# Needle\nHealthy evidence.\n")
    for number in range(5):
        (root / f"broken-{number}.md").write_text("---\ntags: [broken\n---\n# Invalid metadata\n")
    config = await wiki_api.configure(client, root)
    manager = wiki_api.session_manager
    session = await manager.create_session(config["curator_agent_id"], "Diagnostic consumers", str(tmp_path))
    token = tasks.open(session, "read")
    store = registry.store(session.user_id)
    store.refresh()
    headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
    try:
        yield client, store, headers
    finally:
        tasks.close(token)


async def request_entry(client, headers, entry, view):
    if entry == "read-many":
        return await client.post("/api/wiki/read-many", headers=headers,
                                 json={"items": [{"doc": "note.md", "length": 10}], "view": view})
    params = {"query": "Needle"} if entry == "search" else {"doc": "note.md"} if entry in {"read", "graph"} else {}
    if entry != "graph":
        params["view"] = view
    return await client.get("/api/wiki/" + entry, headers=headers, params=params)


@pytest.mark.parametrize("entry", ENTRIES)
async def test_compact_and_full_health_agree_through_real_http(diagnostic_client, entry):
    client, _, headers = diagnostic_client
    compact = await request_entry(client, headers, entry, "compact")
    assert compact.status_code == 200, compact.text
    value = compact.json()
    assert value["index_error_count"] == 5
    assert len(value["index_errors"]) == 3
    assert value["index_errors_truncated"] is True
    assert value["index_status"] == "partial"
    full = await request_entry(client, client.headers, entry, "full")
    assert full.status_code == 200, full.text
    original = full.json()
    assert original["index_error_count"] == 5
    assert len(original["index_errors"]) == 5
    assert value["index_errors"] == original["index_errors"][:3]
    assert original["index_status"] == value["index_status"]


async def test_unlisted_system_failure_still_blocks_maintenance_and_remains_in_full_info(diagnostic_client):
    client, store, headers = diagnostic_client
    with store.connect() as db:
        db.execute("UPDATE index_state SET error='Index unavailable' WHERE singleton=1")
    compact = await request_entry(client, headers, "info", "compact")
    assert compact.status_code == 200, compact.text
    value = compact.json()
    assert value["index_error_count"] == 6 and len(value["index_errors"]) == 3
    assert all(item["path"] for item in value["index_errors"])
    assert value["index_status"] == "unavailable" and value["index_errors_truncated"]
    full = await client.get("/api/wiki/info", headers=client.headers)
    assert full.status_code == 200, full.text
    assert full.json()["index_error_count"] == 6
    assert full.json()["index_errors"][-1] == {"path": "", "error": "Index unavailable"}
    maintenance = await client.get("/api/wiki/maintenance-status", headers=client.headers)
    assert maintenance.status_code == 200, maintenance.text
    assert maintenance.json()["index_error_count"] == 6
    assert maintenance.json()["index_status"] == "unavailable"
    assert maintenance.json()["ready"] is False


async def test_info_mcp_schema_and_real_calls_default_to_compact_with_full_opt_in(diagnostic_client, monkeypatch):
    client, _, headers = diagnostic_client
    manager = wiki_api.session_manager
    bearer = headers["Authorization"].removeprefix("Bearer ")
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager},
                                   exception_handlers={WikiError: _wiki_error}, readiness_token=bearer)
    monkeypatch.setattr(settings, "port", api.port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api.port}", headers=headers) as http:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/wiki/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as peer:
                    await peer.initialize()
                    schema = next(tool.inputSchema for tool in (await peer.list_tools()).tools if tool.name == "info")
                    assert schema["properties"]["view"]["default"] == "compact"
                    assert set(schema["properties"]["view"]["enum"]) == {"compact", "full"}
                    for arguments, count in [({}, 3), ({"view": "full"}, 5)]:
                        result = await peer.call_tool("info", arguments)
                        assert not result.isError, result
                        value = json.loads(result.content[0].text)
                        assert value["index_error_count"] == 5
                        assert len(value["index_errors"]) == count
                        assert bool(value.get("index_errors_truncated")) == (count == 3)
                    assert (await peer.call_tool("info", {"view": "invalid"})).isError
        full = await client.get("/api/wiki/info", headers=client.headers)
        assert full.status_code == 200 and len(full.json()["index_errors"]) == 5
    finally:
        await api.stop()


@pytest.mark.wiki_perf
async def test_compact_entry_points_never_materialize_full_error_rows(diagnostic_client, monkeypatch):
    client, store, headers = diagnostic_client
    for number in range(5, 50):
        (store.root / f"broken-{number}.md").write_text("---\ntags: [broken\n---\n# Invalid metadata\n")
    store.refresh()
    connect = store.connect
    returned = []

    @contextmanager
    def observe_rows():
        with connect() as db:
            def row(cursor, values):
                if tuple(column[0] for column in cursor.description) == ("path", "metadata"):
                    returned.append(values[0])
                return sqlite3.Row(cursor, values)
            db.row_factory = row
            yield db

    monkeypatch.setattr(store, "connect", observe_rows)
    for entry in ENTRIES:
        returned.clear()
        response = await request_entry(client, headers, entry, "compact")
        assert response.status_code == 200, response.text
        assert response.json()["index_error_count"] == 50
        assert len(response.json()["index_errors"]) == 3
        assert len(returned) == 3, (entry, returned)

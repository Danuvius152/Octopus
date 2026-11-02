"""Compact diagnostics survive the actual HTTP response model and MCP shim."""

import httpx

from server.mcp_servers import wiki as mcp_wiki
from server.wiki.registry import registry
from tests import wiki_api as wiki_api

client = wiki_api.client


async def test_navigation_error_bounds_survive_http_and_mcp(client, tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# Needle\nEvidence for the query.\n")
    await wiki_api.configure(client, root)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    store = registry.store(owner)
    state = store.index_state()
    for error_count in (0, 2, 9):
        errors = [{"path": "长" * 200 + str(index), "error": "错误" * 150} for index in range(error_count)]
        monkeypatch.setattr(store, "_index_state", lambda db=None, error_limit=None, errors=errors: state | {"index_status": "partial" if errors else "ready", "index_error_count": len(errors), "index_errors": errors})
        compact_search = None
        for endpoint, base in (("search", {"query": "Needle"}), ("documents", {})):
            compact = await client.get("/api/wiki/" + endpoint, params=base | {"view": "compact"}, headers=client.headers)
            assert compact.status_code == 200, compact.text
            value = compact.json()
            assert value["index_error_count"] == error_count
            assert len(value["index_errors"]) == min(error_count, 3)
            assert bool(value.get("index_errors_truncated")) == bool(errors)
            assert value["index_errors"] == [
                {"path": item["path"], "error": item["error"][:160]} for item in errors[:3]
            ]
            assert value["index_status"] == ("partial" if errors else "ready")
            assert value["pending_changes"] == state["pending_changes"]
            full = await client.get("/api/wiki/" + endpoint, params=base | {"view": "full"}, headers=client.headers)
            assert full.status_code == 200 and full.json()["index_errors"] == errors
            if endpoint == "search":
                compact_search = value
        monkeypatch.setattr(mcp_wiki.httpx, "request", lambda *args, value=compact_search, **kwargs: httpx.Response(200, json=value))
        seen = mcp_wiki.search("Needle", view="compact")
        assert seen["index_error_count"] == error_count
        assert bool(seen.get("index_errors_truncated")) == bool(errors)
        assert seen["index_errors"] == compact_search["index_errors"]

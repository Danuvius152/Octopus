"""Search preserves resource refusals at the HTTP, App and model boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import deps
from server.applications import ApplicationManager, app_scope_token
from server.mcp_identity import mint
from server.routers import applications as app_routes
from server.wiki import registry
from server.wiki import store as storage
from server.wiki.tasks import tasks
from tests import wiki_api
from tests.wiki_api import client as client


@pytest.fixture
async def surfaces(client, tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    article = root / "note.md"
    article.write_text("# Resource evidence\n\nCurrent searchable words.\n")
    config = await wiki_api.configure(client, root)
    token = client.headers["Authorization"].removeprefix("Bearer ")
    owner = await deps.get_user_manager().resolve_token(token, touch=False)
    manager = ApplicationManager()
    manager.bind(wiki_api.session_manager, wiki_api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    now = datetime.now(UTC).isoformat()
    await wiki_api.session_manager.db.save_application(
        app_id="search-surface", name="Search surface", app_dir=str(tmp_path / "app"),
        created_at=now, updated_at=now, agent_id=config["curator_agent_id"], user_id=owner["id"],
    )
    bound = await client.put("/api/wiki/config", json={"root": str(root),
        "curator_agent_id": config["curator_agent_id"], "app_id": "search-surface"})
    assert bound.status_code == 200, bound.text
    session = SimpleNamespace(id="search-reader", user_id=owner["id"], agent_id=config["curator_agent_id"])
    task_id = tasks.open(session, "auto", "Find resource evidence")
    try:
        yield article, registry.store(owner["id"]), [
            ("/api/wiki", client.headers),
            ("/apps/search-surface/wiki", {"Authorization": "Bearer " + app_scope_token("search-surface")}),
            ("/api/wiki", {"Authorization": "Bearer " + mint(session.id, user_id=owner["id"], wiki_task_id=task_id)}),
        ]
    finally:
        tasks.close(task_id)
        manager.shutdown()


@pytest.mark.parametrize("view", ["full", "compact"])
async def test_search_keeps_the_last_revision_and_resource_state_across_surfaces(client, surfaces, monkeypatch, view):
    article, store, consumers = surfaces
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 512)
    original = article.read_bytes()
    params = {"query": "note.md", "view": view}
    before = (await client.get("/api/wiki/search", params=params)).json()["items"][0]
    if view == "compact":
        assert not {"stale", "resource_error", "error"} & before.keys()
        assert "metadata" not in before
    article.write_bytes(b"x" * 513)
    store.refresh()
    for base, headers in consumers:
        response = await client.get(f"{base}/search", params=params, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        item, = result["items"]
        assert item["revision"] == before["revision"] and item["id"] == before["id"]
        assert item["stale"] is True
        resource = item if view == "compact" else item["metadata"]
        assert resource["resource_error"] == "document_too_large"
        assert resource["error"]
        assert result["index_status"] == "partial"
        assert (await client.get(f"{base}/read", params={"doc": "note.md"}, headers=headers)).status_code == 413
    article.write_bytes(original)
    store.refresh()
    for base, headers in consumers:
        response = await client.get(f"{base}/search", params=params, headers=headers)
        assert response.status_code == 200, response.text
        current, = response.json()["items"]
        assert current["id"] == before["id"] and current["revision"] == before["revision"]
        assert not current.get("stale", False)
        resource = current if view == "compact" else current["metadata"]
        assert "resource_error" not in resource and "error" not in resource


@pytest.mark.parametrize("view", ["full", "compact"])
async def test_http_search_preserves_its_typed_shape_and_filters_unrelated_fields(client, surfaces, monkeypatch, view):
    _, store, _ = surfaces
    result = store.search("note.md", view=view)
    result["unrelated"] = "not part of the HTTP contract"
    result["items"][0]["unrelated"] = "not part of a search item"
    monkeypatch.setattr(store, "search", lambda *args, **kwargs: result)
    response = await client.get("/api/wiki/search", params={"query": "note.md", "view": view})
    assert response.status_code == 200, response.text
    payload = response.json()
    item, = payload["items"]
    assert "unrelated" not in payload and "unrelated" not in item
    assert payload["index_errors_truncated"] is False
    if view == "compact":
        assert "metadata" not in item and "quality" not in item
        assert not {"stale", "resource_error", "error"} & item.keys()
    else:
        assert "metadata" in item and item["stale"] is False

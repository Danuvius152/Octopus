"""Same Wiki query contract through human and bound App HTTP routes.

Run from the repository root, without a model or production vault:
    .venv/bin/python -m pytest -p tests.conftest -q -s \
        docs/review/wiki_adapter_repros_20261020.py --tb=short

Four cases expose CR-12's additional boolean parsing mismatch on 5d99de373921;
two true/false controls pass. No application backend process is started.
"""

import json
from datetime import UTC, datetime

import pytest

from server.wiki.registry import registry
from tests import test_wiki_api as api

client = api.client


@pytest.fixture
async def bound_app(client, tmp_path, monkeypatch):
    from server.applications import ApplicationManager, app_scope_token
    from server.routers import applications as routes

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "Library/note.md").write_text("# Ordinary note\n\nAn unrelated claim.\n")
    (root / "tmp/asset.md").write_text("# OnlyAssetNeedle\n\nA supporting artifact.\n")
    config = await api.configure(client, root)
    manager = ApplicationManager()
    manager.bind(session_mgr=api.session_manager, db=api.session_manager.db)
    monkeypatch.setattr(routes, "_manager", manager)
    now = datetime.now(UTC).isoformat()
    await api.session_manager.db.save_application(app_id="wiki-review", name="wiki-review",
        app_dir=str(tmp_path / "application"), created_at=now, updated_at=now,
        agent_id=config["curator_agent_id"])
    configured = await client.put("/api/wiki/config", headers=api.HEADERS, json={
        "root": str(root), "curator_agent_id": config["curator_agent_id"], "app_id": "wiki-review"})
    assert configured.status_code == 200, configured.text
    try:
        yield client, {"Authorization": f"Bearer {app_scope_token('wiki-review')}"}
    finally:
        manager.shutdown()
        registry.close()
        registry.resume_access()


@pytest.mark.parametrize("operation,value", [
    ("documents", "true"), ("documents", "false"), ("documents", "1"),
    ("documents", "yes"), ("documents", "invalid"), ("search", "1"),
])
async def test_app_and_main_interpret_include_assets_identically(bound_app, operation, value):
    client, app_headers = bound_app
    params = {"include_assets": value}
    if operation == "search":
        params["query"] = "OnlyAssetNeedle"
    main = await client.get(f"/api/wiki/{operation}", headers=api.HEADERS, params=params)
    app = await client.get(f"/apps/wiki-review/wiki/{operation}", headers=app_headers, params=params)
    facts = {"operation": operation, "include_assets": value,
        "main_status": main.status_code, "app_status": app.status_code}
    if main.status_code == app.status_code == 200:
        facts |= {"main_paths": [item["path"] for item in main.json()["items"]],
            "app_paths": [item["path"] for item in app.json()["items"]]}
    print(json.dumps(facts, sort_keys=True))
    assert app.status_code == main.status_code, facts
    if "main_paths" in facts:
        assert facts["app_paths"] == facts["main_paths"], facts

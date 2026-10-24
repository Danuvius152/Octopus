"""The application bridge consumes the main API's parameter contract."""

from datetime import UTC, datetime

import pytest

from server.applications import ApplicationManager, app_scope_token
from server.routers import applications as app_routes
from server.wiki.jobs import WikiJobs
from tests import test_wiki_api as api
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare


@pytest.fixture
async def bound(client, tmp_path, monkeypatch):
    root, config, jobs = await prepare(client, tmp_path, {
        "Library/note.md": "# Note\nCurrent words.\n", "Assets/raw.md": "# Asset\nCurrent words.\n",
    })
    manager = ApplicationManager()
    manager.bind(api.session_manager, api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    now = datetime.now(UTC).isoformat()
    await api.session_manager.db.save_application(
        app_id="parameters", name="Parameters", app_dir=str(tmp_path / "app"),
        created_at=now, updated_at=now, agent_id=config["curator_agent_id"],
        user_id=jobs.user_id,
    )
    result = await client.put("/api/wiki/config", headers=client.headers,
                              json={"root": str(root), "curator_agent_id": config["curator_agent_id"], "app_id": "parameters"})
    assert result.status_code == 200, result.text
    jobs = WikiJobs(jobs.user_id)
    jobs.store.refresh()
    try:
        yield client, jobs, {"Authorization": "Bearer " + app_scope_token("parameters")}
    finally:
        manager.shutdown()


async def test_app_cancel_uses_the_original_job_and_rejects_stale_target_without_effects(bound):
    client, jobs, headers = bound
    original = jobs.maintain("original", "", "Review", ["Library/note.md"], None, [], "human")
    other = jobs.maintain("other", "", "Review", ["Library/note.md"], None, [], "human")
    for body in ({"expected_attempt": 9}, {"expected_vault_id": "another-vault"}, {"expected_attempt": True}):
        for base, auth in (("/api/wiki", client.headers), ("/apps/parameters/wiki", headers)):
            rejected = await client.post(f"{base}/jobs/{original['id']}/cancel", headers=auth, json=body)
            assert rejected.status_code == (422 if body.get("expected_attempt") is True else 409), rejected.text
            assert jobs.get(original["id"])["status"] == "queued"
    accepted = await client.post(f"/apps/parameters/wiki/jobs/{original['id']}/cancel", headers=headers,
                                 json={"expected_attempt": original["attempt"], "expected_vault_id": jobs.registration["vault_id"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["id"] == original["id"]
    assert jobs.get(original["id"])["status"] == "cancelled"
    assert jobs.get(other["id"])["status"] == "queued"
    verified = await client.post(f"/apps/parameters/wiki/jobs/{original['id']}/verify", headers=headers,
                                 json={"expected_attempt": original["attempt"],
                                       "expected_vault_id": jobs.registration["vault_id"]})
    assert verified.status_code == 200, verified.text
    assert verified.json()["id"] == original["id"]
    assert verified.json()["settlement_phase"] == "confirmed"
    retried = await client.post(f"/apps/parameters/wiki/jobs/{original['id']}/retry", headers=headers,
                                json={"expected_vault_id": jobs.registration["vault_id"]})
    assert retried.status_code == 200 and retried.json()["id"] == original["id"], retried.text
    assert jobs.get(original["id"])["status"] == "queued"


@pytest.mark.parametrize("value", ["true", "false", "1", "0", "on", "OFF", "yes", "invalid"])
async def test_app_and_main_api_parse_boolean_aliases_and_invalid_values_identically(bound, value):
    client, _, headers = bound
    params = {"include_assets": value}
    main = await client.get("/api/wiki/documents", headers=client.headers, params=params)
    bridged = await client.get("/apps/parameters/wiki/documents", headers=headers, params=params)
    assert main.status_code == bridged.status_code
    if main.status_code == 200:
        assert [row["path"] for row in main.json()["items"]] == [row["path"] for row in bridged.json()["items"]]
    else:
        assert main.status_code == 422


@pytest.mark.parametrize("operation,params", [
    ("read", {}), ("read", {"doc": "Library/note.md", "length": "0"}),
    ("read", {"doc": "Library/note.md", "length": "24001"}),
    ("read", {"doc": "Library/note.md", "view": "unknown"}),
    ("documents", {"offset": "-1"}), ("documents", {"limit": "101"}),
    ("documents", {"limit": "1.0"}), ("search", {"query": "Note", "intent": "unknown"}),
    ("jobs", {"limit": "101"}), ("jobs", {"limit": "1"}),
])
async def test_shared_query_defaults_types_and_bounds_match_the_main_route(bound, operation, params):
    client, jobs, headers = bound
    main = await client.get(f"/api/wiki/{operation}", headers=client.headers, params=params)
    bridged = await client.get(f"/apps/parameters/wiki/{operation}", headers=headers, params=params)
    assert main.status_code == bridged.status_code, (main.text, bridged.text)
    assert main.status_code in {200, 422}
    assert jobs.list()["items"] == []

"""Frozen maintenance observations survive enqueues without granting authority."""

from datetime import datetime, timedelta

import pytest

from server.config import settings
from server.mcp_result import TEXT_RESULT_MAX_BYTES, model_result_bytes
from server.wiki.contracts import ReviewObservation
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import entry, prepare
from tests.test_wiki_maintenance_state import two_accounts as two_accounts

FILES = {f"Library/{name}.md": f"# {name}\nCurrent words.\n" for name in ("a", "b", "c")}


async def select(client, *, headers=None, **body):
    response = await client.post("/api/wiki/maintenance-selections", headers=headers or client.headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def submission(selection, item):
    return {"idempotency_key": item["review"]["review_key"], "paths": [item["path"]],
            "review": item["review"], "selection_id": selection["id"],
            "selection_ordinal": item["ordinal"]}


@pytest.mark.parametrize("mode", ["read", "auto", "maintain"])
async def test_selection_observes_without_body_reads_or_write_grants(client, tmp_path, mode):
    _, _, jobs = await prepare(client, tmp_path, FILES | {"Library/attachment.txt": "Original", "else.md": "# Else\n"})
    async with entry(client, mode=mode, request="/wiki维护" if mode == "maintain" else "/wiki") as (headers, task):
        selection = await select(client, headers=headers, limit=1)
        assert selection["candidate_count"] == 3 and len(selection["items"]) == 1
        assert selection["items"][0]["path"] == "Library/a.md" and selection["items"][0]["ordinal"] == 0
        assert selection["next_cursor"] and selection["expires_at"]
        ReviewObservation.model_validate(selection["items"][0]["review"])
        assert not task.reads and not task.raw_reads
        assert not task.document_ids and not task.reservation_ids
        assert jobs.list()["items"] == []
        if mode == "read":
            rejected = await client.post("/api/wiki/maintain", headers=headers,
                                         json=submission(selection, selection["items"][0]))
            assert rejected.status_code == 403


async def test_enqueue_preserves_next_page_and_changed_frozen_item_is_rejected(client, tmp_path):
    root, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    accepted = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][0]))
    assert accepted.status_code == 200, accepted.text
    (root / "Library/new.md").write_text("# Later candidate\n")
    jobs.store.refresh()
    page = await client.get("/api/wiki/maintenance-selections/" + selection["id"], headers=client.headers,
                            params={"limit": 1, "cursor": selection["next_cursor"]})
    assert page.status_code == 200, page.text
    second = page.json()
    assert second["candidate_count"] == 3
    item = second["items"][0]
    assert item["ordinal"] == 1 and item["path"] == "Library/b.md"
    (root / item["path"]).write_text("# B\nA later human version.\n")
    jobs.store.refresh()
    rejected = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, item))
    assert rejected.status_code == 409 and rejected.json()["code"] == "revision_changed", rejected.text
    assert len(jobs.list()["items"]) == 1


async def test_equivalent_selections_reuse_one_job_and_keep_both_provenance_rows(client, tmp_path):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    first = await select(client, limit=1)
    second = await select(client, limit=1)
    assert first["id"] != second["id"] and first["items"][0]["review"] == second["items"][0]["review"]
    results = []
    for selection in (first, second, second):
        response = await client.post("/api/wiki/maintain", headers=client.headers,
                                     json=submission(selection, selection["items"][0]))
        assert response.status_code == 200, response.text
        results.append(response.json()["id"])
    assert len(set(results)) == 1 and len(jobs.list()["items"]) == 1
    with jobs.store.connect() as db:
        evidence = list(db.execute("SELECT selection_id,selection_ordinal,doc_id,review_key FROM job_selection_evidence WHERE job_id=?", (results[0],)))
    assert {(row[0], row[1]) for row in evidence} == {(first["id"], 0), (second["id"], 0)}
    assert all(row[2:] == (first["items"][0]["doc_id"], first["items"][0]["review"]["review_key"]) for row in map(tuple, evidence))


async def test_expiration_rejects_new_selection_admission_but_keeps_accepted_job(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client)
    accepted = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][0]))
    assert accepted.status_code == 200, accepted.text
    later = datetime.fromisoformat(selection["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr("server.wiki.maintenance_selections.utcnow", lambda: later)
    page = await client.get("/api/wiki/maintenance-selections/" + selection["id"], headers=client.headers)
    assert page.status_code == 410 and page.json()["code"] == "selection_expired", page.text
    rejected = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][1]))
    assert rejected.status_code == 410 and rejected.json()["code"] == "selection_expired", rejected.text
    job = jobs.get(accepted.json()["id"])
    assert job["status"] == "queued" and len(jobs.list()["items"]) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM job_selection_evidence WHERE job_id=?", (job["id"],)).fetchone()[0] == 1


async def test_actor_and_cursor_cannot_be_transferred_between_selections(client, tmp_path):
    await prepare(client, tmp_path, FILES)
    first = await select(client, limit=1)
    second = await select(client, limit=1)
    crossed = await client.get("/api/wiki/maintenance-selections/" + second["id"], headers=client.headers,
                               params={"cursor": first["next_cursor"]})
    assert crossed.status_code == 422 and crossed.json()["code"] == "selection_cursor_invalid", crossed.text
    async with entry(client, mode="read", request="/wiki") as (headers, _):
        other_actor = await client.get("/api/wiki/maintenance-selections/" + first["id"], headers=headers)
    assert other_actor.status_code == 403 and other_actor.json()["code"] == "selection_scope_mismatch", other_actor.text


async def test_frozen_observations_are_isolated_between_real_accounts(two_accounts):
    from server.wiki.jobs import WikiJobs
    from server.workspace import paths_for

    context, observations = two_accounts, {}
    client = context["client"]
    for name in ("archer", "vera"):
        account = context[name]
        headers = {"Authorization": "Bearer " + account["token"]}
        root = paths_for(account["user"]["id"]).workspace / "notes"
        for path, text in FILES.items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
        response = await client.put("/api/wiki/config", headers=headers,
            json={"root": str(root), "curator_agent_id": account["agent"]["id"]})
        assert response.status_code == 200, response.text
        observations[name] = await select(client, headers=headers, limit=1)
    headers = {"Authorization": "Bearer " + context["vera"]["token"]}
    foreign = observations["archer"]
    response = await client.get("/api/wiki/maintenance-selections/" + foreign["id"], headers=headers)
    assert response.status_code == 404 and response.json()["code"] == "selection_not_found", response.text
    response = await client.post("/api/wiki/maintain", headers=headers, json=submission(foreign, foreign["items"][0]))
    assert response.status_code in {404, 409}, response.text
    assert WikiJobs(context["vera"]["user"]["id"]).list()["items"] == []


async def test_worker_can_observe_without_expanding_its_assigned_write_scope(client, tmp_path):
    _, config, jobs = await prepare(client, tmp_path, FILES)
    selected = jobs.store.read("Library/c.md", length=1)
    tasks.assign("state-reader", "assigned-job", [{"doc": selected["id"], "path": selected["path"]}], registration=config)
    assignment = dict(tasks.assignments["state-reader"])
    try:
        async with entry(client) as (headers, task):
            selection = await select(client, headers=headers, limit=1)
            response = await client.post("/api/wiki/maintain", headers=headers,
                                         json=submission(selection, selection["items"][0]))
            assert response.status_code == 403 and jobs.list()["items"] == [], response.text
            assert task.document_ids == (selected["id"],) and task.job_id == "assigned-job"
            assert not task.reservation_ids
            assert not task.reads and not task.raw_reads
            assert tasks.assignments[task.session_id] == assignment
    finally:
        tasks.unassign("state-reader")


async def test_candidate_limit_rejects_overflow_without_saving_a_partial_selection(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    monkeypatch.setattr(settings, "wiki_selection_max_candidates", 2)
    response = await client.post("/api/wiki/maintenance-selections", headers=client.headers, json={})
    assert response.status_code == 422 and response.json()["code"] == "selection_limit_exceeded", response.text
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM maintenance_selections").fetchone()[0] == 0


async def test_model_page_keeps_full_reviews_and_continues_after_actual_prefix(client, tmp_path):
    files = {f"Library/note-{number:03}-{'long-name-' * 8}.md": "# Note\n" for number in range(80)}
    await prepare(client, tmp_path, files)
    async with entry(client, mode="read", request="/wiki") as (headers, task):
        selection = await select(client, headers=headers, limit=200)
        assert selection["candidate_count"] == 80 and 0 < len(selection["items"]) < 80
        assert selection["returned_count"] == len(selection["items"])
        assert model_result_bytes(selection) <= TEXT_RESULT_MAX_BYTES
        for item in selection["items"]:
            ReviewObservation.model_validate(item["review"])
        page = await client.get("/api/wiki/maintenance-selections/" + selection["id"], headers=headers,
                                params={"limit": 200, "cursor": selection["next_cursor"]})
        assert page.status_code == 200, page.text
        assert page.json()["items"][0]["ordinal"] == len(selection["items"])
        assert not task.reads and not task.document_ids and not task.reservation_ids


async def test_bound_app_selection_and_human_selection_keep_distinct_actor_scopes(client, tmp_path, monkeypatch):
    from datetime import UTC

    from server.applications import ApplicationManager, app_scope_token
    from server.routers import applications as app_routes
    from tests import test_wiki_api as api

    root, config, jobs = await prepare(client, tmp_path, FILES)
    before_rebind = await select(client, limit=1)
    manager = ApplicationManager()
    manager.bind(session_mgr=api.session_manager, db=api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    try:
        now = datetime.now(UTC).isoformat()
        for app_id in ("sapphire", "other"):
            await api.session_manager.db.save_application(app_id=app_id, name=app_id,
                app_dir=str(tmp_path / app_id), created_at=now, updated_at=now, agent_id=config["curator_agent_id"], user_id=jobs.user_id)
        bound = await client.put("/api/wiki/config", headers=client.headers,
                                 json={"root": str(root), "curator_agent_id": config["curator_agent_id"], "app_id": "sapphire"})
        assert bound.status_code == 200, bound.text
        stale = await client.get("/api/wiki/maintenance-selections/" + before_rebind["id"], headers=client.headers)
        assert stale.status_code == 409 and stale.json()["code"] == "registration_changed", stale.text
        rebound_store = registry.store(jobs.user_id)
        pending = await client.post("/api/wiki/maintenance-selections", headers=client.headers, json={"limit": 1})
        assert pending.status_code == 503 and pending.json()["code"] == "selection_updating", pending.text
        # Rebinding deliberately leaves the successor cold until compensation;
        # the fixture has no background reconciliation worker.
        rebound_store.refresh()
        human = await select(client, limit=1)
        headers = {"Authorization": "Bearer " + app_scope_token("sapphire")}
        created = await client.post("/apps/sapphire/wiki/maintenance-selections", headers=headers, json={"limit": 1})
        assert created.status_code == 200, created.text
        app_selection = created.json()
        same = await client.put("/api/wiki/config", headers=client.headers,
            json={"root": str(root), "curator_agent_id": config["curator_agent_id"], "app_id": "sapphire"})
        assert same.status_code == 200 and same.json()["registration_generation"] == bound.json()["registration_generation"], same.text
        crossed_human = await client.get("/api/wiki/maintenance-selections/" + app_selection["id"], headers=client.headers)
        crossed_app = await client.get("/apps/sapphire/wiki/maintenance-selections/" + human["id"], headers=headers)
        assert crossed_human.status_code == crossed_app.status_code == 403
        own = await client.get("/apps/sapphire/wiki/maintenance-selections/" + app_selection["id"], headers=headers)
        assert own.status_code == 200, own.text
        rejected = await client.get("/apps/other/wiki/maintenance-selections/" + app_selection["id"],
                                    headers={"Authorization": "Bearer " + app_scope_token("other")})
        assert rejected.status_code == 403
        accepted = await client.post("/apps/sapphire/wiki/maintain", headers=headers,
                                     json=submission(app_selection, app_selection["items"][0]))
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["payload"]["actor"] == "app:sapphire"
        restored = await client.put("/api/wiki/config", headers=client.headers,
                                    json={"root": str(root), "curator_agent_id": config["curator_agent_id"]})
        assert restored.status_code == 200, restored.text
        assert restored.json()["registration_generation"] not in {config["registration_generation"], bound.json()["registration_generation"]}
        stale = await client.get("/api/wiki/maintenance-selections/" + before_rebind["id"], headers=client.headers)
        assert stale.status_code == 409 and stale.json()["code"] == "registration_changed", stale.text
    finally:
        manager.shutdown()

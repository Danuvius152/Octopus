"""Maintenance effects respect every inherited request restriction."""

from dataclasses import replace

import pytest

from server.knowledge import AdmittedRequest, admit, revise
from server.mcp_identity import mint, verify
from server.wiki.jobs import WikiJobs
from server.wiki.limits import MaintenanceLimit
from server.wiki.maintenance_contract import actor_for
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from server.wiki.workflow import view
from tests import test_wiki_api as wiki_api

client = wiki_api.client


async def open_turn(client, tmp_path, *, limit, mode="auto", request="Use our Wiki"):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/x.md").write_text("# X\nPreserve X.\n")
    (vault / "Library/y.md").write_text("# Y\nPreserve Y.\n")
    config = await wiki_api.configure(client, vault)
    session = await wiki_api.session_manager.create_session(
        config["curator_agent_id"], "Request limits", str(tmp_path), backend="codex",
    )
    context = replace(admit(session_id=session.id, request_id="limited-request", user_id=session.user_id,
                            prompt=request, registration=(config["vault_id"], config["registration_generation"])), maintenance=limit)
    token = tasks.open(session, mode, request, request_id="limited-request", knowledge=context)
    bearer = mint(session.id, user_id=session.user_id, wiki_task_id=token)
    return token, tasks.active[token], verify(bearer), {"Authorization": f"Bearer {bearer}"}


async def test_read_restriction_blocks_empty_path_material_through_http(client, tmp_path):
    token, _, _, headers = await open_turn(client, tmp_path, limit=MaintenanceLimit(False))
    try:
        response = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "read-empty-material", "material": "# New material\nKeep me.",
        })
        assert response.status_code == 403, response.text
        queried = await client.get("/api/wiki/read", params={"doc": "Library/x.md"}, headers=headers)
        assert queried.status_code == 200, queried.text
    finally:
        tasks.close(token)


async def test_final_writer_retains_all_inherited_path_restrictions(client, tmp_path):
    limit = MaintenanceLimit(True, ("/wiki maintain Library/x.md",)).restrict(
        MaintenanceLimit(True, ("/wiki maintain Library/y.md",)),
    )
    token, task, scope, _ = await open_turn(client, tmp_path, limit=limit)
    try:
        with pytest.raises(WikiError, match="Select an unambiguous"):
            WikiJobs(None).maintain("narrowed-y", "", "Preserve Y", ["Library/y.md"], None, [],
                                    actor_for(task), selection_task=task, mcp_scope=scope)
    finally:
        tasks.close(token)


async def test_agent_definition_target_edit_is_enforced_by_the_actual_final_writer(client, tmp_path):
    existing = admit(session_id="origin", request_id="r", user_id=None, prompt="Normal task")
    editor = admit(session_id="editor", request_id="e", user_id=None, prompt="Normal current task")
    revised = revise(existing, AdmittedRequest(editor.origin, editor), {
        "prompt": "/wiki maintain Library/x.md", "wiki_mode": "maintain",
    })
    token, task, scope, _ = await open_turn(client, tmp_path, limit=revised.maintenance, mode=revised.mode)
    try:
        with pytest.raises(WikiError, match="Select an unambiguous"):
            WikiJobs(None).maintain("outside-revised-definition", "", "Preserve Y", ["Library/y.md"],
                                    None, [], actor_for(task), selection_task=task, mcp_scope=scope)
    finally:
        tasks.close(token)


async def test_empty_paths_cannot_hide_material_outside_the_inherited_scope(client, tmp_path):
    token, task, scope, _ = await open_turn(
        client, tmp_path, limit=MaintenanceLimit(True, ("/wiki maintain Library/x.md",)),
    )
    try:
        with pytest.raises(WikiError, match="Select an unambiguous"):
            WikiJobs(None).maintain("outside-material", "# New material\nKeep me.", "Retain material", [],
                                    "Library/new.md", [], actor_for(task), selection_task=task, mcp_scope=scope)
    finally:
        tasks.close(token)


@pytest.mark.parametrize("creation", [False, True])
async def test_unrestricted_auto_preserves_normal_new_material_admission(client, tmp_path, creation):
    token, _, _, headers = await open_turn(client, tmp_path, limit=MaintenanceLimit(True))
    try:
        body = {"idempotency_key": "auto-material", "material": "# New material\nKeep me."}
        if creation:
            body["creation"] = {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}
        response = await client.post("/api/wiki/maintain", headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"
        assert response.json()["grant"]["allow_new_pages"] is creation
    finally:
        tasks.close(token)


@pytest.mark.parametrize("payload", [
    {"creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}},
    {"creation": {"purpose": "requested_knowledge_page", "paths": None}},
    {"material": "# Retained material\nPreserve this input.", "creation": {
        "purpose": "requested_knowledge_page", "paths": ["Library/x-summary.md"],
    }},
])
async def test_scope_checks_creation_and_framework_material_destinations(client, tmp_path, payload):
    token, _, _, headers = await open_turn(
        client, tmp_path, limit=MaintenanceLimit(True, ("/wiki maintain Library/x-summary.md",)),
    )
    try:
        response = await client.post("/api/wiki/maintain", headers=headers,
                                     json={"idempotency_key": "full-effects", **payload})
        assert response.status_code == 403, response.text
        assert WikiJobs(None).list()["items"] == []
    finally:
        tasks.close(token)


async def test_explicit_scope_can_admit_its_exact_new_page(client, tmp_path):
    token, _, _, headers = await open_turn(
        client, tmp_path, mode="maintain", request="/wiki maintain Library/new.md",
        limit=MaintenanceLimit(True, ("/wiki maintain Library/new.md",)),
    )
    try:
        response = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "exact-new-page", "creation": {
                "purpose": "requested_knowledge_page", "paths": ["Library/new.md"],
            },
        })
        assert response.status_code == 200, response.text
        assert response.json()["grant"]["allowed_new_paths"] == ["Library/new.md"]
    finally:
        tasks.close(token)


@pytest.mark.parametrize("same_key", [False, True])
async def test_replay_and_material_dedup_recheck_the_actual_admitted_destination(client, tmp_path, same_key):
    token, task, scope, _ = await open_turn(client, tmp_path, limit=MaintenanceLimit(True))
    second_token = None
    try:
        jobs = WikiJobs(None)
        material = "# Repeated material\nPreserve this input."
        original = jobs.maintain("original-material", material, "Preserve", [], None, [], actor_for(task),
                                 selection_task=task, mcp_scope=scope)
        session = wiki_api.session_manager.sessions[task.session_id]
        narrowed = replace(task.knowledge, maintenance=MaintenanceLimit(True, ("/wiki maintain Library/x.md",)))
        second_token = tasks.open(session, "auto", request_id="narrowed-return", knowledge=narrowed)
        second = tasks.active[second_token]
        second_scope = verify(mint(session.id, wiki_task_id=second_token))
        with pytest.raises(WikiError, match="Select an unambiguous"):
            jobs.maintain("original-material" if same_key else "alias-material", material, "Preserve", [], None, [],
                          actor_for(second), selection_task=second, mcp_scope=second_scope)
        assert [item["id"] for item in jobs.list()["items"]] == [original["id"]]
    finally:
        tasks.close(second_token)
        tasks.close(token)


async def test_reusing_current_material_cannot_escape_the_inherited_scope(client, tmp_path):
    token, _, _, headers = await open_turn(
        client, tmp_path, limit=MaintenanceLimit(True, ("/wiki maintain Library/x.md",)),
    )
    try:
        response = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "reuse-outside-scope", "material": "# Y\nPreserve Y.\n",
        })
        assert response.status_code == 403, response.text
        assert WikiJobs(None).list()["items"] == []
    finally:
        tasks.close(token)


async def test_policy_card_and_visible_tools_expose_restrictions_and_source_requirements(client, tmp_path):
    token, task, _, _ = await open_turn(
        client, tmp_path, limit=MaintenanceLimit(False), request="/wiki Explain this question",
    )
    try:
        current = view(task)
        assert current.card["source_requirements"] == ["wiki", "web"]
        assert current.card["actor_role"] == "query" and current.card["may_submit"] is False
        assert {"search", "read", "read_many"} <= current.tools
        assert "maintain" not in current.tools
    finally:
        tasks.close(token)


@pytest.mark.parametrize("binding", [None, ("another-vault", "another-generation")])
async def test_missing_or_changed_registration_keeps_queries_but_rejects_submission(client, tmp_path, binding):
    token, task, _, headers = await open_turn(client, tmp_path, limit=MaintenanceLimit(True))
    second_token = None
    try:
        session = wiki_api.session_manager.sessions[task.session_id]
        second_token = tasks.open(session, "auto", knowledge=replace(task.knowledge, registration=binding))
        bearer = mint(session.id, wiki_task_id=second_token)
        headers = {"Authorization": f"Bearer {bearer}"}
        response = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "unknown-binding", "material": "# New knowledge\nPreserve this input.",
        })
        assert response.status_code == 403, response.text
        queried = await client.get("/api/wiki/read", params={"doc": "Library/x.md"}, headers=headers)
        assert queried.status_code == 200, queried.text
        assert "maintain" not in view(tasks.active[second_token]).tools
    finally:
        tasks.close(second_token)
        tasks.close(token)


@pytest.mark.parametrize("existing", [False, True])
async def test_full_curation_and_its_subscription_cannot_exceed_the_request_scope(client, tmp_path, existing):
    token, task, scope, _ = await open_turn(
        client, tmp_path, limit=MaintenanceLimit(True, ("/wiki maintain Library/x.md",)),
    )
    try:
        jobs = WikiJobs(None)
        if existing:
            jobs.curate("full-curation", "Review all current originals")
        with pytest.raises(WikiError, match="Select an unambiguous"):
            jobs.curate("full-curation", "Review all current originals", task=task, mcp_scope=scope)
        assert len(jobs.list()["items"]) == int(existing)
    finally:
        tasks.close(token)


@pytest.mark.parametrize("allowed", [False, True])
async def test_checked_daily_admission_requires_every_inherited_selection(client, tmp_path, allowed):
    token, task, scope, headers = await open_turn(
        client, tmp_path, mode="maintain", request="/wiki maintain",
        limit=MaintenanceLimit(True, ("/wiki maintain", "/wiki maintain Library/x.md")),
    )
    try:
        path = "Library/x.md" if allowed else "Library/y.md"
        observed = await client.get("/api/wiki/maintenance-status", params={"doc": path}, headers=headers)
        assert observed.status_code == 200, observed.text
        review = observed.json()["items"][0]["review"]
        jobs = WikiJobs(None)
        if allowed:
            accepted = jobs.maintain(review["review_key"], "", "Checked review", [path], None, [], actor_for(task),
                                     review=review, selection_task=task, mcp_scope=scope)
            assert accepted["status"] == "queued"
        else:
            with pytest.raises(WikiError, match="Select an unambiguous"):
                jobs.maintain(review["review_key"], "", "Checked review", [path], None, [], actor_for(task),
                              review=review, selection_task=task, mcp_scope=scope)
            assert jobs.list()["items"] == []
    finally:
        tasks.close(token)


async def test_a_real_worker_grant_survives_a_prohibition_on_new_submissions(client, tmp_path):
    token, task, _, _ = await open_turn(client, tmp_path, limit=MaintenanceLimit(True))
    worker_token = None
    try:
        jobs = WikiJobs(None)
        job = jobs.maintain("assigned-worker", "", "Review only X", ["Library/x.md"], None, [], "human")
        session = wiki_api.session_manager.sessions[task.session_id]
        jobs.update(job["id"], status="running", session_id=session.id)
        tasks.assign(session.id, job["id"], job["items"], registration=jobs.registration)
        context = replace(task.knowledge, maintenance=MaintenanceLimit(False))
        worker_token = tasks.open(session, "maintain", knowledge=context)
        headers = {"Authorization": "Bearer " + mint(session.id, wiki_task_id=worker_token)}
        current = await client.get("/api/wiki/read", params={"doc": "Library/x.md"}, headers=headers)
        assert current.status_code == 200, current.text
        saved = await client.post("/api/wiki/revise", headers=headers, json={
            "doc": current.json()["id"], "expected_revision": current.json()["revision"],
            "operation": "no_change", "reason": "Current source already fits its purpose",
        })
        assert saved.status_code == 200, saved.text
        denied = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "worker-new-material", "material": "# More material\nA separate task.",
        })
        assert denied.status_code == 403, denied.text
        current_policy = view(tasks.active[worker_token])
        assert current_policy.card["actor_role"] == "worker"
        assert current_policy.card["may_submit"] is False
        assert "revise" in current_policy.tools and "maintain" not in current_policy.tools
    finally:
        tasks.close(worker_token)
        tasks.unassign(task.session_id)
        tasks.close(token)

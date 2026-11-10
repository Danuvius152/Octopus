"""The application bridge consumes the main API's parameter contract."""

import asyncio
import fcntl
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.applications import ApplicationManager, app_scope_token
from server.routers import applications as app_routes
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import revision
from tests import wiki_api as api
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


def human_mutation(jobs, operation):
    from tests.test_wiki_history import legacy_snapshot

    history = WikiHistory(jobs.store)
    path = "Library/note.md"
    original = jobs.store.path(path).read_bytes()
    changed = history.write(path, b"# Current\nCurrent editable words.\n", revision(original), "human", "prepare restore")
    if operation == "document":
        return "PUT", {"path": path, "content": "# New\nAuthorized edit.\n", "expected_revision": changed["revision"], "reason": "Edit"}
    if operation == "restore":
        return "POST", {"change_id": changed["change_id"], "expected_revision": changed["revision"], "side": "before"}
    snapshot = legacy_snapshot(history, {path: original})
    return "POST", {"snapshot_id": snapshot["id"], "paths": [path], "expected_revisions": {path: changed["revision"]}}


def mutation_evidence(jobs):
    with jobs.store.connect() as db:
        return jobs.store.path("Library/note.md").read_bytes(), [
            [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("changes", "operation_receipts")
        ]


@pytest.mark.parametrize("operation", ["document", "restore", "snapshot/restore"])
@pytest.mark.parametrize("rebind", [False, True])
async def test_retired_app_request_cannot_write_after_body_wait_even_when_rebound(bound, operation, rebind):
    client, jobs, headers = bound
    method, body = human_mutation(jobs, operation)
    before = mutation_evidence(jobs)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_body():
        entered.set()
        await release.wait()
        yield json.dumps(body).encode()

    pending = asyncio.create_task(client.request(method, f"/apps/parameters/wiki/{operation}",
        headers={**headers, "Content-Type": "application/json"}, content=delayed_body()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        root = jobs.store.root
        registry.configure(jobs.user_id, root, jobs.registration["curator_agent_id"], "another-app")
        if rebind:
            registry.configure(jobs.user_id, root, jobs.registration["curator_agent_id"], "parameters")
        release.set()
        response = await asyncio.wait_for(pending, 3)
        assert response.status_code in {403, 409}, response.text
        assert mutation_evidence(WikiJobs(jobs.user_id)) == before
        # Rebinding grants new requests authority, never the old request.
        registry.configure(jobs.user_id, root, jobs.registration["curator_agent_id"], "parameters")
        fresh = await client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body)
        assert fresh.status_code == 200, fresh.text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("operation", ["document", "restore", "snapshot/restore"])
@pytest.mark.parametrize("rebind", [False, True])
async def test_app_mutation_rechecks_registration_after_real_history_lock_wait(bound, monkeypatch, operation, rebind):
    client, jobs, headers = bound
    method, body = human_mutation(jobs, operation)
    before = mutation_evidence(jobs)
    history = WikiHistory(jobs.store)
    lock_path = history.state / "vault.lock"
    flock = fcntl.flock
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def observe_flock(handle, flags):
        if flags == fcntl.LOCK_EX and getattr(handle, "name", None) == str(lock_path):
            loop.call_soon_threadsafe(entered.set)
        return flock(handle, flags)

    with lock_path.open("a+b") as handle:
        flock(handle, fcntl.LOCK_EX)
        monkeypatch.setattr(fcntl, "flock", observe_flock)
        pending = asyncio.create_task(client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(asyncio.to_thread(registry.configure, jobs.user_id, jobs.store.root,
                jobs.registration["curator_agent_id"], "another-app"), 3)
            if rebind:
                await asyncio.wait_for(asyncio.to_thread(registry.configure, jobs.user_id, jobs.store.root,
                    jobs.registration["curator_agent_id"], "parameters"), 3)
            assert not pending.done()
        finally:
            flock(handle, fcntl.LOCK_UN)
            response = await asyncio.wait_for(pending, 3)
    assert response.status_code in {403, 409}, response.text
    assert mutation_evidence(WikiJobs(jobs.user_id)) == before


@pytest.mark.parametrize("operation", ["document", "restore", "snapshot/restore"])
async def test_protected_human_mutation_finishes_before_rebinding_can_commit(bound, monkeypatch, operation):
    client, jobs, headers = bound
    original = jobs.store.path("Library/note.md").read_bytes()
    method, body = human_mutation(jobs, operation)
    entered, changing = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    replace = os.replace
    destination = jobs.store.path("Library/note.md")
    account = registry._account_lock(jobs.user_id)

    def held_effect(source, target, *args, **kwargs):
        if Path(target) == destination:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "Original protected write did not resume"
        return replace(source, target, *args, **kwargs)

    def rebind():
        # Probe the real lock from the competing thread. No authorization or
        # effect boundary is replaced; configure itself must wait on this lock.
        acquired = account.acquire(blocking=False)
        if acquired:
            account.release()
        assert not acquired, "The file effect escaped its registration protection"
        loop.call_soon_threadsafe(changing.set)
        return registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"], "another-app")

    monkeypatch.setattr(os, "replace", held_effect)
    pending = asyncio.create_task(client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body))
    change = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        change = asyncio.create_task(asyncio.to_thread(rebind))
        await asyncio.wait_for(changing.wait(), 3)
        assert not change.done()
        release.set()
        response, config = await asyncio.wait_for(asyncio.gather(pending, change), 5)
        assert response.status_code == 200, response.text
        assert config["app_id"] == "another-app"
        expected = body["content"].encode() if operation == "document" else original
        assert destination.read_bytes() == expected
        before_refusal = mutation_evidence(WikiJobs(jobs.user_id))
        history = WikiHistory(registry.store(jobs.user_id)).history("Library/note.md")["items"]
        assert history and all(row["status"] == "applied" for row in history)
        refused = await client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body)
        assert refused.status_code == 403
        assert mutation_evidence(WikiJobs(jobs.user_id)) == before_refusal
    finally:
        release.set()
        await asyncio.gather(pending, *([change] if change else []), return_exceptions=True)


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


async def test_app_and_main_api_parse_boolean_aliases_and_invalid_values_identically(bound):
    client, _, headers = bound
    # Read-only variants share the same account, vault and application binding.
    for value in ["true", "false", "1", "0", "on", "OFF", "yes", "invalid"]:
        params = {"include_assets": value}
        main = await client.get("/api/wiki/documents", headers=client.headers, params=params)
        bridged = await client.get("/apps/parameters/wiki/documents", headers=headers, params=params)
        assert main.status_code == bridged.status_code, (value, main.text, bridged.text)
        if main.status_code == 200:
            assert [row["path"] for row in main.json()["items"]] == [row["path"] for row in bridged.json()["items"]], value
        else:
            assert main.status_code == 422, value


async def test_shared_query_defaults_types_and_bounds_match_the_main_route(bound):
    client, jobs, headers = bound
    for operation, params in [
        ("read", {}), ("read", {"doc": "Library/note.md", "length": "0"}),
        ("read", {"doc": "Library/note.md", "length": "24001"}),
        ("read", {"doc": "Library/note.md", "view": "unknown"}),
        ("documents", {"offset": "-1"}), ("documents", {"limit": "101"}),
        ("documents", {"limit": "1.0"}), ("search", {"query": "Note", "intent": "unknown"}),
        ("jobs", {"limit": "101"}), ("jobs", {"limit": "1"}),
    ]:
        main = await client.get(f"/api/wiki/{operation}", headers=client.headers, params=params)
        bridged = await client.get(f"/apps/parameters/wiki/{operation}", headers=headers, params=params)
        assert main.status_code == bridged.status_code, (operation, params, main.text, bridged.text)
        assert main.status_code in {200, 422}, (operation, params, main.text)
        assert jobs.list()["items"] == [], (operation, params)


@pytest.mark.parametrize("operation", ["verify", "cancel", "retry"])
@pytest.mark.parametrize("body,valid", [
    ("", True), ("null", True), ("{}", True),
    ('{"expected_attempt":0}', True), ('{"expected_attempt":true}', False),
    ('{"expected_attempt":-1}', False), ('{"expected_attempt":"one"}', False),
    ('{"unexpected":1}', False), ('{"operation_id":""}', False),
    ('[]', False), ('false', False), ('"text"', False), ('{', False),
])
async def test_job_control_body_contract_matches_without_rejected_effects(bound, monkeypatch, operation, body, valid):
    client, jobs, headers = bound
    calls = []

    async def control(self, job_id, *args, **kwargs):
        calls.append((job_id, kwargs))
        return {"id": job_id}

    if operation == "retry":
        def retry(self, job_id, **kwargs):
            calls.append((job_id, kwargs))
            return {"id": job_id}
        monkeypatch.setattr(WikiJobs, operation, retry)
    else:
        monkeypatch.setattr(WikiJobs, operation, control)
    expected = 200 if valid and (operation != "verify" or body not in {"", "null"}) else 422
    for base, auth in (("/api/wiki", client.headers), ("/apps/parameters/wiki", headers)):
        response = await client.post(f"{base}/jobs/original/{operation}", headers={**auth, "Content-Type": "application/json"}, content=body)
        assert response.status_code == expected, (base, body, response.text)
    assert len(calls) == (2 if expected == 200 else 0)

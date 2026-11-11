"""The application bridge consumes the main API's parameter contract."""

import asyncio
import fcntl
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from server.applications import ApplicationManager, app_scope_token
from server.routers import applications as app_routes
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import revision
from server.workspace import paths_for
from tests import wiki_api as api
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare

CONTENT_OPERATIONS = ["document", "restore", "snapshot/restore", "publish", "organize", "restructure", "revise"]
ADMISSION_OPERATIONS = ["maintain", "maintain-batch", "curate", "maintenance-selections"]
CONTROL_OPERATIONS = ["cancel", "retry", "verify"]


@pytest.fixture
async def bound(client, tmp_path, monkeypatch):
    root, config, jobs = await prepare(client, tmp_path, {
        "Library/note.md": "# Note\nCurrent words.\n", "Assets/raw.md": "# Asset\nCurrent words.\n",
    })
    manager = ApplicationManager()
    manager.bind(api.session_manager, api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    directory = paths_for(jobs.user_id).applications / "parameters"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<p>App</p>")
    now = datetime.now(UTC).isoformat()
    await api.session_manager.db.save_application(
        app_id="parameters", name="Parameters", app_dir=str(directory),
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
    if operation == "snapshot/restore":
        snapshot = legacy_snapshot(history, {path: original})
        return "POST", {"snapshot_id": snapshot["id"], "paths": [path], "expected_revisions": {path: changed["revision"]}}
    if operation == "publish":
        return "POST", {"path": "Wiki/sources/request.md", "content": "# Request\nAuthorized source summary.\n",
                        "expected_revision": None, "reason": "Publish", "kind": "source",
                        "sources": [{"doc": path, "revision": changed["revision"], "location": "全文"}]}
    if operation == "organize":
        return "POST", {"path": path, "expected_revision": changed["revision"],
                        "key_note": "Protected opening points.", "reason": "Organize"}
    if operation == "restructure":
        blocks = WikiCuration(jobs.store).blocks(path)["blocks"]
        return "POST", {"path": path, "expected_revision": changed["revision"], "order": [part["id"] for part in blocks],
                        "additions": [{"before": len(blocks), "content": "Authorized structure addition.", "sources": []}],
                        "reason": "Restructure"}
    assert operation == "revise"
    return "POST", {"doc": path, "expected_revision": changed["revision"], "operation": "no_change", "reason": "Verified current words"}


def mutation_evidence(jobs):
    with jobs.store.connect() as db:
        return {str(path.relative_to(jobs.store.root)): path.read_bytes()
                for path in jobs.store.root.rglob("*") if path.is_file()}, [
            [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("changes", "operation_receipts")
        ]


async def admission_body(client, headers, operation):
    if operation == "maintain":
        return {"idempotency_key": "request", "paths": ["Library/note.md"]}
    if operation == "curate":
        return {"idempotency_key": "request"}
    if operation == "maintenance-selections":
        return {"directory": "Library", "limit": 1}
    selected = await client.post("/apps/parameters/wiki/maintenance-selections", headers=headers,
                                 json={"directory": "Library", "limit": 1})
    assert selected.status_code == 200, selected.text
    return {"selection_id": selected.json()["id"], "ordinals": [0]}


def control_body(jobs, operation):
    job = jobs.maintain("control-target", "", "Review", ["Library/note.md"], None, [], "human")
    if operation != "cancel":
        jobs.update(job["id"], status="failed")
    return f"jobs/{job['id']}/{operation}", {"operation_id": "original-control", "expected_attempt": 0,
                                            "expected_vault_id": jobs.registration["vault_id"]}


def admission_evidence(jobs):
    with jobs.store.connect() as db:
        return [[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in (
            "jobs", "job_requests", "wiki_job_grants", "wiki_job_subscribers", "maintenance_selections",
            "maintenance_selection_items", "job_selection_evidence", "wiki_job_controls")]


def hold_wiki_sql(monkeypatch, predicate, *, committed=False):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    connect = sqlite3.connect

    def hold():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "Original Wiki transaction was not released"

    class HeldConnection(sqlite3.Connection):
        effect_seen = False

        def execute(self, statement, *args, **kwargs):
            cursor = super().execute(statement, *args, **kwargs)
            if predicate(statement):
                self.effect_seen = True
                if not committed:
                    hold()
            return cursor

        def commit(self):
            result = super().commit()
            if committed and self.effect_seen:
                hold()
            return result

    def connection(*args, **kwargs):
        return connect(*args, **(kwargs | {"factory": HeldConnection}))

    monkeypatch.setattr(sqlite3, "connect", connection)
    return entered, release


@pytest.mark.parametrize("operation", ADMISSION_OPERATIONS + CONTROL_OPERATIONS)
@pytest.mark.parametrize("committed", [False, True])
async def test_app_effect_retains_commit_and_handoff_after_http_cancellation(bound, monkeypatch, operation, committed):
    client, jobs, headers = bound
    if operation in CONTROL_OPERATIONS:
        endpoint, body = control_body(jobs, operation)
        table = "wiki_job_controls"
    else:
        endpoint, body = operation, await admission_body(client, headers, operation)
        table = "maintenance_selections" if operation == "maintenance-selections" else "jobs"
    entered, release = hold_wiki_sql(monkeypatch, lambda sql: sql.startswith("INSERT INTO " + table), committed=committed)
    deleting = asyncio.Event()
    delete = app_routes._get_manager().delete_application

    async def observe_delete(*args, **kwargs):
        deleting.set()
        await delete(*args, **kwargs)

    monkeypatch.setattr(app_routes._get_manager(), "delete_application", observe_delete)
    pending = asyncio.create_task(client.post(f"/apps/parameters/wiki/{endpoint}", headers=headers, json=body))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pending.cancel()
        deletion = asyncio.create_task(client.delete("/api/applications/parameters", headers=client.headers,
                                                    params={"keep_files": "true"}))
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done(), "App deletion escaped an original commit/handoff"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        deleted = await asyncio.wait_for(deletion, 3)
        assert deleted.status_code == 204, deleted.text
        with jobs.store.connect() as db:
            if operation == "maintenance-selections":
                assert db.execute("SELECT count(*) FROM maintenance_selections").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM maintenance_selection_items").fetchone()[0] == 1
            else:
                assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM wiki_job_grants").fetchone()[0] == 1
                if operation in CONTROL_OPERATIONS:
                    assert db.execute("SELECT count(*) FROM wiki_job_controls").fetchone()[0] == 1
                    status = db.execute("SELECT status FROM jobs").fetchone()[0]
                    assert status == {"cancel": "cancelled", "retry": "queued", "verify": "completed_with_attention"}[operation]
                elif operation == "maintain-batch":
                    assert db.execute("SELECT count(*) FROM job_selection_evidence").fetchone()[0] == 1
        if operation == "cancel":
            job_id = endpoint.split("/")[1]
            await jobs.settle_cancelled(job_id, None)
            assert jobs.get(job_id)["settlement_phase"] == "confirmed"
        assert registry.config(jobs.user_id) == jobs.registration
    finally:
        release.set()
        await asyncio.gather(pending, *([deletion] if deletion else []), return_exceptions=True)


@pytest.mark.parametrize("operation", ADMISSION_OPERATIONS + CONTROL_OPERATIONS)
@pytest.mark.parametrize("change", ["rebind", "aba", "same", "delete"])
async def test_original_app_admission_eligibility_survives_body_wait(bound, operation, change):
    client, jobs, headers = bound
    if operation in CONTROL_OPERATIONS:
        endpoint, body = control_body(jobs, operation)
    else:
        endpoint, body = operation, await admission_body(client, headers, operation)
    before = admission_evidence(jobs)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_body():
        entered.set()
        await release.wait()
        yield json.dumps(body).encode()

    pending = asyncio.create_task(client.post(f"/apps/parameters/wiki/{endpoint}",
        headers={**headers, "Content-Type": "application/json"}, content=delayed_body()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "delete":
            deleted = await client.delete("/api/applications/parameters", headers=client.headers, params={"keep_files": "true"})
            assert deleted.status_code == 204, deleted.text
        else:
            registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"],
                               "parameters" if change == "same" else "another-app")
            if change == "aba":
                registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"], "parameters")
        release.set()
        response = await asyncio.wait_for(pending, 3)
        current = WikiJobs(jobs.user_id)
        if change == "same":
            assert response.status_code == 200, response.text
            assert admission_evidence(current) != before
        else:
            assert response.status_code in {403, 404, 409}, response.text
            assert admission_evidence(current) == before
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("keep_files", [False, True])
@pytest.mark.parametrize("phase", ["complete", "retiring", "files"])
@pytest.mark.parametrize("operation", CONTENT_OPERATIONS)
async def test_app_deletion_rejects_unadmitted_body_without_wiki_effects(bound, monkeypatch, keep_files, phase, operation):
    client, jobs, headers = bound
    method, body = human_mutation(jobs, operation)
    before = mutation_evidence(jobs)
    registration = registry.config(jobs.user_id)
    entered, release = asyncio.Event(), asyncio.Event()
    deleting, finish_delete = asyncio.Event(), asyncio.Event()
    database = api.session_manager.db
    set_deletion = database.set_application_deletion

    async def hold_deletion(identifier, state):
        await set_deletion(identifier, state)
        if state["phase"] == phase:
            deleting.set()
            await finish_delete.wait()

    monkeypatch.setattr(database, "set_application_deletion", hold_deletion)

    async def delayed_body():
        entered.set()
        await release.wait()
        yield json.dumps(body).encode()

    pending = asyncio.create_task(client.request(method, f"/apps/parameters/wiki/{operation}",
        headers={**headers, "Content-Type": "application/json"}, content=delayed_body()))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        deletion = asyncio.create_task(client.delete("/api/applications/parameters", headers=client.headers,
                                                    params={"keep_files": str(keep_files).lower()}))
        if phase == "complete":
            deleted = await asyncio.wait_for(deletion, 3)
            assert deleted.status_code == 204, deleted.text
        else:
            await asyncio.wait_for(deleting.wait(), 3)
        fresh = await client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body)
        assert fresh.status_code in {404, 409}, fresh.text
        release.set()
        response = await asyncio.wait_for(pending, 3)
        assert response.status_code in {404, 409}, response.text
        assert mutation_evidence(jobs) == before
        assert registry.config(jobs.user_id) == registration
    finally:
        release.set()
        finish_delete.set()
        await asyncio.gather(pending, *([deletion] if deletion else []), return_exceptions=True)


@pytest.mark.parametrize("operation", CONTENT_OPERATIONS)
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


@pytest.mark.parametrize("operation", CONTENT_OPERATIONS)
async def test_same_configuration_save_preserves_original_content_caller(bound, operation):
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
        saved = registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"], "parameters")
        assert saved == jobs.registration
        release.set()
        response = await asyncio.wait_for(pending, 3)
        assert response.status_code == 200, response.text
        assert mutation_evidence(jobs) != before
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("cancel", ["none", "asyncio", "anyio"])
@pytest.mark.parametrize("keep_files", [False, True])
async def test_app_deletion_waits_for_admitted_file_effect_after_waiter_cancellation(bound, monkeypatch, cancel, keep_files):
    client, jobs, headers = bound
    method, body = human_mutation(jobs, "document")
    entered, deleting, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    destination = jobs.store.path("Library/note.md")
    replace = os.replace
    delete = app_routes._get_manager().delete_application
    scope = None

    def held_effect(source, target, *args, **kwargs):
        if Path(target) == destination:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "Admitted file effect was never released"
        return replace(source, target, *args, **kwargs)

    async def observe_delete(*args, **kwargs):
        deleting.set()
        await delete(*args, **kwargs)

    async def request():
        nonlocal scope
        with anyio.CancelScope() as scope:
            response = await client.request(method, "/apps/parameters/wiki/document", headers=headers, json=body)
        cancelled.set()
        return response if not scope.cancel_called else None

    monkeypatch.setattr(os, "replace", held_effect)
    monkeypatch.setattr(app_routes._get_manager(), "delete_application", observe_delete)
    pending = asyncio.create_task(request())
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if cancel == "asyncio":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif cancel == "anyio":
            scope.cancel()
            await asyncio.wait_for(cancelled.wait(), 3)
        deletion = asyncio.create_task(client.delete("/api/applications/parameters", headers=client.headers,
                                                    params={"keep_files": str(keep_files).lower()}))
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
        assert destination.read_text() == "# Current\nCurrent editable words.\n"
        release.set()
        deleted = await asyncio.wait_for(deletion, 3)
        assert deleted.status_code == 204, deleted.text
        assert destination.read_text() == body["content"]
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 2
        assert registry.config(jobs.user_id) == jobs.registration
        if cancel == "none":
            assert (await pending).status_code == 200
    finally:
        release.set()
        await asyncio.gather(pending, *([deletion] if deletion else []), return_exceptions=True)


@pytest.mark.parametrize("operation", CONTENT_OPERATIONS)
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


@pytest.mark.parametrize("operation", CONTENT_OPERATIONS)
async def test_protected_human_mutation_finishes_before_rebinding_can_commit(bound, monkeypatch, operation):
    client, jobs, headers = bound
    original = jobs.store.path("Library/note.md").read_bytes()
    method, body = human_mutation(jobs, operation)
    entered, changing = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    replace = os.replace
    destination = jobs.store.root / body.get("path", "Library/note.md")
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
    if operation == "revise":
        entered, release = hold_wiki_sql(monkeypatch, lambda sql: sql.startswith("INSERT INTO operation_receipts"))
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
        if operation in {"document", "restore", "snapshot/restore"}:
            expected = body["content"].encode() if operation == "document" else original
            assert destination.read_bytes() == expected
        elif operation == "revise":
            assert not response.json()["changed"] and response.json()["receipt_id"]
        else:
            expected_text = {"publish": "Authorized source summary.", "organize": "Protected opening points.",
                             "restructure": "Authorized structure addition."}[operation]
            assert expected_text in destination.read_text()
        before_refusal = mutation_evidence(WikiJobs(jobs.user_id))
        history = WikiHistory(registry.store(jobs.user_id)).history("Library/note.md")["items"]
        assert history and all(row["status"] == "applied" for row in history)
        refused = await client.request(method, f"/apps/parameters/wiki/{operation}", headers=headers, json=body)
        assert refused.status_code == 403
        assert mutation_evidence(WikiJobs(jobs.user_id)) == before_refusal
    finally:
        release.set()
        await asyncio.gather(pending, *([change] if change else []), return_exceptions=True)


@pytest.mark.parametrize("operation", ["maintain", "maintain-batch", "curate"])
async def test_admitted_wiki_worker_continues_after_app_deletion(bound, monkeypatch, operation):
    from server.wiki import jobs as jobs_module
    from server.wiki.jobs import WikiWorker
    from tests.fake_run import capture_model_runs

    client, jobs, headers = bound
    body = await admission_body(client, headers, operation)
    runs = capture_model_runs(api.session_manager, monkeypatch, hold=True)
    worker = WikiWorker()
    monkeypatch.setattr(jobs_module, "worker", worker)
    await worker.start(api.session_manager)
    try:
        accepted = await client.post(f"/apps/parameters/wiki/{operation}", headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
        job_id = accepted.json()["jobs"][0]["id"] if operation == "maintain-batch" else accepted.json()["id"]
        async with asyncio.timeout(3):
            while not runs:
                await asyncio.sleep(0)
        original = runs[0]
        deleted = await asyncio.wait_for(client.delete("/api/applications/parameters", headers=client.headers,
                                                       params={"keep_files": "true"}), 3)
        assert deleted.status_code == 204, deleted.text
        assert api.session_manager.get_session(original.session_id) is not None
        assert not original.release.is_set(), "App deletion retired the independent Wiki execution"
        original.release.set()
        async with asyncio.timeout(3):
            while jobs.get(job_id)["status"] in {"running", "queued"}:
                await asyncio.sleep(0)
        current = jobs.get(job_id)
        assert current["status"] == "completed_with_attention"  # The controlled model publishes no receipt.
        assert current["grant"]["registration_generation"] == jobs.registration["registration_generation"]
        terminal = await api.session_manager.db.get_committed_terminal(original.session_id, original.request_id)
        assert terminal["status"] == "success"
        assert registry.config(jobs.user_id) == jobs.registration
        assert len(runs) == 1
    finally:
        for run in runs:
            run.release.set()
        await worker.stop()


@pytest.mark.parametrize("rebind", [False, True])
async def test_verify_rechecks_original_binding_before_recovering_prior_execution(bound, monkeypatch, rebind):
    from server.wiki import job_execution
    from server.wiki.jobs import worker
    from tests.fake_run import capture_model_runs

    client, jobs, headers = bound
    manager = api.session_manager
    capture_model_runs(manager, monkeypatch)
    session = await manager.create_session(jobs.registration["curator_agent_id"])
    receipt = await manager.start_message(session.id, "Complete the original execution")
    assert (await receipt.wait()).outcome.status == "success"
    await manager.await_session_execution(session.id)
    job = jobs.maintain("verify-target", "", "Review", ["Library/note.md"], None, [], "human")
    running = jobs.update(job["id"], status="running")
    reference = job_execution.prepare(jobs, running, running["items"], session.id, receipt.request_id)
    job_execution.transition(jobs, job["id"], reference, "unknown", retired=True, recording_state="pending")
    jobs.update(job["id"], status="interrupted")
    monkeypatch.setattr(worker, "manager", manager)
    entered, release = asyncio.Event(), asyncio.Event()
    get_terminal = manager.db.get_committed_terminal

    async def held_terminal(*args):
        terminal = await get_terminal(*args)
        entered.set()
        await release.wait()
        return terminal

    monkeypatch.setattr(manager.db, "get_committed_terminal", held_terminal)
    before = admission_evidence(jobs)
    pending = asyncio.create_task(client.post(f"/apps/parameters/wiki/jobs/{job['id']}/verify", headers=headers,
        json={"operation_id": "verify-original", "expected_attempt": running["attempt"]}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"], "another-app")
        if rebind:
            registry.configure(jobs.user_id, jobs.store.root, jobs.registration["curator_agent_id"], "parameters")
        release.set()
        response = await asyncio.wait_for(pending, 3)
        # A captured Store can retire during the main-database wait; its
        # existing 503 is a refusal, never authority to reacquire for effects.
        assert response.status_code in {403, 409, 503}, response.text
        current = WikiJobs(jobs.user_id)
        assert admission_evidence(current) == before
        assert current.get(job["id"])["items"][0]["worker_request"]["phase"] == "unknown"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_unrelated_app_can_commit_while_an_original_app_file_effect_waits(bound, tmp_path, monkeypatch):
    from tests.accounts import create_account

    client, jobs, headers = bound
    method, body = human_mutation(jobs, "document")
    database = api.session_manager.db
    other, token = await create_account(database, username="other-app-owner", extra_roots=[tmp_path])
    directory = paths_for(other["id"]).applications / "unrelated"
    directory.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    await database.save_application(app_id="unrelated", name="Other App", app_dir=str(directory),
                                    created_at=now, updated_at=now, user_id=other["id"])
    agent = await database.get_system_agent(other["id"])
    root = tmp_path / "other-vault"
    root.mkdir()
    (root / "note.md").write_text("# Other\nIndependent current words.\n")
    configured = await client.put("/api/wiki/config", headers={"Authorization": "Bearer " + token}, json={
        "root": str(root), "curator_agent_id": agent["id"], "app_id": "unrelated"})
    assert configured.status_code == 200, configured.text
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    replace = os.replace
    destination = jobs.store.path("Library/note.md")

    def held_effect(source, target, *args, **kwargs):
        if Path(target) == destination:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "Original effect was not released"
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", held_effect)
    pending = asyncio.create_task(client.request(method, "/apps/parameters/wiki/document", headers=headers, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        accepted = await asyncio.wait_for(client.put("/apps/unrelated/wiki/document", headers={
            "Authorization": "Bearer " + app_scope_token("unrelated")}, json={
                "path": "note.md", "content": "# Other\nIndependent new words.\n",
                "expected_revision": revision((root / "note.md").read_bytes()), "reason": "Independent edit"}), 3)
        assert accepted.status_code == 200, accepted.text
        assert (root / "note.md").read_text() == "# Other\nIndependent new words.\n"
        assert not pending.done()
        release.set()
        assert (await pending).status_code == 200
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


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

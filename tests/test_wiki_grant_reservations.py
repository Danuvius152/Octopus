"""Unused creation paths can be released without losing uncertain outputs."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.wiki.jobs import WikiJobs
from tests import test_wiki_api as wiki_api
from tests.test_service_runtime import installation_settings
from tests.test_wiki_grant_v2 import assigned, proposed, publish

client = wiki_api.client


def reservations(jobs, job_id):
    with jobs.store.connect() as db:
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM wiki_path_reservations WHERE job_id=?", (job_id,))}


async def test_cancel_releases_unused_path_and_retry_reacquires_the_original_reservation(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    old, _ = await proposed(client, root, paths=["Library/new.md"], key="original")
    jobs = WikiJobs(None)
    identifier = old["items"][0]["reservation_id"]
    cancelled = await client.post(f"/api/wiki/jobs/{old['id']}/cancel", headers=wiki_api.HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    await jobs.settle_cancelled(old["id"], wiki_api.session_manager)
    assert reservations(jobs, old["id"])[identifier]["state"] == "released"
    other, _ = await proposed(client, root, paths=["Library/new.md"], key="another")
    conflict = await client.post(f"/api/wiki/jobs/{old['id']}/retry", headers=wiki_api.HEADERS)
    assert conflict.status_code == 409
    assert jobs.get(old["id"])["status"] == "cancelled"
    assert reservations(jobs, old["id"])[identifier]["state"] == "released"
    assert (await client.post(f"/api/wiki/jobs/{other['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
    await jobs.settle_cancelled(other["id"], wiki_api.session_manager)
    retried = await client.post(f"/api/wiki/jobs/{old['id']}/retry", headers=wiki_api.HEADERS)
    assert retried.status_code == 200, retried.text
    assert retried.json()["id"] == old["id"] and retried.json()["items"][0]["reservation_id"] == identifier
    assert retried.json()["grant"]["allowed_new_paths"] == ["Library/new.md"]
    assert reservations(jobs, old["id"])[identifier]["state"] == "reserved"
    assert not (root / "Library/new.md").exists()


async def test_cancel_does_not_release_a_live_assignment_or_allow_its_late_publish(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    job, config = await proposed(client, root, paths=["Library/new.md"])
    jobs = WikiJobs(None)
    item = job["items"][0]
    with assigned(job, config) as (headers, _):
        cancelled = await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)
        assert cancelled.status_code == 200
        assert reservations(jobs, job["id"])[item["reservation_id"]]["state"] == "reserved"
        late = await publish(client, headers, item["path"], item["reservation_id"])
        assert late.status_code == 403 and not (root / item["path"]).exists()
        retried = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
        assert retried.status_code == 409
    assert (await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
    await jobs.settle_cancelled(job["id"], None)
    assert reservations(jobs, job["id"])[item["reservation_id"]]["state"] == "released"
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == 0


async def _uncertain_reservation_process(config_file, evidence_file, phase, also_unstarted):
    """Run the real publication fault or its HTTP checks in a fresh process."""
    from httpx import ASGITransport, AsyncClient

    from server.config import settings
    from server.database import Database
    from server.deps import get_session_manager
    from server.main import app
    from server.session_manager import SessionManager
    from server.wiki.registry import registry

    config_file, evidence_file = Path(config_file), Path(evidence_file)
    for name, value in json.loads(config_file.read_text()).items():
        setattr(settings, name, value)
    os.environ["CODEX_HOME"] = str(config_file.parent / "default-codex")
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_file.parent / "default-claude")
    root = config_file.parent / "vault"
    root.mkdir(exist_ok=True)
    database = Database(settings.db_path)
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    wiki_api.session_manager = manager
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            if phase == "publish":
                await _prepare_uncertain_reservation(http, root, evidence_file, also_unstarted)
                raise AssertionError("An unconfirmed file effect returned without exit70")
            await _check_uncertain_reservation(http, root, evidence_file, also_unstarted)
    finally:
        registry.close()
        await database.close()
        app.dependency_overrides.pop(get_session_manager, None)


async def _prepare_uncertain_reservation(http, root, evidence_file, also_unstarted):
    from server.wiki import history as history_module

    paths = ["Library/uncertain.md", *(["Library/unstarted.md"] if also_unstarted else [])]
    job, config = await proposed(http, root, paths=paths)
    jobs = WikiJobs(None)
    item = job["items"][0]
    atomic = history_module.atomic

    def fail_before_replace(path, raw, **kwargs):
        if path == root / item["path"]:
            before = reservations(jobs, job["id"])[item["reservation_id"]]
            with jobs.store.connect() as db:
                receipts = [dict(row) for row in db.execute(
                    "SELECT * FROM operation_receipts WHERE job_id=?", (job["id"],)
                )]
            assert len(receipts) == 1 and receipts[0]["change_id"] == before["change_id"]
            proposal_digest = hashlib.sha256(raw).hexdigest()
            assert proposal_digest == before["output_revision"]
            blob = history_module.WikiHistory(jobs.store).blobs / proposal_digest
            assert hashlib.sha256(blob.read_bytes()).hexdigest() == proposal_digest
            evidence_file.write_text(json.dumps({
                "job": job, "before": before, "receipt": receipts[0], "proposal_digest": proposal_digest,
            }))
            raise OSError("injected failure after the durable prepare")
        return atomic(path, raw, **kwargs)

    with assigned(job, config) as (headers, _):
        history_module.atomic = fail_before_replace
        try:
            await publish(http, headers, item["path"], item["reservation_id"])
        finally:
            history_module.atomic = atomic


async def _check_uncertain_reservation(http, root, evidence_file, also_unstarted):
    evidence = json.loads(evidence_file.read_text())
    job, before = evidence["job"], evidence["before"]
    jobs = WikiJobs(None)
    item = job["items"][0]
    assert reservations(jobs, job["id"])[item["reservation_id"]] == before
    assert before["state"] in {"prepared", "blocked"} and before["change_id"]
    # Registry opening already completed this generation's recovery before
    # exposing jobs.store; it must never apply the retained proposal.
    with jobs.store.connect() as db:
        original_status = db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0]
    # Reopening classifies the missing file against the retained preimage;
    # cancellation must preserve that classification and the original proposal.
    assert original_status == "interrupted"
    assert (await http.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
    await jobs.settle_cancelled(job["id"], wiki_api.session_manager)
    assert reservations(jobs, job["id"])[item["reservation_id"]] == before
    with jobs.store.connect() as db:
        assert db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0] == original_status
    if also_unstarted:
        assert reservations(jobs, job["id"])[job["items"][1]["reservation_id"]]["state"] == "released"
    conflict = await http.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": "must-not-steal", "creation": {"purpose": "requested_knowledge_page", "paths": [item["path"]]}})
    assert conflict.status_code == 409
    retried = await http.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
    assert retried.status_code == 409, retried.text
    retained = jobs.get(job["id"])
    assert retained["status"] == "cancelled" and retained["settlement_phase"] == "attention"
    assert retained["items"][0]["status"] == "attention"
    after = reservations(jobs, job["id"])[item["reservation_id"]]
    assert (after["document_id"], after["change_id"]) == (before["document_id"], before["change_id"])
    assert not (root / item["path"]).exists()


@pytest.mark.parametrize("also_unstarted", [False, True])
def test_cancel_keeps_an_uncertain_prepared_path_and_retry_never_recreates_it(tmp_path, also_unstarted):
    config, config_file = installation_settings(tmp_path / "installation")
    evidence_file = tmp_path / "original-preparation.json"
    program = """
import asyncio,sys
from tests.test_wiki_grant_reservations import _uncertain_reservation_process
asyncio.run(_uncertain_reservation_process(sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]=='True'))
print('operation returned',flush=True)
"""
    for phase in ("publish", "verify", "verify"):
        result = subprocess.run(
            [sys.executable, "-c", program, str(config_file), str(evidence_file), phase, str(also_unstarted)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == (70 if phase == "publish" else 0), result.stdout + result.stderr
        assert ("operation returned" in result.stdout) == (phase != "publish")
        assert evidence_file.is_file(), result.stderr
        evidence = json.loads(evidence_file.read_text())
        before = evidence["before"]
        with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
            reader.row_factory = sqlite3.Row
            change = reader.execute(
                "SELECT status,before_hash,after_hash FROM changes WHERE id=?", (before["change_id"],)
            ).fetchone()
            assert tuple(change) == (
                "prepared" if phase == "publish" else "interrupted", None, before["output_revision"],
            )
            assert [dict(row) for row in reader.execute(
                "SELECT * FROM operation_receipts WHERE job_id=?", (evidence["job"]["id"],)
            )] == [evidence["receipt"]]
        blob = Path(config.wiki_dir) / "blobs" / evidence["proposal_digest"]
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == evidence["proposal_digest"]


async def test_partial_completion_releases_only_unused_targets_and_keeps_bound_identity(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    job, config = await proposed(client, root, paths=["Library/created.md", "Library/unstarted.md"])
    jobs = WikiJobs(None)
    first, second = job["items"]
    with assigned(job, config) as (headers, _):
        created = await publish(client, headers, first["path"], first["reservation_id"])
        assert created.status_code == 200, created.text
    assert jobs.recover_creation(job["id"], first) and first["status"] == "done"
    second.update(status="attention", error="The requested second output was not started")
    jobs.update(job["id"], status="completed_with_attention", items=[first, second], session_id=None)
    assert jobs.release_unused(job["id"]) == 1
    state = reservations(jobs, job["id"])
    assert state[first["reservation_id"]]["state"] == "bound"
    assert state[second["reservation_id"]]["state"] == "released"
    retried = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
    assert retried.status_code == 200, retried.text
    assert retried.json()["items"][0] == first
    assert retried.json()["grant"]["allowed_new_paths"] == [second["path"]]
    assert jobs.store.read(first["path"])["id"] == first["doc"]


async def test_cancel_before_assignment_keeps_the_reservation_until_worker_retirement(client, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from server.wiki.jobs import WikiWorker

    root = tmp_path / "vault"
    root.mkdir()
    job, _ = await proposed(client, root, paths=["Library/new.md"])
    jobs = WikiJobs(None)
    assert jobs.next_queued() == job["id"]
    ready, finish = asyncio.Event(), asyncio.Event()
    archived = []

    async def create_session(*args, **kwargs):
        ready.set()
        await finish.wait()
        return SimpleNamespace(id="not-yet-assigned", user_id=None)

    async def archive(session_id, **kwargs):
        archived.append(session_id)

    worker = WikiWorker()
    worker.manager = SimpleNamespace(db=wiki_api.session_manager.db, create_session=create_session, archive_session=archive)
    execution = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        cancelled = await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)
        assert cancelled.status_code == 200
        assert reservations(jobs, job["id"])[job["items"][0]["reservation_id"]]["state"] == "reserved"
        denied = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
        assert denied.status_code == 409
    finally:
        finish.set()
        await asyncio.wait_for(execution, 3)
    assert archived == ["not-yet-assigned"]
    assert reservations(jobs, job["id"])[job["items"][0]["reservation_id"]]["state"] == "released"


async def test_new_registration_readmission_reacquires_the_same_released_path(client, tmp_path):
    from server.wiki.registry import registry

    root = tmp_path / "vault"
    root.mkdir()
    job, _ = await proposed(client, root, paths=["Library/new.md"])
    jobs = WikiJobs(None)
    assert (await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
    await jobs.settle_cancelled(job["id"], wiki_api.session_manager)
    old = reservations(jobs, job["id"])
    assert next(iter(old.values()))["state"] == "released"
    registry.configure(None, root, "new-curator")
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS, json={
        "creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["grant"]["allowed_new_paths"] == ["Library/new.md"]
    assert accepted.json()["items"][0]["reservation_id"] == next(iter(old))
    assert accepted.json()["items"][0]["status"] == "pending"

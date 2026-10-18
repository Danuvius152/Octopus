"""Unused creation paths can be released without losing uncertain outputs."""

import pytest

from server.wiki.jobs import WikiJobs
from tests import test_wiki_api as wiki_api
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
    assert reservations(jobs, old["id"])[identifier]["state"] == "released"
    other, _ = await proposed(client, root, paths=["Library/new.md"], key="another")
    conflict = await client.post(f"/api/wiki/jobs/{old['id']}/retry", headers=wiki_api.HEADERS)
    assert conflict.status_code == 409
    assert jobs.get(old["id"])["status"] == "cancelled"
    assert reservations(jobs, old["id"])[identifier]["state"] == "released"
    assert (await client.post(f"/api/wiki/jobs/{other['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
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
    assert reservations(jobs, job["id"])[item["reservation_id"]]["state"] == "released"
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("also_unstarted", [False, True])
async def test_cancel_keeps_an_uncertain_prepared_path_and_retry_never_recreates_it(client, tmp_path, monkeypatch, also_unstarted):
    from server.wiki import history as history_module

    root = tmp_path / "vault"
    root.mkdir()
    paths = ["Library/uncertain.md", *(["Library/unstarted.md"] if also_unstarted else [])]
    job, config = await proposed(client, root, paths=paths)
    jobs = WikiJobs(None)
    item = job["items"][0]

    atomic = history_module.atomic

    def fail_before_replace(path, raw, **kwargs):
        if path == root / item["path"]:
            raise OSError("injected failure after the durable prepare")
        return atomic(path, raw, **kwargs)

    with assigned(job, config) as (headers, _):
        with monkeypatch.context() as patch:
            patch.setattr(history_module, "atomic", fail_before_replace)
            with pytest.raises(OSError, match="durable prepare"):
                await publish(client, headers, item["path"], item["reservation_id"])
    before = reservations(jobs, job["id"])[item["reservation_id"]]
    assert before["state"] in {"prepared", "blocked"} and before["change_id"]
    with jobs.store.connect() as db:
        original_status = db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0]
    assert original_status == "prepared"
    assert (await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=wiki_api.HEADERS)).status_code == 200
    assert reservations(jobs, job["id"])[item["reservation_id"]] == before
    with jobs.store.connect() as db:
        assert db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0] == original_status
    if also_unstarted:
        assert reservations(jobs, job["id"])[job["items"][1]["reservation_id"]]["state"] == "released"
    conflict = await client.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": "must-not-steal", "creation": {"purpose": "requested_knowledge_page", "paths": [item["path"]]}})
    assert conflict.status_code == 409
    retried = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
    assert retried.status_code == 409, retried.text
    retained = jobs.get(job["id"])
    assert retained["status"] == "cancelled" and retained["settlement_phase"] == "unconfirmed"
    assert retained["items"][0]["status"] == "attention"
    after = reservations(jobs, job["id"])[item["reservation_id"]]
    assert (after["document_id"], after["change_id"]) == (before["document_id"], before["change_id"])
    assert not (root / item["path"]).exists()
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == 1


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
    old = reservations(jobs, job["id"])
    assert next(iter(old.values()))["state"] == "released"
    registry.configure(None, root, "new-curator")
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS, json={
        "creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["grant"]["allowed_new_paths"] == ["Library/new.md"]
    assert accepted.json()["items"][0]["reservation_id"] == next(iter(old))
    assert accepted.json()["items"][0]["status"] == "pending"

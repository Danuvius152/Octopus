"""Public Wiki operations retain their binding and recheck live callers."""

import asyncio

import pytest

from server.wiki.jobs import worker
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import entry, prepare


@pytest.mark.parametrize("operation", ["maintenance-status", "maintenance-selections"])
async def test_observation_rechecks_turn_after_store_preparation(client, tmp_path, monkeypatch, operation):
    await prepare(client, tmp_path)
    original = registry.store
    async with entry(client, mode="read") as (headers, task):
        def prepare_then_revoke(owner):
            store = original(owner)
            for token, active in tuple(tasks.active.items()):
                if active is task:
                    tasks.close(token)
            return store

        monkeypatch.setattr(registry, "store", prepare_then_revoke)
        response = (await client.get("/api/wiki/" + operation, headers=headers)
                    if operation == "maintenance-status" else
                    await client.post("/api/wiki/" + operation, headers=headers, json={}))
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("batch", [False, True])
async def test_lost_wake_recovers_original_checked_job_on_replay(client, tmp_path, monkeypatch, batch):
    from tests.test_wiki_maintenance_selections import select, submission

    _, _, jobs = await prepare(client, tmp_path)
    selection = await select(client)
    body = submission(selection, selection["items"][0])
    url = "/api/wiki/maintain"
    if batch:
        url += "-batch"
        body = {"selection_id": selection["id"], "ordinals": [selection["items"][0]["ordinal"]]}

    def failed_wake(owner):
        raise RuntimeError("wake unavailable after commit")

    monkeypatch.setattr(worker, "wake", failed_wake)
    with pytest.raises(RuntimeError, match="wake unavailable"):
        await client.post(url, headers=client.headers, json=body)
    retained = jobs.list()["items"]
    assert len(retained) == 1
    wakes = []
    monkeypatch.setattr(worker, "wake", wakes.append)
    replay = await client.post(url, headers=client.headers, json=body)
    assert replay.status_code == 200, replay.text
    result = replay.json()["jobs"][0] if batch else replay.json()
    assert result["id"] == retained[0]["id"]
    assert wakes == [jobs.user_id]
    assert len(jobs.list()["items"]) == 1
    jobs.update(result["id"], status="running")
    wakes.clear()
    running = await client.post(url, headers=client.headers, json=body)
    assert running.status_code == 200, running.text
    assert not wakes


@pytest.mark.parametrize("action", ["cancel", "retry", "verify"])
async def test_control_rechecks_original_turn_after_store_preparation(client, tmp_path, monkeypatch, action):
    _, _, jobs = await prepare(client, tmp_path)
    job = jobs.maintain("original", "", "Review", ["Library/journal.md"], None, [], "human")
    original = registry.store
    async with entry(client, mode="auto") as (headers, task):
        def prepare_then_revoke(owner):
            store = original(owner)
            for token, active in tuple(tasks.active.items()):
                if active is task:
                    tasks.close(token)
            return store

        monkeypatch.setattr(registry, "store", prepare_then_revoke)
        response = await client.post(f"/api/wiki/jobs/{job['id']}/{action}", headers=headers, json={})
        assert response.status_code == 403, response.text
        assert jobs.get(job["id"])["status"] == "queued"


async def test_publication_cannot_switch_generation_after_capturing_store(client, tmp_path, monkeypatch):
    root, _, jobs = await prepare(client, tmp_path)
    original = registry.store
    rebound = False

    def capture_then_rebind(owner):
        nonlocal rebound
        store = original(owner)
        if not rebound:
            rebound = True
            registry.configure(owner, root, "replacement-curator")
        return store

    monkeypatch.setattr(registry, "store", capture_then_rebind)
    response = await client.post("/api/wiki/publish", headers=client.headers, json={
        "path": "Library/new.md", "content": "# New\nA scoped note.\n", "kind": "concept",
        "expected_revision": None, "reason": "Create within the captured binding",
    })
    assert response.status_code in {409, 503}, response.text
    assert not (root / "Library/new.md").exists()
    assert registry.config(jobs.user_id)["curator_agent_id"] == "replacement-curator"


async def test_repeated_cancel_operation_cannot_cancel_a_readmitted_attempt(client, tmp_path, monkeypatch):
    from server.wiki.jobs import WikiJobs

    _, config, jobs = await prepare(client, tmp_path)
    job = jobs.maintain("original", "", "Review", ["Library/journal.md"], None, [], "human")
    body = {"operation_id": "cancel-once", "expected_vault_id": config["vault_id"]}
    url = f"/api/wiki/jobs/{job['id']}"
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finish = WikiJobs._finish_cancelled

    async def finish_after_release(self, *args, **kwargs):
        entered.set()
        try:
            await release.wait()
            return await finish(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(WikiJobs, "_finish_cancelled", finish_after_release)
    try:
        cancelled = await client.post(url + "/cancel", headers=client.headers, json=body)
        assert cancelled.status_code == 200, cancelled.text
        await asyncio.wait_for(entered.wait(), 5)
        assert cancelled.json()["settlement_phase"] == "pending"
        blocked = await client.post(url + "/retry", headers=client.headers, json={})
        assert blocked.status_code == 409
        assert "Confirm the original cancellation" in blocked.json()["detail"]
        release.set()
        verified = await client.post(url + "/verify", headers=client.headers, json={
            "expected_attempt": job["attempt"], "expected_vault_id": config["vault_id"],
        })
        assert verified.status_code == 200, verified.text
        assert verified.json()["settlement_phase"] == "confirmed"
        retry = await client.post(url + "/retry", headers=client.headers, json={})
        assert retry.status_code == 200, retry.text
        repeated = await client.post(url + "/cancel", headers=client.headers, json=body)
        assert repeated.status_code == 200, repeated.text
        assert jobs.get(job["id"])["status"] == "queued"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)


async def test_selection_rechecks_authority_after_observation_preparation(client, tmp_path, monkeypatch):
    from server.wiki import maintenance_selections

    _, _, jobs = await prepare(client, tmp_path)
    original = maintenance_selections.settle
    async with entry(client, mode="read") as (headers, task):
        def settle_then_revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            for token, active in tuple(tasks.active.items()):
                if active is task:
                    tasks.close(token)
            return result

        monkeypatch.setattr(maintenance_selections, "settle", settle_then_revoke)
        response = await client.post("/api/wiki/maintenance-selections", headers=headers, json={})
        assert response.status_code == 403, response.text
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*) FROM maintenance_selections").fetchone()[0] == 0

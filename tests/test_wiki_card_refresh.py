"""Refresh one original card across pagination and historical vaults."""

import json

import pytest

from server.wiki import job_notifications
from tests import wiki_api as api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_job_notifications import pending, subscribe
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs


@pytest.mark.parametrize("action", ["retry", "readmit"])
async def test_old_chat_reads_current_attempt_after_human_retry_and_all_notifications_are_acked(jobs, client, tmp_path, action):
    from server.agent_manager import AgentManager
    from server.wiki.jobs import WikiJobs

    manager = api.session_manager
    entry = await manager.create_session(jobs.registration["curator_agent_id"], "Original", str(tmp_path))
    job = enqueue(jobs)
    subscribe(jobs, job, entry.id)
    jobs.update(job["id"], status="running")
    previous = jobs.update(job["id"], status="completed_with_attention")
    await job_notifications.dispatch(jobs, manager)
    path = f"/api/sessions/{entry.id}/wiki-jobs"
    before = (await client.get(path)).json()[0]
    assert before["state_version"] == previous["state_version"]
    if action == "readmit":
        agent = await AgentManager(manager.db).create_agent(name="Replacement curator", user_id=jobs.user_id)
        configured = await client.put("/api/wiki/config", json={"root": str(jobs.store.root), "curator_agent_id": agent["id"]})
        assert configured.status_code == 200, configured.text
        jobs = WikiJobs(jobs.user_id)
    options = {"paths": [job["items"][0]["path"]]} if action == "readmit" else {}
    response = await client.post(f"/api/wiki/jobs/{job['id']}/{action}", json={
        "operation_id": f"human-{action}", "expected_attempt": previous["attempt"],
        "expected_vault_id": jobs.registration["vault_id"],
        **options,
    })
    assert response.status_code == 200, response.text
    current = jobs.update(job["id"], status="running")
    await job_notifications.dispatch(jobs, manager)
    assert pending(jobs) == []
    # This chat lost automatic result ownership; its committed receipt did not
    # advance, but that original observation relationship still discovers the job.
    assert (await manager.db.wiki_job_cards(entry.id, jobs.user_id))[0]["state_version"] == before["state_version"]
    cards = await client.get(path)
    assert cards.status_code == 200
    card = cards.json()[0]
    assert (card["attempt"], card["state_version"], card["status"]) == (
        current["attempt"], current["state_version"], "running",
    )
    assert card["origin_request_id"] == before["origin_request_id"]
    assert card["current_state"] == "available"
    detail = await client.get(f"/api/wiki/jobs/{job['id']}/details", params={"attempt": card["attempt"]})
    assert detail.status_code == 200 and detail.json()["current_attempt"] == current["attempt"]
    stale = await client.post(f"/api/wiki/jobs/{job['id']}/retry", json={
        "expected_attempt": before["attempt"], "expected_vault_id": card["vault_id"],
    })
    assert stale.status_code == 409
    assert pending(jobs) == [], "A read must not create new subscriptions or notifications"


@pytest.mark.parametrize("unavailable", ["job", "vault", "access"])
async def test_unreadable_current_job_keeps_only_its_original_summary(jobs, client, tmp_path, unavailable):
    from server.mcp_identity import mint
    from server.wiki.tasks import tasks

    manager = api.session_manager
    entry = await manager.create_session(jobs.registration["curator_agent_id"], "Original", str(tmp_path))
    job = enqueue(jobs)
    subscribe(jobs, job, entry.id)
    original = pending(jobs)[0]
    vault = jobs.registration["vault_id"]
    if unavailable == "job":
        body = json.loads(original["body"]) | {"job_id": "retained-missing-job"}
        original = original | {"job_id": body["job_id"], "body": json.dumps(body)}
    elif unavailable == "vault":
        vault = "retained-old-vault"
    await manager.db.receive_wiki_job_notice(vault, jobs.user_id, original)
    jobs.update(job["id"], status="running", error="current private detail")
    headers = client.headers
    if unavailable == "access":
        token = tasks.open(entry, "read", request_id="original-read")
        headers = {"Authorization": "Bearer " + mint(entry.id, user_id=entry.user_id, wiki_task_id=token)}
        tasks.close(token)
    response = await client.get(f"/api/sessions/{entry.id}/wiki-jobs", headers=headers)
    assert response.status_code == 200, response.text
    card = response.json()[0]
    assert card["current_state"] == "unavailable"
    assert card["state_version"] == json.loads(original["body"])["state_version"]
    assert card["error"] is None and "current private detail" not in response.text
    assert card["scope"] == json.loads(original["body"])["scope"]
    # A session without the durable relationship cannot use an exact query to
    # discover whether the current job exists.
    stranger = await manager.create_session(jobs.registration["curator_agent_id"], "Unrelated", str(tmp_path))
    unrelated = await client.get(f"/api/sessions/{stranger.id}/wiki-jobs", params={
        "job_id": job["id"], "vault_id": jobs.registration["vault_id"],
    })
    assert unrelated.status_code == 200 and unrelated.json() == []


async def test_exact_card_query_reaches_old_vault_and_job_beyond_first_page(jobs, client, tmp_path):
    manager = api.session_manager
    entry = await manager.create_session(jobs.registration["curator_agent_id"], "Original", str(tmp_path))
    job = enqueue(jobs)
    subscribe(jobs, job, entry.id)
    original = pending(jobs)[0]
    for number in range(32):
        identifier = f"job-{number}"
        body = json.loads(original["body"]) | {"job_id": identifier}
        delivery = original | {"event_id": f"event-{number}", "job_id": identifier, "body": json.dumps(body)}
        await manager.db.receive_wiki_job_notice("old-vault" if number == 0 else "current-vault", jobs.user_id, delivery)
    path = f"/api/sessions/{entry.id}/wiki-jobs"
    page = await client.get(path, headers=client.headers)
    assert page.status_code == 200 and len(page.json()) == 30
    assert all(card["job_id"] != "job-0" for card in page.json())
    exact = await client.get(path, headers=client.headers, params={"job_id": "job-0", "vault_id": "old-vault"})
    assert exact.status_code == 200, exact.text
    assert [(card["vault_id"], card["job_id"]) for card in exact.json()] == [("old-vault", "job-0")]
    wrong = await client.get(path, headers=client.headers, params={"job_id": "job-0", "vault_id": "current-vault"})
    assert wrong.status_code == 200 and wrong.json() == []
    for partial in ({"job_id": "job-0"}, {"vault_id": "old-vault"}):
        assert (await client.get(path, headers=client.headers, params=partial)).status_code == 422

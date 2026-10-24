"""A stale attempt conflicts; only an absent task is missing."""

from tests.test_wiki_job_events import enqueue
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


async def test_queued_card_attempt_conflicts_after_the_original_task_starts(jobs, client):
    queued = enqueue(jobs)
    assert queued["attempt"] == 0
    assert jobs.next_queued() == queued["id"]
    running = jobs.get(queued["id"])
    assert running["status"] == "running" and running["attempt"] == 1
    endpoint = f"/api/wiki/jobs/{queued['id']}/details"
    stale = await client.get(endpoint, headers=client.headers, params={"attempt": queued["attempt"]})
    assert stale.status_code == 409, stale.text
    current = await client.get(endpoint, headers=client.headers, params={"attempt": running["attempt"]})
    assert current.status_code == 200, current.text
    assert current.json()["current_attempt"] == 1 and current.json()["status"] == "running"
    assert jobs.get(queued["id"])["state_version"] == running["state_version"]


async def test_absent_task_detail_remains_not_found(jobs, client):
    assert jobs.list()["items"] == []
    response = await client.get("/api/wiki/jobs/not-an-existing-task/details", headers=client.headers, params={"attempt": 0})
    assert response.status_code == 404, response.text
    assert jobs.list()["items"] == []

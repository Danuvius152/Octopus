"""Control the displayed attempt without turning a subscription into authority."""

from server.mcp_identity import mint, reset_current_scope, set_current_scope, verify
from server.mcp_servers.wiki import mcp
from server.wiki import job_notifications
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


async def test_stale_attempt_or_vault_does_not_cancel_current_work(jobs, client):
    job = enqueue(jobs)
    running = jobs.update(job["id"], status="running")
    for control in ({"expected_attempt": job["attempt"]}, {"expected_vault_id": "different-vault"}):
        response = await client.post(f"/api/wiki/jobs/{job['id']}/cancel", json=control, headers=wiki_api.HEADERS)
        assert response.status_code == 409, response.text
        current = jobs.get(job["id"])
        assert current["status"] == "running" and current["state_version"] == running["state_version"]


async def test_completed_retry_is_idempotent_and_checks_displayed_vault(jobs, client):
    job = enqueue(jobs)
    done = jobs.update(job["id"], status="completed")
    body = {"expected_attempt": done["attempt"], "expected_vault_id": jobs.registration["vault_id"]}
    response = await client.post(f"/api/wiki/jobs/{job['id']}/retry", json=body, headers=wiki_api.HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["state_version"] == done["state_version"]


async def test_read_only_turn_cannot_control_or_discover_control_tools(jobs, client, tmp_path):
    job = enqueue(jobs)
    session = await wiki_api.session_manager.create_session(jobs.registration["curator_agent_id"], "Read only", str(tmp_path))
    token = tasks.open(session, "read")
    bearer = mint(session.id, wiki_task_id=token)
    scope_token = set_current_scope(verify(bearer))
    try:
        names = {tool.name for tool in await mcp.list_tools()}
        assert not names & {"cancel_job", "retry_job"}
        for action in ("cancel", "retry"):
            response = await client.post(f"/api/wiki/jobs/{job['id']}/{action}", json={}, headers={"Authorization": "Bearer " + bearer})
            assert response.status_code == 403, response.text
        assert jobs.get(job["id"])["status"] == "queued"
    finally:
        reset_current_scope(scope_token)
        tasks.close(token)


async def test_worker_controls_only_own_job_and_gets_bounded_result(jobs, client, tmp_path):
    job = enqueue(jobs)
    other = jobs.maintain("another", "", "Other review", ["Library/note.md"], None, [], "human")
    session = await wiki_api.session_manager.create_session(jobs.registration["curator_agent_id"], "Worker", str(tmp_path))
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"], registration=jobs.registration)
    token = tasks.open(session, "maintain")
    bearer = mint(session.id, wiki_task_id=token)
    try:
        denied = await client.post(f"/api/wiki/jobs/{other['id']}/cancel", json={}, headers={"Authorization": "Bearer " + bearer})
        assert denied.status_code == 403, denied.text
        assert jobs.get(other["id"])["status"] == "queued"
        own = await client.post(f"/api/wiki/jobs/{job['id']}/cancel", json={}, headers={"Authorization": "Bearer " + bearer})
        assert own.status_code == 200, own.text
        assert set(own.json()) == {"id", "status", "state_version", "attempt", "settlement_phase", "progress"}
        assert own.json()["settlement_phase"] == "pending"
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


async def test_settlement_paging_does_not_starve_newer_unconfirmed_jobs(jobs):
    first = enqueue(jobs)
    identifiers = [first["id"]]
    for index in range(10):
        identifiers.append(jobs.maintain(f"pending-{index}", "", "Review", ["Library/note.md"], None, [], "human")["id"])
    for job_id in identifiers:
        jobs.update(job_id, status="running")
        jobs.mark_cancelled(job_id)
    one = job_notifications._unsettled(jobs, "")
    two = job_notifications._unsettled(jobs, one[-1])
    assert len(one) == 10 and len(two) == 1
    assert set(one + two) == set(identifiers)
    assert job_notifications._unsettled(jobs, two[-1]) == []

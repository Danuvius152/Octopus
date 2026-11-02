"""An explicit retry returns to its dispatcher without replaying an old request."""

import asyncio
import json

import pytest

from server.mcp_identity import mint
from server.wiki import job_events, job_notifications, job_returns
from server.wiki.tasks import tasks
from tests.test_wiki_completion_return import complete, finish_original, submitted
from tests.test_wiki_completion_return import returning as returning
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_return_boundaries import live_task, return_inputs, wait_return
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs


@pytest.mark.parametrize("prior_status", ["completed", "completed_with_attention", "interrupted"])
@pytest.mark.parametrize("archive_old", [False, True])
async def test_retry_in_another_chat_returns_only_to_the_current_dispatcher(jobs, returning, client, prior_status, archive_old):
    job = submitted(jobs, returning, "retry-selected-note")
    old_session = returning.original
    await finish_original(returning)
    jobs.update(job["id"], status="running")
    previous = jobs.update(job["id"], status=prior_status,
                           items=[item | {"status": "done" if prior_status == "completed" else "attention"} for item in job["items"]])
    await job_notifications.dispatch(jobs, returning.manager)
    old_returns = await return_inputs(returning.manager, old_session.id)
    if old_returns:
        await wait_return(returning.manager, old_session.id, old_returns[-1][1])
    if archive_old:
        await returning.manager.archive_session(old_session.id, replace=False)

    returning.gate.unlink()
    current = await returning.manager.create_session(
        jobs.registration["curator_agent_id"], "Retry from this chat", old_session.working_dir,
    )
    returning.original = current
    returning.receipt = await returning.manager.start_message(current.id, "Resume the retained Wiki job")
    _, scope = await live_task(returning)
    bearer = mint(current.id, user_id=jobs.user_id,
                  wiki_task_id=scope.wiki_task_id)
    response = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers={"Authorization": "Bearer " + bearer}, json={
        "expected_attempt": previous["attempt"], "expected_vault_id": jobs.registration["vault_id"],
    })
    assert response.status_code == 200, response.text
    if prior_status == "completed":
        assert response.json()["status"] == "completed"
        assert response.json()["state_version"] == previous["state_version"]
        assert generation(jobs, job["id"]) == 0
    else:
        retried = jobs.update(job["id"], status="running")
        assert retried["attempt"] == previous["attempt"] + 1
        complete(jobs, retried)
    await finish_original(returning)
    if prior_status == "interrupted" and archive_old:
        from server.sessions import SessionManager

        await returning.manager.close_session_owners()
        cold = SessionManager()
        await cold.initialize(returning.manager.db)
        returning.manager = cold
    await job_notifications.dispatch(jobs, returning.manager)
    replies = await return_inputs(returning.manager, current.id)
    assert len(replies) == 1, "The current retry request must receive its result"
    await wait_return(returning.manager, current.id, replies[0][1])
    assert len(await return_inputs(returning.manager, old_session.id)) == len(old_returns), (
        "A new dispatch must not restart result reporting for the prior request"
    )
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(await return_inputs(returning.manager, current.id)) == 1


def generation(jobs, job_id):
    with jobs.store.connect() as db:
        return db.execute("SELECT dispatch_generation FROM wiki_job_state WHERE job_id=?", (job_id,)).fetchone()[0]


async def test_stale_retry_has_no_subscription_or_dispatch_side_effect(jobs, returning, client):
    job = submitted(jobs, returning, "stale-retry")
    jobs.update(job["id"], status="running")
    previous = jobs.update(job["id"], status="completed_with_attention")
    with jobs.store.connect() as db:
        before = db.execute("SELECT count(*) FROM wiki_job_subscribers").fetchone()[0]
    for fields in [{"expected_attempt": previous["attempt"] - 1}, {"expected_vault_id": "other-vault"}]:
        response = await client.post(f"/api/wiki/jobs/{job['id']}/retry", json=fields)
        assert response.status_code == 409, response.text
    assert generation(jobs, job["id"]) == 0
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM wiki_job_subscribers").fetchone()[0] == before


async def test_old_control_replay_does_not_subscribe_to_a_later_dispatch(jobs):
    job = enqueue(jobs)
    jobs.update(job["id"], status="running")
    previous = jobs.update(job["id"], status="completed_with_attention")
    control = {"operation_id": "original-retry", "expected_attempt": previous["attempt"]}
    first = job_events.Requester(jobs.user_id, "chat", "first", "original")
    jobs.retry(job["id"], requester=first, **control)
    jobs.update(job["id"], status="running")
    jobs.update(job["id"], status="completed_with_attention")
    jobs.retry(job["id"], requester=job_events.Requester(jobs.user_id, "chat", "new", "later"))
    before = generation(jobs, job["id"])
    jobs.retry(job["id"], requester=job_events.Requester(jobs.user_id, "chat", "replay", "old-control"), **control)
    assert generation(jobs, job["id"]) == before == 2
    with jobs.store.connect() as db:
        assert {row[0] for row in db.execute("SELECT target FROM wiki_job_subscribers")} == {"first", "new"}


async def test_lost_group_ack_and_member_transfer_never_create_a_second_report(jobs, returning, client, monkeypatch):
    from tests.test_wiki_maintenance_state import settle

    first, second = submitted(jobs, returning, "group-a"), submitted(jobs, returning, "group-b")
    for job in [first, second]:
        await settle(client, jobs, jobs.registration, path=job["items"][0]["path"], job=job)
    await finish_original(returning)
    ack = job_notifications._acknowledge

    def lost_ack(*args):
        raise OSError("lost group ACK")

    monkeypatch.setattr(job_notifications, "_acknowledge", lost_ack)
    with pytest.raises(OSError, match="lost group ACK"):
        await job_notifications.dispatch(jobs, returning.manager)
    reports = await return_inputs(returning.manager, returning.original.id)
    assert len(reports) == 1
    await wait_return(returning.manager, returning.original.id, reports[0][1])
    jobs.update(first["id"], status="completed_with_attention",
                items=[item | {"status": "attention"} for item in first["items"]])
    response = await client.post(f"/api/wiki/jobs/{first['id']}/retry", json={})
    assert response.status_code == 200, response.text
    with jobs.store.connect() as db:
        delivery = next(row for row in job_events.pending(db) if row["job_id"] == second["id"] and json.loads(row["body"])["status"] == "completed")
    group = job_returns._group(jobs, delivery)
    assert group["job_count"] == 1 and group["jobs"][0]["job_id"] == second["id"]
    assert group["operations"] == {"no_change": 1}
    assert group["delivery_id"] == reports[0][1]["delivery_id"]
    monkeypatch.setattr(job_notifications, "_acknowledge", ack)
    returning.manager._wiki_notice_cursors.clear()
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(await return_inputs(returning.manager, returning.original.id)) == 1


async def test_return_acceptance_and_retry_have_one_admission_order(jobs, returning, client, monkeypatch):
    job = submitted(jobs, returning, "admission-order")
    await finish_original(returning)
    jobs.update(job["id"], status="running")
    jobs.update(job["id"], status="completed_with_attention")
    entered, release = asyncio.Event(), asyncio.Event()
    context = job_returns.execution_context

    async def paused_context(*args):
        entered.set()
        await release.wait()
        return await context(*args)

    monkeypatch.setattr(job_returns, "execution_context", paused_context)
    reporting = asyncio.create_task(job_notifications.dispatch(jobs, returning.manager))
    retrying = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        gate = job_returns.dispatch_gate(returning.manager, jobs.user_id)
        acquired = asyncio.Event()
        acquire = gate.acquire

        async def observed_acquire():
            acquired.set()
            return await acquire()

        monkeypatch.setattr(gate, "acquire", observed_acquire)
        retrying = asyncio.create_task(client.post(f"/api/wiki/jobs/{job['id']}/retry", json={}))
        await asyncio.wait_for(acquired.wait(), 3)
        assert not retrying.done() and generation(jobs, job["id"]) == 0
        release.set()
        await asyncio.wait_for(reporting, 3)
        assert (await asyncio.wait_for(retrying, 3)).status_code == 200
        assert generation(jobs, job["id"]) == 1
        reports = await return_inputs(returning.manager, returning.original.id)
        assert len(reports) == 1
        await wait_return(returning.manager, returning.original.id, reports[0][1])
    finally:
        release.set()
        await asyncio.gather(reporting, *([retrying] if retrying else []), return_exceptions=True)


async def test_caller_ending_while_retry_waits_cannot_transfer_the_dispatch(jobs, returning, client, monkeypatch):
    job = submitted(jobs, returning, "ended-retry")
    jobs.update(job["id"], status="running")
    jobs.update(job["id"], status="completed_with_attention")
    _, scope = await live_task(returning)
    token = scope.wiki_task_id
    bearer = mint(returning.original.id, user_id=jobs.user_id, wiki_task_id=token)
    entered = asyncio.Event()
    gate = job_returns.dispatch_gate(returning.manager, jobs.user_id)
    await gate.acquire()
    acquire = gate.acquire

    async def observed_acquire():
        entered.set()
        return await acquire()

    monkeypatch.setattr(gate, "acquire", observed_acquire)
    request = asyncio.create_task(client.post(f"/api/wiki/jobs/{job['id']}/retry", json={}, headers={"Authorization": "Bearer " + bearer}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        tasks.close(token)
    finally:
        gate.release()
    assert (await request).status_code == 403
    assert generation(jobs, job["id"]) == 0

"""A Wiki return inherits the exact execution's confirmed inline contract."""

import asyncio
import json

import pytest

from server.knowledge import encode
from server.session_manager import SessionManager
from server.sessions.client_submissions import execution_context
from server.wiki import job_events, job_notifications
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.maintenance_contract import actor_for
from server.wiki.tasks import tasks
from tests.test_knowledge_steering import steering as steering


async def create_steered_job(env, *, unconfirmed=False):
    original = await env.manager.start_message(env.session.id, "Initial comparison")
    assert await asyncio.wait_for(env.started.get(), 3) == "Initial comparison"
    env.control.frame_error = unconfirmed
    correction = "/wiki 比较本地记录及相关网页；保留这里追加的来源要求。"
    steer = await env.manager.start_message(env.session.id, correction, steerable=True)
    assert steer.disposition == "steered"
    await asyncio.wait_for(env.frames.get(), 3)
    # The actual model frame observes the installed contract and submits under
    # the still-live original request's Wiki identity.
    task = tasks.require(env.control.scope)
    jobs = WikiJobs(env.session.user_id)
    WikiHistory(jobs.store).write("Library/steered.md", b"# Note\nKeep this condition.\n", None, "human", "Create input")
    job = jobs.maintain(
        "steered-result", "", "Review the selected note", ["Library/steered.md"], None, [], actor_for(task),
        selection_task=task, mcp_scope=env.control.scope,
    )
    assert job["id"]
    if unconfirmed:
        completion = await asyncio.wait_for(steer.wait(), 3)
        assert completion.reason == "steer_delivery_unknown" and completion.outcome is None
    env.first_done.set()
    completed = await asyncio.wait_for(original.wait(), 3)
    assert completed.outcome.status == "success" and completed.recording_state == "committed"
    jobs.update(job["id"], status="completed", items=[item | {"status": "done"} for item in job["items"]])
    return jobs, job, original, steer, correction


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("legacy", [None, "root", "steer"])
async def test_confirmed_steer_is_preserved_by_the_actual_wiki_return(steering, cold, legacy):
    env = steering
    jobs, job, original, steer, correction = await create_steered_job(env)
    if legacy is not None:
        # Retained-format fixture: older messages can coexist with new inline
        # messages in the same execution snapshot, including after restart.
        request = original if legacy == "root" else steer
        row = await env.db.get_submission_for_request(env.session.id, request.request_id)
        payload = dict(row["payload"])
        operation = payload.pop("operation")
        operation.pop("kind")
        payload.update(operation)
        async with env.db.transaction(write=True) as tx:
            await tx.execute("UPDATE client_submissions SET payload=? WHERE session_id=? AND request_id=?",
                             (json.dumps(payload), env.session.id, request.request_id))
    manager = env.manager
    if cold:
        await manager.stop_all_held_processes()
        await manager.close_session_owners()
        await env.db.recover_client_submissions()
        manager = SessionManager()
        await manager.initialize(env.db)
    try:
        original_row = await env.db.get_submission_for_request(env.session.id, original.request_id)
        assert json.loads(original_row["payload"]["knowledge"])["value"]["sources"] == []
        effective = await execution_context(env.db, env.session.id, original_row)
        assert effective is not None and effective.sources == ("wiki", "web")
        assert effective.origin.request_id == original.request_id
        assert correction in effective.trusted_request
        await job_notifications.dispatch(jobs, manager)
        cursor = await env.db.conn.execute(
            "SELECT request_id,payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
            (env.session.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][1])
        assert job["id"] in payload["operation"]["content"]
        assert payload["knowledge"] == encode(effective)
        receipt = await manager.source_receipt("wiki", payload["delivery_id"], session_id=env.session.id)
        assert receipt is not None
        returned = await asyncio.wait_for(receipt[1].wait(), 3)
        assert returned.outcome.status == "success" and returned.recording_state == "committed"
        assert (await env.db.get_submission_for_request(env.session.id, steer.request_id))["delivery_state"] == "confirmed"
    finally:
        if cold:
            await manager.drain_turns()
            await manager.stop_all_held_processes()
            await manager.close_session_owners()


async def test_unconfirmed_steer_keeps_results_as_material_after_cold_recovery(steering):
    env = steering
    jobs, job, original, steer, _correction = await create_steered_job(env, unconfirmed=True)
    await env.manager.stop_all_held_processes()
    await env.manager.close_session_owners()
    await env.db.recover_client_submissions()
    cold = SessionManager()
    await cold.initialize(env.db)
    try:
        original_row = await env.db.get_submission_for_request(env.session.id, original.request_id)
        assert await execution_context(env.db, env.session.id, original_row) is None
        inline = await env.db.get_submission_for_request(env.session.id, steer.request_id)
        assert inline["delivery_state"] == "unknown"
        await job_notifications.dispatch(jobs, cold)
        cursor = await env.db.conn.execute(
            "SELECT count(*) FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
            (env.session.id,),
        )
        assert (await cursor.fetchone())[0] == 0
        messages = await env.db.load_messages(env.session.id)
        assert any(message["type"] == "delivery_notice" for message in messages)
        cards = await env.db.wiki_job_cards(env.session.id, env.session.user_id)
        assert len(cards) == 1 and cards[0]["job_id"] == job["id"] and cards[0]["status"] == "completed"
        with jobs.store.connect() as connection:
            assert not job_events.pending(connection)
    finally:
        await cold.close_session_owners()

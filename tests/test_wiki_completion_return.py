"""Settled maintenance returns through the real original SessionOwner."""

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.harness import get_harness
from server.harness.driver import ArgvDriver
from server.wiki import job_events, job_notifications
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiWorker
from tests import test_wiki_api as wiki_api
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.fixture
async def returning(jobs, client, tmp_path, monkeypatch):
    manager = wiki_api.session_manager
    await manager.db.update_agent(jobs.registration["curator_agent_id"], backend="codex")
    original = await manager.create_session(jobs.registration["curator_agent_id"], "Original", str(tmp_path))
    gate = tmp_path / "finish-original"
    model = tmp_path / "return_model.py"
    model.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "gate,automatic=Path(sys.argv[1]),sys.argv[2]=='return'\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'return-proof'}),flush=True)\n"
        "while not automatic and not gate.exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'reply','type':'agent_message',"
        "'text':'Maintenance summary returned' if automatic else 'Submitted maintenance'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
    )
    prompts = []
    harness = get_harness("codex")

    def argv(context):
        automatic = "[wiki-maintenance-result" in context.prompt
        prompts.append(context.prompt)
        return [sys.executable, "-u", str(model), str(gate), "return" if automatic else "original"], {"cwd": context.working_dir}

    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        new_driver=ArgvDriver, build_turn_argv=argv,
    ))
    request = await manager.start_message(original.id, "Review these selected notes")
    state = SimpleNamespace(manager=manager, original=original, receipt=request, gate=gate, prompts=prompts)
    try:
        yield state
    finally:
        gate.touch()
        for runtime in {manager, state.manager}:
            await runtime.drain_turns(timeout=4)
            await runtime.stop_all_held_processes()
            await runtime.close_session_owners()


def submitted(jobs, returning, name):
    path = f"Library/{name}.md"
    WikiHistory(jobs.store).write(path, f"# {name}\nKeep this argument.\n".encode(), None, "human", "Create input")
    return jobs.maintain(name, "", "Review selected note", [path], None, [], "human", requester=job_events.Requester(
        jobs.user_id, "chat", returning.original.id, returning.receipt.request_id,
    ))


def complete(jobs, job):
    return jobs.update(job["id"], status="completed", items=[item | {"status": "done"} for item in job["items"]])


async def finish_original(returning):
    returning.gate.touch()
    completion = await asyncio.wait_for(returning.receipt.wait(), 5)
    assert completion.recording_state == "committed" and completion.outcome.status == "success"


@pytest.mark.parametrize("count", [1, 5])
async def test_return_includes_bounded_receipt_reasons_without_exporting_the_job(jobs, returning, client, count):
    from server.wiki import job_returns
    from tests.test_wiki_maintenance_state import settle

    job = submitted(jobs, returning, "receipt-summary")
    item = job["items"][0]
    reason = "Current explanation still applies. " + "checked context; " * 40
    for _ in range(count):
        result, _ = await settle(client, jobs, jobs.registration, path=item["path"], job=job, reason=reason)
    await finish_original(returning)
    with jobs.store.connect() as db:
        delivery = next(row for row in job_events.pending(db) if json.loads(row["body"])["status"] == "completed")
    group = job_returns._group(jobs, delivery)
    evidence = group["jobs"][0]["receipts"]
    assert len(evidence) == min(count, 4) and group["jobs"][0]["receipts_truncated"] == (count > 4)
    assert evidence[0]["receipt_id"] == result["receipt_id"] and evidence[0]["operation"] == "no_change"
    assert evidence[0]["path"] == item["path"] and evidence[0]["output_revision"] == item["revision"]
    assert evidence[0]["reason"] == reason[:384] and evidence[0]["reason_truncated"]


async def test_all_jobs_return_once_to_original_after_the_group_settles(jobs, returning, monkeypatch):
    first, second = submitted(jobs, returning, "first"), submitted(jobs, returning, "second")
    complete(jobs, first)
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(returning.prompts) <= 1
    await finish_original(returning)
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(returning.prompts) == 1
    complete(jobs, second)
    acknowledge = job_notifications._acknowledge

    def lost_ack(*args):
        raise OSError("injected return ACK loss")

    monkeypatch.setattr(job_notifications, "_acknowledge", lost_ack)
    with pytest.raises(OSError, match="ACK loss"):
        await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT request_id,payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    inputs = await cursor.fetchall()
    assert len(inputs) == 1, "All completed job cards must cause one automatic return to the original session"
    payload = json.loads(inputs[0][1])
    assert first["id"] in payload["content"] and second["id"] in payload["content"]
    context = json.loads(payload["knowledge"])["value"]
    assert context["trusted_request"] == "Review these selected notes"
    assert context["origin"]["request_id"] == returning.receipt.request_id
    receipt = (await returning.manager.source_receipt("wiki", payload["delivery_id"], session_id=returning.original.id))[1]
    result = await asyncio.wait_for(receipt.wait(), 5)
    assert result.state == "completed" and result.outcome.status == "success"
    messages = await returning.manager.db.load_committed_request_messages(returning.original.id, receipt.request_id)
    assert any(row["role"] == "assistant" and row["content"] == "Maintenance summary returned" for row in messages)
    monkeypatch.setattr(job_notifications, "_acknowledge", acknowledge)
    returning.manager._wiki_notice_cursors.clear()
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(returning.prompts) == 2
    with jobs.store.connect() as db:
        assert not job_events.pending(db)


async def test_accepted_return_survives_restart_as_a_retained_input_without_replay(jobs, returning, monkeypatch):
    from server.sessions import SessionManager

    job = submitted(jobs, returning, "paused")
    await finish_original(returning)
    await returning.manager.interrupt(returning.original.id)
    complete(jobs, job)
    acknowledge = job_notifications._acknowledge

    def lost_ack(*args):
        raise OSError("return accepted before restart")

    monkeypatch.setattr(job_notifications, "_acknowledge", lost_ack)
    with pytest.raises(OSError, match="accepted before restart"):
        await job_notifications.dispatch(jobs, returning.manager)
    queued = returning.manager.dispatch_snapshot(returning.original.id)
    assert queued["paused"] and len(queued["items"]) == 1
    request_id = queued["items"][0]["request_id"]
    database = returning.manager.db
    await returning.manager.close_session_owners()
    await database.recover_client_submissions()
    cold = SessionManager()
    await cold.initialize(database)
    returning.manager = cold
    monkeypatch.setattr(job_notifications, "_acknowledge", acknowledge)
    await job_notifications.dispatch(jobs, cold)
    saved = await database.get_submission_for_request(returning.original.id, request_id)
    assert saved["execution_state"] == "withdrawn"
    assert saved["payload"]["source"] == "wiki"
    pending, _ = await database.list_recoverable_submissions(returning.original.id)
    assert any(row["request_id"] == request_id for row in pending)
    assert len(returning.prompts) == 1
    with jobs.store.connect() as db:
        assert not job_events.pending(db)


async def test_attention_and_unconfirmed_cancel_return_once_without_claiming_completion(jobs, returning):
    first, second = submitted(jobs, returning, "attention"), submitted(jobs, returning, "cancelled")
    await finish_original(returning)
    jobs.update(first["id"], status="running", error="Index recovery remains pending",
                items=[item | {"status": "attention"} for item in first["items"]])
    worker = WikiWorker()
    worker.manager = returning.manager
    await worker.process(jobs, first["id"])
    assert jobs.get(first["id"])["status"] == "completed_with_attention"
    cancelled = jobs.update(second["id"], status="cancelled")
    with jobs.store.connect() as db:
        job_events.settlement(db, second["id"], cancelled["attempt"], "pending")
    await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    inputs = await cursor.fetchall()
    assert len(inputs) == 1
    body = json.loads(inputs[0][0])["content"]
    report = json.loads(body.split("\n")[-1])
    assert report["statuses"] == {"completed_with_attention": 1, "cancelled": 1}
    assert any(job["settlement_phase"] == "pending" for job in report["jobs"])
    assert "Index recovery remains pending" in body
    with jobs.store.connect() as db:
        job_events.settlement(db, second["id"], cancelled["attempt"], "confirmed")
    await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT count(*) FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.parametrize("with_success", [False, True])
async def test_real_worker_attention_terminal_returns_in_a_single_or_mixed_group(jobs, returning, with_success):
    attention = submitted(jobs, returning, "needs-review")
    success = submitted(jobs, returning, "done") if with_success else None
    await finish_original(returning)
    jobs.update(attention["id"], status="running",
                items=[item | {"status": "attention"} for item in attention["items"]])
    worker = WikiWorker()
    worker.manager = returning.manager
    await worker.process(jobs, attention["id"])
    assert jobs.get(attention["id"])["status"] == "completed_with_attention"
    if success:
        complete(jobs, success)
    await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    inputs = await cursor.fetchall()
    assert len(inputs) == 1
    report = json.loads(json.loads(inputs[0][0])["content"].split("\n")[-1])
    assert report["statuses"] == ({"completed_with_attention": 1, "completed": 1} if success else {"completed_with_attention": 1})


async def test_return_preserves_ancestor_context_of_the_immediate_submitter(jobs, returning):
    from server.sessions.client_submissions import saved_context

    await finish_original(returning)
    original_id = returning.receipt.request_id
    row = await returning.manager.db.get_submission_for_request(returning.original.id, original_id)
    context = saved_context(returning.original.id, row)
    returning.gate.unlink()
    returning.receipt = await returning.manager.start_message(
        returning.original.id, "Continue the original request", source="bg", delivery_id="ancestor-return",
        knowledge_context=context,
    )
    assert returning.receipt.request_id != context.origin.request_id
    job = submitted(jobs, returning, "inherited")
    await finish_original(returning)
    complete(jobs, job)
    await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    inputs = await cursor.fetchall()
    assert len(inputs) == 1
    returned = json.loads(json.loads(inputs[0][0])["knowledge"])["value"]
    assert returned["origin"]["request_id"] == original_id
    assert returned["maintenance"] == json.loads(row["payload"]["knowledge"])["value"]["maintenance"]


async def test_disabled_account_keeps_results_and_ends_automatic_return(jobs, returning):
    job = submitted(jobs, returning, "disabled-owner")
    await finish_original(returning)
    complete(jobs, job)
    await returning.manager.db.update_user_field(jobs.user_id, disabled_at="2026-10-25T14:00:00+00:00")
    await job_notifications.dispatch(jobs, returning.manager)
    with jobs.store.connect() as db:
        assert not job_events.pending(db)
    messages = await returning.manager.db.load_messages(returning.original.id)
    assert any(row["type"] == "delivery_notice" and "账户" in row["content"] for row in messages)
    assert len(returning.prompts) == 1
    assert (await returning.manager.db.wiki_job_cards(returning.original.id, jobs.user_id))[0]["status"] == "completed"


async def test_omitted_job_still_contributes_unconfirmed_recording_and_settlement(jobs, returning):
    batch = [submitted(jobs, returning, f"bounded-{number}") for number in range(11)]
    await finish_original(returning)
    for job in batch:
        complete(jobs, job)
    omitted = max(batch, key=lambda job: job["id"])
    cancelled = jobs.update(omitted["id"], status="cancelled", items=[item | {
        "status": "unknown", "worker_request": {
            "attempt": 0, "session_id": "unconfirmed-worker", "request_id": "unconfirmed-request", "phase": "unknown",
        },
    } for item in omitted["items"]])
    with jobs.store.connect() as db:
        job_events.settlement(db, omitted["id"], cancelled["attempt"], "pending")
    await job_notifications.dispatch(jobs, returning.manager)
    cursor = await returning.manager.db.conn.execute(
        "SELECT payload FROM client_submissions WHERE session_id=? AND json_extract(payload,'$.source')='wiki'",
        (returning.original.id,),
    )
    inputs = await cursor.fetchall()
    assert len(inputs) == 1
    report = json.loads(json.loads(inputs[0][0])["content"].split("\n")[-1])
    assert report["jobs_omitted"] == 1
    assert omitted["id"] not in {job["job_id"] for job in report["jobs"]}
    assert report["recording_pending"] == 1
    assert report["settlement_phases"] == {"none": 10, "pending": 1}

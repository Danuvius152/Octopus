"""Ticket handoff through the original SessionOwner and two native conversations."""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.bg_tasks import BgTaskManager
from server.database import Database
from server.deps import get_session_manager
from server.knowledge import capture, derive
from server.mcp_identity import McpScope, mint
from server.routers.sessions import get_submission_status
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.handoff import HandoffPointers, begin_ticket_batch, ticket_handoff
from server.sessions.submission import (
    ReturnBody,
    ReturnRefused,
    ReturnTarget,
    SubmissionReceipt,
)
from server.wiki import job_events, job_notifications
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from tests.callback_api import start_callback_api
from tests.claude_runtime import SID
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inject as claude_inject
from tests.claude_runtime import inputs as claude_inputs
from tests.native_app_server import (
    ROOT,
    active_root,
    inject,
    until,
    wait_until,
    wire_frames,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle

POINTERS = HandoffPointers(spec="spec.md", accepted_ticket="issues/A.md", candidate="fixed-A",
                           next_work="issues/B.md")


def live_scope(chat):
    run = chat._run
    return McpScope(chat.id, None, chat.user_id, run.wiki_task_id, run.backend.run_lease_id, run.work_id)


async def handoff_state(manager, chat, key="batch-A"):
    return await get_submission_status(manager, chat.id, f"handoff:{key}", user_id=chat.user_id)


def copy_product_database(root, label):
    path = root / f"{label}.db"
    with closing(sqlite3.connect(root / "product.db")) as source, closing(sqlite3.connect(path)) as dest:
        source.backup(dest)
    return path


async def test_accepted_ticket_starts_next_in_new_thread_with_original_scope(native_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr("server.harness.codex_rpc.RPC_TIMEOUT", 3)
    manager, database, chat, mode, _, _ = native_runtime
    sentinel = "PRIVATE-CONTEXT-OF-TICKET-A"
    trusted = "Implement the authorized batch from spec.md. Keep final release for acceptance."
    assert (await (await manager.start_message(chat.id, sentinel)).wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    api = await start_callback_api(overrides={get_session_manager: lambda: manager},
                                   readiness_token=mint(chat.id, user_id=chat.user_id))
    try:
        mode[0] = "control_output"
        active = await manager.start_message(chat.id, trusted)
        await until(lambda: chat._run.backend is not None and chat._run.backend.active_turn is not None
                    and chat._run.backend.active_turn.turn_id == "turn-2")
        scope = live_scope(chat)
        token = mint(**asdict(scope))
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http, streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/ask_agent/mcp", http_client=http,
        ) as (read, write, _), ClientSession(read, write) as client:
            await client.initialize()
            begin = await client.call_tool("begin_ticket_batch", {})
            assert not begin.isError
            assert begin.structuredContent == {"state": "registered", "original_request_id": active.request_id}
            assert (await client.call_tool("begin_ticket_batch", {})).structuredContent == begin.structuredContent
            result = await client.call_tool("handoff", {"handoff_id": "batch-A", "pointers": POINTERS.model_dump()})
            assert not result.isError
            registered = result.structuredContent
            original = await client.call_tool("handoff", {"handoff_id": "batch-A"})
            assert original.structuredContent == registered
    finally:
        await api.stop()
    assert registered["state"] == "registered"
    assert active.completion is None
    assert (await ticket_handoff(manager, scope, "batch-A", POINTERS)) == registered
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-2", "status": "completed"}}})
    assert (await active.wait()).outcome.status == "success"
    async def second_started():
        return "batch-A: started" in (await handoff_state(manager, chat))["message"]
    await wait_until(second_started)
    frames = wire_frames(tmp_path)
    assert sum(frame.get("method") == "thread/start" for frame in frames) == 2
    starts = [frame["params"] for frame in frames if frame.get("method") == "turn/start"]
    assert len(starts) == 3
    assert starts[-1]["threadId"] != starts[-2]["threadId"]
    prompts = str(starts[-1]["input"])
    assert trusted in prompts and "issues/B.md" in prompts and sentinel not in prompts
    assert manager.get_session(chat.id) is chat and Path(chat.working_dir) == tmp_path
    assert sentinel in [m["content"] for m in await database.load_messages(chat.id)]
    next_boundary = HandoffPointers(spec="spec.md", accepted_ticket="issues/B.md", candidate="fixed-B", next_work="final acceptance")
    assert (await ticket_handoff(manager, live_scope(chat), "batch-B", next_boundary))["state"] == "registered"
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": starts[-1]["threadId"],
                                                              "turn": {"id": "turn-3", "status": "completed"}}})
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]
    final = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"][-1]
    assert final["threadId"] != starts[-1]["threadId"]
    assert "final acceptance" in str(final["input"]) and trusted in str(final["input"]) and "fixed-A" not in str(final["input"])


async def test_claude_receipt_proves_new_native_history_received_the_exact_first_input(claude_runtime, tmp_path, monkeypatch):
    manager, database, chat, _, events = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "fresh-claude-B", "recovered-B", "fork-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    active = await manager.start_message(chat.id, "Use only spec.md and stop before release.")
    first, = await claude_inputs(tmp_path, 1)
    sentinel = "PRIVATE-CLAUDE-NOTES-OF-A"
    claude_inject(tmp_path, {"type": "assistant", "session_id": SID, "user_message_uuid": first["input"]["uuid"],
        "message": {"id": "ticket-a-note", "model": "deepseek-flash", "content": [{"type": "text", "text": sentinel}]}})
    await until(lambda: any(e.get("content") == sentinel for e in events))
    await begin_ticket_batch(manager, live_scope(chat))
    registered = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    assert registered["state"] == "registered"
    plan.write_text(json.dumps({"save_input_history": True, "hold_before_history": True, "hold_after_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await active.wait()).outcome.status == "success"
    first, second = await claude_inputs(tmp_path, 2)
    backend = chat._run.backend
    record_input = backend.record_installed_input
    checked = asyncio.Event()

    async def observe_first_check():
        await record_input()
        checked.set()

    monkeypatch.setattr(backend, "record_installed_input", observe_first_check)
    token = mint(**asdict(live_scope(chat)))
    api = await start_callback_api(overrides={get_session_manager: lambda: manager}, readiness_token=token)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http, streamable_http_client(
            f"http://127.0.0.1:{api.port}/mcp/ask_agent/mcp", http_client=http,
        ) as (read, write, _), ClientSession(read, write) as client:
            await client.initialize()
            query = asyncio.create_task(client.call_tool("handoff", {"handoff_id": "batch-A"}))
            await asyncio.wait_for(checked.wait(), 2)
            assert "batch-A: unknown" in (await handoff_state(manager, chat))["message"]
            (tmp_path / "claude-save-input").touch()
            confirmed = await asyncio.wait_for(query, 5)
            assert not confirmed.isError
            assert confirmed.structuredContent["request_id"] == registered["request_id"]
            assert confirmed.structuredContent["state"] == "started"
    finally:
        (tmp_path / "claude-save-input").touch()
        await api.stop()
    assert len(await claude_inputs(tmp_path, 2)) == 2
    plan.write_text(json.dumps({"save_input_history": True}))
    # An untagged result can carry a contradictory provider ID. It cannot
    # supersede the ID matched to the opening input in the saved history.
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False})
    await wait_dispatch_idle(manager, chat.id)
    assert second["pid"] != first["pid"] and "--resume" not in second["argv"]
    prompt = second["input"]["message"]["content"]
    assert "issues/B.md" in prompt and "stop before release" in prompt and sentinel not in prompt
    assert chat.claude_session_id == "fresh-claude-B"
    assert "batch-A: started" in (await handoff_state(manager, chat))["message"]
    assert sentinel in [m["content"] for m in await database.load_messages(chat.id)]
    await manager.stop_all_held_processes()
    from server.harness.claude_code import ClaudeDriver

    prepare = ClaudeDriver.prepare
    async def lost_history(driver, io, ctx, argv, kwargs):
        if ctx.resume_id == "fresh-claude-B":
            raise ValueError(f"No conversation found with session ID: {ctx.resume_id}")
        await prepare(driver, io, ctx, argv, kwargs)
    monkeypatch.setattr(ClaudeDriver, "prepare", lost_history)
    continued = await manager.start_message(chat.id, "Continue on B after history loss")
    assert (await continued.wait()).outcome.recovery["stale_session"] == 1
    await wait_dispatch_idle(manager, chat.id)
    recovered = (await claude_inputs(tmp_path, 3))[-1]["input"]["message"]["content"]
    assert "<session-history" in recovered and "issues/B.md" in recovered and sentinel not in recovered
    assert "stop before release" in recovered
    visible = await database.load_messages(chat.id)
    point = next(m["seq"] for m in visible if m.get("content") == "Continue on B after history loss")
    child = await manager.fork_session(chat.id, point, user_id=chat.user_id)
    assert sentinel in [m["content"] for m in await database.load_messages(child.id)]
    assert (await (await manager.start_message(child.id, "Continue the fork")).wait()).outcome.status == "success"
    forked = (await claude_inputs(tmp_path, 4))[-1]["input"]["message"]["content"]
    assert "<fork-history" in forked and "issues/B.md" in forked and sentinel not in forked
    assert "stop before release" in forked


@pytest.mark.parametrize("control", ["stop", "human"])
async def test_concurrent_ids_get_one_handoff_and_human_control_withdraws_it(native_runtime, tmp_path, monkeypatch, control):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    active = await manager.start_message(chat.id, "Finish ticket A")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    with pytest.raises(ValueError, match="Register the authorized ticket batch"):
        await ticket_handoff(manager, scope, "unregistered", POINTERS)
    await begin_ticket_batch(manager, scope)
    results = await asyncio.gather(*(ticket_handoff(manager, scope, key, POINTERS) for key in ["id-one", "id-two"]),
                                   return_exceptions=True)
    accepted, = [r for r in results if isinstance(r, dict)]
    refused, = [r for r in results if isinstance(r, ValueError)]
    key = accepted["handoff_id"]
    assert key in str(refused)
    if control == "stop":
        persist_stop = database.stop_ticket_batch

        async def stop_after_interrupt(session_id, request_id):
            assert manager._session_owner(chat).runtime.current.interrupted
            assert request_id == active.request_id
            await persist_stop(session_id, request_id)

        monkeypatch.setattr(database, "stop_ticket_batch", stop_after_interrupt)
        await manager.interrupt(chat.id)
        assert (await active.wait()).outcome.status == "interrupted"
    else:
        mode[0] = "normal"
        human = await manager.start_message(chat.id, "Pause ticket B and read my new instructions")
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
        assert (await active.wait()).outcome.status == "success"
        assert (await human.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    status = await handoff_state(manager, chat, key)
    assert status["execution"]["state"] == "withdrawn" and not status["execution"]["recoverable_input"]
    starts = [f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(starts) == (1 if control == "stop" else 2)
    assert all("issues/B.md" not in str(f["params"]["input"]) for f in starts)


async def test_claude_success_without_saved_first_input_leaves_original_handoff_unknown(claude_runtime, tmp_path, monkeypatch):
    manager, _, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "unconfirmed-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Implement ticket B when A is accepted")
    first, = await claude_inputs(tmp_path, 1)
    await begin_ticket_batch(manager, live_scope(chat))
    await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await original.wait()).outcome.status == "success"
    _, second = await claude_inputs(tmp_path, 2)
    monkeypatch.setattr("server.sessions.handoff._CLAUDE_FIRST_INPUT_WAIT_SECONDS", 0.1)
    observed = await asyncio.wait_for(ticket_handoff(manager, live_scope(chat), "batch-A"), 2)
    assert observed["state"] == "unknown"
    claude_inject(tmp_path, {"type": "result", "session_id": "unconfirmed-B", "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    await wait_dispatch_idle(manager, chat.id)
    receipt = await handoff_state(manager, chat)
    assert observed["request_id"] == receipt["request_id"] and "batch-A: unknown" in receipt["message"]
    assert not receipt["execution"]["recoverable_input"] and len(await claude_inputs(tmp_path, 2)) == 2


async def test_return_can_retry_if_a_registered_handoff_is_withdrawn(native_runtime, tmp_path):
    manager, _, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Complete ticket A")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    target = ReturnTarget(chat.id, "delegation", "review-a", scope.work_id, owned)
    registered = await ticket_handoff(manager, scope, "batch-A", POINTERS)

    async def material():
        return ReturnBody("The completed review of A")

    pending = await manager.admit_return(target, prepare=material)
    assert isinstance(pending, ReturnRefused) and pending.reason == "busy"
    assert await manager.cancel_request(chat.id, registered["request_id"])
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    returned = await manager.admit_return(target, prepare=material)
    assert isinstance(returned, SubmissionReceipt)
    assert (await returned.wait()).outcome.status == "success"
    assert (await handoff_state(manager, chat))["execution"]["state"] == "withdrawn"
    assert "The completed review of A" in str(wire_frames(tmp_path))


async def test_preparation_failure_cannot_be_mistaken_for_an_uncertain_handoff(claude_runtime, tmp_path, monkeypatch):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, _, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "resumed-B", "fresh-C"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    original = await manager.start_message(chat.id, "Complete ticket A")
    first, = await claude_inputs(tmp_path, 1)
    await until(lambda: chat.claude_session_id == SID)
    await begin_ticket_batch(manager, live_scope(chat))
    await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)

    async def refused_preparation(*args, **kwargs):
        raise ValueError("The fixture credential is unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(OwnerRuntime, "_prepare_run", refused_preparation)
        claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                                 "user_message_uuid": first["input"]["uuid"]})
        assert (await original.wait()).outcome.status == "success"
        await wait_dispatch_idle(manager, chat.id)
    status = await handoff_state(manager, chat)
    assert "batch-A: failed" in status["message"]
    assert status["execution"]["delivery_state"] == "none"
    assert len(await claude_inputs(tmp_path, 1)) == 1

    resumed = await manager.start_message(chat.id, "The credential is ready. Start the next authorized ticket.")
    _, second = await claude_inputs(tmp_path, 2)
    await until(lambda: chat.claude_session_id == "resumed-B")
    await begin_ticket_batch(manager, live_scope(chat))
    assert (await ticket_handoff(manager, live_scope(chat), "batch-B", POINTERS))["state"] == "registered"
    plan.write_text(json.dumps({"save_input_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": "resumed-B", "subtype": "success", "is_error": False,
                             "user_message_uuid": second["input"]["uuid"]})
    assert (await resumed.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-B: started" in (await handoff_state(manager, chat, "batch-B"))["message"]


async def test_restart_observes_the_original_handoff_without_replaying_uncertain_work(native_runtime, tmp_path):
    manager, _, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    active = await manager.start_message(chat.id, "Complete the authorized batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    await begin_ticket_batch(manager, live_scope(chat))
    receipt = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)

    pending = copy_product_database(tmp_path, "registered")
    mode[0] = "stop_late_accepted"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await active.wait()).outcome.status == "success"
    await until(lambda: len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2)
    uncertain = copy_product_database(tmp_path, "sent")
    await manager.interrupt(chat.id)
    await wait_dispatch_idle(manager, chat.id)
    for path, expected in [(pending, "withdrawn"), (uncertain, "unknown")]:
        database = Database(str(path))
        await database.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(database, previous_writers_retired=True)
            status = await handoff_state(restored, chat)
            assert status["request_id"] == receipt["request_id"]
            assert status["execution"]["state"] == expected and not status["execution"]["recoverable_input"]
            assert restored.dispatch_snapshot(chat.id)["items"] == []
        finally:
            await restored.close_session_owners()
            await database.close()
    assert sum(f.get("method") == "turn/start" for f in wire_frames(tmp_path)) == 2


async def test_claude_handoff_restart_distinguishes_claim_before_write_from_possible_delivery(
    claude_runtime, tmp_path, monkeypatch,
):
    manager, database, chat, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "isolated-claude"))
    (tmp_path / "claude-fresh-ids.json").write_text(json.dumps([SID, "new-claude-B"]))
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold": True}))
    active = await manager.start_message(chat.id, "Finish the authorized batch")
    first, = await claude_inputs(tmp_path, 1)
    await begin_ticket_batch(manager, live_scope(chat))
    registered = await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    clips = {}
    record = database.record_input_history

    async def capture_write_barrier(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run):
        if request_id == registered["request_id"] and native_id is None and may_have_run:
            clips["withdrawn"] = copy_product_database(tmp_path, "claimed-before-write")
            await record(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run)
            clips["unknown"] = copy_product_database(tmp_path, "possibly-sent")
        else:
            await record(session_id, request_id, attempt_id, generation, backend, native_id, may_have_run)

    monkeypatch.setattr(database, "record_input_history", capture_write_barrier)
    plan.write_text(json.dumps({"save_input_history": True, "hold_after_history": True}))
    claude_inject(tmp_path, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
                             "user_message_uuid": first["input"]["uuid"]})
    assert (await active.wait()).outcome.status == "success"
    await claude_inputs(tmp_path, 2)

    async def received():
        return (await ticket_handoff(manager, live_scope(chat), "batch-A"))["state"] == "started"

    await wait_until(received)
    clips["started"] = copy_product_database(tmp_path, "proven-sent")
    for expected, path in clips.items():
        stored = Database(str(path))
        await stored.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(stored, previous_writers_retired=True)
            status = await handoff_state(restored, chat)
            assert status["request_id"] == registered["request_id"]
            assert f"batch-A: {expected}" in status["message"] and not status["execution"]["recoverable_input"]
            assert restored.dispatch_snapshot(chat.id)["items"] == []
            if expected != "started":
                conflict = await stored.conflicting_handoff(chat.id, "a-new-human-request", "a-new-native-history")
                assert conflict == ("batch-A" if expected == "unknown" else None)
        finally:
            await restored.close_session_owners()
            await stored.close()
    assert len(await claude_inputs(tmp_path, 2)) == 2


async def test_confirmed_steer_survives_handoff_and_late_background_output_stays_at_its_source(native_runtime, tmp_path):
    manager, database, chat, mode, _, _ = native_runtime
    await active_root(native_runtime, tmp_path)
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    background = BgTaskManager()
    release = asyncio.Event()

    async def return_after_boundary(record):
        await release.wait()
        return await manager.deliver_bg_result(record)

    background.bind(database, return_after_boundary, manager._broadcast)
    await background.start()
    try:
        material = "A-background-material"
        job = await background.start_task(session_id=chat.id, working_dir=str(tmp_path), command=f"echo {material}",
            scope=scope, owned_context=derive(capture(chat, scope, prompt="Read earlier evidence"), "Read evidence"))
        correction = "Only handle ticket B. Leave deployment paused."
        accepted = await submit_client_message(manager, chat.id, correction, [], "narrow", wiki_mode=None)
        assert accepted["disposition"] == "steered"
        async def confirmed():
            observed = await submission_status(manager, chat.id, "narrow")
            return observed["execution"]["delivery_state"] == "confirmed"
        await wait_until(confirmed)
        assert (await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS))["state"] == "registered"
        mode[0] = "normal"
        (tmp_path / "native-finish").touch()
        await wait_dispatch_idle(manager, chat.id)
        assert "batch-A: started" in (await handoff_state(manager, chat))["message"]
        release.set()
        async def retained():
            record = await background.get_task(job.id)
            return record and record.delivery_disposition == "stopped"
        await wait_until(retained)
        kept = await background.get_task(job.id)
        assert kept.exit_code == 0 and kept.stdout.strip() == material
        starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
        assert len(starts) == 2 and correction in str(starts[-1]["input"]) and material not in str(starts[-1]["input"])
    finally:
        release.set()
        await background.shutdown()


async def test_late_wiki_job_stays_in_its_card_without_opening_a_turn_in_the_new_history(native_runtime, tmp_path, monkeypatch):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, database, chat, mode, _, _ = native_runtime
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(chat.user_id, vault, chat.agent_id)
    jobs = WikiJobs(chat.user_id)
    path = "Library/old-ticket.md"
    WikiHistory(jobs.store).write(path, b"# Old ticket\nRetained material.\n", None, "human", "Create input")
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the authorized batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    await begin_ticket_batch(manager, live_scope(chat))
    job = jobs.maintain("old-ticket", "", "Review old evidence", [path], None, [], "human",
                        requester=job_events.Requester(chat.user_id, "chat", chat.id, original.request_id))
    claimed = asyncio.Event()
    release = asyncio.Event()
    prepare_input = OwnerRuntime._prepare_input

    async def hold_before_boundary(runtime, configured, request, execution):
        if request.source == "handoff":
            claimed.set()
            await release.wait()
        return await prepare_input(runtime, configured, request, execution)

    monkeypatch.setattr(OwnerRuntime, "_prepare_input", hold_before_boundary)
    assert (await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS))["state"] == "registered"
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await asyncio.wait_for(claimed.wait(), 5)
    jobs.update(job["id"], status="completed", items=[item | {"status": "done"} for item in job["items"]])
    try:
        await job_notifications.dispatch(jobs, manager)
        with jobs.store.connect() as db:
            pending, = job_events.pending(db)
            assert pending["job_id"] == job["id"] and json.loads(pending["body"])["status"] == "completed"
    finally:
        release.set()
    await wait_dispatch_idle(manager, chat.id)
    assert "batch-A: started" in (await handoff_state(manager, chat))["message"]

    await job_notifications.dispatch(jobs, manager)
    await wait_dispatch_idle(manager, chat.id)
    with jobs.store.connect() as db:
        assert not job_events.pending(db)
    cards = await database.wiki_job_cards(chat.id, chat.user_id)
    assert [(card["job_id"], card["status"]) for card in cards] == [(job["id"], "completed")]
    assert sum(f.get("method") == "turn/start" for f in wire_frames(tmp_path, finished=True)) == 2


@pytest.mark.parametrize("scope_update", [
    "none", "replacement", "replacement_reopened", "steers", "stop_restart", "restart_stop", "reopen_stop",
])
async def test_review_callback_can_continue_only_with_the_current_human_scope(native_runtime, tmp_path, scope_update):
    manager, database, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the ticket batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    updates = ["Keep the release paused.", "Run only the checks for B."]

    async def steer(text, client_id):
        await until(lambda: chat._steer_open)
        receipt = await submit_client_message(manager, chat.id, text, [], client_id, wiki_mode=None)
        assert receipt["disposition"] == "steered"

        async def confirmed():
            observed = await submission_status(manager, chat.id, client_id)
            return observed["execution"]["delivery_state"] == "confirmed"

        await wait_until(confirmed)

    if scope_update == "steers":
        await steer(updates[0], "first-change")
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    replaced = scope_update in {"replacement", "replacement_reopened"}
    if replaced:
        changed = await manager.start_message(chat.id, "Pause implementation and discuss the plan first")
        assert (await changed.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    if scope_update == "replacement_reopened":
        await manager.archive_session(chat.id, replace=False)
        await manager.unarchive_session(chat.id, chat.user_id)
        await manager.interrupt(chat.id)
        assert await database.ticket_batch_state(chat.id, original.request_id) == "registered"

    async def material():
        return ReturnBody("Earlier review of ticket A")

    target = ReturnTarget(chat.id, "delegation", "old-review", scope.work_id, owned)
    if scope_update in {"stop_restart", "restart_stop", "reopen_stop"}:
        if scope_update == "stop_restart":
            await manager.interrupt(chat.id)
        path = copy_product_database(tmp_path, "stopped-batch")
        stored = Database(str(path))
        await stored.initialize()
        restored = SessionManager()
        try:
            await restored.initialize(stored, previous_writers_retired=True)
            if scope_update == "reopen_stop":
                await restored.archive_session(chat.id, replace=False)
                await restored.unarchive_session(chat.id, chat.user_id)
            if scope_update != "stop_restart":
                await restored.interrupt(chat.id)
            assert await stored.ticket_batch_state(chat.id, original.request_id) == "stopped"
            stale = await restored.admit_return(target, prepare=material)
            assert isinstance(stale, ReturnRefused) and stale.reason == "admission_closed"
            assert await restored.source_receipt("delegation", "old-review", session_id=chat.id) is None
        finally:
            await restored.close_session_owners()
            await stored.close()
        return

    mode[0] = "control_output"
    callback = await manager.admit_return(target, prepare=material)
    if replaced:
        assert isinstance(callback, ReturnRefused) and callback.reason == "admission_closed"
        assert await manager.source_receipt("delegation", "old-review", session_id=chat.id) is None
        assert len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2
        return
    assert isinstance(callback, SubmissionReceipt)
    await until(lambda: len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == 2)
    if scope_update == "steers":
        starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
        assert updates[0] in str(starts[1]["input"]) and updates[1] not in str(starts[1]["input"])
        await steer(updates[1], "second-change")
    await ticket_handoff(manager, live_scope(chat), "batch-A", POINTERS)
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {
        "id": "turn-2", "status": "completed"}}})
    assert (await callback.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    receipt = await handoff_state(manager, chat)
    assert "batch-A: started" in receipt["message"]
    starts = [f["params"] for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(starts) == 3
    new_input = str(starts[-1]["input"])
    assert "issues/B.md" in new_input
    if scope_update == "steers":
        assert all(update in new_input for update in updates)
        assert new_input.index(updates[0]) < new_input.index(updates[1])


@pytest.mark.parametrize("control", ["human", "stop"])
async def test_control_after_review_admission_cancels_its_unwritten_input(native_runtime, tmp_path, monkeypatch, control):
    from server.sessions.owner_runtime import OwnerRuntime

    manager, _, chat, mode, _, _ = native_runtime
    mode[0] = "control_output"
    original = await manager.start_message(chat.id, "Implement the ticket batch")
    await until(lambda: any(f.get("method") == "turn/start" for f in wire_frames(tmp_path)))
    scope = live_scope(chat)
    await begin_ticket_batch(manager, scope)
    owned = derive(capture(chat, scope, prompt="Review A"), "Review A")
    mode[0] = "normal"
    inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    assert (await original.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)

    async def material():
        return ReturnBody("Earlier review of ticket A")

    preparing = asyncio.Event()
    release = asyncio.Event()
    prepare_input = OwnerRuntime._prepare_input

    async def hold_before_input(runtime, configured, request, execution):
        if request.batch_return:
            preparing.set()
            await release.wait()
        return await prepare_input(runtime, configured, request, execution)

    monkeypatch.setattr(OwnerRuntime, "_prepare_input", hold_before_input)
    callback = await manager.admit_return(ReturnTarget(chat.id, "delegation", "old-review", scope.work_id, owned),
                                          prepare=material)
    assert isinstance(callback, SubmissionReceipt)
    human = None
    try:
        await asyncio.wait_for(preparing.wait(), 5)
        if control == "stop":
            assert await manager.interrupt(chat.id)
        else:
            human = await manager.start_message(chat.id, "Pause implementation")
    finally:
        release.set()
    assert (await callback.wait()).outcome.status == "interrupted"
    if human is not None:
        assert (await human.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, chat.id)
    starts = [f["params"] for f in wire_frames(tmp_path, finished=True) if f.get("method") == "turn/start"]
    assert len(starts) == (1 if control == "stop" else 2)
    assert "Earlier review of ticket A" not in str(starts)

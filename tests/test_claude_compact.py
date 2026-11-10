"""Native Claude maintenance through public APIs, SQLite and a controlled CLI."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing

import pytest

from server.database import Database
from server.harness import HarnessRun
from server.routers.sessions import get_session, get_submission_status, submit_input
from server.sessions import SessionManager
from server.storage_verification import verify_main
from server.submission_models import ClientSubmissionRequest
from tests.claude_runtime import SID, inject, inputs
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.session_runtime import wait_dispatch_idle


def compact_request(client_id="compact-original"):
    return ClientSubmissionRequest.model_validate({"client_request_id": client_id, "operation": {"kind": "compact"}})


@pytest.mark.parametrize("held", [False, True])
async def test_claude_native_compact_once_then_continue_original_history(claude_runtime, tmp_path, held):
    manager, _, session, starts, _ = claude_runtime
    await (await manager.start_message(session.id, "Keep original product messages")).wait()
    await wait_dispatch_idle(manager, session.id)
    if not held:
        await manager.stop_all_held_processes()
    accepted = await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    assert accepted["accepted"] and accepted["operation"] == "compact"
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "success"
    assert result["outcome"]["compact"]["effect"] == "applied"
    assert result["outcome"]["final_answer"] is None
    before = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert before.validity == "stale" and before.invalidation.reason == "compacted"
    assert before.sample.used_tokens == 666
    duplicate = await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    assert duplicate["request_id"] == accepted["request_id"]
    await (await manager.start_message(session.id, "Continue original history")).wait()
    await wait_dispatch_idle(manager, session.id)
    records = await inputs(tmp_path, 3)
    assert [r["input"]["message"]["content"] for r in records].count("/compact") == 1
    assert records[0]["pid"] != records[1]["pid"]
    for record in records[1:]:
        args = record["argv"]
        assert args[args.index("--resume") + 1] == SID
    assert len(starts) == 3 and all(context.resume_id == SID for context in starts[1:])
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert detail.context_state.validity == "valid"
    assert [m.content for m in detail.messages if m.role.value == "user"] == [
        "Keep original product messages", "Continue original history"]
    assert [m.content for m in detail.messages if m.role.value == "assistant" and m.type == "text"] == [
        "Native answer", "Native answer after compaction"]


async def test_native_local_failure_is_not_hidden_by_success_wrapper(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "local_failed"}))
    accepted = await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    assert accepted["accepted"]
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed"
    compact = result["outcome"]["compact"]
    assert compact["effect"] == "unknown"
    assert compact["reason"] == "Error: No messages to compact"
    assert compact["native_command"]["local_command_run"] == {"command": "compact", "args": ""}
    assert compact["native_command"]["local_command_outcome"] == {"kind": "failed"}
    records = await inputs(tmp_path, 2)
    assert compact["native_command"]["input_uuid"] == records[1]["input"]["uuid"]
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert [m.content for m in detail.messages if m.role.value == "assistant" and m.type == "text"] == ["Native answer"]
    assert detail.context_state.validity == "stale" and detail.context_state.activity is None
    await manager.stop_all_held_processes()
    await manager.close_session_owners()
    with closing(sqlite3.connect(tmp_path / "product.db")) as source, closing(sqlite3.connect(tmp_path / "backup.db")) as target:
        assert verify_main(source) == 9
        source.backup(target)
        assert verify_main(target) == 9
    restored_db = Database(str(tmp_path / "backup.db"))
    await restored_db.initialize()
    restored = SessionManager()
    try:
        await restored.initialize(restored_db, previous_writers_retired=True)
        recovered = await get_submission_status(restored, session.id, "compact-original", user_id=session.user_id)
        assert recovered["outcome"] == result["outcome"]
        assert len(await inputs(tmp_path, 2)) == 2
    finally:
        await restored.close_session_owners()
        await restored_db.close()


async def test_confirmed_native_noop_preserves_last_valid_sample(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    before = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "noop"}))
    accepted = await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    assert accepted["accepted"]
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "success"
    assert result["outcome"]["compact"]["effect"] == "not_applied"
    assert result["outcome"]["compact"]["reason"] == "Not enough messages to compact."
    after = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert after.validity == "valid" and after.activity is None and after.sample == before.sample


@pytest.mark.parametrize("mode", ["success_without_boundary", "auto_only", "no_start", "disconnect",
                                  "malformed_noop", "missing_identity", "unrecognized_noop"])
async def test_unproven_compaction_never_claims_noop_or_replays(claude_runtime, tmp_path, mode):
    manager, _, session, starts, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": mode}))
    assert (await submit_input(manager, session.id, compact_request(), user_id=session.user_id))["accepted"]
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "unknown"
    assert not any(result["outcome"]["recovery"].values())
    assert len(starts) == 2 and len(await inputs(tmp_path, 2)) == 2
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.activity is None


async def test_explicit_local_failure_vetoes_documented_noop_words(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "local_failed", "reason": "Not enough messages to compact."}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "unknown"
    assert result["outcome"]["compact"]["native_command"]["local_command_outcome"]["kind"] == "failed"


async def test_failure_after_boundary_keeps_applied_effect(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "applied_then_error"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "applied"
    assert result["outcome"]["compact"]["reason"] == "Native post-compaction hook failed"


@pytest.mark.parametrize("effect", ["absent", "before_stop", "during_retirement"])
async def test_stop_retains_original_native_effect_through_eof(claude_runtime, tmp_path, effect):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    current = (await inputs(tmp_path, 2))[1]["input"]["uuid"]
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": SID,
        "uuid": "stop-boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000}}
    if effect == "before_stop":
        inject(tmp_path, boundary)
    elif effect == "during_retirement":
        (tmp_path / "claude-eof.json").write_text(json.dumps([boundary]))
    async with asyncio.timeout(3):
        while True:
            state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
            if effect == "before_stop" and state.validity == "stale" or effect != "before_stop" and state.activity == "compacting":
                break
            await asyncio.sleep(0)
    assert await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "interrupted"
    compact = result["outcome"]["compact"]
    assert compact["effect"] == ("unknown" if effect == "absent" else "applied")
    assert compact["native_command"]["input_uuid"] == current
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.activity is None
    with pytest.raises(ProcessLookupError):
        os.killpg((await inputs(tmp_path, 2))[1]["pid"], 0)


@pytest.mark.parametrize("effect", [False, True])
async def test_archive_preserves_original_claude_compact_effect(claude_runtime, tmp_path, effect):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    accepted = await submit_input(manager, session.id, compact_request("archive-compact"), user_id=session.user_id)
    await inputs(tmp_path, 2)
    async with asyncio.timeout(3):
        while (await get_session(manager, session.id, user_id=session.user_id)).context_state.activity != "compacting":
            await asyncio.sleep(0)
    owner = manager._session_owners[id(session)]
    if effect:
        (tmp_path / "claude-eof.json").write_text(json.dumps([{
            "type": "system", "subtype": "compact_boundary", "session_id": SID,
            "uuid": "archive-boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000},
        }]))

    await asyncio.wait_for(manager.archive_session(session.id, replace=False), 5)
    assert not owner.failed
    result = await get_submission_status(manager, session.id, "archive-compact", user_id=session.user_id)
    assert result["request_id"] == accepted["request_id"]
    assert result["outcome"]["status"] == "interrupted"
    assert result["outcome"]["compact"]["effect"] == ("applied" if effect else "unknown")
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.activity is None


@pytest.mark.parametrize("closure", ["terminal", "eof"])
async def test_stop_after_parser_closure_preserves_unconsumed_boundary(claude_runtime, tmp_path, monkeypatch, closure):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original_stream = HarnessRun.stream
    original_runs = []

    async def hold_original_prefix(run):
        async for event in original_stream(run):
            yield event
            if event.context is not None and event.context.kind == "compaction_started":
                original_runs.append(run)
                entered.set()
                await release.wait()

    monkeypatch.setattr(HarnessRun, "stream", hold_original_prefix)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        current = (await inputs(tmp_path, 2))[1]["input"]["uuid"]
        inject(tmp_path, {"type": "system", "subtype": "compact_boundary", "session_id": SID,
            "uuid": "parsed-but-unconsumed", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000}},
            {"__exit": True} if closure == "eof" else {"type": "result", "session_id": SID,
                "user_message_uuid": current, "subtype": "success", "is_error": False, "local_command": "compact"})
        async with asyncio.timeout(3):
            while not original_runs[0]._stream_closed:
                await asyncio.sleep(0)
        before = (await get_session(manager, session.id, user_id=session.user_id)).context_state
        assert before.validity == "valid" and before.activity == "compacting"
        assert await manager.interrupt(session.id)
        release.set()
        await wait_dispatch_idle(manager, session.id)
        result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
        assert result["outcome"]["status"] == "interrupted"
        assert result["outcome"]["compact"]["effect"] == "applied"
        state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
        assert state.validity == "stale" and state.activity is None
        assert state.invalidation.reason == "compacted" and state.sample.used_tokens == 666
    finally:
        release.set()


async def test_known_unavailable_command_is_refused_before_sending(claude_runtime, tmp_path):
    manager, _, session, starts, _ = claude_runtime
    (tmp_path / "claude-plan.json").write_text(json.dumps({"commands": []}))
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert not detail.can_compact and "environment" in detail.compact_unavailable_reason
    accepted = await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    assert not accepted["accepted"] and accepted["code"] == "compact_unavailable"
    assert len(starts) == 1 and len(await inputs(tmp_path, 1)) == 1


async def test_wrong_resumed_native_session_fails_without_rebinding_history(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"init_session_id": "wrong-native-session"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "unknown"
    assert session.claude_session_id == SID


async def test_unassociated_terminal_cannot_redirect_followup_history(claude_runtime, tmp_path):
    manager, _, session, starts, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"compact_mode": "hold"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await inputs(tmp_path, 2)
    inject(tmp_path, {"type": "result", "session_id": "foreign-history", "subtype": "success",
        "is_error": False, "local_command": "compact", "result": ""})
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "unknown"
    assert (await get_session(manager, session.id, user_id=session.user_id)).claude_session_id == SID
    plan.write_text("{}")
    await (await manager.start_message(session.id, "Continue original history")).wait()
    await wait_dispatch_idle(manager, session.id)
    assert starts[-1].resume_id == SID
    record = (await inputs(tmp_path, 3))[-1]
    assert record["argv"][record["argv"].index("--resume") + 1] == SID


async def test_refusal_at_original_pipe_write_proves_no_compaction_effect(claude_runtime, tmp_path, monkeypatch):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    original = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    authorize = HarnessRun.authorize_input

    async def refuse_compact(run):
        if run.operation == "compact":
            raise ValueError("Original write authority was revoked")
        await authorize(run)

    monkeypatch.setattr(HarnessRun, "authorize_input", refuse_compact)
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "not_applied"
    assert len(await inputs(tmp_path, 1)) == 1
    after = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert after == original


async def test_later_command_message_cannot_erase_original_failure(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    current = (await inputs(tmp_path, 2))[1]["input"]["uuid"]
    frames = [{"type": "assistant", "session_id": SID, "local_command_run": {"command": "compact", "args": ""},
        "local_command_outcome": {"kind": kind}, "message": {"model": "<synthetic>",
            "content": [{"type": "text", "text": text}]}}
        for kind, text in [("failed", "Native command failed"), ("completed", "Not enough messages to compact.")]]
    inject(tmp_path, *frames, {"type": "result", "session_id": SID, "subtype": "success", "is_error": False,
        "user_message_uuid": current, "local_command": "compact", "result": "Not enough messages to compact."})
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == "failed" and result["outcome"]["compact"]["effect"] == "unknown"
    assert result["outcome"]["compact"]["native_command"]["local_command_outcome"]["kind"] == "failed"


async def test_unassociated_terminal_keeps_native_failure_and_separate_diagnostic(claude_runtime, tmp_path):
    manager, _, session, _, events = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    await submit_input(manager, session.id, compact_request(), user_id=session.user_id)
    await inputs(tmp_path, 2)
    inject(tmp_path, {"type": "assistant", "session_id": SID,
        "local_command_run": {"command": "compact", "args": ""}, "local_command_outcome": {"kind": "failed"},
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Native hook refused compaction"}]}},
        {"type": "result", "session_id": SID, "subtype": "success", "is_error": False, "result": ""})
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["compact"]["reason"] == "Native hook refused compaction"
    assert result["outcome"]["compact"]["native_command"]["local_command_outcome"] == {"kind": "failed"}
    diagnostics = [m for m in (await get_session(manager, session.id, user_id=session.user_id)).messages
        if m.type == "recovery" and m.content.get("code") == "native_compact_unassociated"]
    assert len(diagnostics) == 1 and "could not be associated" in diagnostics[0].content["message"]
    assert any(e["type"] == "recovery" and e.get("code") == "native_compact_unassociated" for e in events)


@pytest.mark.parametrize("effect", ["unknown", "applied"])
@pytest.mark.parametrize("local_failure", [False, True])
async def test_terminal_errors_keep_native_reason_in_query_live_and_restored_history(claude_runtime, tmp_path, effect, local_failure):
    manager, database, session, _, events = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    receipt = await manager.start_compact(session.id, client_request_id="compact-original")
    current = (await inputs(tmp_path, 2))[1]["input"]["uuid"]
    errors = ["Native hook refused compaction", "Native command cannot continue"]
    reason = "Earlier native failure" if local_failure else "\n".join(errors)
    frames = []
    if local_failure:
        frames.append({"type": "assistant", "session_id": SID,
            "local_command_run": {"command": "compact", "args": ""}, "local_command_outcome": {"kind": "failed"},
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": reason}]}})
    if effect == "applied":
        frames.append({"type": "system", "subtype": "compact_boundary", "session_id": SID,
            "uuid": "errors-boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000}})
    inject(tmp_path, *frames, {"type": "result", "session_id": SID, "user_message_uuid": current,
        "subtype": "error_during_execution", "is_error": True, "local_command": "compact", "errors": errors})
    assert (await receipt.wait()).outcome.status == "failed"
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["compact"]["reason"] == reason
    assert result["outcome"]["compact"]["effect"] == effect
    live = [event for event in events if event.get("request_id") == receipt.request_id and event.get("terminal")][-1]
    history = [message for message in (await get_session(manager, session.id, user_id=session.user_id)).messages
        if message.type == "result" and message.content.get("request_id") == receipt.request_id][-1]
    assert live["content"]["compact"]["reason"] == history.content["compact"]["reason"] == reason
    await manager.stop_all_held_processes()
    restarted = SessionManager()
    try:
        await restarted.initialize(database, previous_writers_retired=True)
        restored = await get_submission_status(restarted, session.id, "compact-original", user_id=session.user_id)
        assert restored["outcome"] == result["outcome"]
        messages = (await get_session(restarted, session.id, user_id=session.user_id)).messages
        retained = [message for message in messages if message.type == "result"
            and message.content.get("request_id") == receipt.request_id][-1]
        assert retained.content["compact"]["reason"] == reason
    finally:
        await restarted.close_session_owners()


@pytest.mark.parametrize("closure", ["eof", "stop"])
@pytest.mark.parametrize("effect", ["unknown", "applied"])
async def test_native_failure_reason_survives_missing_terminal(claude_runtime, tmp_path, closure, effect):
    manager, _, session, _, events = claude_runtime
    await (await manager.start_message(session.id, "Native history exists")).wait()
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
    receipt = await manager.start_compact(session.id, client_request_id="compact-original")
    await inputs(tmp_path, 2)
    frames = [{"type": "assistant", "session_id": SID,
        "local_command_run": {"command": "compact", "args": ""}, "local_command_outcome": {"kind": "failed"},
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Native hook refused compaction"}]}}]
    if effect == "applied":
        frames.append({"type": "system", "subtype": "compact_boundary", "session_id": SID,
            "uuid": "applied-before-close", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000}})
    if closure == "stop":
        (tmp_path / "claude-eof.json").write_text(json.dumps(frames))
        await manager.interrupt(session.id)
    else:
        inject(tmp_path, *frames, {"__exit": True})
    completion = await receipt.wait()
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert result["outcome"]["status"] == ("interrupted" if closure == "stop" else "failed")
    assert result["outcome"]["compact"]["effect"] == effect
    assert result["outcome"]["compact"]["reason"] == "Native hook refused compaction"
    assert completion.outcome.compact.reason == "Native hook refused compaction"
    live = next(e for e in events if e["type"] == "result" and e.get("request_id") == receipt.request_id)
    history = next(m for m in (await get_session(manager, session.id, user_id=session.user_id)).messages
        if m.type == "result" and m.content.get("request_id") == receipt.request_id)
    assert live["content"]["compact"]["reason"] == history.content["compact"]["reason"] == "Native hook refused compaction"

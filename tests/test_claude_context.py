"""Claude context through public session APIs and the real subprocess seam."""

import asyncio
import json

import pytest

from server.routers.sessions import get_session, get_submission_status
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message
from tests.claude_runtime import MODEL, SID, inject, inputs
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.session_runtime import wait_dispatch_idle


def assistant(input_id, mid="sample", used=200, *, model=MODEL, **extra):
    return {"type": "assistant", "session_id": SID, "parent_tool_use_id": None,
        "user_message_uuid": input_id, "message": {"id": mid, "model": model,
            "usage": {"input_tokens": used, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 30, "output_tokens": 90000},
            "content": []}, **extra}


def terminal(input_id, models=None):
    return {"type": "result", "subtype": "success", "is_error": False, "session_id": SID,
        "user_message_uuid": input_id, "modelUsage": models if models is not None else {
            "wrong-first": {"contextWindow": 1000000}, MODEL: {"contextWindow": 200000}}}


async def context_when(manager, session, predicate):
    async with asyncio.timeout(3):
        while True:
            state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
            if state is not None and predicate(state):
                return state
            await asyncio.sleep(0)


async def hold_next(manager, session, root, count):
    (root / "claude-plan.json").write_text(json.dumps({"hold": True}))
    receipt = await manager.start_message(session.id, "Next original input")
    records = await inputs(root, count)
    return receipt, records[-1]["input"]["uuid"]


async def test_archive_preserves_original_claude_message_tail_context(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    (tmp_path / "claude-plan.json").write_text(json.dumps({"hold": True}))
    accepted = await submit_client_message(manager, session.id, "Original tail", [], "archive-tail", wiki_mode=None)
    current = (await inputs(tmp_path, 1))[0]["input"]["uuid"]
    owner = manager._session_owners[id(session)]
    (tmp_path / "claude-eof.json").write_text(json.dumps([assistant(current), terminal(current)]))

    await asyncio.wait_for(manager.archive_session(session.id, replace=False), 5)
    assert not owner.failed
    result = await get_submission_status(manager, session.id, "archive-tail", user_id=session.user_id)
    assert result["request_id"] == accepted["request_id"]
    assert result["outcome"]["status"] == "interrupted"
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert detail.archived and detail.context_state.sample.used_tokens == 250
    assert detail.context_state.sample.source.request_id == accepted["request_id"]


async def test_claude_last_root_input_and_exact_window_survive_restart(claude_runtime):
    manager, database, session, _, events = claude_runtime
    receipt = await manager.start_message(session.id, "Preserve original history")
    assert (await receipt.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state is not None
    assert state.validity == "valid" and state.activity is None
    sample = state.sample
    assert sample.used_tokens == 666 and sample.capacity_tokens == 200000
    assert sample.basis == "claude_last_request_input" and sample.actual_model == MODEL
    assert sample.native_session_id == SID and sample.source.request_id == receipt.request_id
    assert [e["context_state"] for e in events if e["type"] == "context_state"][-1] == state.model_dump()
    await manager.stop_all_held_processes()
    restarted = SessionManager()
    try:
        await restarted.initialize(database, previous_writers_retired=True)
        restored = (await get_session(restarted, session.id, user_id=session.user_id)).context_state
        assert restored == state
    finally:
        await restarted.close_session_owners()


async def test_held_opening_ignores_old_results_children_replays_and_duplicate_blocks(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    first = await manager.start_message(session.id, "Original input")
    await first.wait()
    await wait_dispatch_idle(manager, session.id)
    previous = (await inputs(tmp_path, 1))[0]
    receipt, current = await hold_next(manager, session, tmp_path, 2)
    inject(tmp_path, terminal(previous["input"]["uuid"]),
        assistant(current, "earlier", 100), assistant(current, "latest", 200),
        assistant(current, "latest", 200), assistant(current, "earlier", 999),
        assistant(current, "child", 999, parent_tool_use_id="child-tool"),
        assistant(current, "foreign", 999, session_id="another-session"),
        assistant(previous["input"]["uuid"], "old", 999),
        assistant(None, "unbound", 999), terminal(current))
    assert (await receipt.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.sample.used_tokens == 250 and state.sample.capacity_tokens == 200000
    assert state.sample.source.request_id == receipt.request_id
    assert state.sample.source.native_sample_id == "latest"
    assert state.sample.source.observation_seq == 2
    records = await inputs(tmp_path, 2)
    assert records[0]["pid"] == records[1]["pid"] == previous["pid"]


@pytest.mark.parametrize("models", [
    {}, {"sonnet": {"contextWindow": 200000}}, {MODEL: {"contextWindow": 0}},
    {MODEL: {"contextWindow": True}}, {MODEL: {"contextWindow": "200000"}},
])
async def test_unknown_or_invalid_window_keeps_observed_input(claude_runtime, tmp_path, models):
    manager, _, session, _, _ = claude_runtime
    receipt, current = await hold_next(manager, session, tmp_path, 1)
    inject(tmp_path, assistant(current), terminal(current, models))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.sample.used_tokens == 250 and state.sample.capacity_tokens is None


async def test_auto_boundary_does_not_pair_precompact_input_with_late_window(claude_runtime, tmp_path):
    manager, _, session, _, events = claude_runtime
    await (await manager.start_message(session.id, "Before automatic compaction")).wait()
    await wait_dispatch_idle(manager, session.id)
    receipt, current = await hold_next(manager, session, tmp_path, 2)
    inject(tmp_path, {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID},
        assistant(current, "summary-request", 999),
        {"type": "system", "subtype": "compact_boundary", "session_id": SID, "uuid": "auto-boundary",
         "compact_metadata": {"trigger": "auto", "pre_tokens": 90000, "post_tokens": 123}},
        terminal(current))
    await receipt.wait()
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.invalidation.reason == "compacted"
    assert state.activity is None and state.sample.used_tokens == 666
    assert any(e["type"] == "context_state" and e["context_state"]["activity"] == "compacting" for e in events)
    assert not any(item["operation"] == "compact" for item in manager.dispatch_snapshot(session.id)["items"])
    (tmp_path / "claude-plan.json").unlink()
    await (await manager.start_message(session.id, "Next normal sample")).wait()
    refreshed = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert refreshed.validity == "valid" and refreshed.sample.used_tokens == 666


@pytest.mark.parametrize("ending", ["terminal", "eof", "stop"])
async def test_unconfirmed_automatic_compaction_finishes_pending_not_compacting(claude_runtime, tmp_path, ending):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Before automatic compaction")).wait()
    await wait_dispatch_idle(manager, session.id)
    receipt, current = await hold_next(manager, session, tmp_path, 2)
    inject(tmp_path, {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID},
        {"type": "system", "subtype": "status", "status": None, "session_id": SID})
    await context_when(manager, session, lambda state: state.activity == "compacting")
    if ending == "stop":
        assert await manager.interrupt(session.id)
    else:
        inject(tmp_path, terminal(current) if ending == "terminal" else {"__exit": True})
    result = await asyncio.wait_for(receipt.wait(), 5)
    if ending == "stop":
        assert result.outcome.status == "interrupted"
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.invalidation.reason == "operation_unconfirmed"
    assert state.activity is None and state.sample.used_tokens == 666


async def test_retirement_observations_preserve_normal_claude_continuation(claude_runtime, tmp_path, monkeypatch):
    manager, _, session, starts, _ = claude_runtime
    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"fail_once": True}))
    receipt = await manager.start_message(session.id, "Original request with possible effects")
    result = await asyncio.wait_for(receipt.wait(), 5)
    assert result.outcome.status == "success"
    assert result.outcome.recovery["transient"] == 1
    records = await inputs(tmp_path, 2)
    assert records[1]["input"]["message"]["content"].startswith("Continue the user's")
    assert len(starts) == 2 and starts[1].resume_id == SID


async def test_post_boundary_sample_survives_duplicate_boundary_and_old_sample(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    receipt, current = await hold_next(manager, session, tmp_path, 1)
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": SID, "uuid": "auto-boundary",
                "compact_metadata": {"trigger": "auto", "pre_tokens": 90000, "post_tokens": 123}}
    inject(tmp_path, assistant(current, "pre", 90000),
        {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID},
        assistant(current, "summary", 999), boundary,
        assistant(current, "pre", 90000), assistant(current, "summary", 999),
        assistant(current, "post", 300), boundary, terminal(current))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "valid" and state.activity is None
    assert state.sample.used_tokens == 350 and state.sample.capacity_tokens == 200000
    assert state.sample.source.native_sample_id == "post"


@pytest.mark.parametrize("invalid", [-1, True, 1.25, "200", None])
async def test_invalid_native_input_does_not_replace_last_sample(claude_runtime, tmp_path, invalid):
    manager, _, session, _, _ = claude_runtime
    receipt, current = await hold_next(manager, session, tmp_path, 1)
    inject(tmp_path, assistant(current, "valid", 200), assistant(current, "bad", invalid), terminal(current))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.sample.used_tokens == 250 and state.sample.source.native_sample_id == "valid"


async def test_claude_replaces_host_and_precisely_resumes_for_next_sample(claude_runtime, tmp_path):
    manager, _, session, starts, _ = claude_runtime
    await (await manager.start_message(session.id, "Keep original history")).wait()
    await wait_dispatch_idle(manager, session.id)
    assert await manager.stop_all_held_processes() == 1
    receipt = await manager.start_message(session.id, "Continue exact native history")
    assert (await receipt.wait()).outcome.status == "success"
    records = await inputs(tmp_path, 2)
    assert records[0]["pid"] != records[1]["pid"]
    arguments = records[1]["argv"]
    assert arguments[arguments.index("--resume") + 1] == SID
    assert len(starts) == 2 and starts[1].resume_id == SID
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "valid" and state.sample.source.request_id == receipt.request_id


async def test_held_old_input_compaction_frames_do_not_invalidate_current_sample(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "First request")).wait()
    await wait_dispatch_idle(manager, session.id)
    old = (await inputs(tmp_path, 1))[0]["input"]["uuid"]
    receipt, current = await hold_next(manager, session, tmp_path, 2)
    inject(tmp_path, assistant(current, "current", 300),
        {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID,
         "user_message_uuids": [old]},
        {"type": "system", "subtype": "compact_boundary", "session_id": SID, "uuid": "old-boundary",
         "user_message_uuid": old, "compact_metadata": {"trigger": "auto", "pre_tokens": 90000}},
        terminal(current))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "valid" and state.sample.used_tokens == 350


async def test_notification_terminal_does_not_close_active_compaction_observation(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    await (await manager.start_message(session.id, "Before background notification")).wait()
    await wait_dispatch_idle(manager, session.id)
    receipt, current = await hold_next(manager, session, tmp_path, 2)
    inject(tmp_path,
        {"type": "system", "subtype": "status", "status": "compacting", "session_id": SID},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": SID,
         "origin": {"kind": "task-notification", "producer": "session-task"},
         "result": "Earlier background task finished", "modelUsage": {MODEL: {"contextWindow": 1000000}}},
        {"type": "system", "subtype": "compact_boundary", "session_id": SID, "uuid": "actual-boundary",
         "compact_metadata": {"trigger": "auto", "pre_tokens": 90000}},
        assistant(current, "actual-post", 300), terminal(current))
    await receipt.wait()
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "valid" and state.activity is None
    assert state.sample.used_tokens == 350 and state.sample.capacity_tokens == 200000
    assert state.sample.source.native_sample_id == "actual-post"


@pytest.mark.parametrize("opening", ["same_attempt", "held", "cold"])
@pytest.mark.parametrize("usage", [None, {"input_tokens": -1}, "missing_id"])
async def test_model_change_without_usage_cannot_reuse_old_model_window(claude_runtime, tmp_path, opening, usage):
    manager, _, session, _, _ = claude_runtime
    if opening != "same_attempt":
        await (await manager.start_message(session.id, "Observe model A")).wait()
        await wait_dispatch_idle(manager, session.id)
        if opening == "cold":
            await manager.stop_all_held_processes()
    count = 1 if opening == "same_attempt" else 2
    receipt, current = await hold_next(manager, session, tmp_path, count)
    if opening == "same_attempt":
        inject(tmp_path, assistant(current, "model-a", 200))
    changed = assistant(current, "model-b", model="actual-model-b")
    changed["message"]["usage"] = None if usage == "missing_id" else usage
    if usage == "missing_id":
        changed["message"].pop("id")
    inject(tmp_path, changed, terminal(current, {
        MODEL: {"contextWindow": 200000}, "actual-model-b": {"contextWindow": 1000000}}))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.invalidation.reason == "native_context_changed"
    assert state.sample.actual_model == MODEL


async def test_refined_new_model_sample_recovers_after_missing_usage(claude_runtime, tmp_path):
    manager, _, session, _, _ = claude_runtime
    receipt, current = await hold_next(manager, session, tmp_path, 1)
    changed = assistant(current, "new-model", model="actual-model-b")
    changed["message"]["usage"] = None
    inject(tmp_path, assistant(current, "old-model", 200), changed)
    await context_when(manager, session, lambda state: state.validity == "stale")
    inject(tmp_path, assistant(current, "new-model", 300, model="actual-model-b"),
        terminal(current, {MODEL: {"contextWindow": 200000}, "actual-model-b": {"contextWindow": 400000}}))
    await receipt.wait()
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "valid" and state.sample.actual_model == "actual-model-b"
    assert state.sample.used_tokens == 350 and state.sample.capacity_tokens == 400000

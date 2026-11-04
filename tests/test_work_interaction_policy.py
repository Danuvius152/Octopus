"""Original input policy, inherited returns and native human wait boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.config import settings
from tests.native_app_server import (
    inject,
    question_request,
    start_output,
    terminal,
    until,
    wire_frames,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle
from tests.test_research_session_return import returning_research as returning_research


async def test_human_native_blocking_keeps_original_call_past_question_and_idle_limits(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _mode, starts, _events = native_runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .03)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    await start_output(native_runtime)
    run = session._run.backend
    inject(tmp_path, question_request("human-thinking"))
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .07)
    from server.sessions import owner_questions
    clock = owner_questions.time.monotonic
    monkeypatch.setattr(owner_questions, "time", SimpleNamespace(monotonic=lambda: clock() + 3600))
    await asyncio.sleep(.15)
    assert manager.get_pending_questions(session.id) == (original,)
    assert original.deadline is None and original.timer is None
    assert original.interaction_policy == "human" and original.wait_mode == "blocking"
    assert run.is_alive() and len(starts) == 1
    assert await manager.answer_question(session.id, original.question_id, [{"text": "Decided after thinking"}])
    await until(lambda: any(frame.get("id") == "human-thinking" for frame in wire_frames(tmp_path)))
    reply, = [frame for frame in wire_frames(tmp_path) if frame.get("id") == "human-thinking"]
    assert reply["result"]["answers"]["q"]["answers"] == ["Decided after thinking"]
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)
    saved, = [row for row in await database.load_messages(session.id) if row["type"] == "question_request"]
    assert saved["tool_input"]["interaction_policy"] == "human"
    assert saved["tool_input"]["wait_mode"] == "blocking"


@pytest.mark.parametrize("ending", ["stop", "execution-end", "hard-cap"])
async def test_human_blocking_wait_preserves_original_execution_endings(native_runtime, tmp_path, monkeypatch, ending):
    manager, database, session, _mode, starts, _events = native_runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .03)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    receipt = await start_output(native_runtime)
    inject(tmp_path, question_request(f"ending-{ending}"))
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    if ending == "stop":
        await manager.interrupt(session.id)
    elif ending == "execution-end":
        inject(tmp_path, terminal())
    else:
        # Arm only after native preparation. The original whole-turn start,
        # rather than time since the answer, remains the hard-cap origin.
        monkeypatch.setattr(settings, "turn_max_seconds", .03)
        manager._session_owners[id(session)].wake()
    await wait_dispatch_idle(manager, session.id)
    assert not manager.get_pending_questions(session.id)
    assert not await manager.answer_question(session.id, original.question_id, [{"text": "late"}])
    result = await database.read_session_result(session.id, receipt["request_id"])
    if ending == "hard-cap":
        assert result.outcome.code == "turn_timeout"
        assert "maximum duration" in result.outcome.message.lower() and "user timeout" not in result.outcome.message.lower()
        assert len(starts) == 1
    elif ending == "stop":
        assert result.outcome.status == "interrupted"


async def test_same_session_admits_schedule_then_human_with_distinct_persisted_policies(native_runtime, tmp_path):
    from server.knowledge import decode_context

    manager, database, session, mode, _starts, _events = native_runtime
    mode[0] = "control_output"
    scheduled = await manager.start_message(session.id, "scheduled work", source="schedule")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    assert session._run.knowledge_context.interaction_policy == "unattended"
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)
    await start_output(native_runtime)
    assert session._run.knowledge_context.interaction_policy == "human"
    row = await database.get_client_submission(session.id, scheduled.request_id)
    assert decode_context(row["payload"]["knowledge"]).interaction_policy == "unattended"
    row = await database.get_client_submission(session.id, "output")
    assert decode_context(row["payload"]["knowledge"]).interaction_policy == "human"


async def test_actual_async_returns_inherit_the_original_human_policy(returning_research, tmp_path, monkeypatch):
    from server.agent_manager import AgentManager
    from server.bg_tasks import BgTaskManager
    from server.delegations import DelegationManager
    from server.knowledge import decode_context

    database, manager, research, started, release, processed = returning_research
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Original human work")
    admitted = manager.start_message
    policies = []
    async def observe(session_id, prompt, **kwargs):
        receipt = await admitted(session_id, prompt, **kwargs)
        client_id = f"{kwargs['source']}:{kwargs['delivery_id']}" if kwargs.get("delivery_id") else receipt.request_id
        row = await database.get_client_submission(session_id, client_id)
        policies.append((kwargs.get("source", "human"), decode_context(row["payload"]["knowledge"]).interaction_policy))
        return receipt
    monkeypatch.setattr(manager, "start_message", observe)
    background = BgTaskManager()
    background.bind(database, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    delegations = DelegationManager()
    delegations.bind(manager, database)
    try:
        bg = await background.start_task(session_id=session.id, command="printf 'human background'", working_dir=str(tmp_path))
        await asyncio.wait_for(asyncio.shield(background._running[bg.id].task), 3)
        job = await research.start(session.id, "Human research")
        await asyncio.wait_for(started.wait(), 2)
        release.set()
        await asyncio.wait_for(research._tasks[job["id"]], 3)
        await AgentManager(database).create_agent(name="Policy child", user_id=session.user_id)
        rec = await delegations.start_delegation(parent_session_id=session.id, agent_name="Policy child", request="Human child work")
        async with asyncio.timeout(3):
            while not any(source == "delegation" for source, _policy in policies):
                await asyncio.sleep(.01)
        await delegations.confirm_recording()
        assert ("bg", "human") in policies and ("research", "human") in policies
        assert all(policy == "human" for _source, policy in policies)
        assert rec.owned_context.execution.interaction_policy == "human"
    finally:
        await background.shutdown()
        await manager.drain_turns(timeout=3)
        await delegations.confirm_recording()
        delegations.shutdown()


@pytest.mark.parametrize("source", ["schedule", "wiki"])
async def test_unattended_original_work_returns_missing_decision_without_a_human_form(native_runtime, tmp_path, source):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_output"
    await manager.start_message(session.id, "Complete the authorized independent part; destination still needs a decision", source=source)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, question_request("unattended-decision"))
    await until(lambda: any(frame.get("id") == "unattended-decision" for frame in wire_frames(tmp_path)))
    reply, = [frame for frame in wire_frames(tmp_path) if frame.get("id") == "unattended-decision"]
    assert reply["result"] == {"answers": {}}
    assert not manager.get_pending_questions(session.id)
    assert not [event for event in events if event["type"] == "question_answer"]
    assert "无人值守" in starts[0].turn_instructions
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    closed, = [row for row in rows if row["type"] == "question_closed"]
    assert closed["tool_input"]["reason"] == "needs_input"
    assert closed["tool_input"]["question_resolution"]["answers"] is None
    assert "decision" in closed["content"].lower()
    assert len(starts) == 1


async def test_legacy_return_keeps_unknown_policy_and_existing_native_expiry(native_runtime, tmp_path, monkeypatch):
    import json

    from server.knowledge import decode_context, encode

    manager, _database, session, mode, _starts, _events = native_runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .03)
    mode[0] = "control_output"
    await manager.start_message(session.id, "Legacy return", source="bg")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    saved = json.loads(encode(session._run.knowledge_context))
    saved["value"].pop("interaction_policy")
    assert decode_context(json.dumps(saved)).interaction_policy is None
    inject(tmp_path, question_request("legacy-wait"))
    await until(lambda: any(frame.get("id") == "legacy-wait" for frame in wire_frames(tmp_path)))
    reply, = [frame for frame in wire_frames(tmp_path) if frame.get("id") == "legacy-wait"]
    assert reply["result"] == {"answers": {}}

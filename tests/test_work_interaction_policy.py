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


async def test_human_native_optional_uses_creation_deadline_and_system_expiry_once(native_runtime, tmp_path, monkeypatch):
    from datetime import datetime

    from server.sessions import owner_questions

    manager, database, session, _mode, _starts, _events = native_runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 1800)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    await start_output(native_runtime)
    request = question_request("optional-expiry")
    request["params"]["isBlocking"] = False
    inject(tmp_path, request)
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    saved, = [row for row in await database.load_messages(session.id) if row["type"] == "question_request"]
    created = datetime.fromisoformat(saved["created_at"])
    assert 119 <= (datetime.fromisoformat(original.deadline) - created).total_seconds() <= 121
    clock = owner_questions.time.monotonic
    monkeypatch.setattr(owner_questions, "time", SimpleNamespace(monotonic=lambda: clock() + 121))
    assert not await manager.answer_question(session.id, original.question_id, [{"text": "too late"}])
    assert not await manager.answer_question(session.id, original.question_id, [{"text": "duplicate"}])
    await until(lambda: any(frame.get("id") == "optional-expiry" for frame in wire_frames(tmp_path)))
    response, = [frame for frame in wire_frames(tmp_path) if frame.get("id") == "optional-expiry"]
    assert response["result"]["answers"] == {}
    resolutions = [row for row in await database.load_messages(session.id) if row["type"] == "question_answer"]
    assert len(resolutions) == 1
    fact = resolutions[0]["tool_input"]["question_resolution"]
    assert fact["disposition"] == "expired" and fact["source"] == {"kind": "system"} and fact["answers"] == []
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)


async def test_optional_snooze_survives_original_deadline_and_is_published_once(native_runtime, tmp_path, monkeypatch):
    from server.question_resolution import AnswerSource
    from server.sessions import owner_questions
    from server.sessions.commands import process_command

    manager, database, session, _mode, _starts, events = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    await start_output(native_runtime)
    request = question_request("snooze")
    request["params"]["isBlocking"] = False
    inject(tmp_path, request)
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    command = {"type": "snooze_question", "session_id": session.id, "client_command_id": "first-focus",
               "question_id": original.question_id, "request_id": original.request_id, "attempt_id": original.attempt_id,
               "generation": manager.dispatch_snapshot(session.id)["generation"]}
    source = AnswerSource("account", user_id=session.user_id)
    assert (await process_command(manager, command, session.user_id, source=source))["accepted"]
    assert (await process_command(manager, {**command, "client_command_id": "other-page"}, session.user_id, source=source))["accepted"]
    clock = owner_questions.time.monotonic
    monkeypatch.setattr(owner_questions, "time", SimpleNamespace(monotonic=lambda: clock() + 121))
    assert manager.get_pending_questions(session.id) == (original,)
    assert original.deadline is None and original.expires_at is None
    await until(lambda: any(event["type"] == "question_snoozed" for event in events))
    published, = [event for event in events if event["type"] == "question_snoozed"]
    assert published["snoozed"] is True and published["deadline"] is None
    assert published["request_id"] == original.request_id and published["attempt_id"] == original.attempt_id
    assert await manager.answer_question(session.id, original.question_id, [{"text": "Still answerable"}])
    await until(lambda: any(frame.get("id") == "snooze" for frame in wire_frames(tmp_path)))
    saved, = [row for row in await database.load_messages(session.id) if row["type"] == "question_snoozed"]
    assert saved["tool_input"]["snoozed"] is True
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)


@pytest.mark.parametrize("rejected", ["expired", "wrong-attempt", "machine-source"])
async def test_optional_snooze_cannot_reverse_expiry_or_change_its_original_authority(native_runtime, tmp_path, monkeypatch, rejected):
    from server.question_resolution import AnswerSource
    from server.sessions import owner_questions
    from server.sessions.commands import process_command

    manager, database, session, _mode, _starts, events = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    await start_output(native_runtime)
    request = question_request("snooze-refused")
    request["params"]["isBlocking"] = False
    inject(tmp_path, request)
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    if rejected == "expired":
        clock = owner_questions.time.monotonic
        monkeypatch.setattr(owner_questions, "time", SimpleNamespace(monotonic=lambda: clock() + 121))
    response = await process_command(manager, {
        "type": "snooze_question", "session_id": session.id, "client_command_id": "refused-snooze",
        "question_id": original.question_id, "request_id": original.request_id,
        "attempt_id": "another-attempt" if rejected == "wrong-attempt" else original.attempt_id,
        "generation": manager.dispatch_snapshot(session.id)["generation"],
    }, session.user_id, source=AnswerSource("agent" if rejected == "machine-source" else "account", user_id=session.user_id))
    assert not response["accepted"]
    assert not [event for event in events if event["type"] == "question_snoozed"]
    if rejected == "expired":
        result = await database.get_question_result(session.id, original.question_id)
        assert result["resolution"]["disposition"] == "expired" and result["resolution"]["answers"] == []
    else:
        assert manager.get_pending_questions(session.id) == (original,)
        assert original.deadline is not None
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)


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
@pytest.mark.parametrize("waiting", ["blocking", "snoozed"])
async def test_human_blocking_wait_preserves_original_execution_endings(native_runtime, tmp_path, monkeypatch, ending, waiting):
    manager, database, session, _mode, starts, _events = native_runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .03)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 3600 if ending == "hard-cap" else 0)
    receipt = await start_output(native_runtime)
    request = question_request(f"ending-{ending}")
    request["params"]["isBlocking"] = waiting == "blocking"
    inject(tmp_path, request)
    await until(lambda: manager.get_pending_questions(session.id))
    original, = manager.get_pending_questions(session.id)
    if waiting == "snoozed":
        from server.question_resolution import AnswerSource
        from server.sessions.commands import process_command
        assert (await process_command(manager, {
            "type": "snooze_question", "session_id": session.id, "client_command_id": "snooze-ending",
            "question_id": original.question_id, "request_id": original.request_id, "attempt_id": original.attempt_id,
        }, session.user_id, source=AnswerSource("account", user_id=session.user_id)))["accepted"]
    if ending == "stop":
        await manager.interrupt(session.id)
    elif ending == "execution-end":
        inject(tmp_path, terminal())
    else:
        from server.sessions import owner_runtime
        from tests.native_app_server import item
        # Advance the original configured whole-turn budget. A real native
        # observation wakes/rearms its deadline; changing settings after the
        # owner became idle would instead test unsupported hot reconfiguration.
        clock = owner_runtime.time.monotonic
        monkeypatch.setattr(owner_runtime, "time", SimpleNamespace(monotonic=lambda: clock() + 3601))
        inject(tmp_path, item("exact-native-thread", "turn-1", "cap-observation", "Output before the overall limit"))
    await wait_dispatch_idle(manager, session.id)
    assert not manager.get_pending_questions(session.id)
    assert not await manager.answer_question(session.id, original.question_id, [{"text": "late"}])
    closed, = [row for row in await database.load_messages(session.id) if row["type"] == "question_closed"]
    assert closed["tool_input"]["reason"] == {"stop": "interrupted", "execution-end": "execution_completed", "hard-cap": "hard_cap"}[ending]
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

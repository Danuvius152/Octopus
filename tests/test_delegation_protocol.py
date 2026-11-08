"""Actual delegation inputs and selected replies through a controlled native peer."""

import asyncio
import sys

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationManager
from server.harness import get_harness
from tests.harness_profiles import process_profile, session_turn_argv
from tests.native_app_server import ROOT, inject, item, until, wire_frames
from tests.native_app_server import native_runtime as native_runtime


@pytest.fixture
async def protocol_runtime(native_runtime):
    manager, database, parent, mode, _, _ = native_runtime
    await manager.interrupt(parent.id)
    await AgentManager(database).create_agent(name="Protocol reviewer", backend="codex", user_id=parent.user_id)
    delegations = DelegationManager()
    delegations.bind(manager, database)
    mode[0] = "control_output"
    try:
        yield manager, delegations, parent
    finally:
        delegations.shutdown()


@pytest.mark.parametrize("empty_final", [False, True])
async def test_initial_and_follow_up_explain_the_round_and_return_only_selected_final(protocol_runtime, tmp_path, empty_final):
    manager, delegations, parent = protocol_runtime
    (tmp_path / "source.txt").write_text("Relevant evidence")
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Protocol reviewer", request="Review the evidence",
        files=["source.txt", "missing.txt"],
    )
    records = []
    try:
        for index in (1, 2):
            record = first if index == 1 else await delegations.follow_up_delegation(
                parent_session_id=parent.id, delegation_id=first.delegation_id, request="Review the follow-up evidence",
            )
            records.append(record)
            child = manager.get_session(record.delegation_id)
            await until(lambda child=child: child._run.backend is not None and child._run.backend.active_turn is not None)
            starts = [frame["params"] for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]
            sent = "\n".join(part["text"] for part in starts[-1]["input"] if part["type"] == "text")
            turn = "turn-" + str(index)
            commentary = item(ROOT, turn, "progress", "Working notes that must stay out of the reply")
            commentary["params"]["item"]["phase"] = "commentary"
            inject(tmp_path, commentary,
                item(ROOT, turn, "answer", "" if empty_final else f"Evidence and result for round {index}"),
                {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": turn, "status": "completed"}}})
            await until(lambda record=record: record.state == "completed")
            await asyncio.wait_for(delegations.confirm_recording(), 3)
            replies = manager.dispatch_snapshot(parent.id)["items"]
            assert len(replies) == index
            expected = "(child session ended without producing any text)" if empty_final else f"Evidence and result for round {index}"
            assert replies[-1]["summary"] == f"[agent-reply:Protocol reviewer delegation={record.delegation_id}]\n{expected}"
            assert record._terminal_disposition == "accepted"
            assert "single-turn delegation" not in sent
            assert "everything you have written" not in sent
            assert "original work completes" in sent
            assert "owned descendants" in sent
            assert "selected final answer" in sent
            assert "without a final-answer reference" in sent
            assert "human follow-ups do not resend" in sent
            assert "self-contained" in sent
            if index == 1:
                assert "Review the evidence" in sent
                assert str(tmp_path / "source.txt") in sent
                assert str(tmp_path / "missing.txt") + "  (not found)" in sent
            else:
                assert "Review the follow-up evidence" in sent
        assert records[0].work_id != records[1].work_id
        assert records[0].delegation_id == records[1].delegation_id
    finally:
        await manager.interrupt(first.delegation_id)
        await manager.await_session_execution(first.delegation_id, timeout=5)


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_reply_without_final_reference_keeps_associated_fragment_policy(protocol_runtime, monkeypatch, backend):
    manager, delegations, parent = protocol_runtime
    await AgentManager(manager.db).create_agent(name="Legacy reviewer", backend=backend, user_id=parent.user_id)
    if backend == "codex":
        frames = [
            {"type": "thread.started", "thread_id": "legacy-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "  first "}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "second\n"}},
            {"type": "turn.completed", "usage": {}},
        ]
    else:
        frames = [
            {"type": "system", "subtype": "init", "session_id": "legacy-thread"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "  first "}, {"type": "text", "text": "second\n"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "session_id": "legacy-thread"},
        ]
    code = "import json\n" + "\n".join(f"print(json.dumps({frame!r}), flush=True)" for frame in frames)

    def argv(context):
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness(backend)
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile,
        build_turn_argv=session_turn_argv(harness.profile, argv)))
    record = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Legacy reviewer", request="Return both evidence fragments",
    )
    await until(lambda: record.state == "completed")
    await asyncio.wait_for(delegations.confirm_recording(), 3)
    reply, = manager.dispatch_snapshot(parent.id)["items"]
    assert record._terminal_disposition == "accepted"
    assert reply["summary"] == f"[agent-reply:Legacy reviewer delegation={record.delegation_id}]\nfirst second"
    messages = await manager.db.load_messages(record.delegation_id)
    terminal, = [message for message in messages if message["type"] == "result"]
    saved = await manager.db.read_session_result(record.delegation_id, terminal["content"]["request_id"])
    assert saved.outcome.final_answer is None
    assert saved.fragments == ("  first ", "second\n")


async def test_compact_completion_does_not_send_another_delegation_reply(protocol_runtime, native_runtime, tmp_path):
    manager, delegations, parent = protocol_runtime
    record = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Protocol reviewer", request="Finish the original round",
    )
    child = manager.get_session(record.delegation_id)
    await until(lambda: child._run.backend is not None and child._run.backend.active_turn is not None)
    inject(tmp_path, item(ROOT, "turn-1", "answer", "Original round result"),
        {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "turn-1", "status": "completed"}}})
    await until(lambda: record.state == "completed")
    await asyncio.wait_for(delegations.confirm_recording(), 3)
    replies = manager.dispatch_snapshot(parent.id)["items"]
    assert len(replies) == 1 and "Original round result" in replies[0]["summary"]
    native_runtime.mode[0] = "compact_ack_first"
    receipt = await manager.start_compact(child.id, client_request_id="compact-original-history")
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.operation == "compact" and completion.outcome.status == "success"
    saved = await manager.db.read_session_result(child.id, receipt.request_id)
    assert saved.operation == "compact" and saved.outcome.final_answer is None
    await asyncio.wait_for(delegations.confirm_recording(), 3)
    assert manager.dispatch_snapshot(parent.id)["items"] == replies
    assert delegations.get_delegation(child.id) is record

"""Busy compaction through the original Owner and both native stdio peers."""

import asyncio
import json
import os
import shlex
import sys

import pytest

from server import fork_helpers
from server.bg_tasks import BgTaskManager
from server.config import settings
from server.harness import HarnessRun
from server.question_resolution import AnswerSource
from server.routers.sessions import get_session, get_submission_status, submit_input
from server.sessions import SessionManager
from server.sessions.commands import process_command
from server.sessions.owner_runtime import CancelRequest
from server.submission_models import ClientSubmissionRequest
from tests._fixtures.jsonl_frames import read_frames
from tests.claude_runtime import SID
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inject as claude_inject
from tests.native_app_server import ROOT, until
from tests.native_app_server import inject as codex_inject
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


class QueueRuntime:
    """Control external producer frames while keeping Owner/Harness/DB real."""

    def __init__(self, runtime, root):
        self.manager, self.database, self.session = runtime[:3]
        self.root = root
        self.claude = self.session.backend == "claude-code"
        self.mode = None if self.claude else runtime[3]
        self.starts, self.events = runtime[-2:]

    def hold(self):
        if self.claude:
            (self.root / "claude-plan.json").write_text(json.dumps({"hold": True}))
        else:
            self.mode[0] = "control_output"

    def ready(self):
        if self.claude:
            (self.root / "claude-plan.json").write_text("{}")
        else:
            self.mode[0] = "compact_ack_first"

    def hold_compact(self):
        if self.claude:
            (self.root / "claude-plan.json").write_text(json.dumps({"compact_mode": "hold"}))
        else:
            self.mode[0] = "compact_control"

    async def compacting(self):
        async with asyncio.timeout(3):
            while True:
                state = (await get_session(self.manager, self.session.id, user_id=self.session.user_id)).context_state
                if state is not None and state.activity == "compacting":
                    return
                await asyncio.sleep(.01)

    def finish_compact(self):
        if self.claude:
            claude_inject(self.root, {"type": "system", "subtype": "compact_boundary", "session_id": SID,
                "uuid": "manual-queued-boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 90000}},
                {"type": "result", "session_id": SID, "user_message_uuid": self.records()[-1]["input"]["uuid"],
                    "subtype": "success", "is_error": False, "local_command": "compact", "result": ""})
        else:
            codex_inject(self.root, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "compact-turn",
                "item": {"id": "compact-item", "type": "contextCompaction"}}},
                {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "compact-turn", "status": "completed"}}})

    def records(self):
        return read_frames(self.root / ("claude-inputs.jsonl" if self.claude else "native-business.jsonl"))

    async def input(self, count):
        await until(lambda: len(self.records()) >= count)
        return self.records()[count - 1]

    async def message_ready(self, record, ordinal):
        if self.claude:
            claude_inject(self.root, {"type": "assistant", "session_id": SID,
                "user_message_uuid": record["input"]["uuid"], "message": {"id": "ready-" + str(ordinal),
                    "model": "deepseek-flash", "content": [{"type": "text", "text": "Work in progress"}]}})
        else:
            codex_inject(self.root, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": f"turn-{ordinal}",
                "item": {"type": "agentMessage", "id": "ready-" + str(ordinal), "phase": "commentary", "text": "Work in progress"}}})
        await until(lambda: any(e["type"] == "assistant_text" and e.get("content") == "Work in progress" for e in self.events))

    def finish_message(self, record, ordinal):
        if self.claude:
            claude_inject(self.root, {"type": "result", "session_id": SID,
                "user_message_uuid": record["input"]["uuid"], "subtype": "success", "is_error": False})
        else:
            turn = f"turn-{ordinal}"
            codex_inject(self.root, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": turn,
                "item": {"type": "agentMessage", "id": "answer-" + turn, "phase": "final_answer", "text": "Work finished"}}},
                {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": turn, "status": "completed"}}})


@pytest.fixture(params=["native_runtime", "claude_runtime"], ids=["codex", "claude"])
def queued_runtime(request, tmp_path):
    return QueueRuntime(request.getfixturevalue(request.param), tmp_path)


async def test_busy_compact_precedes_ordinary_queue_without_reordering_it(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "A: current work", wiki_mode=None)
    record = await runtime.input(2)
    before = await manager.start_message(session.id, "B: ordinary return", source="bg", wiki_mode=None)
    compact = await submit_input(manager, session.id, ClientSubmissionRequest.model_validate({
        "client_request_id": "queued-compact", "operation": {"kind": "compact"},
    }), user_id=session.user_id)
    assert compact["accepted"]
    after = await manager.start_message(session.id, "D: later human", steerable=True, wiki_mode=None)
    assert after.disposition != "steered"
    snapshot = manager.dispatch_snapshot(session.id)
    assert [item["request_id"] for item in snapshot["items"]] == [before.request_id, compact["request_id"], after.request_id]
    assert active.completion is None
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    terminal_order = [event["request_id"] for event in runtime.events
        if event["type"] == "result" and event.get("terminal") and event.get("request_id") in {
            active.request_id, before.request_id, compact["request_id"], after.request_id}]
    assert terminal_order == [active.request_id, compact["request_id"], before.request_id, after.request_id]
    status = await get_submission_status(manager, session.id, "queued-compact", user_id=session.user_id)
    assert status["outcome"]["status"] == "success" and status["outcome"]["compact"]["effect"] == "applied"
    assert (await get_session(manager, session.id, user_id=session.user_id)).claude_session_id == (SID if runtime.claude else ROOT)


async def test_busy_compact_sample_result_and_original_history_survive_restart(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Establish original history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Work before compact", wiki_mode=None)
    record = await runtime.input(2)
    await runtime.message_ready(record, 2)
    if not runtime.claude:
        codex_inject(runtime.root, {"method": "thread/tokenUsage/updated", "params": {
            "threadId": ROOT, "turnId": "turn-2", "tokenUsage": {
                "total": {"totalTokens": 90000}, "last": {"totalTokens": 250}, "modelContextWindow": 1000}}})
    async with asyncio.timeout(3):
        while True:
            observed = (await get_session(manager, session.id, user_id=session.user_id)).context_state
            if observed is not None and observed.validity == "valid":
                break
            await asyncio.sleep(0)
    original = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    request = ClientSubmissionRequest.model_validate({
        "client_request_id": "complete-chain", "operation": {"kind": "compact"}})
    accepted = await submit_input(manager, session.id, request, user_id=session.user_id)
    assert accepted["accepted"] and active.completion is None
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    assert (await active.wait()).outcome.status == "success"
    completed = await get_submission_status(manager, session.id, "complete-chain", user_id=session.user_id)
    assert completed["outcome"]["status"] == "success"
    assert completed["outcome"]["compact"]["effect"] == "applied"
    post_compact = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert post_compact.activity is None
    if runtime.claude:
        assert post_compact.validity == "stale" and post_compact.sample == original.sample
    else:
        assert post_compact.validity == "valid" and post_compact.sample.used_tokens == 120
    # A lost browser receipt is recovered by its original identity, without another native operation.
    assert (await submit_input(manager, session.id, request, user_id=session.user_id))["request_id"] == accepted["request_id"]
    await manager.stop_all_held_processes()
    await manager.close_session_owners()
    restarted = SessionManager()
    try:
        await restarted.initialize(runtime.database, previous_writers_retired=True)
        restored = await get_session(restarted, session.id, user_id=session.user_id)
        assert restored.context_state == post_compact
        assert (await get_submission_status(restarted, session.id, "complete-chain", user_id=session.user_id))["outcome"] == completed["outcome"]
        assert [start.operation for start in runtime.starts].count("compact") == 1
        runtime.ready()
        followup = await restarted.start_message(session.id, "Continue the same native history", wiki_mode=None)
        assert (await followup.wait()).outcome.status == "success"
        await wait_dispatch_idle(restarted, session.id)
        detail = await get_session(restarted, session.id, user_id=session.user_id)
        assert detail.claude_session_id == (SID if runtime.claude else ROOT)
        assert runtime.starts[-1].resume_id == detail.claude_session_id
        assert any(message.content == "Native answer after compaction" for message in detail.messages)
        if runtime.claude:
            assert detail.context_state.validity == "valid" and detail.context_state.sample.used_tokens == 666
        else:
            assert detail.context_state.validity == "valid" and detail.context_state.sample.used_tokens == 120
    finally:
        await restarted.stop_all_held_processes()
        await restarted.close_session_owners()


async def test_cancel_pending_compact_preserves_current_work_and_other_queue(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Current work", wiki_mode=None)
    record = await runtime.input(2)
    before = await manager.start_message(session.id, "Ordinary return", source="bg", wiki_mode=None)
    compact = await manager.start_compact(session.id, client_request_id="cancel-pending")
    command = {"type": "cancel_compact", "client_command_id": "pending-cancel", "session_id": session.id,
        "generation": manager.dispatch_snapshot(session.id)["generation"], "request_id": compact.request_id}
    reply = await process_command(manager, command, session.user_id)
    assert reply["accepted"]
    assert await process_command(manager, command, session.user_id) == reply
    completion = await compact.wait()
    assert completion.state == "withdrawn" and completion.operation == "compact"
    assert active.completion is None
    assert [i["request_id"] for i in manager.dispatch_snapshot(session.id)["items"]] == [before.request_id]
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    assert (await active.wait()).outcome.status == "success"
    assert (await before.wait()).outcome.status == "success"
    assert not any(context.operation == "compact" for context in runtime.starts)


@pytest.mark.parametrize("entry", ["correlated", "mailbox"])
async def test_claimed_compact_rejects_pending_cancel_without_retiring_run(queued_runtime, entry):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold_compact()
    compact = await manager.start_compact(session.id, client_request_id="already-claimed")
    await runtime.compacting()
    if entry == "correlated":
        reply = await process_command(manager, {"type": "cancel_compact", "client_command_id": "claimed-cancel",
            "session_id": session.id, "generation": manager.dispatch_snapshot(session.id)["generation"],
            "request_id": compact.request_id}, session.user_id)
        assert not reply["accepted"] and reply["code"] == "already_started"
    else:
        cancelled, retirement = await manager._session_owner(session).call_runtime(
            CancelRequest(compact.request_id, pending_only=True))
        assert not cancelled and retirement is None
    assert compact.completion is None
    assert manager.dispatch_snapshot(session.id)["active_request_id"] == compact.request_id
    runtime.finish_compact()
    await wait_dispatch_idle(manager, session.id)
    result = await get_submission_status(manager, session.id, "already-claimed", user_id=session.user_id)
    assert result["outcome"]["status"] == "success" and result["outcome"]["compact"]["effect"] == "applied"


async def test_stop_withdraws_pending_compact_and_new_compact_does_not_resume_backlog(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    await manager.start_message(session.id, "Current work", wiki_mode=None)
    record = await runtime.input(2)
    await runtime.message_ready(record, 2)
    original_attempt = (await get_session(manager, session.id, user_id=session.user_id)).active_attempt
    before = await manager.start_message(session.id, "Old queued message", wiki_mode=None)
    compact = await manager.start_compact(session.id, client_request_id="withdrawn-pending")
    after = await manager.start_message(session.id, "Background return", source="bg", wiki_mode=None)
    await manager.interrupt(session.id)
    await manager.await_session_execution(session.id, timeout=3)
    assert (await compact.wait()).state == "withdrawn"
    paused = manager.dispatch_snapshot(session.id)
    assert paused["paused"] and [i["request_id"] for i in paused["items"]] == [before.request_id, after.request_id]
    runtime.hold_compact()
    correction = await manager.start_compact(session.id, client_request_id="new-explicit-compact")
    await runtime.compacting()
    stale_stop = await process_command(manager, {"type": "interrupt", "client_command_id": "old-stop",
        "session_id": session.id, **original_attempt, "generation": paused["generation"]}, session.user_id)
    assert not stale_stop["accepted"] and stale_stop["code"] == "stale_command"
    runtime.ready()
    runtime.finish_compact()
    assert (await correction.wait()).outcome.status == "success"
    await manager.await_session_execution(session.id, timeout=3)
    paused = manager.dispatch_snapshot(session.id)
    assert paused["paused"] and [i["request_id"] for i in paused["items"]] == [before.request_id, after.request_id]
    assert before.completion is None and after.completion is None
    assert sum(context.operation == "compact" for context in runtime.starts) == 1
    await manager.resume_dispatch(session.id)
    await wait_dispatch_idle(manager, session.id)
    assert (await before.wait()).outcome.status == (await after.wait()).outcome.status == "success"


async def test_stop_during_compact_claim_withdraws_without_resurrection(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    stopped = asyncio.Event()

    async def stop_unstarted(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session.id:
            await manager.interrupt(session.id)
            stopped.set()

    manager.on_broadcast("stop-unstarted", stop_unstarted)
    try:
        compact = await manager.start_compact(session.id, client_request_id="unstarted")
        await asyncio.wait_for(stopped.wait(), 3)
    finally:
        manager.remove_broadcast("stop-unstarted")
    await wait_dispatch_idle(manager, session.id)
    assert (await compact.wait()).state == "withdrawn"
    await manager.resume_dispatch(session.id)
    await wait_dispatch_idle(manager, session.id)
    assert not any(context.operation == "compact" for context in runtime.starts)


async def test_already_admitted_steer_keeps_delivery_before_queued_compact(queued_runtime, monkeypatch):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "A: current work", wiki_mode=None)
    record = await runtime.input(2)
    await runtime.message_ready(record, 2)
    anchor = fork_helpers.capture_git_anchor
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_anchor(*args):
        result = await anchor(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(fork_helpers, "capture_git_anchor", held_anchor)
    steer = await manager.start_message(session.id, "S: admitted steer", steerable=True,
        client_request_id="prior-steer", wiki_mode=None)
    assert steer.disposition == "steered"
    await asyncio.wait_for(entered.wait(), 3)
    try:
        before = await manager.start_message(session.id, "B: ordinary return", source="bg", wiki_mode=None)
        compact = await manager.start_compact(session.id, client_request_id="after-steer")
        after = await manager.start_message(session.id, "D: later human", steerable=True, wiki_mode=None)
        assert after.disposition == "accepted"
    finally:
        release.set()
    await until(lambda: len(runtime.records()) == 3 if runtime.claude else (runtime.root / "native-steers.jsonl").exists())
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    assert (await steer.wait()).outcome.status == "success"
    assert (await compact.wait()).outcome.status == "success"
    core = {active.request_id, compact.request_id, before.request_id, after.request_id}
    assert [e["request_id"] for e in runtime.events if e["type"] == "result" and e.get("terminal")
        and e.get("request_id") in core] == [active.request_id, compact.request_id, before.request_id, after.request_id]


async def test_pending_compact_does_not_answer_or_cancel_original_question(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Work needs a human answer", wiki_mode=None)
    record = await runtime.input(2)
    await runtime.message_ready(record, 2)
    question_id = await manager.create_pending_question(session.id, [{"question": "Continue?", "options": [{"label": "Yes"}]}])
    question, = manager.get_pending_questions(session.id)
    compact = await manager.start_compact(session.id, client_request_id="after-answer")
    later = await manager.start_message(session.id, "Later queued work", steerable=True, wiki_mode=None)
    assert later.disposition == "accepted"
    assert [q.question_id for q in manager.get_pending_questions(session.id)] == [question_id]
    assert not any(context.operation == "compact" for context in runtime.starts)
    reply = await process_command(manager, {"type": "answer_question", "client_command_id": "original-answer",
        "session_id": session.id, "generation": manager.dispatch_snapshot(session.id)["generation"],
        "request_id": question.request_id, "attempt_id": question.attempt_id, "question_id": question_id,
        "answers": [{"selected": ["Yes"]}]}, session.user_id, source=AnswerSource("account", user_id=session.user_id))
    assert reply["accepted"] and not manager.get_pending_questions(session.id)
    assert "Yes" in await manager.wait_for_question_answer(session.id, question_id)
    assert active.completion is None
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    assert (await compact.wait()).outcome.status == "success"


async def test_queued_compact_waits_for_original_process_retirement(queued_runtime, monkeypatch):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    await manager.start_message(session.id, "Work before retirement", wiki_mode=None)
    record = await runtime.input(2)
    compact = await manager.start_compact(session.id, client_request_id="after-retirement")
    entered, release = asyncio.Event(), asyncio.Event()
    original = HarnessRun._stop_process
    original_groups = []

    async def held_shutdown(run):
        if not entered.is_set():
            original_groups.append(run.process_group_id)
            entered.set()
            await release.wait()
        return await original(run)

    monkeypatch.setattr(HarnessRun, "_stop_process", held_shutdown)
    runtime.ready()
    runtime.finish_message(record, 2)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        os.killpg(original_groups[0], 0)
        assert compact.completion is None
        assert not any(context.operation == "compact" for context in runtime.starts)
    finally:
        release.set()
    await wait_dispatch_idle(manager, session.id)
    assert (await compact.wait()).outcome.status == "success"
    with pytest.raises(ProcessLookupError):
        os.killpg(original_groups[0], 0)


@pytest.mark.parametrize("damage", ["generation", "request", "kind", "account"])
async def test_pending_cancel_rejects_stale_or_foreign_target_without_changing_queue(queued_runtime, damage):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    await manager.start_message(session.id, "Current work", wiki_mode=None)
    record = await runtime.input(2)
    ordinary = await manager.start_message(session.id, "Ordinary queue", source="bg", wiki_mode=None)
    compact = await manager.start_compact(session.id, client_request_id="correct-compact")
    before = manager.dispatch_snapshot(session.id)
    command = {"type": "cancel_compact", "session_id": session.id, "client_command_id": "wrong-target",
        "generation": "old-service" if damage == "generation" else before["generation"],
        "request_id": ordinary.request_id if damage == "kind" else "old-request" if damage == "request" else compact.request_id}
    reply = await process_command(manager, command, "other-account" if damage == "account" else session.user_id)
    assert not reply["accepted"]
    assert manager.dispatch_snapshot(session.id) == before
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    assert (await compact.wait()).outcome.status == "success"


async def test_proven_rejected_steer_requeues_after_compact_in_original_order(native_runtime, tmp_path, monkeypatch):
    runtime = QueueRuntime(native_runtime, tmp_path)
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Current work", wiki_mode=None)
    record = await runtime.input(2)
    await runtime.message_ready(record, 2)
    anchor = fork_helpers.capture_git_anchor
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_anchor(*args):
        result = await anchor(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(fork_helpers, "capture_git_anchor", held_anchor)
    steer = await manager.start_message(session.id, "Undelivered steer", steerable=True, wiki_mode=None)
    assert steer.disposition == "steered"
    await asyncio.wait_for(entered.wait(), 3)
    try:
        before = await manager.start_message(session.id, "Ordinary return", source="bg", wiki_mode=None)
        compact = await manager.start_compact(session.id, client_request_id="before-requeued-steer")
        (tmp_path / "native-steer-reject").touch()
    finally:
        release.set()
    await until(lambda: any(i["request_id"] == steer.request_id for i in manager.dispatch_snapshot(session.id)["items"]))
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    core = {active.request_id, compact.request_id, steer.request_id, before.request_id}
    assert [e["request_id"] for e in runtime.events if e["type"] == "result" and e.get("terminal")
        and e.get("request_id") in core] == [active.request_id, compact.request_id, steer.request_id, before.request_id]
    assert (await steer.wait()).outcome.status == "success"


async def test_automatic_compaction_does_not_complete_pending_manual_request(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Work with auto compaction", wiki_mode=None)
    record = await runtime.input(2)
    compact = await manager.start_compact(session.id, client_request_id="manual-after-auto")
    if runtime.claude:
        claude_inject(runtime.root, {"type": "system", "subtype": "compact_boundary", "session_id": SID,
            "uuid": "automatic-boundary", "compact_metadata": {"trigger": "auto", "pre_tokens": 90000}})
    else:
        codex_inject(runtime.root, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-2",
            "item": {"id": "automatic-boundary", "type": "contextCompaction"}}})
    await until(lambda: any(e["type"] == "context_state" and
        ((e.get("context_state") or {}).get("invalidation") or {}).get("reason") == "compacted" for e in runtime.events))
    assert compact.completion is None and active.completion is None
    assert not any(context.operation == "compact" for context in runtime.starts)
    runtime.ready()
    runtime.finish_message(record, 2)
    await wait_dispatch_idle(manager, session.id)
    outcome = (await compact.wait()).outcome
    assert outcome.status == "success" and outcome.compact.effect == "applied"
    assert "automatic-boundary" not in outcome.compact.item_ids
    assert sum(context.operation == "compact" for context in runtime.starts) == 1


async def test_busy_compact_uses_original_turn_quota_after_current_work(queued_runtime, monkeypatch):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    monkeypatch.setattr(settings, "max_concurrent_turns_per_user", 1)
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    active = await manager.start_message(session.id, "Occupy original quota slot", wiki_mode=None)
    record = await runtime.input(2)
    compact = await manager.start_compact(session.id, client_request_id="same-quota")
    assert compact.accepted and manager.running_turns_for(session.user_id) == 1
    assert compact.completion is None
    runtime.ready()
    runtime.finish_message(record, 2)
    assert (await active.wait()).outcome.status == "success"
    assert (await compact.wait()).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    assert manager.running_turns_for(session.user_id) == 0


async def test_archive_withdraws_pending_compact_before_restoring_session(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    await manager.start_message(session.id, "Current work", wiki_mode=None)
    await runtime.input(2)
    compact = await manager.start_compact(session.id, client_request_id="archive-pending")
    await manager.archive_session(session.id, replace=False)
    assert (await compact.wait()).state == "withdrawn"
    runtime.ready()
    restored = await manager.unarchive_session(session.id, session.user_id)
    await manager.resume_dispatch(restored.id)
    await wait_dispatch_idle(manager, restored.id)
    assert not any(context.operation == "compact" for context in runtime.starts)


async def test_independent_background_task_returns_after_compact_without_blocking_it(queued_runtime):
    runtime = queued_runtime
    manager, session = runtime.manager, runtime.session
    await (await manager.start_message(session.id, "Seed native history", wiki_mode=None)).wait()
    await wait_dispatch_idle(manager, session.id)
    runtime.hold()
    await manager.start_message(session.id, "Current work", wiki_mode=None)
    record = await runtime.input(2)
    release = runtime.root / "release-independent-background"
    background = BgTaskManager()
    background.bind(runtime.database, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    try:
        task = await background.start_task(session_id=session.id, working_dir=str(runtime.root), idle_timeout_seconds=0,
            command=shlex.join([sys.executable, "-c", "from pathlib import Path; import time; "
                f"release=Path({str(release)!r}); "
                "exec('while not release.exists(): time.sleep(.01)'); print('Independent task finished')"]))
        compact = await manager.start_compact(session.id, client_request_id="before-bg-return")
        runtime.ready()
        runtime.finish_message(record, 2)
        async with asyncio.timeout(5):
            assert (await compact.wait()).outcome.status == "success"
        assert (await background.get_task(task.id)).status == "running"
        release.touch()
        await until(lambda: any(e["type"] == "user_message" and "Independent task finished" in str(e.get("content"))
            for e in runtime.events))
        await wait_dispatch_idle(manager, session.id)
        result = await background.get_task(task.id)
        assert result.status == "completed" and result.delivery_disposition is None
        detail = await get_session(manager, session.id, user_id=session.user_id)
        assert any(m.role.value == "user" and "Independent task finished" in str(m.content) for m in detail.messages)
    finally:
        release.touch()
        await background.shutdown()

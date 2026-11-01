"""Original product questions and wire responses through the owned native host."""

import asyncio
import json

import pytest

from server.question_resolution import AnswerSource
from server.sessions.client_submissions import submit_client_message
from tests import test_codex_app_server_submission as native_tests
from tests.test_runtime_review_races import wait_dispatch_idle

native_runtime = native_tests.native_runtime


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


def wire_frames(root):
    path = root / "native-frames.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def inject(root, *frames):
    with (root / "native-injections.jsonl").open("a") as handle:
        for frame in frames:
            handle.write(json.dumps(frame) + "\n")


def terminal(status="completed"):
    return {"method": "turn/completed", "params": {"threadId": "exact-native-thread",
        "turn": {"id": "turn-1", "status": status}}}


async def test_original_question_answer_uses_server_namespace_and_saved_source(native_runtime, tmp_path):
    manager, database, session, mode, _, events = native_runtime
    mode[0] = "control_question"
    receipt = await submit_client_message(manager, session.id, "Ask me", [], "question", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    question = manager.get_pending_questions(session.id)[0]
    assert question.request_id == receipt["request_id"]
    assert question.questions[0]["question"] == "Choose one"
    source = AnswerSource("account", user_id=session.user_id)
    assert await manager.answer_question(session.id, question.question_id, [{"selected": ["B"]}], source=source)
    assert not await manager.answer_question(session.id, question.question_id, [{"selected": ["A"]}], source=source)
    await until(lambda: any("result" in frame and "method" not in frame for frame in wire_frames(tmp_path)))
    answers = [frame for frame in wire_frames(tmp_path) if "result" in frame and "method" not in frame]
    start = next(frame for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start")
    assert answers == [{"id": start["id"], "result": {"answers": {"choice": {"answers": ["B"]}}}}]
    await until(lambda: any(event["type"] == "question_answer" for event in events))
    rows = await database.load_messages(session.id)
    answer = next(row for row in rows if row["type"] == "question_answer")
    assert answer["tool_input"]["question_resolution"]["source"] == {"kind": "account", "user_id": session.user_id}
    assert answer["tool_input"]["question_resolution"]["answers"][0]["selected"] == [{"label": "B", "option_index": 1}]


@pytest.mark.parametrize("close", ["terminal", "resolved", "stop", "disconnect"])
async def test_closure_at_write_lock_prevents_old_answer(native_runtime, tmp_path, close):
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    question = manager.get_pending_questions(session.id)[0]
    run = session._run.backend
    await run._write_lock.acquire()
    try:
        assert await manager.answer_question(session.id, question.question_id, [{"text": "old answer"}])
        if close == "terminal":
            inject(tmp_path, terminal())
            await until(lambda: not run.steering_open)
        elif close == "resolved":
            start = next(frame for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start")
            inject(tmp_path, {"method": "serverRequest/resolved", "params": {
                "threadId": "exact-native-thread", "requestId": start["id"]}})
            await until(lambda: not run.question_open(question.native_ref))
        elif close == "stop":
            await manager.interrupt(session.id)
        else:
            run.close_stdin()
            await until(lambda: not run.question_open(question.native_ref))
    finally:
        run._write_lock.release()
    await until(lambda: not run._question_replies)
    assert not [frame for frame in wire_frames(tmp_path) if "result" in frame and "method" not in frame]
    assert not manager.has_pending_question(session.id, question.question_id)


async def test_terminal_is_read_while_input_proof_commit_waits(native_runtime, tmp_path, monkeypatch):
    from server.harness import codex_app_server
    monkeypatch.setattr(codex_app_server, "_RPC_TIMEOUT", 30)
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def gated(session_id, request_id, evidence, **kwargs):
        if evidence.state == "accepted":
            entered.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", gated)
    try:
        await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
        await asyncio.wait_for(entered.wait(), 3)
        run = session._run.backend
        inject(tmp_path, terminal())
        await until(lambda: run.retirement_state != "pending" or run._retirement_started.is_set())
        assert not run.steering_open
        assert run._stream_failure is None
    finally:
        release.set()


async def test_native_question_and_terminal_advance_behind_full_publication(native_runtime, tmp_path):
    from tests.test_codex_app_server_output import item
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event):
        if event.get("type") == "assistant_text":
            entered.set()
            await release.wait()

    manager.on_broadcast("blocked-presentation", hold)
    try:
        await submit_client_message(manager, session.id, "Wait", [], "question", wiki_mode=None)
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        run = session._run.backend
        inject(tmp_path, item("exact-native-thread", "turn-1", "prefix", "x" * (1024 * 1024 + 1)))
        await asyncio.wait_for(entered.wait(), 3)
        inject(tmp_path, {"id": 0, "method": "item/tool/requestUserInput", "params": {
            "threadId": "exact-native-thread", "turnId": "turn-1", "itemId": "ask", "isBlocking": True,
            "questions": [{"id": "q", "header": "Choice", "question": "Still answer?"}]}})
        await until(lambda: manager.get_pending_questions(session.id))
        question = manager.get_pending_questions(session.id)[0]
        assert await manager.answer_question(session.id, question.question_id, [{"text": "yes"}])
        await until(lambda: any(frame.get("id") == 0 and "result" in frame for frame in wire_frames(tmp_path)))
        inject(tmp_path, terminal())
        await until(lambda: run.retirement_state == "proven")
        assert not manager.get_pending_questions(session.id)
        assert not release.is_set()
    finally:
        release.set()
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    assert [row["type"] for row in rows].count("question_answer") == 1


@pytest.mark.parametrize("method,result", [
    ("item/commandExecution/requestApproval", {"decision": "decline"}),
    ("item/fileChange/requestApproval", {"decision": "decline"}),
    ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
    ("mcpServer/elicitation/request", {"action": "decline"}),
    ("item/tool/call", {"success": False, "contentItems": [{"type": "inputText", "text": "No dynamic tool is registered for this request"}]}),
])
async def test_native_requests_preserve_permission_policy(native_runtime, tmp_path, method, result):
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    await submit_client_message(manager, session.id, "Request", [], "request", wiki_mode=None)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, {"id": 0, "method": method, "params": {
        "threadId": "child", "turnId": "child-turn", "itemId": "approval", "callId": "dynamic"}})
    await until(lambda: any(frame.get("id") == 0 and "result" in frame for frame in wire_frames(tmp_path)))
    assert next(frame for frame in wire_frames(tmp_path) if frame.get("id") == 0) == {"id": 0, "result": result}
    assert not manager.get_pending_questions(session.id)


@pytest.mark.parametrize("child_terminal", ["unfinished", "causes_overflow", "after_overflow"])
async def test_output_overflow_retires_before_presentation_and_never_reports_success(native_runtime, tmp_path, monkeypatch, child_terminal):
    import server.harness.run as run_module
    from tests.test_codex_app_server_output import child_end, child_start, item
    manager, database, session, mode, _, events = native_runtime
    mode[0] = "control_output"
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(run_module, "_EVENT_BUFFER_COUNT", 4)

    async def hold(event):
        if event.get("type") == "assistant_text":
            entered.set()
            await release.wait()

    manager.on_broadcast("overflow-presentation", hold)
    try:
        receipt = await submit_client_message(manager, session.id, "Output", [], "overflow", wiki_mode=None)
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        run = session._run.backend
        inject(tmp_path, child_start())
        await until(lambda: any(card.native_thread_id == "child" for card in session._subagents.values()))
        inject(tmp_path, item("exact-native-thread", "turn-1", "large", "L" * (1024 * 1024 + 1)))
        await asyncio.wait_for(entered.wait(), 3)
        # Fill the Owner's original publication handoff before filling the
        # reader's one offered event and the four native queue slots.
        execution = manager._session_owners[id(session)].runtime.current
        inject(tmp_path, item("exact-native-thread", "turn-1", "item-0", "part-0"))
        await until(lambda: any(frame.get("content") == "part-0" for frame in execution.frames))
        inject(tmp_path, item("exact-native-thread", "turn-1", "item-1", "part-1"))
        await until(lambda: execution.reader.ready and execution.reader.event.content == "part-1")
        inject(tmp_path, *[item("exact-native-thread", "turn-1", f"item-{index}", f"part-{index}") for index in range(2, 6)])
        await until(lambda: len(run._event_queue) == 4)
        assert run._stream_failure is None
        tail = [] if child_terminal == "causes_overflow" else [item("exact-native-thread", "turn-1", "item-6", "part-6")]
        if child_terminal != "unfinished":
            tail.append(child_end())
        inject(tmp_path, *tail, terminal())
        await until(lambda: run.retirement_state == "proven")
        assert run._stream_failure.error_code == "native_output_overflow"
        assert not any(event.get("terminal") for event in events)
    finally:
        release.set()
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed"
    rows = [row["content"] for row in await database.load_messages(session.id) if row["type"] == "text" and row["role"] == "assistant"]
    assert rows[1:] == [f"part-{index}" for index in range(len(rows) - 1)]
    cards = [row["tool_input"] for row in await database.load_messages(session.id) if row["type"] == "native_subagent"]
    expected = "interrupted" if child_terminal == "unfinished" else "completed"
    assert cards[-1]["status"] == expected
    assert next(iter(session._subagents.values())).status == expected
    assert [event for event in events if event.get("type") == "subagent"][-1]["status"] == expected


async def test_reused_server_id_on_new_connection_cannot_reopen_old_product_question(native_runtime, tmp_path):
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    await submit_client_message(manager, session.id, "First", [], "first", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    old = manager.get_pending_questions(session.id)[0]
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    await submit_client_message(manager, session.id, "Second", [], "second", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    current = manager.get_pending_questions(session.id)[0]
    assert current.question_id != old.question_id
    assert current.native_ref.server_request_id == old.native_ref.server_request_id
    assert current.native_ref.connection_generation != old.native_ref.connection_generation
    assert not await manager.answer_question(session.id, old.question_id, [{"text": "late"}])
    assert await manager.answer_question(session.id, current.question_id, [{"text": "current"}])
    await until(lambda: any("result" in frame and "method" not in frame for frame in wire_frames(tmp_path)))
    answers = [frame["result"] for frame in wire_frames(tmp_path) if "result" in frame and "method" not in frame]
    assert answers == [{"answers": {"choice": {"answers": ["current"]}}}]


async def test_resolved_before_start_ack_is_never_reopened(native_runtime):
    manager, database, session, mode, _, events = native_runtime
    mode[0] = "control_question_resolved_before_ack"
    await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
    await until(lambda: any(event["type"] == "question_closed" for event in events))
    assert not manager.get_pending_questions(session.id)
    request = next(event for event in events if event["type"] == "question_request")
    assert not await manager.answer_question(session.id, request["question_id"], [{"text": "late"}])
    rows = await database.load_messages(session.id)
    assert [row["type"] for row in rows].count("question_closed") == 1


def question_request(identifier):
    return {"id": identifier, "method": "item/tool/requestUserInput", "params": {
        "threadId": "exact-native-thread", "turnId": "turn-1", "itemId": f"ask-{identifier}", "isBlocking": True,
        "questions": [{"id": "q", "header": "Choice", "question": "Choose"}]}}


async def test_retired_attempt_records_every_accepted_question_prefix(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    entered, release = asyncio.Event(), asyncio.Event()
    append = database.append_message

    async def hold_request(*args, **kwargs):
        if kwargs.get("type") == "question_request" and not entered.is_set():
            entered.set()
            await release.wait()
        return await append(*args, **kwargs)

    monkeypatch.setattr(database, "append_message", hold_request)
    try:
        await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        run = session._run.backend
        inject(tmp_path, *[question_request(index) for index in range(8)])
        await asyncio.wait_for(entered.wait(), 3)
        await until(lambda: len(run._question_events) == 7)
        inject(tmp_path, terminal())
        await until(lambda: run.retirement_state == "proven")
    finally:
        release.set()
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    assert [row["type"] for row in rows].count("question_request") == 8
    assert [row["type"] for row in rows].count("question_closed") == 8
    assert not run.has_question_events


async def test_retained_question_budget_cannot_be_bypassed_by_draining_inbox(native_runtime, tmp_path, monkeypatch):
    from server.sessions import owner_questions
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(owner_questions, "_MAX_NATIVE_QUESTIONS", 2)
    await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    run = session._run.backend
    for identifier in range(2):
        inject(tmp_path, question_request(identifier))
        await until(lambda identifier=identifier: len(manager.get_pending_questions(session.id)) == identifier + 1)
        assert not run.has_question_events
    inject(tmp_path, question_request(2))
    await until(lambda: run.retirement_state == "proven")
    assert run._stream_failure.error_code == "native_question_overflow"
    await wait_dispatch_idle(manager, session.id)


async def test_rejected_input_commit_does_not_block_root_terminal(native_runtime, tmp_path, monkeypatch):
    from server.harness import codex_app_server
    from tests.test_codex_app_server_steering import active_root
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "steer_reject"
    monkeypatch.setattr(codex_app_server, "_RPC_TIMEOUT", 30)
    await active_root(native_runtime, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def hold_rejection(session_id, request_id, evidence, **kwargs):
        if evidence.kind == "steer" and evidence.state == "rejected":
            entered.set()
            await release.wait()
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", hold_rejection)
    try:
        await submit_client_message(manager, session.id, "Refused", [], "refused", wiki_mode=None)
        await asyncio.wait_for(entered.wait(), 3)
        run = session._run.backend
        inject(tmp_path, terminal())
        await until(lambda: run._retirement_started.is_set())
        assert run.native_end_reason == "parent_done"
        assert run._stream_failure is None
    finally:
        release.set()


async def test_correlated_stop_seals_native_answer_while_answer_storage_waits(native_runtime, tmp_path, monkeypatch):
    from server.sessions.commands import process_command
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    question = manager.get_pending_questions(session.id)[0]
    run = session._run.backend
    entered, release = asyncio.Event(), asyncio.Event()
    append = database.append_message

    async def hold_answer(*args, **kwargs):
        if kwargs.get("type") == "question_answer":
            entered.set()
            await release.wait()
        return await append(*args, **kwargs)

    monkeypatch.setattr(database, "append_message", hold_answer)
    answer = asyncio.create_task(manager.answer_question(session.id, question.question_id, [{"text": "before Stop"}]))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        stop = asyncio.create_task(process_command(manager, {"type": "interrupt", "session_id": session.id,
            "client_command_id": "stop-original", "request_id": question.request_id, "attempt_id": question.attempt_id,
            "generation": manager.dispatch_snapshot(session.id)["generation"]}, session.user_id))
        await until(lambda: run._retirement_started.is_set())
        assert not run.question_open(question.native_ref)
        assert not release.is_set()
    finally:
        release.set()
        await answer
        if stop is not None:
            assert (await stop)["accepted"]
    await wait_dispatch_idle(manager, session.id)
    assert not [frame for frame in wire_frames(tmp_path) if "result" in frame and "method" not in frame]


async def test_start_rejection_after_stop_selection_only_refines_original_facts(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "stop_late_rejected"
    await submit_client_message(manager, session.id, "Original", [], "stop", wiki_mode=None)
    await until(lambda: (tmp_path / "native-business.jsonl").exists())
    owner = manager._session_owners[id(session)]
    execution = owner.runtime.current
    entered, release = asyncio.Event(), asyncio.Event()
    reader_release = asyncio.Event()
    record = database.record_terminal
    finish = execution.attempt.backend._driver.finish_observations

    async def hold_reader_finish():
        await finish()
        # Keep the original finite reader alive after the real proof commits.
        # No fabricated RPC or retirement fact is introduced by this barrier.
        await reader_release.wait()

    monkeypatch.setattr(execution.attempt.backend._driver, "finish_observations", hold_reader_finish)

    async def hold_stop_commit(**kwargs):
        if kwargs["content"]["status"] == "interrupted":
            entered.set()
            await release.wait()
        return await record(**kwargs)

    monkeypatch.setattr(database, "record_terminal", hold_stop_commit)
    stop = asyncio.create_task(manager.interrupt(session.id))
    try:
        await stop
        await until(lambda: execution.operation.done())
        assert execution.interrupted and execution.settlement.outcome.status == "interrupted"
        assert not entered.is_set()
        # A public owner command must remain available while the original
        # reader is finishing. Ordinary failure cleanup would cancel it here.
        assert not await manager.close_pending_question(session.id, "not-a-question")
        reader_release.set()
        await asyncio.wait_for(entered.wait(), 3)
        release.set()
    finally:
        release.set()
        reader_release.set()
        await stop
    await until(lambda: owner.failed or manager.dispatch_snapshot(session.id)["active_request_id"] is None)
    assert not owner.failed
    saved = await database.get_client_submission(session.id, "stop")
    assert saved["native_inputs"][0]["state"] == "rejected"
    assert saved["terminal"]["status"] == "interrupted"


async def test_conflicting_stop_cannot_retire_host_while_original_answer_owns_command_id(native_runtime, tmp_path, monkeypatch):
    from server.sessions.commands import process_command
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    await submit_client_message(manager, session.id, "Ask", [], "question", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    question = manager.get_pending_questions(session.id)[0]
    run = session._run.backend
    entered, release = asyncio.Event(), asyncio.Event()
    append = database.append_message

    async def hold_answer(*args, **kwargs):
        if kwargs.get("type") == "question_answer":
            entered.set()
            await release.wait()
        return await append(*args, **kwargs)

    monkeypatch.setattr(database, "append_message", hold_answer)
    identity = {"session_id": session.id, "client_command_id": "same-command",
                "request_id": question.request_id, "attempt_id": question.attempt_id}
    answer = asyncio.create_task(process_command(manager, {**identity, "type": "answer_question",
        "question_id": question.question_id, "answers": [{"text": "original"}]}, session.user_id))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        stop = asyncio.create_task(process_command(manager, {**identity, "type": "interrupt",
            "generation": manager.dispatch_snapshot(session.id)["generation"]}, session.user_id))
        owner = manager._session_owners[id(session)]
        await until(lambda: len(owner._controls) > 0)
        assert not run._retirement_started.is_set(), "A conflicting command must have no resource effect"
    finally:
        release.set()
        assert (await answer)["accepted"]
        if stop is not None:
            assert (await stop)["code"] == "command_conflict"
    await until(lambda: any(frame.get("result", {}).get("answers") for frame in wire_frames(tmp_path)))


async def test_mcp_owner_question_also_closes_at_native_terminal_behind_presentation(native_runtime, tmp_path):
    from tests.test_codex_app_server_output import item
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event):
        if event.get("type") == "assistant_text":
            entered.set()
            await release.wait()

    manager.on_broadcast("mcp-question-terminal", hold)
    try:
        await submit_client_message(manager, session.id, "Ask", [], "mcp-question", wiki_mode=None)
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        run = session._run.backend
        question = await manager.create_pending_question(session.id, [{"question": "MCP question"}],
            attempt_id=session._run.question_attempt_id)
        inject(tmp_path, item("exact-native-thread", "turn-1", "large", "x" * (1024 * 1024 + 1)))
        await asyncio.wait_for(entered.wait(), 3)
        inject(tmp_path, terminal())
        await until(lambda: run._retirement_started.is_set())
        assert not manager.has_pending_question(session.id, question)
        assert not await manager.answer_question(session.id, question, [{"text": "late"}])
    finally:
        release.set()

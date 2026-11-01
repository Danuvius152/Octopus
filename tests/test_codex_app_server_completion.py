"""The bound root, its collected prefix and its original resource retire once."""

import asyncio
import json
import signal

import pytest

from server.config import settings
from server.database import Database
from server.mcp_identity import McpScope, scope_is_live
from server.routers.sessions import get_session
from server.service_health import ServiceHealth
from server.session_results import LiveReply
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message
from tests import test_codex_app_server_submission as native_tests
from tests.accounts import create_account
from tests.test_codex_app_server_output import (
    ROOT,
    child_end,
    child_start,
    item,
    start_output,
)
from tests.test_codex_app_server_questions import inject, terminal, until
from tests.test_runtime_review_races import wait_dispatch_idle

native_runtime = native_tests.native_runtime


@pytest.mark.parametrize("window", ["committed", "delivered"])
async def test_stop_after_selection_preserves_original_completion_and_pauses_dispatch(native_runtime, tmp_path, window):
    manager, database, session, _, starts, events = native_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_terminal(event):
        if event.get("terminal") and window == "committed":
            entered.set()
            await release.wait()

    manager.on_broadcast("terminal-stop-window", hold_terminal)
    receipt = await manager.start_message(session.id, "Finish once", source="human")
    waiting = asyncio.create_task(receipt.wait())
    try:
        if window == "committed":
            await asyncio.wait_for(entered.wait(), 4)
            assert not waiting.done()
        else:
            completion = await asyncio.wait_for(asyncio.shield(waiting), 4)
            assert completion.notification_state == "delivered"
        # The resource observation survives removal of the current backend.
        resource = receipt.execution.attempts[0]
        saved = await database.read_session_result(session.id, receipt.request_id)
        assert saved.outcome.status == "success"
        assert resource.state == "proven"
        await manager.interrupt(session.id)
        assert manager.dispatch_snapshot(session.id)["paused"]
    finally:
        release.set()
    completion = await asyncio.wait_for(waiting, 4)
    await wait_dispatch_idle(manager, session.id)
    assert completion.outcome.status == "success"
    assert completion.recording_state == "committed" and completion.notification_state == "delivered"
    assert (await database.read_session_result(session.id, receipt.request_id)).outcome == completion.outcome
    assert len(starts) == 1 and resource.state == "proven"
    assert len([event for event in events if event.get("terminal")]) == 1


async def test_root_terminal_uses_retirement_grace_instead_of_active_budget(native_runtime, tmp_path, monkeypatch):
    from server.harness import run as run_module

    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_group"
    monkeypatch.setattr(settings, "turn_max_seconds", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    wait_group = run_module.wait_group_exit

    async def hold_exit_proof(*args, **kwargs):
        entered.set()
        await release.wait()
        return await wait_group(*args, **kwargs)

    monkeypatch.setattr(run_module, "wait_group_exit", hold_exit_proof)
    receipt = await submit_client_message(manager, session.id, "Finish the root", [], "completion", wiki_mode=None)
    await until(lambda: (tmp_path / "native-group-ready").exists())
    run = session._run.backend
    try:
        inject(tmp_path, child_start(), item(ROOT, "turn-1", "answer", "Root final"), terminal(),
               item("child", "child-turn", "partial", "Late child partial"))
        await asyncio.wait_for(entered.wait(), 3)
        assert run.active_turn is None and not run.question_admission_open
        assert run.retirement_state == "pending"
        await asyncio.sleep(1.05)
        assert not any(event.get("terminal") for event in events)
    finally:
        (tmp_path / "native-group-release").touch()
        release.set()
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success"
    assert any(row["content"] == "Late child partial" for row in await database.load_messages(session.id))
    assert len(starts) == 1 and run.retirement_state == "proven"
    assert len([event for event in events if event.get("terminal")]) == 1


@pytest.mark.parametrize("missing", ["started", "delta"])
async def test_root_success_cannot_hide_a_missing_complete_root_item(native_runtime, tmp_path, missing):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    if missing == "started":
        incomplete = item(ROOT, "turn-1", "missing", "")
        incomplete["method"] = "item/started"
    else:
        incomplete = {"method": "item/agentMessage/delta", "params": {
            "threadId": ROOT, "turnId": "turn-1", "itemId": "missing", "delta": "Unfinished"}}
    inject(tmp_path, item(ROOT, "turn-1", "saved", "Saved final"), incomplete, terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed"
    assert result.outcome.recording_integrity == "incomplete"
    assert result.fragments == ("Saved final",)


@pytest.mark.parametrize("native_status", ["failed", "interrupted"])
async def test_native_root_status_is_preserved_without_another_root(native_runtime, tmp_path, native_status):
    manager, database, session, _, starts, events = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, child_start(), item(ROOT, "turn-1", "partial", "Saved final"), terminal(native_status))
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == native_status
    assert result.fragments == ("Saved final",)
    assert len(starts) == 1
    assert len([event for event in events if event.get("terminal")]) == 1


@pytest.mark.parametrize("gate", ["prefix", "terminal"])
async def test_stop_respects_outcome_selection_while_storage_cannot_delay_retirement(native_runtime, tmp_path, monkeypatch, gate):
    from server.harness import run as run_module

    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_group"
    entered, release, killed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    append, record, terminate = database.append_message, database.record_terminal, run_module._terminate_process_group

    async def gated_append(**kwargs):
        if gate == "prefix" and kwargs.get("role") == "assistant" and kwargs.get("content") == "Root final":
            entered.set()
            await release.wait()
        return await append(**kwargs)

    async def gated_terminal(**kwargs):
        if gate == "terminal":
            entered.set()
            await release.wait()
        return await record(**kwargs)

    def observe_signal(proc, sig):
        result = terminate(proc, sig)
        if sig == signal.SIGKILL:
            killed.set()
        return result

    monkeypatch.setattr(database, "append_message", gated_append)
    monkeypatch.setattr(database, "record_terminal", gated_terminal)
    monkeypatch.setattr(run_module, "_terminate_process_group", observe_signal)
    receipt = await manager.start_message(session.id, "Root final", source="human")
    await until(lambda: (tmp_path / "native-group-ready").exists())
    run = session._run.backend
    scope = McpScope(session.id, user_id=session.user_id, run_lease_id=run.run_lease_id, work_id=run.work_id)
    assert scope_is_live(scope)
    stopping = None
    try:
        inject(tmp_path, child_start(), item(ROOT, "turn-1", "answer", "Root final"), terminal(),
               item("child", "child-turn", "tail", "Late partial"))
        await asyncio.wait_for(entered.wait(), 5)
        assert run.active_turn is None and not run.question_admission_open and not scope_is_live(scope)
        await asyncio.wait_for(killed.wait(), 6)
        await asyncio.wait_for(run.wait_retirement(), 3)
        assert not release.is_set() and run.retirement_state == "proven"
        assert not any(event.get("terminal") for event in events)
        stopping = asyncio.create_task(manager.interrupt(session.id))
        await asyncio.sleep(0)
    finally:
        release.set()
        if stopping is not None:
            await stopping
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == ("interrupted" if gate == "prefix" else "success")
    saved = await database.read_session_result(session.id, receipt.request_id)
    assert saved.outcome.status == completion.outcome.status
    assert saved.outcome.native_status == "completed"
    assert any(row["content"] == "Late partial" for row in await database.load_messages(session.id))
    assert len(starts) == 1 and len([event for event in events if event.get("terminal")]) == 1


async def test_terminal_references_only_ordered_bound_root_final_items(native_runtime, tmp_path):
    manager, database, session, _, starts, events = native_runtime
    receipt = await start_output(native_runtime)
    commentary = item(ROOT, "turn-1", "progress", "Checking the helper")
    commentary["params"]["item"]["phase"] = "commentary"
    inject(tmp_path, commentary, child_start(), item("child", "child-turn", "answer", "Child only"),
           child_end(), child_start(turn="followup"), item(ROOT, "turn-1", "one", "First final"),
           item("child", "followup", "answer", "Child followup"),
           item(ROOT, "turn-1", "two", "Second final"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.fragments == ("First final", "Second final")
    rows = await database.load_messages(session.id)
    expected = [row["seq"] for row in rows if row["content"] in ("First final", "Second final")]
    assert result.outcome.final_answer.model_dump() == {
        "thread_id": ROOT, "turn_id": "turn-1", "message_seqs": expected,
    }
    assert len(starts) == 1
    assert len([event for event in events if event.get("terminal")]) == 1
    live = LiveReply(receipt["request_id"])
    for event in events:
        live.observe(event)
    assert live.text() == "First final\n\nSecond final"


@pytest.mark.parametrize("first_final", [False, True])
async def test_stop_selects_status_before_collecting_and_recording_final_references(native_runtime, tmp_path, first_final):
    manager, database, session, mode, _, events = native_runtime
    mode[0] = "control_stop_tail"
    receipt = await submit_client_message(manager, session.id, "Stop with a tail", [], "stop-tail", wiki_mode=None)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    if first_final:
        inject(tmp_path, item(ROOT, "turn-1", "first", "First final"))
        await until(lambda: any(event.get("content") == "First final" for event in events))
    commentary = item(ROOT, "turn-1", "progress", "Late commentary")
    commentary["params"]["item"]["phase"] = "commentary"
    (tmp_path / "native-stop-tail.json").write_text(json.dumps([
        commentary, item(ROOT, "turn-1", "last", "Late final"), terminal("interrupted"),
    ]))
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "interrupted"
    assert result.fragments == (("First final", "Late final") if first_final else ("Late final",))
    assert result.outcome.final_answer is not None
    live = LiveReply(receipt["request_id"])
    for event in events:
        live.observe(event)
    assert live.text() == "\n\n".join(result.fragments)
    assert len([event for event in events if event.get("terminal")]) == 1


@pytest.mark.parametrize("blocked_prefix", [False, True])
async def test_native_retirement_failure_ends_completion_and_reaches_existing_fatal_boundary(
    native_runtime, tmp_path, monkeypatch, short_process_shutdown, blocked_prefix,
):
    from server.harness import run as run_module

    _, _, _, mode, _, _ = native_runtime
    mode[0] = "control_group"
    fatal, exits = asyncio.Event(), []

    def observe_exit(code):
        exits.append(code)
        fatal.set()
        raise RuntimeError("Test observed the operating-system exit boundary")

    database = Database(str(tmp_path / "failed-retirement.db"), health=ServiceHealth(exit_process=observe_exit))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    publisher_release = asyncio.Event()

    async def hold_publication(event):
        if blocked_prefix and event.get("type") == "assistant_text":
            await publisher_release.wait()

    manager.on_broadcast("slow-native-failure-prefix", hold_publication)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Failed original retirement", str(tmp_path), backend="codex")
    wait_group = run_module.wait_group_exit

    async def lose_exit_proof(*args, **kwargs):
        await wait_group(*args, **kwargs)
        if kwargs.get("after_kill"):
            raise OSError("Original native group exit proof is unavailable")

    monkeypatch.setattr(run_module, "wait_group_exit", lose_exit_proof)
    receipt = await manager.start_message(session.id, "Keep the partial", source="human")
    try:
        await until(lambda: (tmp_path / "native-group-ready").exists())
        run = session._run.backend
        inject(tmp_path, item(ROOT, "turn-1", "saved", "Saved partial"), terminal())
        completion = await asyncio.wait_for(receipt.wait(), 4)
        assert completion.outcome is None or completion.outcome.status != "success"
        await asyncio.wait_for(fatal.wait(), 4)
        assert manager.session_owner_failed(session.id) and exits == [70]
        assert run.retirement_state == "failed"
    finally:
        publisher_release.set()
        owner = manager._session_owners[id(session)]
        if owner.task is not None and not owner.task.done():
            owner.task.cancel()
        await asyncio.gather(manager.close_session_owners(), return_exceptions=True)
        with pytest.raises(RuntimeError, match="Database teardown remains unconfirmed"):
            await database.close()
        # The test substitutes only the fatal OS exit. That process would
        # release SQLite; close its test connection after asserting fail-stop.
        await database._conn.close()


@pytest.mark.parametrize("noise", ["started", "completed"])
async def test_native_wait_and_duplicate_notifications_cannot_renew_idle_or_drop_shutdown_records(native_runtime, tmp_path, monkeypatch, noise):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_stop_tail"
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 3)
    receipt = await manager.start_message(session.id, "Wait within the original budget", source="human")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .2)
    wait = {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "type": "collabAgentToolCall", "id": "wait", "tool": "wait", "receiverThreadIds": ["child"], "status": "inProgress"}}}
    completed_wait = {"method": "item/completed", "params": {**wait["params"], "item": {**wait["params"]["item"], "status": "completed"}}}
    (tmp_path / "native-stop-tail.json").write_text(json.dumps([
        item("child", "child-turn", "partial", "Partial at idle cancellation"), completed_wait, terminal("interrupted"),
    ]))
    inject(tmp_path, child_start(), wait)
    repeated = child_start() if noise == "started" else child_end()
    for _ in range(10):
        inject(tmp_path, repeated, wait)
        await asyncio.sleep(.04)
    assert not session._run.backend or not session._run.backend.question_admission_open
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.status == "failed" and completion.outcome.code == "turn_timeout"
    assert completion.outcome.recovery["idle"] == 0
    rows = await database.load_messages(session.id)
    assert any(row["content"] == "Partial at idle cancellation" for row in rows)
    if noise == "started":
        child = next(row["tool_input"] for row in reversed(rows) if row["type"] == "native_subagent")
        assert child["status"] == "interrupted" and child["stop_reason"] == "parent_timeout"
    assert len(starts) == 1


@pytest.mark.parametrize("empty_item", [False, True])
async def test_legal_empty_root_answer_never_borrows_progress_or_optional_child_details(native_runtime, tmp_path, empty_item):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    commentary = item(ROOT, "turn-1", "progress", "Progress is not an answer")
    commentary["params"]["item"]["phase"] = "commentary"
    frames = [commentary, child_start(), {"method": "item/agentMessage/delta", "params": {
        "threadId": "child", "turnId": "child-turn", "itemId": "optional", "delta": "Optional child partial"}}]
    if empty_item:
        frames.append(item(ROOT, "turn-1", "empty", ""))
    inject(tmp_path, *frames, terminal())
    await wait_dispatch_idle(manager, session.id)
    saved = await database.read_session_result(session.id, receipt["request_id"])
    assert saved.outcome.status == "success" and saved.missing is None
    assert saved.fragments == (("",) if empty_item else ())
    assert saved.outcome.final_answer is not None and saved.reply_text() == ""
    live = LiveReply(receipt["request_id"])
    for event in events:
        live.observe(event)
    assert live.text() == "" and live.missing is None


async def test_late_root_terminal_cannot_erase_idle_expiry_while_owner_storage_waits(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, _, _ = native_runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .3)
    entered, release = asyncio.Event(), asyncio.Event()
    append = database.append_message

    async def hold_progress(**kwargs):
        if kwargs.get("role") == "assistant" and kwargs.get("content") == "Progress":
            entered.set()
            await release.wait()
        return await append(**kwargs)

    monkeypatch.setattr(database, "append_message", hold_progress)
    receipt = await start_output(native_runtime)
    progress = item(ROOT, "turn-1", "progress", "Progress")
    progress["params"]["item"]["phase"] = "commentary"
    try:
        inject(tmp_path, progress)
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(.35)
        inject(tmp_path, terminal())
        run = session._run.backend
        await until(lambda: not run.question_admission_open)
    finally:
        release.set()
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.outcome.code == "turn_timeout"


async def test_start_wait_failure_keeps_the_same_finite_late_output_prefix(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_stop_tail"
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def hold_acceptance(session_id, request_id, evidence, **kwargs):
        if evidence.state == "accepted":
            entered.set()
            await release.wait()
        return await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", hold_acceptance)
    (tmp_path / "native-stop-tail.json").write_text(json.dumps([
        item(ROOT, "turn-1", "late", "Late final after start wait"), terminal("interrupted"),
    ]))
    receipt = await manager.start_message(session.id, "Retain the accepted tail", source="human")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(.6)  # The controlled peer's start waiter expires at .5s.
    finally:
        release.set()
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed"
    result = await database.read_session_result(session.id, receipt.request_id)
    assert result.fragments == ("Late final after start wait",)
    assert len(starts) == 1


@pytest.mark.parametrize("early_close", ["native", "stop", "start_wait_failure"])
async def test_provider_failure_remains_readable_across_business_outcome_selection(native_runtime, tmp_path, monkeypatch, early_close):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_stop_tail"
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_native_input

    async def hold_acceptance(session_id, request_id, evidence, **kwargs):
        if early_close == "start_wait_failure" and evidence.state == "accepted":
            entered.set()
            await release.wait()
        return await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", hold_acceptance)
    failed = terminal("failed")
    provider_error = {"message": "Late root failure: original stream disconnected",
        "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}},
        "additionalDetails": "original-provider-cause", "misalignment": None}
    failed["params"]["turn"]["error"] = provider_error
    (tmp_path / "native-stop-tail.json").write_text(json.dumps([
        item(ROOT, "turn-1", "partial", "Saved final partial"), failed,
    ]))
    receipt = await manager.start_message(session.id, "Preserve the late failure", source="human")
    try:
        if early_close in {"native", "stop"}:
            await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
            if early_close == "stop":
                await manager.interrupt(session.id)
            else:
                inject(tmp_path, failed)
        else:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(.6)
    finally:
        release.set()
    completion = await asyncio.wait_for(receipt.wait(), 4)
    expected = "interrupted" if early_close == "stop" else "failed"
    assert completion.outcome.status == expected and completion.outcome.native_status == "failed"
    snapshot = await get_session(manager, session.id, user_id=session.user_id)
    failures = [message for message in snapshot.messages
                if message.type == "recovery" and message.content.get("message") == provider_error["message"]]
    assert len(failures) == 1
    diagnostic = failures[0]
    assert diagnostic.content["phase"] == "diagnostic" and diagnostic.content["code"] == "native_turn_failed"
    assert diagnostic.tool_input["provider_error"] == provider_error
    assert diagnostic.runtime_metadata.request_id == receipt.request_id
    assert diagnostic.runtime_metadata.native.thread_id == ROOT and diagnostic.runtime_metadata.native.turn_id == "turn-1"
    terminals = [message for message in snapshot.messages if message.type == "result"]
    assert len(terminals) == 1 and diagnostic.seq < terminals[0].seq
    assert len([event for event in events if event.get("terminal")]) == 1
    result = await database.read_session_result(session.id, receipt.request_id)
    assert result.fragments == ("Saved final partial",)
    assert diagnostic.runtime_metadata.attempt_id == result.attempt_id
    assert len(starts) == 1

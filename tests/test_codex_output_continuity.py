"""One original native execution survives a finite output/persistence burst."""

import asyncio
import hashlib
import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.deps import get_session_manager, get_user_manager
from server.fork_helpers import render_replay_history
from server.models import MessageContent
from server.routers.sessions import router
from tests.native_app_server import (
    ROOT,
    child_start,
    inject,
    item,
    start_output,
    terminal,
    until,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle

NOTICE = "[输出已缩短：仅保存收到输出的头尾，中间未归档；以下不是完整日志。]"


def command(identifier, output, stage):
    return {"method": "item/" + stage, "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "id": identifier, "type": "commandExecution", "command": f"run-{identifier}", "cwd": "/tmp",
        "commandActions": [{"type": "unknown", "command": f"run-{identifier}"}], "status": "completed",
        "processId": "original-process", "durationMs": 123, "exitCode": 0, "aggregatedOutput": output}}}


async def test_large_log_preview_pressure_second_tool_and_committed_final_share_original_run(native_runtime, tmp_path, monkeypatch):
    from server.harness import run as run_module

    manager, database, session, _, starts, events = native_runtime
    receipt = await start_output(native_runtime)
    run = session._run.backend
    original_root, original_lease = run.active_turn, run.run_lease_id
    monkeypatch.setattr(run_module, "_EVENT_BUFFER_COUNT", 4)
    entered, release = asyncio.Event(), asyncio.Event()
    append, emit, retirement = database.append_message, run._emit, run._driver.retirement_events
    observed = []

    async def gated_append(**kwargs):
        if kwargs.get("type") == "tool_result" and kwargs.get("tool_use_id") == "large":
            entered.set()
            await release.wait()
        return await append(**kwargs)

    async def observe(value):
        observed.append(value)
        await emit(value)

    def observe_retirement(*args, **kwargs):
        values = retirement(*args, **kwargs)
        observed.extend(values)
        return values

    monkeypatch.setattr(database, "append_message", gated_append)
    monkeypatch.setattr(run, "_emit", observe)
    monkeypatch.setattr(run._driver, "retirement_events", observe_retirement)
    first = "HEAD\n" + "ordinary-log-line\n" * 80_000 + "TAIL\n"
    try:
        inject(tmp_path, command("large", first, "started"), command("large", first, "completed"))
        await asyncio.wait_for(entered.wait(), 3)
        delta = {"method": "item/agentMessage/delta", "params": {"threadId": ROOT,
            "turnId": "turn-1", "itemId": "answer", "delta": "Disposable preview " * 10}}
        inject(tmp_path, *[delta for _ in range(100)])
        await until(lambda: len([value for value in observed if value.type == "text_delta"]) == 100)
        assert len(run._event_queue) == 4 and all(value.type == "text_delta" for value, _ in run._event_queue)
        assert run._event_bytes == sum(len(json.dumps(value, default=vars)) for value, _ in run._event_queue)
        assert run.collection_error is None
        inject(tmp_path, command("second", "Second tool completed", "started"),
               command("second", "Second tool completed", "completed"))
        await until(lambda: any(value.tool_use_id == "second" and value.type == "tool_result" for value in observed))
        assert not any(value.type == "text_delta" for value, _ in run._event_queue)
        assert run._event_bytes == sum(len(json.dumps(value, default=vars)) for value, _ in run._event_queue)
        assert run.collection_error is None and run.active_turn == original_root and run.run_lease_id == original_lease
    finally:
        release.set()
    await until(lambda: any(event.get("output") == "Second tool completed" for event in events))
    final_text = "Both original commands completed; this is the independent full final."
    final = item(ROOT, "turn-1", "answer", final_text)
    end = terminal()
    end["params"]["turn"].update(itemsView="summary", items=[{**final["params"]["item"], "completedAtMs": 100}])
    inject(tmp_path, final, end)
    await wait_dispatch_idle(manager, session.id)
    saved = await database.read_session_result(session.id, receipt["request_id"])
    assert saved.outcome.status == "success" and saved.outcome.native_status == "completed"
    assert saved.fragments == (final_text,) and not saved.outcome.recording_issues
    rows = await database.load_messages(session.id)
    tools = [row for row in rows if row["type"] in {"tool_use", "tool_result"}]
    assert [(row["type"], row["tool_use_id"]) for row in tools] == [
        ("tool_use", "large"), ("tool_result", "large"), ("tool_use", "second"), ("tool_result", "second")]
    big, second = tools[1], tools[3]
    assert big["content"].startswith(NOTICE + "\nHEAD\n") and big["content"].endswith("TAIL\n")
    assert len(json.dumps(big["content"])) <= 8192
    assert big["tool_input"]["output_capture"]["received_chars"] == len(first)
    assert big["tool_input"]["output_capture"]["state"] == "head_tail"
    assert second["content"] == "Second tool completed" and second["tool_input"]["output_capture"]["state"] == "inline"
    for row in tools:
        assert "aggregatedOutput" not in row["tool_input"]["native_item"]
        assert row["tool_input"]["native_item"]["durationMs"] == 123
        assert row["tool_input"]["native_item"]["processId"] == "original-process"
        assert row["runtime_metadata"]["request_id"] == receipt["request_id"]
        assert row["runtime_metadata"]["attempt_id"] == original_root.attempt_id
        assert row["runtime_metadata"]["native"]["thread_id"] == ROOT
        assert row["runtime_metadata"]["native"]["turn_id"] == "turn-1"
    assert all(value.raw is None for value in observed if value.type in {"tool_use", "tool_result", "text_delta", "text", "result"})
    final_row = next(row for row in rows if row["content"] == final_text)
    assert saved.outcome.final_answer.message_seqs == [final_row["seq"]]
    proof = next(value for value in observed if value.type == "native_completion")
    assert proof.raw == {"answer": hashlib.sha256(final_text.encode()).hexdigest()}
    assert proof.runtime_metadata["request_id"] == receipt["request_id"]
    assert run.retirement_state == "proven" and run.run_lease_id == original_lease
    assert len(starts) == 1 and len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    original = await database.get_submission_for_request(session.id, receipt["request_id"])
    assert len(original["native_inputs"]) == 1
    assert original["native_inputs"][0]["attempt_id"] == original_root.attempt_id
    assert original["native_inputs"][0]["turn_id"] == "turn-1"

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    token = await get_user_manager().issue_token(session.user_id)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                           headers={"Authorization": "Bearer " + token}) as client:
        detail = (await client.get(f"/api/sessions/{session.id}/messages/{big['seq']}")).json()
        page = (await client.get(f"/api/sessions/{session.id}/message-export")).json()["messages"]
        transcript = await client.get(f"/api/sessions/{session.id}/transcript-export")
        typed = [json.loads(line) for line in transcript.text.splitlines()]
        exported = next(value for value in page if value["seq"] == big["seq"])
        typed_command = next(value for value in typed if value["seq"] == big["seq"])
        for key in ("content", "tool_input", "runtime_metadata"):
            assert detail[key] == exported[key] == big[key]
            assert json.loads(typed_command[key]) == big[key]
        shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"]
        shown_command = next(value for value in shown if value["seq"] == big["seq"])
        assert shown_command["body_state"] == "inline" and shown_command["content"].startswith(NOTICE)
    replay = render_replay_history([MessageContent.model_validate(big)])
    assert NOTICE in replay and "root thread=" + ROOT in replay


async def test_native_error_diagnostics_keep_their_source_without_whole_frame_copies(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    run = session._run.backend
    emit, errors = run._emit, []

    async def observe(value):
        if value.type == "error":
            errors.append(value)
        await emit(value)

    monkeypatch.setattr(run, "_emit", observe)
    detail = {"message": "Original diagnostic chain", "additionalDetails": "Inner cause", "codexErrorInfo": "other"}
    inject(tmp_path, child_start(), {"method": "error", "params": {"threadId": ROOT, "turnId": "turn-1",
        "willRetry": True, "error": detail}}, {"method": "turn/completed", "params": {"threadId": "child",
        "turn": {"id": "child-turn", "rootTurnId": "turn-1", "status": "failed", "error": detail}}},
        item(ROOT, "turn-1", "answer", "Recovered root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert len(errors) == 2
    assert [value.error_code for value in errors] == ["native_provider_retry", "native_child_error"]
    for value, thread in zip(errors, (ROOT, "child"), strict=True):
        assert value.raw is None and value.content == detail["message"]
        assert value.error_details.items() >= detail.items()
        assert value.runtime_metadata["native"]["thread_id"] == thread
        assert value.runtime_metadata["request_id"] == receipt["request_id"]
    assert errors[0].error_details["willRetry"] is True
    assert errors[0].error_details["connection_generation"]
    saved = await database.read_session_result(session.id, receipt["request_id"])
    assert saved.outcome.status == "success" and saved.fragments == ("Recovered root answer",)
    rows = await database.load_messages(session.id)
    diagnostics = [row for row in rows if row["type"] == "recovery"]
    assert len(diagnostics) == 2
    for row, value in zip(diagnostics, errors, strict=True):
        assert row["content"]["code"] == value.error_code
        assert row["content"]["message"] == detail["message"]
        assert row["tool_input"]["provider_error"] == value.error_details
        assert row["runtime_metadata"] == value.runtime_metadata

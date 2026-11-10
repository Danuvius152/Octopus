"""Owned browser representations and full records share one original transcript."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.accounts import create_account
from tests.browser_message_samples import control_sample
from tests.fake_run import FakeRunBase
from tests.message_export_runtime import export_runtime as export_runtime
from tests.websocket_client import connection, frame

CONTROL_SAMPLES = Path(__file__).parents[1] / "web/src/lib/browserProjectionReplay.json"


@pytest.mark.parametrize("kind", ["recovery", "async_question", "missing_final", "unavailable_question", "unavailable_small_question"])
async def test_minimal_public_projection_preserves_controls_for_browser_replay(export_runtime, request, kind):
    db, manager, session, client, _, _ = export_runtime
    prefix, source, native_root = control_sample(kind)
    unavailable = kind.startswith("unavailable_")
    if kind == "unavailable_small_question":
        assert wire_size(source.tool_input["questions"]) < 24 * 1024
        assert wire_size(source.tool_input["native_question"]) > 256 * 1024
    saved = []
    for message in [*prefix, source]:
        await db.append_message(session.id, message.seq, message.role.value, message.type, message.content,
            tool_input=message.tool_input, tool_use_id=message.tool_use_id, runtime_metadata=message.runtime_metadata.model_dump(mode="json"),
            created_at="2026-11-10T00:00:00Z")
        saved.append((await client.get(f"/api/sessions/{session.id}/messages/{message.seq}")).json())
    shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][-1]
    assert wire_size(shown) <= 64 * 1024
    assert shown["identity_unavailable"] == unavailable
    assert shown["body_state"] == "preview" and shown["detail_ref"]
    assert saved[-1]["content"] == source.content and saved[-1]["tool_input"] == source.tool_input
    async with connection(manager, client.headers["Authorization"].removeprefix("Bearer ")) as browser:
        outgoing = {"type": "assistant_text" if source.type == "text" else source.type,
            "session_id": session.id, "seq": source.seq, "content": source.content, "tool_input": source.tool_input,
            "runtime_metadata": source.runtime_metadata.model_dump(mode="json")}
        if source.type == "question_request":
            outgoing.pop("content")
            outgoing.pop("tool_input")
            outgoing.update(question_id=source.tool_use_id, questions=source.tool_input["questions"],
                request_id="request", attempt_id="attempt", deadline=None)
            if "native_question" in source.tool_input:
                outgoing["native_question"] = source.tool_input["native_question"]
        await manager._broadcast(outgoing)
        event = await frame(browser[1])
        if kind == "unavailable_small_question":
            await manager._broadcast({**outgoing, "seq": None})
            unsaved = await frame(browser[1])
            assert unsaved["type"] == "browser_unavailable" and unsaved["identity_unavailable"]
            assert unsaved["seq"] is None and unsaved["detail_ref"] is None
            assert wire_size(unsaved) <= 256 * 1024
    assert wire_size(event) <= 256 * 1024
    assert event["type"] == ("browser_unavailable" if unavailable else "assistant_text" if source.type == "text" else source.type)
    if unavailable:
        assert event["identity_unavailable"] is True
        assert event["seq"] == source.seq
        assert event["detail_ref"] == f"/api/sessions/{session.id}/messages/{source.seq}"
        assert (await client.get(event["detail_ref"])).json() == saved[-1]

    # Normalize only identities after measuring the real wire representation.
    # Every occurrence maps identically, so matching and distinctness survive.
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, str):
            return value.replace(native_root, "native-root").replace(session.id, "sample-session")
        return value

    actual = stable({"message": shown, "event": event, "prefix": saved[:-1]})
    if unavailable:
        actual["full"] = stable(saved[-1])
    samples = json.loads(CONTROL_SAMPLES.read_text())
    if request.config.getoption("--update-ws-fixtures"):
        samples[kind] = actual
        CONTROL_SAMPLES.write_text(json.dumps(samples, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    assert actual == samples[kind]
    if kind == "recovery":
        assert event["content"]["code"] == "transient_error" and event["content"]["attempt"] == 1
        assert shown["content"]["maximum"] == 2 and shown["content"]["delay"] == 0.5
    elif kind == "async_question":
        assert shown["tool_input"]["async_questions_preview"] and event["tool_input"]["async_questions_preview"]
    elif kind == "missing_final":
        assert shown["content"]["recording_issues"] == {"native_final_missing": 1}
        assert event["content"]["recording_issues"] == {"native_final_missing": 1}
        assert event["content"]["final_answer"] == source.content["final_answer"]


async def test_saved_card_externalizes_display_text_before_original_identity(export_runtime):
    db, _, session, client, _, _ = export_runtime
    details = {"card_id": "original-card", "task_id": "original-task", "native_thread_id": "original-child",
        "native_turn_id": "original-turn", "status": "failed", "name": "Name " * 20_000,
        "description": "Description " * 20_000, "steps": [f"step-{index} " * 1_000 for index in range(20)]}
    await db.append_message(session.id, 0, "system", "native_subagent", None, tool_input=details)
    shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][0]
    assert wire_size(shown) <= 64 * 1024 and not shown["identity_unavailable"]
    for key in ("card_id", "task_id", "native_thread_id", "native_turn_id", "status"):
        assert shown["tool_input"][key] == details[key]
    assert shown["tool_input"]["display_truncated"]
    assert wire_size(shown["tool_input"]["name"]) <= 4 * 1024
    assert wire_size(shown["tool_input"]["description"]) <= 4 * 1024
    assert len(shown["tool_input"]["steps"]) == 12
    assert shown["tool_input"]["steps"][-1].startswith("step-19 ")
    assert (await client.get(shown["detail_ref"])).json()["tool_input"] == details


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def test_browser_windows_are_byte_bounded_and_page_without_holes(export_runtime):
    db, _, session, client, _, _ = export_runtime
    body = '中\\"\n' * 6_000
    for seq in range(100):
        await db.append_message(session.id, seq, "assistant", "text", body)
    page = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()
    assert wire_size(page["messages"]) <= 2 * 1024 * 1024
    assert 0 < page["oldest_loaded_seq"] < 99
    assert page["next_message_seq"] == 100
    collected = page["messages"]
    while page["has_more_messages"]:
        cursor = page["oldest_loaded_seq"]
        response = await client.get(f"/api/sessions/{session.id}/messages", params={"view": "browser", "before_seq": cursor})
        assert response.status_code == 200
        page = response.json()
        assert wire_size(page["messages"]) <= 2 * 1024 * 1024
        assert page["oldest_loaded_seq"] < cursor
        collected = page["messages"] + collected
    assert [row["seq"] for row in collected] == list(range(100))
    assert all(row["body_state"] == "preview" and wire_size(row) <= 64 * 1024 for row in collected)


@pytest.mark.parametrize("archived", [False, True])
async def test_full_detail_rechecks_account_session_missing_and_duplicate_sequence(export_runtime, archived):
    db, manager, session, client, _, _ = export_runtime
    await db.append_message(session.id, 0, "assistant", "text", "full original")
    if archived:
        await manager.archive_session(session.id, replace=False)
    url = f"/api/sessions/{session.id}/messages/0"
    assert (await client.get(url)).json()["content"] == "full original"
    _, foreign = await create_account(db, username="foreign-details")
    assert (await client.get(url, headers={"Authorization": f"Bearer {foreign}"})).status_code == 404
    assert (await client.get(url, headers={"Authorization": ""})).status_code == 401
    assert (await client.get(url.replace("/0", "/1"))).status_code == 404
    async with db.transaction() as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
        await tx.execute("INSERT INTO messages(session_id,seq,role,type,content) VALUES(?,0,'assistant','text','\"duplicate\"')", (session.id,))
    assert (await client.get(url)).status_code == 409
    async with db.transaction() as tx:
        await tx.execute("DELETE FROM messages WHERE session_id=?", (session.id,))
    assert (await client.get(url)).status_code == 404


@pytest.mark.parametrize("kind", ["text", "tool_use", "tool_result", "result"])
async def test_preview_has_one_body_source_and_full_detail_preserves_large_structures(export_runtime, kind):
    db, _, session, client, _, _ = export_runtime
    large = "原文\\\"\n" * 110_000
    body = {"kind": "outcome", "operation": "message", "request_id": "request-original", "status": "failed",
            "message": large, "recovery": {}, "recording_integrity": "complete"} if kind == "result" else large
    arguments = {"payload": {"large": large}} if kind == "tool_use" else {
        "native_item": {"type": "agentMessage", "text": large},
        "request_context": {"source": "human", "request_id": "request-original", "content": large}}
    await db.append_message(session.id, 0, "assistant", kind, body, tool_input=arguments)
    raw = (await client.get(f"/api/sessions/{session.id}/messages/0")).json()
    assert raw["content"] == body and raw["tool_input"] == arguments
    shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][0]
    assert shown["body_state"] == "preview" and wire_size(shown) <= 64 * 1024
    assert shown["detail_ref"] == f"/api/sessions/{session.id}/messages/0"
    if kind == "result":
        assert shown["content"]["status"] == "failed" and shown["content"]["request_id"] == "request-original"
    if kind == "text":
        assert shown["tool_input"]["request_context"]["source"] == "human"
        assert "content" not in shown["tool_input"]["request_context"]
        assert "text" not in shown["tool_input"]["native_item"]


async def test_oversized_original_identity_is_explicitly_unavailable_in_browser_only(export_runtime):
    db, _, session, client, _, _ = export_runtime
    identity = {"request_id": "r", "attempt_id": "a", "native": {
        "thread_id": "native" * 20_000, "turn_id": "turn", "root_thread_id": "root", "root_turn_id": "turn"}}
    await db.append_message(session.id, 0, "assistant", "text", "Original saved body", runtime_metadata=identity)
    display = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][0]
    assert display["identity_unavailable"] and display["body_state"] == "preview"
    assert wire_size(display) <= 64 * 1024 and display["runtime_metadata"] is None
    original = (await client.get(display["detail_ref"])).json()
    assert original["runtime_metadata"]["native"]["thread_id"] == identity["native"]["thread_id"]
    assert original["runtime_metadata"]["request_id"] == "r"


async def test_large_asynchronous_question_keeps_its_cue_and_original_full_body(export_runtime):
    db, _, session, client, _, _ = export_runtime
    item = {"type": "agentMessage", "questions": [{"title": "Original question " * 40_000}], "text": "Original body"}
    await db.append_message(session.id, 0, "assistant", "text", "Original body", tool_input={"native_item": item})
    shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][0]
    assert shown["body_state"] == "preview" and wire_size(shown) <= 64 * 1024
    assert shown["tool_input"]["async_questions_preview"] is True
    full = (await client.get(shown["detail_ref"])).json()
    assert full["tool_input"] == {"native_item": item}


@pytest.mark.parametrize("retire", ["delete", "revoke"])
async def test_full_reference_loses_access_after_public_deletion_or_logout(export_runtime, retire):
    db, _, session, client, _, _ = export_runtime
    await db.append_message(session.id, 0, "assistant", "text", "Saved original")
    ref = f"/api/sessions/{session.id}/messages/0"
    assert (await client.get(ref)).status_code == 200
    if retire == "delete":
        assert (await client.delete(f"/api/sessions/{session.id}")).status_code == 204
        expected = 404
    else:
        assert (await client.post("/api/auth/logout")).status_code == 204
        expected = 401
    assert (await client.get(ref)).status_code == expected


@pytest.mark.parametrize("reference_bytes", [30 * 1024, 90 * 1024])
async def test_terminal_reference_precedes_diagnostic_budget_in_http_and_ws(export_runtime, reference_bytes):
    db, manager, session, client, _, _ = export_runtime
    final = {"thread_id": "f" * reference_bytes, "turn_id": "final-turn", "message_seqs": [0]}
    metadata = {"request_id": "r", "attempt_id": "a", "native": {
        "thread_id": "n" * (20 * 1024), "turn_id": "t", "root_thread_id": "root", "root_turn_id": "t"}}
    body = {"kind": "outcome", "operation": "message", "request_id": "r", "status": "success",
        "final_answer": final, "message": "diagnostic" * 100_000, "recovery": {}}
    inputs = {"attempt_context": {"request_id": "r", "attempt_id": "a"},
        "rejected_input": {"content": "d" * (12 * 1024), "attachment_ids": []}}
    await db.append_message(session.id, 0, "system", "result", body, tool_input=inputs, runtime_metadata=metadata)
    saved = (await client.get(f"/api/sessions/{session.id}/messages/0")).json()
    assert saved["content"] == body and saved["runtime_metadata"]["native"]["thread_id"] == metadata["native"]["thread_id"]
    shown = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()["messages"][0]
    assert wire_size(shown) <= 64 * 1024
    if reference_bytes == 30 * 1024:
        assert not shown["identity_unavailable"]
        assert shown["content"]["final_answer"] == final and shown["runtime_metadata"] == saved["runtime_metadata"]
        assert shown["tool_input"]["attempt_context"] == inputs["attempt_context"]
    else:
        assert shown["identity_unavailable"] and "success" in shown["content"]
    assert (await client.get(shown["detail_ref"])).json()["content"] == body
    async with connection(manager, client.headers["Authorization"].removeprefix("Bearer ")) as browser:
        await manager._broadcast({"type": "result", "session_id": session.id, "seq": 0, "terminal": True,
            "content": body, "tool_input": inputs, "runtime_metadata": metadata})
        received = await frame(browser[1])
        assert wire_size(received) <= 256 * 1024
        if reference_bytes == 30 * 1024:
            assert received["type"] == "result" and received["content"]["final_answer"] == final
            assert received["runtime_metadata"] == saved["runtime_metadata"]
        else:
            assert received["type"] == "browser_unavailable" and "success" in received["message"]


async def test_pending_question_keeps_saved_reference_outside_the_browser_window(export_runtime, monkeypatch):
    from server.harness import HarnessEvent

    db, manager, session, client, _, _ = export_runtime
    started, release, emitted, finish = (asyncio.Event() for _ in range(4))

    class Run(FakeRunBase):
        async def start(self, *args, **kwargs):
            started.set()

        async def stream(self):
            await release.wait()
            for _ in range(100):
                yield HarnessEvent(type="text", content="Later text " * 4_000)
            emitted.set()
            await finish.wait()
            yield HarnessEvent(type="result", session_id="question-browser-native")

        async def stop(self):
            release.set()
            finish.set()

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    questions = [{"question": "Question body " * 20_000, "options": [{"label": "Keep the exact original option"}]}]
    receipt = await manager.start_message(session.id, "Keep the question visible")
    try:
        await asyncio.wait_for(started.wait(), 2)
        question_id = await manager.create_pending_question(session.id, questions)
        release.set()
        await asyncio.wait_for(emitted.wait(), 4)
        response = (await client.get(f"/api/sessions/{session.id}?view=browser")).json()
        pending = next(row for row in response["pending_questions"] if row["question_id"] == question_id)
        assert pending["body_state"] == "preview" and pending["questions"] == []
        assert pending["message_seq"] < response["oldest_loaded_seq"]
        assert pending["detail_ref"] == f"/api/sessions/{session.id}/messages/{pending['message_seq']}"
        full = (await client.get(pending["detail_ref"])).json()
        assert full["tool_input"]["questions"] == questions
        assert manager.has_pending_question(session.id, question_id)
        assert (await client.get(f"/api/sessions/{session.id}")).json()["pending_questions"][0]["questions"] == questions
    finally:
        finish.set()
        release.set()
        await asyncio.wait_for(receipt.wait(), 5)


async def test_native_browser_pages_keep_work_matching_and_the_actual_forward_cursor(export_runtime):
    db, _, session, client, _, _ = export_runtime
    identity = {"request_id": "r", "attempt_id": "a", "native": {
        "thread_id": "child", "turn_id": "child-turn", "root_thread_id": "root", "root_turn_id": "root-turn"}}
    await db.append_message(session.id, 0, "system", "native_subagent", None,
        tool_input={"task_id": "child", "card_id": "original-card", "native_thread_id": "child", "native_turn_id": "child-turn", "status": "completed"},
        runtime_metadata=identity)
    body = "中文\\\"\n" * 12_000
    for seq in range(1, 101):
        await db.append_message(session.id, seq, "assistant", "text", body, runtime_metadata=identity)
    base = f"/api/sessions/{session.id}/native-details/0"
    first = (await client.get(base, params={"view": "browser", "limit": 100})).json()
    assert first["availability"] == "available" and first["has_more"]
    assert wire_size(first["messages"]) <= 2 * 1024 * 1024
    after = first["next_after_seq"]
    assert after == first["messages"][-1]["seq"] < 100
    second = (await client.get(base, params={"view": "browser", "after_seq": after, "limit": 100})).json()
    assert [row["seq"] for row in first["messages"] + second["messages"]] == list(range(1, 101))
    assert not second["has_more"]
    full = (await client.get(first["messages"][0]["detail_ref"])).json()
    assert full["content"] == body and full["runtime_metadata"]["native"]["thread_id"] == "child"
    assert (await client.get(base, params={"after_seq": 0, "view": "browser"})).status_code == 409

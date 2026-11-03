"""Opt-in transport through original browser admission, SQLite and SessionOwner."""

import asyncio
import json
from pathlib import Path

import pytest

from server.harness import codex_app_server
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.owner_runtime import RuntimeReader
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


@pytest.mark.parametrize("kind", ["start", "steer"])
@pytest.mark.parametrize("missing", ["id", "content", "bad_content", "image_detail_array", "image_detail_unknown",
                                     "localImage_detail_array", "localImage_detail_unknown"])
async def test_malformed_correlated_user_item_cannot_prove_acceptance(native_runtime, tmp_path, kind, missing):
    from tests.native_app_server import ROOT, active_root, inject, until

    manager, database, session, mode, starts, _ = native_runtime
    if kind == "steer":
        mode[0] = "steer_lost"
        await active_root(native_runtime, tmp_path)
        reply = await submit_client_message(manager, session.id, "Lost ACK correction", [], "bad-proof", wiki_mode=None)
        await until(lambda: (tmp_path / "native-steers.jsonl").exists())
    else:
        mode[0] = "unknown"
        reply = await submit_client_message(manager, session.id, "Lost start ACK", [], "bad-proof", wiki_mode=None)
        await until(lambda: (tmp_path / "native-business.jsonl").exists())
    payload = {"type": "userMessage", "id": "proof", "content": [], "clientId": reply["request_id"]}
    if missing == "bad_content":
        payload["content"] = [{"type": "text", "text": {"truncated": True}}]
    elif "_detail_" in missing:
        variant, _, invalid = missing.split("_")
        payload["content"] = [{"type": variant, "url": "https://example.test/image.png", "path": "/tmp/image.png",
                               "detail": [] if invalid == "array" else "unknown"}]
    else:
        del payload[missing]
    inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": payload}})
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "bad-proof")
    assert status["execution"]["state"] == "unknown"
    assert status["execution"]["native_inputs"][-1]["state"] == "unknown"
    assert status["outcome"]["status"] == "failed" and len(starts) == 1


async def test_browser_deduplication_and_exact_history_resume(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    first = await submit_client_message(manager, session.id, "First input", [], "browser-1", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    again = await submit_client_message(manager, session.id, "First input", [], "browser-1", wiki_mode=None)
    assert again["request_id"] == first["request_id"]
    status = await submission_status(manager, session.id, "browser-1")
    assert status["execution"]["state"] == "done"
    evidence = status["execution"]["native_inputs"][0]
    assert evidence["state"] == "accepted" and evidence["turn_id"] == "turn-1"
    assert evidence["input_id"] == first["request_id"]
    result = await database.read_session_result(session.id, first["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Native answer",)
    await submit_client_message(manager, session.id, "Second input", [], "browser-2", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert len(starts) == 2 and starts[1].resume_id == "exact-native-thread"
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 2
    assert [frame["content"] for frame in events if frame.get("type") == "assistant_text"] == ["Native answer", "Native answer"]


async def test_unknown_start_keeps_input_even_when_native_reports_success(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "unknown"
    reply = await submit_client_message(manager, session.id, "Keep original text", [], "unknown-input", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "unknown-input")
    assert status["execution"]["state"] == "unknown"
    assert status["input"]["content"] == "Keep original text"
    assert status["execution"]["native_inputs"][0]["turn_id"] is None
    assert (await database.read_session_result(session.id, reply["request_id"])).missing == "input_unconfirmed"
    await submit_client_message(manager, session.id, "Keep original text", [], "unknown-input", wiki_mode=None)
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    assert len(starts) == 1


async def test_missing_history_fails_without_replacement_and_retains_draft(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    await submit_client_message(manager, session.id, "Create history", [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "native-history.jsonl").unlink()
    await submit_client_message(manager, session.id, "Retain my continuation", [], "missing", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "missing")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["state"] == "withdrawn"
    assert status["execution"]["recoverable_input"] is True
    assert status["input"]["content"] == "Retain my continuation"
    assert len(starts) == 2 and starts[-1].resume_id == "exact-native-thread"
    assert not (tmp_path / "native-history.jsonl").exists()
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_storage_failure_at_send_boundary_does_not_write_business_input(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    record = database.record_native_input

    async def fail_boundary(session_id, request_id, evidence, **kwargs):
        if evidence.state == "unknown":
            raise OSError("Original input evidence could not commit")
        await record(session_id, request_id, evidence, **kwargs)

    monkeypatch.setattr(database, "record_native_input", fail_boundary)
    await submit_client_message(manager, session.id, "Must not reach native", [], "not-sent", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert not (tmp_path / "native-business.jsonl").exists()
    status = await submission_status(manager, session.id, "not-sent")
    assert status["execution"]["state"] == "withdrawn"
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"


async def test_stop_retires_original_host_and_preserves_partial(native_runtime):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "held"
    reply = await submit_client_message(manager, session.id, "Wait for Stop", [], "stop-input", wiki_mode=None)
    async with asyncio.timeout(3):
        while not any(event.get("type") == "assistant_text" for event in events):
            await asyncio.sleep(0.01)
    row = await database.get_client_submission(session.id, "stop-input")
    pid = row["process_group"]
    assert Path(f"/proc/{pid}").exists()
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    assert not Path(f"/proc/{pid}").exists()
    result = await database.read_session_result(session.id, reply["request_id"])
    assert result.outcome.status == "interrupted"
    assert result.fragments == ("Native answer",)
    assert len(starts) == 1


@pytest.mark.parametrize("failure", ["bad_config", "early_root", "controller_lost"])
async def test_native_preparation_failure_never_sends_business_input(native_runtime, tmp_path, failure):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = failure
    await submit_client_message(manager, session.id, "Keep unsent original", [], "prepare", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "prepare")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["state"] == "withdrawn"
    evidence = status["execution"]["native_inputs"][0]
    assert evidence["state"] == "not_sent"
    assert evidence["controller"] == ("unknown" if failure == "controller_lost" else "not_needed")
    assert not (tmp_path / "native-business.jsonl").exists()
    assert len(starts) == 1


@pytest.mark.parametrize("failure", ["wrong_root", "second_root", "disconnect", "unknown_disconnect", "missing_item_turn", "missing_terminal_turn"])
async def test_native_failure_retires_without_replay_and_keeps_evidence(native_runtime, tmp_path, failure):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = failure
    reply = await submit_client_message(manager, session.id, "Original failure input", [], "failure", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "failure")
    assert status["outcome"]["status"] == "failed"
    assert len(starts) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    result = await database.read_session_result(session.id, reply["request_id"])
    if failure == "unknown_disconnect":
        assert status["execution"]["state"] == "unknown"
        assert result.missing == "input_unconfirmed"
        assert "Native answer" in status["outcome"]["message"]
    else:
        assert status["execution"]["native_inputs"][0]["turn_id"] == "turn-1"
        if failure in {"disconnect", "second_root"}:
            assert result.fragments == ("Native answer",)


async def test_explicit_rejection_allows_manual_recovery_but_never_automatic_replay(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "rejected"
    await submit_client_message(manager, session.id, "Rejected original", [], "rejected", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "rejected")
    assert status["execution"]["state"] == "withdrawn"
    assert status["execution"]["recoverable_input"]
    assert status["execution"]["native_inputs"][0]["state"] == "rejected"
    assert len(starts) == 1
    mode[0] = "normal"
    await submit_client_message(manager, session.id, "Rejected original", [], "manual-recovery", wiki_mode=None,
                                recovery_of_client_id="rejected")
    await wait_dispatch_idle(manager, session.id)
    assert len(starts) == 2 and starts[-1].resume_id == "exact-native-thread"
    assert (await submission_status(manager, session.id, "manual-recovery"))["outcome"]["status"] == "success"


async def test_attachment_and_large_input_preserve_original_receipt(native_runtime, tmp_path):
    from server.attachments import save_upload

    manager, database, session, mode, starts, events = native_runtime
    attachment = save_upload(session.id, "source.txt", b"Original attachment", user_id=session.user_id)
    original = "Large input " + "a" * 300_000
    await submit_client_message(manager, session.id, original, [attachment.id], "large", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "large")
    assert status["outcome"]["status"] == "success"
    assert status["input"]["content"] == original
    assert status["input"]["attachments"][0]["id"] == attachment.id
    delivered = json.loads((tmp_path / "native-business.jsonl").read_text())["input"][0]["text"]
    assert original in delivered
    assert str(attachment.path) in delivered


@pytest.mark.parametrize("scenario", ["late_tools", "native_tools", "late_bad_item"])
async def test_root_tool_items_keep_their_original_identity_and_complete_observations(native_runtime, scenario):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = scenario
    reply = await submit_client_message(manager, session.id, "Observe complete tools", [], "tools", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "tools")
    assert status["outcome"]["status"] == ("failed" if scenario == "late_bad_item" else "success")
    evidence = status["execution"]["native_inputs"][0]
    rows = [row for row in await database.load_messages(session.id) if row["type"] in {"tool_use", "tool_result"}]
    assert len(rows) == (8 if scenario == "native_tools" else 2)
    for row in rows:
        assert row["runtime_metadata"]["request_id"] == reply["request_id"]
        assert row["runtime_metadata"]["attempt_id"] == evidence["attempt_id"]
    for call, result in zip(rows[::2], rows[1::2], strict=True):
        assert call["runtime_metadata"]["tool_call_id"] == result["runtime_metadata"]["tool_call_id"]
        if scenario == "native_tools":
            assert "child-1" in result["content"]
    result = await database.read_session_result(session.id, reply["request_id"])
    assert result.fragments == ("Native answer",)


@pytest.mark.parametrize("ending", ["preparation_failure", "stop"])
async def test_committed_history_resumes_when_session_started_was_not_consumed(native_runtime, tmp_path, monkeypatch, ending):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "controller_lost"
    offered, release = asyncio.Event(), asyncio.Event()
    read = RuntimeReader.read

    async def delay_session_started(reader, stream, wake):
        async def delayed():
            async for event in stream:
                if event.type == "session_started":
                    offered.set()
                    await release.wait()
                    continue  # Lose the internal notification, not the finite output prefix.
                yield event
        await read(reader, delayed(), wake)

    monkeypatch.setattr(RuntimeReader, "read", delay_session_started)
    await submit_client_message(manager, session.id, "Keep the prepared history", [], "first", wiki_mode=None)
    try:
        await asyncio.wait_for(offered.wait(), 3)
        committed_id = await database.get_session_resume_id(session.id)
        assert committed_id and session.claude_session_id is None
        if ending == "stop":
            await manager.interrupt(session.id)
            release.set()
        await wait_dispatch_idle(manager, session.id)
    finally:
        release.set()
    first = await submission_status(manager, session.id, "first")
    assert first["outcome"]["status"] == ("interrupted" if ending == "stop" else "failed")
    native_id = first["execution"]["native_inputs"][0]["thread_id"]
    assert native_id == committed_id and session.claude_session_id is None
    mode[0] = "normal"
    await submit_client_message(manager, session.id, "Continue that exact history", [], "next", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    second = await submission_status(manager, session.id, "next")
    assert second["outcome"]["status"] == "success"
    assert starts[-1].resume_id == native_id
    assert second["execution"]["native_inputs"][0]["thread_id"] == native_id
    assert (tmp_path / "native-thread-starts").read_text() == "1"


@pytest.mark.parametrize("proof", ["accepted", "rejected"])
async def test_stop_retains_late_native_proof_without_changing_interrupted_outcome(native_runtime, tmp_path, proof):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "stop_late_" + proof
    reply = await submit_client_message(manager, session.id, "Original stopped input", [], "stop", wiki_mode=None)
    async with asyncio.timeout(3):
        while not (tmp_path / "native-business.jsonl").exists():
            await asyncio.sleep(.01)
    before = await submission_status(manager, session.id, "stop")
    assert before["execution"]["native_inputs"][0]["state"] == "unknown"
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    after = await submission_status(manager, session.id, "stop")
    assert after["outcome"]["status"] == "interrupted"
    assert after["execution"]["native_inputs"][0]["state"] == proof
    assert after["execution"]["state"] == ("done" if proof == "accepted" else "withdrawn")
    assert after["execution"]["recoverable_input"] is (proof == "rejected")
    terminal = await database.get_committed_terminal(session.id, reply["request_id"])
    assert terminal["status"] == "interrupted"
    assert after["outcome"] == terminal
    assert len(starts) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_missing_bare_binary_keeps_an_unsent_recoverable_original(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    build = codex_app_server.build_argv
    monkeypatch.setattr(codex_app_server, "build_argv", lambda context: (["octopus-no-such-native-binary"], {"cwd": context.working_dir}))
    await submit_client_message(manager, session.id, "Keep original startup failure", [], "missing", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "missing")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["state"] == "withdrawn"
    assert status["execution"]["recoverable_input"]
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
    assert status["input"]["content"] == "Keep original startup failure"
    assert not (tmp_path / "native-business.jsonl").exists()
    monkeypatch.setattr(codex_app_server, "build_argv", build)
    await submit_client_message(manager, session.id, status["input"]["content"], [], "restored", wiki_mode=None,
                                recovery_of_client_id="missing")
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "restored"))["outcome"]["status"] == "success"


@pytest.mark.parametrize("failure", ["effort", "credential"])
async def test_configuration_failure_keeps_original_available_for_manual_recovery(native_runtime, tmp_path, failure):
    from server.routers.sessions import get_recoverable_inputs

    manager, database, session, mode, starts, events = native_runtime
    if failure == "effort":
        session.reasoning_effort = "invalid-effort"
        await database.update_session_field(session.id, reasoning_effort=session.reasoning_effort)
    else:
        await database.save_credential("disconnected", "codex", "Disconnected", "native", "unused", "2026-11-01", session.user_id)
        await database.update_credential("disconnected", needs_reconnect=True)
        session.credential_id = "disconnected"
        await database.update_session_field(session.id, credential_id=session.credential_id)
    original = await submit_client_message(manager, session.id, "Preserve unsent configuration input", [], "config", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "config")
    assert status["outcome"]["status"] == "failed"
    assert ("reasoning_effort" if failure == "effort" else "credential") in status["outcome"]["message"]
    assert status["execution"]["state"] == "withdrawn"
    assert status["execution"]["recoverable_input"]
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
    saved = await get_recoverable_inputs(manager, session.id, before=None, limit=20, user_id=session.user_id)
    assert [item["client_request_id"] for item in saved["items"]] == ["config"]
    assert not starts and not (tmp_path / "native-business.jsonl").exists()
    duplicate = await submit_client_message(manager, session.id, status["input"]["content"], [], "config", wiki_mode=None)
    assert duplicate["request_id"] == original["request_id"]
    assert not starts

    session.reasoning_effort = None
    session.credential_id = None
    await database.update_session_field(session.id, reasoning_effort=None, credential_id=None)
    await submit_client_message(manager, session.id, status["input"]["content"], [], "config-restored", wiki_mode=None,
                                recovery_of_client_id="config")
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "config-restored"))["outcome"]["status"] == "success"
    assert len(starts) == 1 and len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_stop_before_claim_keeps_unstarted_native_input_recoverable(native_runtime, tmp_path, monkeypatch):
    from server.routers.sessions import get_recoverable_inputs

    manager, database, session, mode, starts, events = native_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    claim = database.claim_client_submission

    async def wait_to_claim(*args, **kwargs):
        entered.set()
        await release.wait()
        return await claim(*args, **kwargs)

    monkeypatch.setattr(database, "claim_client_submission", wait_to_claim)
    try:
        await submit_client_message(manager, session.id, "Stop before native preparation", [], "early-stop", wiki_mode=None)
        await asyncio.wait_for(entered.wait(), 3)
        await manager.interrupt(session.id)
        status = await submission_status(manager, session.id, "early-stop")
        assert status["outcome"]["status"] == "interrupted"
        assert status["execution"]["state"] == "withdrawn"
        assert status["execution"]["recoverable_input"]
        assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
        saved = await get_recoverable_inputs(manager, session.id, before=None, limit=20, user_id=session.user_id)
        assert [item["client_request_id"] for item in saved["items"]] == ["early-stop"]
        assert not starts and not (tmp_path / "native-business.jsonl").exists()
    finally:
        release.set()
        await wait_dispatch_idle(manager, session.id)

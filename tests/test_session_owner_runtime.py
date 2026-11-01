"""Public runtime controls remain live while original resource I/O unwinds."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from server.crypto import encrypt
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.sessions.owner import SessionOwnerUnavailable
from server.users import UserManager
from server.workspace import paths_for
from tests import test_question_lifecycle as question_tests

question_host = question_tests.question_host


@pytest.mark.parametrize("operation", ["interrupt", "archive"])
async def test_runtime_text_subscriber_can_stop_its_original_request(question_host, tmp_path, monkeypatch, operation):
    manager, original, client, events = question_host
    session = await manager.create_session(original.agent_id, "Stop from runtime text", str(tmp_path), backend="codex")
    model = tmp_path / "runtime_stop_model.py"
    model.write_text(
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'runtime-stop-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'active','type':'agent_message','text':'This original turn is active.'}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: ([sys.executable, "-u", str(model)], {"cwd": context.working_dir}),
    ))
    stopped = asyncio.Event()
    replies = []

    async def stop_from_original_text(event):
        if (event.get("session_id") == session.id and event.get("type") == "assistant_text"
                and event.get("content") == "This original turn is active."):
            if operation == "interrupt":
                replies.append(await manager.interrupt(session.id))
            else:
                await manager.archive_session(session.id, replace=False)
                replies.append("archived")
            stopped.set()

    manager.on_broadcast("stop-from-original-runtime-text", stop_from_original_text)
    receipt = await manager.start_message(session.id, "Keep this request until I stop it.")
    try:
        await asyncio.wait_for(stopped.wait(), 2)
        completed = await asyncio.wait_for(receipt.wait(), 2)
        assert replies == ([True] if operation == "interrupt" else ["archived"])
        assert completed.outcome is not None and completed.outcome.status == "interrupted"
        assert completed.outcome.code == "interrupted"
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert detail.status_code == 200
        assert detail.json()["archived"] is (operation == "archive")
        messages = detail.json()["messages"]
        assert [row["content"] for row in messages if row["role"] == "user"] == [
            "Keep this request until I stop it.",
        ]
        assert [row["content"] for row in messages if row["type"] == "text" and row["role"] == "assistant"] == [
            "This original turn is active.",
        ]
        terminal = [row for row in messages if row["type"] == "result"]
        assert len(terminal) == 1 and terminal[0]["content"]["status"] == "interrupted"
        assert len([event for event in events if event.get("session_id") == session.id and event["type"] == "result"]) == 1
        await asyncio.wait_for(manager.drain_turns(timeout=2), 3)
    finally:
        await manager.interrupt(session.id)


async def test_initial_stdout_is_delivered_before_a_blocked_initial_stdin_drains(
    question_host, tmp_path, monkeypatch,
):
    manager, original, client, _events = question_host
    session = await manager.create_session(original.agent_id, "Original stdin handshake", str(tmp_path), backend="codex")
    allow_input = tmp_path / "allow-original-input"
    saved_input = tmp_path / "original-input.json"
    model = tmp_path / "initial_handshake_model.py"
    model.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "print(json.dumps({'type':'thread.started','thread_id':'initial-handshake-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'Original stdout is ready before input drains.'}}),flush=True)\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
        "Path(sys.argv[2]).write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(allow_input), str(saved_input)],
            {"cwd": context.working_dir},
        ),
    ))
    ready = asyncio.Event()

    async def receive_original_stdout(event):
        if (event.get("session_id") == session.id and event.get("type") == "assistant_text"
                and event.get("content") == "Original stdout is ready before input drains."):
            ready.set()

    manager.on_broadcast("observe-original-stdin-handshake", receive_original_stdout)
    # UTF-8 fits the existing spill threshold. The actual JSON wire escapes
    # these characters, exceeding the unread pipe/transport budget.
    prompt = "问" * (32 * 1024)
    receipt = await manager.start_message(session.id, prompt)
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert not saved_input.exists(), "The child read input before the stdout observation."
        allow_input.touch()
        completed = await asyncio.wait_for(receipt.wait(), 3)
        assert completed.outcome is not None and completed.outcome.status == "success"
        assert json.loads(saved_input.read_text())["message"]["content"] == prompt
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert [row["content"] for row in detail.json()["messages"] if row["role"] == "user"] == [prompt]
        assert len([row for row in detail.json()["messages"] if row["type"] == "result"]) == 1
    finally:
        allow_input.touch()
        await manager.interrupt(session.id)


async def test_preparation_failure_keeps_original_credential_diagnostics(question_host, tmp_path, monkeypatch, caplog):
    manager, original, client, _events = question_host
    secret = "originalpreparationcredentialproof"
    context = "The provider catalog could not be read during preparation"
    failure = OSError(errno.EIO, f"{context}; {secret}")
    cause = RuntimeError(f"Original catalog context: {secret}")
    await manager.db.save_credential(
        "original-preparation-credential", "claude-code", "Preparation diagnostic credential", "api_key",
        encrypt(secret, await UserManager(manager.db).data_key_for(original.user_id)), "2026-10-21T00:00:00+00:00",
        user_id=original.user_id,
    )
    session = await manager.create_session(
        original.agent_id, "Original preparation diagnostic", str(tmp_path),
        backend="claude-code", credential_id="original-preparation-credential",
    )
    credentials = []

    def refuse_catalog(credential):
        credentials.append(credential.secret)
        raise failure from cause

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, model_settings=refuse_catalog))
    prompt = "Keep this original input when provider preparation fails."
    with caplog.at_level(logging.ERROR, logger="server"):
        receipt = await manager.start_message(session.id, prompt)
        completed = await asyncio.wait_for(receipt.wait(), 3)
        await asyncio.wait_for(manager.drain_turns(timeout=2), 3)
    assert credentials == [secret]
    assert completed.outcome is not None and completed.outcome.status == "failed"
    assert failure.args == (errno.EIO, f"{context}; {secret}") and failure.__cause__ is cause
    diagnostic = "\n".join(logging.Formatter().format(record) for record in caplog.records
                           if record.name.startswith("server."))
    assert context in diagnostic and "Original catalog context" in diagnostic
    assert "Traceback (most recent call last)" in diagnostic and "refuse_catalog" in diagnostic
    assert session.id in diagnostic and receipt.request_id in diagnostic
    assert secret not in diagnostic and "[redacted]" in diagnostic
    detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert [row["content"] for row in detail.json()["messages"] if row["role"] == "user"] == [prompt]
    terminal = [row for row in detail.json()["messages"] if row["type"] == "result"]
    assert len(terminal) == 1 and terminal[0]["content"]["status"] == "failed"


async def test_owner_failure_waits_for_the_original_prompt_spill_thread(question_host, tmp_path, monkeypatch):
    manager, original, client, _events = question_host
    session = await manager.create_session(
        original.agent_id, "Original prompt writer", str(tmp_path), backend="codex",
    )
    spill_root = paths_for(session.user_id).large_prompts
    spawned = tmp_path / "model-started"
    model = tmp_path / "must_not_start.py"
    model.write_text("from pathlib import Path\nPath(__file__).with_name('model-started').touch()\n")
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        build_turn_argv=lambda context: ([sys.executable, str(model)], {"cwd": context.working_dir}),
    ))
    loop = asyncio.get_running_loop()
    writer_entered = asyncio.Event()
    release_writer = threading.Event()
    write_text = Path.write_text

    def hold_original_write(path, data, *args, **kwargs):
        if path.parent == spill_root / session.id and path.suffix == ".tmp":
            loop.call_soon_threadsafe(writer_entered.set)
            assert release_writer.wait(5), "The original spill writer was never released."
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", hold_original_write)
    read_notice = manager.db.has_delivery_notice

    async def fail_after_original_read(session_id, delivery_id):
        assert await read_notice(session_id, delivery_id) is False
        raise RuntimeError("Original owner failed while its prompt writer was still running")

    prompt = "Keep these original bytes while the worker finishes.\n" * 4096
    receipt = await manager.start_message(session.id, prompt)
    draining = None
    try:
        await asyncio.wait_for(writer_entered.wait(), 2)
        monkeypatch.setattr(manager.db, "has_delivery_notice", fail_after_original_read)
        with pytest.raises(SessionOwnerUnavailable):
            await asyncio.wait_for(manager.retain_result_notice(
                session.id, "original-thread-failure", "Do not replace the original prompt writer.",
            ), 2)
        draining = asyncio.create_task(manager.close_session_owners())
        done, _ = await asyncio.wait({draining}, timeout=.05)
        assert not done, "Owner drain completed while the original file writer was still running."
        assert not spawned.exists()
        release_writer.set()
        await asyncio.wait_for(draining, 2)
        completion = await asyncio.wait_for(receipt.wait(), 2)
        assert completion.state == "unavailable" and completion.outcome is None
        assert completion.notification_state == "unavailable"
        spills = list((spill_root / session.id).glob("*.txt"))
        assert len(spills) == 1 and spills[0].read_text() == prompt
        assert not list((spill_root / session.id).glob("*.tmp"))
        assert not spawned.exists()
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert detail.status_code == 200
        assert [row["content"] for row in detail.json()["messages"] if row["role"] == "user"] == [prompt]
        assert not [row for row in detail.json()["messages"] if row["type"] == "result"]
    finally:
        release_writer.set()
        monkeypatch.setattr(manager.db, "has_delivery_notice", read_notice)
        if draining is not None:
            await asyncio.wait_for(draining, 2)


async def test_runtime_publication_failure_uses_its_original_credential(question_host, tmp_path, monkeypatch, caplog):
    manager, original, client, _events = question_host
    secret = "originalpublicationcredentialproof"
    detail = "Original response transport was cancelled while publishing its prefix"
    failure = asyncio.CancelledError(f"{detail}; {secret}")
    cause = RuntimeError(f"Original publication cause: {secret}")
    await manager.db.save_credential(
        "original-publication-credential", "claude-code", "Publication diagnostic credential", "api_key",
        encrypt(secret, await UserManager(manager.db).data_key_for(original.user_id)), "2026-10-21T00:00:00+00:00",
        user_id=original.user_id,
    )
    session = await manager.create_session(
        original.agent_id, "Original publication diagnostic", str(tmp_path),
        backend="claude-code", credential_id="original-publication-credential",
    )
    model = tmp_path / "publication_model.py"
    model.write_text(
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'assistant','session_id':'publication-thread','message':{'content':[{'type':'text','text':'Keep the original response body.'}]}}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'publication-thread','result':'Keep the original response body.','num_turns':1}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    credentials = []

    def offline_argv(context):
        credentials.append(context.credential.secret)
        return [sys.executable, "-u", str(model)], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True, build_turn_argv=offline_argv,
    ))

    async def cancel_original_publication(event):
        if event.get("session_id") == session.id and event.get("type") == "assistant_text":
            raise failure from cause

    manager.on_broadcast("original-publication-failure", cancel_original_publication)
    prompt = "Keep this original input and its committed result when publication fails."
    with caplog.at_level(logging.ERROR, logger="server"):
        receipt = await manager.start_message(session.id, prompt)
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert completion.outcome.status == "success" and completion.notification_state == "failed"
        await asyncio.wait_for(manager.drain_turns(timeout=2), 3)
    assert credentials == [secret]
    assert failure.args == (f"{detail}; {secret}",) and failure.__cause__ is cause
    diagnostic = "\n".join(logging.Formatter().format(record) for record in caplog.records
                           if record.name.startswith("server."))
    assert detail in diagnostic and "Original publication cause" in diagnostic
    assert "Traceback (most recent call last)" in diagnostic and "cancel_original_publication" in diagnostic
    assert secret not in diagnostic and "[redacted]" in diagnostic
    detail_response = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert detail_response.status_code == 200
    messages = detail_response.json()["messages"]
    assert [row["content"] for row in messages if row["role"] == "user"] == [prompt]
    assert [row["content"] for row in messages if row["type"] == "text" and row["role"] == "assistant"] == [
        "Keep the original response body.",
    ]
    terminal = [row for row in messages if row["type"] == "result"]
    assert len(terminal) == 1 and terminal[0]["content"]["status"] == "success"

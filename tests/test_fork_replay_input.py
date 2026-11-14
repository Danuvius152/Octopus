"""Public first-turn dispatch consumes only proven resumable native input."""

import json
import re

import pytest

from server.sessions.client_submissions import submit_client_message
from server.workspace import paths_for
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inputs
from tests.native_app_server import inject, item, terminal, until, wire_frames
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


async def branch(manager, parent):
    for seq, (role, text) in enumerate([("user", "Parent context"), ("assistant", "Parent answer"), ("user", "Edit me")]):
        await manager.db.append_message(session_id=parent.id, seq=seq, role=role, type="text", content=text)
    parent._message_count = 3
    return await manager.fork_session(parent.id, 2)


async def test_ack_and_success_without_saved_replay_cannot_enable_replay_again(native_runtime, tmp_path):
    manager, db, parent, mode, _, _ = native_runtime
    child = await branch(manager, parent)
    mode[0] = "control_output"
    await submit_client_message(manager, child.id, "First input", [], "first", wiki_mode=None)
    await until(lambda: child._run.backend is not None and child._run.backend.active_turn is not None)
    inject(tmp_path, item("exact-native-thread", "turn-1", "answer", "Response without persistence"), terminal())
    await wait_dispatch_idle(manager, child.id)
    assert child.fork_needs_replay is True
    assert json.loads(child.fork_metadata)["prefilled_prompt"] == "Edit me"
    before = len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"])
    await submit_client_message(manager, child.id, "Next input", [], "next", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    assert len([f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]) == before
    stored = await db.get_client_submission(child.id, "next")
    assert "replay" in str(stored["terminal"]).lower()


async def test_stop_commits_installed_replay_without_a_native_result(native_runtime, tmp_path):
    manager, db, parent, mode, _, _ = native_runtime
    child = await branch(manager, parent)
    mode[0] = "replay_installed_hold"
    await submit_client_message(manager, child.id, "Stopped after installation", [], "first", wiki_mode=None)
    await until(lambda: (tmp_path / "native-history.jsonl").exists()
                and "internal_chat_message_metadata_passthrough" in (tmp_path / "native-history.jsonl").read_text())
    await manager.interrupt(child.id)
    await wait_dispatch_idle(manager, child.id)
    await manager.await_session_execution(child.id, timeout=5)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == "interrupted"
    saved = next(row for row in await db.load_sessions() if row["id"] == child.id)
    assert saved["fork_needs_replay"] == 0, original["native_inputs"]
    assert await db.get_session_resume_id(child.id) == "exact-native-thread"


async def test_claude_stop_commits_installed_opening_without_a_result(claude_runtime, tmp_path, monkeypatch):
    manager, db, parent, _, _ = claude_runtime
    config = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    child = await branch(manager, parent)
    (tmp_path / "claude-plan.json").write_text(json.dumps({"save_input_history": True, "hold_after_history": True}))
    await submit_client_message(manager, child.id, "Stopped opening", [], "first", wiki_mode=None)
    await until(lambda: bool(list(config.glob("projects/*/exact-claude-session.jsonl"))))
    await manager.interrupt(child.id)
    await wait_dispatch_idle(manager, child.id)
    await manager.await_session_execution(child.id, timeout=5)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == "interrupted"
    saved = next(row for row in await db.load_sessions() if row["id"] == child.id)
    assert saved["fork_needs_replay"] == 0
    assert await db.get_session_resume_id(child.id) == "exact-claude-session"


@pytest.mark.parametrize("retire", ["stop", "archive"])
async def test_claude_held_result_retains_late_history_proof(claude_runtime, tmp_path, monkeypatch, retire):
    manager, db, parent, _, _ = claude_runtime
    config = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    child = await branch(manager, parent)
    await submit_client_message(manager, child.id, "Opening with late history", [], "late", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    original = child._run.backend
    assert original is not None and original.is_alive()
    saved = next(row for row in await db.load_sessions() if row["id"] == child.id)
    assert saved["fork_needs_replay"] == 1

    # The native result preceded installation. Only EOF/retirement can now
    # record that this original input consumed the branch replay.
    delivered = (await inputs(tmp_path, 1))[0]["input"]
    directory = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", child.working_dir)
    directory.mkdir(parents=True)
    (directory / "exact-claude-session.jsonl").write_text(json.dumps({
        "type": "user", "uuid": delivered["uuid"], "sessionId": "exact-claude-session",
        "isSidechain": False, "message": delivered["message"],
    }) + "\n")
    if retire == "archive":
        await manager.archive_session(child.id, replace=False)
    else:
        await manager.interrupt(child.id)
        await original.wait_retirement()
    saved = next(row for row in await db.load_sessions(include_archived=True) if row["id"] == child.id)
    assert saved["fork_needs_replay"] == 0
    assert await db.get_session_resume_id(child.id) == "exact-claude-session"
    assert original.retirement_state == "proven"


async def test_unknown_installation_remains_blocked_after_restart(native_runtime, tmp_path):
    manager, db, parent, mode, _, _ = native_runtime
    child = await branch(manager, parent)
    mode[0] = "unknown_disconnect"
    await submit_client_message(manager, child.id, "Original input", [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    await manager.stop_all_held_processes()
    await manager.close_session_owners()
    await manager.initialize(db, previous_writers_retired=True)
    child = manager.get_session(child.id)
    assert child.fork_needs_replay is True
    before = len(wire_frames(tmp_path))
    await submit_client_message(manager, child.id, "Inspect continuation", [], "next", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    assert len(wire_frames(tmp_path)) == before
    first = await db.get_client_submission(child.id, "first")
    assert first["delivery_state"] == "unknown"
    assert first["native_inputs"][0]["state"] == "unknown"


@pytest.mark.parametrize("installed", [False, True])
async def test_transient_recovery_requires_saved_original_replay(native_runtime, tmp_path, monkeypatch, installed):
    manager, db, parent, mode, _, _ = native_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    child = await branch(manager, parent)
    mode[0] = "control_replay_installed" if installed else "control_output"
    await submit_client_message(manager, child.id, "Original replay", [], "first", wiki_mode=None)
    await until(lambda: child._run.backend is not None and child._run.backend.active_turn is not None)
    if installed:
        await until(lambda: "internal_chat_message_metadata_passthrough" in (tmp_path / "native-history.jsonl").read_text())
    mode[0] = "replay_installed"  # Only a generic retry would be saved.
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Provider at capacity"}
    inject(tmp_path, failure)
    await wait_dispatch_idle(manager, child.id)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == ("success" if installed else "failed")
    assert child.fork_needs_replay is (not installed)
    assert len([frame for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]) == (2 if installed else 1)
    assert original["terminal"]["recovery"]["transient"] == int(installed)


@pytest.mark.parametrize("installed", [False, True])
async def test_claude_transient_recovery_requires_saved_original_replay(claude_runtime, tmp_path, monkeypatch, installed):
    from tests.claude_runtime import inject as claude_inject

    manager, db, parent, _, _ = claude_runtime
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    child = await branch(manager, parent)
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"hold_after_history": True, "save_input_history": installed}))
    await submit_client_message(manager, child.id, "Original replay", [], "first", wiki_mode=None)
    first = (await inputs(tmp_path, 1))[0]["input"]
    await until(lambda: child.claude_session_id == "exact-claude-session")
    plan.write_text(json.dumps({"save_input_history": True}))
    claude_inject(tmp_path, {"type": "result", "subtype": "error_during_execution", "is_error": True,
        "session_id": "exact-claude-session", "parent_tool_use_id": None,
        "user_message_uuid": first["uuid"], "user_message_uuids": [first["uuid"]],
        "errors": ["API Error: 503 Service unavailable"]})
    await wait_dispatch_idle(manager, child.id)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == ("success" if installed else "failed")
    assert child.fork_needs_replay is (not installed)
    assert len(await inputs(tmp_path, 1)) == (2 if installed else 1)
    assert original["terminal"]["recovery"]["transient"] == int(installed)


async def test_final_frame_revalidates_native_configuration_and_preserves_unsent_replay(native_runtime, tmp_path, monkeypatch):
    manager, db, parent, _, _, _ = native_runtime
    from server.harness import codex_rpc
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", 45000)
    child = await branch(manager, parent)
    defaults = tmp_path / "native-defaults.json"
    defaults.write_text(json.dumps({"model": "model-" + "x" * 30000, "effort": "low"}))
    text = '中"\\\n' * 3000
    await submit_client_message(manager, child.id, text, [], "oversize", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    original = await db.get_client_submission(child.id, "oversize")
    assert original["terminal"]["status"] == "failed"
    assert "Shorten the new input" in str(original["terminal"])
    assert original["delivery_state"] == "none"
    assert original["native_inputs"][0]["state"] == "not_sent"
    assert child.fork_needs_replay is True
    assert not any(f.get("method") == "turn/start" for f in wire_frames(tmp_path))
    # Native defaults are resolved again for this new input, not inherited from
    # the rejected input's preparation or silently trimmed out of its frame.
    defaults.write_text(json.dumps({"model": "fixture", "effort": "low"}))
    await submit_client_message(manager, child.id, text, [], "fits", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    sent = [f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(sent) == 1
    assert text in sent[0]["params"]["input"][0]["text"]
    assert "Parent context" in sent[0]["params"]["input"][0]["text"]
    assert sent[0]["params"]["collaborationMode"]["settings"]["model"] == "fixture"


async def test_actual_utf8_frame_boundary_includes_enhancements_but_not_other_rpcs(native_runtime, tmp_path, monkeypatch):
    from server.attachments import save_upload
    from server.harness import codex_rpc

    manager, db, parent, mode, _, _ = native_runtime
    mode[0] = "replay_installed"
    # The peer's first native ID has no numeric suffix. Start it separately so
    # the measured branches use real, equally wide -2/-3/-4 native identities.
    warmup = await manager.create_session(parent.agent_id, "Warmup", str(tmp_path), backend="codex")
    await submit_client_message(manager, warmup.id, "Warmup", [], "warmup", wiki_mode=None)
    await wait_dispatch_idle(manager, warmup.id)
    first = await branch(manager, parent)
    text = '中"\\\n' * 5000

    async def send(child, client_id):
        await manager.update_session_configuration(child.id, {"model": "fixture", "reasoning_effort": "low"})
        mode[0] = "rejected"
        await submit_client_message(manager, child.id, "Refuse before acceptance", [], "unsent", wiki_mode=None)
        await wait_dispatch_idle(manager, child.id)
        refused = await db.get_client_submission(child.id, "unsent")
        assert refused["native_inputs"][0]["state"] == "rejected"
        assert child.claude_session_id  # The next preflight knows its real identity.
        mode[0] = "replay_installed"
        attachment = save_upload(child.id, "source.txt", b"Attachment content", user_id=child.user_id)
        await submit_client_message(manager, child.id, text, [attachment.id], client_id, wiki_mode="read")
        await wait_dispatch_idle(manager, child.id)
        return await db.get_client_submission(child.id, client_id), attachment

    original, attachment = await send(first, "measure")
    assert original["terminal"]["status"] == "success"
    frames = (tmp_path / "native-stdin.jsonl").read_bytes().splitlines(keepends=True)
    raw = next(line for line in frames if json.loads(line).get("method") == "turn/start"
               and json.loads(line)["params"]["clientUserMessageId"] == original["request_id"])
    delivered = json.loads(raw)["params"]["input"][0]["text"]
    assert text in delivered and "Parent context" in delivered and str(attachment.path) in delivered
    assert '中'.encode() in raw and b'\\\\' in raw and b'\\n' in raw and b'\\"' in raw
    assert max(len(line) for line in frames) == len(raw) < sum(map(len, frames))

    # Identity values differ, but their real encoded widths and the complete
    # copied history/augmentation/configuration are the same on each branch.
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", len(raw))
    exact_parent = await manager.create_session(parent.agent_id, parent.name, str(tmp_path), backend="codex")
    exact = await branch(manager, exact_parent)
    exact_result, _ = await send(exact, "exact")
    assert exact_result["terminal"]["status"] == "success"
    starts = [line for line in (tmp_path / "native-stdin.jsonl").read_bytes().splitlines(keepends=True)
              if json.loads(line).get("method") == "turn/start"
              and text in json.loads(line)["params"]["input"][0]["text"]]
    assert len(starts) == 2 and len(starts[-1]) == len(raw)

    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", len(raw) - 1)
    oversized_parent = await manager.create_session(parent.agent_id, parent.name, str(tmp_path), backend="codex")
    oversized = await branch(manager, oversized_parent)
    rejected, _ = await send(oversized, "one-byte-over")
    assert rejected["terminal"]["status"] == "failed" and rejected["delivery_state"] == "none"
    assert rejected["native_inputs"][0]["state"] == "not_sent"
    assert oversized.fork_needs_replay is True
    assert len([line for line in (tmp_path / "native-stdin.jsonl").read_bytes().splitlines()
                if json.loads(line).get("method") == "turn/start"
                and text in json.loads(line)["params"]["input"][0]["text"]]) == 2


@pytest.mark.parametrize("full_copy", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_saved_replay_resumes_without_wrapping_again(native_runtime, tmp_path, full_copy, failed):
    manager, db, parent, mode, _, _ = native_runtime
    child = await branch(manager, parent)
    if full_copy:
        metadata = {**json.loads(child.fork_metadata), "full_copy": True, "duplicated_from": parent.id}
        child.fork_metadata = json.dumps(metadata)
        child.fork_revert_record = '{"status":"applied"}'
        await db.update_session_field(child.id, fork_metadata=child.fork_metadata, fork_revert_record=child.fork_revert_record)
    mode[0] = "replay_installed_failure" if failed else "replay_installed"
    await submit_client_message(manager, child.id, "First input", [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == ("failed" if failed else "success")
    assert child.fork_needs_replay is False
    saved = next(row for row in await db.load_sessions() if row["id"] == child.id)
    assert saved["fork_needs_replay"] == 0
    assert saved["claude_session_id"] == "exact-native-thread"
    assert (json.loads(saved["fork_metadata"]) if saved["fork_metadata"] else None) == (
        {"full_copy": True, "duplicated_from": parent.id} if full_copy else None)
    assert saved["fork_revert_record"] == ('{"status":"applied"}' if full_copy else None)
    mode[0] = "normal"
    await submit_client_message(manager, child.id, "Next input", [], "next", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    frames = [f for f in wire_frames(tmp_path) if f.get("method") == "turn/start"]
    assert len(frames) == 2
    assert "Parent context" in frames[0]["params"]["input"][0]["text"]
    assert "<fork-history" not in frames[1]["params"]["input"][0]["text"]
    messages = await db.load_messages(child.id)
    assert next(m for m in messages if (m.get("tool_input") or {}).get("request_context", {}).get("request_id")
        == original["request_id"])["content"] == "First input"


@pytest.mark.parametrize("spill", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_claude_installed_input_retains_stream_json_and_spill(claude_runtime, tmp_path, monkeypatch, spill, failed):
    manager, db, parent, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    child = await branch(manager, parent)
    text = '中文"\\' * 30000 if spill else "First input"
    plan = tmp_path / "claude-plan.json"
    plan.write_text(json.dumps({"save_input_history": True, "failure": failed}))
    await submit_client_message(manager, child.id, text, [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    original = await db.get_client_submission(child.id, "first")
    assert original["terminal"]["status"] == ("failed" if failed else "success")
    assert child.fork_needs_replay is False
    assert child.claude_session_id == "exact-claude-session"
    assert original["native_inputs"] == []  # Claude keeps its own protocol contract.
    first = (await inputs(tmp_path, 1))[0]["input"]["message"]["content"]
    if spill:
        assert "[octopus-large-prompt]" in first
        files = list((paths_for(child.user_id).large_prompts / child.id).glob("*.txt"))
        assert len(files) == 1
        assert "Parent context" in files[0].read_text()
        assert text in files[0].read_text()
    else:
        assert "Parent context" in first
    plan.write_text(json.dumps({"save_input_history": True}))
    # A lost read-model notification must not make the next input replay history.
    child.fork_needs_replay = True
    child.fork_metadata = '{"prefilled_prompt":"stale hint"}'
    await submit_client_message(manager, child.id, "Next input", [], "next", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    second = (await inputs(tmp_path, 2))[1]["input"]["message"]["content"]
    assert "<fork-history" not in second
    assert "Next input" in second

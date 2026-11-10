"""Rewind refuses known unsendable history before publishing any effects."""

import json
import subprocess
import time

import pytest

from server.crypto import encrypt
from server.harness import codex_rpc
from server.oauth_providers import get_provider
from server.sessions import ForkError
from server.sessions.client_submissions import submit_client_message
from server.submission_models import NativeInputEvidence
from server.users import UserManager
from server.workspace import paths_for
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inputs
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


async def test_700_chinese_messages_refuse_before_child_revert_archive_and_spawn(native_runtime, tmp_path):
    manager, db, parent, _, starts, events = native_runtime
    for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    project_file = tmp_path / "project.txt"
    project_file.write_text("Original\n")
    subprocess.run(["git", "add", "project.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "Initial"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    for seq in range(700):
        await db.append_message(session_id=parent.id, seq=seq, role="assistant", type="text", content="中" * 2000)
    await db.append_message(session_id=parent.id, seq=700, role="user", type="text", content="Edit me",
                            git_head=head, git_status_clean=True)
    await db.append_message(session_id=parent.id, seq=701, role="assistant", type="tool_use", content="",
                            tool_name="Write", tool_input={"file_path": str(project_file)})
    parent.restore_message_state(702, 702, 0)
    project_file.write_text("Keep current work\n")
    before = await db.load_sessions(include_archived=True)
    events.clear()

    with pytest.raises(ForkError) as refused:
        await manager.fork_session(parent.id, 700, revert_files=True)

    assert refused.value.reason == "replay_capacity_exceeded"
    assert "4096" in str(refused.value)
    assert await db.load_sessions(include_archived=True) == before
    assert manager.get_session(parent.id) is parent
    assert project_file.read_text() == "Keep current work\n"
    assert subprocess.check_output(["git", "stash", "list"], cwd=tmp_path) == b""
    assert starts == []
    assert not any(event["type"] in {"session_archived", "session_forked"} for event in events)
    await submit_client_message(manager, parent.id, "Continue original", [], "after-refusal", wiki_mode=None)
    await wait_dispatch_idle(manager, parent.id)
    result = await db.get_client_submission(parent.id, "after-refusal")
    assert result["terminal"]["status"] == "success"


async def test_selected_credential_launch_budget_is_checked_before_child(claude_runtime):
    manager, db, parent, _, _ = claude_runtime
    key = await UserManager(db).data_key_for(parent.user_id)
    await db.save_credential("oversize-key", "claude-code", "Selected", "api_key",
                             encrypt("x" * 131072, key), "now", user_id=parent.user_id)
    await manager.update_session_configuration(parent.id, {"credential_id": "oversize-key"}, parent.user_id)
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="Edit me")
    parent.restore_message_state(1, 1, 0)
    before = await db.load_sessions(include_archived=True)

    with pytest.raises(ForkError) as refused:
        await manager.fork_session(parent.id, 0, revert_files=True)

    assert refused.value.reason == "replay_capacity_exceeded"
    assert "environment item" in str(refused.value)
    assert "x" * 100 not in str(refused.value)
    assert await db.load_sessions(include_archived=True) == before
    assert manager.get_session(parent.id) is parent


@pytest.mark.parametrize("history", ["a" * 2000, "中" * 1500, '引号"反斜杠\\换行\n' * 100], ids=["ascii", "chinese", "escaped"])
async def test_encoded_reserve_boundary_and_first_turn_after_restart(native_runtime, tmp_path, monkeypatch, history):
    from server.sessions import SessionManager

    manager, db, parent, mode, _, _ = native_runtime
    mode[0] = "replay_installed"

    async def seed(session):
        await manager.update_session_configuration(session.id, {"model": "fixture", "reasoning_effort": "low"}, session.user_id)
        await db.admit_client_submission(session.id, "source", "digest", "0" * 32,
            payload={"content": history, "attachment_ids": [], "order": 1}, receipt={"accepted": True})
        await db.claim_client_submission(session.id, "0" * 32, "0" * 32, "generation")
        await db.record_native_input(session.id, "0" * 32, NativeInputEvidence(input_id="0" * 32, attempt_id="1" * 32))
        await db.complete_client_submission(session.id, "0" * 32, owner="0" * 32, state="withdrawn")
        await db.append_message(session_id=session.id, seq=0, role="user", type="text", content=history,
            tool_input={"request_context": {"request_id": "0" * 32}})
        await db.append_message(session_id=session.id, seq=1, role="assistant", type="text", content="Known context " * 100)
        await db.append_message(session_id=session.id, seq=2, role="user", type="text", content="Editable prefill")
        session.restore_message_state(3, 3, 0)

    await seed(parent)
    measured_child = await manager.fork_session(parent.id, 2)
    await submit_client_message(manager, measured_child.id, "x" * 4096, [], "measure", wiki_mode=None)
    await wait_dispatch_idle(manager, measured_child.id)
    original = await db.get_client_submission(measured_child.id, "measure")
    assert original["terminal"]["status"] == "success"
    captured = [json.loads(line) for line in (tmp_path / "native-stdin.jsonl").read_bytes().splitlines()]
    sent = next(frame for frame in captured if frame.get("method") == "turn/start")
    assert len(sent["id"]) == 40 and len(sent["params"]["clientUserMessageId"]) == 32
    delivered = sent["params"]["input"][0]["text"]
    assert 'input_observation=' in delivered and parent.id in delivered and '"state": "not_sent"' in delivered
    assert delivered.count("x") >= 4096
    # The peer uses short synthetic thread IDs; budget the real UUID width.
    sent["params"]["threadId"] = "11111111-1111-7111-8111-111111111111"
    reserve_frame = (json.dumps(sent, ensure_ascii=False) + "\n").encode()
    sent["params"]["input"][0]["text"] = delivered.replace("x" * 4096, "")
    assert len(reserve_frame) - len((json.dumps(sent, ensure_ascii=False) + "\n").encode()) == 4096

    next_parent = await manager.create_session(parent.agent_id, parent.name, str(tmp_path), backend="codex")
    await seed(next_parent)
    before = await db.load_sessions(include_archived=True)
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", len(reserve_frame) - 1)
    with pytest.raises(ForkError, match="4096"):
        await manager.fork_session(next_parent.id, 2)
    assert await db.load_sessions(include_archived=True) == before
    assert manager.get_session(next_parent.id) is next_parent

    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", len(reserve_frame))
    child = await manager.fork_session(next_parent.id, 2)
    assert child.fork_status == "ready"
    restored = SessionManager()
    await restored.initialize(db)
    try:
        await submit_client_message(restored, child.id, "x" * 4096, [], "after-restart", wiki_mode=None)
        await wait_dispatch_idle(restored, child.id)
        result = await db.get_client_submission(child.id, "after-restart")
        assert result["terminal"]["status"] == "success"
        assert restored.get_session(child.id).fork_needs_replay is False
    finally:
        await restored.close_session_owners()


@pytest.mark.parametrize("lifetime", [60, -60], ids=["near-expiry", "expired"])
async def test_rewind_reads_selected_oauth_without_refreshing(claude_runtime, monkeypatch, tmp_path, lifetime):
    manager, db, parent, _, _ = claude_runtime
    key = await UserManager(db).data_key_for(parent.user_id)
    secret = encrypt(json.dumps({"access_token": "stored-access", "refresh_token": "stored-refresh",
                                "expires_at_epoch": time.time() + lifetime}), key)
    await db.save_credential("short-oauth", "claude-code", "Selected", "oauth", secret, "now", user_id=parent.user_id)
    await manager.update_session_configuration(parent.id, {"credential_id": "short-oauth"}, parent.user_id)
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="Editable")
    parent.restore_message_state(1, 1, 0)
    before = await db.get_credential("short-oauth", parent.user_id)

    async def unexpected_refresh(_):
        pytest.fail("rewind must not refresh authentication")

    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", unexpected_refresh)
    child = await manager.fork_session(parent.id, 0)
    assert child.fork_status == "ready" and child.credential_id == "short-oauth"
    assert await db.get_credential("short-oauth", parent.user_id) == before
    assert not (tmp_path / "claude-inputs.jsonl").exists()
    assert not (paths_for(child.user_id).large_prompts / child.id).exists()


async def test_claude_large_rewind_keeps_spill_and_completes_first_turn(claude_runtime, tmp_path, monkeypatch):
    manager, db, parent, _, _ = claude_runtime
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    for seq in range(700):
        await db.append_message(session_id=parent.id, seq=seq, role="assistant", type="text", content="中" * 2000)
    await db.append_message(session_id=parent.id, seq=700, role="user", type="text", content="Editable")
    parent.restore_message_state(701, 701, 0)
    # A different backend's frame budget cannot reject Claude's stdin/spill.
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", 1000)
    child = await manager.fork_session(parent.id, 700)
    assert child.fork_status == "ready"
    assert not (paths_for(child.user_id).large_prompts / child.id).exists()
    (tmp_path / "claude-plan.json").write_text(json.dumps({"save_input_history": True}))
    await submit_client_message(manager, child.id, "Continue here", [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    assert (await db.get_client_submission(child.id, "first"))["terminal"]["status"] == "success"
    assert child.fork_needs_replay is False
    pointer = (await inputs(tmp_path, 1))[0]["input"]["message"]["content"]
    assert "[octopus-large-prompt]" in pointer
    spills = list((paths_for(child.user_id).large_prompts / child.id).glob("*.txt"))
    assert len(spills) == 1 and str(spills[0]) in pointer
    body = spills[0].read_text()
    assert "[seq 0] assistant:" in body and "[seq 699] assistant:" in body and "Continue here" in body


async def test_rewind_counts_inherited_product_controls_and_releases_after_refusal(native_runtime, monkeypatch):
    manager, db, parent, _, _, _ = native_runtime
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="Editable")
    parent.restore_message_state(1, 1, 0)
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", 45000)
    await db.update_agent(parent.agent_id, model="m" * 45000)
    before = await db.load_sessions(include_archived=True)
    with pytest.raises(ForkError):
        await manager.fork_session(parent.id, 0)
    assert await db.load_sessions(include_archived=True) == before
    assert manager.get_session(parent.id) is parent
    await db.update_agent(parent.agent_id, model="fixture")
    child = await manager.fork_session(parent.id, 0)
    assert child.fork_status == "ready" and child.model is None


async def test_ready_keeps_unknown_native_defaults_conditional(native_runtime, tmp_path, monkeypatch):
    manager, db, parent, mode, _, _ = native_runtime
    mode[0] = "replay_installed"
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="Editable")
    parent.restore_message_state(1, 1, 0)
    monkeypatch.setattr(codex_rpc, "FRAME_LIMIT_BYTES", 45000)
    native_defaults = tmp_path / "native-defaults.json"
    native_defaults.write_text(json.dumps({"model": "native-" + "m" * 45000, "effort": "low"}))
    child = await manager.fork_session(parent.id, 0)
    assert child.fork_status == "ready"
    assert not (tmp_path / "native-stdin.jsonl").exists()
    await submit_client_message(manager, child.id, "First", [], "oversize-default", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    result = await db.get_client_submission(child.id, "oversize-default")
    assert result["terminal"]["status"] == "failed" and result["native_inputs"][0]["state"] == "not_sent"
    assert child.fork_needs_replay is True
    native_defaults.write_text(json.dumps({"model": "fixture", "effort": "low"}))
    await submit_client_message(manager, child.id, "First", [], "current-default", wiki_mode=None)
    await wait_dispatch_idle(manager, child.id)
    assert (await db.get_client_submission(child.id, "current-default"))["terminal"]["status"] == "success"
    assert child.fork_needs_replay is False

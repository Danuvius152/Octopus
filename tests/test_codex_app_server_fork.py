"""Forking preserves product message boundaries and collected native history."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.harness import get_harness
from server.harness.codex import _cleanup_legacy_fork
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.forking import ForkingMixin
from tests import test_codex_app_server_submission as native_tests
from tests.test_codex_app_server_output import ROOT, child_end, child_start, item
from tests.test_codex_app_server_questions import inject, terminal, until, wire_frames
from tests.test_codex_app_server_steering import active_root, wait_until
from tests.test_runtime_review_races import wait_dispatch_idle
from tests.test_session_fork import _git

native_runtime = native_tests.native_runtime


def select_legacy_cleanup(monkeypatch):
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, fork_cleanup=_cleanup_legacy_fork))


@pytest.fixture
async def fork_source(native_runtime, tmp_path, monkeypatch):
    manager, _, parent, mode, _, _ = native_runtime
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(ForkingMixin, "_fork_copy_base", staticmethod(lambda: str(tmp_path / "copies")))
    work = tmp_path / "work"
    work.mkdir()
    parent = await manager.create_session(parent.agent_id, "Fork source", str(work), backend="codex")
    mode[0] = "control_output"
    receipt = await manager.start_message(parent.id, "Original request")
    await until(lambda: parent._run.backend is not None and parent._run.backend.active_turn is not None)
    inject(tmp_path, child_start(), item("child", "child-turn", "partial", "Historical partial"),
           item(ROOT, "turn-1", "answer", "Native answer"), terminal())
    assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    (tmp_path / "native-injections.jsonl").unlink()
    mode[0] = "normal"
    return (*native_runtime[:2], parent, *native_runtime[3:]), receipt


async def test_rewind_at_steer_uses_product_seq_and_keeps_native_sources(native_runtime, tmp_path):
    manager, database, parent, mode, starts, _ = native_runtime
    first = await active_root(native_runtime, tmp_path)
    inject(tmp_path, child_start(), item("child", "child-turn", "answer", "Child first round"), child_end(),
           child_start(turn="followup"), item("child", "followup", "partial", "Child followup partial"))

    async def collected():
        return any(row["content"] == "Child followup partial" for row in await database.load_messages(parent.id))

    await wait_until(collected)
    correction = await submit_client_message(manager, parent.id, "Change direction here", [], "correction", wiki_mode=None)

    async def steered():
        status = await submission_status(manager, parent.id, "correction")
        return any(item["state"] == "accepted" for item in status["execution"]["native_inputs"])

    await wait_until(steered)
    inject(tmp_path, item(ROOT, "turn-1", "later", "Excluded root final"), terminal())
    await wait_dispatch_idle(manager, parent.id)
    original = await database.load_messages(parent.id)
    target = next(row["seq"] for row in original if row["content"] == "Change direction here")
    branch = await manager.fork_session(parent.id, target, user_id=parent.user_id)
    assert len(starts) == 1 and branch.claude_session_id is None and branch.fork_needs_replay
    copied = await database.load_messages(branch.id)
    assert [{**row, "tool_input": {key: value for key, value in (row["tool_input"] or {}).items() if key != "copied_input"} or None}
            for row in copied] == [row for row in original if row["seq"] < target]
    assert not any(row["content"] == "Excluded root final" for row in copied)
    assert (await database.get_submission_for_request(parent.id, correction["request_id"]))["native_inputs"][0]["state"] == "accepted"
    assert (await database.read_session_result(branch.id, first["request_id"])).outcome is None
    anchor = next(row["seq"] for row in copied if row["content"] == "Child first round")
    detail = await database.read_native_details(branch.id, parent.user_id, anchor)
    assert detail.messages[0].content == "Child first round"
    (tmp_path / "native-injections.jsonl").unlink()
    mode[0] = "normal"
    receipt = await manager.start_message(branch.id, "Explicit new branch request")
    assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    sent = json.loads((tmp_path / "native-business.jsonl").read_text().splitlines()[-1])["input"][0]["text"]
    assert f"[seq {anchor}] assistant child" in sent
    assert "thread=child turn=child-turn phase=final_answer" in sent
    assert "assistant root" in sent
    assert "native_subagent" in sent and "followup" in sent
    assert "Change direction here" not in sent and "Excluded root final" not in sent
    assert sent.endswith("Explicit new branch request\n</continue-from-here>")


async def test_full_fork_saves_actual_native_id_without_starting_a_turn(fork_source, tmp_path):
    runtime, receipt = fork_source
    manager, database, parent, mode, starts, _ = runtime
    before = len((tmp_path / "native-business.jsonl").read_text().splitlines())
    branch = await manager.duplicate_session(parent.id, user_id=parent.user_id)
    assert branch.claude_session_id == "actual-native-fork"
    assert not branch.fork_needs_replay and branch.backend == parent.backend
    assert branch.user_id == parent.user_id and branch.working_dir != parent.working_dir
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == before
    fork = next(frame for frame in wire_frames(tmp_path) if frame.get("method") == "thread/fork")
    assert fork["params"]["deferGoalContinuation"] is True
    assert fork["params"]["config"]["features.goals"] is False
    assert fork["params"]["cwd"] == branch.working_dir
    assert (await database.read_session_result(branch.id, receipt.request_id)).fragments == ("Native answer",)
    continued = await manager.start_message(branch.id, "Explicit continuation")
    assert (await asyncio.wait_for(continued.wait(), 4)).outcome.status == "success"
    assert starts[-1].resume_id == branch.claude_session_id
    assert parent.claude_session_id == ROOT
    source = await manager.start_message(parent.id, "Independent source continuation")
    assert (await asyncio.wait_for(source.wait(), 4)).outcome.status == "success"
    assert starts[-1].resume_id == ROOT
    anchor = next(row["seq"] for row in await database.load_messages(branch.id) if row["content"] == "Historical partial")
    await manager.delete_session(parent.id)
    assert (await database.read_native_details(branch.id, branch.user_id, anchor)).messages[0].content == "Historical partial"
    assert (await database.read_session_result(branch.id, receipt.request_id)).fragments == ("Native answer",)


@pytest.mark.parametrize("failure", ["fork_lost_ack", "fork_error_after_creation", "fork_wrong_identity"])
async def test_unknown_creation_keeps_saga_and_never_deletes_the_hint(fork_source, tmp_path, monkeypatch, failure):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    mode[0] = failure
    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = await database.load_incomplete_forks()
    artifact = json.loads(row["fork_metadata"])["native_fork"]
    assert artifact["phase"] == "unknown" and row["resume_id"] is None
    assert (tmp_path / "actual-native-fork.jsonl").exists()
    assert manager.get_session(row["id"]) is None
    # Current selection must not turn an unknown native artifact into a
    # legacy hint-based file deletion at the next service entry.
    select_legacy_cleanup(monkeypatch)
    await manager._recover_incomplete_forks()
    assert len(await database.load_incomplete_forks()) == 1
    assert not any(frame.get("method") == "thread/delete" for frame in wire_frames(tmp_path))
    assert len([frame for frame in wire_frames(tmp_path) if frame.get("method") == "thread/fork"]) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    assert (tmp_path / "native-history.jsonl").exists()


async def test_foreign_artifact_path_is_retained_without_deleting_source_or_guessing(fork_source, tmp_path):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    mode[0] = "fork_foreign_path"
    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = await database.load_incomplete_forks()
    artifact = json.loads(row["fork_metadata"])["native_fork"]
    assert artifact["phase"] == "created" and artifact["path"] == "/foreign-store/fork.jsonl"
    assert Path(row["working_dir"]).exists() and (tmp_path / "native-history.jsonl").exists()
    assert not any(frame.get("method") == "thread/delete" for frame in wire_frames(tmp_path))


@pytest.mark.parametrize("fault", ["turn", "item", "request", "unreadable"])
async def test_untrusted_fork_after_ack_keeps_its_effects_and_disables_cold_cleanup(fork_source, tmp_path, monkeypatch, fault):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    mode[0] = f"fork_untrusted_{fault}"
    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = await database.load_incomplete_forks()
    artifact = json.loads(row["fork_metadata"])["native_fork"]
    assert artifact["phase"] == "created" and artifact["thread_id"] == "actual-native-fork"
    assert artifact["cleanup_allowed"] is False
    assert row["resume_id"] == "actual-native-fork" and manager.get_session(row["id"]) is None
    assert (Path(row["working_dir"]) / "unexpected-native-work.txt").read_text() == "Retain this unexpected effect\n"
    assert (tmp_path / "actual-native-fork.jsonl").exists()
    assert (tmp_path / "native-history.jsonl").exists()
    assert not manager.session_has_resources(parent.id)
    assert not any(frame.get("method") == "thread/delete" for frame in wire_frames(tmp_path))
    select_legacy_cleanup(monkeypatch)
    mode[0] = "normal"
    await manager.close_session_owners()
    cold = SessionManager()
    try:
        await cold.initialize(database)
        retained, = await database.load_incomplete_forks()
        assert retained["id"] == row["id"]
        assert (Path(row["working_dir"]) / "unexpected-native-work.txt").exists()
        assert (tmp_path / "actual-native-fork.jsonl").exists()
        assert not any(frame.get("method") == "thread/delete" for frame in wire_frames(tmp_path))
    finally:
        await cold.close_session_owners()
    assert len([frame for frame in wire_frames(tmp_path) if frame.get("method") == "thread/fork"]) == 1
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_restart_cleans_only_acknowledged_artifact_using_saved_strategy(fork_source, tmp_path, monkeypatch):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    update = database.update_session_field

    async def fail_install(session_id, **fields):
        if fields.get("fork_status") == "ready":
            raise OSError("Lost final product installation")
        await update(session_id, **fields)

    monkeypatch.setattr(database, "update_session_field", fail_install)
    with pytest.raises(OSError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    monkeypatch.setattr(database, "update_session_field", update)
    row, = await database.load_incomplete_forks()
    assert row["resume_id"] == "actual-native-fork"
    mode[0] = "fork_cleanup_failed"
    select_legacy_cleanup(monkeypatch)
    await manager.close_session_owners()
    cold = SessionManager()
    try:
        await cold.initialize(database)
        assert len(await database.load_incomplete_forks()) == 1
        assert (tmp_path / "actual-native-fork.jsonl").exists()
        mode[0] = "normal"
        await cold._recover_incomplete_forks()
        assert not await database.load_incomplete_forks()
        assert not (tmp_path / "actual-native-fork.jsonl").exists()
        assert not Path(row["working_dir"]).exists()
    finally:
        await cold.close_session_owners()
    assert (tmp_path / "native-history.jsonl").exists()
    assert {frame["params"]["threadId"] for frame in wire_frames(tmp_path) if frame.get("method") == "thread/delete"} == {"actual-native-fork"}
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("artifact", [None, {}, [], {"transport": "exec"}])
async def test_corrupt_native_fork_checkpoint_never_uses_legacy_cleanup(
    fork_source, tmp_path, monkeypatch, artifact,
):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    mode[0] = "fork_lost_ack"
    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = await database.load_incomplete_forks()
    metadata = json.loads(row["fork_metadata"])
    metadata["native_fork"] = artifact
    # A retained hint still must not become permission for file cleanup.
    hint = "retained-native-hint"
    saved = tmp_path / "sessions" / f"rollout-fork-{hint}.jsonl"
    saved.parent.mkdir(exist_ok=True)
    saved.write_text("Retained uncertain provider effects\n")
    await database.update_session_field(row["id"], resume_id=hint, fork_metadata=json.dumps(metadata))
    select_legacy_cleanup(monkeypatch)
    await manager._recover_incomplete_forks()
    retained, = await database.load_incomplete_forks()
    assert retained["id"] == row["id"]
    assert saved.exists() and Path(row["working_dir"]).exists()
    assert not any(frame.get("method") == "thread/delete" for frame in wire_frames(tmp_path))


async def test_branch_waits_for_its_original_management_host_retirement(fork_source, tmp_path, monkeypatch):
    from server.harness.codex_history import _HistoryDriver

    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    finish = _HistoryDriver.finish_observations

    async def hold_exit(driver):
        await finish(driver)
        entered.set()
        await release.wait()

    monkeypatch.setattr(_HistoryDriver, "finish_observations", hold_exit)
    operation = asyncio.create_task(manager.duplicate_session(parent.id, user_id=parent.user_id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row, = await database.load_incomplete_forks()
        assert parent._forking and manager.get_session(row["id"]) is None and not operation.done()
        assert json.loads(row["fork_metadata"])["native_fork"]["cleanup_allowed"] is False
        assert manager.session_has_resources(parent.id)
        assert parent._run.backend is None or not parent._run.backend.question_admission_open
        assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    finally:
        release.set()
    branch = await asyncio.wait_for(operation, 4)
    assert branch.fork_status == "ready" and not parent._forking
    assert json.loads(branch.fork_metadata)["native_fork"]["cleanup_allowed"] is True


async def test_fork_keeps_unknown_input_as_history_without_admitting_its_replay(fork_source, tmp_path):
    runtime, _ = fork_source
    manager, database, parent, mode, _, _ = runtime
    mode[0] = "unknown"
    unknown = await manager.start_message(parent.id, "Possibly already performed")
    assert (await asyncio.wait_for(unknown.wait(), 4)).outcome.status == "failed"
    assert (await database.read_session_result(parent.id, unknown.request_id)).missing == "input_unconfirmed"
    mode[0] = "normal"
    branch = await manager.duplicate_session(parent.id, user_id=parent.user_id)
    assert (await database.read_session_result(branch.id, unknown.request_id)).missing == "input_unconfirmed"
    copied = next(row for row in await database.load_messages(branch.id) if row["content"] == "Possibly already performed")
    assert copied["tool_input"]["copied_input"]["native_inputs"][0]["state"] == "unknown"
    assert await database.get_submission_for_request(branch.id, unknown.request_id) is None
    assert not manager.dispatch_snapshot(branch.id)["items"]
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("complete_only", [False, True])
@pytest.mark.parametrize("child", [False, True])
async def test_native_patch_is_attributed_before_real_rewind_file_restore(native_runtime, tmp_path, rename, complete_only, child):
    manager, _, parent, mode, _, _ = native_runtime
    repo = tmp_path / "git-work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    file = repo / "a.py"
    file.write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Initial state")
    parent = await manager.create_session(parent.agent_id, "Native file change", str(repo), backend="codex")
    mode[0] = "control_output"
    receipt = await manager.start_message(parent.id, "Change the file")
    await until(lambda: parent._run.backend is not None and parent._run.backend.active_turn is not None)
    destination = repo / "改名 \n -> file.py" if rename else file
    if rename:
        file.rename(destination)
    destination.write_text("native patch\n")
    if rename:
        _git(repo, "add", "-A")
    changes = [{"path": str(file), "kind": {"type": "update", "move_path": str(destination) if rename else None}, "diff": "+native patch"}]
    if child:
        inject(tmp_path, child_start())
    phases = [("completed", "completed")] if complete_only else [("started", "inProgress"), ("completed", "completed")]
    for phase, status in phases:
        inject(tmp_path, {"method": f"item/{phase}", "params": {
            "threadId": "child" if child else ROOT, "turnId": "child-turn" if child else "turn-1",
            "item": {"id": "patch", "type": "fileChange", "changes": changes, "status": status}}})
    inject(tmp_path, terminal())
    assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    preview = await manager.fork_preview(parent.id, 0, user_id=parent.user_id)
    assert set(preview["side_effect_summary"]["agent_touched_paths"]) == {str(file), str(destination)}
    assert all(edit["turns"] == 1 for edit in preview["side_effect_summary"]["file_edits"])
    assert preview["revert"]["available"]
    branch = await manager.fork_session(parent.id, 0, revert_files=True, user_id=parent.user_id)
    record = json.loads(branch.fork_revert_record)
    assert record["status"] == "completed" and record["stash_ref"]
    assert file.read_text() == "original\n"
    if rename:
        assert not destination.exists()


async def test_native_patch_never_authorizes_reverting_unrelated_user_edits(native_runtime, tmp_path):
    manager, _, parent, mode, _, _ = native_runtime
    repo = tmp_path / "git-work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    file, user_file = repo / "a.py", repo / "human.py"
    file.write_text("original\n")
    user_file.write_text("keep original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Initial state")
    parent = await manager.create_session(parent.agent_id, "Native file change", str(repo), backend="codex")
    mode[0] = "control_output"
    receipt = await manager.start_message(parent.id, "Change a.py")
    await until(lambda: parent._run.backend is not None and parent._run.backend.active_turn is not None)
    file.write_text("native patch\n")
    # A connector can return JSON resembling a native patch. It remains tool
    # text; only the actual native fileChange producer can identify that kind.
    fake = {"threadId": ROOT, "turnId": "turn-1", "item": {"id": "connector", "type": "fileChange",
        "changes": [{"path": str(user_file), "kind": {"type": "update"}}]}}
    inject(tmp_path,
        {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "id": "patch", "type": "fileChange", "status": "completed", "changes": [
                {"path": str(file), "kind": {"type": "update", "move_path": None}, "diff": "+native patch"}]}}},
        {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "id": "connector", "type": "mcpToolCall", "server": "external", "tool": "inspect",
            "result": {"content": [{"type": "text", "text": json.dumps(fake)}]}}}}, terminal())
    assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    user_file.write_text("user private edit\n")
    preview = await manager.fork_preview(parent.id, 0, user_id=parent.user_id)
    assert preview["side_effect_summary"]["agent_touched_paths"] == [str(file)]
    assert not preview["revert"]["available"]
    branch = await manager.fork_session(parent.id, 0, revert_files=True, user_id=parent.user_id)
    assert json.loads(branch.fork_revert_record)["status"] == "refused"
    assert file.read_text() == "native patch\n" and user_file.read_text() == "user private edit\n"

"""Rewind proofs from admitted input through the original Owner and Git bytes."""

import json
import subprocess

import pytest

from server import fork_helpers
from server.sessions.client_submissions import submit_client_message
from tests.claude_runtime import SID, inputs
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inject as claude_inject
from tests.native_app_server import ROOT, inject, terminal, until
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(params=["codex", "claude-code"])
def file_runtime(request, tmp_path):
    if request.param == "codex":
        runtime = request.getfixturevalue("native_runtime")
        runtime.mode[0] = "control_output"
    else:
        runtime = request.getfixturevalue("claude_runtime")
        (tmp_path / "claude-plan.json").write_text('{"hold":true}')
    return (*runtime[:3], request.param)


async def finish_edit(backend, tmp_path, paths):
    if backend == "codex":
        inject(tmp_path, {"method": "item/completed", "params": {
            "threadId": ROOT, "turnId": "turn-1", "item": {
                "id": "patch", "type": "fileChange", "status": "completed",
                "changes": [{"path": str(path), "kind": {"type": "update"}, "diff": "+edit"} for path in paths]}}}, terminal())
    else:
        opening = (await inputs(tmp_path, 1))[0]["input"]
        for index, path in enumerate(paths):
            identifier = f"edit-{index}"
            claude_inject(tmp_path,
                {"type": "assistant", "session_id": SID, "parent_tool_use_id": None,
                 "message": {"id": identifier, "content": [{"type": "tool_use", "id": identifier,
                    "name": "Write", "input": {"file_path": str(path)}}]}},
                {"type": "user", "session_id": SID, "parent_tool_use_id": None,
                 "message": {"content": [{"type": "tool_result", "tool_use_id": identifier, "content": "saved"}]}})
        claude_inject(tmp_path, {"type": "result", "subtype": "success", "is_error": False,
            "session_id": SID, "parent_tool_use_id": None, "user_message_uuid": opening["uuid"],
            "user_message_uuids": [opening["uuid"]], "result": "Saved"})


@pytest.mark.parametrize("case", [
    "ignored", "mixed", "new", "staged-new", "rename", "rule-changed", "forced-add",
    "tracked-now-ignored", "unchanged", "assume-before", "assume-after", "skip-before", "skip-after",
])
async def test_original_input_controls_all_promised_file_baselines(file_runtime, tmp_path, case, request):
    manager, database, source, backend = file_runtime
    repo = tmp_path / "working-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    tracked = repo / "tracked.txt"
    tracked.write_text("HEAD bytes\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "baseline")
    ignored = repo / "private.txt"
    rules = repo / ".git/info/exclude"
    rules.write_text("private.txt\n")
    ignored.write_text("existing private bytes\n")
    flag = "--assume-unchanged" if case.startswith("assume") else "--skip-worktree"
    if case.endswith("before"):
        _git(repo, "update-index", flag, "tracked.txt")
        tracked.write_text("hidden user bytes before input\n")
    parent = await manager.create_session(source.agent_id, "File baselines", str(repo), backend=backend)
    receipt = await submit_client_message(manager, parent.id, "Edit the selected files", [], "edit", wiki_mode=None)
    assert receipt["accepted"]
    if backend == "codex":
        await until(lambda: parent._run.backend is not None and parent._run.backend.active_turn is not None)
    else:
        await inputs(tmp_path, 1)
    anchor = next(row for row in await database.load_messages(parent.id) if row["role"] == "user")
    if not case.endswith("before"):
        assert anchor["git_status_clean"] is True
        assert json.loads(anchor["git_ignored_baseline"]) == {"version": 1, "paths": ["private.txt"]}
    if case in {"ignored", "mixed", "rule-changed", "forced-add"}:
        ignored.write_text("Agent changed private bytes\n")
        paths = [ignored]
        if case == "mixed":
            tracked.write_text("Agent changed tracked bytes\n")
            paths.append(tracked)
        elif case == "rule-changed":
            rules.write_text("")
        elif case == "forced-add":
            _git(repo, "add", "-f", "private.txt")
    elif case in {"new", "staged-new", "rename"}:
        new = repo / "new.txt"
        paths = [new]
        if case == "rename":
            _git(repo, "mv", "tracked.txt", "new.txt")
            paths.append(tracked)
        else:
            new.write_text("Agent created bytes\n")
            if case == "staged-new":
                _git(repo, "add", "new.txt")
    else:
        paths = [tracked]
        if case != "unchanged":
            if case.endswith("before"):
                _git(repo, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "tracked.txt")
            tracked.write_text("Agent changed tracked bytes\n")
            if case.endswith("after"):
                _git(repo, "update-index", flag, "tracked.txt")
            elif case == "tracked-now-ignored":
                rules.write_text("private.txt\ntracked.txt\n")
    await finish_edit(backend, tmp_path, paths)
    await wait_dispatch_idle(manager, parent.id)
    allowed = case in {"new", "staged-new", "rename", "tracked-now-ignored", "unchanged"}
    preview = await manager.fork_preview(parent.id, 0, user_id=parent.user_id)
    assert set(preview["side_effect_summary"]["agent_touched_paths"]) == {str(path) for path in paths}
    assert preview["revert"]["available"] is allowed, preview
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    branch = await manager.fork_session(parent.id, 0, revert_files=True, user_id=parent.user_id)
    record = json.loads(branch.fork_revert_record)
    assert record["status"] == ("completed" if allowed else "refused"), record
    assert branch.fork_status == "ready"
    row = next(row for row in await database.load_sessions() if row["id"] == branch.id)
    assert json.loads(row["fork_revert_record"]) == record
    note = json.loads(row["fork_metadata"])["first_turn_note"]
    assert ("WERE reverted" in note) is allowed
    if allowed:
        assert tracked.read_text() == "HEAD bytes\n"
        if case in {"new", "staged-new", "rename"}:
            assert not (repo / "new.txt").exists()
    else:
        assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
        assert subprocess.check_output(["git", "stash", "list"], cwd=repo) == b""
    assert ignored.read_text() == ("Agent changed private bytes\n" if case in {
        "ignored", "mixed", "rule-changed", "forced-add"} else "existing private bytes\n")
    if case == "ignored":
        await observe_successor_note(manager, branch, backend, request, note)


async def observe_successor_note(manager, branch, backend, request, note):
    runtime = request.getfixturevalue("native_runtime" if backend == "codex" else "claude_runtime")
    starts = runtime[4] if backend == "codex" else runtime[3]
    previous = len(starts)
    await submit_client_message(manager, branch.id, "Inspect the retained effects", [], "successor", wiki_mode=None)
    try:
        await until(lambda: len(starts) > previous)
        trusted_context = starts[-1].turn_instructions if backend == "codex" else starts[-1].system_prompt
        assert note in trusted_context
    finally:
        await manager.interrupt(branch.id)
        await wait_dispatch_idle(manager, branch.id)


async def test_steer_keeps_its_own_real_turn_start_observation(file_runtime, tmp_path):
    manager, database, source, backend = file_runtime
    repo = tmp_path / "steering-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("*.private\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "base")
    (repo / "original.private").write_text("first observation")
    parent = await manager.create_session(source.agent_id, "Steer anchor", str(repo), backend=backend)
    await submit_client_message(manager, parent.id, "Wait for correction", [], "opening", wiki_mode=None)
    await until(lambda: parent._steer_open)
    (repo / "steer.private").write_text("exists before steer")
    reply = await submit_client_message(manager, parent.id, "Correct the original task", [], "correction", wiki_mode=None)
    assert reply["disposition"] == "steered"
    if backend == "codex":
        await until(lambda: (tmp_path / "native-steers.jsonl").exists())
    else:
        await inputs(tmp_path, 2)
    await finish_edit(backend, tmp_path, [])
    await wait_dispatch_idle(manager, parent.id)
    users = [row for row in await database.load_messages(parent.id) if row["role"] == "user"]
    assert [row["content"] for row in users] == ["Wait for correction", "Correct the original task"]
    assert json.loads(users[0]["git_ignored_baseline"])["paths"] == ["original.private"]
    assert json.loads(users[1]["git_ignored_baseline"])["paths"] == ["original.private", "steer.private"]


async def test_post_stash_failure_records_actual_effect_and_warns_the_successor(file_runtime, tmp_path, monkeypatch, request):
    manager, database, source, backend = file_runtime
    repo = tmp_path / "stash-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    path = repo / "tracked.txt"
    path.write_text("original bytes")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "base")
    parent = await manager.create_session(source.agent_id, "Stash failure", str(repo), backend=backend)
    await submit_client_message(manager, parent.id, "Edit file", [], "edit", wiki_mode=None)
    if backend == "codex":
        await until(lambda: parent._run.backend is not None and parent._run.backend.active_turn is not None)
    else:
        await inputs(tmp_path, 1)
    path.write_text("Agent effect")
    await finish_edit(backend, tmp_path, [path])
    await wait_dispatch_idle(manager, parent.id)
    real_git = fork_helpers._git

    async def unavailable_stash_list(cwd, *args, **kwargs):
        if args[:2] == ("stash", "list"):
            return 1, "", "snapshot lookup unavailable"
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fork_helpers, "_git", unavailable_stash_list)
    branch = await manager.fork_session(parent.id, 0, revert_files=True, user_id=parent.user_id)
    assert path.read_text() == "original bytes"
    assert subprocess.check_output(["git", "show", "stash@{0}:tracked.txt"], cwd=repo) == b"Agent effect"
    record = json.loads(branch.fork_revert_record)
    assert record["status"] == "failed" and record["ran"] is True and record["stash_ref"] is None
    saved = next(row for row in await database.load_sessions() if row["id"] == branch.id)
    assert json.loads(saved["fork_revert_record"]) == record
    note = json.loads(saved["fork_metadata"])["first_turn_note"]
    assert "snapshot lookup unavailable" in note and "not a rollback" in note
    assert "were NOT reverted" not in note and "WERE reverted" not in note
    await observe_successor_note(manager, branch, backend, request, note)

"""Unit tests for the pure fork helpers (session-rewind.md §5.6 / §5.3.2).

Covers the side-effect classifier (incl. the bg_tasks live-state join), the
replay-prompt wrapper, the first-turn note, git-anchor capture, and the
safe-revert preflight over a real temp git repo.
"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import fork_git
from server import fork_helpers as fh
from server.database import Database
from server.models import MessageContent, MessageRole

# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    await d.save_agent(
        agent_id="classifier-agent", name="Classifier",
        created_at="2026-06-08", updated_at="2026-06-08",
    )
    try:
        yield d
    finally:
        await d.close()


async def _seed_session(db, sid="parent"):
    await db.save_session(
        session_id=sid, name="P", working_dir="/tmp",
        created_at="2026-06-08T00:00:00+00:00", agent_id="classifier-agent",
    )


async def _append(db, sid, seq, role, type, **kw):
    await db.append_message(session_id=sid, seq=seq, role=role, type=type, **kw)


# ------------------------------------------------------------------ classifier


@pytest.mark.asyncio
async def test_classify_bins_each_tool_class(db):
    await _seed_session(db)
    # seq 0: user msg (the rewind target M=0 is not counted; we classify >= M)
    await _append(db, "parent", 0, "user", "text", content="do it")
    # seq 1: Edit → file edit
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/a.py"},
                  tool_use_id="t1")
    # seq 2: Bash with `>` → file edit (best-effort)
    await _append(db, "parent", 2, "assistant", "tool_use",
                  tool_name="Bash", tool_input={"command": "echo hi > /repo/b.txt"},
                  tool_use_id="t2")
    # seq 3: Bash plain command → "other tool activity"
    await _append(db, "parent", 3, "assistant", "tool_use",
                  tool_name="Bash", tool_input={"command": "pytest -q"},
                  tool_use_id="t3")
    # seq 4: connector tool → irreversible "other"
    await _append(db, "parent", 4, "assistant", "tool_use",
                  tool_name="mcp__github__comment", tool_input={}, tool_use_id="t4")
    # seq 5: a read-only tool → excluded
    await _append(db, "parent", 5, "assistant", "tool_use",
                  tool_name="Read", tool_input={"file_path": "/repo/a.py"},
                  tool_use_id="t5")

    summary = await fh.classify_side_effects(db, "parent", 0)
    paths = {e["path"] for e in summary["file_edits"]}
    assert paths == {"/repo/a.py", "/repo/b.txt"}
    assert summary["agent_touched_paths"] == []  # disclosure alone grants no revert authority
    labels = {o["label"] for o in summary["other_tools"]}
    assert "Bash commands" in labels  # the plain pytest run
    assert any("github" in lab for lab in labels)
    # Read is excluded entirely.
    assert not any("Read" in lab for lab in labels)


@pytest.mark.asyncio
async def test_classify_respects_from_seq(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="q1")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/early.py"},
                  tool_use_id="e1")
    await _append(db, "parent", 2, "user", "text", content="q2")
    await _append(db, "parent", 3, "assistant", "tool_use",
                  tool_name="Edit", tool_input={"file_path": "/repo/late.py"},
                  tool_use_id="e2")
    # Rewinding to M=2 should only see seq >= 2.
    summary = await fh.classify_side_effects(db, "parent", 2)
    assert summary["file_edits"] == [{"path": "/repo/late.py", "turns": 1}]
    assert summary["agent_touched_paths"] == []


async def test_shell_example_cannot_authorize_reverting_user_file(db, git_repo):
    await _seed_session(db)
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    path.write_bytes(b"private user edit\n")
    command = f"printf '%s\\n' 'rm {path}'"
    subprocess.run(["bash", "-c", command], check=True, capture_output=True)
    await _append(db, "parent", 0, "assistant", "tool_use", tool_name="Bash",
                  tool_input={"command": command})
    summary = await fh.classify_side_effects(db, "parent", 0)
    record = await fh.safe_revert_files(str(git_repo), summary["agent_touched_paths"], head, clean, "shell", fork_ignored_baseline=ignored)
    assert record["status"] == "refused"
    assert path.read_bytes() == b"private user edit\n"


@pytest.mark.parametrize("result", ["success", "error", "started", "legacy", "wrong-attempt", "relative"])
async def test_only_correlated_successful_file_tools_authorize_revert(db, git_repo, result):
    await _seed_session(db)
    path = git_repo / "a.py"
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path.write_text("agent edit\n")
    identity = {"request_id": "r", "attempt_id": "a", "tool_call_id": "call"}
    await _append(db, "parent", 0, "assistant", "tool_use", tool_name="Edit", tool_use_id="reused",
                  tool_input={"file_path": path.name if result == "relative" else str(path)},
                  runtime_metadata=None if result == "legacy" else identity)
    if result != "started":
        await _append(db, "parent", 1, "tool", "tool_result", tool_use_id="reused", content="done",
                      is_error=result == "error", runtime_metadata=None if result == "legacy" else
                      {**identity, "attempt_id": "other" if result == "wrong-attempt" else "a"})
    # Bash activity does not prohibit a revert that has independent file evidence.
    await _append(db, "parent", 2, "assistant", "tool_use", tool_name="Bash", tool_input={"command": "pwd"})
    summary = await fh.classify_side_effects(db, "parent", 0)
    record = await fh.safe_revert_files(str(git_repo), summary["agent_touched_paths"], head, clean, "file-tool", fork_ignored_baseline=ignored)
    assert record["status"] == ("completed" if result == "success" else "refused")
    assert path.read_text() == ("v1\n" if result == "success" else "agent edit\n")


async def _record_claude_protocol(db, frames):
    from server.harness.claude_code import ClaudeEventParser
    from server.harness.tool_identity import ToolIdentityTracker

    parser, tracker = ClaudeEventParser(), ToolIdentityTracker()
    seq = 0
    for frame in frames:
        for event in parser.parse(frame).events:
            tracker.normalize(event, "request", "attempt")
            await _append(db, "parent", seq, "assistant" if event.type == "tool_use" else "tool", event.type,
                          tool_name=event.tool_name, tool_use_id=event.tool_use_id, tool_input=event.tool_input,
                          content=event.content, is_error=event.is_error, runtime_metadata=event.runtime_metadata)
            seq += 1


async def test_symlink_parent_path_cannot_authorize_the_lexical_neighbor(db, git_repo, tmp_path):
    await _seed_session(db)
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "a.py").write_text("external original\n")
    (git_repo / "link").symlink_to(outside / "deep", target_is_directory=True)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "link baseline")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    raw_path = str(git_repo / "link" / ".." / "a.py")
    # POSIX traversal follows link before applying '..'. These are two files.
    with open(raw_path, "w") as stream:
        stream.write("agent external edit\n")
    user_file = git_repo / "a.py"
    user_file.write_text("private user edit\n")
    await _record_claude_protocol(db, [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "edit", "name": "Edit",
            "input": {"file_path": raw_path}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "edit", "content": "done"}]}},
    ])
    summary = await fh.classify_side_effects(db, "parent", 0)
    assert summary["file_edits"] == [{"path": raw_path, "turns": 1}]
    record = await fh.safe_revert_files(str(git_repo), summary["agent_touched_paths"], head, clean, "symlink-parent", fork_ignored_baseline=ignored)
    assert record["status"] == "refused"
    assert summary["agent_touched_paths"] == []
    assert user_file.read_text() == "private user edit\n"
    assert (outside / "a.py").read_text() == "agent external edit\n"


@pytest.mark.parametrize("sequence", ["unique", "overlap-reused", "sequential-reused"])
async def test_claude_protocol_reused_id_does_not_prove_a_file_result(db, git_repo, sequence):
    await _seed_session(db)
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    file = git_repo / "a.py"
    file.write_text("preserve this edit\n")
    read_id = "read" if sequence == "unique" else "reused"
    edit_id = "edit" if sequence == "unique" else "reused"
    read = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": read_id, "name": "Read",
        "input": {"file_path": str(file)}}]}}
    edit = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": edit_id, "name": "Edit",
        "input": {"file_path": str(file)}}]}}

    def result(raw_id, content):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": raw_id, "content": content}]}}

    frames = [read, edit, result(read_id, "old Read succeeded")] if sequence == "overlap-reused" else [
        read, result(read_id, "Read succeeded"), edit, result(edit_id, "success without a unique raw identity")]
    await _record_claude_protocol(db, frames)
    # Ambiguity before the rewind boundary must still invalidate attribution.
    summary = await fh.classify_side_effects(db, "parent", 1 if sequence == "overlap-reused" else 2)
    record = await fh.safe_revert_files(str(git_repo), summary["agent_touched_paths"], head, clean, "reused-id", fork_ignored_baseline=ignored)
    assert summary["file_edits"] == [{"path": str(file), "turns": 1}]
    assert record["status"] == ("completed" if sequence == "unique" else "refused")
    assert file.read_text() == ("v1\n" if sequence == "unique" else "preserve this edit\n")


async def test_repository_relative_path_does_not_also_authorize_cwd_path(git_repo):
    subdir = git_repo / "sub"
    subdir.mkdir()
    (subdir / "a.py").write_text("sub original\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "sub baseline")
    head, clean, ignored = await fh.capture_git_anchor(str(subdir))
    (git_repo / "a.py").write_text("root edit\n")
    (subdir / "a.py").write_text("sub edit\n")
    record = await fh.safe_revert_files(str(subdir), ["a.py"], head, clean, "ambiguous", fork_ignored_baseline=ignored)
    assert record["status"] == "refused"
    assert (subdir / "a.py").read_text() == "sub edit\n"


@pytest.mark.asyncio
async def test_classify_bg_task_live_state_join(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="run tests")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="mcp__bg__run",
                  tool_input={"command": "bun run test:e2e"}, tool_use_id="bg1",
                  runtime_metadata={"request_id": "r", "attempt_id": "a", "tool_call_id": "call1"})
    await _append(db, "parent", 2, "tool", "tool_result",
                  content="Started bg task `abc123def` (e2e suite)",
                  tool_use_id="bg1", runtime_metadata={"request_id": "r", "attempt_id": "a", "tool_call_id": "call1",
                                                        "receipt": {"kind": "bg", "task_id": "abc123def"}})
    # The bg task itself is still running per bg_tasks (the source of truth).
    await db.create_bg_task("abc123def", "parent", "bun run test:e2e",
                            "e2e suite", "/tmp", "2026-06-08T00:00:00+00:00")

    summary = await fh.classify_side_effects(db, "parent", 0)
    assert len(summary["bg_tasks"]) == 1
    task = summary["bg_tasks"][0]
    assert task["task_id"] == "abc123def"
    assert task["status"] == "running"          # read from bg_tasks, not messages
    assert task["command"] == "bun run test:e2e"


@pytest.mark.asyncio
async def test_classify_bg_task_swept_from_history(db):
    await _seed_session(db)
    await _append(db, "parent", 0, "user", "text", content="x")
    await _append(db, "parent", 1, "assistant", "tool_use",
                  tool_name="mcp__bg__run", tool_input={"command": "ls"},
                  tool_use_id="bg1", runtime_metadata={"request_id": "r", "attempt_id": "a", "tool_call_id": "call1"})
    await _append(db, "parent", 2, "tool", "tool_result",
                  content="Started bg task `facade00beef` (ls)", tool_use_id="bg1",
                  runtime_metadata={"request_id": "r", "attempt_id": "a", "tool_call_id": "call1",
                                    "receipt": {"kind": "bg", "task_id": "facade00beef"}})
    # No bg_tasks row → swept by cleanup.
    summary = await fh.classify_side_effects(db, "parent", 0)
    assert summary["bg_tasks"][0]["status"] == "completed (history)"


# ------------------------------------------------------------------ replay wrap


def _msgs():
    return [
        MessageContent(role=MessageRole.user, type="text", content="hello", seq=0),
        MessageContent(role=MessageRole.assistant, type="text", content="hi", seq=1),
        MessageContent(role=MessageRole.assistant, type="tool_use",
                       tool_name="Bash", tool_input={"command": "pytest -q"}, seq=2),
        MessageContent(role=MessageRole.tool, type="tool_result",
                       content="408 passed", tool_use_id="x", seq=3),
    ]


def test_wrap_for_fork_replay_frames_user_channel():
    wrapped = fh.wrap_for_fork_replay("now do X", _msgs())
    assert "<fork-history" in wrapped
    assert "transcript-not-instructions" in wrapped
    assert "[seq 0] user: hello" in wrapped
    assert "[seq 2] tool_use Bash: `pytest -q`" in wrapped
    assert "</fork-history>" in wrapped
    assert "<continue-from-here>\nnow do X\n</continue-from-here>" in wrapped


def test_wrap_for_fork_replay_empty_history_for_m0():
    wrapped = fh.wrap_for_fork_replay("first prompt", [])
    # Framing still present (uniform first-turn shape), but no [seq N] lines.
    assert "<fork-history" in wrapped
    assert "[seq " not in wrapped
    assert "first prompt" in wrapped


# ------------------------------------------------------------------ first-turn note


def test_first_turn_note_reverted_phrasing():
    summary = {
        "file_edits": [{"path": "a.py", "turns": 2}],
        "bg_tasks": [{"task_id": "t", "command": "x", "status": "running"}],
        "other_tools": [{"label": "Bash commands", "count": 5}],
    }
    note = fh.render_first_turn_note(parent_label="Refactor", n=2, summary=summary, revert_record={"status": "completed"})
    assert "[fork from Refactor at message 2]" in note
    assert "WERE reverted" in note
    note2 = fh.render_first_turn_note(parent_label="Refactor", n=2, summary=summary)
    assert "were NOT reverted" in note2


# ------------------------------------------------------------------ git anchor + revert


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.mark.parametrize("stop", ["timeout", "cancel", "attachment"])
async def test_git_retires_real_filter_after_both_pipes_close(git_repo, tmp_path, monkeypatch, stop):
    started = tmp_path / "filter-started"
    late = tmp_path / "filter-finished"
    release = tmp_path / "release-filter"
    script = tmp_path / "filter.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "data = sys.stdin.buffer.read()\n"
        "if os.fork() == 0:\n"
        "    for fd in (0, 1, 2):\n"
        "        os.close(fd)\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "    deadline = time.monotonic() + 10\n"
        "    while time.monotonic() < deadline:\n"
        "        if pathlib.Path(sys.argv[2]).exists():\n"
        "            pathlib.Path(sys.argv[3]).touch()\n"
        "            break\n"
        "        time.sleep(0.01)\n"
        "    os._exit(0)\n"
        "sys.stdout.buffer.write(data)\n"
    )
    (git_repo / ".gitattributes").write_text("a.py filter=observe\n")
    _git(git_repo, "config", "filter.observe.clean",
         shlex.join([sys.executable, str(script), str(started), str(release), str(late)]))
    collected = asyncio.Event()
    original_read = fork_git._collect_output
    group = None

    async def collect(process, stopping):
        nonlocal group
        group = process.pid
        result = await original_read(process, stopping)
        if stop != "attachment":
            assert process.returncode == 0
        collected.set()
        return result

    monkeypatch.setattr(fork_git, "_collect_output", collect)
    fail_attachment = asyncio.Event()
    if stop == "attachment":
        loop = asyncio.get_running_loop()
        connect = loop.connect_read_pipe
        calls = 0

        async def attach(factory, stream):
            nonlocal calls
            calls += 1
            if calls == 1:
                await fail_attachment.wait()
                raise OSError("test pipe attachment failed after spawn")
            return await connect(factory, stream)

        monkeypatch.setattr(loop, "connect_read_pipe", attach)
    command = asyncio.create_task(fork_git.run_git(
        str(git_repo), "hash-object", "--path=a.py", "a.py", timeout=1.0 if stop == "timeout" else 5,
    ))
    try:
        async with asyncio.timeout(3):
            if stop != "attachment":
                await collected.wait()
            while not started.exists() or not started.read_text():
                await asyncio.sleep(0.01)
        assert os.getpgid(int(started.read_text())) == group
        assert group is not None and fork_git.group_has_live_members_after_kill(group)
        if stop == "attachment":
            fail_attachment.set()
            result = await command
            assert result.output_failed and result.returncode is not None and not result.timed_out
        elif stop == "cancel":
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
        else:
            result = await command
            assert result.timed_out and result.returncode == 0
            assert len(result.stdout.strip()) == 40
            assert result.stderr == ""
        assert not fork_git.group_has_live_members_after_kill(group)
    finally:
        fail_attachment.set()
        release.touch()
        if not command.done():
            command.cancel()
        await asyncio.gather(command, return_exceptions=True)
    assert not late.exists()


@pytest.mark.parametrize("after_deadline", [False, True])
async def test_git_repeated_cancellation_waits_for_the_original_late_spawn(git_repo, monkeypatch, after_deadline):
    spawning = asyncio.Event()
    release = asyncio.Event()
    processes = []
    original_spawn = fork_git._spawn_git

    async def delayed_spawn(*args, **kwargs):
        spawning.set()
        await release.wait()
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(fork_git, "_spawn_git", delayed_spawn)
    command = asyncio.create_task(fh._git(
        str(git_repo), "rev-parse", "HEAD", timeout=0.03 if after_deadline else 10,
    ))
    await asyncio.wait_for(spawning.wait(), 2)
    try:
        if after_deadline:
            async with asyncio.timeout(1):
                while not fork_git.worktree_retiring(fork_git.worktree_for(str(git_repo))):
                    await asyncio.sleep(0.01)
        for _ in range(2):
            command.cancel()
            await asyncio.sleep(0)
            assert not command.done()
            assert fork_git.git_executions_pending()
    finally:
        release.set()
        with pytest.raises(fork_git.GitRetirementCancelled) as cancellation:
            await command
        assert cancellation.value.timed_out is after_deadline
        assert cancellation.value.execution.result().returncode is not None
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert not fork_git.git_executions_pending()


async def test_git_collects_both_large_pipes_and_preserves_failure_exit(git_repo):
    script = "import os; os.write(1, b'a' * 300000); os.write(2, b'b' * 300001); raise SystemExit(19)"
    alias = "alias.capture=!" + shlex.join([sys.executable, "-c", script])
    assert await fh._git(str(git_repo), "-c", alias, "capture") == fork_git.GitResult(19, "a" * 300000, "b" * 300001)
    assert not fork_git.git_executions_pending()


async def test_git_joins_other_pipe_after_output_read_error(git_repo, monkeypatch):
    stderr_started = asyncio.Event()
    release_stderr = asyncio.Event()
    loop = asyncio.get_running_loop()
    attach = loop.connect_read_pipe
    attached = 0

    async def connect(factory, stream):
        nonlocal attached
        attached += 1
        if attached == 1:
            raise OSError("test stdout attach failure")
        stderr_started.set()
        await release_stderr.wait()
        return await attach(factory, stream)

    monkeypatch.setattr(loop, "connect_read_pipe", connect)
    command = asyncio.create_task(fork_git.run_git(str(git_repo), "octopus-invalid-subcommand"))
    try:
        await asyncio.wait_for(stderr_started.wait(), 2)
        assert not command.done()
        assert fork_git.git_executions_pending()
    finally:
        release_stderr.set()
        result = await command
    assert result.output_failed and result.returncode is not None
    assert not fork_git.git_executions_pending()


async def test_git_spawn_failure_has_no_process_to_retire(git_repo):
    assert await fh._git(str(git_repo / "absent"), "status") == fork_git.GitResult(None, "", "git not spawnable")
    assert not fork_git.git_executions_pending()


async def test_git_spawn_uses_the_opened_directory_after_its_path_is_replaced(git_repo, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn = fork_git._spawn_git

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(fork_git, "_spawn_git", delayed_spawn)
    command = asyncio.create_task(fh._git(str(git_repo), "rev-parse", "--show-toplevel"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        original = git_repo.rename(git_repo.parent / "moved")
        git_repo.mkdir()
        _git(git_repo, "init", "-q")
    finally:
        release.set()
        result = await command
    assert result == fork_git.GitResult(0, f"{original}\n", "")


@pytest.mark.parametrize("nested", [False, True])
async def test_git_rejects_an_opened_replacement_even_if_path_looks_reserved(git_repo, monkeypatch, nested):
    replacement = git_repo.parent / "replacement"
    replacement.mkdir()
    _git(replacement, "init", "-q")
    if nested:
        (git_repo / "sub").mkdir()
        (replacement / "sub").mkdir()
    cwd = str(git_repo / "sub" if nested else git_repo)
    moved = git_repo.parent / "original"
    open_dir, readlink = os.open, os.readlink
    swapped = False

    def open_replacement(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and os.fspath(path) == cwd:
            git_repo.rename(moved)
            replacement.rename(git_repo)
            swapped = True
        return open_dir(path, flags, *args, **kwargs)

    def restore_path():
        if moved.exists():
            git_repo.rename(replacement)
            moved.rename(git_repo)

    def stale_readlink(path, *args, **kwargs):
        observed = readlink(path, *args, **kwargs)
        if swapped and os.fspath(path).startswith("/proc/self/fd/"):
            restore_path()
        return observed

    worktree = fork_git.worktree_for(cwd)
    monkeypatch.setattr(os, "open", open_replacement)
    monkeypatch.setattr(os, "readlink", stale_readlink)
    try:
        with fork_git.git_scope(worktree, None, None):
            result = await fh._git(cwd, "rev-parse", "--show-toplevel")
        assert swapped
        assert result.returncode is None and "outside the reserved Git worktree" in result.stderr
    finally:
        restore_path()


async def test_preflight_keeps_a_rejected_paths_git_result_after_checking_later_paths(git_repo, monkeypatch):
    paths = [git_repo / part / "new.txt" for part in ("first", "second")]
    for path in paths:
        path.parent.mkdir()
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    for path in paths:
        path.write_text("new content")
    git = fh._git
    visited = []

    async def lookup(cwd, *args, **kwargs):
        if args == ("rev-parse", "--show-toplevel") and cwd in {str(path.parent) for path in paths}:
            visited.append(cwd)
            if cwd == str(paths[0].parent):
                return fork_git.GitResult(19, "observed before failure", "unavailable ancestor")
        return await git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", lookup)
    record = await fh.safe_revert_files(str(git_repo), [str(path) for path in paths], head, clean, "rejected", ignored)
    assert visited == [str(path.parent) for path in paths]
    assert record["status"] == "refused" and record["git_result"]["exit_code"] == 19
    assert record["git_result"]["stderr"] == "unavailable ancestor"
    assert record["git_result"]["stdout"] == "observed before failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("inner_git", [None, "head-only", "invalid-head", "incomplete-common"])
async def test_capture_git_anchor_clean(git_repo, inner_git):
    cwd = git_repo
    if inner_git:
        cwd = git_repo / "inner"
        marker = cwd / ".git"
        marker.mkdir(parents=True)
        if inner_git != "invalid-head":
            (marker / "HEAD").write_text("ref: refs/heads/main\n")
            if inner_git == "incomplete-common":
                (marker / "shared").mkdir()
                (marker / "commondir").write_text("shared\n")
        else:
            (marker / "objects").mkdir()
            (marker / "refs").mkdir()
            (marker / "HEAD").write_text("not a Git ref\n")
    head, clean, ignored = await fh.capture_git_anchor(str(cwd))
    assert head and len(head) == 40
    assert clean is True
    path = git_repo / "a.py"
    path.write_text("agent edit\n")
    assert await fh.safe_revert_preflight(str(cwd), [str(path)], head, clean, ignored) == (True, None, ["a.py"])


@pytest.mark.asyncio
async def test_capture_git_anchor_dirty(git_repo):
    (git_repo / "a.py").write_text("v2\n")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is False


@pytest.mark.asyncio
async def test_capture_git_anchor_non_repo(tmp_path):
    head, clean, ignored = await fh.capture_git_anchor(str(tmp_path))
    assert head is None and clean is None


@pytest.mark.parametrize("observation", ["missing-nul", "bad-status", "empty-path", "warning", "failed"])
async def test_anchor_does_not_call_an_incomplete_observation_clean(git_repo, monkeypatch, observation):
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if args[0] == "status":
            return fork_git.GitResult(*{
                "missing-nul": (0, "!! existing", ""), "bad-status": (0, "?! existing\0", ""),
                "empty-path": (0, "!! \0", ""), "warning": (0, "", "warning: cannot open directory"),
                "failed": (1, "", "cannot read status"),
            }[observation])
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is None and ignored is None
    available, _, _ = await fh.safe_revert_preflight(str(git_repo), ["a.py"], head, True)
    assert not available


@pytest.mark.parametrize("size", [262143, 262144, 262145])
async def test_complete_ignored_json_budget_preserves_independent_head_and_clean(git_repo, monkeypatch, size):
    # Realistic short filenames; choose the last one to hit the complete JSON
    # byte boundary, including all quotes, commas, version and object overhead.
    paths = [f"file-{index:05}-" + "x" * 180 for index in range(1350)]
    raw = json.dumps({"version": 1, "paths": paths}, separators=(",", ":"))
    padding = size - len(raw.encode())
    paths.append("last-" + "x" * (padding - 8))
    expected = json.dumps({"version": 1, "paths": paths}, separators=(",", ":"))
    assert len(expected.encode()) == size
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if args[0] == "status":
            assert "--ignored=matching" in args and "-z" in args
            return fork_git.GitResult(0, "".join(f"!! {path}\0" for path in paths), "")
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is True
    assert ignored == (expected if size <= 262144 else None)


@pytest.mark.parametrize("pattern", ["cache/", "cache/*.tmp"])
async def test_matching_ignored_observation_handles_collapsed_directory_and_children(git_repo, pattern):
    directory = git_repo / "cache"
    directory.mkdir()
    for index in range(80):
        (directory / f"{index}.tmp").write_text("existing bytes")
    (git_repo / ".git/info/exclude").write_text(pattern + "\n")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    covers = json.loads(ignored)["paths"]
    assert covers == (["cache"] if pattern == "cache/" else sorted(f"cache/{index}.tmp" for index in range(80)))
    (git_repo / ".git/info/exclude").write_text("")
    available, reason, _ = await fh.safe_revert_preflight(str(git_repo), ["cache/0.tmp"], head, clean, ignored)
    assert not available and "historical ignored" in reason


@pytest.mark.parametrize("kind", ["metadata", "nested", "gitlink", "head-symlink", "head-directory"])
async def test_absence_proof_never_authorizes_an_unsupported_git_location(git_repo, kind):
    if kind == "gitlink":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo).decode().strip()
        _git(git_repo, "clone", "-q", "--shared", str(git_repo), "nested")
        _git(git_repo, "update-index", "--add", "--cacheinfo", f"160000,{head},nested")
        _git(git_repo, "commit", "-qm", "Submodule entry")
    elif kind == "head-symlink":
        (git_repo / "nested").symlink_to("elsewhere")
        _git(git_repo, "add", "nested")
        _git(git_repo, "commit", "-qm", "Symlink entry")
    elif kind == "head-directory":
        (git_repo / "nested").mkdir()
        (git_repo / "nested/original").write_text("original")
        _git(git_repo, "add", "nested")
        _git(git_repo, "commit", "-qm", "Directory entry")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    if kind == "metadata":
        path = ".git/config"
    elif kind == "head-directory":
        path = "nested"
    else:
        nested = git_repo / "nested"
        if nested.is_symlink():
            nested.unlink()
        nested.mkdir(exist_ok=True)
        if kind == "nested":
            _git(nested, "init", "-q")
        (nested / "new").write_text("retain these bytes")
        path = "nested/new"
    before = subprocess.check_output(["git", "status", "--porcelain=v1", "-z"], cwd=git_repo)
    record = await fh.safe_revert_files(str(git_repo), [path], head, clean, "unsupported", ignored)
    assert record["status"] == "refused" and path in record["refused_reason"]
    assert subprocess.check_output(["git", "status", "--porcelain=v1", "-z"], cwd=git_repo) == before
    assert subprocess.check_output(["git", "stash", "list"], cwd=git_repo) == b""


@pytest.mark.parametrize("descendant", [True, False])
async def test_historical_ignored_symlink_covers_components_even_after_becoming_directory(git_repo, descendant):
    old = git_repo / "hidden"
    old.symlink_to("missing")
    rules = git_repo / ".git/info/exclude"
    rules.write_text("hidden\n")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert json.loads(ignored)["paths"] == ["hidden"]
    old.unlink()
    old.mkdir()
    rules.write_text("")
    path = git_repo / ("hidden" if descendant else "hidden-neighbor") / "new"
    path.parent.mkdir(exist_ok=True)
    path.write_text("Agent bytes")
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "component-coverage", ignored)
    assert record["status"] == ("refused" if descendant else "completed"), record
    assert path.exists() is descendant


async def test_legacy_null_baseline_allows_head_restore_but_cannot_prove_new_file_absent(git_repo):
    head, clean, _ = await fh.capture_git_anchor(str(git_repo))
    (git_repo / "a.py").write_text("Agent edit")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, clean, "legacy-tracked")
    assert record["status"] == "completed"
    (git_repo / "new.txt").write_text("retain unknown origin")
    record = await fh.safe_revert_files(str(git_repo), ["new.txt"], head, clean, "legacy-new")
    assert record["status"] == "refused" and "unavailable" in record["refused_reason"]
    assert (git_repo / "new.txt").read_text() == "retain unknown origin"


@pytest.mark.asyncio
async def test_safe_revert_runs_when_clean_and_head_matches(git_repo):
    head, _, _ = await fh.capture_git_anchor(str(git_repo))
    # Agent dirtied a.py after the fork point.
    (git_repo / "a.py").write_text("agent-edit\n")
    record = await fh.safe_revert_files(
        str(git_repo), ["a.py"], head, True, "fork123"
    )
    assert record["status"] == "completed"
    assert record["ran"] is True
    assert len(record["stash_ref"]) == 40
    assert (git_repo / "a.py").read_text() == "v1\n"  # restored
    # The agent edit is recoverable in a stash.
    out = subprocess.run(["git", "stash", "list"], cwd=git_repo,
                         capture_output=True, text=True).stdout
    assert "octopus: pre-fork stash fork123" in out


@pytest.mark.parametrize("name", [
    "中文文件.txt", "with spaces.txt", 'quotes"and\\slash.txt', "line\nbreak.txt",
    " leading and trailing ", "left -> right.txt", "literal*[?].txt", ":(exclude)a.py",
    "--option.txt", "sub/新目录/文件.txt", "non-utf8-\udcff.txt",
])
@pytest.mark.parametrize("tracked", [True, False])
async def test_safe_revert_preserves_literal_filenames_and_recoverable_stash(git_repo, name, tracked):
    path = git_repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if tracked:
        path.write_text("original\n")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "filename baseline")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path.write_text("agent edit\n")
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "literal-path", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert record["files"] == [name]
    if tracked:
        assert path.read_text() == "original\n"
    else:
        assert not path.exists()
    _git(git_repo, "stash", "apply", record["stash_ref"])
    assert path.read_text() == "agent edit\n"


@pytest.mark.parametrize("staged", [False, True])
async def test_safe_revert_restores_file_type_and_stash_recovers_original_type(git_repo, staged):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    original = path.read_bytes()
    path.unlink()
    path.symlink_to("missing-target")
    if staged:
        _git(git_repo, "add", "--", path.name)
    record = await fh.safe_revert_files(str(git_repo), [path.name], head, clean, "type-change", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert record["files"] == [path.name]
    assert not path.is_symlink() and path.read_bytes() == original
    _, reverted_clean, _ = await fh.capture_git_anchor(str(git_repo))
    assert reverted_clean is True
    _git(git_repo, "stash", "apply", "--index", record["stash_ref"])
    assert path.is_symlink() and str(path.readlink()) == "missing-target"
    recovered = subprocess.run(["git", "status", "--porcelain=v1", "-z"],
                               cwd=git_repo, capture_output=True, check=True).stdout
    assert recovered == (b"T " if staged else b" T") + b" a.py\0"


async def test_safe_revert_checks_both_rename_locations_and_restores_stash(git_repo):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    destination = "renamed 中文\n -> file.py"
    _git(git_repo, "mv", "--", "a.py", destination)
    refused = await fh.safe_revert_files(str(git_repo), [destination], head, clean, "incomplete-rename", fork_ignored_baseline=ignored)
    assert refused["status"] == "refused" and (git_repo / destination).exists()
    record = await fh.safe_revert_files(str(git_repo), ["a.py", destination], head, clean, "rename", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert set(record["files"]) == {"a.py", destination}
    assert (git_repo / "a.py").read_text() == "v1\n"
    assert not (git_repo / destination).exists()
    _git(git_repo, "stash", "apply", "--index", record["stash_ref"])
    assert not (git_repo / "a.py").exists()
    assert (git_repo / destination).read_text() == "v1\n"


@pytest.mark.parametrize("staged,recreated", [(False, False), (True, False), (True, True)])
async def test_safe_revert_restores_deleted_paths_and_preserves_original_staging(git_repo, staged, recreated):
    path = git_repo / "a.py"
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    if staged:
        _git(git_repo, "rm", "--", "a.py")
    else:
        path.unlink()
    if recreated:
        path.write_text("new untracked contents")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, clean, "deletion", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "v1\n"
    recovered = subprocess.run(["git", "stash", "apply", "--index", record["stash_ref"]],
                               cwd=git_repo, capture_output=True)
    if recreated:
        # Native Git also reports this collision for a whole-tree stash, even
        # though it restores the original staged deletion + untracked content.
        assert recovered.returncode == 1 and b"could not restore untracked files" in recovered.stderr
    else:
        assert recovered.returncode == 0, recovered.stderr
    assert path.exists() is recreated
    if recreated:
        assert path.read_text() == "new untracked contents"
    state = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=git_repo,
                           capture_output=True, check=True).stdout
    assert (b"a.py" in state) is staged


async def test_safe_revert_preserves_whitespace_in_repository_root(git_repo):
    root = git_repo.with_name("repository \n")
    git_repo.rename(root)
    head, clean, ignored = await fh.capture_git_anchor(str(root))
    (root / "a.py").write_text("agent")
    record = await fh.safe_revert_files(str(root), ["a.py"], head, clean, "root-whitespace", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert (root / "a.py").read_text() == "v1\n"
    _git(root, "stash", "apply", record["stash_ref"])
    assert (root / "a.py").read_text() == "agent"


async def test_failed_restore_keeps_original_changes_in_a_named_stash(git_repo, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    _git(git_repo, "rm", "a.py")
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if "restore" in args:
            return fork_git.GitResult(1, "", "restore denied")
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, clean, "failed-restore", fork_ignored_baseline=ignored)
    assert record["status"] == "failed" and record["ran"] is True
    assert len(record["stash_ref"]) == 40 and "restore denied" in record["error"]
    # Recovery does not depend on the failed restore's current tree.
    _git(git_repo, "reset", "--hard", "HEAD")
    _git(git_repo, "stash", "apply", "--index", record["stash_ref"])
    assert not (git_repo / "a.py").exists()


async def test_safe_revert_consumes_real_copy_records(git_repo):
    source = git_repo / "a.py"
    source.write_text("original line\n" * 50)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "copy baseline")
    _git(git_repo, "config", "status.renames", "copies")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    copy = git_repo / "copy 中文.py"
    copy.write_bytes(source.read_bytes())
    source.write_text("different line\n" * 50)
    _git(git_repo, "add", "-A")
    actual = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=git_repo,
                            capture_output=True, check=True).stdout
    assert b"C  " in actual, "The integration must actually exercise Git's copy record"
    record = await fh.safe_revert_files(str(git_repo), ["a.py", copy.name], head, clean, "copy", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert source.read_text() == "original line\n" * 50 and not copy.exists()
    _git(git_repo, "stash", "apply", "--index", record["stash_ref"])
    assert source.read_text() == "different line\n" * 50
    assert copy.read_text() == "original line\n" * 50


async def test_recovery_snapshot_identity_survives_interleaved_and_later_stashes(git_repo, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    path.write_text("original agent edit\n")
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        result = await real_git(cwd, *args, **kwargs)
        if "push" in args:
            path.write_text("interleaved stash\n")
            _git(git_repo, "stash", "push", "-m", "another caller")
        return result

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "stable", fork_ignored_baseline=ignored)
    assert record["status"] == "completed"
    path.write_text("later stash\n")
    _git(git_repo, "stash", "push", "-m", "later")
    _git(git_repo, "stash", "apply", record["stash_ref"])
    assert path.read_text() == "original agent edit\n"


async def test_unverified_snapshot_never_borrows_an_old_stash_or_restores(git_repo, monkeypatch):
    path = git_repo / "a.py"
    path.write_text("old snapshot\n")
    _git(git_repo, "stash", "push", "-m", "old")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    _git(git_repo, "rm", "a.py")
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if "rev-parse" in args and "--verify" in args:
            return fork_git.GitResult(1, "", "object unavailable")
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "unverified", fork_ignored_baseline=ignored)
    assert record["status"] == "failed" and record["stash_ref"] is None
    assert not path.exists()  # no explicit restore after identity verification failed
    assert "snapshot" in record["error"]


async def test_snapshot_verified_after_deadline_remains_available_without_restore(git_repo, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    _git(git_repo, "rm", path.name)
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        assert "restore" not in args
        result = await real_git(cwd, *args, **kwargs)
        if args[:2] == ("rev-parse", "--verify"):
            assert result.returncode == 0 and not result.failed
            return replace(result, timed_out=True)
        return result

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "late-verified", ignored)
    assert record["status"] == "failed" and not path.exists()
    assert record["git_result"]["timed_out"] and record["git_result"]["exit_code"] == 0
    assert record["stash_ref"] == subprocess.check_output(
        ["git", "rev-parse", "refs/stash"], cwd=git_repo, text=True,
    ).strip()


@pytest.mark.parametrize("lookup", ["failed", "missing", "duplicate"])
async def test_unidentified_present_snapshot_never_restores_other_paths(git_repo, monkeypatch, lookup):
    removed = git_repo / "removed.py"
    removed.write_text("original removed file\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "deletion baseline")
    present = git_repo / "a.py"
    present.write_text("unrelated old stash\n")
    _git(git_repo, "stash", "push", "-m", "old")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    present.write_text("original agent changes\n")
    _git(git_repo, "rm", "removed.py")
    real_git, restores = fh._git, []

    async def git(cwd, *args, **kwargs):
        if "restore" in args:
            restores.append(args)
        result = await real_git(cwd, *args, **kwargs)
        if args[:2] == ("stash", "list"):
            if lookup == "failed":
                return fork_git.GitResult(1, "", "stash lookup denied")
            lines = result.stdout.splitlines()
            return fork_git.GitResult(0, lines[-1] + "\n" if lookup == "missing" else result.stdout + lines[0] + "\n", "")
        return result

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), [str(present), str(removed)], head, clean, "missing-marker", fork_ignored_baseline=ignored)
    assert record["status"] == "failed" and record["stash_ref"] is None
    assert "snapshot" in record["error"] and "marker" in record["error"]
    assert restores == [] and not removed.exists()
    # Native stash already saved and reset the present path before lookup.
    assert present.read_text() == "v1\n"
    saved = subprocess.run(["git", "show", "stash@{0}:a.py"], cwd=git_repo, check=True, capture_output=True, text=True)
    assert saved.stdout == "original agent changes\n"


async def test_copy_source_is_not_itself_a_changed_path(git_repo, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    copy = git_repo / "copy.py"
    copy.write_bytes((git_repo / "a.py").read_bytes())
    _git(git_repo, "add", "copy.py")
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if args[0] == "status":
            # A copy record carries its clean source as metadata, not a write.
            return fork_git.GitResult(0, "C  copy.py\0a.py\0", "")
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), ["copy.py"], head, clean, "clean-copy-source", fork_ignored_baseline=ignored)
    assert record["status"] == "completed" and record["files"] == ["copy.py"]
    assert not copy.exists() and (git_repo / "a.py").read_text() == "v1\n"
    _git(git_repo, "stash", "apply", "--index", record["stash_ref"])
    assert copy.read_text() == "v1\n"


@pytest.mark.parametrize("touched", ["repo", "absolute"])
async def test_safe_revert_from_a_subdirectory(git_repo, touched):
    subdir = git_repo / "sub"
    subdir.mkdir()
    path = subdir / "file \n.txt"
    path.write_text("original")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "subdirectory baseline")
    head, clean, ignored = await fh.capture_git_anchor(str(subdir))
    path.write_text("agent")
    names = {"cwd": path.name, "repo": "sub/" + path.name, "absolute": str(path)}
    record = await fh.safe_revert_files(str(subdir), [names[touched]], head, clean, "subdir", fork_ignored_baseline=ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "original"
    _git(git_repo, "stash", "apply", record["stash_ref"])
    assert path.read_text() == "agent"


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("split_index", [False, True])
async def test_file_revert_starts_git_with_the_verified_repository(git_repo, tmp_path, monkeypatch, linked, split_index):
    (git_repo / "deleted.py").write_text("original\n")
    _git(git_repo, "add", "deleted.py")
    _git(git_repo, "commit", "-qm", "Track the file to delete")
    root = git_repo
    if linked:
        root = tmp_path / "linked"
        _git(git_repo, "worktree", "add", "--detach", str(root))
    if split_index:
        _git(root, "config", "core.splitIndex", "true")
        _git(root, "config", "splitIndex.maxPercentChange", "0")
        _git(root, "update-index", "--split-index")
    git_directory = subprocess.check_output(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=root, text=True,
    ).rstrip("\n")
    common_directory = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root, text=True,
    ).rstrip("\n")
    inner = root / "inner"
    (inner / ".git").mkdir(parents=True)
    (inner / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    head, clean, ignored = await fh.capture_git_anchor(str(root))
    assert clean is True
    path = root / "a.py"
    path.write_text("agent edit\n")
    deleted = root / "deleted.py"
    _git(root, "rm", "deleted.py")
    touched = [str(path), str(deleted)]
    untracked = inner / "untracked.txt"
    untracked.write_text("recoverable\n")
    touched.append(str(untracked))
    original_spawn = fork_git._spawn_git
    writes = []
    statuses = []
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    _git(alternate, "init", "-q")
    common_locator = Path(git_directory) / "commondir"
    original_locator = common_locator.read_bytes() if common_locator.is_file() else None

    async def observe(args, *, directory_fd, repository_env=None, executable="git"):
        if args == ("rev-parse", "--show-toplevel"):
            assert repository_env is None
        else:
            options = args[1:] if args[0] == "--no-optional-locks" else args
            assert options[0].startswith("--git-dir=")
            admin = Path(options[0].partition("=")[2])
            if "status" in args or "restore" in args or ("stash" in args and "push" in args):
                assert "GIT_COMMON_DIR" not in repository_env
                common = admin / (admin / "commondir").read_text().strip()
                assert (common / "objects").samefile(Path(common_directory) / "objects")
                assert os.path.samefile(repository_env["GIT_INDEX_FILE"], Path(git_directory) / "index")
            else:
                assert admin.samefile(git_directory)
                assert os.path.samefile(repository_env["GIT_COMMON_DIR"], common_directory)
            assert options[1] == "--work-tree=."
            assert os.path.samestat(os.fstat(directory_fd), root.stat())
        if ("stash" in args and "push" in args) or "restore" in args:
            writes.append(args)
        if "status" in args:
            assert args[0] == "--no-optional-locks"
            statuses.append(args)
        if "stash" not in args or "push" not in args:
            return await original_spawn(
                args, directory_fd=directory_fd, repository_env=repository_env, executable=executable,
            )
        common_locator.write_text(str(alternate / ".git") + "\n")
        try:
            process = await original_spawn(
                args, directory_fd=directory_fd, repository_env=repository_env, executable=executable,
            )
            assert await asyncio.to_thread(process.wait, 3) == 0
            return process
        finally:
            if original_locator is None:
                common_locator.unlink()
            else:
                common_locator.write_bytes(original_locator)

    monkeypatch.setattr(fork_git, "_spawn_git", observe)
    foreign_index = tmp_path / "foreign-index"
    foreign_index.write_bytes(b"unchanged")
    foreign_objects = tmp_path / "foreign-objects"
    foreign_objects.mkdir()
    with monkeypatch.context() as inherited:
        inherited.setenv("GIT_INDEX_FILE", str(foreign_index))
        inherited.setenv("GIT_OBJECT_DIRECTORY", str(foreign_objects))
        record = await fh.safe_revert_files(str(root), touched, head, clean, "bound-write", ignored)
    assert record["status"] == "completed", record
    assert len(writes) == 2 and statuses and record["stash_ref"]
    assert path.read_text() == "v1\n"
    assert deleted.read_text() == "original\n"
    assert not untracked.exists()
    assert foreign_index.read_bytes() == b"unchanged" and not list(foreign_objects.iterdir())
    assert not (alternate / ".git" / "refs" / "stash").exists()
    _git(root, "status", "--porcelain")
    _git(root, "stash", "apply", "--index", record["stash_ref"])
    assert untracked.read_text() == "recoverable\n"


async def test_git_anchor_ignores_inherited_repository_selection(git_repo, tmp_path, monkeypatch):
    expected = await fh.capture_git_anchor(str(git_repo))
    monkeypatch.setenv("GIT_COMMON_DIR", str(tmp_path / "unrelated"))
    assert expected[1] is True
    assert await fh.capture_git_anchor(str(git_repo)) == expected


@pytest.mark.parametrize("linked", [False, True])
async def test_status_preserves_output_when_common_locator_changes(git_repo, tmp_path, monkeypatch, linked):
    root = git_repo
    if linked:
        root = tmp_path / "linked"
        _git(git_repo, "worktree", "add", "--detach", str(root))
    git_dir = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=root, text=True).rstrip("\n")
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    _git(alternate, "init", "-q")
    common = os.path.join(git_dir, "commondir")
    saved = os.path.isfile(common)
    spawn = fork_git._spawn_git

    async def change_before_status(args, **kwargs):
        if "status" not in args:
            return await spawn(args, **kwargs)
        if saved:
            os.rename(common, common + ".saved")
        Path(common).write_text(str(alternate / ".git") + "\n")
        return await spawn(args, **kwargs)

    monkeypatch.setattr(fork_git, "_spawn_git", change_before_status)
    (root / "a.py").write_text("edit\n")
    try:
        observed = await fh._git(str(root), "status", "--porcelain=v1")
        assert observed.returncode == 0 and observed.stdout == " M a.py\n"
        assert observed.binding_failed and not observed.output_failed
    finally:
        os.unlink(common)
        if saved:
            os.rename(common + ".saved", common)


async def test_linked_reftable_stash_store_uses_the_bound_repository(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    created = subprocess.run(["git", "init", "-q", "--ref-format=reftable"], cwd=main, capture_output=True, text=True)
    if created.returncode and "unknown option" in created.stderr:
        pytest.skip("Installed Git has no reftable support")
    created.check_returncode()
    _git(main, "config", "user.name", "T")
    _git(main, "config", "user.email", "t@example.invalid")
    (main / "a.py").write_text("base\n")
    _git(main, "add", "a.py")
    _git(main, "commit", "-qm", "base")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "--detach", str(linked))
    head, clean, ignored = await fh.capture_git_anchor(str(linked))
    assert clean is True
    _git(linked, "rm", "a.py")
    admin = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=linked, text=True).rstrip("\n"))
    locator = admin / "commondir"
    original_locator = locator.read_bytes()
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "--ref-format=reftable")
    spawn = fork_git._spawn_git
    stores = []

    async def change_before_store(args, **kwargs):
        if "stash" not in args or "store" not in args:
            return await spawn(args, **kwargs)
        locator.write_text(str(other / ".git") + "\n")
        try:
            process = await spawn(args, **kwargs)
            stores.append(process)
            assert await asyncio.to_thread(process.wait, 3) == 0
            return process
        finally:
            locator.write_bytes(original_locator)

    monkeypatch.setattr(fork_git, "_spawn_git", change_before_store)
    record = await fh.safe_revert_files(str(linked), [str(linked / "a.py")], head, clean, "reftable", ignored)
    assert record["status"] == "completed", record
    assert len(stores) == 1
    assert subprocess.check_output(["git", "stash", "list", "--format=%H"], cwd=linked, text=True).strip() == record["stash_ref"]
    assert not subprocess.check_output(["git", "stash", "list"], cwd=other, text=True)
    assert (linked / "a.py").read_text() == "base\n"


async def test_git_view_preserves_native_include_paths_and_complete_head(git_repo, tmp_path, monkeypatch):
    branch = "/".join(("section" * 14,) * 4)
    _git(git_repo, "branch", "-m", branch)
    excluded = tmp_path / "excludes"
    excluded.write_text("generated.txt\n")
    (tmp_path / "revert-config").write_text(f'[core]\nexcludesFile = "{excluded}"\n')
    _git(git_repo, "config", f"includeIf.gitdir:{git_repo}/.git.path", "../../revert-config")
    (git_repo / "generated.txt").write_text("ignored\n")
    unrelated = tmp_path / "unrelated-config"
    unrelated.write_text("[core]\nbare = true\n")
    monkeypatch.setenv("GIT_CONFIG", str(unrelated))
    existing = git_repo / ".git" / "resolved.config"
    existing.write_bytes(b"original Git metadata contents")
    assert len((git_repo / ".git" / "HEAD").read_bytes()) > 256
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    assert existing.read_bytes() == b"original Git metadata contents"
    assert "generated.txt" in fh.decode_ignored_baseline(ignored)
    path = git_repo / "a.py"
    path.write_text("agent\n")
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "config", ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "v1\n"


async def test_git_view_replays_command_scope_includes_for_the_original_repository(git_repo, tmp_path, monkeypatch):
    excluded = tmp_path / "excludes"
    excluded.write_text("generated.txt\n")
    included = tmp_path / "command-include"
    included.write_text(f'[core]\nexcludesFile = "{excluded}"\n')
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"includeIf.gitdir:{git_repo}/.git.path")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(included))
    (git_repo / "generated.txt").write_text("ignored\n")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is True
    assert "generated.txt" in fh.decode_ignored_baseline(ignored)
    changed = git_repo / "a.py"
    changed.write_text("agent edit\n")
    result = await fh.safe_revert_files(str(git_repo), [str(changed)], head, clean, "command-scope", ignored)
    assert result["status"] == "completed", result


async def test_git_view_preserves_the_primary_repository_format(git_repo, tmp_path):
    format_include = tmp_path / "format-include"
    format_include.write_text("[extensions]\nobjectFormat = sha256\n")
    _git(git_repo, "config", "include.path", str(format_include))
    _git(git_repo, "status", "--porcelain=v1")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and len(head) == 40 and clean is True
    path = git_repo / "a.py"
    path.write_text("agent\n")
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "repo-format", ignored)
    assert record["status"] == "completed", record


@pytest.mark.parametrize("hide_in_submodule", [False, True])
async def test_git_view_leaves_submodule_global_conditions_native(git_repo, tmp_path, monkeypatch, hide_in_submodule):
    origin = tmp_path / "submodule-origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "config", "user.name", "T")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "commit", "-qm", "base", "--allow-empty")
    _git(git_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    _git(git_repo, "commit", "-qam", "submodule")
    include = tmp_path / "only-main.config"
    include.write_text("[status]\nshowUntrackedFiles = no\n")
    global_config = tmp_path / "global.config"
    controls = f'[includeIf "gitdir:{git_repo}/.git"]\npath = "{include}"\n'
    if hide_in_submodule:
        controls += f'[includeIf "gitdir:{git_repo}/.git/modules/sub"]\npath = "{include}"\n'
    global_config.write_text(controls)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    (git_repo / "sub" / "untracked").write_text("present\n")
    native = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=git_repo, text=True,
    )
    assert ("sub" in native) is not hide_in_submodule
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is hide_in_submodule and ignored is not None


@pytest.mark.parametrize("relative_global", [False, True])
async def test_git_view_preserves_submodule_config_entrypoint_and_include_depth(git_repo, tmp_path, monkeypatch, relative_global):
    source = tmp_path / "submodule-origin"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "T")
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "commit", "-qm", "base", "--allow-empty")
    _git(git_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
    _git(git_repo, "commit", "-qam", "submodule")
    submodule = git_repo / "sub"
    config_chain = [tmp_path / f"include-{number}" for number in range(10)]
    for current, following in zip(config_chain[:-1], config_chain[1:], strict=True):
        current.write_text(f'[include]\npath = "{following}"\n')
    config_chain[-1].write_text("[status]\nshowUntrackedFiles = no\n")
    if relative_global:
        global_config = ".scoped-config"
        (git_repo / global_config).write_text("[core]\n")
        (git_repo / ".git" / "info" / "exclude").write_text(global_config + "\n")
        (submodule / global_config).write_text(f'[include]\npath = "{config_chain[0]}"\n')
    else:
        global_path = tmp_path / "global.config"
        global_path.write_text(
            f'[includeIf "gitdir:{git_repo}/.git/modules/sub"]\npath = "{config_chain[0]}"\n',
        )
        global_config = str(global_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", global_config)
    (submodule / "untracked").write_text("nested file\n")
    native = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=git_repo, text=True,
    )
    assert native == ""
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is True and ignored is not None


@pytest.mark.parametrize("stop", ["timeout", "cancel", "pipe"])
@pytest.mark.parametrize("phase", ["resolved", "primary", "exec_path", "shell_path", "hooks_path"])
async def test_git_configuration_retirement_keeps_exit_evidence_without_config_values(git_repo, monkeypatch, stop, phase):
    launched = asyncio.Event()
    original = fork_git._spawn_git
    read_pipe = fork_git._read_pipe
    path = git_repo / "a.py"
    path.write_text("agent\n")
    _git(git_repo, "config", "filter.probe.clean", "cat | cat")
    inspection_flags = {
        "resolved": "--includes",
        "primary": "--no-includes",
        "exec_path": "--exec-path",
        "shell_path": "GIT_SHELL_PATH",
        "hooks_path": "--git-path",
    }

    async def hold_config(args, **kwargs):
        if not set(args).intersection(inspection_flags.values()):
            pytest.fail("No write should start after configuration inspection is interrupted")
        if inspection_flags[phase] not in args:
            return await original(args, **kwargs)
        process = await original(("-c", "alias.wait=!printf private-config-value; sleep 10", "wait"), **kwargs)
        launched.set()
        return process

    async def broken_pipe(stream, stopping):
        if not launched.is_set():
            return await read_pipe(stream, stopping)
        stream.close()
        stopping.set()
        return b"", True

    monkeypatch.setattr(fork_git, "_spawn_git", hold_config)
    if stop == "pipe":
        monkeypatch.setattr(fork_git, "_read_pipe", broken_pipe)
    task = asyncio.create_task(fh._git(
        str(git_repo), "--literal-pathspecs", "stash", "push", "-u", "--", path.name,
        timeout=0.35 if stop == "timeout" else 10,
    ))
    await asyncio.wait_for(launched.wait(), 3)
    if stop == "cancel":
        task.cancel()
        with pytest.raises(fork_git.GitRetirementCancelled) as cancelled:
            await task
        result = await asyncio.shield(cancelled.value.execution)
    else:
        result = await asyncio.wait_for(task, 3)
    record = {"ran": False}
    fh.record_git_result(record, result, file_operation=True)
    assert not record["ran"] and record["git_result"]["retired"]
    assert record["git_result"]["exit_code"] is None
    assert record["git_result"]["configuration"]["phase"] == phase
    assert record["git_result"]["configuration"]["exit_code"] is not None
    assert record["git_result"]["configuration"]["output_failed"] is (stop == "pipe")
    assert record["git_result"]["timed_out"] is (stop == "timeout")
    assert "private-config-value" not in json.dumps(record)
    assert path.read_text() == "agent\n"


async def test_git_view_preserves_stash_child_lookup_and_native_shell_helpers(git_repo, tmp_path, monkeypatch):
    git = Path(shutil.which("git")).resolve()
    native_exec = Path(subprocess.check_output([git, "--exec-path"], cwd=git_repo, text=True).rstrip("\n"))
    custom_exec = tmp_path / "git-helpers"
    custom_exec.mkdir()
    for helper in native_exec.iterdir():
        if helper.name != "git":
            (custom_exec / helper.name).symlink_to(helper)
    child_log = tmp_path / "child-args"
    child = custom_exec / "git"
    child.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(child_log))}\n"
        f"exec {shlex.quote(str(git))} \"$@\"\n",
    )
    child.chmod(0o755)
    hook_log = tmp_path / "hook-exec-path"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text(
        f"#!/bin/sh\nset -e\ngit --exec-path >> {shlex.quote(str(hook_log))}\ngit submodule status\n",
    )
    hook.chmod(0o755)
    _git(git_repo, "config", "core.hooksPath", str(hooks))
    monkeypatch.setenv("GIT_EXEC_PATH", str(custom_exec))
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "native hook control")
    assert hook_log.read_text().splitlines()
    assert set(hook_log.read_text().splitlines()) == {str(custom_exec)}
    child_log.write_text("")
    hook_log.write_text("")

    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    path.write_text("agent edit\n")
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "native-child", ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "v1\n"
    assert any(entry.startswith("add ") for entry in child_log.read_text().splitlines())
    assert hook_log.read_text().splitlines()
    assert set(hook_log.read_text().splitlines()) == {str(custom_exec)}
    _git(git_repo, "stash", "apply")
    assert path.read_text() == "agent edit\n"


@pytest.mark.parametrize("global_option", ["separate", "joined", "attr-source", "implicit", "absolute", "worktree-env"])
async def test_git_view_filter_child_uses_explicit_repository(git_repo, tmp_path, monkeypatch, global_option):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "probe.marker", "native")
    _git(other, "config", "core.worktree", str(other))
    (other / "other.txt").write_text("tracked only in the target repository\n")
    _git(other, "add", "other.txt")
    include = tmp_path / "only-main"
    include.write_text("[probe]\nonlyMain = projected\n")
    global_config = tmp_path / "global.config"
    global_config.write_text(f'[includeIf "gitdir:{git_repo}/.git"]\npath = "{include}"\n')
    marker = tmp_path / "filter-result"
    options = ["--git-dir=other/.git"] if global_option == "joined" else ["--git-dir", "other/.git"]
    if global_option == "attr-source":
        options = ["--attr-source", "HEAD", *options]
    command = ["git", "-C", "..", *options]
    if global_option == "implicit":
        command = ["git", "-C", "../other"]
    elif global_option == "absolute":
        command[0] = str(Path(shutil.which("git")).resolve())
    elif global_option == "worktree-env":
        command = ["env", "GIT_WORK_TREE=.", *command]
    commands = [
        shlex.join([*command, *args])
        for args in [("config", "--get-regexp", "^probe\\."), ("ls-files",), ("rev-parse", "--show-toplevel")]
    ]
    filter_script = tmp_path / "filter"
    filter_script.write_text(
        "#!/bin/sh\n{\n" + "\n".join(commands) + f"\n}} > {shlex.quote(str(marker))}\ncat\n",
    )
    filter_script.chmod(0o755)
    (git_repo / ".gitattributes").write_text("a.py filter=probe\n")
    _git(git_repo, "add", ".gitattributes")
    _git(git_repo, "commit", "-qm", "filter attributes")
    _git(git_repo, "config", "filter.probe.clean", str(filter_script))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    # A same-size edit forces status to compare the filtered contents.
    (git_repo / "a.py").write_text("v2\n")
    _git(git_repo, "status", "--porcelain=v1")
    expected_worktree = tmp_path if global_option == "worktree-env" else other
    expected = f"probe.marker native\nother.txt\n{expected_worktree}\n"
    assert marker.read_text() == expected
    marker.unlink()

    head, clean, _ = await fh.capture_git_anchor(str(git_repo))
    assert head and clean is False
    assert marker.read_text() == expected


@pytest.mark.parametrize("exec_dir_kind", ["search-only", "search-only-direct", "loop"])
async def test_git_view_keeps_native_named_helper_fallback(git_repo, tmp_path, monkeypatch, request, exec_dir_kind):
    monkeypatch.setattr(fork_git, "_executions", {})
    helper_dir = tmp_path / "available-helpers"
    helper_dir.mkdir()
    marker = tmp_path / "helper-called"
    helper = helper_dir / "probe-clean-filter"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n")
    helper.chmod(0o755)
    if exec_dir_kind == "loop":
        exec_dir = tmp_path / "recursive"
        exec_dir.symlink_to(exec_dir.name)
        monkeypatch.setenv("PATH", f"{helper_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    else:
        exec_dir = helper_dir
        exec_dir.chmod(0o111)
        request.addfinalizer(lambda: exec_dir.chmod(0o755))
        if os.access(exec_dir, os.R_OK):
            pytest.skip("Test requires a search-only helper directory")
    monkeypatch.setenv("GIT_EXEC_PATH", str(exec_dir))

    (git_repo / ".gitattributes").write_text("a.py filter=probe\n")
    _git(git_repo, "add", ".gitattributes")
    _git(git_repo, "commit", "-qm", "filter attributes")
    filter_command = '"$GIT_EXEC_PATH/probe-clean-filter"' if exec_dir_kind == "search-only-direct" else helper.name
    _git(git_repo, "config", "filter.probe.clean", filter_command)
    _git(git_repo, "config", "filter.probe.required", "true")
    (git_repo / "a.py").write_text("v2\n")
    _git(git_repo, "status", "--porcelain=v1")
    assert marker.exists()
    marker.unlink()

    try:
        head, clean, _ = await fh.capture_git_anchor(str(git_repo))
    finally:
        await asyncio.wait_for(fork_git.drain_git_executions(), 1)
    assert head and clean is False
    assert marker.exists()
    assert not fork_git.git_executions_pending()


async def test_git_view_preserves_filter_path_expansion_when_restoring_a_deletion(git_repo, tmp_path):
    path = git_repo / "space and 'quote'.txt"
    path.write_text("tracked\n")
    (git_repo / ".gitattributes").write_text("*.txt filter=probe\n")
    marker = tmp_path / "smudge-args"
    _git(git_repo, "config", "filter.probe.required", "true")
    _git(git_repo, "config", "filter.probe.clean", "cat")
    _git(git_repo, "config", "filter.probe.smudge",
         f"printf '%%s\\n' %f '%%f' \"$0\" | cat > {shlex.quote(str(marker))}\ncat")
    _git(git_repo, "add", ".gitattributes", path.name)
    _git(git_repo, "commit", "-qm", "filter baseline")
    path.unlink()
    _git(git_repo, "checkout", "--", path.name)
    expected = marker.read_text()
    assert expected.splitlines()[:2] == [path.name, "%f"]
    marker.unlink()

    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    _git(git_repo, "rm", path.name)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "smudge", ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "tracked\n"
    assert marker.read_text() == expected


@pytest.mark.parametrize("linked", [False, True])
async def test_git_view_filter_preserves_native_git_directory(git_repo, tmp_path, linked):
    _git(git_repo, "config", "filter.probe.required", "true")
    (git_repo / ".gitattributes").write_text("a.py filter=probe\n")
    _git(git_repo, "config", "filter.probe.clean", "cat")
    _git(git_repo, "config", "filter.probe.smudge", "cat")
    _git(git_repo, "add", ".gitattributes")
    _git(git_repo, "commit", "-qm", "filter attributes")
    if linked:
        worktree = tmp_path / "linked"
        _git(git_repo, "worktree", "add", "--detach", str(worktree))
    else:
        worktree = git_repo
    marker = tmp_path / "filter-git-dir"
    _git(git_repo, "config", "filter.probe.clean",
         f'test -f "${{GIT_DIR:-.git}}/index" && printf \'%s\\n\' "${{GIT_DIR-}}"'
         f' > {shlex.quote(str(marker))} && cat')
    # Match the tracked size so status checks the value produced by the filter.
    (worktree / "a.py").write_text("v2\n")
    _git(worktree, "status", "--porcelain=v1")
    expected = marker.read_text()
    assert bool(expected.rstrip("\n")) is linked
    marker.unlink()

    head, clean, _ = await fh.capture_git_anchor(str(worktree))
    assert head and clean is False
    assert marker.read_text() == expected


async def test_git_view_restore_keeps_checkout_hook_writes(git_repo, tmp_path):
    hook = git_repo / ".git" / "hooks" / "post-checkout"
    marker = tmp_path / "hook-path"
    hook.write_text(
        '#!/bin/sh\ngit config --local probe.hook yes\n'
        f'printf "%s\\n" "$0" > {shlex.quote(str(marker))}\n',
    )
    hook.chmod(0o755)
    path = git_repo / "a.py"
    _git(git_repo, "rm", path.name)
    _git(git_repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", path.name)
    command = ["git", "config", "--local", "--get", "probe.hook"]
    assert subprocess.check_output(command, cwd=git_repo, text=True) == "yes\n"
    native_hook_path = marker.read_text()
    _git(git_repo, "config", "--local", "--unset", "probe.hook")

    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    _git(git_repo, "rm", path.name)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "hook", ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "v1\n"
    assert subprocess.check_output(command, cwd=git_repo, text=True) == "yes\n"
    assert marker.read_text() == native_hook_path


@pytest.mark.parametrize("advice", [True, False])
@pytest.mark.parametrize("access", ["no-execute", "no-directory-search", "no-ancestor-search"])
async def test_git_view_restore_preserves_ignored_hook_advice(git_repo, advice, access):
    hooks = git_repo / ".git" / "hooks"
    if access == "no-ancestor-search":
        hooks = git_repo / ".git" / "private" / "hooks"
        hooks.mkdir(parents=True)
        _git(git_repo, "config", "core.hooksPath", str(hooks.relative_to(git_repo)))
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 17\n")
    hook.chmod(0o644 if access == "no-execute" else 0o755)
    path = git_repo / "a.py"
    _git(git_repo, "config", "advice.ignoredHook", str(advice).lower())
    args = ("restore", "--source=HEAD", "--staged", "--worktree", "--", path.name)
    directory = hooks.parent if access == "no-ancestor-search" else hooks
    mode = directory.stat().st_mode
    try:
        if access != "no-execute":
            directory.chmod(0o600)
            if os.access(directory, os.X_OK):
                pytest.skip("Test requires a hook directory without search permission")
        _git(git_repo, "rm", path.name)
        native = subprocess.run(["git", *args], cwd=git_repo, text=True, capture_output=True)
        assert native.returncode == 0
        assert ("not set as executable" in native.stderr) is advice
        assert path.read_text() == "v1\n"
        _git(git_repo, "rm", path.name)
        result = await fh._git(str(git_repo), "--literal-pathspecs", *args)
    finally:
        directory.chmod(mode)
    assert result.returncode == native.returncode and not result.binding_failed
    assert result.stderr == native.stderr
    assert path.read_text() == "v1\n"


@pytest.mark.parametrize("change", [
    "during-hook", "repository-before-launch", "hooks-before-launch", "hooks-without-original",
])
async def test_git_view_hook_checks_binding_before_launch(git_repo, tmp_path, monkeypatch, change):
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _git(replacement, "init", "-q")
    entered, proceed = tmp_path / "hook-entered", tmp_path / "hook-proceed"
    hook = git_repo / ".git" / "hooks" / "post-checkout"
    pause = (
        f'touch {shlex.quote(str(entered))}\n'
        f'while [ ! -f {shlex.quote(str(proceed))} ]; do sleep 0.01; done\n'
    )
    if change != "hooks-without-original":
        hook.write_text("#!/bin/sh\n" + (pause if change == "during-hook" else "") + "git config --local probe.hook yes\n")
        hook.chmod(0o755)
    other_hook = replacement / ".git" / "hooks" / "post-checkout"
    other_hook.write_text("#!/bin/sh\ngit config --local probe.hook replacement\n")
    other_hook.chmod(0o755)
    path = git_repo / "a.py"
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    _git(git_repo, "rm", path.name)
    relative = Path(".git/hooks") if change.startswith("hooks-") else Path(".git")
    directory, original = git_repo / relative, tmp_path / "original"
    other_directory = replacement / relative
    spawn = fork_git._spawn_git

    async def replace_inside_callback(args, **kwargs):
        if "restore" in args and change == "hooks-without-original":
            directory.rename(original)
            other_directory.rename(directory)
            try:
                process = await spawn(args, **kwargs)
                await asyncio.to_thread(process.wait, 3)
                return process
            finally:
                directory.rename(other_directory)
                original.rename(directory)
        if "restore" in args and change != "during-hook":
            view = Path(kwargs["repository_env"]["OCTOPUS_GIT_VIEW"])
            with (view / "native-callback-env").open("a") as source:
                source.write(pause)
        process = await spawn(args, **kwargs)
        if "restore" not in args:
            return process
        try:
            async with asyncio.timeout(3):
                while not entered.exists():
                    await asyncio.sleep(0.01)
            directory.rename(original)
            other_directory.rename(directory)
            proceed.touch()
            await asyncio.to_thread(process.wait, 3)
        finally:
            proceed.touch()
            if original.exists():
                directory.rename(other_directory)
                original.rename(directory)
        return process

    monkeypatch.setattr(fork_git, "_spawn_git", replace_inside_callback)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "inside-hook", ignored)
    if change == "during-hook":
        assert record["status"] == "completed", record
        assert subprocess.check_output(
            ["git", "config", "--local", "--get", "probe.hook"], cwd=git_repo, text=True,
        ) == "yes\n"
    else:
        assert record["status"] == "failed" and record["git_result"]["binding_failed"], record
        assert record["git_result"]["retired"]
        assert subprocess.run(
            ["git", "config", "--local", "--get", "probe.hook"], cwd=git_repo, capture_output=True,
        ).returncode == 1
    assert path.read_text() == "v1\n"
    assert record["stash_ref"] == subprocess.check_output(
        ["git", "rev-parse", "refs/stash"], cwd=git_repo, text=True,
    ).strip()
    assert subprocess.run(
        ["git", "config", "--local", "--get", "probe.hook"], cwd=replacement, capture_output=True,
    ).returncode == 1


async def test_git_view_retains_native_hook_path_in_separate_common_directory(git_repo, tmp_path):
    gitdir = git_repo / ".git"
    common = tmp_path / "common"
    common.mkdir()
    for entry in ("config", "objects", "refs", "hooks", "logs"):
        (gitdir / entry).rename(common / entry)
    (gitdir / "commondir").write_text(os.path.relpath(common, gitdir) + "\n")
    marker = tmp_path / "hook-path"
    hook = common / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/bash\nprintf "%s\\n" "$0" > {shlex.quote(str(marker))}\n')
    hook.chmod(0o755)
    path = git_repo / "a.py"
    args = ("restore", "--source=HEAD", "--staged", "--worktree", "--", path.name)
    _git(git_repo, "rm", path.name)
    _git(git_repo, *args)
    native = marker.read_text()
    assert native == str(hook) + "\n"
    marker.unlink()

    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    _git(git_repo, "rm", path.name)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "common-hook", ignored)
    assert record["status"] == "completed", record
    assert path.read_text() == "v1\n"
    assert marker.read_text() == native


async def test_git_view_hook_can_select_another_git_repository(git_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    hook = git_repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\ngit --git-dir={shlex.quote(str(other / '.git'))} config --local probe.target other\n",
    )
    hook.chmod(0o755)
    path = git_repo / "a.py"
    args = ("restore", "--source=HEAD", "--staged", "--worktree", "--", path.name)
    _git(git_repo, "rm", path.name)
    _git(git_repo, *args)
    command = ["git", "config", "--local", "--get", "probe.target"]
    assert subprocess.check_output(command, cwd=other, text=True) == "other\n"
    _git(other, "config", "--local", "--unset", "probe.target")
    _git(git_repo, "rm", path.name)
    result = await fh._git(str(git_repo), "--literal-pathspecs", *args)
    assert result.returncode == 0, result.stderr
    assert subprocess.check_output(command, cwd=other, text=True) == "other\n"
    assert subprocess.run(command, cwd=git_repo, capture_output=True).returncode == 1


@pytest.mark.parametrize("default_git", ["named", "absolute"])
async def test_git_view_linked_hook_keeps_common_directory_and_can_select_another_repository(git_repo, tmp_path, default_git):
    linked = tmp_path / "linked"
    _git(git_repo, "worktree", "add", "--detach", str(linked), "HEAD")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    common_ref = Path(subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "commondir"], cwd=linked, text=True,
    ).strip())
    original = common_ref.read_bytes()
    hook = git_repo / ".git" / "hooks" / "post-checkout"
    git = shutil.which("git") if default_git == "absolute" else "git"
    assert git is not None
    # The native hook starts with the correct common directory. Only change
    # the locator during the two child calls and restore it before Git exits.
    hook.write_text(
        f"#!/bin/sh\ncommondir={shlex.quote(str(common_ref))}\n"
        f"contents={shlex.quote(os.fsdecode(original))}\n"
        "trap 'printf \"%s\" \"$contents\" > \"$commondir\"' EXIT\n"
        f"printf '%s\\n' {shlex.quote(str(other / '.git'))} > \"$commondir\"\n"
        f"{shlex.quote(git)} config --local probe.default original\n"
        f"git --git-dir={shlex.quote(str(other / '.git'))} config --local probe.explicit other\n",
    )
    hook.chmod(0o755)
    _git(linked, "rm", "a.py")
    result = await fh._git(str(linked), "--literal-pathspecs", "restore",
                           "--source=HEAD", "--staged", "--worktree", "--", "a.py")
    assert result.returncode == 0 and not result.binding_failed, result.stderr
    assert common_ref.read_bytes() == original
    command = ["git", "config", "--local", "--get"]
    assert subprocess.check_output([*command, "probe.default"], cwd=git_repo, text=True) == "original\n"
    assert subprocess.check_output([*command, "probe.explicit"], cwd=other, text=True) == "other\n"
    assert subprocess.run([*command, "probe.default"], cwd=other, capture_output=True).returncode == 1
    assert subprocess.run([*command, "probe.explicit"], cwd=git_repo, capture_output=True).returncode == 1


@pytest.mark.parametrize("script", ["bash", "bash-project-root", "no-shebang"])
async def test_git_view_checkout_runs_native_shell_hook_formats(git_repo, tmp_path, script):
    hooks = git_repo / ".git" / "hooks"
    marker = tmp_path / "hook-ran"
    (hooks / "neighbor").write_text(f"printf done > {shlex.quote(str(marker))}\n")
    hook = hooks / "post-checkout"
    bodies = {
        "bash": '#!/bin/bash\n. "$(dirname "${BASH_SOURCE[0]}")/neighbor"\n',
        "bash-project-root": (
            '#!/bin/bash\ncd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 31\n'
            'test -f a.py || exit 32\n. .git/hooks/neighbor\n'
        ),
        "no-shebang": ". .git/hooks/neighbor\n",
    }
    hook.write_text(bodies[script])
    hook.chmod(0o755)
    args = ("restore", "--source=HEAD", "--staged", "--worktree", "--", "a.py")
    _git(git_repo, "rm", "a.py")
    _git(git_repo, *args)
    assert marker.read_text() == "done"
    marker.unlink()
    _git(git_repo, "rm", "a.py")
    result = await fh._git(str(git_repo), "--literal-pathspecs", *args)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "done"


@pytest.mark.parametrize("change", ["script-before-exec", "directory-during-lookup", "directory-during-validation"])
def test_git_hook_replacement_cannot_bypass_launch_checks(git_repo, tmp_path, change):
    hook = git_repo / ".git" / "hooks" / "post-checkout"
    replacement = tmp_path / "replacement"
    marker = tmp_path / "hook-effect"
    if change != "directory-during-lookup":
        hook.write_text(f"#!/bin/bash\nprintf original > {shlex.quote(str(marker))}\n")
        hook.chmod(0o755)
    if change.startswith("directory-"):
        replacement.mkdir()
        replacement_script = replacement / hook.name
    else:
        replacement_script = replacement
    replacement_script.write_text(f"#!/bin/bash\nprintf replacement > {shlex.quote(str(marker))}\n")
    replacement_script.chmod(0o755 if change.startswith("directory-") else 0o644)
    (tmp_path / "callback-repository.json").write_text(json.dumps({
        "worktree": str(git_repo), "root": str(git_repo), "git": str(git_repo / ".git"),
        "common": str(git_repo / ".git"), "original_git_dir": None,
    }))
    launcher = Path(fork_git.__file__).with_name("git_view_child.py")
    probe = """
import os
import runpy
import sys

runner, hook, replacement, change = sys.argv[1:]
native = ".git/hooks/post-checkout"
directory = os.path.dirname(hook)
retained = os.open(directory, os.O_PATH | os.O_DIRECTORY)
module = runpy.run_path(runner)
def replace_directory():
    os.rename(directory, replacement + "-old")
    os.rename(replacement, directory)

if change == "directory-during-validation":
    binding = module["RepositoryBinding"]
    check = binding.require_current

    def replace_during_validation(self):
        check(self)
        replace_directory()

    binding.require_current = replace_during_validation
elif change == "directory-during-lookup":
    path_type = module["Path"]
    stat = path_type.stat

    def replace_during_lookup(path, *args, **kwargs):
        try:
            return stat(path, *args, **kwargs)
        finally:
            if os.fspath(path) == native:
                replace_directory()

    path_type.stat = replace_during_lookup
else:
    execv = os.execv

    def replace_before_exec(path, args):
        if os.fspath(path) == native:
            os.replace(replacement, hook)
        return execv(path, args)

    os.execv = replace_before_exec
module["execute_hook"](f"/proc/self/fd/{retained}", native, "git", [])
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(launcher), str(hook), str(replacement), change], cwd=git_repo,
        env={**os.environ, "OCTOPUS_GIT_VIEW": str(tmp_path)},
        capture_output=True, text=True, timeout=5,
    )
    assert not replacement.exists()
    binding_failed = (tmp_path / "callback-binding-failed").exists()
    if change.startswith("directory-"):
        assert result.returncode != 0 and binding_failed, result
    else:
        assert result.returncode == 0 and "not set as executable" in result.stderr, result.stderr
        assert not binding_failed
    assert not marker.exists()


async def test_stash_store_retains_snapshot_when_binding_changes_after_git_exits(git_repo, tmp_path, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    path = git_repo / "a.py"
    _git(git_repo, "rm", path.name)
    hook = git_repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text('#!/bin/sh\ngit config --local probe.refhook "$1"\n')
    hook.chmod(0o755)
    gitdir, moved = git_repo / ".git", tmp_path / "original"
    spawn = fork_git._spawn_git

    async def move_after_store(args, **kwargs):
        process = await spawn(args, **kwargs)
        if "stash" in args and "store" in args:
            assert await asyncio.to_thread(process.wait, 3) == 0
            gitdir.rename(moved)
        assert "restore" not in args
        return process

    monkeypatch.setattr(fork_git, "_spawn_git", move_after_store)
    try:
        record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "store-move", ignored)
    finally:
        if moved.exists():
            moved.rename(gitdir)
    assert record["status"] == "failed" and record["git_result"]["binding_failed"]
    assert record["git_result"]["exit_code"] == 0
    assert not path.exists()
    assert record["stash_ref"] == subprocess.check_output(
        ["git", "rev-parse", "refs/stash"], cwd=git_repo, text=True,
    ).strip()
    assert subprocess.check_output(
        ["git", "config", "--local", "--get", "probe.refhook"], cwd=git_repo, text=True,
    ) == "committed\n"


@pytest.mark.parametrize("shell", ["sh", "bash"])
async def test_git_view_restore_checks_hooks_when_git_invokes_them(git_repo, shell):
    path = git_repo / "a.py"
    hooks = git_repo / ".git" / "hooks"
    checkout = hooks / "post-checkout"
    index_changed = hooks / "post-index-change"
    locate_checkout = (
        'checkout="$(dirname "${BASH_SOURCE[0]}")/post-checkout"'
        if shell == "bash" else f"checkout={shlex.quote(str(checkout))}"
    )
    index_changed.write_text(
        f"#!/bin/{shell}\n{locate_checkout}\n"
        "if [ -f a.py ]; then\n"
        "  printf '#!/bin/sh\\nexit 17\\n' > \"$checkout\"\n"
        '  chmod +x "$checkout"\n'
        "fi\n",
    )
    index_changed.chmod(0o755)
    _git(git_repo, "rm", path.name)
    native = subprocess.run(
        ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", path.name], cwd=git_repo,
    )
    assert native.returncode == 17
    checkout.unlink()
    assert path.read_text() == "v1\n"
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    _git(git_repo, "rm", path.name)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "new-hook", ignored)
    assert checkout.is_file()
    assert record["status"] == "failed" and record["git_result"]["exit_code"] == 17
    assert path.read_text() == "v1\n"
    assert record["stash_ref"] == subprocess.check_output(
        ["git", "rev-parse", "refs/stash"], cwd=git_repo, text=True,
    ).strip()


async def test_git_view_restore_refuses_hooks_after_repository_path_replacement(git_repo, tmp_path, monkeypatch):
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _git(replacement, "init", "-q")
    marker = tmp_path / "hook-ran"
    for repo in (git_repo, replacement):
        hook = repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\necho {repo.name} >> {shlex.quote(str(marker))}\n")
        hook.chmod(0o755)
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    assert head and clean
    path = git_repo / "a.py"
    _git(git_repo, "rm", path.name)
    original_gitdir = tmp_path / "original"
    gitdir = git_repo / ".git"
    spawn = fork_git._spawn_git

    async def replace_during_restore(args, **kwargs):
        if "restore" not in args:
            return await spawn(args, **kwargs)
        gitdir.rename(original_gitdir)
        (replacement / ".git").rename(gitdir)
        try:
            process = await spawn(args, **kwargs)
            await asyncio.to_thread(process.wait, 3)
            return process
        finally:
            gitdir.rename(replacement / ".git")
            original_gitdir.rename(gitdir)

    monkeypatch.setattr(fork_git, "_spawn_git", replace_during_restore)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "hook-swap", ignored)
    assert record["status"] == "failed" and record["git_result"]["binding_failed"]
    assert record["stash_ref"] and path.read_text() == "v1\n"
    assert not marker.exists()


async def test_git_view_preserves_a_command_config_entry_without_a_value(git_repo, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.excludesfile'")
    command = ("status", "--porcelain=v1")
    native = subprocess.run(["git", *command], cwd=git_repo, capture_output=True, text=True)
    assert native.returncode != 0 and "missing value" in native.stderr

    observed = await fh._git(str(git_repo), *command, timeout=10)
    assert observed.returncode == native.returncode
    assert "missing value" in observed.stderr


async def test_git_view_cleanup_failure_preserves_retirement(git_repo, monkeypatch, caplog):
    original = fork_git.TemporaryDirectory.cleanup
    retained = []

    def fail_cleanup(view):
        retained.append(view)
        raise OSError("view cleanup unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(fork_git.TemporaryDirectory, "cleanup", fail_cleanup)
        result = await fh._git(str(git_repo), "status", "--porcelain=v1")
    assert result.returncode == 0
    assert not fork_git.git_executions_pending()
    assert "Git directory view could not be removed" in caplog.text
    for view in retained:
        original(view)


async def test_unlaunched_stash_removes_unused_logs_directory(git_repo, tmp_path, monkeypatch):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    logs = git_repo / ".git" / "logs"
    logs.rename(tmp_path / "existing-logs")
    path = git_repo / "a.py"
    path.write_text("agent\n")
    spawn = fork_git._spawn_git

    async def refuse_stash(args, **kwargs):
        if "stash" in args and "push" in args:
            raise FileNotFoundError("Git failed to launch")
        return await spawn(args, **kwargs)

    monkeypatch.setattr(fork_git, "_spawn_git", refuse_stash)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "unlaunched", ignored)
    assert record["status"] == "failed" and not record["ran"]
    assert not logs.exists()
    assert path.read_text() == "agent\n"


async def test_revert_refuses_changed_repository_binding_before_writing(git_repo, tmp_path, monkeypatch):
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _git(replacement, "init", "-q")
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    path.write_text("agent edit\n")
    original_git = fh._git

    async def replace_metadata(cwd, *args, **kwargs):
        if args[:3] == ("--literal-pathspecs", "stash", "push"):
            (git_repo / ".git").rename(tmp_path / "saved-metadata")
            (replacement / ".git").rename(git_repo / ".git")
        return await original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", replace_metadata)
    record = await fh.safe_revert_files(str(git_repo), [str(path)], head, clean, "changed-repository", ignored)
    assert record["status"] == "failed" and not record["ran"]
    assert record["git_result"]["exit_code"] is None
    assert record["stash_ref"] is None
    assert path.read_text() == "agent edit\n"


@pytest.mark.parametrize("porcelain", [" M a.py", "bad\0", "XX a.py\0", "R  renamed\0", " M \0"])
async def test_safe_revert_refuses_malformed_git_status(git_repo, monkeypatch, porcelain):
    head, clean, ignored = await fh.capture_git_anchor(str(git_repo))
    path = git_repo / "a.py"
    path.write_text("agent")
    real_git = fh._git

    async def git(cwd, *args, **kwargs):
        if args[0] == "status":
            return fork_git.GitResult(0, porcelain, "")
        return await real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fh, "_git", git)
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, clean, "malformed", fork_ignored_baseline=ignored)
    assert record["status"] == "refused" and "parsed safely" in record["refused_reason"]
    assert path.read_text() == "agent"
    assert subprocess.run(["git", "stash", "list"], cwd=git_repo, capture_output=True, check=True).stdout == b""


@pytest.mark.asyncio
async def test_safe_revert_removes_untracked_agent_file(git_repo):
    # Regression for the real-CLI failure: an agent-created (untracked) file
    # must be removed by revert without a `git checkout` pathspec error.
    head, _, ignored = await fh.capture_git_anchor(str(git_repo))
    (git_repo / "notes.txt").write_text("HELLO\n")  # new, untracked
    record = await fh.safe_revert_files(
        str(git_repo), [str(git_repo / "notes.txt")], head, True, "fork-unt", ignored
    )
    assert record["status"] == "completed", record
    assert not (git_repo / "notes.txt").exists()  # removed → fork-point state
    out = subprocess.run(["git", "stash", "list"], cwd=git_repo,
                         capture_output=True, text=True).stdout
    assert "octopus: pre-fork stash fork-unt" in out


@pytest.mark.asyncio
async def test_safe_revert_refused_when_fork_point_not_clean(git_repo):
    head, _, _ = await fh.capture_git_anchor(str(git_repo))
    (git_repo / "a.py").write_text("dirty\n")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, False, "f")
    assert record["status"] == "refused"
    assert "clean" in record["refused_reason"].lower()


@pytest.mark.asyncio
async def test_safe_revert_refused_when_head_moved(git_repo):
    record = await fh.safe_revert_files(
        str(git_repo), ["a.py"], "0" * 40, True, "f"
    )
    assert record["status"] == "refused"
    assert "HEAD" in record["refused_reason"]


@pytest.mark.asyncio
async def test_safe_revert_refused_when_unknown_dirty(git_repo):
    head, _, _ = await fh.capture_git_anchor(str(git_repo))
    # A file the agent never touched is dirty → refuse to protect user work.
    (git_repo / "human.py").write_text("hand-written\n")
    record = await fh.safe_revert_files(str(git_repo), ["a.py"], head, True, "f")
    assert record["status"] == "refused"
    assert "didn't touch" in record["refused_reason"]


@pytest.mark.asyncio
async def test_safe_revert_refused_non_git(tmp_path):
    record = await fh.safe_revert_files(str(tmp_path), ["a.py"], "abc", True, "f")
    assert record["status"] == "refused"
    assert "git repo" in record["refused_reason"].lower()


# ---------------------------------------------------------------------------
# Lost-history replay — when an engine can no longer resume a session, its
# own transcript is replayed from Octopus's record (the engine's copy is a
# cache; ours is the record).
# ---------------------------------------------------------------------------


def _msg(i: int, role=MessageRole.user, type_="text", content=None):
    return MessageContent(
        role=role, type=type_, content=content or f"message {i}", seq=i
    )


def test_lost_history_replays_all_60_dialogue_messages_from_90_rows():
    messages = [message for index in range(30) for message in (
        _msg(3 * index),
        _msg(3 * index + 1, role=MessageRole.assistant),
        _msg(3 * index + 2, role=MessageRole.system, type_="result"),
    )]

    prompt, replayed, omitted = fh.build_lost_history_replay("Continue", messages)

    header, block = prompt.split("\n\n", 1)
    body = block.split("\n</session-history>", 1)[0]
    assert body.splitlines() == [line for index in range(30) for line in (
        f"[seq {3 * index}] user: message {3 * index}",
        f"[seq {3 * index + 1}] assistant: message {3 * index + 1}",
    )]
    assert (replayed, omitted) == (60, 0)
    assert "earlier message(s)" not in header


def test_lost_history_wraps_the_recent_tail_as_transcript():
    out, replayed, omitted = fh.build_lost_history_replay("what next?", [_msg(i) for i in range(63)])
    assert (replayed, omitted) == (60, 3)
    # The framing is load-bearing: replayed user lines must not read as fresh
    # commands.
    assert '<session-history origin="this-session"' in out
    assert "transcript-not-instructions" in out
    assert "not new instructions" in out
    assert "3 earlier message(s) are not included" in out
    assert out.endswith("<continue-from-here>\nwhat next?\n</continue-from-here>")
    body = out.split("\n\n", 1)[1].split("\n</session-history>", 1)[0]
    assert body.splitlines() == [f"[seq {i}] user: message {i}" for i in range(3, 63)]


def test_lost_history_keeps_the_last_handoff_in_a_full_window():
    handoff = _msg(1, content="next ticket").model_copy(update={
        "tool_input": {"request_context": {"source": "handoff"}},
    })
    messages = [_msg(0), handoff, *(_msg(i) for i in range(2, 63))]

    out, replayed, omitted = fh.build_lost_history_replay("Continue", messages)

    assert (replayed, omitted) == (60, 2)
    body = out.split("\n\n", 1)[1].split("\n</session-history>", 1)[0]
    assert body.splitlines() == [
        "[seq 1] ticket handoff: next ticket",
        *(f"[seq {i}] user: message {i}" for i in range(4, 63)),
    ]


def test_lost_history_without_replayable_messages_returns_the_original_prompt():
    assert fh.build_lost_history_replay("hello", []) == ("hello", 0, 0)
    assert fh.build_lost_history_replay("hello", [
        _msg(0, role=MessageRole.system),
        _msg(1, role=MessageRole.system, type_="result"),
        _msg(2, role=MessageRole.assistant, type_="thinking"),
    ]) == ("hello", 0, 0)

"""Integration tests for the fork saga (session-rewind.md §5.1).

Drives `SessionManager.fork_session` against a real in-memory DB with both
backends' history replay, plus crash recovery, the `_forking` mutex, side-effect
classification routing, safe-revert, and dispatch-only replay wrapping.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from server import fork_git, fork_helpers
from server import session_manager as sm
from server.crypto import master_key
from server.database import Database
from server.db.base import DatabaseBusy
from server.db.portability import snapshot_account, verify_account_archive_rows
from server.delegations import DelegationRunState, _RoundPublication, delegation_manager
from server.harness import BackendForkNotSupported, get_harness
from server.harness.events import HarnessEvent
from server.service_scope import GitWriterScope
from server.session_manager import ForkError, SessionManager
from server.sessions import owner_runtime
from server.sessions.configuration import SessionConfigurationError
from server.sessions.owner_runtime import ReserveFork
from server.sessions.publication import WorkPublication
from server.sessions.runtime_types import SessionAdmissionClosed
from tests.accounts import create_account
from tests.fake_run import FakeRunBase, model_run_factory


@pytest.fixture
async def manager(tmp_path):
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    await mgr.initialize(db)
    try:
        yield mgr
    finally:
        await db.close()


async def _seed_parent(mgr, *, backend="claude-code", working_dir=None,
                       n_user=3, git=(None, None)):
    """Parent with n_user user/assistant turn pairs; user rows carry the git
    anchor tuple so revert preflight has data."""
    agent = await mgr.db.get_system_agent()
    parent = await mgr.create_session(
        agent["id"], "Parent", working_dir, backend=backend
    )
    seq = 0
    for i in range(n_user):
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="user", type="text",
            content=f"q{i}", git_head=git[0], git_status_clean=git[1],
        )
        seq += 1
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="assistant", type="text",
            content=f"a{i}",
        )
        seq += 1
    parent._message_count = seq
    return parent


# ------------------------------------------------------------------ happy path


@pytest.mark.asyncio
async def test_fork_claude_history_replay(manager):
    # Claude forks use HISTORY_REPLAY (not native synth-resume — see
    # claude_code.py / Phase-5 finding), same contract as Codex.
    parent = await _seed_parent(manager, backend="claude-code")
    # Rewind to user message at seq=2 (the 2nd user turn). Copies seq < 2.
    fork = await manager.fork_session(parent.id, 2)

    assert fork.origin == "fork"
    assert fork.forked_from_session_id == parent.id
    assert fork.fork_after_seq == 1
    assert fork.fork_needs_replay is True  # replay on first turn
    assert fork.claude_session_id is None  # claude's own id arrives turn 1
    assert fork.fork_status == "ready"
    # Pi-style boundary: copied seq < 2 → 2 rows, next seq = 2.
    assert fork._message_count == 2
    copied = await manager.db.load_messages(fork.id)
    assert [m["seq"] for m in copied] == [0, 1]
    # Ephemeral metadata holds the prefilled prompt (parent's seq-2 text).
    meta = json.loads(fork.fork_metadata)
    assert meta["prefilled_prompt"] == "q1"  # seq 2 is the 2nd user turn ("q1")
    # Rewind, not branch: the fork inherits the parent's name and the parent
    # is archived so the fork takes its place in the active list.
    assert fork.name == parent.name == "Parent"
    assert parent.id not in manager.sessions
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[parent.id]["archived"] == 1
    assert rows[fork.id]["archived"] == 0


@pytest.mark.asyncio
async def test_fork_codex_history_replay(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    assert fork.fork_needs_replay is True
    assert fork.claude_session_id is None  # resume id arrives on thread.started
    assert fork.fork_status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_first", [True, False])
async def test_duplicate_uses_the_engine_at_its_reservation(manager, tmp_path, monkeypatch, switch_first):
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    parent = await _seed_parent(manager, working_dir=str(parent_dir), n_user=0)
    owner = manager._session_owner(parent)
    real_call = owner.call_runtime
    reserved = asyncio.Event()
    release = asyncio.Event()
    selected = []
    real_resolve = manager.resolve_credential_by_id

    async def call(command):
        if not isinstance(command, ReserveFork) or not command.reserve:
            return await real_call(command)
        if switch_first:
            reserved.set()
            await release.wait()
            return await real_call(command)
        snapshot = await real_call(command)
        reserved.set()
        await release.wait()
        return snapshot

    async def resolve(credential_id, **kwargs):
        selected.append(kwargs["backend"])
        return await real_resolve(credential_id, **kwargs)

    monkeypatch.setattr(owner, "call_runtime", call)
    monkeypatch.setattr(manager, "resolve_credential_by_id", resolve)
    copying = asyncio.create_task(manager.duplicate_session(parent.id))
    fields = {"expected_backend": "claude-code", "expected_configuration_version": parent.configuration_version,
              "backend": "codex", "model": None,
              "reasoning_effort": None, "service_tier": None, "credential_id": None}
    try:
        await asyncio.wait_for(reserved.wait(), 2)
        if switch_first:
            await manager.update_session_configuration(parent.id, fields)
        else:
            with pytest.raises(SessionConfigurationError) as refused:
                await manager.update_session_configuration(parent.id, fields)
            assert refused.value.code == "conversation_busy"
    finally:
        release.set()
        duplicate = await asyncio.wait_for(copying, 5)
    expected = "codex" if switch_first else "claude-code"
    assert duplicate.backend == expected
    assert selected == [expected]
    if not switch_first:
        assert (await manager.update_session_configuration(parent.id, fields)).backend == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("full_copy", [False, True])
async def test_fork_settings_snapshot_matches_database_during_parent_update(manager, tmp_path, monkeypatch, full_copy):
    received = []
    manager.on_broadcast("test", lambda event: asyncio.sleep(0, result=received.append(event)))
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    parent = await _seed_parent(manager, backend="codex", working_dir=str(parent_dir))
    parent.model = "original-model"
    parent.reasoning_effort = "low"
    parent.service_tier = "fast"
    original = manager.db.create_fork_session

    async def mutate_after_insert(**kwargs):
        await original(**kwargs)
        # A successful concurrent PATCH can change the parent while the fork
        # saga is awaiting DB or filesystem work.
        async with manager.configuration_lock:
            parent.model = "updated-model"
            parent.reasoning_effort = "high"
            parent.service_tier = "default"
            await manager.db.update_session_field(
                parent.id, model=parent.model, reasoning_effort=parent.reasoning_effort,
                service_tier=parent.service_tier,
            )

    monkeypatch.setattr(manager.db, "create_fork_session", mutate_after_insert)
    fork = await manager.duplicate_session(parent.id) if full_copy else await manager.fork_session(parent.id, 2)
    assert (fork.model, fork.reasoning_effort, fork.service_tier) == ("original-model", "low", "fast")
    if not full_copy:
        event = next(event for event in received if event["type"] == "session_archived")
        assert event["new_session_id"] == fork.id
        assert (event["model"], event["reasoning_effort"], event["service_tier"]) == (
            fork.model, fork.reasoning_effort, fork.service_tier,
        )
    restored = SessionManager()
    await restored.initialize(manager.db)
    loaded = restored.get_session(fork.id)
    assert (loaded.model, loaded.reasoning_effort, loaded.service_tier) == (fork.model, fork.reasoning_effort, fork.service_tier)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_fork_resume_id_null_survives_restart(manager, backend):
    # A rewind establishes native identity only on its first user input.
    parent = await _seed_parent(manager, backend=backend)
    fork = await manager.fork_session(parent.id, 2)
    assert fork.claude_session_id is None
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[fork.id]["claude_session_id"] is None
    # Restart: reload over the same DB — still None.
    mgr2 = SessionManager()
    await mgr2.initialize(manager.db)
    assert mgr2.get_session(fork.id).claude_session_id is None
    assert mgr2.get_session(fork.id).fork_needs_replay is True


@pytest.mark.asyncio
async def test_fork_m0_empty(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    parent = await _seed_parent(manager, backend="claude-code")
    fork = await manager.fork_session(parent.id, 0)
    assert fork.fork_after_seq == -1
    assert fork._message_count == 0
    assert await manager.db.load_messages(fork.id) == []
    meta = json.loads(fork.fork_metadata)
    assert meta["prefilled_prompt"] == "q0"  # the original first user message


@pytest.mark.asyncio
async def test_fork_m0_codex_still_wraps_replay(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 0)
    # Even with an empty history, Codex forks need replay so the first-turn
    # shape stays uniform.
    assert fork.fork_needs_replay is True


@pytest.mark.asyncio
async def test_fork_of_fork(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    # Give the fork some messages so it can be re-forked.
    await manager.db.append_message(session_id=fork.id, seq=2, role="user",
                                    type="text", content="fork-q")
    fork._message_count = 3
    grandchild = await manager.fork_session(fork.id, 2)
    assert grandchild.forked_from_session_id == fork.id
    # Each fork archives its (now-replaced) parent — only the tip stays live.
    assert parent.id not in manager.sessions
    assert fork.id not in manager.sessions
    assert grandchild.id in manager.sessions


@pytest.mark.asyncio
async def test_fork_repoints_schedules_to_the_successor(manager):
    # Mirrors archive_session's tail: a `/schedule` anchored on the parent
    # follows onto the fork, so its next fire lands in the live thread rather
    # than a session the user has moved on from.
    parent = await _seed_parent(manager, backend="codex")
    await manager.db.save_schedule(
        schedule_id="sch1", agent_id=parent.agent_id, name="nightly",
        prompt="do it", created_at="2026-06-08T00:00:00+00:00",
        interval_seconds=3600, origin_session_id=parent.id,
    )

    fork = await manager.fork_session(parent.id, 2)

    schedules = {s["id"]: s for s in await manager.db.load_schedules()}
    assert schedules["sch1"]["origin_session_id"] == fork.id


# ------------------------------------------------------------------ validation


@pytest.mark.asyncio
async def test_fork_rejects_unknown_seq(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 999)
    assert ei.value.reason == "invalid_rewind_seq"


@pytest.mark.asyncio
async def test_fork_rejects_negative_seq(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, -1)
    assert ei.value.reason == "invalid_rewind_seq"


@pytest.mark.asyncio
async def test_fork_rejects_non_user_target(manager):
    parent = await _seed_parent(manager, backend="codex")
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 1)  # seq 1 is an assistant msg
    assert ei.value.reason == "target_not_user_message"


@pytest.mark.asyncio
async def test_fork_unknown_parent(manager):
    with pytest.raises(ForkError) as ei:
        await manager.fork_session("nope", 0)
    assert ei.value.reason == "parent_not_found"


@pytest.mark.asyncio
async def test_fork_backend_not_supported(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")

    class _NoFork:
        can_fork = False

    monkeypatch.setattr(owner_runtime, "get_harness", lambda b: _NoFork())
    with pytest.raises(BackendForkNotSupported):
        await manager.fork_session(parent.id, 0)
    # No half-created row.
    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["origin"] == "fork" for r in rows)


# ------------------------------------------------------------------ live-work refusal


@pytest.mark.asyncio
async def test_fork_refused_active_task(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    await manager.initialize(manager.db)
    started, release = asyncio.Event(), asyncio.Event()

    class HeldParent(FakeRunBase):
        async def _start(self, *_args, **_kwargs):
            started.set()

        async def _stream(self):
            await release.wait()
            yield HarnessEvent(type="result")

        async def interrupt(self):
            release.set()

        async def _stop_process(self):
            release.set()

    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda *_args, **_kwargs: HeldParent()))
    receipt = await manager.start_message(parent.id, "Keep this parent active")
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ForkError) as ei:
            await manager.fork_session(parent.id, 0)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        await manager.interrupt(parent.id)
        await asyncio.wait_for(receipt.wait(), 1)


@pytest.mark.asyncio
async def test_fork_refused_queued_message(manager):
    parent = await _seed_parent(manager, backend="codex")
    await manager.interrupt(parent.id)
    queued = await manager.start_message(parent.id, "x", source="bg")
    assert manager.dispatch_snapshot(parent.id)["items"][0]["request_id"] == queued.request_id
    with pytest.raises(ForkError) as ei:
        await manager.fork_session(parent.id, 0)
    assert ei.value.reason == "fork_blocked_parent_turn_active"


@pytest.mark.asyncio
async def test_fork_refused_active_delegation(manager):
    parent = await _seed_parent(manager, backend="codex")
    rec = DelegationRunState(
        delegation_id="child1", parent_session_id=parent.id,
        target_agent_id="a", target_agent_name="A", request="do",
        work_id="fork-blocking-work", _publication=_RoundPublication(WorkPublication("fork-blocking-work")),
    )
    delegation_manager._records["child1"] = rec
    try:
        with pytest.raises(ForkError) as ei:
            await manager.fork_session(parent.id, 0)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        delegation_manager._records.pop("child1", None)


@pytest.mark.asyncio
async def test_forking_flag_blocks_start_message(manager):
    parent = await _seed_parent(manager, backend="codex")
    parent._forking = True
    with pytest.raises(ValueError, match="busy"):
        await manager.start_message(parent.id, "hello")


# ------------------------------------------------------------------ replay validation


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_invalid_replay_prefix_leaves_parent_available_without_creating_child(manager, backend):
    parent = await _seed_parent(manager, backend=backend)
    # The selected user message is valid; the retained assistant prefix has
    # decodable JSON that does not satisfy the replay message contract.
    async with manager.db.transaction(write=True) as tx:
        await tx.execute("UPDATE messages SET runtime_metadata=? WHERE session_id=? AND seq=1",
                         (json.dumps({"request_id": "missing-attempt"}), parent.id))
    with pytest.raises(ForkError, match="attempt_id"):
        await manager.fork_session(parent.id, 2)
    rows = await manager.db.load_sessions(include_archived=True)
    assert [row["id"] for row in rows] == [parent.id]
    assert not rows[0]["archived"]
    assert manager.get_session(parent.id) is parent
    assert parent._forking is False
    assert not any(s.origin == "fork" for s in manager.sessions.values())


# ------------------------------------------------------------------ crash recovery


@pytest.mark.asyncio
async def test_recover_purges_initializing(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    # Manually plant an 'initializing' fork row (saga crashed before step 7).
    await manager.db.create_fork_session(
        fork_id="orphan01", name="o", working_dir="/repo",
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend="claude-code", agent_id=parent.agent_id, credential_id=None,
        resume_id="resume-xyz", fork_after_seq=1,
    )
    harness = get_harness("claude-code")
    seen = {}

    async def fake_cleanup(working_dir, resume_id_hint, fork_id, *, credential=None, artifact=None, hooks=None):
        seen["args"] = (working_dir, resume_id_hint, fork_id)

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)
    await manager._recover_incomplete_forks()
    assert seen["args"] == ("/repo", "resume-xyz", "orphan01")
    rows = await manager.db.load_sessions(include_archived=True)
    assert not any(r["id"] == "orphan01" for r in rows)


@pytest.mark.asyncio
async def test_startup_recovers_backend_default_fork_with_host_login(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    await manager.db.save_credential(
        credential_id="claude-login", backend="claude-code", label="Claude login", auth_type="api_key",
        secret_encrypted="unused", created_at="2026-11-17T00:00:00+00:00", user_id=parent.user_id,
    )
    await manager.db.update_agent(parent.agent_id, backend="claude-code", credential_id="claude-login")
    await manager.db.create_fork_session(
        fork_id="interrupted-host-fork", name="Interrupted", working_dir=parent.working_dir,
        created_at="2026-11-17T00:00:00+00:00", parent_id=parent.id,
        backend="codex", agent_id=parent.agent_id, user_id=parent.user_id,
        credential_id=None, defaults_source="backend", resume_id="interrupted-native-id", fork_after_seq=1,
    )
    observed = []

    async def cleanup(_path, _resume_id, fork_id, *, credential, artifact, hooks):
        observed.append((fork_id, credential))

    monkeypatch.setattr(get_harness("codex"), "cleanup_incomplete_fork_artifacts", cleanup)
    restarted = SessionManager()
    await restarted.initialize(manager.db)
    assert observed == [("interrupted-host-fork", None)]
    assert await manager.db.load_incomplete_forks() == []


@pytest.mark.asyncio
async def test_startup_retains_failed_fork_cleanup_and_cleans_its_neighbor(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="claude-code")
    db = manager.db
    fork_ids = ("orphan-a", "orphan-b")
    for fork_id in fork_ids:
        await db.create_fork_session(
            fork_id=fork_id, name="Interrupted fork", working_dir=parent.working_dir,
            created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
            backend=parent.backend, agent_id=parent.agent_id, credential_id=None,
            resume_id=fork_id, fork_after_seq=1, user_id=parent.user_id,
        )
    harness = get_harness("claude-code")
    before = {row["id"]: row for row in await db.load_incomplete_forks()}
    messages = {fork_id: await db.load_messages(fork_id) for fork_id in fork_ids}
    attempted = []

    async def cleanup(working_dir, resume_id_hint, fork_id, **kwargs):
        attempted.append(fork_id)
        if len(attempted) == 1:
            raise OSError("disk full")

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup)
    cold = SessionManager()
    await cold.initialize(db)
    failed, healthy = attempted
    assert await db.load_incomplete_forks() == [before[failed]]
    assert await db.load_messages(failed) == messages[failed]
    assert await db.get_session_binding(healthy) is None
    assert cold.get_session(failed, parent.user_id) is None

    await SessionManager().initialize(db)
    assert attempted == [failed, healthy, failed]
    assert await db.get_session_binding(failed) is None


@pytest.mark.parametrize("writer", ["covered", "standalone", "different", "current", "legacy"])
async def test_recover_reverting_requires_retirement_of_the_original_writer(manager, monkeypatch, writer):
    parent = await _seed_parent(manager, backend="codex")
    current = GitWriterScope("installation", "current")
    written_by = {"installation": current.installation, "invocation": "previous"}
    if writer == "different":
        written_by["installation"] = "another"
    if writer == "current":
        written_by["invocation"] = current.invocation
    metadata = {} if writer == "legacy" else {"revert_writer": written_by}
    await manager.db.create_fork_session(
        fork_id="rev01", name="o", working_dir=parent.working_dir,
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend="codex", agent_id=parent.agent_id, credential_id=None,
        resume_id=None, fork_after_seq=1, user_id=parent.user_id,
    )
    await manager.db.update_session_field("rev01", fork_status="reverting", fork_metadata=json.dumps(metadata))
    history = await manager.db.load_messages("rev01")
    assert len(history) == 2

    async def no_git(*args, **kwargs):
        pytest.fail("startup must not repeat or inspect the original Git operation")

    monkeypatch.setattr(fork_helpers, "_git", no_git)
    restarted = SessionManager()
    if writer == "standalone":
        await restarted.initialize(manager.db)
    else:
        await restarted.initialize(manager.db, previous_writers_retired=True, git_writer_scope=current)
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    row = rows["rev01"]
    assert restarted.get_session(parent.id) is not None
    assert await manager.db.load_messages("rev01") == history
    if writer != "covered":
        assert row["fork_status"] == "reverting"
        assert restarted.get_session("rev01") is None
        assert not await restarted.delete_session("rev01")
        with pytest.raises(ForkError) as refused:
            await restarted.fork_session(parent.id, 2)
        assert refused.value.reason == "git_retirement_pending"
        return
    assert restarted.get_session("rev01") is not None
    assert row["fork_status"] == "ready"
    rec = json.loads(row["fork_revert_record"])
    assert rec["status"] == "unknown_post_crash"
    note = json.loads(row["fork_metadata"])["first_turn_note"]
    assert "unknown" in note and "Inspect" in note
    assert "were NOT reverted" not in note and "WERE reverted" not in note


async def test_startup_propagates_failure_to_persist_revert_recovery(manager):
    parent = await _seed_parent(manager, backend="codex")
    db = manager.db
    await db.create_fork_session(
        fork_id="reverting-fork", name="Reverting fork", working_dir=parent.working_dir,
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id,
        backend=parent.backend, agent_id=parent.agent_id, credential_id=None,
        resume_id=None, fork_after_seq=1, user_id=parent.user_id,
    )
    await db.update_session_field("reverting-fork", fork_status="reverting", fork_metadata=json.dumps({
        "revert_writer": {"installation": "installation", "invocation": "previous"},
    }))
    before = await db.load_incomplete_forks()
    async with db.transaction(write=True) as tx:
        await tx.execute(
            "CREATE TRIGGER reject_revert_recovery BEFORE UPDATE OF fork_status ON sessions "
            "WHEN OLD.id = 'reverting-fork' AND NEW.fork_status = 'ready' "
            "BEGIN SELECT RAISE(ABORT, 'revert recovery write failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="revert recovery write failed"):
        await SessionManager().initialize(db, git_writer_scope=GitWriterScope("installation", "current"))

    assert await db.load_incomplete_forks() == before


async def test_pending_revert_survives_a_complete_backup_and_is_excluded_from_account_transfer(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    account_copy = tmp_path / "account.db"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = Database(str(source))
    await db.initialize()
    account, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    try:
        parent = await _seed_parent(manager, backend="codex", working_dir=str(worktree))
        await db.create_fork_session(
            fork_id="retained-fork", name="Retained", working_dir=parent.working_dir,
            created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id, backend=parent.backend,
            agent_id=parent.agent_id, credential_id=None, resume_id=None, fork_after_seq=1, user_id=parent.user_id,
        )
        await db.update_session_field("retained-fork", fork_status="reverting", fork_metadata=json.dumps({
            "revert_worktree": str(worktree),
            "rewind_worktree_id": fork_git.worktree_for(str(worktree)).identity,
            "revert_writer": {"installation": "installation", "invocation": "previous"},
        }))
        before = await db.load_incomplete_forks()
        history = await db.load_messages("retained-fork")
    finally:
        await db.close()

    with closing(sqlite3.connect(source)) as connection:
        connection.row_factory = sqlite3.Row
        with closing(sqlite3.connect(backup)) as backup_db:
            connection.backup(backup_db)
        snapshot_account(connection, account_copy, account["id"], key=master_key())
    with closing(sqlite3.connect(account_copy)) as exported:
        verify_account_archive_rows(exported, account["id"])
        assert exported.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert exported.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

    reopened = Database(str(backup))
    await reopened.initialize()
    moved = worktree.rename(tmp_path / "renamed-worktree")
    try:
        standalone = SessionManager()
        await standalone.initialize(reopened)
        assert await reopened.load_incomplete_forks() == before
        assert await reopened.load_messages("retained-fork") == history
        assert standalone.get_session(parent.id) is not None
        assert standalone.get_session("retained-fork") is None

        async def no_git(*args, **kwargs):
            pytest.fail("An anchor in a persistently fenced worktree must not start Git")

        monkeypatch.setattr(fork_helpers, "_git", no_git)
        assert await fork_helpers.capture_git_anchor(str(moved), db=reopened) == (None, None, None)
        relocated = await _seed_parent(standalone, backend="codex", working_dir=str(moved))
        with pytest.raises(ForkError) as blocked:
            await standalone.fork_session(relocated.id, 0)
        assert blocked.value.reason == "git_retirement_pending"
    finally:
        await reopened.close()


# ------------------------------------------------------------------ persistence


@pytest.mark.parametrize("stash_ref", ["stash@{0}", "a" * 40])
async def test_recovery_reference_survives_storage_and_public_projection(manager, stash_ref):
    parent = await _seed_parent(manager, backend="codex")
    record = {"ran": True, "files": ["a.py"], "stash_ref": stash_ref, "status": "completed"}
    await manager.db.update_session_field(parent.id, fork_revert_record=json.dumps(record))
    saved = next(row for row in await manager.db.load_sessions() if row["id"] == parent.id)
    info = sm.fork_info_fields(backend="codex", forked_from_session_id=None,
                              fork_after_seq=None, fork_metadata=None,
                              fork_revert_record=saved["fork_revert_record"])
    assert info["fork_revert_record"] == record


@pytest.mark.asyncio
async def test_fork_metadata_survives_restart(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2, label="My Fork")
    fork_id = fork.id
    # Simulate a restart: a fresh manager over the same DB.
    mgr2 = SessionManager()
    await mgr2.initialize(manager.db)
    reloaded = mgr2.get_session(fork_id)
    assert reloaded is not None
    meta = json.loads(reloaded.fork_metadata)
    assert meta["prefilled_prompt"] == "q1"
    assert meta["fork_label"] == "My Fork"
    # The public field surfaces the prefilled prompt.
    info = sm.fork_info_fields(
        backend=reloaded.backend,
        forked_from_session_id=reloaded.forked_from_session_id,
        fork_after_seq=reloaded.fork_after_seq,
        fork_metadata=reloaded.fork_metadata,
        fork_revert_record=reloaded.fork_revert_record,
    )
    assert info["fork_prefilled_prompt"] == "q1"


@pytest.mark.asyncio
async def test_unarchive_restores_fork_fields(manager):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    await manager.db.update_session_field(fork.id, archived=True)
    manager.sessions.pop(fork.id, None)
    restored = await manager.unarchive_session(fork.id)
    assert restored.forked_from_session_id == parent.id
    assert restored.fork_after_seq == 1
    assert restored.fork_metadata is not None


# ------------------------------------------------------------------ revert via saga


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


async def _rewind_repository(manager, tmp_path, name, *, subdirectory=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / name).write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()

    agent = await manager.db.get_system_agent()
    working_dir = repo / "subdirectory" if subdirectory else repo
    working_dir.mkdir(exist_ok=True)
    parent = await manager.create_session(agent["id"], "P", str(working_dir),
                                          backend="claude-code")
    # seq 0 user msg with clean git anchor at fork point; seq 1 the agent edits a.py.
    await manager.db.append_message(session_id=parent.id, seq=0, role="user",
                                    type="text", content="edit it",
                                    git_head=head, git_status_clean=True)
    await manager.db.append_message(session_id=parent.id, seq=1, role="assistant",
                                    type="tool_use", tool_name="Edit",
                                    tool_input={"file_path": str(repo / name)},
                                    tool_use_id="e1", runtime_metadata={
                                        "request_id": "r", "attempt_id": "a", "tool_call_id": "edit"})
    await manager.db.append_message(session_id=parent.id, seq=2, role="tool", type="tool_result",
                                    content="edited", tool_use_id="e1", is_error=False,
                                    runtime_metadata={"request_id": "r", "attempt_id": "a", "tool_call_id": "edit"})
    parent._message_count = 3
    # The agent's edit is now on disk (dirty tree).
    (repo / name).write_text("agent-edited\n")
    return parent, repo


@pytest.mark.parametrize("name", ["a.py", "中文 \n -> file.py"])
async def test_fork_with_revert_restores_files(manager, tmp_path, monkeypatch, name):
    monkeypatch.setenv("HOME", str(tmp_path))
    parent, repo = await _rewind_repository(manager, tmp_path, name)
    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    rec = json.loads(fork.fork_revert_record)
    assert rec["status"] == "completed"
    assert len(rec["stash_ref"]) == 40
    assert (repo / name).read_text() == "v1\n"   # restored to fork-point
    assert fork.fork_status == "ready"


async def test_legacy_revert_fences_the_outer_repository_after_a_nested_init(manager, tmp_path):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py", subdirectory=True)
    outside = await _seed_parent(manager, working_dir=str(repo))
    independent = tmp_path / "independent"
    _git(repo, "worktree", "add", "--detach", str(independent), "HEAD")
    other = await _seed_parent(manager, working_dir=str(independent))
    db = manager.db
    await db.create_fork_session(
        fork_id="legacy-revert", name="Legacy revert", working_dir=parent.working_dir,
        created_at="2026-06-08T00:00:00+00:00", parent_id=parent.id, backend=parent.backend,
        agent_id=parent.agent_id, credential_id=None, resume_id=None, fork_after_seq=0,
        user_id=parent.user_id,
    )
    await db.update_session_field("legacy-revert", fork_status="reverting")
    history = await db.load_messages("legacy-revert")
    _git(parent.working_dir, "init", "-q")

    restarted = SessionManager()
    await restarted.initialize(db)
    assert restarted.get_session("legacy-revert") is None
    with pytest.raises(ForkError) as blocked:
        await restarted.fork_session(outside.id, 0)
    assert blocked.value.reason == "git_retirement_pending"
    assert restarted.get_session(outside.id) is not None
    assert restarted.get_session(parent.id) is not None
    assert await db.load_messages("legacy-revert") == history
    assert (await restarted.fork_session(other.id, 0)).fork_status == "ready"


async def test_recover_partial_revert_without_claiming_stash_never_started(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    manager._git_writer_scope = GitWriterScope("installation", "previous")
    db = manager.db
    spawn, git = fork_git._spawn_git, fork_helpers._git
    update = db.update_session_field
    partial_saved, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stash_processes = []

    async def hold_stash_result(args, **kwargs):
        process = await spawn(args, **kwargs)
        if "stash" in args and "push" in args:
            stash_processes.append(process)
            await release.wait()
        return process

    async def shorten_stash_wait(cwd, *args, **kwargs):
        if args[:3] == ("--literal-pathspecs", "stash", "push"):
            kwargs["timeout"] = 0.12
        return await git(cwd, *args, **kwargs)

    async def note_partial_write(session_id, **fields):
        await update(session_id, **fields)
        if set(fields) == {"fork_revert_record"}:
            partial_saved.set()

    async def interrupt_before_terminal_write(*args, **kwargs):
        stopped.set()
        raise RuntimeError("stop before saving terminal revert result")

    monkeypatch.setattr(fork_git, "_spawn_git", hold_stash_result)
    monkeypatch.setattr(fork_helpers, "_git", shorten_stash_wait)
    monkeypatch.setattr(fork_git, "_RETIRE_TIMEOUT", 0.12)
    monkeypatch.setattr(db, "update_session_field", note_partial_write)
    monkeypatch.setattr(manager, "_install_reverted_fork", interrupt_before_terminal_write)
    try:
        with pytest.raises(ForkError) as rejected:
            await manager.fork_session(parent.id, 0, revert_files=True)
        assert rejected.value.reason == "git_retirement_pending"
        await asyncio.wait_for(partial_saved.wait(), 3)
        [pending] = await db.load_incomplete_forks()
        assert "ran" not in json.loads(pending["fork_revert_record"])
        [process] = stash_processes
        assert await asyncio.to_thread(process.wait, 3) == 0
    finally:
        release.set()
    await asyncio.wait_for(stopped.wait(), 3)

    restarted = SessionManager()
    await restarted.initialize(db, previous_writers_retired=True, git_writer_scope=GitWriterScope("installation", "next"))
    recovered = restarted.get_session(pending["id"])
    assert recovered is not None
    record = json.loads(recovered.fork_revert_record)
    assert record["ran"] and record["status"] == "unknown_post_crash"
    assert len(stash_processes) == 1
    assert (repo / "a.py").read_text() == "v1\n"


async def test_second_git_root_lookup_keeps_original_rewind_and_owns_late_spawn(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py", subdirectory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    neighbor = await _seed_parent(manager, working_dir=str(alias))
    missing = await _seed_parent(manager, working_dir=str(repo / "not-created"))
    independent = tmp_path / "independent"
    _git(repo, "worktree", "add", "--detach", str(independent), "HEAD")
    other = await _seed_parent(manager, working_dir=str(independent))

    release = asyncio.Event()
    delayed = asyncio.Event()
    original_spawn = fork_git._spawn_git
    original_git = fork_helpers._git
    lookup = ("rev-parse", "--show-toplevel")
    matching_lookups = 0
    commands = []
    original_process = None
    commit_busy = asyncio.Event()
    commit_allowed = asyncio.Event()
    hold_commit = False
    finish_revert = manager.db.finish_fork_revert

    async def temporarily_busy(*args, **kwargs):
        if hold_commit and not commit_allowed.is_set():
            commit_busy.set()
            raise DatabaseBusy("Only the original revert write is busy")
        return await finish_revert(*args, **kwargs)

    async def record_git(cwd, *args, **kwargs):
        nonlocal matching_lookups
        commands.append((cwd, args))
        if cwd == parent.working_dir and args == lookup:
            matching_lookups += 1
            if matching_lookups == 2:
                kwargs["timeout"] = 0.12
        return await original_git(cwd, *args, **kwargs)

    async def spawn_git(args, **kwargs):
        nonlocal original_process
        if args == lookup and matching_lookups == 2 and not delayed.is_set():
            # Git is already alive in the established cwd when its handle
            # is delayed. Only this original process can settle the fence.
            original_process = await original_spawn(("-c", "alias.wait=!sleep 10", "wait"), **kwargs)
            delayed.set()
            await release.wait()
            return original_process
        return await original_spawn(args, **kwargs)

    monkeypatch.setattr(fork_helpers, "_git", record_git)
    monkeypatch.setattr(fork_git, "_spawn_git", spawn_git)
    monkeypatch.setattr(fork_git, "_RETIRE_TIMEOUT", 0.12)
    monkeypatch.setattr(manager.db, "finish_fork_revert", temporarily_busy)
    attempt = asyncio.create_task(manager.fork_session(parent.id, 0, revert_files=True))
    try:
        await asyncio.wait_for(delayed.wait(), 4)
        with pytest.raises(ForkError) as competing:
            await manager.fork_session(neighbor.id, 0)
        assert competing.value.reason == "git_retirement_pending"
        with pytest.raises(ForkError) as failed:
            await attempt
        assert failed.value.reason == "git_retirement_pending"
        assert commands[-1] == (parent.working_dir, lookup)
        assert not any("stash" in args or "restore" in args for _, args in commands)
        assert (repo / "a.py").read_text() == "agent-edited\n"
        assert "Git execution" in manager.drain_blockers()

        [pending] = await manager.db.load_incomplete_forks()
        assert pending["fork_status"] == "reverting"
        assert manager.get_session(pending["id"]) is None
        preview = await manager.fork_preview(neighbor.id, 0)
        assert not preview["can_fork"] and not preview["revert"]["available"]
        for source in (parent, neighbor, missing):
            with pytest.raises(ForkError) as blocked:
                await manager.fork_session(source.id, 0)
            assert blocked.value.reason == "git_retirement_pending"
            assert manager.get_session(source.id) is source
        assert not (repo / "not-created").exists()
        inner_git = Path(parent.working_dir) / ".git"
        inner_git.mkdir()
        discovered = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=parent.working_dir,
            check=True, capture_output=True, text=True,
        )
        assert Path(discovered.stdout.strip()) == repo
        with pytest.raises(ForkError) as inner_blocked:
            await manager.fork_session(parent.id, 0)
        assert inner_blocked.value.reason == "git_retirement_pending"
        assert manager.get_session(parent.id) is parent
        inner_git.rmdir()
        detached_git = (repo / ".git").rename(tmp_path / "temporarily-hidden-git")
        try:
            for source in (parent, missing):
                with pytest.raises(ForkError) as still_blocked:
                    await manager.fork_session(source.id, 0)
                assert still_blocked.value.reason == "git_retirement_pending"
                assert manager.get_session(source.id) is source
            assert (await manager.fork_session(other.id, 0)).fork_status == "ready"
        finally:
            detached_git.rename(repo / ".git")
        assert original_process is not None and original_process.poll() is None
        relocated_path = repo.rename(tmp_path / "moved-repo")
        relocated = await _seed_parent(manager, working_dir=str(relocated_path))
        with pytest.raises(ForkError) as moved:
            await manager.fork_session(relocated.id, 0)
        assert moved.value.reason == "git_retirement_pending"
        assert manager.get_session(relocated.id) is relocated
        assert await fork_helpers.capture_git_anchor(str(relocated_path), db=manager.db) == (None, None, None)
        hold_commit = True
    finally:
        release.set()
        await asyncio.gather(attempt, return_exceptions=True)

    try:
        await asyncio.wait_for(commit_busy.wait(), 2)
        # The worktree is still fenced, but unrelated Agent admission can
        # proceed between retry attempts.
        await asyncio.wait_for(_seed_parent(manager, working_dir=str(independent)), 1)
    finally:
        commit_allowed.set()

    async with asyncio.timeout(4):
        while True:
            [retained] = await manager.db.load_incomplete_forks()
            if json.loads(retained["fork_metadata"]).get("revert_git_retired"):
                break
            await asyncio.sleep(0.01)
    assert manager.get_session(pending["id"]) is None
    record = json.loads(retained["fork_revert_record"])
    assert record["status"] == "unknown_post_crash" and not record["ran"]
    assert record["git_result"]["retired"] and record["git_result"]["timed_out"]
    assert manager.get_session(parent.id) is parent
    assert "Git execution" not in manager.drain_blockers()
    assert (await manager.fork_session(relocated.id, 0)).fork_status == "ready"


async def test_cancelled_preview_keeps_its_original_git_execution_in_drain(manager, tmp_path, monkeypatch):
    parent, _ = await _rewind_repository(manager, tmp_path, "a.py")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_spawn = fork_git._spawn_git

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(fork_git, "_spawn_git", delayed_spawn)
    monkeypatch.setattr(fork_git, "_RETIRE_TIMEOUT", 0.12)
    preview = asyncio.create_task(manager.fork_preview(parent.id, 0))
    competing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        competing = asyncio.create_task(manager.fork_session(parent.id, 0))
        done, _ = await asyncio.wait({competing}, timeout=0.05)
        assert not done
        preview.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(preview, 2)
        assert "Git execution" in manager.drain_blockers()
        assert await manager.db.load_incomplete_forks() == []
        with pytest.raises(ForkError) as blocked:
            await asyncio.wait_for(competing, 2)
        assert blocked.value.reason == "git_retirement_pending"
        assert manager.get_session(parent.id) is parent
    finally:
        release.set()
        await asyncio.gather(preview, return_exceptions=True)
        if competing is not None:
            await asyncio.gather(competing, return_exceptions=True)
        await fork_git.drain_git_executions()
    assert "Git execution" not in manager.drain_blockers()


async def test_rewind_checks_the_reserved_tree_again_before_ready_commit(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    finish = manager.db.finish_rewind_fork

    async def replace_before_commit(session_id, **kwargs):
        repo.rename(tmp_path / "old-tree")
        repo.mkdir()
        return await finish(session_id, **kwargs)

    monkeypatch.setattr(manager.db, "finish_rewind_fork", replace_before_commit)
    with pytest.raises(ForkError) as blocked:
        await manager.fork_session(parent.id, 0)
    assert blocked.value.reason == "fork_worktree_changed"
    assert manager.get_session(parent.id) is parent
    assert not await manager.db.load_incomplete_forks()


@pytest.mark.parametrize("restored", [False, True])
async def test_restart_omits_a_ready_rewind_whose_saved_directory_was_replaced(manager, tmp_path, restored):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    fork = await manager.fork_session(parent.id, 0)
    if restored:
        await manager.archive_session(fork.id, replace=False)
        fork = await manager.unarchive_session(fork.id, fork.user_id)
        assert "rewind_restore_pending" not in json.loads(fork.fork_metadata)
    original = repo.rename(tmp_path / "saved-tree")
    repo.mkdir()
    restarted = SessionManager()
    await restarted.initialize(manager.db)
    assert restarted.get_session(fork.id) is None
    assert not (await manager.db.get_session_binding(fork.id))["archived"]

    repo.rmdir()
    original.rename(repo)
    rebound = SessionManager()
    await rebound.initialize(manager.db)
    assert rebound.get_session(fork.id) is not None


@pytest.mark.parametrize("boundary", ["ready", "startup"])
async def test_rewind_directory_permission_failure_keeps_storage_available(manager, tmp_path, monkeypatch, boundary):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    unaffected = await _seed_parent(manager, working_dir=str(tmp_path))
    original_open = os.open
    deny = False

    def open_directory(path, flags, *args, **kwargs):
        if deny and os.fspath(path) == str(repo):
            raise PermissionError("test directory search permission")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_directory)
    if boundary == "ready":
        finish = manager.db.finish_rewind_fork

        async def write(session_id, **kwargs):
            nonlocal deny
            deny = True
            return await finish(session_id, **kwargs)

        monkeypatch.setattr(manager.db, "finish_rewind_fork", write)
        with pytest.raises(ForkError) as rejected:
            await manager.fork_session(parent.id, 0)
        assert rejected.value.reason == "fork_worktree_changed"
        assert manager.get_session(parent.id) is parent
    else:
        child = await manager.fork_session(parent.id, 0)
        deny = True
        db = manager.db
        manager = SessionManager()
        await manager.initialize(db)
        assert manager.get_session(child.id) is None
    manager.db.health.require_healthy()
    assert manager.get_session(unaffected.id) is not None
    assert len(await manager.db.load_messages(unaffected.id)) == 6


@pytest.mark.parametrize("boundary", ["first-input", "unarchive"])
async def test_prepared_rewind_refuses_a_replaced_directory_at_session_admission(manager, tmp_path, monkeypatch, boundary):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    child = await manager.fork_session(parent.id, 0)
    if boundary == "unarchive":
        await manager.archive_session(child.id, replace=False)
    repo.rename(tmp_path / "saved-original")
    repo.mkdir()

    def no_backend(*args, **kwargs):
        pytest.fail("The reserved rewind must not start a backend in a replacement directory")

    monkeypatch.setattr(manager, "_make_run", no_backend)
    if boundary == "unarchive":
        with pytest.raises(SessionAdmissionClosed, match="reserved Git worktree"):
            await manager.unarchive_session(child.id, child.user_id)
        assert manager.get_session(child.id) is None
        assert (await manager.db.get_session_binding(child.id))["archived"]
    else:
        receipt = await manager.start_message(child.id, "Continue the original history", wiki_mode=None)
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.outcome.status == "failed"
        assert completion.outcome.code == "fork_worktree_changed"


async def test_prepared_rewind_binding_survives_an_attachment_wait_and_blocks_a_successor(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    await manager.db.append_message(session_id=parent.id, seq=3, role="user", type="text", content="Continue the work")
    child = await manager.fork_session(parent.id, 3)
    validate = owner_runtime.validate_message_attachments

    def replace_after_check(*args, **kwargs):
        files = validate(*args, **kwargs)
        repo.rename(tmp_path / "working-tree")
        repo.mkdir()
        _git(repo, "init", "-q")
        return files

    async def unexpected_git(*args, **kwargs):
        pytest.fail("A replacement directory must not receive a Git anchor")

    monkeypatch.setattr(owner_runtime, "validate_message_attachments", replace_after_check)
    monkeypatch.setattr(fork_helpers, "_git", unexpected_git)
    receipt = await manager.start_message(child.id, "Continue from the original path", wiki_mode=None)
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.code == "fork_worktree_changed"
    with pytest.raises(ForkError) as blocked:
        await manager.fork_session(child.id, 0)
    assert blocked.value.reason == "fork_worktree_changed"
    with pytest.raises(ForkError) as blocked_preview:
        await manager.fork_preview(child.id, 0)
    assert blocked_preview.value.reason == "fork_worktree_changed"
    assert manager.get_session(child.id) is child


async def test_unarchive_retries_busy_compensation_after_a_post_commit_directory_change(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    child = await manager.fork_session(parent.id, 0)
    await manager.archive_session(child.id, replace=False)
    begin = manager.db.begin_bound_archive_restore
    settle = manager.db.settle_bound_archive_restore
    moved = tmp_path / "original-directory"
    compensation_attempts = 0
    swapped = False

    async def move_after_begin(session_id, *args, **kwargs):
        nonlocal swapped
        started = await begin(session_id, *args, **kwargs)
        if started and session_id == child.id and not swapped:
            repo.rename(moved)
            repo.mkdir()
            swapped = True
        return started

    async def settle_with_busy(session_id, marker, **kwargs):
        nonlocal compensation_attempts
        if session_id == child.id and kwargs["archived"]:
            compensation_attempts += 1
            if compensation_attempts == 1:
                raise DatabaseBusy("unarchive compensation write is busy")
        return await settle(session_id, marker, **kwargs)

    monkeypatch.setattr(manager.db, "begin_bound_archive_restore", move_after_begin)
    monkeypatch.setattr(manager.db, "settle_bound_archive_restore", settle_with_busy)
    with pytest.raises(SessionAdmissionClosed, match="reserved Git worktree"):
        await manager.unarchive_session(child.id, child.user_id)
    assert compensation_attempts == 2
    assert (await manager.db.get_session_binding(child.id))["archived"]
    assert manager.get_session(child.id) is None
    repo.rmdir()
    moved.rename(repo)
    assert (await manager.unarchive_session(child.id, child.user_id)).id == child.id


async def test_restart_recovers_unarchive_interrupted_before_compensation(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    child = await manager.fork_session(parent.id, 0)
    await manager.archive_session(child.id, replace=False)
    db = manager.db
    begin = db.begin_bound_archive_restore
    moved = tmp_path / "original-directory"

    async def interrupt_after_commit(session_id, *args, **kwargs):
        started = await begin(session_id, *args, **kwargs)
        if started and session_id == child.id:
            repo.rename(moved)
            repo.mkdir()
            raise RuntimeError("stop after the unarchive commit")
        return started

    with monkeypatch.context() as fault:
        fault.setattr(db, "begin_bound_archive_restore", interrupt_after_commit)
        with pytest.raises(RuntimeError, match="stop after"):
            await manager.unarchive_session(child.id, child.user_id)
    assert not (await db.get_session_binding(child.id))["archived"]
    restarted = SessionManager()
    await restarted.initialize(db)
    assert restarted.get_session(child.id) is None
    assert (await db.get_session_binding(child.id))["archived"]

    repo.rmdir()
    moved.rename(repo)
    assert (await restarted.unarchive_session(child.id, child.user_id)).id == child.id


@pytest.mark.parametrize("boundary", ["acknowledged", "restart", "busy"])
async def test_unarchive_refuses_directory_change_at_final_commit(manager, tmp_path, monkeypatch, boundary):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    await manager.db.append_message(session_id=parent.id, seq=3, role="user", type="text", content="Continue")
    child = await manager.fork_session(parent.id, 3)
    await manager.archive_session(child.id, replace=False)
    db = manager.db
    settle = db.settle_bound_archive_restore
    moved = tmp_path / "original-directory"
    events = []

    async def observe(event):
        events.append(event)

    async def replace_before_ack(*args, **kwargs):
        if boundary == "busy" and not kwargs["archived"] and not moved.exists():
            repo.rename(moved)
            repo.mkdir()
            raise DatabaseBusy("final transaction did not start")
        metadata = await settle(*args, **kwargs)
        if boundary != "busy" and metadata is not None and not kwargs["archived"]:
            repo.rename(moved)
            repo.mkdir()
            if boundary == "restart":
                raise RuntimeError("stop after confirming restore")
        return metadata

    manager.on_broadcast("restore-observer", observe, user_id=child.user_id)
    with monkeypatch.context() as fault:
        fault.setattr(db, "settle_bound_archive_restore", replace_before_ack)
        expected = RuntimeError if boundary == "restart" else SessionAdmissionClosed
        with pytest.raises(expected, match="stop after" if boundary == "restart" else "reserved Git worktree"):
            await manager.unarchive_session(child.id, child.user_id)
    if boundary == "restart":
        manager = SessionManager()
        await manager.initialize(db)
    assert manager.get_session(child.id) is None
    assert (await manager.db.get_session_binding(child.id))["archived"] is (boundary != "restart")
    archives = await manager.list_archived_sessions(child.user_id)
    assert any(item["id"] == child.id for item in archives)
    assert (await manager.load_archived_session_detail(child.id, child.user_id)).messages
    assert await manager.load_archived_session_detail(child.id, "foreign-account") is None
    with pytest.raises(SessionAdmissionClosed, match="reserved Git worktree"):
        await manager.unarchive_session(child.id, child.user_id)
    assert not any(event["type"] == "session_unarchived" for event in events)

    repo.rmdir()
    moved.rename(repo)
    assert (await manager.unarchive_session(child.id, child.user_id)).id == child.id
    assert not any(item["id"] == child.id for item in await manager.list_archived_sessions(child.user_id))


async def test_unavailable_rewind_confirmation_retains_pending_archived_deletion(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    child = await manager.fork_session(parent.id, 0)
    repo.rename(tmp_path / "original-directory")
    restarted = SessionManager()
    await restarted.initialize(manager.db)
    entered, release = asyncio.Event(), asyncio.Event()
    finish = restarted._finish_session_deletion

    async def hold_delete(*args, **kwargs):
        entered.set()
        await release.wait()
        return await finish(*args, **kwargs)

    monkeypatch.setattr(restarted, "_finish_session_deletion", hold_delete)
    waiter = asyncio.create_task(restarted.delete_session(child.id, archived_user_id=child.user_id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        presence = await restarted.session_presence(child.id, child.user_id)
        assert presence.archived is True and presence.deletion_pending
        foreign = await restarted.session_presence(child.id, "foreign-account")
        assert foreign.archived is None and not foreign.deletion_pending
        original = restarted._archived_deletions[child.id][1]
    finally:
        release.set()
    assert await original
    absence = await restarted.session_presence(child.id, child.user_id)
    assert absence.archived is None and not absence.deletion_pending


@pytest.mark.parametrize("failure", ["first-root-timeout", "second-root-lookup", "second-root-timeout", "stash"])
async def test_retired_git_failure_keeps_the_ready_fork_and_git_diagnostic(manager, tmp_path, monkeypatch, failure):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py", subdirectory=True)
    original_git = fork_helpers._git
    lookups = 0

    async def git(cwd, *args, **kwargs):
        nonlocal lookups
        if cwd == parent.working_dir and args == ("rev-parse", "--show-toplevel"):
            lookups += 1
            if lookups == 1 and failure == "first-root-timeout":
                return fork_git.GitResult(0, str(repo) + "\n", "", timed_out=True)
            if lookups == 2 and failure == "second-root-lookup":
                return await original_git(str(repo), "octopus-invalid-subcommand")
            if lookups == 2 and failure == "second-root-timeout":
                return fork_git.GitResult(0, str(repo) + "\n", "", timed_out=True)
        if args[:3] == ("--literal-pathspecs", "stash", "push"):
            assert failure == "stash"
            return await original_git(cwd, "octopus-invalid-subcommand")
        return await original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(fork_helpers, "_git", git)
    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    record = json.loads(fork.fork_revert_record)
    assert record["status"] == ("refused" if failure == "first-root-timeout" else "failed")
    assert record["ran"] is (failure == "stash")
    assert record["stash_ref"] is None
    observation = record["git_result"]
    assert observation["retired"]
    if failure.endswith("timeout"):
        if failure == "second-root-timeout":
            assert "timed out" in record["error"]
        assert observation["timed_out"] and observation["exit_code"] == 0
        assert observation["stdout"] == str(repo) + "\n"
    else:
        assert "octopus-invalid-subcommand" in record["error"]
        assert observation["exit_code"] == 1 and not observation["timed_out"]
        assert "octopus-invalid-subcommand" in observation["stderr"]
    assert (repo / "a.py").read_text() == "agent-edited\n"
    assert fork.fork_status == "ready"
    assert manager.get_session(parent.id) is None
    assert await manager.db.load_incomplete_forks() == []


async def test_revert_retries_only_the_unstarted_result_transaction(manager, tmp_path, monkeypatch):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    original = manager.db.finish_fork_revert
    writes = []

    async def finish(*args, **kwargs):
        writes.append(args)
        if len(writes) == 1:
            assert (repo / "a.py").read_text() == "v1\n"
            raise DatabaseBusy("transaction acquisition test")
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager.db, "finish_fork_revert", finish)
    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    assert len(writes) == 2 and writes[0] == writes[1]
    record = json.loads(fork.fork_revert_record)
    assert record["status"] == "completed"
    git = subprocess.run(["git", "stash", "list", "--format=%H"], cwd=repo, check=True, capture_output=True, text=True)
    assert git.stdout.splitlines() == [record["stash_ref"]]


@pytest.mark.parametrize("verification_fails", [False, True])
async def test_rewind_cancellation_settles_stash_snapshot(manager, tmp_path, monkeypatch, verification_fails):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    _git(repo, "rm", "-f", "a.py")
    exited, release = asyncio.Event(), asyncio.Event()
    commands = []
    spawn = fork_git._spawn_git

    async def hold_exited_store(args, **kwargs):
        commands.append(args)
        verifying = "rev-parse" in args and "--verify" in args
        if verification_fails and verifying:
            args = ("rev-parse", "--verify", "missing-object^{commit}")
        process = await spawn(args, **kwargs)
        if (verification_fails and verifying) or (not verification_fails and "stash" in args and "store" in args):
            exit_code = await asyncio.to_thread(process.wait, 3)
            assert (exit_code != 0) if verification_fails else (exit_code == 0)
            exited.set()
            await release.wait()
        return process

    monkeypatch.setattr(fork_git, "_spawn_git", hold_exited_store)
    request = asyncio.create_task(manager.fork_session(parent.id, 0, revert_files=True))
    try:
        await asyncio.wait_for(exited.wait(), 4)
        [pending] = await manager.db.load_incomplete_forks()
        [original] = [task for task in manager._owned_tasks if task.get_name().startswith("agent-operation-")]
        original.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)

    async with asyncio.timeout(3):
        while (candidate := manager.get_session(pending["id"])) is None:
            await asyncio.sleep(0.01)
    record = json.loads(candidate.fork_revert_record)
    assert (record["git_result"]["exit_code"] != 0) is verification_fails
    stored = subprocess.check_output(
        ["git", "rev-parse", "refs/stash"], cwd=repo, text=True,
    ).strip()
    assert record["stash_ref"] == (None if verification_fails else stored)
    assert not (repo / "a.py").exists()
    assert all("restore" not in args for args in commands)
    assert manager.get_session(parent.id) is parent


@pytest.mark.parametrize("phase", ["reverting", "ready"])
async def test_rewind_cancellation_cannot_separate_commit_from_original_handoff(manager, tmp_path, monkeypatch, phase):
    parent, repo = await _rewind_repository(manager, tmp_path, "a.py")
    committed = asyncio.Event()
    release = asyncio.Event()
    write = manager.db.update_session_field
    finish = manager.db.finish_fork_revert
    candidate_id = None

    async def record_commit(session_id):
        nonlocal candidate_id
        candidate_id = session_id
        committed.set()
        await release.wait()

    async def write_reverting(session_id, **fields):
        await write(session_id, **fields)
        if phase == "reverting" and fields.get("fork_status") == "reverting":
            await record_commit(session_id)

    async def write_ready(session_id, *args, **kwargs):
        archived = await finish(session_id, *args, **kwargs)
        if phase == "ready":
            await record_commit(session_id)
        return archived

    monkeypatch.setattr(manager.db, "update_session_field", write_reverting)
    monkeypatch.setattr(manager.db, "finish_fork_revert", write_ready)
    request = asyncio.create_task(manager.fork_session(parent.id, 0, revert_files=True))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        [original] = [task for task in manager._owned_tasks if task.get_name().startswith("agent-operation-")]
        original.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)

    async with asyncio.timeout(3):
        while (candidate := manager.get_session(candidate_id)) is None:
            await asyncio.sleep(0.01)
    record = json.loads(candidate.fork_revert_record)
    assert candidate.fork_status == "ready"
    if phase == "reverting":
        assert record["status"] == "unknown_post_crash" and not record["ran"]
        assert (repo / "a.py").read_text() == "agent-edited\n"
    else:
        assert record["status"] == "completed" and record["ran"]
        assert (repo / "a.py").read_text() == "v1\n"
    assert manager.get_session(parent.id) is parent


@pytest.mark.asyncio
async def test_fork_with_revert_refused_non_git(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    wd = tmp_path / "plain"
    wd.mkdir()
    agent = await manager.db.get_system_agent()
    parent = await manager.create_session(agent["id"], "P", str(wd),
                                          backend="claude-code")
    await manager.db.append_message(session_id=parent.id, seq=0, role="user",
                                    type="text", content="x")
    parent._message_count = 1
    fork = await manager.fork_session(parent.id, 0, revert_files=True)
    rec = json.loads(fork.fork_revert_record)
    assert rec["status"] == "refused"
    # Fork still created successfully.
    assert fork.fork_status == "ready"


# ------------------------------------------------------------------ replay dispatch


class _FakeRun(FakeRunBase):
    """Records the dispatch prompt and emits a clean session_started+result."""

    def __init__(self):
        super().__init__()
        self.started_prompt = None

    async def _start(self, prompt, working_dir, resume_id=None, credential=None):
        self.started_prompt = prompt

    async def _stream(self):
        yield HarnessEvent(type="session_started", session_id="newthread")
        yield HarnessEvent(type="result", session_id="newthread")

    async def _stop_process(self):
        pass

    async def interrupt(self):
        pass


@pytest.mark.asyncio
async def test_codex_first_turn_wraps_dispatch_only(manager, monkeypatch):
    parent = await _seed_parent(manager, backend="codex")
    fork = await manager.fork_session(parent.id, 2)
    assert fork.fork_needs_replay is True

    fake = _FakeRun()
    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda *a, **k: fake))

    receipt = await manager.start_message(fork.id, "edited prompt")
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.recording_state == "committed" and completion.outcome.status == "success"
    await manager.await_session_execution(fork.id)

    # Dispatch prompt was wrapped with the fork history…
    assert "<fork-history" in fake.started_prompt
    assert "edited prompt" in fake.started_prompt
    # …but the PERSISTED user message is the raw text (dispatch-only).
    msgs = await manager.db.load_messages(fork.id)
    user_rows = [m for m in msgs if m["role"] == "user" and m["seq"] == 2]
    assert user_rows and user_rows[0]["content"] == "edited prompt"
    # This generic run proves dispatch and raw persistence, not native storage.
    # Native consumption and the following unwrapped turn are covered through
    # actual protocol drivers in test_fork_replay_input.py.
    assert fork.fork_needs_replay is True
    assert json.loads(fork.fork_metadata)["prefilled_prompt"] == "q1"
    assert fork.claude_session_id == "newthread"


@pytest.mark.asyncio
async def test_a_fork_inherits_the_model_and_the_copied_times(manager):
    """A fork is the same conversation, branched.

    It already carried the parent's backend and credential; the per-session
    model belongs in that list for the same reason — a rewind of a conversation
    running on a stronger model should still be running on it. And the copied
    messages keep the times they actually happened at: the INSERT-SELECT names
    its columns, so a new one is silently dropped unless it is added, which
    would have left forked history as the one place with no hover timestamps.
    """
    from server.models import MessageContent, MessageRole

    agent = await manager.db.get_system_agent()
    parent = await manager.create_session(agent["id"], "Modelled Parent")
    parent.model = "opus"
    await manager.db.update_session_field(parent.id, model="opus")
    await manager._persist_message(
        parent, MessageContent(role=MessageRole.user, type="text", content="q1")
    )
    original = (await manager.db.load_messages(parent.id))[0]
    assert original["created_at"], "precondition: the parent's row has a time"

    fork_id = "forkmodel0001"
    await manager.db.create_fork_session(
        fork_id=fork_id,
        name="Forked",
        working_dir=parent.working_dir,
        created_at=original["created_at"],
        parent_id=parent.id,
        backend=parent.backend,
        model=parent.model,
        agent_id=parent.agent_id,
        credential_id=parent.credential_id,
        resume_id=None,
        fork_after_seq=0,
    )

    rows = {r["id"]: r for r in await manager.db.load_sessions()}
    assert rows[fork_id]["model"] == "opus"
    copied = await manager.db.load_messages(fork_id)
    assert [m["content"] for m in copied] == ["q1"]
    assert copied[0]["created_at"] == original["created_at"]

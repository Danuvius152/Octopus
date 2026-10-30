"""Tests for the cross-turn background task feature.

Covers:
  * `BgTaskManager` lifecycle — spawn/cancel/timeout/cap/persist
  * `mark_in_flight_bg_tasks_interrupted` on startup
  * `render_delivery_prompt` shape (the synthesized text we inject)
  * REST endpoints (`POST /bg-tasks`, list, get, cancel)
  * `SessionManager.deliver_bg_result` glue — verifies the synthesized
    prompt flows through `start_message` and broadcasts a user_message
  * `ClaudeCodeBackend.build_args` registers the bg MCP server

The MCP tool unit tests (bg_run/cancel/list) live inline below; they
invoke the FastMCP-wrapped function directly (no real stdio loop)
with httpx mocked so we don't need a live FastAPI to verify the tool's
request shape.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.bg_tasks import (
    MAX_STREAM_BYTES,
    BgTaskError,
    BgTaskManager,
    BgTaskRecord,
    bg_task_manager,
    render_delivery_prompt,
)
from server.database import Database
from server.main import app
from server.session_manager import SessionManager, session_manager
from tests.accounts import create_account

# ---------------------------------------------------------------------------
# BgTaskManager unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Per-test in-memory DB with bg_tasks schema applied."""
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def manager(db):
    """Fresh manager bound to a per-test DB, with capture-only callbacks."""
    mgr = BgTaskManager()
    delivered: list[BgTaskRecord] = []
    broadcasts: list[dict] = []

    async def deliver(rec: BgTaskRecord) -> None:
        delivered.append(rec)

    async def broadcast(msg: dict) -> None:
        broadcasts.append(msg)

    mgr.bind(db=db, deliver_cb=deliver, broadcast_cb=broadcast)
    await mgr.start()
    # Stash the lists on the manager for test access.
    mgr._delivered_ = delivered  # type: ignore[attr-defined]
    mgr._broadcasts_ = broadcasts  # type: ignore[attr-defined]
    yield mgr
    await mgr.shutdown()


async def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    """Poll predicate() until True or timeout. Fails the test on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {predicate}")
        await asyncio.sleep(interval)


async def _make_session_row(db, session_id: str, working_dir: str) -> None:
    """Insert a sessions row so the FK on bg_tasks is satisfied."""
    await db.save_session(
        session_id=session_id,
        name="t",
        working_dir=working_dir,
        created_at="2026-05-18T00:00:00Z",
    )


def _redirected_writer(tmp_path: Path, *, ignore_term: bool = True) -> tuple[str, Path, Path]:
    import shlex
    import sys

    ready, output = tmp_path / "writer.ready", tmp_path / "writes"
    child = (
        "import os,signal,time\nfrom pathlib import Path\n"
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if ignore_term else "")
        + f"Path({str(ready)!r}).write_text(str(os.getpgrp()))\n"
        + f"with open({str(output)!r},'ab',buffering=0) as f:\n"
        + " while True:\n  f.write(b'x'); time.sleep(.005)\n"
    )
    command = (
        shlex.join([sys.executable, "-c", child])
        + " >/dev/null 2>&1 & "
        + f"while [ ! -f {shlex.quote(str(ready))} ]; do sleep .01; done; exit 0"
    )
    return command, ready, output


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_normal_completion_retires_redirected_descendant_before_delivery(
    manager, db, tmp_path, monkeypatch, ignore_term,
):
    import os

    from server import bg_tasks
    from server.process_groups import group_has_live_members_after_kill

    await _make_session_row(db, "orphan-writer", str(tmp_path))
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    command, ready, output = _redirected_writer(tmp_path, ignore_term=ignore_term)
    try:
        record = await manager.start_task(
            session_id="orphan-writer", command=command, working_dir=str(tmp_path),
        )
        await _wait_for(lambda: bool(manager._delivered_))
        delivered = manager._delivered_[0]
        assert delivered.id == record.id
        assert delivered.status == "completed" and delivered.exit_code == 0
        assert delivered.stop_reason is None
        pgid = int(ready.read_text())
        assert not group_has_live_members_after_kill(pgid), "Completion returned with a live writer"
        size = output.stat().st_size
        await asyncio.sleep(0.04)
        assert output.stat().st_size == size
        await manager.cancel_task(record.id)
        await manager.shutdown()
        persisted = await manager.get_task(record.id)
        assert persisted.status == "completed" and persisted.stop_reason is None
    finally:
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_completed_command_retries_recording_after_retiring_resources(
    manager, db, tmp_path, monkeypatch,
):
    from server import bg_tasks
    from server.process_groups import group_has_live_members_after_kill

    await _make_session_row(db, "record-failure", str(tmp_path))
    real_update = db.update_bg_task
    recording_failed = asyncio.Event()

    async def fail_recording(task_id, **fields):
        recording_failed.set()
        raise RuntimeError("record unavailable")

    monkeypatch.setattr(db, "update_bg_task", fail_recording)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    command, ready, _ = _redirected_writer(tmp_path)
    record = await manager.start_task(
        session_id="record-failure", command="echo x >> executions; " + command,
        working_dir=str(tmp_path),
    )
    await asyncio.wait_for(recording_failed.wait(), 2)
    pgid = int(ready.read_text())
    assert not group_has_live_members_after_kill(pgid)
    assert not manager._delivered_
    monkeypatch.setattr(db, "update_bg_task", real_update)
    await manager.shutdown()
    result = await manager.get_task(record.id)
    assert (result.status, result.stop_reason) == ("completed", None)
    assert (tmp_path / "executions").read_text() == "x\n"
    assert [item.id for item in manager._delivered_] == [record.id]


async def test_delivery_retry_preserves_original_completed_command(db, tmp_path, monkeypatch):
    from server import bg_tasks
    from server.sessions.runtime_types import SessionMailboxFull

    await _make_session_row(db, "delivery-retry", str(tmp_path))
    manager = BgTaskManager()
    failed = asyncio.Event()
    succeeded = asyncio.Event()
    attempts, broadcasts = [], []
    allow_delivery = False

    async def deliver(record):
        attempts.append(record.id)
        if not allow_delivery:
            failed.set()
            raise SessionMailboxFull("delivery unavailable before acceptance")
        succeeded.set()

    async def broadcast(event):
        broadcasts.append(event)

    manager.bind(db, deliver, broadcast)
    await manager.start()
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.03)
    record = await manager.start_task(
        session_id="delivery-retry", command="echo x >> executions", working_dir=str(tmp_path),
    )
    try:
        await asyncio.wait_for(failed.wait(), 2)
        result = await manager.get_task(record.id)
        assert result.status == "completed" and result.stop_reason is None
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        allow_delivery = True
        await asyncio.wait_for(succeeded.wait(), 2)
        await manager.shutdown()
        assert len(attempts) >= 2 and set(attempts) == {record.id}
        assert len([event for event in broadcasts if event["type"] == "bg_completed"]) == 1
        assert (tmp_path / "executions").read_text() == "x\n"
    finally:
        allow_delivery = True
        await manager.shutdown()


@pytest.mark.parametrize("fault", ["term_denied", "kill_denied", "exit_unconfirmed"])
async def test_retirement_failure_keeps_original_result_for_later_shutdown(
    manager, db, tmp_path, monkeypatch, fault,
):
    import os

    from server import bg_tasks
    from server.process_groups import group_has_live_members_after_kill

    await _make_session_row(db, "retirement-retry", str(tmp_path))
    command, ready, output = _redirected_writer(tmp_path)
    failed = asyncio.Event()
    real_killpg = os.killpg
    real_confirm = bg_tasks._group_has_live_members_after_kill
    inject_failure = True
    loop = asyncio.get_running_loop()

    def killpg(pgid, sig):
        if inject_failure and ((fault == "term_denied" and sig == signal.SIGTERM)
                               or (fault == "kill_denied" and sig == signal.SIGKILL)):
            failed.set()
            raise PermissionError("retirement signal denied")
        real_killpg(pgid, sig)

    def confirm(pgid):
        if inject_failure and fault == "exit_unconfirmed":
            loop.call_soon_threadsafe(failed.set)
            raise TimeoutError("retirement unconfirmed")
        return real_confirm(pgid)

    monkeypatch.setattr(bg_tasks.os, "killpg", killpg)
    monkeypatch.setattr(bg_tasks, "_group_has_live_members_after_kill", confirm)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", 0.03)
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.03)
    try:
        record = await manager.start_task(
            session_id="retirement-retry", command=command, working_dir=str(tmp_path),
        )
        await asyncio.wait_for(failed.wait(), 2)
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        assert not manager._delivered_
        assert (await manager.get_task(record.id)).status == "running"
        if fault != "exit_unconfirmed":
            size = output.stat().st_size
            await _wait_for(lambda: output.stat().st_size > size)
        inject_failure = False
        await manager.shutdown()
        assert not group_has_live_members_after_kill(int(ready.read_text()))
        result = await manager.get_task(record.id)
        assert (result.status, result.exit_code, result.stop_reason) == ("completed", 0, None)
        assert [item.id for item in manager._delivered_] == [record.id]
    finally:
        inject_failure = False
        await manager.shutdown()
        if ready.exists():
            try:
                real_killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("action", ["cancel_task", "cancel_work", "shutdown"])
async def test_cancel_during_normal_retirement_joins_cleanup_without_changing_success(
    manager, db, tmp_path, monkeypatch, action,
):
    import os
    from dataclasses import replace
    from types import SimpleNamespace

    from server import bg_tasks
    from server.knowledge import capture, derive

    await _make_session_row(db, "completion-cancel", str(tmp_path))
    command, _, _ = _redirected_writer(tmp_path)
    owned = derive(capture(SimpleNamespace(id="completion-cancel", user_id=None, agent_id=None), None,
                           prompt=command), command)
    work_id = f"bg-completion-cancel-{action}"
    owned = replace(owned, return_binding=replace(owned.return_binding, work_id=work_id))
    term_started = asyncio.Event()
    signals = []
    real_killpg = os.killpg

    def killpg(pgid, sig):
        real_killpg(pgid, sig)
        if sig != 0:
            signals.append(sig)
        if sig == signal.SIGTERM:
            term_started.set()

    monkeypatch.setattr(bg_tasks.os, "killpg", killpg)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.1)
    record = await manager.start_task(
        session_id="completion-cancel", command=command, working_dir=str(tmp_path), owned_context=owned,
    )
    await asyncio.wait_for(term_started.wait(), 2)
    if action == "cancel_task":
        assert await manager.cancel_task(record.id)
        await _wait_for(lambda: bool(manager._delivered_))
    elif action == "cancel_work":
        await manager.cancel_work(work_id)
    else:
        await manager.shutdown()
    result = await manager.get_task(record.id)
    assert (result.status, result.exit_code, result.stop_reason) == ("completed", 0, None)
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert len(manager._delivered_) == 1


async def test_shutdown_drains_group_kill_created_while_stopping(manager, db, tmp_path, monkeypatch):
    import shlex
    import sys

    from server import bg_tasks

    await _make_session_row(db, "shutdown", str(tmp_path))
    signals = []
    real_killpg = bg_tasks.os.killpg

    def signal_group(pgid, sig):
        signals.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(bg_tasks.os, "killpg", signal_group)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03, raising=False)
    ready = tmp_path / "resists-term.ready"
    command = ("import signal,time; from pathlib import Path; "
               "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
               f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)")
    record = await manager.start_task(
        session_id="shutdown", command="exec " + shlex.join([sys.executable, "-c", command]),
        working_dir=str(tmp_path),
    )
    await _wait_for(ready.exists)
    await manager.shutdown()
    assert signal.SIGKILL in signals, "shutdown returned with an owned group-kill still pending"
    assert (await manager.get_task(record.id)).status == "interrupted"
    with pytest.raises(BgTaskError, match="shutting down"):
        await manager.start_task(session_id="shutdown", command="true", working_dir=str(tmp_path))


async def test_shutdown_owns_cancelled_startup_until_its_process_handle_arrives(manager, db, tmp_path, monkeypatch):
    from server import bg_tasks

    await _make_session_row(db, "spawn-race", str(tmp_path))
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release_handle.wait()
        return proc

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    startup = asyncio.create_task(manager.start_task(
        session_id="spawn-race", command="sleep 60", working_dir=str(tmp_path),
    ))
    cleanup = None
    try:
        await spawned.wait()
        manager.begin_shutdown()
        cleanup = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert not cleanup.done()
        release_handle.set()
        await cleanup
        assert (await asyncio.gather(startup, return_exceptions=True))[0].__class__ is asyncio.CancelledError
        assert processes[0].returncode is not None
        retained = await manager.list_tasks("spawn-race")
        assert len(retained) == 1
        assert retained[0].status == "interrupted"
        assert retained[0].stop_reason == "server_shutdown"
        assert retained[0].knowledge_context is not None
    finally:
        release_handle.set()
        await asyncio.gather(startup, *(task for task in [cleanup] if task is not None), return_exceptions=True)


@pytest.mark.parametrize("waiter_cancelled", [False, True])
async def test_closed_work_joins_bg_startup_and_original_process(manager, db, tmp_path, monkeypatch, waiter_cancelled):
    from dataclasses import replace
    from types import SimpleNamespace

    from server import bg_tasks
    from server.knowledge import capture, derive
    from server.mcp_identity import close_work

    await _make_session_row(db, "work-spawn", str(tmp_path))
    owned = derive(capture(SimpleNamespace(id="work-spawn", user_id=None, agent_id=None), None, prompt="sleep 60"), "sleep 60")
    work_id = f"wiki-bg-startup-{waiter_cancelled}"
    owned = replace(owned, return_binding=replace(owned.return_binding, work_id=work_id))
    real_spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    startup = asyncio.create_task(manager.start_task(session_id="work-spawn", command="sleep 60",
                                                     working_dir=str(tmp_path), owned_context=owned))
    stopping = None
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        close_work(work_id)
        if waiter_cancelled:
            startup.cancel()
        stopping = asyncio.create_task(manager.cancel_work(work_id))
        await asyncio.sleep(0)
        assert not stopping.done(), "An outstanding original process handle is not retired"
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert processes[0].returncode is not None
        final = (await manager.list_tasks("work-spawn"))[0]
        assert final.status in {"cancelled", "interrupted"}
        assert final.stop_reason == "work_cancelled"
    finally:
        release.set()
        await asyncio.gather(startup, *([stopping] if stopping else []), return_exceptions=True)


async def test_closed_work_drains_running_process_after_startup_cleanup_fails(db, tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from server import bg_tasks
    from server.knowledge import capture, derive
    from server.mcp_identity import close_work

    await _make_session_row(db, "work-cleanup-failure", str(tmp_path))
    owned = derive(capture(SimpleNamespace(id="work-cleanup-failure", user_id=None, agent_id=None), None,
                           prompt="sleep 60"), "sleep 60")
    work_id = "wiki-bg-cleanup-failure"
    owned = replace(owned, return_binding=replace(owned.return_binding, work_id=work_id))
    delivering, release_delivery, compensation_failed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def deliver(record):
        delivering.set()
        await release_delivery.wait()

    async def broadcast(message):
        pass

    manager = BgTaskManager()
    manager.bind(db=db, deliver_cb=deliver, broadcast_cb=broadcast)
    await manager.start()
    real_spawn, real_update = asyncio.create_subprocess_exec, db.update_bg_task
    spawned, release_handle = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_second_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        if len(processes) == 2:
            spawned.set()
            await release_handle.wait()
        return process

    async def fail_startup_recording(task_id, **kwargs):
        if kwargs.get("status") == "interrupted":
            compensation_failed.set()
            raise RuntimeError("startup compensation recording failed")
        return await real_update(task_id, **kwargs)

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", delayed_second_spawn)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    await manager.start_task(session_id="work-cleanup-failure", command="sleep 60",
                             working_dir=str(tmp_path), owned_context=owned)
    startup = asyncio.create_task(manager.start_task(session_id="work-cleanup-failure", command="sleep 60",
                                                     working_dir=str(tmp_path), owned_context=owned))
    stopping = delivery_waiter = None
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        monkeypatch.setattr(db, "update_bg_task", fail_startup_recording)
        close_work(work_id)
        stopping = asyncio.create_task(manager.cancel_work(work_id))
        delivery_waiter = asyncio.create_task(delivering.wait())
        release_handle.set()
        await asyncio.wait({stopping, delivery_waiter}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert delivering.is_set(), "A failed startup cleanup must still retire the captured running process"
        await asyncio.wait_for(asyncio.gather(*(process.wait() for process in processes)), 3)
        await asyncio.wait_for(compensation_failed.wait(), 3)
        assert all(process.returncode is not None for process in processes)
        await asyncio.wait({stopping}, timeout=0.05)
        assert not stopping.done(), "Report cleanup failure only after the other original task is joined"
        release_delivery.set()
        with pytest.raises(RuntimeError, match="startup compensation recording failed"):
            await asyncio.wait_for(stopping, 3)
    finally:
        release_handle.set()
        release_delivery.set()
        monkeypatch.setattr(db, "update_bg_task", real_update)
        if delivery_waiter is not None:
            delivery_waiter.cancel()
        await asyncio.gather(startup, *[task for task in (stopping, delivery_waiter) if task is not None],
                             return_exceptions=True)
        # The failed durable acknowledgement keeps its original cleanup owner;
        # all OS resources must nevertheless have been reaped before returning.
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        assert all(process.returncode is not None for process in processes)


async def test_shutdown_retains_background_process_until_exit_is_confirmed(manager, db, tmp_path, monkeypatch):
    from server import bg_tasks

    class UnconfirmedProcess:
        pid = 2**30
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

    proc = UnconfirmedProcess()

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(bg_tasks.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.03)
    await _make_session_row(db, "unconfirmed", str(tmp_path))
    record = await manager.start_task(
        session_id="unconfirmed", command="synthetic task", working_dir=str(tmp_path),
    )
    try:
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await asyncio.wait_for(manager.shutdown(), 0.3)
        assert (await manager.get_task(record.id)).status == "running"
        proc.returncode = -9
        proc.stdout.feed_eof()
        proc.stderr.feed_eof()
        proc.exited.set()
        await manager.shutdown()
        assert (await manager.get_task(record.id)).status == "interrupted"
    finally:
        proc.returncode = -9
        proc.stdout.feed_eof()
        proc.stderr.feed_eof()
        proc.exited.set()


@pytest.mark.parametrize("blocked_callback", ["broadcast", "delivery"])
async def test_shutdown_retains_completed_task_until_its_callback_finishes(db, tmp_path, monkeypatch, blocked_callback):
    from server import bg_tasks

    await _make_session_row(db, "callback", str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    recorded = asyncio.Event()
    mgr = BgTaskManager()

    async def complete_callback(task_id):
        entered.set()
        await release.wait()
        await db.update_bg_task(task_id, stdout="recorded after callback release")
        recorded.set()

    async def broadcast(event):
        if blocked_callback == "broadcast" and event["type"] == "bg_completed":
            await complete_callback(event["task_id"])

    async def deliver(record):
        if blocked_callback == "delivery":
            await complete_callback(record.id)

    mgr.bind(db, deliver, broadcast)
    await mgr.start()
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.03)
    record = await mgr.start_task(
        session_id="callback", command="printf finished", working_dir=str(tmp_path),
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        completed = await mgr.get_task(record.id)
        assert completed.status == "completed" and completed.exit_code == 0
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await asyncio.wait_for(mgr.shutdown(), 0.3)
        assert not recorded.is_set()
        release.set()
        await asyncio.wait_for(recorded.wait(), 1.0)
        await mgr.shutdown()
        completed = await mgr.get_task(record.id)
        assert completed.status == "completed" and completed.stop_reason is None
        assert completed.stdout == "recorded after callback release"
    finally:
        release.set()
        await asyncio.wait_for(mgr.shutdown(), 1.0)


async def test_start_task_returns_immediately_and_completes(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="echo hello world",
        working_dir=str(tmp_path),
        description="say hi",
    )
    assert rec.id
    assert rec.status == "running"
    assert (rec.idle_timeout_seconds, rec.timeout_seconds) == (60, 1800)

    await _wait_for(lambda: rec.id in [d.id for d in manager._delivered_])
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "completed"
    assert delivered.exit_code == 0
    assert "hello world" in delivered.stdout
    # DB row mirrors the in-memory record.
    row = await db.get_bg_task(rec.id)
    assert row["status"] == "completed"
    assert "hello world" in row["stdout"]


async def test_failing_command_records_failure(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="exit 7",
        working_dir=str(tmp_path),
    )
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "failed"
    assert delivered.exit_code == 7


@pytest.mark.parametrize("idle, expected_status", [(0, "completed"), (1, "failed")])
async def test_task_selected_idle_limit_controls_a_normal_quiet_phase(
    manager, db, tmp_path, idle, expected_status
):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 1.6; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=idle, timeout_seconds=5,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert result.status == expected_status
    assert result.idle_timeout_seconds == idle
    if idle:
        assert result.stop_reason == "idle_timeout"
        assert "finished" not in result.stdout
    else:
        assert result.stop_reason is None
        assert "finished" in result.stdout


async def test_total_timeout_still_stops_a_task_with_idle_disabled(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 10; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=0, timeout_seconds=1,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert result.status == "failed"
    assert result.stop_reason == "total_timeout"
    assert "started" in result.stdout and "finished" not in result.stdout
    assert "total_timeout" in render_delivery_prompt(result)


@pytest.mark.parametrize("notification_delay", [0.6, 1.5])
async def test_slow_started_notification_does_not_extend_total_runtime(
    manager, db, tmp_path, notification_delay
):
    await _make_session_row(db, "s1", str(tmp_path))
    delivered = []

    async def capture(record):
        delivered.append(record)

    async def slow_notification(event):
        if event["type"] == "bg_started":
            await asyncio.sleep(notification_delay)

    manager.bind(db=db, deliver_cb=capture, broadcast_cb=slow_notification)
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 1.2; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=0, timeout_seconds=1,
    )
    await _wait_for(lambda: bool(delivered))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("failed", "total_timeout")
    assert "finished" not in result.stdout


@pytest.mark.parametrize("cause, expected_status", [
    ("user_cancel", "cancelled"), ("server_shutdown", "interrupted"),
    ("external_signal", "interrupted"),
])
async def test_task_reports_the_actual_interruption_source(
    manager, db, tmp_path, cause, expected_status
):
    import os
    import signal

    await _make_session_row(db, "s1", str(tmp_path))
    pid_file = tmp_path / "process.pid"
    rec = await manager.start_task(
        session_id="s1", command="echo $$ > process.pid; exec sleep 10",
        working_dir=str(tmp_path),
    )
    await _wait_for(pid_file.exists)
    if cause == "user_cancel":
        assert await manager.cancel_task(rec.id)
    elif cause == "server_shutdown":
        await manager.shutdown()
    else:
        os.killpg(int(pid_file.read_text()), signal.SIGTERM)
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == (expected_status, cause)
    assert cause in render_delivery_prompt(result)


async def test_cancel_reaps_a_descendant_that_ignores_sigterm(manager, db, tmp_path, monkeypatch):
    import os
    import shlex
    import signal
    import sys

    # Exercise real group escalation; this test does not measure the grace time.
    monkeypatch.setattr("server.bg_tasks._TERM_GRACE_SECONDS", 0.03)
    await _make_session_row(db, "s1", str(tmp_path))
    child_code = (
        "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open('child.pid','w').write(str(os.getpid())); time.sleep(30)"
    )
    parent_code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "time.sleep(30)"
    )
    rec = await manager.start_task(
        session_id="s1", command=f"exec {shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}",
        working_dir=str(tmp_path),
    )
    child_file = tmp_path / "child.pid"
    await _wait_for(child_file.exists)
    child_pid = int(child_file.read_text())

    def child_finished():
        try:
            return (Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z")
        except FileNotFoundError:
            return True

    from pathlib import Path

    try:
        assert await manager.cancel_task(rec.id)
        await _wait_for(child_finished, timeout=7)
        await _wait_for(lambda: bool(manager._delivered_))
        assert (await manager.get_task(rec.id)).stop_reason == "user_cancel"
    finally:
        if not child_finished():
            os.kill(child_pid, signal.SIGKILL)


async def test_an_unsent_signal_does_not_claim_a_user_cancellation(
    manager, db, tmp_path, monkeypatch
):
    import os

    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="touch ready; sleep 0.15; echo finished",
        working_dir=str(tmp_path),
    )
    await _wait_for((tmp_path / "ready").exists)

    real_killpg = os.killpg

    def denied(pgid, sig):
        # Deny a real signal to a live group; absent groups still report ESRCH.
        real_killpg(pgid, 0)
        raise PermissionError("test signal denied")

    monkeypatch.setattr(os, "killpg", denied)
    assert not await manager.cancel_task(rec.id)
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("completed", None)


async def test_stderr_activity_keeps_a_stdout_quiet_task_running(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="echo started; for n in 1 2 3 4; do echo tick >&2; sleep 0.4; done; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=1, timeout_seconds=5,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("completed", None)
    assert "finished" in result.stdout and result.stderr.count("tick") == 4


async def test_idle_watchdog_terminates_proc_that_goes_silent(
    manager, db, tmp_path, monkeypatch
):
    """A command that produces output and then goes silent while
    still alive (the pytest-atexit hang we hit in the field) must
    be force-terminated by the idle watchdog rather than camp on
    the chip until the 30-min wall-clock timeout fires.

    Tightens IDLE_AFTER_OUTPUT_TIMEOUT_SECS so the test takes
    seconds instead of a minute.
    """
    import sys

    from server import bg_tasks as bgm

    monkeypatch.setattr(bgm, "IDLE_CHECK_INTERVAL_SECS", 1)

    await _make_session_row(db, "s1", str(tmp_path))
    # Print one line, then sleep — simulates the atexit-hang shape
    # (visible work done, process won't return).
    cmd = (
        f"{sys.executable} -c "
        "\"import sys, time; print('hello', flush=True); time.sleep(30)\""
    )
    start = asyncio.get_running_loop().time()
    rec = await manager.start_task(
        session_id="s1",
        command=cmd,
        working_dir=str(tmp_path),
        idle_timeout_seconds=2,
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=20.0,
    )
    elapsed = asyncio.get_running_loop().time() - start
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "failed", (
        f"idle watchdog should report a timeout failure, got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code is not None and delivered.exit_code < 0
    # Should kill well before the 30s sleep completes.
    assert elapsed < 10.0, f"watchdog took too long: {elapsed:.1f}s"
    # The output we printed before going silent must be preserved.
    assert "hello" in delivered.stdout


async def test_idle_watchdog_does_not_fire_on_quiet_short_command(
    manager, db, tmp_path, monkeypatch
):
    """A command that never produces output (e.g. `sleep N`) must
    NOT be considered idle — the idle clock only starts after the
    first byte. Otherwise the watchdog would kill legitimate quiet
    tasks unfairly."""
    from server import bg_tasks as bgm

    monkeypatch.setattr(bgm, "IDLE_CHECK_INTERVAL_SECS", 1)

    await _make_session_row(db, "s1", str(tmp_path))
    # 3-second silent sleep — would be killed at ~1s if the watchdog
    # incorrectly treated "no output yet" as idle.
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 3",
        working_dir=str(tmp_path),
        idle_timeout_seconds=1,
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=10.0,
    )
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "completed", (
        f"quiet command should complete naturally, got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code == 0


async def test_external_sigterm_yields_interrupted_status(manager, db, tmp_path):
    """Externally SIGTERMing the bg process group (i.e. not via
    cancel_task / shutdown / timeout — simulating uvicorn --reload,
    systemd KillMode=control-group, or an outside `pkill`) must be
    surfaced as `interrupted`, NOT `failed`. The latter mis-labels
    "something killed us" as "the command itself failed", which is
    what we hit on pytest runs that completed successfully and then
    got SIGTERMed.
    """
    import os
    import signal as _signal

    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 30",
        working_dir=str(tmp_path),
    )
    # Wait until the manager actually has the proc tracked.
    await _wait_for(lambda: rec.id in manager._running)
    rt = manager._running[rec.id]
    pgid = os.getpgid(rt.proc.pid)
    # External signal — does NOT touch cancel_task / shutdown /
    # timeout. _run_task's flags stay False, so the new branch is the
    # only thing that can label this correctly.
    os.killpg(pgid, _signal.SIGTERM)

    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "interrupted", (
        f"externally-signaled task should be 'interrupted', got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code is not None and delivered.exit_code < 0


async def test_cancel_running_task(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 30",
        working_dir=str(tmp_path),
    )
    # Give the proc a tick to be tracked.
    await asyncio.sleep(0.05)
    cancelled = await manager.cancel_task(rec.id)
    assert cancelled is True
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "cancelled"


async def test_cancel_unknown_task_returns_false(manager):
    assert await manager.cancel_task("no-such-id") is False


async def test_output_truncation(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    # Generate >MAX_STREAM_BYTES (200 KB) of stdout. `yes` repeats
    # its arg + newline forever; `head -c` clips to the byte target.
    # Both are in coreutils on every Linux distro. (Avoid `python`
    # — this box has only `python3` on PATH — and avoid commands
    # that filter bytes since /dev/urandom + tr produces far less
    # than the raw input length.)
    target = MAX_STREAM_BYTES + 50_000
    rec = await manager.start_task(
        session_id="s1",
        command=f"yes 0123456789 | head -c {target}",
        working_dir=str(tmp_path),
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=10.0,
    )
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.truncated is True, (
        f"expected truncated=True; got {delivered!r}"
    )
    # The marker prefix tells the model output was clipped.
    assert "truncated" in delivered.stdout
    assert len(delivered.stdout.encode("utf-8")) <= MAX_STREAM_BYTES


async def test_invalid_working_dir_raises(manager):
    with pytest.raises(BgTaskError):
        await manager.start_task(
            session_id="s1",
            command="echo hi",
            working_dir="/does/not/exist/anywhere",
        )


async def test_list_tasks_returns_most_recent_first(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    a = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == a.id for d in manager._delivered_))
    # Small sleep so started_at differs deterministically.
    await asyncio.sleep(0.01)
    b = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == b.id for d in manager._delivered_))
    rows = await manager.list_tasks("s1")
    assert [r.id for r in rows[:2]] == [b.id, a.id]


async def test_broadcasts_started_and_completed(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    types = [b["type"] for b in manager._broadcasts_]
    assert "bg_started" in types
    assert "bg_completed" in types
    started = next(b for b in manager._broadcasts_ if b["type"] == "bg_started")
    assert started["session_id"] == "s1"
    assert started["task_id"] == rec.id


async def test_in_flight_marked_interrupted_on_startup(db):
    """Simulate a row left in 'running' by a prior process; new
    manager.start() should flip it to 'interrupted' so the chip stops
    spinning forever."""
    await _make_session_row(db, "s1", "/tmp")
    await db.create_bg_task(
        task_id="orphan-1",
        session_id="s1",
        command="ls",
        description=None,
        working_dir="/tmp",
        started_at="2026-05-18T00:00:00Z",
    )
    mgr = BgTaskManager()
    mgr.bind(db=db, deliver_cb=_noop_deliver, broadcast_cb=_noop_broadcast)
    await mgr.start()
    row = await db.get_bg_task("orphan-1")
    assert row["status"] == "interrupted"
    assert row["completed_at"] is not None


async def test_restart_marks_only_orphans_and_keeps_old_limits_unknown(db):
    await _make_session_row(db, "s1", "/tmp")
    for task_id in ("legacy-finished", "legacy-running"):
        await db.create_bg_task(
            task_id=task_id, session_id="s1", command="true", description=None,
            working_dir="/tmp", started_at="2026-05-18T00:00:00Z",
        )
    await db.update_bg_task("legacy-finished", status="failed", exit_code=7)
    restarted = BgTaskManager()
    restarted.bind(db=db, deliver_cb=_noop_deliver, broadcast_cb=_noop_broadcast)
    await restarted.start()
    orphan = await restarted.get_task("legacy-running")
    history = await restarted.get_task("legacy-finished")
    assert (orphan.status, orphan.stop_reason) == ("interrupted", "server_restart")
    assert (history.status, history.stop_reason) == ("failed", None)
    assert orphan.idle_timeout_seconds is None and orphan.timeout_seconds is None
    assert history.idle_timeout_seconds is None and history.timeout_seconds is None
    assert "idle timeout: unknown" in render_delivery_prompt(history)


async def _noop_deliver(_rec):
    pass


async def _noop_broadcast(_msg):
    pass


# ---------------------------------------------------------------------------
# render_delivery_prompt
# ---------------------------------------------------------------------------


def test_render_delivery_prompt_includes_marker_and_status():
    rec = BgTaskRecord(
        id="abc123",
        session_id="s1",
        command="pytest tests/",
        description="run tests",
        working_dir="/tmp",
        status="completed",
        exit_code=0,
        stdout="3 passed",
        stderr="",
        truncated=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:05Z",
    )
    text = render_delivery_prompt(rec)
    assert text.startswith("[bg-task-result]")
    assert "abc123" in text
    assert "completed" in text
    assert "3 passed" in text


def test_render_delivery_prompt_handles_truncation_and_stderr():
    rec = BgTaskRecord(
        id="x",
        session_id="s1",
        command="bad",
        description=None,
        working_dir="/tmp",
        status="failed",
        exit_code=1,
        stdout="",
        stderr="boom",
        truncated=True,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:05Z",
    )
    text = render_delivery_prompt(rec)
    assert "truncated" in text.lower()
    assert "boom" in text


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    """In-memory DB + ASGI client + a manager wired in the same shape
    as production's lifespan. Mirrors test_attachments.client."""
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    # The subprocess is the agreed system boundary. Delivery still runs the
    # real session path, but no hermetic API check may launch a live model.
    import os

    spawn_process = asyncio.create_subprocess_exec

    async def isolated_process(program, *args, **kwargs):
        if os.path.basename(str(program)) in {"claude", "codex"}:
            raise FileNotFoundError("model CLI excluded from this hermetic test")
        return await spawn_process(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", isolated_process)

    # Mirror main.py lifespan: bind manager to the singleton used by
    # the router and broadcast through session_manager._broadcast.
    bg_task_manager.__init__()  # reset singleton state between tests
    bg_task_manager.bind(
        db=db,
        deliver_cb=session_manager.deliver_bg_result,
        broadcast_cb=session_manager._broadcast,
    )
    await bg_task_manager.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": "Bearer " + bearer}) as c:
        yield c, tmp_path

    await bg_task_manager.shutdown()
    await db.close()


async def test_rest_start_and_get_bg_task(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "echo hello", "description": "say hi"},
        headers=client.headers,
    )
    assert r.status_code == 201
    body = r.json()
    task_id = body["id"]
    assert body["status"] == "running"
    assert "knowledge_context" not in body
    assert body["delivery_disposition"] is None and body["delivery_error"] is None

    # Wait for the task to finish.
    for _ in range(100):
        g = await client.get(
            f"/api/sessions/{sess.id}/bg-tasks/{task_id}", headers=client.headers
        )
        assert g.status_code == 200
        if g.json()["status"] != "running":
            break
        await asyncio.sleep(0.05)
    final = g.json()
    assert "knowledge_context" not in final
    assert final["status"] == "completed"
    assert "hello" in final["stdout"]

    await session_manager.db.update_bg_task(task_id, delivery_disposition="unknown", delivery_error="Original ACK lost")
    detail = (await client.get(f"/api/sessions/{sess.id}/bg-tasks/{task_id}")).json()
    listed = (await client.get(f"/api/sessions/{sess.id}/bg-tasks")).json()
    for projection in (detail, next(item for item in listed if item["id"] == task_id)):
        assert projection["status"] == "completed" and projection["exit_code"] == 0
        assert projection["delivery_disposition"] == "unknown" and projection["delivery_error"] == "Original ACK lost"


async def test_rest_rejects_client_supplied_knowledge_context(api_client):
    client, wd = api_client
    session = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="contract", working_dir=str(wd),
    )
    response = await client.post(
        f"/api/sessions/{session.id}/bg-tasks", headers=client.headers,
        json={"command": "touch must-not-run", "knowledge_context": '{"version":1}'},
    )
    assert response.status_code == 422
    assert not (wd / "must-not-run").exists()


async def test_rest_task_keeps_its_explicit_limits_in_details_and_results(api_client):
    client, wd = api_client
    # Observe the real background process without starting a model on delivery.
    delivered = []
    events = []

    async def capture(record):
        delivered.append(record)

    async def capture_event(event):
        events.append(event)

    bg_task_manager.bind(
        db=session_manager.db,
        deliver_cb=capture,
        broadcast_cb=capture_event,
    )
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"],
        name="explicit limits", working_dir=str(wd),
    )
    response = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "echo started; sleep 0.05; echo finished",
              "idle_timeout_seconds": 0, "timeout_seconds": 2},
        headers=client.headers,
    )
    assert response.status_code == 201
    started = response.json()
    assert (started["idle_timeout_seconds"], started["timeout_seconds"]) == (0, 2)
    await _wait_for(lambda: bool(delivered))
    details = await client.get(
        f"/api/sessions/{sess.id}/bg-tasks/{started['id']}", headers=client.headers,
    )
    body = details.json()
    assert (body["idle_timeout_seconds"], body["timeout_seconds"]) == (0, 2)
    assert body["status"] == "completed"
    assert "finished" in body["stdout"]
    prompt = render_delivery_prompt(delivered[0])
    assert "idle timeout: disabled" in prompt
    assert "total timeout: 2s" in prompt
    assert [e["type"] for e in events] == ["bg_started", "bg_completed"]
    assert all((e["idle_timeout_seconds"], e["timeout_seconds"]) == (0, 2) for e in events)
    listing = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=client.headers)
    assert listing.json()[0] == body


INVALID_LIMITS = [
    ("idle_timeout_seconds", -1), ("idle_timeout_seconds", 0.5),
    ("idle_timeout_seconds", True), ("idle_timeout_seconds", "60"),
    ("idle_timeout_seconds", None), ("timeout_seconds", 0),
    ("timeout_seconds", -1), ("timeout_seconds", 0.5),
    ("timeout_seconds", False), ("timeout_seconds", "1800"),
    ("timeout_seconds", None),
    ("idle_timeout_seconds", 2**63), ("timeout_seconds", 2**63),
]


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
async def test_rest_rejects_invalid_limits_before_starting_a_command(api_client, field, value):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"],
        name="invalid limits", working_dir=str(wd),
    )
    response = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "touch spawned", field: value}, headers=client.headers,
    )
    assert response.status_code == 422
    assert not (wd / "spawned").exists()
    tasks = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=client.headers)
    assert tasks.json() == []


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
async def test_manager_rejects_invalid_limits_before_starting_a_command(
    manager, db, tmp_path, field, value
):
    await _make_session_row(db, "s1", str(tmp_path))
    with pytest.raises(BgTaskError, match="integer"):
        await manager.start_task(
            session_id="s1", command="touch spawned", working_dir=str(tmp_path),
            **{field: value},
        )
    assert not (tmp_path / "spawned").exists()
    assert await manager.list_tasks("s1") == []


async def test_rest_rejects_empty_command(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "  "},
        headers=client.headers,
    )
    assert r.status_code == 400


async def test_rest_list(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "true"},
        headers=client.headers,
    )
    r = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=client.headers)
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_rest_cancel_running(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    start = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "sleep 30"},
        headers=client.headers,
    )
    task_id = start.json()["id"]
    # Give it a moment to enter `running` state.
    await asyncio.sleep(0.05)
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks/{task_id}/cancel", headers=client.headers
    )
    assert r.status_code == 200
    assert r.json()["cancelled"] is True


async def test_rest_cancel_already_finished(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    start = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "true"},
        headers=client.headers,
    )
    task_id = start.json()["id"]
    # Let it complete.
    for _ in range(40):
        g = await client.get(
            f"/api/sessions/{sess.id}/bg-tasks/{task_id}", headers=client.headers
        )
        if g.json()["status"] != "running":
            break
        await asyncio.sleep(0.05)
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks/{task_id}/cancel", headers=client.headers
    )
    assert r.status_code == 200
    # Already finished → cancelled=False (no live handle).
    assert r.json()["cancelled"] is False


async def test_rest_session_required(api_client):
    client, _ = api_client
    r = await client.post(
        "/api/sessions/no-such/bg-tasks",
        json={"command": "true"},
        headers=client.headers,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cross-turn delivery integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("context_present", [True, False])
async def test_deliver_bg_result_starts_a_new_session_turn(api_client, context_present):
    """End-to-end: a completed bg task's deliver_cb runs, calls
    session_manager.deliver_bg_result → start_message → broadcasts a
    user_message with the [bg-task-result] marker. We don't run a real
    claude (no API key), so the turn errors out at backend.start time,
    but the user_message broadcast happens *before* the backend turn,
    which is what we're checking."""
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="delivery", working_dir=str(wd)
    )

    captured: list[dict] = []
    async def collect(msg):
        captured.append(msg)
    session_manager.on_broadcast("test", collect)

    try:
        from server.knowledge import AdmittedRequest, admit, derive, encode

        original = admit(
            session_id=sess.id, request_id="original", user_id=sess.user_id,
            prompt="/wiki Explain the check.",
        )
        rec = BgTaskRecord(
            id="bg-fake",
            session_id=sess.id,
            command="echo done",
            description="fake",
            working_dir=str(wd),
            status="completed",
            exit_code=0,
            stdout="done",
            stderr="",
            truncated=False,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:05Z",
            knowledge_context=encode(derive(AdmittedRequest(original.origin, original), "echo done")) if context_present else None,
        )
        delivered = await session_manager.deliver_bg_result(rec)
        if not context_present:
            assert delivered is False
            assert not any(message.get("type") == "user_message" for message in captured)
            return
        assert delivered is True

        # The user_message broadcast happens synchronously inside
        # send_message — before the backend spawn. Wait a tick for the
        # async task to push it through.
        for _ in range(50):
            if any(
                m.get("type") == "user_message"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[bg-task-result]")
                for m in captured
            ):
                break
            await asyncio.sleep(0.02)
        markers = [
            m for m in captured
            if m.get("type") == "user_message"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("[bg-task-result]")
        ]
        assert len(markers) == 1
        assert "bg-fake" in markers[0]["content"]
    finally:
        session_manager.remove_broadcast("test")


async def test_deliver_bg_result_returns_false_for_missing_session(db):
    sessions = SessionManager()
    await sessions.initialize(db)
    rec = BgTaskRecord(
        id="x",
        session_id="ghost",
        command="x",
        description=None,
        working_dir="/tmp",
        status="completed",
        exit_code=0,
        stdout="",
        stderr="",
        truncated=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z",
    )
    assert await sessions.deliver_bg_result(rec) is False


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


def _call(name: str, **kwargs):
    """Invoke a FastMCP-wrapped tool function directly."""
    from server.mcp_servers import bg as bg_mcp

    fn = getattr(bg_mcp, name)
    # FastMCP wraps; the underlying callable lives at .fn on newer
    # versions, while older versions expose the function directly.
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def test_mcp_bg_run_misconfigured(monkeypatch):
    monkeypatch.delenv("OCTOPUS_API_BASE", raising=False)
    monkeypatch.delenv("OCTOPUS_SESSION_ID", raising=False)
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    out = _call("bg_run", command="echo hi")
    assert "misconfigured" in out.lower()


def test_mcp_bg_run_rejects_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    out = _call("bg_run", command="   ")
    assert "non-empty" in out


def test_mcp_bg_run_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "task-9", "description": posted.get("description")}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["description"] = (json or {}).get("description")
        posted["command"] = (json or {}).get("command")
        return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call("bg_run", command="echo hi", description="say hi")
    assert "Started bg task `task-9`" in out
    assert posted["command"] == "echo hi"
    assert posted["description"] == "say hi"
    assert posted["url"].endswith("/api/sessions/s/bg-tasks")


def test_mcp_task_receipt_uses_the_host_confirmed_limits(monkeypatch):
    import httpx

    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted = {}

    def post(url, *, json, headers, timeout):
        posted.update(json)
        return httpx.Response(201, json={"id": "quiet-tests", **json})

    monkeypatch.setattr(httpx, "post", post)
    receipt = _call(
        "bg_run", command="echo started; sleep 2", idle_timeout_seconds=0,
        timeout_seconds=10,
    )
    assert posted["idle_timeout_seconds"] == 0
    assert posted["timeout_seconds"] == 10
    assert "idle timeout: disabled" in receipt
    assert "total timeout: 10s" in receipt


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
def test_mcp_rejects_invalid_limits_without_sending_a_request(monkeypatch, field, value):
    import httpx

    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    def post(*args, **kwargs):
        pytest.fail("an invalid command must not reach the host")

    monkeypatch.setattr(httpx, "post", post)
    assert "integer" in _call("bg_run", command="touch spawned", **{field: value})


def test_mcp_bg_cancel_handles_404(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("bg_cancel", task_id="t1")
    assert "No bg task" in out


def test_mcp_bg_list_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return []

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("bg_list")
    assert "No background tasks" in out


def test_mcp_bg_list_summary(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "t1",
                    "status": "running",
                    "description": "one",
                    "exit_code": None,
                },
                {
                    "id": "t2",
                    "status": "completed",
                    "description": None,
                    "exit_code": 0,
                    "delivery_disposition": "unknown",
                    "delivery_error": "Original ACK lost",
                },
            ]

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("bg_list")
    assert "t1" in out and "t2" in out
    assert "running" in out and "completed" in out
    assert "return=unknown (Original ACK lost)" in out


# ---------------------------------------------------------------------------
# build_args integration
# ---------------------------------------------------------------------------


def test_build_args_registers_bg_mcp_over_http_scoped_to_the_session(tmp_path):
    """The bg namespace reaches the model as an HTTP server this app hosts, not
    a subprocess to spawn (docs/2026-09-25-polish-pass.md).

    Two things this pins. The key is still `bg`, because both CLIs build the
    tool name from it — change the key and `mcp__bg__run` silently becomes
    something else, breaking every prompt that names it. And the entry is scoped
    to *this* session: with one endpoint serving every session, the identity
    that used to arrive as OCTOPUS_SESSION_ID in a spawn environment now has to
    travel with the request.
    """
    from server.harness import RunConfig, get_harness
    from server.mcp_identity import verify

    run = get_harness("claude-code").create_run(RunConfig(session_id="sess-xyz"))
    argv, spawn = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    cfg_idx = argv.index("--mcp-config") + 1
    cfg = json.loads(argv[cfg_idx])
    assert "bg" in cfg["mcpServers"], "the key is what the tool name is built from"

    entry = cfg["mcpServers"]["bg"]
    assert entry["type"] == "http"
    assert entry["url"].endswith("/mcp/bg/mcp")
    assert "command" not in entry and "args" not in entry, "nothing is spawned"

    bearer = entry["headers"]["Authorization"].removeprefix("Bearer ")
    scope = verify(bearer)
    assert scope is not None and scope.session_id == "sess-xyz"


def test_build_args_omits_session_env_when_none(tmp_path):
    from server.harness import RunConfig, get_harness

    run = get_harness("claude-code").create_run(RunConfig())  # no session_id
    argv, _ = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    env = cfg["mcpServers"]["bg"]["env"]
    assert "OCTOPUS_SESSION_ID" not in env


def test_build_args_system_prompt_teaches_bg_usage(tmp_path):
    from server.harness import RunConfig, get_harness

    run = get_harness("claude-code").create_run(RunConfig(session_id="s"))
    argv, _ = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    sp = argv[argv.index("--append-system-prompt") + 1]
    assert "mcp__bg__run" in sp
    # Bright-line rule (replaced the older "≥30s" heuristic): the
    # prompt now lists categories like test suites / builds /
    # installs that must always use bg_run.
    assert "Use bg_run unconditionally" in sp

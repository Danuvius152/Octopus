"""BG startup compensation keeps original OS and durable-record ownership."""

import asyncio
import os
import shlex
import signal
import sqlite3
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server import bg_tasks
from server.bg_tasks import BgTaskError, BgTaskManager
from server.database import Database
from server.knowledge import capture, derive
from server.process_groups import group_has_live_members_after_kill
from server.sessions.submission import ReturnRefused


@pytest.fixture
async def runtime(tmp_path):
    database = Database(str(tmp_path / "background.db"))
    await database.initialize()
    await database.save_session("session", "BG startup", str(tmp_path), "2026-11-12")

    async def deliver(record):
        return ReturnRefused("source_withdrawn")

    manager = BgTaskManager()
    manager.bind(db=database, deliver_cb=deliver, broadcast_cb=None)
    await manager.start()
    try:
        yield manager, database
    finally:
        try:
            await manager.shutdown()
        finally:
            await database.close()


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
@pytest.mark.parametrize("cause", ["shutdown", "user_cancel"])
async def test_stop_drains_late_bg_process_before_releasing_its_startup(runtime, tmp_path, monkeypatch, pipe, cause):
    manager, _database = runtime
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", .03)
    monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", .3)
    descriptor = 1 if pipe == "stdout" else 2
    script = f"import os,time\nfor _ in range(192): os.write({descriptor}, b'x'*65536)\ntime.sleep(60)"
    startup = asyncio.create_task(manager.start_task(
        session_id="session", command="exec " + shlex.join([sys.executable, "-c", script]),
        working_dir=str(tmp_path),
    ))
    process = await asyncio.wait_for(spawned, 3)
    stopping = None
    try:
        async with asyncio.timeout(5):
            while not getattr(process, pipe)._paused:
                await asyncio.sleep(.01)
        if cause == "shutdown":
            manager.begin_shutdown()
        else:
            task_id = (await manager.list_tasks("session"))[0].id
            assert await manager.cancel_task(task_id)
        await asyncio.gather(startup, return_exceptions=True)
        stopping = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert manager.drain_blockers()
        assert not stopping.done()
        release_handle.set()
        await asyncio.wait_for(stopping, 2)
        result = (await manager.list_tasks("session"))[0]
        assert (result.status, result.stop_reason) == (
            ("interrupted", "server_shutdown") if cause == "shutdown" else ("cancelled", "user_cancel")
        )
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert not group_has_live_members_after_kill(process.pid)
        await asyncio.wait_for(process.wait(), 1)
        assert not manager.drain_blockers()
    finally:
        release_handle.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        for stream in (process.stdout, process.stderr):
            while await stream.read(65536):
                pass
        await process.wait()
        await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.parametrize("failure", ["missing_binary", "vanished_cwd"])
@pytest.mark.parametrize("stopping", ["ordinary", "waiter_cancelled", "shutdown", "user_cancel"])
async def test_bg_definitive_start_failure_is_recorded_with_its_real_stop_cause(
    runtime, tmp_path, monkeypatch, failure, stopping, caplog,
):
    manager, _database = runtime
    original_spawn = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    cwd = tmp_path / "command-cwd"
    cwd.mkdir()

    async def controlled_spawn(*argv, **kwargs):
        entered.set()
        await release.wait()
        if failure == "missing_binary":
            argv = (str(tmp_path / "missing-shell"), *argv[1:])
        return await original_spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_spawn)
    startup = asyncio.create_task(manager.start_task(session_id="session", command="true", working_dir=str(cwd)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if failure == "vanished_cwd":
            cwd.rmdir()
        if stopping == "shutdown":
            manager.begin_shutdown()
        elif stopping == "user_cancel":
            task_id = (await manager.list_tasks("session"))[0].id
            assert await manager.cancel_task(task_id)
        elif stopping == "waiter_cancelled":
            startup.cancel()
        if stopping != "ordinary":
            with pytest.raises(asyncio.CancelledError):
                await startup
            assert manager.drain_blockers(), "The original spawn result remains unsettled."
        release.set()
        if stopping == "ordinary":
            with pytest.raises(BgTaskError, match="failed to spawn"):
                await startup
        async with asyncio.timeout(2):
            while (result := (await manager.list_tasks("session"))[0]).status == "running":
                await asyncio.sleep(.01)
        assert (result.status, result.stop_reason) == (
            ("interrupted", "server_shutdown") if stopping == "shutdown"
            else ("cancelled", "user_cancel") if stopping == "user_cancel" else ("failed", "startup_failed")
        )
        assert "FileNotFoundError" in result.stderr
        assert str(tmp_path / "missing-shell" if failure == "missing_binary" else cwd) in result.stderr
        assert "FileNotFoundError" in caplog.text
        assert result.completed_at is not None
        await manager.shutdown()
        assert not manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
async def test_user_cancel_preserves_normal_bg_readers_until_output_eof(runtime, tmp_path, monkeypatch, pipe):
    manager, _database = runtime
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def observe_spawn(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", .03)
    descriptor = 1 if pipe == "stdout" else 2
    script = f"import os\nwhile True: os.write({descriptor}, b'x'*65536)"
    record = await manager.start_task(
        session_id="session", command="exec " + shlex.join([sys.executable, "-c", script]),
        working_dir=str(tmp_path),
    )
    try:
        async with asyncio.timeout(3):
            while not getattr(manager._running[record.id], f"{pipe}_buf"):
                await asyncio.sleep(.01)
        assert await manager.cancel_task(record.id)
        await manager.shutdown()
        result = await manager.get_task(record.id)
        assert (result.status, result.stop_reason) == ("cancelled", "user_cancel")
        assert getattr(result, pipe).endswith("x")
        assert len(getattr(result, pipe)) <= bg_tasks.MAX_STREAM_BYTES + 100
        process = processes[0]
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert not group_has_live_members_after_kill(process.pid)
        await asyncio.wait_for(process.wait(), 1)
        assert not manager.drain_blockers()
    finally:
        try:
            os.killpg(processes[0].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await manager.shutdown()


@pytest.mark.parametrize("phase,spawn_success", [
    ("recording", False), ("recording", True), ("process", True), ("prelaunch_recording", False),
])
async def test_bg_startup_keeps_original_responsibility_when_retirement_or_recording_fails(
    tmp_path, monkeypatch, phase, spawn_success,
):
    database = Database(str(tmp_path / "failure.db"))
    await database.initialize()
    await database.save_session("session", "BG failure", str(tmp_path), "2026-11-12")

    async def deliver(record):
        return ReturnRefused("source_withdrawn")

    manager = BgTaskManager()
    manager.bind(db=database, deliver_cb=deliver, broadcast_cb=None)
    await manager.start()
    original_spawn, original_kill = asyncio.create_subprocess_exec, os.killpg
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def held_spawn(*argv, **kwargs):
        if spawn_success:
            processes.append(await original_spawn(*argv, **kwargs))
        entered.set()
        await release.wait()
        if spawn_success:
            return processes[0]
        return await original_spawn(str(tmp_path / "missing-shell"), **kwargs)

    def denied_signal(pgid, sig):
        if sig == 0:
            return original_kill(pgid, sig)
        raise PermissionError("controlled signal denial")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", held_spawn)
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", .03)
    if phase == "prelaunch_recording":
        create = database.create_bg_task

        async def held_creation(**kwargs):
            await create(**kwargs)
            entered.set()
            await release.wait()

        monkeypatch.setattr(database, "create_bg_task", held_creation)
    startup = asyncio.create_task(manager.start_task(
        session_id="session", command="exec sleep 60", working_dir=str(tmp_path),
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if phase == "prelaunch_recording":
            await database.close()
        manager.begin_shutdown()
        await asyncio.gather(startup, return_exceptions=True)
        if phase == "recording":
            # The storage resource is unavailable before the transaction.
            # Actual failed SQLite writes retain their process-fatal policy,
            # verified separately by the isolated storage regressions.
            await database.close()
        elif phase == "process":
            monkeypatch.setattr(os, "killpg", denied_signal)
        release.set()
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        assert manager.drain_blockers()
        with sqlite3.connect(tmp_path / "failure.db") as reader:
            persisted = reader.execute("SELECT status, completed_at FROM bg_tasks").fetchall()
        assert persisted == [("running", None)]
        if processes:
            if phase == "process":
                assert group_has_live_members_after_kill(processes[0].pid)
            else:
                assert not group_has_live_members_after_kill(processes[0].pid)
                assert processes[0].stdout.at_eof() and processes[0].stderr.at_eof()
        assert all(task.done() for task in manager._cleanup_tasks), "The original failure remains reachable."
    finally:
        release.set()
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        for process in processes:
            try:
                original_kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
        await database.close()


@pytest.mark.parametrize("cause", ["user_cancel", "shutdown"])
async def test_bg_cancel_before_os_submission_keeps_its_original_stop_cause(runtime, tmp_path, monkeypatch, cause):
    manager, database = runtime
    committed = asyncio.Event()
    release = asyncio.Event()
    create = database.create_bg_task
    spawns = []

    async def held_creation(**kwargs):
        await create(**kwargs)
        committed.set()
        await release.wait()

    async def forbidden_spawn(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("Cancelled pre-launch work must not reach the OS.")

    monkeypatch.setattr(database, "create_bg_task", held_creation)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    startup = asyncio.create_task(manager.start_task(session_id="session", command="true", working_dir=str(tmp_path)))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        if cause == "user_cancel":
            assert await manager.cancel_task((await manager.list_tasks("session"))[0].id)
        else:
            manager.begin_shutdown()
        with pytest.raises(asyncio.CancelledError):
            await startup
        result = (await manager.list_tasks("session"))[0]
        assert (result.status, result.stop_reason) == (
            ("cancelled", "user_cancel") if cause == "user_cancel" else ("interrupted", "server_shutdown")
        )
        assert spawns == []
        await manager.shutdown()
        assert not manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.parametrize("action", ["session", "work", "shutdown"])
@pytest.mark.parametrize("timing", ["same_caller", "after_handoff", "before_handoff"])
async def test_bg_retirement_joins_startup_handoff_instead_of_callers_lifetime(
    tmp_path, monkeypatch, action, timing,
):
    database = Database(str(tmp_path / "handoff.db"))
    await database.initialize()
    await database.save_session("session", "BG handoff", str(tmp_path), "2026-11-12")

    async def deliver(record):
        return ReturnRefused("source_withdrawn")

    manager = BgTaskManager()
    manager.bind(db=database, deliver_cb=deliver, broadcast_cb=None)
    await manager.start()
    owned = derive(capture(SimpleNamespace(id="session", user_id=None, agent_id=None), None, prompt="true"), "true")
    owned = replace(owned, return_binding=replace(owned.return_binding, work_id="handoff-work"))
    committed, release_creation = asyncio.Event(), asyncio.Event()
    start_failed, release_caller = asyncio.Event(), asyncio.Event()
    create = database.create_bg_task

    async def held_creation(**kwargs):
        await create(**kwargs)
        committed.set()
        await release_creation.wait()

    monkeypatch.setattr(database, "create_bg_task", held_creation)

    async def retire():
        if action == "session":
            await manager.retire_session("session")
        elif action == "work":
            await manager.cancel_work("handoff-work")
        else:
            await manager.shutdown()

    async def expect_retirement_failure():
        if action == "shutdown":
            with pytest.raises(RuntimeError, match="shutdown incomplete"):
                await retire()
        else:
            with pytest.raises(asyncio.CancelledError):
                await retire()

    async def caller():
        with pytest.raises(asyncio.CancelledError, match="Database is closed"):
            await manager.start_task(
                session_id="session", command="true", working_dir=str(tmp_path), owned_context=owned,
            )
        start_failed.set()
        if timing == "same_caller":
            await expect_retirement_failure()
        else:
            await release_caller.wait()

    startup = asyncio.create_task(caller())
    retirement = None
    try:
        await asyncio.wait_for(committed.wait(), 2)
        task_id = (await manager.list_tasks("session"))[0].id
        await database.close()
        if timing == "before_handoff":
            retirement = asyncio.create_task(expect_retirement_failure())
            await asyncio.sleep(0)
            assert not retirement.done(), "Retirement must wait for the original startup handoff."
            if action != "shutdown":
                release_creation.set()
        else:
            assert await manager.cancel_task(task_id)
        await asyncio.wait_for(start_failed.wait(), 2)
        if timing == "same_caller":
            done, _ = await asyncio.wait({startup}, timeout=.5)
            assert done, "Retirement must report the retained failure without waiting for itself."
            await startup
        else:
            if retirement is None:
                retirement = asyncio.create_task(expect_retirement_failure())
            done, _ = await asyncio.wait({retirement}, timeout=.5)
            assert done, "Retirement must report failure while unrelated caller work continues."
            await retirement
            assert not startup.done(), "Unrelated caller work must not be cancelled or joined."
        assert manager.drain_blockers(), "The failed original terminal commit must remain owned."
    finally:
        release_creation.set()
        release_caller.set()
        if retirement is not None:
            if not retirement.done():
                retirement.cancel()
            await asyncio.gather(retirement, return_exceptions=True)
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await database.close()

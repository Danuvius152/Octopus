"""Cancellation during shutdown must not strand application processes."""

import asyncio
import os
import signal
import sys

import pytest

from server import app_backends
from server.app_backends import RUNNING, STOPPED, BackendState, BackendSupervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("reaper_state", ["running", "cancelled", "failed"])
async def test_shutdown_drains_reaper_and_readers_before_stopping_backend(tmp_path, reaper_state):
    supervisor = BackendSupervisor()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    readers = [asyncio.create_task(proc.stdout.read()), asyncio.create_task(proc.stderr.read())]
    state = BackendState(
        app_id="test-app", app_dir=str(tmp_path), state=RUNNING,
        process=proc, pid=proc.pid, port=12345, _readers=readers.copy(),
    )
    supervisor._backends[state.app_id] = state
    supervisor.start_reaper()
    task = supervisor._reaper
    assert task is not None
    await asyncio.sleep(0)
    if reaper_state != "running":
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if reaper_state == "failed":
        async def failed_reaper():
            raise RuntimeError("reaper failed before shutdown")
        task = asyncio.create_task(failed_reaper())
        supervisor._reaper = task
        await asyncio.sleep(0)
    try:
        assert not supervisor.drain_blockers(), "An idle server remains a shutdown resource, not unfinished work"
        await supervisor.shutdown()
        assert task.done()
        assert supervisor._reaper is None
        assert all(reader.done() for reader in readers)
        assert state._readers == []
        assert state.process is None and state.pid is None and state.port is None
        assert state.state == STOPPED
        assert proc.returncode is not None, "shutdown skipped terminating the live backend"
        # A second shutdown is harmless after all owned resources were drained.
        await supervisor.shutdown()
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.asyncio
async def test_successful_install_survives_cancelling_pending_output_reader(tmp_path, monkeypatch):
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    supervisor = BackendSupervisor()
    reader_started = asyncio.Event()
    reader_finished = asyncio.Event()

    async def delayed_reader(*args):
        reader_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reader_finished.set()

    monkeypatch.setattr(supervisor, "_pump", delayed_reader)
    assert await supervisor.install("test-app", str(tmp_path))
    assert reader_started.is_set() and reader_finished.is_set()
    state = supervisor.get("test-app")
    assert state is not None and state.installed and state.state == STOPPED


@pytest.mark.asyncio
async def test_shutdown_retains_backend_when_kill_does_not_confirm_exit(tmp_path, monkeypatch):
    class UnconfirmedProcess:
        pid = 2**30
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

    proc = UnconfirmedProcess()
    signals = []
    monkeypatch.setattr(app_backends.os, "killpg", lambda pgid, sig: signals.append(sig))
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", 0.03, raising=False)
    supervisor = BackendSupervisor()
    state = BackendState(
        app_id="unconfirmed", app_dir=str(tmp_path), state=RUNNING,
        process=proc, pid=proc.pid, port=12345,
    )
    supervisor._backends[state.app_id] = state
    try:
        with pytest.raises(RuntimeError, match="process exit unconfirmed"):
            await asyncio.wait_for(supervisor.shutdown(), timeout=0.3)
        assert [sig for sig in signals if sig != 0] == [signal.SIGTERM, signal.SIGKILL]
        assert supervisor.get(state.app_id).process is proc
        assert state.pid == proc.pid and state.state != STOPPED
        proc.returncode = -9
        proc.exited.set()
        await supervisor.shutdown()
        assert state.process is None and state.state == STOPPED
    finally:
        proc.returncode = -9
        proc.exited.set()


@pytest.mark.asyncio
async def test_shutdown_seals_admission_and_retains_a_reader_that_delays_cancellation(tmp_path, monkeypatch):
    supervisor = BackendSupervisor()
    released = asyncio.Event()
    started = asyncio.Event()

    async def reader():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released.wait()

    reader_task = asyncio.create_task(reader())
    await started.wait()
    state = BackendState(app_id="reader", app_dir=str(tmp_path), state=RUNNING, _readers=[reader_task])
    supervisor._backends[state.app_id] = state
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", 0.03, raising=False)
    try:
        with pytest.raises(RuntimeError, match="output readers did not exit"):
            await supervisor.shutdown()
        assert state._readers == [reader_task]
        assert not reader_task.done() and state.state != STOPPED
        with pytest.raises(RuntimeError, match="shutting down"):
            await supervisor.install("late", str(tmp_path))
        with pytest.raises(RuntimeError, match="shutting down"):
            await supervisor.ensure_running("late", str(tmp_path))
        released.set()
        await reader_task
        await supervisor.shutdown()
        assert state._readers == [] and state.state == STOPPED
    finally:
        released.set()
        await reader_task


@pytest.mark.asyncio
async def test_shutdown_joins_an_install_process_that_is_already_running(tmp_path):
    script = tmp_path / "install.sh"
    pid_file = tmp_path / "install.pid"
    script.write_text(f"#!/bin/sh\necho $$ > '{pid_file}'\nsleep 60\n")
    script.chmod(0o700)
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("installing", str(tmp_path)))
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        await supervisor.shutdown()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert installation.done()
        assert (await asyncio.gather(installation, return_exceptions=True))[0].__class__ is asyncio.CancelledError
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        installation.cancel()
        await asyncio.gather(installation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_shutdown_owns_install_spawn_before_its_process_handle_arrives(tmp_path, monkeypatch, cancel_waiter):
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o700)
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

    monkeypatch.setattr(app_backends.asyncio, "create_subprocess_exec", delayed_spawn)
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("spawning", str(tmp_path)))
    cleanup = None
    try:
        await spawned.wait()
        if cancel_waiter:
            installation.cancel()
            await asyncio.sleep(0)
            installation.cancel()
            await asyncio.sleep(0)
            assert not installation.done(), "Cancellation must retain the outstanding spawn handoff"
        supervisor.begin_shutdown()
        cleanup = asyncio.create_task(supervisor.shutdown())
        await asyncio.sleep(0)
        assert not cleanup.done()
        release_handle.set()
        await cleanup
        assert (await asyncio.gather(installation, return_exceptions=True))[0].__class__ is asyncio.CancelledError
        assert processes[0].returncode is not None
        assert supervisor.get("spawning").process is None
    finally:
        release_handle.set()
        for proc in processes:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.gather(installation, *(task for task in [cleanup] if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_owns_completed_spawn_cancelled_before_its_waiter_resumes(tmp_path, monkeypatch):
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o700)
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release_handle.wait()
        # Completion first resolves the shield, then this callback cancels
        # the waiter before its already-queued resume can adopt the handle.
        asyncio.current_task().add_done_callback(lambda task: installation.cancel())
        return proc

    monkeypatch.setattr(app_backends.asyncio, "create_subprocess_exec", delayed_spawn)
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("completed-spawn", str(tmp_path)))
    try:
        await spawned.wait()
        installation.cancel()
        await asyncio.sleep(0)
        release_handle.set()
        await asyncio.gather(installation, return_exceptions=True)
        assert processes[0].returncode is not None, "The cancelled owner must retire its captured process"
        await supervisor.shutdown()
        assert processes[0].returncode is not None
        assert supervisor.get("completed-spawn").process is None
    finally:
        release_handle.set()
        for proc in processes:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        await asyncio.gather(installation, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_reports_a_reaper_that_retains_the_backend_lock(tmp_path, monkeypatch):
    supervisor = BackendSupervisor()
    state = BackendState(app_id="locked", app_dir=str(tmp_path), state=RUNNING)
    supervisor._backends[state.app_id] = state
    locked = asyncio.Event()
    release = asyncio.Event()

    async def reaper():
        async with state.lock:
            locked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

    task = asyncio.create_task(reaper())
    supervisor._reaper = task
    await locked.wait()
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", 0.03)
    try:
        with pytest.raises(RuntimeError, match="reaper did not exit"):
            await asyncio.wait_for(supervisor.shutdown(), 0.3)
        assert supervisor._reaper is task and not task.done()
        release.set()
        await task
        await supervisor.shutdown()
        assert supervisor._reaper is None and state.state == STOPPED
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_shutdown_drains_output_while_the_backend_handles_sigterm(tmp_path):
    source = (
        "import os, select, signal, sys\n"
        "def stop(*args):\n"
        "    sys.stdout.write('shutdown output\\n' * 100000)\n"
        "    sys.stdout.flush()\n"
        "    os._exit(0)\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.set_blocking(write_fd, False)\n"
        "signal.set_wakeup_fd(write_fd)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    select.select([read_fd], [], [])\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", source,
        stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    assert proc.stdout is not None
    await proc.stdout.readline()
    reader = asyncio.create_task(proc.stdout.read())
    supervisor = BackendSupervisor()
    state = BackendState(
        app_id="output", app_dir=str(tmp_path), state=RUNNING,
        process=proc, pid=proc.pid, _readers=[reader],
    )
    supervisor._backends[state.app_id] = state
    # Use the production grace period: this checks output draining during TERM,
    # not whether a loaded machine can transfer the entire pipe in 100 ms.
    # The wakeup pipe also covers TERM arriving just before the child blocks,
    # when a Python callback could otherwise wait for a long sleep to finish.
    try:
        await supervisor.shutdown()
        assert proc.returncode == 0
        assert reader.done() and state.process is None
        assert reader.result() == b"shutdown output\n" * 100000
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.stdout.read()
        await proc.wait()
        await asyncio.gather(reader, return_exceptions=True)

"""Cancellation during shutdown must not strand application processes."""

import asyncio
import os
import signal
import sys

import pytest

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

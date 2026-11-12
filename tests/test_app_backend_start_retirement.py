"""Startup and normal-reader retirement through BackendSupervisor entry points."""

import asyncio
import os
import shlex
import signal
import sys

import pytest

from server import app_backends
from server.app_backends import RUNNING, BackendSupervisor
from server.process_groups import group_has_live_members_after_kill


def _write_app(directory, descriptor, *, output=True):
    script = directory / "start.sh"
    code = (
        "import os,http.server\n"
        + (f"for _ in range(8): os.write({descriptor}, b'x'*65536)\n" if output else "")
        + "http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),http.server.BaseHTTPRequestHandler).serve_forever()\n"
    )
    script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", code]) + "\n")
    script.chmod(0o700)


@pytest.mark.parametrize("descriptor", [1, 2], ids=["stdout", "stderr"])
@pytest.mark.parametrize("phase", ["late_handle", "normal_reader"])
async def test_backend_stop_drains_original_output_before_reusing_its_only_slot(tmp_path, monkeypatch, descriptor, phase):
    _write_app(tmp_path, descriptor)
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .1)
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()
    if phase == "normal_reader":
        release_handle.set()

    async def held_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        if not spawned.done():
            spawned.set_result(process)
            await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", held_handle)
    supervisor = BackendSupervisor()
    starting = asyncio.create_task(supervisor.ensure_running("app", str(tmp_path)))
    process = await asyncio.wait_for(spawned, 3)
    stopping = None
    try:
        if phase == "late_handle":
            async with asyncio.timeout(3):
                while not process.stdout._paused:
                    await asyncio.sleep(.01)
        else:
            state = await asyncio.wait_for(starting, 3)
            assert state.state == RUNNING
        state = supervisor.get("app")
        original_readers = state._readers[:]
        stopping = asyncio.create_task(supervisor.stop("app"))
        await asyncio.sleep(0)
        if phase == "late_handle":
            assert supervisor.drain_blockers()
            assert not stopping.done()
        release_handle.set()
        await asyncio.wait_for(stopping, 3)
        if phase == "late_handle":
            with pytest.raises(asyncio.CancelledError):
                await starting
        assert process.returncode is not None
        assert process.stdout.at_eof()  # Backend stderr is merged into stdout.
        assert not group_has_live_members_after_kill(process.pid)
        await asyncio.wait_for(process.wait(), 1)
        assert all(reader.done() for reader in original_readers)
        assert state.process is None and not state._readers
        assert not supervisor.drain_blockers()
        _write_app(tmp_path, descriptor, output=False)
        replacement = await supervisor.ensure_running("app", str(tmp_path))
        assert replacement.state == RUNNING and replacement.process is not process
        assert len(supervisor._slots) == 1
    finally:
        release_handle.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        while await process.stdout.read(65536):
            pass
        await process.wait()
        await asyncio.gather(starting, return_exceptions=True)
        await supervisor.shutdown()


async def test_failed_stop_keeps_capacity_and_retry_drains_output_after_readers_retire(tmp_path, monkeypatch):
    code = (
        "import os,http.server,threading,time\nfrom pathlib import Path\n"
        "server=http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),http.server.BaseHTTPRequestHandler)\n"
        "threading.Thread(target=server.serve_forever,daemon=True).start()\n"
        "while not Path('pressure').exists(): time.sleep(.01)\n"
        "for _ in range(8): os.write(1,b'x'*65536)\n"
        "while True: time.sleep(1)\n"
    )
    script = tmp_path / "start.sh"
    script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", code]) + "\n")
    script.chmod(0o700)
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .1)
    supervisor = BackendSupervisor()
    state = await supervisor.ensure_running("app", str(tmp_path))
    process = state.process
    original_kill = os.killpg
    allow_signal = False

    def controlled_signal(pgid, sig):
        if sig == 0 or allow_signal:
            return original_kill(pgid, sig)
        # Preserve actual group/pipe observations while withholding delivery.

    monkeypatch.setattr(os, "killpg", controlled_signal)
    try:
        with pytest.raises(RuntimeError, match="process exit unconfirmed"):
            await supervisor.stop("app")
        assert state.process is process and len(supervisor._slots) == 1
        assert supervisor.drain_blockers()
        assert group_has_live_members_after_kill(process.pid)
        await asyncio.gather(*state._readers, return_exceptions=True)
        (tmp_path / "pressure").touch()
        async with asyncio.timeout(3):
            while not process.stdout._paused:
                await asyncio.sleep(.01)
        allow_signal = True
        await asyncio.wait_for(supervisor.stop("app"), 3)
        assert process.stdout.at_eof()
        assert not group_has_live_members_after_kill(process.pid)
        assert state.process is None and not supervisor._slots
        assert not supervisor.drain_blockers()
    finally:
        allow_signal = True
        try:
            original_kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while await process.stdout.read(65536):
            pass
        await process.wait()
        await supervisor.shutdown()

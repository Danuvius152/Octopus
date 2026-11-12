"""Application logs drain through the real supervisor while scripts are alive."""

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from server import app_backends
from server.app_backends import (
    RUNNING,
    STOPPED,
    BackendCapacityError,
    BackendSupervisor,
)


async def _wait(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


def _script(root: Path, name: str, source: str) -> None:
    script = root / name
    script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-u", "-c", source]) + "\n")
    script.chmod(0o700)


async def test_install_drains_unbroken_output_before_exit_and_keeps_the_full_disk_log(tmp_path, short_process_shutdown):
    first = "X" * (96 * 1024)
    second = "Y" * (48 * 1024)
    _script(tmp_path, "install.sh", (
        "import os,time\nfrom pathlib import Path\n"
        "os.write(1, b'X' * (96 * 1024))\n"
        "while not Path('more').exists(): time.sleep(.01)\n"
        "os.write(1, b'Y' * (48 * 1024))\n"
        "while not Path('finish').exists(): time.sleep(.01)\n"
        "os.write(1, '\\nshort line\\n跨块 UTF-8\\n'.encode())\n"
        "os.write(2, b'stderr survived\\n')\n"
        "os.write(1, b'EOF tail without newline')\n"
    ))
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("logs", str(tmp_path)))
    log = tmp_path.with_name(tmp_path.name + ".runtime") / "logs" / "backend.log"
    try:
        await _wait(lambda: log.exists() and first in log.read_text())
        state = supervisor.get("logs")
        assert state is not None and state.process is not None
        proc = state.process
        assert proc.returncode is None and not installation.done()
        (tmp_path / "more").touch()
        await _wait(lambda: first + second in log.read_text())
        assert proc.returncode is None and not installation.done()
        (tmp_path / "finish").touch()
        assert await installation
        assert proc.returncode == 0, "Draining, process exit and retirement are separate facts"
        assert state.state == STOPPED and state.process is None and not state._readers
        body = log.read_text()
        assert first + second + "\nshort line\n跨块 UTF-8\nstderr survived\nEOF tail without newline" in body
        tail = supervisor.describe("logs", str(tmp_path))["log_tail"]
        assert any("short line" in line for line in tail)
        assert any("stderr survived" in line for line in tail)
        assert any("EOF tail without newline" in line for line in tail)
        assert max(map(len, tail)) <= 4200
        assert any("continued" in line for line in tail)
    finally:
        (tmp_path / "more").touch()
        (tmp_path / "finish").touch()
        await supervisor.shutdown()
        await asyncio.gather(installation, return_exceptions=True)


@pytest.mark.parametrize("delayed_cancel", [False, True])
async def test_eof_timeout_reports_truncation_and_keeps_capacity_until_reader_exits(
    tmp_path, monkeypatch, delayed_cancel,
):
    _script(tmp_path, "start.sh", (
        "import os,socket,time\n"
        "listener=socket.socket()\n"
        "listener.bind(('127.0.0.1',int(os.environ['PORT'])))\nlistener.listen()\n"
        "time.sleep(60)\n"
    ))
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .03)
    supervisor = BackendSupervisor()
    real_pump = supervisor._pump
    release = asyncio.Event()

    async def held_reader(*args):
        await real_pump(*args)
        try:
            await release.wait()
        except asyncio.CancelledError:
            if not delayed_cancel:
                raise
            await release.wait()

    # Hold the original log consumer after EOF, without also blocking the
    # subsequent descriptor cleanup once that consumer has retired.
    monkeypatch.setattr(supervisor, "_pump", held_reader)
    try:
        state = await supervisor.ensure_running("logs", str(tmp_path))
        proc = state.process
        assert proc is not None
        if delayed_cancel:
            with pytest.raises(RuntimeError, match="output readers did not exit"):
                await supervisor.stop("logs")
            assert state.process is proc and state._readers and state._retiring
            with pytest.raises(BackendCapacityError):
                await supervisor.ensure_running("replacement", str(tmp_path))
            release.set()
            await supervisor.stop("logs")
        else:
            await supervisor.stop("logs")
        assert proc.returncode is not None
        assert state.process is None and not state._readers and state.state == STOPPED
        tail = supervisor.describe("logs", str(tmp_path))["log_tail"]
        assert any("EOF drain timed out" in line and "may be truncated" in line for line in tail)
        release.set()
        replacement = await supervisor.ensure_running("replacement", str(tmp_path))
        assert replacement.state == RUNNING
    finally:
        release.set()
        await supervisor.shutdown()


async def test_running_backend_bounds_display_without_losing_utf8_or_exit_output(tmp_path, short_process_shutdown):
    _script(tmp_path, "start.sh", (
        "import os,socket,time\nfrom pathlib import Path\n"
        "listener=socket.socket()\n"
        "listener.bind(('127.0.0.1',int(os.environ['PORT'])))\nlistener.listen()\n"
        "while not Path('emit').exists(): time.sleep(.01)\n"
        "os.write(1, b'old line\\n' * 300)\n"
        "os.write(1, b'Z' * 4095 + b'\\xe8')\n"
        "while not Path('split').exists(): time.sleep(.01)\n"
        "os.write(1, b'\\xb7\\xa8\\xe5\\x9d\\x97\\n')\n"
        "os.write(2, b'stderr after split\\n')\n"
        "while not Path('finish').exists(): time.sleep(.01)\n"
        "os.write(1, b'T' * (256 * 1024) + b'final tail\\xe2\\x82')\n"
    ))
    supervisor = BackendSupervisor()
    log = tmp_path.with_name(tmp_path.name + ".runtime") / "logs" / "backend.log"
    try:
        state = await supervisor.ensure_running("logs", str(tmp_path))
        assert state.state == RUNNING and state.process is not None
        proc = state.process
        (tmp_path / "emit").touch()
        await _wait(lambda: "Z" * 4095 in log.read_text())
        assert proc.returncode is None
        (tmp_path / "split").touch()
        await _wait(lambda: "stderr after split" in log.read_text())
        assert "Z" * 4095 + "跨块\n" in log.read_text()
        tail = supervisor.describe("logs", str(tmp_path))["log_tail"]
        assert len(tail) <= 200 and "earlier output omitted" in tail[0]
        assert max(map(len, tail)) <= 4200
        assert any("跨" in fragment for fragment in tail) and any("块" in fragment for fragment in tail)
        assert not any("�" in fragment for fragment in tail)
        (tmp_path / "finish").touch()
        await asyncio.wait_for(proc.wait(), 3)
        assert proc.returncode == 0
        await supervisor.stop("logs")
        assert state.state == STOPPED and state.process is None and not state._readers
        assert "T" * (256 * 1024) + "final tail�" in log.read_text()
        tail = supervisor.describe("logs", str(tmp_path))["log_tail"]
        assert len(tail) <= 200 and max(map(len, tail)) <= 4200
        assert any("final tail�" in fragment for fragment in tail)
    finally:
        await supervisor.shutdown()


async def test_install_waits_for_pipe_eof_before_retiring(tmp_path, monkeypatch):
    _script(tmp_path, "install.sh", "print('last line', flush=True)")
    real_read = asyncio.StreamReader.read
    at_eof, finish_eof = asyncio.Event(), asyncio.Event()

    async def held_eof(stream, n=-1):
        raw = await real_read(stream, n)
        if not raw:
            at_eof.set()
            await finish_eof.wait()
        return raw

    # Delay only the pipe's EOF delivery; the child and supervisor remain real.
    monkeypatch.setattr(asyncio.StreamReader, "read", held_eof)
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("eof", str(tmp_path)))
    try:
        await asyncio.wait_for(at_eof.wait(), 3)
        state = supervisor.get("eof")
        assert state is not None and state.process is not None
        await asyncio.sleep(.03)
        assert state.process.returncode == 0
        assert not installation.done(), "Process exit does not prove the reader drained EOF"
        finish_eof.set()
        assert await installation
        assert state.process is None and not state._readers
    finally:
        finish_eof.set()
        await supervisor.shutdown()
        await asyncio.gather(installation, return_exceptions=True)


async def test_disk_log_failure_does_not_stop_install_or_hide_its_display(tmp_path):
    _script(tmp_path, "install.sh", "print('still visible', flush=True)")
    runtime = tmp_path.with_name(tmp_path.name + ".runtime")
    runtime.mkdir()
    (runtime / "logs").write_text("not a directory")
    supervisor = BackendSupervisor()
    try:
        assert await supervisor.install("unwritable", str(tmp_path))
        tail = supervisor.describe("unwritable", str(tmp_path))["log_tail"]
        assert "[install] still visible" in tail
        assert supervisor.get("unwritable").process is None
    finally:
        await supervisor.shutdown()


async def test_shutdown_allows_the_original_install_its_eof_and_cancel_windows(tmp_path, monkeypatch):
    _script(tmp_path, "install.sh", "import time; print('alive', flush=True); time.sleep(60)")
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .1)
    real_read = asyncio.StreamReader.read
    cancellation, release = asyncio.Event(), asyncio.Event()

    async def held_eof(stream, n=-1):
        raw = await real_read(stream, n)
        if not raw:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation.set()
                await release.wait()
        return raw

    monkeypatch.setattr(asyncio.StreamReader, "read", held_eof)
    supervisor = BackendSupervisor()
    installation = asyncio.create_task(supervisor.install("stopping", str(tmp_path)))
    shutdown = None
    try:
        await _wait(lambda: any("alive" in line for line in supervisor.describe("stopping", str(tmp_path))["log_tail"]))
        shutdown = asyncio.create_task(supervisor.shutdown())
        await asyncio.wait_for(cancellation.wait(), 1)
        # EOF has used 100ms; cancellation needs another 40ms. The old outer
        # 120ms deadline would report failure while cleanup still has time.
        await asyncio.sleep(.04)
        release.set()
        await shutdown
        assert installation.done() and installation.cancelled()
        assert supervisor.get("stopping").process is None
    finally:
        release.set()
        await supervisor.shutdown()
        await asyncio.gather(installation, *(task for task in [shutdown] if task is not None), return_exceptions=True)


async def test_stop_joins_idle_reaping_through_the_second_reader_window(tmp_path, monkeypatch):
    _script(tmp_path, "start.sh", (
        "import os,socket,time\n"
        "listener=socket.socket()\n"
        "listener.bind(('127.0.0.1',int(os.environ['PORT'])))\nlistener.listen()\n"
        "time.sleep(60)\n"
    ))
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(app_backends, "_KILL_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .1)
    real_read = asyncio.StreamReader.read
    at_eof, cancellation, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held_eof(stream, n=-1):
        raw = await real_read(stream, n)
        if not raw:
            at_eof.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation.set()
                await release.wait()
        return raw

    monkeypatch.setattr(asyncio.StreamReader, "read", held_eof)
    supervisor = BackendSupervisor()
    reaping = stopping = None
    try:
        state = await supervisor.ensure_running("idle", str(tmp_path))
        proc = state.process
        state.last_used -= app_backends.IDLE_TIMEOUT_S + 1
        reaping = asyncio.create_task(supervisor.reap_idle())
        await asyncio.wait_for(at_eof.wait(), 1)
        stopping = asyncio.create_task(supervisor.stop("idle"))
        await asyncio.wait_for(cancellation.wait(), 1)
        await asyncio.sleep(.04)
        release.set()
        assert await reaping == 1
        await stopping
        assert proc is not None and proc.returncode is not None
        assert state.process is None and not state._readers and state.state == STOPPED
        assert not supervisor.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(*(task for task in [reaping, stopping] if task is not None), return_exceptions=True)
        await supervisor.shutdown()

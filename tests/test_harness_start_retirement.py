"""Startup retirement through the original Harness APIs and real OS pipes."""

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest

from server.harness import (
    Harness,
    HarnessOneshotError,
    OneShotContext,
    RunConfig,
    get_harness,
)
from server.harness.private_settings import PrivateSettings
from server.mcp_identity import close_work
from server.process_groups import group_has_live_members_after_kill
from tests.harness_profiles import process_profile


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
async def test_cancelled_oneshot_drains_output_from_a_late_process_handle(tmp_path, monkeypatch, pipe):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    descriptor = 1 if pipe == "stdout" else 2
    script = f"import os,time\nfor _ in range(192): os.write({descriptor}, b'x'*65536)\ntime.sleep(60)"
    profile = process_profile(
        get_harness("codex").profile,
        build_oneshot_argv=lambda ctx: ([sys.executable, "-c", script], {"cwd": str(tmp_path)}),
    )
    harness = Harness(profile)
    invocation = asyncio.create_task(harness.run_oneshot(OneShotContext(prompt="late output")))
    process = await asyncio.wait_for(spawned, 3)
    try:
        async with asyncio.timeout(5):
            # Observe actual asyncio pipe backpressure. Some hosts hide the
            # kernel wait channel, so /proc/wchan cannot establish this fact.
            while not getattr(process, pipe)._paused:
                await asyncio.sleep(.01)
        invocation.cancel("stop original work")
        await asyncio.sleep(0)
        assert harness.drain_blockers(), "The pending spawn must retain its original owner."
        release_handle.set()
        with pytest.raises(asyncio.CancelledError, match="stop original work"):
            async with asyncio.timeout(3):
                await invocation
        assert process.returncode is not None
        assert not group_has_live_members_after_kill(process.pid)
        assert process.stdout.at_eof() and process.stderr.at_eof()
        await asyncio.wait_for(process.wait(), 1)
        assert not harness.drain_blockers()
        await harness.shutdown()
    finally:
        if not invocation.done():
            invocation.cancel()
            await asyncio.sleep(0)
        release_handle.set()
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        # On a regression, release transport backpressure so the test's real
        # child and original retirement operation can still finish.
        for stream in (process.stdout, process.stderr):
            while await stream.read(65536):
                pass
        await process.wait()
        await asyncio.gather(invocation, return_exceptions=True)


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
@pytest.mark.parametrize("stop_at", ["start_waiter", "retirement", "closed_work", "retire_then_cancel"])
async def test_run_retires_a_backpressured_process_before_normal_readers(
    tmp_path, monkeypatch, backend, pipe, stop_at,
):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    descriptor = 1 if pipe == "stdout" else 2
    script = f"import os,time\nfor _ in range(192): os.write({descriptor}, b'x'*65536)\ntime.sleep(60)"
    profile = process_profile(
        get_harness(backend).profile,
        build_turn_argv=lambda ctx: ([sys.executable, "-c", script], {"cwd": str(tmp_path)}),
    )
    run = Harness(profile).create_run(RunConfig(work_id=f"late-{tmp_path.name}"))
    starting = asyncio.create_task(run.start("late output", str(tmp_path)))
    process = await asyncio.wait_for(spawned, 3)
    try:
        async with asyncio.timeout(5):
            while not getattr(process, pipe)._paused:
                await asyncio.sleep(.01)
        if stop_at in {"retirement", "retire_then_cancel"}:
            run.retire()
            await asyncio.sleep(0)  # The original retirement now owns the pending spawn.
        if stop_at in {"start_waiter", "retire_then_cancel"}:
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        elif stop_at == "closed_work":
            close_work(run.work_id)
        assert run.retirement_state == "pending"
        release_handle.set()
        async with asyncio.timeout(6):
            with pytest.raises(asyncio.CancelledError):
                await starting
            await run.stop()
        assert run.retirement_state == "proven"
        assert not run.is_alive()
        assert process.returncode is not None
        assert not group_has_live_members_after_kill(process.pid)
        assert process.stdout.at_eof() and process.stderr.at_eof()
        await asyncio.wait_for(process.wait(), 1)
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        release_handle.set()
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        for stream in (process.stdout, process.stderr):
            while await stream.read(65536):
                pass
        await process.wait()
        await asyncio.gather(run.stop(), return_exceptions=True)


@pytest.mark.parametrize("kind", ["oneshot", "run"])
@pytest.mark.parametrize("timing", ["ordinary", "cancelled"])
@pytest.mark.parametrize("failure", ["missing_binary", "vanished_cwd", "transport"])
async def test_definitive_spawn_failure_does_not_become_retirement_failure(
    tmp_path, monkeypatch, kind, timing, failure, caplog,
):
    original_spawn = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    cwd = tmp_path / "working"
    argv = [str(tmp_path / "missing")] if failure == "missing_binary" else [sys.executable, "-c", "pass"]

    async def controlled_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        if failure == "transport":
            raise RuntimeError("OS launch transport unavailable")
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_spawn)
    profile = process_profile(
        get_harness("codex").profile,
        build_turn_argv=lambda ctx: (argv, {"cwd": str(cwd)}),
        build_oneshot_argv=lambda ctx: (argv, {"cwd": str(cwd)}),
    )
    harness = Harness(profile)
    run = harness.create_run()
    invocation = asyncio.create_task(
        harness.run_oneshot(OneShotContext(prompt="failed start")) if kind == "oneshot"
        else run.start("failed start", str(cwd)),
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if failure == "vanished_cwd":
            cwd.rmdir()  # Remove it after prepare_spawn, at the real OS boundary.
        if timing == "cancelled":
            invocation.cancel("cancel launch")
            await asyncio.sleep(0)
            if kind == "run":
                await asyncio.gather(invocation, return_exceptions=True)
                assert run.retirement_state == "pending"
            else:
                assert harness.drain_blockers()
        release.set()
        expected = (asyncio.CancelledError,) if timing == "cancelled" else (FileNotFoundError, HarnessOneshotError, RuntimeError)
        with pytest.raises(expected) as raised:
            async with asyncio.timeout(3):
                await invocation
        assert "retirement" not in str(raised.value), "A completed failed launch has no process to reap."
        if kind == "run":
            await asyncio.wait_for(run.stop(), 2)
            assert run.retirement_state == "proven"
        else:
            assert not harness.drain_blockers()
            await harness.shutdown()
        if failure == "transport":
            assert "OS launch transport unavailable" in caplog.text
    finally:
        release.set()
        await asyncio.gather(invocation, return_exceptions=True)
        if kind == "run":
            await asyncio.gather(run.stop(), return_exceptions=True)


@pytest.mark.parametrize("kind", ["oneshot", "run"])
async def test_failed_launch_still_exposes_private_settings_cleanup_failure(tmp_path, monkeypatch, kind):
    original_spawn = asyncio.create_subprocess_exec
    private = PrivateSettings(argument_index=1, values={"test": True})
    captured = []

    async def fail_with_unremovable_settings(*argv, **kwargs):
        directory = Path(argv[1]).parent
        captured.append(directory)
        directory.chmod(0o500)  # Real unlink failure, independent of spawn failure.
        return await original_spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_with_unremovable_settings)

    def render(ctx):
        return [str(tmp_path / "missing"), "settings-placeholder"], {"private_settings": private}

    profile = process_profile(get_harness("codex").profile, build_turn_argv=render, build_oneshot_argv=render)
    harness = Harness(profile)
    run = harness.create_run()
    try:
        with pytest.raises((RuntimeError, PermissionError)):
            if kind == "oneshot":
                await harness.run_oneshot(OneShotContext(prompt="cleanup failure"))
            else:
                await run.start("cleanup failure", str(tmp_path))
        assert captured and captured[0].exists()
        if kind == "oneshot":
            assert harness.drain_blockers()
            with pytest.raises(PermissionError):
                await harness.shutdown()
        else:
            with pytest.raises(PermissionError):
                await run.stop()
            assert run.retirement_state == "failed"
    finally:
        for directory in captured:
            directory.chmod(0o700)
            shutil.rmtree(directory)
        if kind == "run":
            await asyncio.gather(run.stop(), return_exceptions=True)


async def test_resource_free_launch_does_not_hide_input_history_recording_failure(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        raise FileNotFoundError("definitive OS launch failure")

    async def failed_record(identity, possibly_sent):
        assert identity is None and not possibly_sent
        raise OSError("input history recording failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)
    profile = process_profile(
        get_harness("claude-code").profile,
        build_turn_argv=lambda ctx: ([sys.executable], {"cwd": str(tmp_path)}),
    )
    run = Harness(profile).create_run()
    run.bind_input_history_recorder(failed_record)
    starting = asyncio.create_task(run.start("recording failure", str(tmp_path)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        release.set()
        with pytest.raises(OSError, match="input history recording failed"):
            await asyncio.wait_for(run.stop(), 2)
        assert run.retirement_state == "failed"
    finally:
        release.set()
        await asyncio.gather(starting, run.stop(), return_exceptions=True)


async def test_late_run_keeps_failed_process_exit_responsibility(tmp_path, monkeypatch, short_process_shutdown):
    original_spawn, original_kill = asyncio.create_subprocess_exec, os.killpg
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    def withheld_kill(pgid, sig):
        if sig == 0:
            return original_kill(pgid, sig)
        # OS-boundary fault: the captured group remains alive. Exit proof
        # still runs against the real process and cannot use the sent signal.

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    monkeypatch.setattr(os, "killpg", withheld_kill)
    profile = process_profile(
        get_harness("codex").profile,
        build_turn_argv=lambda ctx: ([sys.executable, "-c", "import time;time.sleep(60)"], {}),
    )
    run = Harness(profile).create_run()
    starting = asyncio.create_task(run.start("exit failure", str(tmp_path)))
    process = await asyncio.wait_for(spawned, 2)
    try:
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        release_handle.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run.stop(), 2)
        assert run.retirement_state == "failed"
        assert process.returncode is None
        assert group_has_live_members_after_kill(process.pid)
        with pytest.raises(TimeoutError):
            await run.stop()  # A later observer cannot manufacture successful retirement.
    finally:
        release_handle.set()
        original_kill(process.pid, signal.SIGKILL)
        await process.communicate()
        await asyncio.gather(starting, run.stop(), return_exceptions=True)


@pytest.mark.parametrize("kind", ["oneshot", "run"])
@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
async def test_late_handle_retires_descendant_pipes_after_the_leader_has_exited(tmp_path, monkeypatch, kind, pipe):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    descriptor = 1 if pipe == "stdout" else 2
    script = (
        "import os,time\n"
        "if os.fork(): os._exit(0)\n"
        f"for _ in range(192): os.write({descriptor}, b'x'*65536)\n"
        "time.sleep(60)\n"
    )
    def render(ctx):
        return [sys.executable, "-c", script], {"cwd": str(tmp_path)}
    profile = process_profile(get_harness("codex").profile, build_turn_argv=render, build_oneshot_argv=render)
    harness = Harness(profile)
    run = harness.create_run()
    starting = asyncio.create_task(
        harness.run_oneshot(OneShotContext(prompt="exited leader")) if kind == "oneshot"
        else run.start("exited leader", str(tmp_path)),
    )
    process = await asyncio.wait_for(spawned, 3)
    try:
        async with asyncio.timeout(5):
            while process.returncode is None or not getattr(process, pipe)._paused:
                await asyncio.sleep(.01)
        assert process.returncode == 0
        assert group_has_live_members_after_kill(process.pid), "The descendant still owns the pipe's write end."
        starting.cancel()
        await asyncio.sleep(0)
        release_handle.set()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(3):
                await starting
        if kind == "run":
            await asyncio.wait_for(run.stop(), 3)
            assert run.retirement_state == "proven"
        else:
            assert not harness.drain_blockers()
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert not group_has_live_members_after_kill(process.pid)
        await asyncio.wait_for(process.wait(), 1)
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.sleep(0)
        release_handle.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for stream in (process.stdout, process.stderr):
            while await stream.read(65536):
                pass
        await process.wait()
        await asyncio.gather(starting, return_exceptions=True)
        if kind == "run":
            await asyncio.gather(run.stop(), return_exceptions=True)

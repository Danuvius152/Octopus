"""Private credential settings follow the actual subprocess lifetime."""

import asyncio
import json
import os
import stat
import struct
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.harness import (
    Harness,
    HarnessCredential,
    HarnessOneshotError,
    OneShotContext,
    RunConfig,
    get_harness,
)
from server.harness.private_settings import PrivateSettings
from server.harness.run import prepare_spawn


@pytest.fixture
def settings_harness(tmp_path):
    evidence = tmp_path / "settings-path"
    script = tmp_path / "cli.py"
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "path=Path(sys.argv[sys.argv.index('--settings')+1])\n"
        "settings=json.loads(path.read_text())\n"
        "assert settings['env']['ANTHROPIC_API_KEY']=='selected-key'\n"
        "assert settings['env']['ANTHROPIC_AUTH_TOKEN']==''\n"
        f"Path({str(evidence)!r}).write_text(str(path))\n"
        "if '--hang' in sys.argv: time.sleep(30)\n"
        "print(json.dumps({'type':'result','is_error':False,'session_id':'settings-thread','result':'OK'}),flush=True)\n"
        "if '--input-format' in sys.argv: sys.stdin.read()\n"
    )
    original = get_harness("claude-code").profile

    def wrap(render):
        def argv(context):
            args, kwargs = render(context)
            kwargs["private_settings"].argument_index += 1
            return [sys.executable, str(script), *args[1:]], kwargs
        return argv

    profile = replace(original, binary=sys.executable,
                      build_turn_argv=wrap(original.build_turn_argv),
                      build_oneshot_argv=wrap(original.build_oneshot_argv))
    return Harness(profile), evidence


def _credential():
    return HarnessCredential(backend="claude-code", auth_type="api_key", secret="selected-key")


async def _settings_path(evidence):
    async with asyncio.timeout(3):
        while not evidence.exists():
            await asyncio.sleep(.01)
    path = Path(evidence.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    return path


async def test_streaming_settings_persist_until_retirement(settings_harness, tmp_path):
    harness, evidence = settings_harness
    run = harness.create_run(RunConfig(mcp_servers=[]))
    try:
        await run.start("hello", str(tmp_path), credential=_credential())
        path = await _settings_path(evidence)
        assert [event.type async for event in run.stream()] == ["result"]
        assert path.exists(), "a held CLI can reread its settings"
    finally:
        await run.stop()
    assert not path.parent.exists()


@pytest.mark.parametrize("cancel", [False, True])
async def test_oneshot_settings_cleanup_on_exit_and_cancel(settings_harness, tmp_path, cancel):
    harness, evidence = settings_harness
    task = asyncio.create_task(harness.run_oneshot(OneShotContext(
        prompt="--hang" if cancel else "hello", credential=_credential(), working_dir=str(tmp_path),
    )))
    if cancel:
        path = await _settings_path(evidence)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == "OK"
        path = Path(evidence.read_text())
    assert not path.parent.exists()


@pytest.mark.parametrize("oneshot", [False, True])
async def test_settings_cleanup_when_os_spawn_fails(settings_harness, tmp_path, monkeypatch, oneshot):
    harness, _evidence = settings_harness
    observed = []

    async def fail(*args, **kwargs):
        path = Path(args[args.index("--settings") + 1])
        assert json.loads(path.read_text())["env"]["ANTHROPIC_API_KEY"] == "selected-key"
        observed.append(path)
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    if oneshot:
        with pytest.raises(OSError, match="synthetic"):
            await harness.run_oneshot(OneShotContext(prompt="hello", credential=_credential()))
    else:
        run = harness.create_run(RunConfig(mcp_servers=[]))
        with pytest.raises(OSError, match="synthetic"):
            await run.start("hello", str(tmp_path), credential=_credential())
        await run.stop()
    assert observed and all(not path.parent.exists() for path in observed)


@pytest.mark.parametrize("cancel", [False, True])
async def test_oneshot_final_settings_budget_is_configuration_error(settings_harness, monkeypatch, caplog, cancel):
    from server.harness import launch_limits

    harness, _evidence = settings_harness
    authorized, release = asyncio.Event(), asyncio.Event()

    async def authorize():
        authorized.set()
        await release.wait()

    context = OneShotContext(prompt="hello", credential=_credential(), authorize_spawn=authorize)
    argv, kwargs = harness.profile.build_oneshot_argv(context)
    kwargs.pop("private_settings")
    argv, kwargs = prepare_spawn(argv, kwargs)
    env = kwargs["env"]
    # The placeholder fits exactly; the real path must exceed the same budget.
    budget = (len(argv) + len(env) + 2) * struct.calcsize("P")
    budget += sum(len(os.fsencode(arg)) + 1 for arg in argv)
    budget += sum(len(os.fsencode(f"{key}={value}")) + 1 for key, value in env.items())
    monkeypatch.setattr(launch_limits, "launch_limits", lambda: (1024 * 1024, budget))
    observed = []
    materialize = PrivateSettings.materialize

    def capture(settings, args):
        rendered = materialize(settings, args)
        observed.append(Path(rendered[settings.argument_index]))
        return rendered

    async def forbidden_spawn(*_args, **_kwargs):
        pytest.fail("final configuration refusal must precede OS submission")

    monkeypatch.setattr(PrivateSettings, "materialize", capture)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    task = asyncio.create_task(harness.run_oneshot(context))
    await asyncio.wait_for(authorized.wait(), 3)
    if cancel:
        task.cancel()
        await asyncio.sleep(0)
    release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HarnessOneshotError, match="Combined arguments/environment") as error:
            await task
        assert error.value.code == "configuration"
    await asyncio.sleep(0)
    assert observed and all(not path.parent.exists() for path in observed)
    assert "retirement failed" not in caplog.text


@pytest.mark.parametrize("oneshot", [False, True])
async def test_cancelled_late_spawn_keeps_settings_until_process_retired(settings_harness, tmp_path, monkeypatch, oneshot):
    harness, evidence = settings_harness
    original = asyncio.create_subprocess_exec
    release = asyncio.Event()

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    run = harness.create_run(RunConfig(mcp_servers=[]))
    task = asyncio.create_task(
        harness.run_oneshot(OneShotContext(prompt="--hang", credential=_credential(), working_dir=str(tmp_path)))
        if oneshot else run.start("hello", str(tmp_path), credential=_credential())
    )
    path = await _settings_path(evidence)
    task.cancel()
    await asyncio.sleep(0)
    assert path.exists(), "a late process handle still owns its credential input"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    if not oneshot:
        await run.stop()
    assert not path.parent.exists()


async def test_oneshot_io_failure_retires_process_and_settings(settings_harness, tmp_path, monkeypatch):
    harness, evidence = settings_harness

    async def fail(process, *args, **kwargs):
        await _settings_path(evidence)
        raise OSError("synthetic pipe failure")

    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", fail)
    with pytest.raises(OSError, match="synthetic pipe failure"):
        await harness.run_oneshot(OneShotContext(prompt="--hang", credential=_credential(), working_dir=str(tmp_path)))
    assert not Path(evidence.read_text()).parent.exists()


@pytest.mark.parametrize("oneshot", [False, True])
async def test_authorization_refusal_creates_no_settings(settings_harness, tmp_path, monkeypatch, oneshot):
    harness, _evidence = settings_harness

    async def denied():
        raise ValueError("credential revoked before spawn")

    def unexpected(*_args):
        pytest.fail("authorization must precede materializing private credentials")

    monkeypatch.setattr(PrivateSettings, "materialize", unexpected)
    run = harness.create_run(RunConfig(mcp_servers=[], authorize_spawn=denied))
    try:
        with pytest.raises(Exception, match="credential revoked"):
            if oneshot:
                await harness.run_oneshot(OneShotContext(prompt="hello", credential=_credential(), authorize_spawn=denied))
            else:
                await run.start("hello", str(tmp_path), credential=_credential())
    finally:
        await run.stop()


def test_partial_settings_write_rolls_back(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    settings = PrivateSettings(1, {"env": {"ANTHROPIC_API_KEY": "selected-key"}})

    def failed_write(*_args):
        raise OSError("synthetic settings write failure")

    monkeypatch.setattr(json, "dump", failed_write)
    with pytest.raises(OSError, match="synthetic settings write failure"):
        settings.materialize(["claude", "<settings>"])
    assert list(tmp_path.iterdir()) == []

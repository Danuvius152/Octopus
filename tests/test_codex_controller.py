"""Native instruction delivery is acknowledged, bounded and owned until exit."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from server.harness import RunConfig, get_harness
from server.harness.codex_control import refresh_instructions
from server.harness.profile import TurnContext
from server.harness.run import HarnessRun


@pytest.fixture
def controller(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['OCTOPUS_CONTROLLER_TEST_ROOT'])
(root / 'pid').write_text(str(os.getpid()))
with (root / 'argv.json').open('w') as handle:
    json.dump(sys.argv[1:], handle)
if sys.argv[1] != 'app-server':
    (root / 'exec-started').touch()
    raise SystemExit(0)
mode = os.environ.get('OCTOPUS_CONTROLLER_TEST_MODE', '')
if mode == 'noisy':
    sys.stderr.write('x' * 1024 * 1024)
    sys.stderr.flush()
for line in sys.stdin:
    message = json.loads(line)
    with (root / 'requests.jsonl').open('a') as handle:
        handle.write(json.dumps(message) + '\\n')
    if 'id' not in message:
        continue
    method = message['method']
    if method == 'thread/resume':
        if mode == 'hold':
            time.sleep(60)
        if mode == 'eof':
            raise SystemExit(0)
        result = {'thread': {'id': 'wrong' if mode == 'wrong-thread' else message['params']['threadId']}}
    else:
        result = {}
    if mode == method:
        response = {'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported operation'}}
    else:
        response = {'id': message['id'], 'result': result}
    print(json.dumps(response), flush=True)
(root / 'closed').touch()
''')
    binary.chmod(0o700)
    context = TurnContext(prompt="hello", working_dir=str(tmp_path), resume_id="thread-proof",
        system_prompt="All agents can query Wiki. " * 10000, model=None,
        tool_allow=None, tool_deny=None, mcp_servers=[], credential=None)
    options = {"cwd": str(tmp_path), "env": {**os.environ, "OCTOPUS_CONTROLLER_TEST_ROOT": str(tmp_path)},
               "start_new_session": True}
    return context, [str(binary), "exec"], options, tmp_path


def assert_reaped(root):
    pid = int((root / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_current_developer_snapshot_uses_stdin_and_waits_for_native_exit(controller):
    context, argv, options, root = controller
    context = replace(context, model="fixture-model", reasoning_effort="high", service_tier="fast")
    options["env"]["OCTOPUS_CONTROLLER_TEST_MODE"] = "noisy"
    await refresh_instructions(context, argv, options)
    requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
    assert [item["method"] for item in requests] == ["initialize", "initialized", "thread/resume", "thread/inject_items"]
    resumed = requests[2]["params"]
    assert resumed["model"] == "fixture-model"
    assert resumed["config"]["model_reasoning_effort"] == "high"
    assert resumed["config"]["service_tier"] == "fast"
    injected = requests[-1]["params"]
    assert injected["threadId"] == context.resume_id
    assert injected["items"][0]["role"] == "developer"
    assert injected["items"][0]["content"][0]["text"].endswith(context.system_prompt)
    assert (root / "closed").exists()
    arguments = json.loads((root / "argv.json").read_text())
    assert max(map(len, arguments)) < 1000
    assert "features.retain_client_developer_messages=true" in arguments
    assert "mcp_servers={}" in arguments
    assert not (root / "exec-started").exists()
    assert_reaped(root)


@pytest.mark.parametrize("failure", ["thread/resume", "thread/inject_items", "wrong-thread", "eof"])
async def test_failed_controller_delivery_prevents_exec_resume(controller, failure):
    context, argv, options, root = controller
    options["env"]["OCTOPUS_CONTROLLER_TEST_MODE"] = failure
    base = get_harness("codex").profile
    profile = replace(base, activity_observer=None, build_turn_argv=lambda ctx: (argv, options))
    run = HarnessRun(profile, RunConfig())
    with pytest.raises(RuntimeError):
        await run.start(context.prompt, context.working_dir, resume_id=context.resume_id)
    await run.stop()
    assert not (root / "exec-started").exists()
    assert_reaped(root)


async def test_controller_timeout_reaps_its_process(controller, monkeypatch):
    from server.harness import codex_control

    context, argv, options, root = controller
    options["env"]["OCTOPUS_CONTROLLER_TEST_MODE"] = "hold"
    monkeypatch.setattr(codex_control, "_UPDATE_TIMEOUT", .5)
    with pytest.raises(TimeoutError):
        await refresh_instructions(context, argv, options)
    assert_reaped(root)


async def test_stop_during_controller_delivery_confirms_retirement(controller):
    context, argv, options, root = controller
    options["env"]["OCTOPUS_CONTROLLER_TEST_MODE"] = "hold"
    profile = replace(get_harness("codex").profile, activity_observer=None,
                      build_turn_argv=lambda ctx: (argv, options))
    run = HarnessRun(profile, RunConfig())
    start = asyncio.create_task(run.start(context.prompt, context.working_dir, resume_id=context.resume_id))
    async with asyncio.timeout(5):
        while not (root / "requests.jsonl").exists() or "thread/resume" not in (root / "requests.jsonl").read_text():
            await asyncio.sleep(.01)
    await run.stop()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert not (root / "exec-started").exists()
    assert_reaped(root)


async def test_cancel_during_spawn_still_reaps_late_process_handle(controller, monkeypatch):
    context, argv, options, root = controller
    launch = asyncio.create_subprocess_exec
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_handle(*args, **kwargs):
        process = await launch(*args, **kwargs)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    update = asyncio.create_task(refresh_instructions(context, argv, options))
    await started.wait()
    async with asyncio.timeout(5):
        while not (root / "pid").exists():
            await asyncio.sleep(.01)
    update.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await update
    assert_reaped(root)

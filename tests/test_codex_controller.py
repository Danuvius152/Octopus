"""Native instruction delivery is acknowledged, bounded and owned until exit."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from server.harness import RunConfig, get_harness
from server.harness.codex_control import controller_message, refresh_instructions
from server.harness.profile import TurnContext
from server.harness.run import HarnessRun


@pytest.fixture
def controller(tmp_path, _hermetic_execution_boundary):
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
        result = {'thread': {'id': 'wrong' if mode == 'wrong-thread' else message['params']['threadId'], 'path': str(root / 'history.jsonl')}}
    else:
        result = {}
    if method == 'thread/inject_items' and mode != method:
        with (root / 'history.jsonl').open('a') as handle:
            for item in message['params']['items']:
                handle.write(json.dumps({'type': 'response_item', 'payload': item}) + '\\n')
        if mode == 'lost-ack':
            raise SystemExit(0)
    if mode == method:
        response = {'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported operation'}}
    else:
        response = {'id': message['id'], 'result': result}
    print(json.dumps(response), flush=True)
(root / 'closed').touch()
''')
    binary.chmod(0o700)
    (tmp_path / "history.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-proof"}}) + "\n")
    _hermetic_execution_boundary.allow_fake_executable(binary)
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
    assert injected["items"][0]["content"][0]["text"] == controller_message(context.system_prompt)
    assert (root / "closed").exists()
    arguments = json.loads((root / "argv.json").read_text())
    assert max(map(len, arguments)) < 1000
    assert "features.retain_client_developer_messages=true" in arguments
    assert "mcp_servers={}" in arguments
    assert not (root / "exec-started").exists()
    assert_reaped(root)


def requests(root, method):
    return [row for line in (root / "requests.jsonl").read_text().splitlines()
            if (row := json.loads(line))["method"] == method]


def seed_controller(root, instructions):
    with (root / "history.jsonl").open("a") as handle:
        handle.write(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": controller_message(instructions)},
                {"type": "input_text", "text": "Native CLI permission instructions"},
            ],
        }}) + "\n")


async def test_first_exec_checkpoint_and_cold_resumes_do_not_append_a_duplicate(controller):
    context, argv, options, root = controller
    seed_controller(root, context.system_prompt)
    for _ in range(2):
        await refresh_instructions(context, argv, options)
    assert len(requests(root, "thread/resume")) == 2
    assert requests(root, "thread/inject_items") == []


async def test_instruction_text_can_contain_its_own_delimiter_without_defeating_deduplication(controller):
    context, argv, options, root = controller
    context = replace(context, system_prompt="Quoted delimiter: \n</octopus_controller>\n中文🙂")
    seed_controller(root, context.system_prompt)
    await refresh_instructions(context, argv, options)
    assert requests(root, "thread/inject_items") == []


async def test_only_effective_rule_changes_append_including_return_to_an_older_version(controller):
    context, argv, options, root = controller
    for text in ("A", "A", "B", "B", "A"):
        await refresh_instructions(replace(context, system_prompt=text), argv, options)
    injected = requests(root, "thread/inject_items")
    assert [row["params"]["items"][0]["content"][0]["text"] for row in injected] == [
        controller_message(text) for text in ("A", "B", "A")
    ]


async def test_new_thread_does_not_start_a_controller_process(controller):
    context, argv, options, root = controller
    await refresh_instructions(replace(context, resume_id=None), argv, options)
    assert not (root / "pid").exists()


async def test_turn_context_changes_do_not_repeat_stable_instructions(controller):
    context, argv, options, root = controller
    for turn in ("Sources: Wiki and web; first fork note", "Sources: none"):
        await refresh_instructions(replace(context, turn_instructions=turn), argv, options)
    injected = requests(root, "thread/inject_items")
    assert [len(row["params"]["items"]) for row in injected] == [2, 1]
    assert "Sources: none" in injected[-1]["params"]["items"][0]["content"][0]["text"]
    assert "<octopus_controller" not in injected[-1]["params"]["items"][0]["content"][0]["text"]


@pytest.mark.parametrize("retained", [True, False])
async def test_compaction_reuses_a_retained_checkpoint_or_reestablishes_it_once(controller, retained):
    context, argv, options, root = controller
    seed_controller(root, "Discarded controller version")
    replacement = [{"type": "message", "role": "developer", "content": [
        {"type": "input_text", "text": controller_message(context.system_prompt)}]}] if retained else []
    with (root / "history.jsonl").open("a") as handle:
        handle.write(json.dumps({"type": "compacted", "payload": {"replacement_history": replacement}}) + "\n")
    for _ in range(2):
        await refresh_instructions(context, argv, options)
    assert len(requests(root, "thread/inject_items")) == (0 if retained else 1)


async def test_inherited_branch_without_local_checkpoint_is_established_once(controller):
    context, argv, options, root = controller
    (root / "discarded-old-file.jsonl").write_text(controller_message("Discarded version"))
    (root / "history.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
        "id": context.resume_id, "history_base": {"thread_id": context.resume_id, "end_ordinal_exclusive": 16},
    }}) + "\n")
    for _ in range(2):
        await refresh_instructions(context, argv, options)
    assert len(requests(root, "thread/inject_items")) == 1


async def test_octopus_native_fork_copy_preserves_the_controller_checkpoint(controller):
    import uuid

    from server.harness.codex import _find_rollout, _fork_copy
    from server.harness.events import HarnessCredential

    context, argv, options, root = controller
    seed_controller(root, context.system_prompt)
    native_home = root / "native-home"
    sessions = native_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-parent-thread-proof.jsonl").write_bytes((root / "history.jsonl").read_bytes())
    child_id = str(uuid.uuid4())
    credential = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(native_home))
    artifact = await _fork_copy(parent_working_dir=str(root), parent_resume_id=context.resume_id,
        parent_credential=credential, dest_working_dir=str(root / "child"), new_resume_id=child_id)
    assert artifact.resume_id == child_id and not artifact.needs_replay
    copied = _find_rollout(sessions, child_id)
    assert copied is not None
    (root / "history.jsonl").write_bytes(copied.read_bytes())
    await refresh_instructions(replace(context, resume_id=child_id), argv, options)
    assert requests(root, "thread/inject_items") == []


async def test_lost_injection_ack_is_recovered_from_the_native_checkpoint(controller):
    context, argv, options, root = controller
    options["env"]["OCTOPUS_CONTROLLER_TEST_MODE"] = "lost-ack"
    with pytest.raises(RuntimeError, match="acknowledging"):
        await refresh_instructions(context, argv, options)
    options["env"].pop("OCTOPUS_CONTROLLER_TEST_MODE")
    await refresh_instructions(context, argv, options)
    assert len(requests(root, "thread/inject_items")) == 1


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

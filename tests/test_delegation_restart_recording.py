"""Manager operations stop on real driver failure and classify on cold reopen."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationManager
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.mcp_identity import revoke_run, scope_is_live, verify
from server.sessions import SessionManager
from server.users import UserManager
from tests.accounts import create_account
from tests.harness_profiles import session_turn_argv


async def _finished(manager, rec):
    async with asyncio.timeout(5):
        while rec.state == "running" or manager.has_pending_or_active_work(rec.delegation_id, rec.work_id):
            await asyncio.sleep(.01)


def seed_unfinished_round(path: Path, *, request: str, agents_dir: Path) -> dict:
    """Crash a real manager after input/partial text, without a terminal or close."""
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_delegation_restart_recording",
         str(path), "seed", request, str(agents_dir)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads(path.with_suffix(".unfinished.json").read_text())
    try:
        process = os.pidfd_open(saved["model_pid"])
    except ProcessLookupError:
        pass
    else:
        try:
            exited = select.poll()
            exited.register(process, select.POLLIN)
            assert exited.poll(1000), "the seed's original scripted child must exit after its manager"
        finally:
            os.close(process)
    return saved


async def _seed_running_round(path: Path, request: str, agents_dir: Path) -> None:
    from server.config import settings

    settings.agents_dir = str(agents_dir)
    assert os.environ.get("OCTOPUS_USERS_ROOT") and os.environ.get("OCTOPUS_MASTER_KEY_FILE"), "Seed requires the isolated test environment"
    settings.default_working_dir = str(path.parent)
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[path.parent])
    manager = SessionManager()
    await manager.initialize(database)
    parent = await manager.create_session(
        (await database.get_system_agent())["id"], "Crash seed caller", str(path.parent), backend="codex",
    )
    await manager.interrupt(parent.id)
    await AgentManager(database).create_agent(name="Worker", backend="codex", user_id=owner["id"])
    ready = asyncio.Event()
    model_pid = path.with_suffix(".model-pid")
    body = "Original unfinished review prefix"
    code = (
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.stdin.readline()\n"
        f"Path({str(model_pid)!r}).write_text(str(os.getpid()))\n"
        "print(json.dumps({'type':'thread.started','thread_id':'unfinished-round'}),flush=True)\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'id':'prefix','type':'agent_message','text':{body!r}}}}}),flush=True)\n"
        f"while os.getppid()=={os.getpid()}: time.sleep(.005)\n"
    )
    harness = get_harness("codex")
    harness.profile = replace(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        stdin_mode=StdinMode.STREAM_JSON, can_steer=True, reusable=True,
        build_turn_argv=lambda context: ([sys.executable, "-u", "-c", code], {"cwd": context.working_dir}),
    )

    async def observe(event):
        if event["type"] == "assistant_text" and event.get("content") == body:
            ready.set()

    manager.on_broadcast("unfinished-seed-prefix", observe)
    delegations = DelegationManager()
    delegations.bind(manager, database)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request=request,
    )
    await asyncio.wait_for(ready.wait(), 5)
    stored = json.loads(await database.load_delegation_state(
        rec.delegation_id, parent_session_id=parent.id, user_id=parent.user_id,
    ))
    assert stored["state"] == "running" and stored["work_id"] == rec.work_id
    rows = await database.load_messages(rec.delegation_id)
    original = next(row for row in rows if row["role"] == "user")
    request_id = original["tool_input"]["request_context"]["request_id"]
    assert await database.get_committed_terminal(rec.delegation_id, request_id) is None
    path.with_suffix(".unfinished.json").write_text(json.dumps({
        "parent_id": parent.id, "child_id": rec.delegation_id, "work_id": rec.work_id,
        "request_id": request_id, "request": request, "input": original["content"],
        "input_seq": original["seq"], "body": body, "model_pid": int(model_pid.read_text()),
    }))
    os._exit(0)


async def _crash_scenario(directory: Path, mode: str, applied: bool) -> None:
    """Run only scripted Python children; the failing seam is the DB driver."""
    if mode == "close":
        from tests.process_shutdown import shorten_harness_shutdown

        # This fresh process must exit through the real failing DB driver.
        # Its held CLI still receives real signals, with a test-only grace.
        shutdown_clock = pytest.MonkeyPatch()
        shorten_harness_shutdown(shutdown_clock)
    os.environ["OCTOPUS_AGENTS_DIR"] = str(directory / "agents")
    from server.config import settings

    settings.agents_dir = str(directory / "agents")
    assert os.environ.get("OCTOPUS_USERS_ROOT") and os.environ.get("OCTOPUS_MASTER_KEY_FILE"), "Crash scenario requires the isolated test environment"
    path = directory / "rounds.db"
    unfinished = seed_unfinished_round(
        path, request="held before cold restore", agents_dir=directory / "agents",
    ) if mode == "restore" else None
    starts: asyncio.Queue = asyncio.Queue()
    release = directory / "release-outer"

    def argv(context):
        starts.put_nowait(context)
        prompt = context.prompt.rsplit("\n---\n", 1)[-1]
        code = f"import json,os,time\nfrom pathlib import Path\nowner={os.getpid()}\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'command-process-thread'}),flush=True)\n"
        if "outer gate" in prompt:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'waiting for helper'}}),flush=True)\n"
            code += f"while os.getppid()==owner and not Path({str(release)!r}).exists(): time.sleep(.01)\n"
            code += "if os.getppid()!=owner: raise SystemExit\n"
        elif "held" in prompt:
            code += "while os.getppid()==owner: time.sleep(.01)\nraise SystemExit\n"
        else:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'acknowledged review answer'}}),flush=True)\n"
        if mode == "nested_failure" and "outer gate" in prompt:
            code += "print(json.dumps({'type':'turn.failed','error':{'message':'authentication failed'}}),flush=True)\n"
        else:
            code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    harness.profile = replace(harness.profile, build_turn_argv=session_turn_argv(harness.profile, argv), prepare_resume=None, activity_observer=None)
    database = Database(str(path))
    await database.initialize()
    if unfinished is None:
        await create_account(database, extra_roots=[directory])
    else:
        deps.set_user_manager(UserManager(database))
    manager = SessionManager()
    await manager.initialize(database)
    parent = manager.get_session(unfinished["parent_id"]) if unfinished else await manager.create_session(
        (await database.get_system_agent())["id"], "Caller", str(directory), backend="codex",
    )
    await manager.interrupt(parent.id)
    if unfinished is None:
        await AgentManager(database).create_agent(name="Worker", backend="codex", user_id=parent.user_id)
        await AgentManager(database).create_agent(name="Leaf", backend="codex", user_id=parent.user_id)
    delegations = DelegationManager()
    delegations.bind(manager, database)
    metadata = {"parent_id": parent.id}
    original = None
    outer = None
    scope = None

    if mode in {"followup", "rollback", "nested_setup", "nested_failure", "nested_defer"}:
        if mode != "followup":
            outer = await delegations.start_delegation(
                parent_session_id=parent.id, agent_name="Worker", request="outer gate review",
            )
            context = await asyncio.wait_for(starts.get(), 5)
            scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
            async with asyncio.timeout(5):
                while not scope_is_live(scope):
                    await asyncio.sleep(.01)
            metadata["outer_id"] = outer.delegation_id
        if mode in {"followup", "rollback"}:
            original = await delegations.start_delegation(
                parent_session_id=outer.delegation_id if outer else parent.id,
                agent_name="Leaf" if outer else "Worker", request="first confirmed review", scope=scope,
            )
            await asyncio.wait_for(starts.get(), 5)
            await _finished(manager, original)
            await asyncio.wait_for(delegations.confirm_recording(), 5)
            metadata.update(original_work=original.work_id, original_request=original.request)

    if unfinished is not None:
        metadata["unfinished"] = unfinished

    execute, commit = database.conn.execute, database.conn.commit
    armed = False
    revoke_after_commit = False

    async def execute_round(sql, parameters=()):
        nonlocal armed, revoke_after_commit
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE sessions SET delegation_state = "):
            proposed = json.loads(parameters[0])
            previous = json.loads(parameters[-1]) if parameters[-1] is not None else None
            if mode == "nested_setup" and proposed["parent_work_id"] == outer.work_id:
                metadata.update(leaf_id=parameters[1], leaf_work=proposed["work_id"])
            new_followup = original is not None and proposed["state"] == "running" and proposed["work_id"] != original.work_id
            trigger = (
                (mode == "initial" and previous is None)
                or (mode == "terminal" and proposed["state"] == "completed")
                or (mode in {"closed", "cancel"} and proposed["state"] == "cancelled")
                or (mode == "followup" and new_followup)
                or (mode == "rollback" and previous is not None
                    and previous["work_id"] != proposed["work_id"]
                    and proposed["work_id"] == original.work_id)
                or (mode == "nested_setup" and parameters[1] == outer.delegation_id and proposed["awaiting_sub_relay"])
                or (mode == "nested_failure" and parameters[1] == outer.delegation_id and proposed["state"] == "failed")
                or (mode == "nested_defer" and parameters[1] == outer.delegation_id and proposed["deferred_request_id"] is not None)
                or (mode in {"close", "restore"} and proposed["state"] == "interrupted")
            )
            if mode == "rollback" and new_followup:
                revoke_after_commit = True
            if trigger:
                metadata.update(
                    child_id=parameters[1], work_id=proposed["work_id"],
                    previous=previous, proposed=proposed,
                )
                (directory / "identity.json").write_text(json.dumps(metadata))
                armed = True
        return cursor

    async def unconfirmed_commit():
        nonlocal revoke_after_commit
        if armed:
            if applied:
                await commit()
            raise OSError("real driver lost the transaction-end acknowledgement")
        await commit()
        if revoke_after_commit:
            revoke_run(scope.run_lease_id)
            revoke_after_commit = False

    database.conn.execute = execute_round
    database.conn.commit = unconfirmed_commit

    if mode == "closed":
        manager.close_admission()
    if mode in {"initial", "terminal", "closed", "cancel", "close"}:
        rec = await delegations.start_delegation(
            parent_session_id=parent.id, agent_name="Worker",
            request="held before shutdown" if mode in {"close", "cancel"} else "review operation",
        )
        if mode == "close":
            await asyncio.wait_for(starts.get(), 5)
            await database.close()
        elif mode == "cancel":
            await asyncio.wait_for(starts.get(), 5)
            await delegations.cancel_delegation(rec.delegation_id, reason="caller withdrew the review")
        elif mode == "terminal":
            await asyncio.wait_for(starts.get(), 5)
            await _finished(manager, rec)
    elif mode in {"followup", "rollback"}:
        await delegations.follow_up_delegation(
            parent_session_id=original.parent_session_id, delegation_id=original.delegation_id,
            request="held new review never admitted", scope=scope,
        )
    elif mode in {"nested_setup", "nested_failure", "nested_defer"}:
        await delegations.start_delegation(
            parent_session_id=outer.delegation_id, agent_name="Leaf", request="held helper", scope=scope,
        )
        await asyncio.wait_for(starts.get(), 5)
        release.touch()
        await _finished(manager, outer)
    elif mode == "restore":
        await delegations.restore()
    (directory / "continued-after-failure").touch()
    await database.close()


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("mode", [
    "initial", "terminal", "closed", "cancel", "followup", "rollback",
    "nested_setup", "nested_failure", "nested_defer", "close", "restore",
])
async def test_complete_manager_command_fails_stop_then_cold_reopen_preserves_facts(tmp_path, mode, applied):
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_delegation_restart_recording", str(tmp_path), mode, str(applied)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 70, result.stderr
    assert not (tmp_path / "continued-after-failure").exists()
    metadata = json.loads((tmp_path / "identity.json").read_text())
    if mode == "rollback":
        assert metadata["previous"]["work_id"] != metadata["original_work"]
        assert metadata["proposed"]["work_id"] == metadata["original_work"]
    # Restore twice: classification is durable and neither lifecycle replays
    # the child or automatically relays an old result into the parent.
    for _ in range(2):
        database = Database(str(tmp_path / "rounds.db"))
        await database.initialize()
        deps.set_user_manager(UserManager(database))
        manager = SessionManager()
        await manager.initialize(database)
        starts = []
        harness = get_harness("codex")
        previous_profile = harness.profile

        def unexpected_start(context, starts=starts):
            starts.append(context)
            raise AssertionError("cold recovery must not run the child")

        harness.profile = replace(previous_profile, build_turn_argv=unexpected_start)
        delegations = DelegationManager()
        delegations.bind(manager, database)
        try:
            await delegations.restore()
            rec = delegations.get_delegation(metadata["child_id"])
            if mode == "initial" and not applied:
                assert rec is None
            else:
                assert rec is not None
                expected_work = metadata["work_id"]
                expected_state = "interrupted"
                if mode == "terminal":
                    expected_state = "completed"
                elif mode in {"closed", "cancel"} and applied:
                    expected_state = "cancelled"
                elif mode == "followup" and not applied:
                    expected_work, expected_state = metadata["original_work"], "completed"
                elif mode == "rollback":
                    expected_work = metadata["proposed" if applied else "previous"]["work_id"]
                    expected_state = "completed" if applied else "interrupted"
                elif mode == "nested_failure":
                    expected_state = "failed"
                assert rec.work_id == expected_work and rec.state == expected_state
                assert rec.to_public_dict()["recording_state"] == "committed"
                if expected_state == "completed":
                    messages = await database.load_messages(rec.delegation_id)
                    assert any(row["content"] == "acknowledged review answer" for row in messages)
                if mode == "nested_failure":
                    assert "authentication failed" in rec.error
                if mode == "cancel" and applied:
                    assert rec.error == "caller withdrew the review"
                if mode == "nested_setup":
                    leaf = delegations.get_delegation(metadata["leaf_id"])
                    assert leaf.work_id == metadata["leaf_work"] and leaf.state == "interrupted"
                    assert await database.load_messages(leaf.delegation_id) == []
                if mode in {"terminal", "nested_failure", "close", "nested_defer"}:
                    messages = await database.load_messages(rec.delegation_id)
                    terminal = [row["content"] for row in messages if row["type"] == "result"]
                    assert len(terminal) == 1
                    expected = "failed" if mode == "nested_failure" else "interrupted" if mode == "close" else "success"
                    assert terminal[0]["status"] == expected
                if mode == "restore":
                    seed = metadata["unfinished"]
                    messages = await database.load_messages(rec.delegation_id)
                    original = next(row for row in messages if row["role"] == "user")
                    assert original["content"] == seed["input"] and original["seq"] == seed["input_seq"]
                    assert original["tool_input"]["request_context"]["request_id"] == seed["request_id"]
                    assert rec.work_id == seed["work_id"] and rec.request == seed["request"]
                    assert any(row["content"] == seed["body"] for row in messages)
                    assert not [row for row in messages if row["type"] == "result"]
                    assert await database.get_committed_terminal(rec.delegation_id, seed["request_id"]) is None
            assert starts == []
            assert manager.dispatch_snapshot(metadata["parent_id"])["items"] == []
            assert await database.load_messages(metadata["parent_id"]) == []
            await delegations.confirm_recording()
        finally:
            harness.profile = previous_profile
            delegations.shutdown()
            await database.close()


if __name__ == "__main__":
    if sys.argv[2] == "seed":
        asyncio.run(_seed_running_round(Path(sys.argv[1]), sys.argv[3], Path(sys.argv[4])))
    else:
        asyncio.run(_crash_scenario(Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "True"))

"""Retirement completes when final spawn authorization never submits an OS process."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.harness import (
    EventParser,
    Harness,
    HarnessEvent,
    ParseOutput,
    RunConfig,
    RuntimeProfile,
    StdinMode,
    get_harness,
)
from server.session_manager import session_manager
from server.workspace import paths_for
from tests import test_live_account_authorization as live_authorization

accounts = live_authorization.accounts
client = live_authorization.client


class _Parser(EventParser):
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        return ParseOutput(
            events=[HarnessEvent(type=obj["type"], raw=obj)],
            end_of_stream=obj["type"] == "result",
        )


class _Observer:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.invalidated = asyncio.Event()

    def observe_stdout(self, obj: dict[str, Any]) -> None:
        pass

    def consume_stderr(self, line: str) -> bool:
        return False

    async def start(self) -> None:
        self.started = True

    def invalidate(self) -> None:
        self.invalidated.set()

    async def close(self) -> None:
        self.closed = True


def _profile(model: Path, marker: Path, observer: _Observer) -> RuntimeProfile:
    return RuntimeProfile(
        backend="authorization-test",
        binary=sys.executable,
        tools_prompt="",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(marker)],
            {"cwd": context.working_dir},
        ),
        new_event_parser=_Parser,
        build_oneshot_argv=lambda context: ([sys.executable], {}),
        parse_oneshot_stdout=lambda stdout: stdout,
        activity_observer=lambda *args, **kwargs: observer,
    )


@pytest.mark.parametrize("cancellation", ["stop", "start-waiter", "authorization"])
async def test_stop_during_spawn_authorization_closes_observer_without_spawning(
    tmp_path, monkeypatch, cancellation,
):
    entered, release = asyncio.Event(), asyncio.Event()
    marker = tmp_path / "model-started"
    model = tmp_path / "model.py"
    model.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('started')\n"
        "print('{\"type\":\"result\"}', flush=True)\n"
    )
    observer = _Observer()
    dispatched = []
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        if str(model) in argv:
            dispatched.append(argv)
        process = await original_spawn(*argv, **kwargs)
        if str(model) in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)

    async def authorize_spawn():
        entered.set()
        await release.wait()
        if cancellation == "authorization":
            raise asyncio.CancelledError

    run = Harness(_profile(model, marker, observer)).create_run(
        RunConfig(authorize_spawn=authorize_spawn),
    )
    run.bind_activity(lambda timestamp: None)
    starting = asyncio.create_task(run.start("prompt", str(tmp_path)))
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert observer.started
        if cancellation == "start-waiter":
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        elif cancellation == "authorization":
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
        stopping = asyncio.create_task(run.stop())
        await asyncio.wait_for(observer.invalidated.wait(), 2)
        if cancellation != "authorization":
            assert not stopping.done()
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(starting, stopping, return_exceptions=True), 2,
        )
        assert dispatched == [], "Retired authorization submitted an OS spawn."
        assert not marker.exists()
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None, (
            f"Stop must confirm retirement after a pre-spawn cancellation; "
            f"got {results[1]!r}, observer.closed={observer.closed}."
        )
        assert observer.closed
        assert [event async for event in run.stream()] == []
        await run.stop()
    finally:
        release.set()
        await asyncio.gather(starting, *([stopping] if stopping else []), return_exceptions=True)
        await asyncio.gather(run.stop(), return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_session_stopped_during_spawn_authorization_can_run_again_without_holding_quota(
    accounts, tmp_path, monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()
    marker = tmp_path / "model-starts.jsonl"
    model = tmp_path / "session_model.py"
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as record:\n"
        "    record.write(json.dumps({'prompt':sys.argv[2]})+'\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'authorization-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'reply','type':'agent_message','text':'A fresh request succeeded.'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )
    observers = []

    def observer_factory(*args, **kwargs):
        observer = _Observer()
        observers.append(observer)
        return observer

    def argv(context):
        return [sys.executable, "-u", str(model), str(marker), context.prompt], {
            "cwd": context.working_dir,
        }

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, build_turn_argv=argv,
        activity_observer=observer_factory, prepare_resume=None,
        stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
    ))
    original_create_run = harness.create_run
    gated = False

    def create_run(config=None):
        nonlocal gated
        if not gated:
            gated = True
            assert config is not None and config.authorize_spawn is not None
            original_authorization = config.authorize_spawn

            async def authorize_spawn():
                entered.set()
                await release.wait()
                await original_authorization()

            config = replace(config, authorize_spawn=authorize_spawn)
        return original_create_run(config)

    # Gate the public RunConfig callback while preserving real owner authorization.
    monkeypatch.setattr(harness, "create_run", create_run)
    monkeypatch.setattr(settings, "max_concurrent_turns_per_user", 1)
    dispatched, processes = [], []
    original_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        if str(model) in argv:
            dispatched.append(argv)
        process = await original_spawn(*argv, **kwargs)
        if str(model) in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    owner = accounts["owner"]["id"]
    agent = await AgentManager(accounts["db"]).create_agent(
        name="Authorization retirement", user_id=owner, backend="codex",
    )
    session = await session_manager.create_session(
        agent["id"], "Stop final authorization", str(paths_for(owner).workspace), backend="codex",
    )
    peer = await session_manager.create_session(
        agent["id"], "Reuse the released quota", str(paths_for(owner).workspace), backend="codex",
    )

    async def wait_for_quota_release():
        while session_manager.running_turns_for(owner):
            await asyncio.sleep(0.005)

    try:
        first = await session_manager.start_message(session.id, "Stop me before the OS spawn.")
        assert first.accepted
        await asyncio.wait_for(entered.wait(), 3)
        assert observers[0].started
        assert session_manager.running_turns_for(owner) == 1
        assert await session_manager.interrupt(session.id)
        await asyncio.wait_for(observers[0].invalidated.wait(), 2)
        release.set()
        assert (await asyncio.wait_for(first.wait(), 3)).outcome.status == "interrupted"
        await asyncio.wait_for(wait_for_quota_release(), 3)
        assert observers[0].closed
        assert dispatched == []
        assert not marker.exists()

        next_prompt = "A new request in the interrupted session."
        next_receipt = await session_manager.start_message(session.id, next_prompt)
        assert next_receipt.accepted
        assert (await asyncio.wait_for(next_receipt.wait(), 5)).outcome.status == "success"
        await asyncio.wait_for(wait_for_quota_release(), 3)

        peer_prompt = "Another session may use the only account slot."
        peer_receipt = await session_manager.start_message(peer.id, peer_prompt)
        assert peer_receipt.accepted
        assert (await asyncio.wait_for(peer_receipt.wait(), 5)).outcome.status == "success"
        await asyncio.wait_for(wait_for_quota_release(), 3)
        assert [json.loads(line) for line in marker.read_text().splitlines()] == [
            {"prompt": next_prompt}, {"prompt": peer_prompt},
        ]
        assert len(dispatched) == 2
        assert all(observer.started and observer.closed for observer in observers)
    finally:
        release.set()
        await asyncio.gather(session_manager.drain_turns(), return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()

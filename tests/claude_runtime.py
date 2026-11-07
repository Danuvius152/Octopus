"""Original Owner/SQLite/Harness with a controlled stream-json process."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from tests._fixtures.jsonl_frames import read_frames
from tests.accounts import create_account

SID = "exact-claude-session"
MODEL = "deepseek-flash"
PEER = Path(__file__).parent / "_fixtures" / "claude_context_peer.py"


@pytest.fixture
async def claude_runtime(tmp_path, monkeypatch):
    harness = get_harness("claude-code")
    render = harness.profile.build_turn_argv
    starts, events = [], []

    def argv(context):
        starts.append(context)
        arguments, kwargs = render(context)
        return [sys.executable, "-u", str(PEER), str(tmp_path), *arguments[1:]], kwargs

    monkeypatch.setattr(harness, "profile", replace(harness.profile,
        binary=sys.executable, build_turn_argv=argv))
    database = Database(str(tmp_path / "product.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Claude context", str(tmp_path), backend="claude-code")

    async def observe(event):
        events.append(event)

    manager.on_broadcast("claude-context", observe)
    try:
        yield manager, database, session, starts, events
    finally:
        await manager.stop_all_held_processes()
        await database.close()


async def inputs(root, count):
    async with asyncio.timeout(3):
        while len(values := read_frames(root / "claude-inputs.jsonl")) < count:
            await asyncio.sleep(.01)
    return values


def inject(root, *frames):
    with (root / "claude-injections.jsonl").open("a") as handle:
        for frame in frames:
            handle.write(json.dumps(frame) + "\n")

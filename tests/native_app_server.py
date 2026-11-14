"""Controlled native stdio runtime and frames; no test-case module dependencies."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from server.database import Database
from server.harness import codex_app_server, get_harness
from server.harness.profile import TurnContext
from server.sessions import Session, SessionManager
from server.sessions.client_submissions import submit_client_message
from tests._fixtures.jsonl_frames import read_frames
from tests.accounts import create_account

PEER = Path(__file__).parent / "_fixtures" / "fake_app_server.py"
ROOT = "exact-native-thread"


class NativeRuntime(NamedTuple):
    manager: SessionManager
    database: Database
    session: Session
    mode: list[str]
    starts: list[TurnContext]
    events: list[dict]


@pytest.fixture
async def native_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("server.harness.codex_rpc.RPC_TIMEOUT", 0.5)
    mode, starts, events = ["normal"], [], []

    def argv(context):
        starts.append(context)
        return [sys.executable, "-u", str(PEER), str(tmp_path), mode[0]], {
            "cwd": context.working_dir, "env": {**os.environ, "CODEX_HOME": str(tmp_path / "codex-home")},
        }

    monkeypatch.setattr(codex_app_server, "build_argv", argv)
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(codex_app_server.profile(harness.profile),
        build_turn_argv=lambda context: codex_app_server.build_argv(context)))
    database = Database(str(tmp_path / "product.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Native input", str(tmp_path), backend="codex")

    async def observe(event):
        events.append(event)

    manager.on_broadcast("native-input-tests", observe)
    try:
        yield NativeRuntime(manager, database, session, mode, starts, events)
    finally:
        owner = manager._session_owners.get(id(session))
        if owner is None or not owner.failed:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
        # Failed owners reject new commands; close joins their original
        # registered resource cleanup instead of submitting another interrupt.
        await manager.close_session_owners()
        await database.close()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


def wire_frames(root, *, finished=False):
    return read_frames(root / "native-frames.jsonl", finished=finished)


def inject(root, *frames):
    with (root / "native-injections.jsonl").open("a") as handle:
        for frame in frames:
            handle.write(json.dumps(frame) + "\n")


def terminal(status="completed"):
    return {"method": "turn/completed", "params": {"threadId": "exact-native-thread",
        "turn": {"id": "turn-1", "status": status}}}


def question_request(identifier):
    return {"id": identifier, "method": "item/tool/requestUserInput", "params": {
        "threadId": "exact-native-thread", "turnId": "turn-1", "itemId": f"ask-{identifier}", "isBlocking": True,
        "questions": [{"id": "q", "header": "Choice", "question": "Choose"}]}}


def item(thread, turn, identifier, text):
    return {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
        "item": {"id": identifier, "type": "agentMessage", "text": text, "phase": "final_answer"}}}


def child_start(thread="child", turn="child-turn"):
    return {"method": "turn/started", "params": {"threadId": thread,
        "turn": {"id": turn, "rootTurnId": "turn-1", "status": "inProgress"}}}


def child_end(thread="child", turn="child-turn"):
    return {"method": "turn/completed", "params": {"threadId": thread,
        "turn": {"id": turn, "rootTurnId": "turn-1", "status": "completed"}}}


async def start_output(runtime):
    manager, _, session, mode, _, _ = runtime
    mode[0] = "control_output"
    receipt = await submit_client_message(manager, session.id, "Native output", [], "output", wiki_mode=None)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    return receipt


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not await predicate():
            await asyncio.sleep(.005)


async def active_root(runtime, tmp_path):
    manager, database, session, mode, starts, events = runtime
    if mode[0] == "normal":
        mode[0] = "steer_wait"
    first = await submit_client_message(manager, session.id, "Wait for the helper", [], "root", wiki_mode=None)

    async def waiting():
        return any(event.get("type") == "tool_use" for event in events)

    await wait_until(waiting)
    return first

"""Native helper endings through real subprocesses and public session surfaces."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from server import deps
from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.harness.run import HarnessRun
from server.routers.sessions import router
from server.sessions import SessionManager


@pytest.fixture
async def native_runtime(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "native.db"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Native helper", str(tmp_path), backend="codex")
    events = []
    helper_seen = asyncio.Event()

    async def observe(event):
        events.append(event)
        if event.get("type") == "subagent":
            helper_seen.set()

    manager.on_broadcast("native-observer", observe)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[deps.get_session_manager] = lambda: manager
    monkeypatch.setattr(settings, "auth_token", "native-test-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
        headers={"Authorization": "Bearer native-test-token"},
    ) as client:
        try:
            yield manager, session, client, events, helper_seen
        finally:
            await manager.drain_turns()
            await database.close()


def native_cli(monkeypatch, *, hold=False, child_status="running", parent_error=False, recover_after_timeout=False):
    child = "01a135af-58f2-7fe1-b734-30e71d5bb26c"
    events = [
        {"type": "thread.started", "thread_id": "native-parent"},
        {"type": "item.started", "item": {
            "id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent", "prompt": "Review the file",
        }},
        {"type": "item.completed", "item": {
            "id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
            "receiver_thread_ids": [child], "agents_states": {
                child: {"status": child_status, "message": "Partial evidence remains readable"},
            },
        }},
    ]
    script = "import json,time\n"
    for event in events:
        script += f"print({json.dumps(json.dumps(event))},flush=True)\n"
    if hold:
        script += "time.sleep(60)\n"
    elif parent_error:
        script += "print(json.dumps({'type':'turn.failed','error':{'message':'authentication failed'}}),flush=True)\n"
    else:
        script += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"

    starts = []

    def argv(context):
        starts.append(context.prompt)
        current_script = script
        if recover_after_timeout and len(starts) > 1:
            completed = {"type": "item.completed", "item": {
                "id": "item_1", "type": "collab_tool_call", "tool": "wait",
                "receiver_thread_ids": [child], "agents_states": {
                    child: {"status": "completed", "message": "Recovered review"},
                },
            }}
            tail = f"print({json.dumps(json.dumps(completed))},flush=True)\n"
            tail += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
            current_script = script.replace("time.sleep(60)\n", tail)
        return [sys.executable, "-u", "-c", current_script], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, activity_observer=None,
    ))
    return starts


async def test_successful_parent_ends_unfinished_helper_without_reporting_helper_failure(native_runtime, monkeypatch):
    manager, session, client, events, _ = native_runtime
    native_cli(monkeypatch)
    receipt = await manager.start_message(session.id, "Review once")
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "success"
    response = await client.get(f"/api/sessions/{session.id}")
    assert response.status_code == 200
    helper = response.json()["subagents"][0]
    assert helper["status"] == "interrupted"
    assert helper["stop_reason"] == "parent_done"
    assert helper["tool_call_id"]
    assert helper["summary"] == "Partial evidence remains readable"
    assert helper["steps"]
    async with asyncio.timeout(3):
        while not any(event.get("type") == "subagent" and event.get("status") == "interrupted" for event in events):
            await asyncio.sleep(.01)
    live = next(event for event in reversed(events) if event.get("type") == "subagent")
    assert live["tool_call_id"] == helper["tool_call_id"]
    assert live["stop_reason"] == helper["stop_reason"]
    assert live["summary"] == helper["summary"]


async def test_selected_helper_ending_reaches_the_browser_while_cli_cleanup_is_blocked(native_runtime, monkeypatch):
    manager, session, client, events, _ = native_runtime
    native_cli(monkeypatch, hold=True)
    entered, release, reported = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = HarnessRun.stop

    async def delayed_stop(run):
        entered.set()
        await release.wait()
        await stop(run)

    async def observe(event):
        if event.get("type") == "subagent" and event.get("stop_reason") == "user_stop":
            reported.set()

    monkeypatch.setattr(HarnessRun, "stop", delayed_stop)
    manager.on_broadcast("observe-blocked-cleanup", observe)
    receipt = await manager.start_message(session.id, "Stop this helper")
    async with asyncio.timeout(3):
        while not any(event.get("type") == "subagent" and event.get("summary") for event in events):
            await asyncio.sleep(.01)
    stopping = asyncio.create_task(manager.interrupt(session.id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await client.get(f"/api/sessions/{session.id}")
        assert response.json()["subagents"][0]["status"] == "interrupted"
        await asyncio.wait_for(reported.wait(), .5)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.wait_for(stopping, 6)
        await asyncio.wait_for(receipt.wait(), 6)


@pytest.mark.parametrize("action,reason", [("stop", "user_stop"), ("shutdown", "service_shutdown")])
async def test_native_helper_reports_who_stopped_its_parent(native_runtime, monkeypatch, action, reason):
    manager, session, client, events, _ = native_runtime
    native_cli(monkeypatch, hold=True)
    receipt = await manager.start_message(session.id, "Review until stopped")
    async with asyncio.timeout(3):
        while not any(event.get("type") == "subagent" and event.get("summary") for event in events):
            await asyncio.sleep(.01)
    if action == "stop":
        await manager.interrupt(session.id)
    else:
        await manager.drain_turns()
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.status == "interrupted"
    response = await client.get(f"/api/sessions/{session.id}")
    helper = response.json()["subagents"][0]
    assert helper["status"] == "interrupted"
    assert helper["stop_reason"] == reason
    assert helper["tool_call_id"]
    assert helper["summary"] == "Partial evidence remains readable"
    async with asyncio.timeout(3):
        while not any(event.get("type") == "subagent" and event.get("stop_reason") == reason for event in events):
            await asyncio.sleep(.01)
    assert manager.dispatch_snapshot(session.id)["items"] == []


async def test_parent_failure_interrupts_helper_without_fabricating_a_helper_error(native_runtime, monkeypatch):
    manager, session, client, events, _ = native_runtime
    native_cli(monkeypatch, parent_error=True)
    receipt = await manager.start_message(session.id, "Review before parent failure")
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "failed"
    response = await client.get(f"/api/sessions/{session.id}")
    helper = response.json()["subagents"][0]
    assert helper["status"] == "interrupted"
    assert helper["stop_reason"] == "parent_error"
    assert helper["summary"] == "Partial evidence remains readable"
    assert any(event.get("type") == "subagent" and event.get("stop_reason") == "parent_error" for event in events)


@pytest.mark.parametrize("child_status", ["completed", "failed"])
async def test_parent_retirement_preserves_a_confirmed_helper_outcome(native_runtime, monkeypatch, child_status):
    manager, session, client, events, _ = native_runtime
    native_cli(monkeypatch, child_status=child_status)
    receipt = await manager.start_message(session.id, "Review a confirmed result")
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "success"
    response = await client.get(f"/api/sessions/{session.id}")
    helper = response.json()["subagents"][0]
    assert helper["status"] == child_status
    assert helper["stop_reason"] is None
    assert helper["tool_call_id"]
    assert helper["summary"] == "Partial evidence remains readable"
    assert not any(event.get("type") == "subagent" and event.get("status") == "interrupted" for event in events)


async def test_idle_recovery_keeps_ended_helper_separate_from_the_new_attempt(native_runtime, monkeypatch):
    manager, session, client, events, _ = native_runtime
    starts = native_cli(monkeypatch, hold=True, recover_after_timeout=True)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 1)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 1)
    receipt = await manager.start_message(session.id, "Review with one idle recovery")
    completion = await asyncio.wait_for(receipt.wait(), 8)
    assert completion.outcome.status == "success"
    assert len(starts) == 2
    response = await client.get(f"/api/sessions/{session.id}")
    ended, recovered = response.json()["subagents"]
    assert ended["status"] == "interrupted"
    assert ended["stop_reason"] == "parent_timeout"
    assert ended["summary"] == "Partial evidence remains readable"
    assert recovered["status"] == "completed"
    assert recovered["summary"] == "Recovered review"
    assert ended["tool_call_id"] != recovered["tool_call_id"]
    assert any(event.get("type") == "subagent" and event.get("stop_reason") == "parent_timeout" for event in events)

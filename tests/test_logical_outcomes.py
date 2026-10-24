"""Request outcomes through public receipts, the real parser and history API."""

import asyncio
import json
import sys
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.routers.sessions import router
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.fixture
async def runtime(monkeypatch):
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db)
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Outcomes", backend="codex")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + bearer},
        ) as client:
            yield manager, session, client
    finally:
        await db.close()


def scripted_cli(monkeypatch, scripts):
    """Replace only the external CLI command; keep the real run engine."""
    prompts = []

    def argv(ctx):
        index = min(len(prompts), len(scripts) - 1)
        prompts.append(ctx.prompt)
        events = scripts[index]
        if isinstance(events, Exception):
            raise events
        code = "import json,time,sys\n"
        for event in events:
            if event is None:
                code += "time.sleep(30)\n"
                continue
            if "_stderr" in event:
                code += f"sys.stderr.write({json.dumps(event['_stderr'])})\n"
                continue
            code += f"print({json.dumps(json.dumps(event))},flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": ctx.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    return prompts


def success(text="answer"):
    return [
        {"type": "thread.started", "thread_id": "saved-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {}},
    ]


async def drive(runtime, prompt="request"):
    manager, session, client = runtime
    events = []

    async def capture(event):
        events.append(event)

    manager.on_broadcast("outcomes-test", capture)
    receipt = await manager.start_message(session.id, prompt)
    await asyncio.wait_for(receipt.wait(), 10)
    response = await client.get(f"/api/sessions/{session.id}")
    assert response.status_code == 200
    return events, response.json()["messages"]


async def test_recovered_cli_notification_does_not_repeat_a_successful_request(runtime, monkeypatch):
    events = success()
    events.insert(1, {"type": "error", "message": "stream disconnected before completion"})
    prompts = scripted_cli(monkeypatch, [events])
    live, history = await drive(runtime)
    assert prompts == ["request"]
    assert len([e for e in live if e["type"] == "result"]) == 1
    assert len([m for m in history if m["type"] == "result"]) == 1
    assert not any(e.get("code") == "transient_retry" for e in live)


async def test_provider_failure_recovers_with_one_result_in_live_and_history(runtime, monkeypatch):
    failed = [
        {"type": "thread.started", "thread_id": "saved-thread"},
        {"type": "item.started", "item": {"type": "command_execution", "id": "tool-1", "command": "true"}},
        {"type": "turn.failed", "error": {"message": "stream disconnected", "codex_error_info": "server_overloaded"}},
    ]
    prompts = scripted_cli(monkeypatch, [failed, success("recovered")])
    live, history = await drive(runtime)
    assert len([m for m in history if m["type"] == "result"]) == 1
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1
    assert final[0]["content"]["status"] == "success"
    assert final[0]["content"]["recovery"]["transient"] == 1
    assert history[-1]["content"] == final[0]["content"]
    assert history[-1]["is_error"] is False
    assert prompts[0] == "request" and "Continue" in prompts[1]
    assert any(m["type"] == "recovery" for m in history)


async def test_stop_delivers_one_interrupted_result_without_success(runtime, monkeypatch):
    manager, session, client = runtime
    scripted_cli(monkeypatch, [[
        {"type": "thread.started", "thread_id": "saved-thread"},
        {"type": "item.started", "item": {"type": "command_execution", "id": "tool-1", "command": "true"}},
        None,
    ]])
    running = asyncio.Event()
    live = []

    async def capture(event):
        live.append(event)
        if event["type"] == "tool_use":
            running.set()

    manager.on_broadcast("stop-test", capture)
    receipt = await manager.start_message(session.id, "first")
    await asyncio.wait_for(running.wait(), 3)
    assert await manager.interrupt(session.id)
    await asyncio.wait_for(receipt.wait(), 5)
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1
    assert final[0]["content"]["status"] == "interrupted"
    assert final[0]["is_error"] is True
    history = (await client.get(f"/api/sessions/{session.id}")).json()["messages"]
    assert len([m for m in history if m["type"] == "result"]) == 1
    assert not any(m["type"] == "error" for m in history)


@pytest.mark.parametrize("error", [
    {"message": "Selected model is at capacity. Please try a different model."},
    {"message": "provider unavailable", "codex_error_info": "server_overloaded"},
])
async def test_capacity_failure_exhausts_two_retries_and_preserves_reason(runtime, monkeypatch, error):
    # This checks the retry count and final reason, not the backoff duration.
    monkeypatch.setattr(runtime[0], "_TRANSIENT_RETRY_BASE_DELAY", .01)
    prompts = scripted_cli(monkeypatch, [[{"type": "turn.failed", "error": error}]])
    live, history = await drive(runtime)
    final = [e for e in live if e["type"] == "result"]
    assert prompts == ["request", "request", "request"]
    assert len(final) == 1 and final[0]["is_error"] is True
    assert final[0]["content"]["status"] == "failed"
    assert final[0]["content"]["recovery"]["transient"] == 2
    assert error["message"] in final[0]["content"]["message"]
    assert final[0]["content"]["provider_code"] == error.get("codex_error_info")
    assert history[-1]["content"] == final[0]["content"]


async def test_diagnostics_are_safe_and_bounded_in_live_and_history(runtime, monkeypatch):
    raw = 'password="correct horse battery staple" Authorization: Bearer abcdefghijkl token=secret123 ' + "x" * 3000
    events = success()
    events.insert(1, {"type": "error", "message": raw})
    scripted_cli(monkeypatch, [events])
    live, history = await drive(runtime)
    diagnostic = next(e for e in live if e["type"] == "recovery")
    message = diagnostic["content"]["message"]
    assert "horse" not in message and "abcdefgh" not in message and "secret123" not in message
    assert "[redacted]" in message
    assert len(message) <= 2001
    assert next(m for m in history if m["type"] == "recovery")["content"] == diagnostic["content"]


async def test_cli_initialization_failure_has_one_safe_final_result(runtime, monkeypatch):
    scripted_cli(monkeypatch, [RuntimeError("Cannot start CLI: password=secret123")])
    live, history = await drive(runtime)
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["is_error"] is True
    assert final[0]["content"]["status"] == "failed"
    assert "Cannot start CLI" in final[0]["content"]["message"]
    assert "secret123" not in final[0]["content"]["message"]
    assert history[-1]["content"] == final[0]["content"]


@pytest.mark.parametrize("error", [
    {"message": "Authentication failed: 401 unauthorized", "codex_error_info": "unauthorized"},
    {"message": "stream disconnected", "codex_error_info": "usage_limit_reached"},
])
async def test_auth_and_quota_failures_do_not_retry(runtime, monkeypatch, error):
    prompts = scripted_cli(monkeypatch, [[{"type": "turn.failed", "error": error}]])
    live, history = await drive(runtime)
    assert prompts == ["request"]
    assert len([e for e in live if e["type"] == "result"]) == 1
    assert history[-1]["is_error"] is True
    assert history[-1]["content"]["recovery"]["transient"] == 0


async def test_quota_notification_without_a_terminal_event_does_not_retry(runtime, monkeypatch):
    prompts = scripted_cli(monkeypatch, [[{
        "type": "error", "message": "stream disconnected",
        "codex_error_info": "usage_limit_reached",
    }]])
    live, history = await drive(runtime)
    assert prompts == ["request"]
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["content"]["status"] == "failed"
    assert final[0]["content"]["provider_code"] == "usage_limit_reached"
    assert history[-1]["content"] == final[0]["content"]


async def test_terminal_provider_code_takes_priority_over_earlier_diagnostics(runtime, monkeypatch):
    failed = [
        {"type": "error", "message": "Earlier connection rejected: 401 unauthorized"},
        {"type": "turn.failed", "error": {"message": "provider overloaded", "codex_error_info": "server_overloaded"}},
    ]
    prompts = scripted_cli(monkeypatch, [failed, success()])
    live, history = await drive(runtime)
    assert prompts == ["request", "request"]
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["content"]["status"] == "success"
    assert history[-1]["content"]["recovery"]["transient"] == 1


async def test_cli_stream_ending_without_a_result_is_a_truthful_failure(runtime, monkeypatch):
    scripted_cli(monkeypatch, [[
        {"type": "item.completed", "item": {"type": "agent_message", "text": "partial answer"}},
        {"_stderr": "transport unexpectedly closed: token=secret123"},
    ]])
    live, history = await drive(runtime)
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["content"]["status"] == "failed"
    assert "transport unexpectedly closed" in final[0]["content"]["message"]
    assert "secret123" not in final[0]["content"]["message"]
    assert history[-1]["content"] == final[0]["content"]


async def test_stream_processing_exception_has_one_failed_result(runtime, monkeypatch):
    scripted_cli(monkeypatch, [[
        {"type": "item.completed", "item": {"type": "agent_message", "text": "partial answer"}},
        {"type": "error", "message": {"unexpected": "shape"}},
    ]])
    live, history = await drive(runtime)
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["content"]["status"] == "failed"
    assert final[0]["content"]["code"] == "backend_exception"
    assert history[-1]["content"] == final[0]["content"]


async def test_stop_during_recovery_preserves_snapshot_and_allows_a_fresh_request(runtime, monkeypatch):
    manager, session, client = runtime
    prompts = scripted_cli(monkeypatch, [[
        {"type": "turn.failed", "error": {"message": "provider overloaded", "codex_error_info": "server_overloaded"}},
    ], success()])
    retrying = asyncio.Event()
    live = []

    async def capture(event):
        live.append(event)
        if event.get("code") == "transient_retry":
            retrying.set()

    manager.on_broadcast("backoff-test", capture)
    receipt = await manager.start_message(session.id, "first")
    await asyncio.wait_for(retrying.wait(), 3)
    snapshot = (await client.get(f"/api/sessions/{session.id}")).json()["messages"]
    assert not any(m["type"] == "result" for m in snapshot)
    marker = next(m for m in snapshot if m["type"] == "recovery" and m["content"]["phase"] == "retrying")
    assert marker["content"]["attempt"] == 1
    assert await manager.interrupt(session.id)
    await asyncio.wait_for(receipt.wait(), 5)
    final = [e for e in live if e["type"] == "result"]
    assert len(final) == 1 and final[0]["content"]["status"] == "interrupted"
    assert final[0]["content"]["recovery"]["transient"] == 1
    first_id = final[0]["content"]["request_id"]
    new_live, history = await drive(runtime, "correction")
    second = next(e for e in new_live if e["type"] == "result")
    assert second["content"]["status"] == "success"
    assert second["content"]["request_id"] != first_id
    assert all(count == 0 for count in second["content"]["recovery"].values())
    assert prompts == ["first", "correction"]
    page = (await client.get(f"/api/sessions/{session.id}/messages", params={"before_seq": history[-1]["seq"] + 1, "limit": 2})).json()
    assert page["messages"][-1]["content"] == second["content"]

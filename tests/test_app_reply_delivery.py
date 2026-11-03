"""App responses report required-consumer failures without replaying saved work."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.app_agent import AppAgentError, AppAgentManager
from server.applications import ApplicationManager, app_scope_token
from server.config import settings
from server.harness import HarnessEvent
from server.routers import applications as routes
from tests.fake_run import FakeRunBase
from tests.test_session_owner_publication import notices as notices


@pytest.fixture
async def reply_app(notices, tmp_path, monkeypatch):
    manager, original, _ = notices
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=manager.db)
    agent = await AgentManager(manager.db).create_agent(name="App reply", user_id=original.user_id)
    await manager.db.save_application(
        app_id="reply-app", name="Reply app", agent_id=agent["id"], description="Existing application",
        app_dir=str(tmp_path / "apps" / "reply"), status="ready", user_id=original.user_id,
        created_at="2026-11-03T00:00:00+00:00", updated_at="2026-11-03T00:00:00+00:00",
    )
    aam = AppAgentManager()
    monkeypatch.setattr(routes, "_manager", apps)
    monkeypatch.setattr(routes, "app_agent_manager", aam)
    app = FastAPI()
    app.include_router(routes.static_router)
    state = {"texts": ["Visible prefix", "Saved suffix"], "starts": 0}
    retiring, release = asyncio.Event(), asyncio.Event()
    started, finish = asyncio.Event(), asyncio.Event()
    finish.set()
    state.update(started=started, finish=finish)
    release.set()

    class NormalRun(FakeRunBase):
        async def start(self, *args, **kwargs):
            state["starts"] += 1
            started.set()

        async def stream(self):
            for text in state["texts"]:
                yield HarnessEvent(type="text", content=text)
            await finish.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            pass

        async def stop(self):
            retiring.set()
            await release.wait()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: NormalRun())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Octopus-App-Token": app_scope_token("reply-app")}) as client:
            yield client, manager, aam, apps, state, retiring, release
    finally:
        finish.set()
        release.set()
        await manager.drain_turns(timeout=5)
        await manager.close_session_owners()
        aam.shutdown()
        apps.shutdown()


@pytest.mark.parametrize("delivery", ["ask", "chat"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_required_body_failure_reports_error_and_keeps_original_result(reply_app, failure, delivery):
    client, manager, aam, apps, state, retiring, release = reply_app
    observe = aam._on_broadcast
    failed = []

    async def broken_body(event):
        if event.get("type") == "assistant_text" and event.get("content") == "Saved suffix":
            failed.append(event)
            raise failure("Required App body consumer failed")
        await observe(event)

    aam._on_broadcast = broken_body
    aam.bind(session_mgr=manager, db=manager.db, app_mgr=apps)
    try:
        response = await asyncio.wait_for(client.post(f"/apps/reply-app/agent/{delivery}", json={"message": "Original work"}), 3)
        assert len(failed) == 1
        if delivery == "ask":
            assert response.status_code == 502, response.text
            recovery = response.json()["detail"]
        else:
            assert response.status_code == 200
            frames = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
            assert [frame["text"] for frame in frames if frame["type"] == "message"] == ["Visible prefix"]
            assert frames[-1]["type"] == "error" and not any(frame["type"] == "done" for frame in frames)
            recovery = frames[-1]
        event = failed[0]
        status_url = f"/apps/reply-app/agent/conversations/{event['session_id']}/requests/{event['request_id']}"
        assert recovery["code"] == "reply_delivery_failed" and recovery["status_url"] == status_url
        saved = await client.get(status_url)
        assert saved.status_code == 200
        assert saved.json()["state"] == "completed"
        assert saved.json()["reply"] == "Visible prefix\n\nSaved suffix"
        assert saved.json()["outcome"]["status"] == "success"
        await asyncio.wait_for(retiring.wait(), 2)
        assert state["starts"] == 1
    finally:
        release.set()


@pytest.mark.parametrize("delivery", ["ask", "chat"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_best_effort_observer_failure_does_not_damage_empty_success(reply_app, delivery, failure):
    client, manager, aam, apps, state, _, _ = reply_app
    state["texts"] = []

    async def broken_observer(event):
        raise failure("Unrelated observer failed")

    manager.on_broadcast("unrelated-observer", broken_observer)
    aam.bind(session_mgr=manager, db=manager.db, app_mgr=apps)
    response = await client.post(f"/apps/reply-app/agent/{delivery}", json={"message": "An empty answer is valid"})
    assert response.status_code == 200, response.text
    if delivery == "ask":
        assert response.json()["reply"] == ""
    else:
        frames = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
        assert frames[-1]["type"] == "done" and frames[-1]["reply"] == ""
    assert state["starts"] == 1


@pytest.mark.parametrize("consumer", ["ask", "stream"])
async def test_response_consumer_exit_keeps_original_execution_capacity(reply_app, monkeypatch, consumer):
    from server import app_agent

    client, manager, aam, apps, state, _, _ = reply_app
    monkeypatch.setattr(app_agent, "MAX_ACTIVE_TURNS_PER_APP", 1)
    aam.bind(session_mgr=manager, db=manager.db, app_mgr=apps)
    row = await manager.db.get_application("reply-app")
    state["finish"].clear()
    asking = None
    try:
        if consumer == "ask":
            asking = asyncio.create_task(client.post("/apps/reply-app/agent/ask", json={"message": "Keep original work"}))
            await asyncio.wait_for(state["started"].wait(), 2)
            asking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asking
        else:
            turn = await aam.begin_turn(row, message="Keep original work")
            stream = aam.stream_turn(turn)
            assert (await anext(stream))["type"] == "conversation"
            await stream.aclose()
            await asyncio.wait_for(state["started"].wait(), 2)
        assert aam.list_conversations("reply-app")[0]["running"]
        with pytest.raises(AppAgentError) as refused:
            await aam.begin_turn(row, message="Execution cap still applies")
        assert refused.value.status_code == 429
        assert state["starts"] == 1
        state["finish"].set()
        await manager.drain_turns(timeout=3)
        async with asyncio.timeout(2):
            while aam.list_conversations("reply-app")[0]["running"]:
                await asyncio.sleep(0)
    finally:
        state["finish"].set()
        if asking is not None:
            await asyncio.gather(asking, return_exceptions=True)

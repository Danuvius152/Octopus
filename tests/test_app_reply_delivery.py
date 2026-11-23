"""App responses report required-consumer failures without replaying saved work."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from server import app_agent as app_agent_module
from server.agent_manager import AgentManager
from server.app_agent import AppAgentError, AppAgentManager
from server.applications import ApplicationManager, app_scope_token
from server.config import settings
from server.harness import HarnessEvent
from server.main import _HTTPCompression
from server.routers import applications as routes
from tests.app_sse import app_sse_event
from tests.fake_run import FakeRunBase, model_run_factory
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
    aam.bind(session_mgr=manager, db=manager.db, app_mgr=apps)
    monkeypatch.setattr(routes, "_manager", apps)
    monkeypatch.setattr(routes, "app_agent_manager", aam)
    app = FastAPI()
    app.include_router(routes.static_router)
    state = {"texts": ["Visible prefix", "Saved suffix"], "starts": 0}
    retiring, release = asyncio.Event(), asyncio.Event()
    started, finish = asyncio.Event(), asyncio.Event()
    publish = asyncio.Event()
    finish.set()
    publish.set()
    state["publish"] = publish
    state.update(started=started, finish=finish)
    release.set()

    class NormalRun(FakeRunBase):
        def __init__(self, session_id):
            super().__init__()
            self.session_id = session_id

        async def _start(self, *args, **kwargs):
            state["starts"] += 1
            started.set()

        async def _stream(self):
            await publish.wait()
            for text in state["texts"]:
                yield HarnessEvent(type="text", content=text)
            if state.get("hold_conversation", self.session_id) == self.session_id:
                await finish.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            pass

        async def _stop_process(self):
            retiring.set()
            await release.wait()

    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda session, *args, **kwargs: NormalRun(session.id)))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Octopus-App-Token": app_scope_token("reply-app")}) as client:
            yield client, manager, aam, apps, state, retiring, release
    finally:
        publish.set()
        finish.set()
        release.set()
        await manager.drain_turns(timeout=5)
        await manager.close_session_owners()
        aam.shutdown()
        apps.shutdown()


async def chat_response():
    scope = {
        "type": "http", "method": "POST", "path": "/apps/reply-app/agent/chat",
        "headers": [(b"x-octopus-app-token", app_scope_token("reply-app").encode())],
        "asgi": {"version": "3.0", "spec_version": "2.4"},
    }
    response = await routes.app_agent_chat(Request(scope), "reply-app", routes.AppAgentTurnRequest(message="Original work"))
    return scope, response


async def idle_receive():
    await asyncio.Event().wait()


def request_status_url(headers):
    values = dict(headers)
    conversation_id = values[b"x-octopus-conversation-id"].decode()
    request_id = values[b"x-octopus-request-id"].decode()
    return f"/apps/reply-app/agent/conversations/{conversation_id}/requests/{request_id}"


@pytest.mark.parametrize("root_path", ["", "/octopus"])
@pytest.mark.parametrize("encoding", ["gzip", "identity"])
async def test_app_sse_headers_reach_asgi_before_any_body_with_http_compression(reply_app, monkeypatch, root_path, encoding):
    client, manager, aam, _, state, _, _ = reply_app
    app = FastAPI()
    app.add_middleware(_HTTPCompression, minimum_size=1024, compresslevel=5)
    app.include_router(routes.static_router)
    awaiting_first_frame = asyncio.Event()
    original_stream = aam.stream_turn

    async def hold_first_frame(turn):
        awaiting_first_frame.set()
        await asyncio.Event().wait()
        async for frame in original_stream(turn):
            yield frame

    monkeypatch.setattr(aam, "stream_turn", hold_first_frame)
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b'{"message":"Original work"}', "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    path = root_path + "/apps/reply-app/agent/chat"
    observing = asyncio.create_task(app({
        "type": "http", "method": "POST", "scheme": "http", "http_version": "1.1",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "path": path, "raw_path": path.encode(), "root_path": root_path, "query_string": b"",
        "headers": [(b"x-octopus-app-token", app_scope_token("reply-app").encode()),
                    (b"content-type", b"application/json"), (b"accept-encoding", encoding.encode())],
    }, incoming.get, send))
    try:
        await asyncio.wait_for(awaiting_first_frame.wait(), 3)
        assert [message["type"] for message in sent] == ["http.response.start"]
        assert sent[0]["status"] == 200 and b"content-encoding" not in dict(sent[0]["headers"])
    finally:
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(observing, 3)
    status_url = request_status_url(sent[0]["headers"])
    original = (await client.get(status_url)).json()
    await manager.await_session_execution(original["conversation_id"], timeout=3)
    assert (await client.get(status_url)).json()["reply"] == "Visible prefix\n\nSaved suffix"
    assert state["starts"] == 1


@pytest.mark.parametrize("delivery", ["ask", "chat"])
@pytest.mark.parametrize("code", ["response_wait_timeout", "reply_delivery_failed"])
async def test_app_recovery_url_keeps_the_http_mount_prefix(reply_app, monkeypatch, delivery, code):
    template_client, manager, _, _, state, _, _ = reply_app
    prefix = "/octopus"
    app = FastAPI(root_path=prefix)
    app.add_middleware(_HTTPCompression, minimum_size=1024, compresslevel=5)
    app.include_router(routes.static_router)

    async def break_notification(event):
        if event.get("type") == "assistant_text" and event.get("content") == "Saved suffix":
            raise RuntimeError("Injected notification failure")

    if code == "response_wait_timeout":
        monkeypatch.setattr(app_agent_module, "ASK_TIMEOUT_SECONDS", 0.05)
        state["finish"].clear()
    else:
        manager.on_broadcast("mount-notification-failure", break_notification, required=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=template_client.headers) as client:
        response = await client.post(f"{prefix}/apps/reply-app/agent/{delivery}", json={"message": "Original work"})
        if delivery == "chat":
            assert response.status_code == 200
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            recovery = frames[-1]
            assert recovery["type"] == "error"
            assert recovery["conversation_id"] == frames[0]["conversation_id"] == response.headers["X-Octopus-Conversation-Id"]
            assert recovery["request_id"] == frames[0]["request_id"] == response.headers["X-Octopus-Request-Id"]
        else:
            assert response.status_code == (504 if code == "response_wait_timeout" else 502)
            recovery = response.json()["detail"]
        assert recovery["code"] == code
        assert recovery["status_url"] == (
            f"{prefix}/apps/reply-app/agent/conversations/{recovery['conversation_id']}/requests/{recovery['request_id']}"
        )
        state["finish"].set()
        await manager.await_session_execution(recovery["conversation_id"], timeout=3)
        saved = await client.get(recovery["status_url"])
        assert saved.status_code == 200
        assert saved.json()["request_id"] == recovery["request_id"]
        assert saved.json()["state"] == "completed" and saved.json()["reply"] == "Visible prefix\n\nSaved suffix"
    assert state["starts"] == 1


@pytest.mark.parametrize("stalled", ["headers", "body", "end"])
async def test_app_sse_times_out_a_blocked_send_and_keeps_the_original_reply(reply_app, monkeypatch, stalled):
    client, manager, _, _, state, _, _ = reply_app
    monkeypatch.setattr(routes, "_APP_SSE_SEND_TIMEOUT_SECONDS", 0.05)
    if stalled != "end":
        state["finish"].clear()
    scope, response = await chat_response()
    seen = []
    blocked = []

    async def send(message):
        seen.append(message)
        phase = {None: "headers", True: "body", False: "end"}[message.get("more_body")]
        if phase == stalled:
            blocked.append(message)
            await asyncio.Event().wait()

    await asyncio.wait_for(response(scope, idle_receive, send), 1)
    assert blocked == [seen[-1]]
    status_url = request_status_url(seen[0]["headers"])
    observed = (await client.get(status_url)).json()
    assert observed["state"] == ("completed" if stalled == "end" else "running")
    state["finish"].set()
    await manager.await_session_execution(observed["conversation_id"], timeout=3)
    assert (await client.get(status_url)).json()["reply"] == "Visible prefix\n\nSaved suffix"
    assert state["starts"] == 1


@pytest.mark.parametrize("limit", ["frames", "bytes", "single-frame"])
async def test_app_sse_limits_include_the_in_flight_frame_without_blocking_another_ask(reply_app, monkeypatch, limit):
    client, manager, aam, _, state, _, _ = reply_app
    if limit == "frames":
        monkeypatch.setattr(app_agent_module, "_SSE_MAX_FRAMES", 2)
    elif limit == "bytes":
        monkeypatch.setattr(app_agent_module, "_SSE_MAX_BYTES", 420)
        state["texts"] = ["中" * 18, "文" * 18]
    else:
        monkeypatch.setattr(app_agent_module, "_SSE_MAX_BYTES", 256)
        state["texts"] = ["中" * 90]

    # The SSE turn may publish text, but its terminal event cannot be the
    # cause of the overflow. The other conversation can run to completion.
    state["publish"].clear()
    state["finish"].clear()
    scope, response = await chat_response()
    first_frame_started, release_send, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == state["texts"][-1]:
            published.set()

    manager.on_broadcast("observe-sse-pressure", observe)

    async def send(message):
        seen.append(message)
        if len(seen) == 2:
            first_frame_started.set()
            await release_send.wait()

    responding = asyncio.create_task(response(scope, idle_receive, send))
    try:
        await asyncio.wait_for(first_frame_started.wait(), 3)
        status_url = request_status_url(seen[0]["headers"])
        first = app_sse_event(seen[1]["body"])
        assert first["type"] == "conversation" and status_url.endswith(
            f"/conversations/{first['conversation_id']}/requests/{first['request_id']}"
        )
        state["hold_conversation"] = first["conversation_id"]
        state["publish"].set()
        await asyncio.wait_for(published.wait(), 3)
        parallel = await asyncio.wait_for(
            client.post("/apps/reply-app/agent/ask", json={"message": "Independent work"}), 3,
        )
        answer = "\n\n".join(state["texts"])
        assert parallel.status_code == 200 and parallel.json()["reply"] == answer
        assert parallel.json()["conversation_id"] != first["conversation_id"]
        assert not responding.done()
        assert (await client.get(status_url)).json()["state"] == "running"

        release_send.set()
        await asyncio.wait_for(responding, 3)
        assert seen[2:] == [{"type": "http.response.body", "body": b"", "more_body": False}]
        state["finish"].set()
        await manager.await_session_execution(first["conversation_id"], timeout=3)
        saved = (await client.get(status_url)).json()
        assert saved["state"] == "completed" and saved["reply"] == answer
        assert state["starts"] == 2
        async with asyncio.timeout(3):
            while aam.drain_blockers():
                await asyncio.sleep(0)
    finally:
        state["publish"].set()
        state["finish"].set()
        release_send.set()
        if not responding.done():
            responding.cancel()
        await asyncio.gather(responding, return_exceptions=True)


@pytest.mark.parametrize("unencodable", ["message", "done"])
async def test_app_sse_encoding_failure_ends_only_the_response(reply_app, monkeypatch, unencodable):
    client, manager, aam, _, state, _, _ = reply_app

    def dumps(event, **kwargs):
        if event["type"] == unencodable:
            raise ValueError("Frame cannot be serialized")
        return json.dumps(event, **kwargs)

    monkeypatch.setattr(app_agent_module, "json", SimpleNamespace(dumps=dumps))
    response = await client.post("/apps/reply-app/agent/chat", json={"message": "Original work"})
    assert response.status_code == 200
    assert "event: done\n" not in response.text and "event: error\n" not in response.text
    await manager.await_session_execution(response.headers["X-Octopus-Conversation-Id"], timeout=3)
    saved = (await client.get(request_status_url(response.headers.raw))).json()
    assert saved["state"] == "completed" and saved["reply"] == "Visible prefix\n\nSaved suffix"
    assert state["starts"] == 1
    async with asyncio.timeout(3):
        while aam.drain_blockers():
            await asyncio.sleep(0)


async def test_app_chat_sends_identity_headers_if_observation_overflows_before_admission_returns(reply_app, monkeypatch):
    client, manager, _, _, state, _, _ = reply_app
    monkeypatch.setattr(app_agent_module, "_SSE_MAX_FRAMES", 1)
    state["texts"] = ["Published before the response"]
    published = asyncio.Event()

    async def observe(event):
        if event.get("type") == "assistant_text":
            published.set()

    manager.on_broadcast("sse-before-admission-returns", observe)
    start = manager.start_message

    async def admit_and_wait_for_publication(*args, **kwargs):
        receipt = await start(*args, **kwargs)
        await asyncio.wait_for(published.wait(), 3)
        return receipt

    monkeypatch.setattr(manager, "start_message", admit_and_wait_for_publication)
    response = await client.post("/apps/reply-app/agent/chat", json={"message": "Original work"})
    assert response.status_code == 200 and response.content == b""
    await manager.await_session_execution(response.headers["X-Octopus-Conversation-Id"], timeout=3)
    saved = (await client.get(request_status_url(response.headers.raw))).json()
    assert saved["state"] == "completed" and saved["reply"] == state["texts"][0]
    assert state["starts"] == 1


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
    client, manager, _, _, state, _, _ = reply_app
    state["texts"] = []

    async def broken_observer(event):
        raise failure("Unrelated observer failed")

    manager.on_broadcast("unrelated-observer", broken_observer)
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
    client, manager, aam, _, state, _, _ = reply_app
    monkeypatch.setattr(app_agent_module, "MAX_ACTIVE_TURNS_PER_APP", 1)
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
            turn = await aam.begin_turn(row, message="Keep original work", observe_stream=True)
            stream = aam.stream_turn(turn)
            assert app_sse_event(await anext(stream))["type"] == "conversation"
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

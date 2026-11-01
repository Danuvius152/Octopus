"""A failed Session owner ends observers without inventing an execution result."""

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.applications import ApplicationManager, app_scope_token
from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.routers import applications as app_routes
from server.routers import questions, sessions
from server.session_manager import SessionManager
from server.sessions.client_submissions import submit_client_message
from server.sessions.owner import SessionOwnerUnavailable
from server.users import UserManager
from tests.accounts import create_account


@pytest.mark.parametrize("delivery", ["ask", "chat"])
async def test_owner_failure_ends_app_and_question_waits_without_replaying_input(
    tmp_path, monkeypatch, delivery,
):
    monkeypatch.setattr(settings, "auth_token", "owner-failure-token")
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    database = Database(str(tmp_path / "owner-failure.db"))
    await database.initialize()
    owner, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    applications = ApplicationManager()
    applications.bind(session_mgr=manager, db=database)
    app_agent = AppAgentManager()
    app_agent.bind(session_mgr=manager, db=database, app_mgr=applications)

    app = FastAPI()
    app.include_router(questions.router)
    app.include_router(sessions.router)
    app.include_router(app_routes.static_router)
    app.dependency_overrides[deps.get_session_manager] = lambda: manager
    monkeypatch.setattr(app_routes, "_manager", applications)
    monkeypatch.setattr(app_routes, "app_agent_manager", app_agent)
    agent = await AgentManager(database).create_agent(
        name="Owner failure worker", backend="codex", mcp_servers=[],
        user_id=owner["id"],
    )
    app_directory = tmp_path / "application"
    app_directory.mkdir()
    stamp = datetime.now(UTC).isoformat()
    await database.save_application(
        app_id="owner-failure-app", name="Owner failure app",
        description="An existing application conversation", agent_id=agent["id"],
        session_id=None, app_dir=str(app_directory), entrypoint="index.html",
        status="ready", created_at=stamp, updated_at=stamp,
        user_id=owner["id"],
    )
    app_row = await database.get_application("owner-failure-app")
    assert app_row is not None

    model = tmp_path / "owner_failure_model.py"
    retired = tmp_path / "retired"
    body = "The original model output is preserved without a confirmed outcome."
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'owner-failure-thread'}),flush=True)\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'id':'original','type':'agent_message','text':{body!r}}}}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[1]).write_text('The original process reached stdin EOF.')\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None,
        prepare_resume=None, new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(retired)], {"cwd": context.working_dir},
        ),
    ))
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        process = await spawn(*argv, **kwargs)
        if str(model) in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    receipts = []
    start_message = manager.start_message

    async def observe_admission(*args, **kwargs):
        receipt = await start_message(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(manager, "start_message", observe_admission)
    ready = asyncio.Event()
    events = []

    async def observe_output(event):
        events.append(event)
        if event["type"] == "assistant_text" and event.get("content") == body:
            ready.set()

    manager.on_broadcast("owner-failure-output", observe_output)
    original_read = database.has_delivery_notice
    queried = asyncio.Event()

    async def fail_after_real_notice_read(session_id, delivery_id):
        result = await original_read(session_id, delivery_id)
        assert result is False
        queried.set()
        raise RuntimeError("Injected Session owner command failure after its real read")

    original_input = "Keep this original App input available for human inspection."
    asking = polling = receipt_wait = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as client:
            asking = asyncio.create_task(client.post(
                f"/apps/{app_row['id']}/agent/{delivery}",
                json={"message": original_input},
                headers={"X-Octopus-App-Token": app_scope_token(app_row["id"])},
            ))
            await asyncio.wait_for(ready.wait(), 5)
            assert len(processes) == 1 and processes[0].returncode is None
            assert len(receipts) == 1 and receipts[0].accepted
            conversations = app_agent.list_conversations(app_row["id"])
            assert len(conversations) == 1
            session_id = conversations[0]["id"]
            request_id = receipts[0].request_id
            receipt_wait = asyncio.create_task(receipts[0].wait())

            created = await client.post(
                f"/api/sessions/{session_id}/questions",
                json={"questions": [{"question": "Continue?", "options": [{"label": "Yes"}]}]},
            )
            assert created.status_code == 201, created.text
            question_id = created.json()["question_id"]
            polling = asyncio.create_task(client.get(
                f"/api/sessions/{session_id}/questions/{question_id}/answer?timeout=60",
            ))
            done, _ = await asyncio.wait({polling, asking, receipt_wait}, timeout=.05)
            assert not done, "The original App request and question must be waiting before owner failure."

            monkeypatch.setattr(database, "has_delivery_notice", fail_after_real_notice_read)
            with pytest.raises(SessionOwnerUnavailable):
                await asyncio.wait_for(manager.retain_result_notice(
                    session_id, "owner-failure-notice", "This failed notice was never confirmed.",
                ), 5)
            assert queried.is_set()
            reply = await asyncio.wait_for(asking, 5)
            if delivery == "ask":
                assert reply.status_code == 502, reply.text
                assert "owner" in reply.json()["detail"].lower()
            else:
                assert reply.status_code == 200, reply.text
                assert reply.headers["content-type"].startswith("text/event-stream")
                frames = [json.loads(line.removeprefix("data: "))
                          for line in reply.text.splitlines() if line.startswith("data: ")]
                assert frames[0]["type"] == "conversation"
                assert [frame["text"] for frame in frames if frame["type"] == "message"] == [body]
                assert [frame["type"] for frame in frames if frame["type"] in {"done", "error"}] == ["error"]
                assert frames[-1]["type"] == "error"
                assert "owner" in frames[-1]["message"].lower()
            completion = await asyncio.wait_for(receipt_wait, 5)
            assert completion.state == "unavailable" and completion.outcome is None
            assert completion.notification_state == "unavailable"
            polled = await asyncio.wait_for(polling, 5)
            assert polled.status_code == 404, polled.text

            # This assertion precedes test cleanup: the original owner must
            # retire the real process, rather than relying on finally's kill.
            assert await asyncio.wait_for(processes[0].wait(), 5) == 0
            assert retired.read_text() == "The original process reached stdin EOF."
            detail = await client.get(f"/api/sessions/{session_id}")
            assert detail.status_code == 200, detail.text
            messages = detail.json()["messages"]
            assert any(original_input in message["content"]
                       for message in messages if message["role"] == "user")
            assert any(message["content"] == body for message in messages if message["role"] == "assistant")
            assert not [message for message in messages if message["type"] in {"result", "delivery_notice"}]
            assert detail.json()["pending_questions"] == []
            assert not [event for event in events if event["type"] == "result"]
            status = await app_agent.get_request_status(app_row["id"], session_id, request_id)
            assert status["state"] != "completed"

            monkeypatch.setattr(database, "has_delivery_notice", original_read)
            with pytest.raises(SessionOwnerUnavailable):
                await manager.retain_result_notice(
                    session_id, "owner-failure-later", "Restoring the dependency must not restart the owner.",
                )
            await asyncio.wait_for(manager.close_session_owners(), 5)
            assert len(processes) == 1
            final_history = await client.get(f"/api/sessions/{session_id}")
            assert final_history.status_code == 200
            assert final_history.json()["messages"] == messages
    finally:
        monkeypatch.setattr(database, "has_delivery_notice", original_read)
        app_agent.shutdown()
        for task in (asking, polling, receipt_wait):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (asking, polling, receipt_wait) if task is not None),
                             return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await asyncio.wait_for(manager.drain_turns(), 5)
        await asyncio.wait_for(manager.close_session_owners(), 5)
        manager.remove_broadcast("owner-failure-output")
        applications.shutdown()
        await database.close()


async def test_unconfirmed_owner_retirement_exits_70_and_cold_start_keeps_original_input(
    tmp_path,
):
    artifacts = tmp_path / "unconfirmed-retirement"
    artifacts.mkdir()
    proof_file = artifacts / "retirement-proof.json"
    model_identity = artifacts / "model-identity.json"
    holder_identity = artifacts / "holder-identity.json"
    database = None
    try:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
                 str(Path(__file__).resolve()), "-o", "python_functions=_owner_retirement_failure_child",
                 "--basetemp", str(artifacts / "child-tests")],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired as error:
            print((error.stdout or b"").decode(errors="replace"), flush=True)
            print((error.stderr or b"").decode(errors="replace"), flush=True)
            raise
        assert result.returncode == 70, result.stdout + result.stderr
        assert proof_file.exists(), result.stdout + result.stderr
        proof = json.loads(proof_file.read_text())
        model = json.loads(model_identity.read_text())
        holder = json.loads(holder_identity.read_text())
        print(f"Actual Manager fail-stop exit={result.returncode}, model={model}, holder={holder}", flush=True)
        assert proof["model"] == model
        assert proof["holder"] == holder
        assert proof["process_group"] == model["pid"] == model["process_group"]
        assert holder["process_group"] == holder["pid"]
        assert holder["process_group"] != model["process_group"]
        assert (artifacts / "model-reached-eof").read_text() == "Original model received stdin EOF."
        holder_stat = Path(f"/proc/{holder['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        assert holder_stat[19] == holder["start_time"]
        assert holder_stat[0] != "Z", "The original inherited output pipe must still be held at fail-stop."

        # The fatal process never closes or reopens its connection. Inspect
        # the actual persisted writer identity before restart reconciliation.
        database = Database(proof["database"])
        await database.initialize()
        users = UserManager(database)
        deps.set_user_manager(users)
        bearer = await users.issue_token(proof["user_id"])
        original = await database.get_client_submission(proof["session_id"], "owner-failure-input")
        assert original["request_id"] == original["execution_owner"] == proof["request_id"]
        assert original["process_group"] == model["process_group"]
        assert original["payload"]["content"] == "Retain this exact input after unconfirmed resource retirement."
        restored = SessionManager()
        await restored.initialize(database)
        app = FastAPI()
        app.include_router(sessions.router)
        app.dependency_overrides[deps.get_session_manager] = lambda: restored
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as client:
            detail = await client.get(f"/api/sessions/{proof['session_id']}")
            assert detail.status_code == 200, detail.text
            messages = detail.json()["messages"]
            assert [message["content"] for message in messages
                    if message["role"] == "user" and message["type"] == "text"] == [
                "Retain this exact input after unconfirmed resource retirement.",
            ]
            assert [message["content"] for message in messages
                    if message["role"] == "assistant" and message["type"] == "text"] == [
                "Original output before the owner failed with an inherited pipe still open.",
            ]
            assert not [message for message in messages if message["type"] == "result"]
            status = await client.get(
                f"/api/sessions/{proof['session_id']}/submissions/owner-failure-input",
            )
            assert status.status_code == 200, status.text
            saved = status.json()
            assert saved["accepted"] is True
            assert saved["request_id"] == proof["request_id"]
            assert saved["execution"]["execution_owner_request_id"] == proof["request_id"]
            assert saved["execution"]["state"] == "unknown"
            assert saved["input"]["content"] == "Retain this exact input after unconfirmed resource retirement."
            assert saved["execution"]["recoverable_input"] is False
            assert restored.dispatch_snapshot(proof["session_id"])["items"] == []
            assert (await client.get(f"/api/sessions/{proof['session_id']}")).json()["messages"] == messages
    finally:
        # A pidfd is tied to this exact original PID. Verify its recorded
        # start time before signalling, then confirm OS exit even if init has
        # not yet reaped an orphan's zombie entry.
        for identity_file in (holder_identity, model_identity):
            if not identity_file.exists():
                continue
            identity = json.loads(identity_file.read_text())
            try:
                descriptor = os.pidfd_open(identity["pid"])
            except ProcessLookupError:
                continue
            try:
                try:
                    fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    continue
                if fields[19] != identity["start_time"]:
                    continue
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                ready, _, _ = await asyncio.to_thread(select.select, [descriptor], [], [], 5)
                assert ready, f"Original test process did not exit: {identity}"
            finally:
                os.close(descriptor)
        if database is not None:
            await database.close()


async def _owner_retirement_failure_child(tmp_path, tmp_path_factory, monkeypatch):
    """Explicitly selected in a child pytest process to observe real exit70."""
    artifacts = tmp_path_factory.getbasetemp().parent
    database_path = tmp_path / "unconfirmed-owner.db"
    database = Database(str(database_path))
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await AgentManager(database).create_agent(
        name="Unconfirmed owner resource", backend="codex", mcp_servers=[],
        user_id=owner["id"],
    )
    holder_script = tmp_path / "original_pipe_holder.py"
    holder_script.write_text(
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'process_group':os.getpgrp(),'start_time':fields[19]}))\n"
        "while True: time.sleep(1)\n"
    )
    model_script = tmp_path / "original_pipe_model.py"
    model_script.write_text(
        "import json,os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'process_group':os.getpgrp(),'start_time':fields[19]}))\n"
        "sys.stdin.readline()\n"
        "subprocess.Popen([sys.executable,'-u',sys.argv[2],sys.argv[3]],stdin=subprocess.DEVNULL,start_new_session=True)\n"
        "while not Path(sys.argv[3]).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'thread.started','thread_id':'unconfirmed-owner-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'original','type':'agent_message','text':'Original output before the owner failed with an inherited pipe still open.'}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[4]).write_text('Original model received stdin EOF.')\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None,
        prepare_resume=None, new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model_script), str(artifacts / "model-identity.json"),
             str(holder_script), str(artifacts / "holder-identity.json"), str(artifacts / "model-reached-eof")],
            {"cwd": context.working_dir},
        ),
    ))
    ready = asyncio.Event()

    async def observe(event):
        if event["type"] == "assistant_text":
            ready.set()

    manager.on_broadcast("original-unconfirmed-output", observe)
    session = await manager.create_session(agent["id"], "Original unknown execution", str(tmp_path), backend="codex")
    admitted = await submit_client_message(
        manager, session.id, "Retain this exact input after unconfirmed resource retirement.",
        [], "owner-failure-input", wiki_mode=None,
    )
    assert admitted["accepted"] is True
    await asyncio.wait_for(ready.wait(), 5)
    stored = await database.get_client_submission(session.id, "owner-failure-input")
    model = json.loads((artifacts / "model-identity.json").read_text())
    assert stored["process_group"] == model["process_group"]
    (artifacts / "retirement-proof.json").write_text(json.dumps({
        "database": str(database_path), "session_id": session.id, "request_id": admitted["request_id"],
        "user_id": owner["id"],
        "process_group": stored["process_group"], "model": model,
        "holder": json.loads((artifacts / "holder-identity.json").read_text()),
    }))
    read_notice = database.has_delivery_notice

    async def owner_failure_after_real_read(session_id, delivery_id):
        await read_notice(session_id, delivery_id)
        raise RuntimeError("Original owner failed while its inherited output pipe was held")

    monkeypatch.setattr(database, "has_delivery_notice", owner_failure_after_real_read)
    with pytest.raises(SessionOwnerUnavailable):
        await manager.retain_result_notice(session.id, "unconfirmed-owner-fault", "No confirmed notice")
    # Keep the original event loop available to Manager's actual cleanup.
    # Its unchanged bounded retirement path must invoke actual health.fail.
    await asyncio.wait_for(asyncio.Event().wait(), 12)


@pytest.mark.parametrize("delivered_before_failure", [False, True], ids=["pending-prefix", "delivered-before-uptake"])
async def test_owner_failure_keeps_a_committed_result_while_its_notification_is_blocked(
    tmp_path, monkeypatch, delivered_before_failure,
):
    monkeypatch.setattr(settings, "auth_token", "owner-finality-token")
    database = Database(str(tmp_path / "committed-result.db"))
    await database.initialize()
    owner, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    applications = ApplicationManager()
    applications.bind(session_mgr=manager, db=database)
    held, release = asyncio.Event(), asyncio.Event()
    delivered, failure_entered, release_failure = asyncio.Event(), asyncio.Event(), asyncio.Event()
    published_results = []
    broadcast = manager._broadcast

    async def observe_actual_delivery(event, *, user_id=None):
        await broadcast(event, user_id=user_id)
        if event["type"] == "result":
            delivered.set()

    monkeypatch.setattr(manager, "_broadcast", observe_actual_delivery)

    async def hold_committed_terminal(event):
        if event["type"] == "result":
            held.set()
            await release.wait()
            published_results.append(event)

    # The original result is committed before this first subscriber waits;
    # App's subscriber has not received that final prefix notification yet.
    manager.on_broadcast("hold-committed-result", hold_committed_terminal)
    app_agent = AppAgentManager()
    app_agent.bind(session_mgr=manager, db=database, app_mgr=applications)
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(app_routes.static_router)
    app.dependency_overrides[deps.get_session_manager] = lambda: manager
    monkeypatch.setattr(app_routes, "_manager", applications)
    monkeypatch.setattr(app_routes, "app_agent_manager", app_agent)
    agent = await AgentManager(database).create_agent(
        name="Committed owner failure", backend="codex", mcp_servers=[],
        user_id=owner["id"],
    )
    application_directory = tmp_path / "existing-app"
    application_directory.mkdir()
    stamp = datetime.now(UTC).isoformat()
    await database.save_application(
        app_id="committed-app", name="Committed app", description="Existing app",
        agent_id=agent["id"], session_id=None, app_dir=str(application_directory),
        entrypoint="index.html", status="ready", created_at=stamp, updated_at=stamp,
        user_id=owner["id"],
    )
    model = tmp_path / "completed_model.py"
    retired = tmp_path / "completed-model-retired"
    body = "This exact original reply was committed before its final notification completed."
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'committed-owner-thread'}),flush=True)\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'id':'original','type':'agent_message','text':{body!r}}}}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[1]).write_text('Original completed model reached EOF.')\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None,
        prepare_resume=None, new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(retired)], {"cwd": context.working_dir},
        ),
    ))
    processes, receipts = [], []
    spawn = asyncio.create_subprocess_exec
    start_message = manager.start_message

    async def observe_spawn(*argv, **kwargs):
        process = await spawn(*argv, **kwargs)
        if str(model) in argv:
            processes.append(process)
        return process

    async def observe_admission(*args, **kwargs):
        receipt = await start_message(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(manager, "start_message", observe_admission)
    read_notice = database.has_delivery_notice

    async def fail_after_real_read(session_id, delivery_id):
        await read_notice(session_id, delivery_id)
        failure_entered.set()
        await release_failure.wait()
        raise RuntimeError("Owner failed after the original result committed")

    original_input = "Keep the committed result tied to this exact original input."
    chatting = receipt_wait = failing = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as client:
            chatting = asyncio.create_task(client.post(
                "/apps/committed-app/agent/chat", json={"message": original_input},
                headers={"X-Octopus-App-Token": app_scope_token("committed-app")},
            ))
            await asyncio.wait_for(held.wait(), 5)
            assert len(processes) == len(receipts) == 1
            receipt = receipts[0]
            assert receipt.accepted
            session_id = app_agent.list_conversations("committed-app")[0]["id"]
            receipt_wait = asyncio.create_task(receipt.wait())
            done, _ = await asyncio.wait({chatting, receipt_wait}, timeout=.05)
            assert not done, "Committed storage alone cannot complete App's original notification prefix."

            snapshot = await client.get(f"/api/sessions/{session_id}")
            assert snapshot.status_code == 200, snapshot.text
            messages = snapshot.json()["messages"]
            terminals = [message for message in messages if message["type"] == "result"]
            assert len(terminals) == 1
            original_terminal = terminals[0]
            assert original_terminal["content"]["request_id"] == receipt.request_id
            assert original_terminal["content"]["status"] == "success"
            assert any(original_input in message["content"]
                       for message in messages if message["role"] == "user")
            assert [message["content"] for message in messages
                    if message["role"] == "assistant" and message["type"] == "text"] == [body]
            known = await client.get(
                f"/apps/committed-app/agent/conversations/{session_id}/requests/{receipt.request_id}",
                headers={"X-Octopus-App-Token": app_scope_token("committed-app")},
            )
            assert known.status_code == 200
            assert known.json()["state"] == "completed"
            assert known.json()["reply"] == body

            monkeypatch.setattr(database, "has_delivery_notice", fail_after_real_read)
            failing = asyncio.create_task(manager.retain_result_notice(
                session_id, "fault-after-commit", "Never committed notice",
            ))
            await asyncio.wait_for(failure_entered.wait(), 2)
            if delivered_before_failure:
                release.set()
                await asyncio.wait_for(delivered.wait(), 2)
                assert not receipt_wait.done(), "The original writer is still waiting on its failing query"
            release_failure.set()
            with pytest.raises(SessionOwnerUnavailable):
                await failing
            # This receipt had not satisfied its publication condition. Its
            # unavailable result must not replace the known committed outcome.
            completion = await asyncio.wait_for(receipt_wait, 5)
            assert completion.state == "completed" and completion.recording_state == "committed"
            assert completion.outcome.model_dump() == original_terminal["content"]
            assert completion.notification_state == ("delivered" if delivered_before_failure else "unavailable")
            reply = await asyncio.wait_for(chatting, 5)
            assert reply.status_code == 200
            frames = [json.loads(line.removeprefix("data: "))
                      for line in reply.text.splitlines() if line.startswith("data: ")]
            assert [frame["text"] for frame in frames if frame["type"] == "message"] == [body]
            assert [frame["type"] for frame in frames if frame["type"] in {"done", "error"}] == [
                "done" if delivered_before_failure else "error",
            ]
            if not delivered_before_failure:
                assert not release.is_set()
                assert "owner" in frames[-1]["message"].lower()
            still_known = await app_agent.get_request_status("committed-app", session_id, receipt.request_id)
            assert still_known["state"] == "completed"
            assert still_known["outcome"] == original_terminal["content"]
            assert still_known["reply"] == body

            release.set()
            await asyncio.wait_for(manager.drain_turns(), 5)
            await asyncio.wait_for(manager.close_session_owners(), 5)
            assert await asyncio.wait_for(processes[0].wait(), 5) == 0
            assert retired.read_text() == "Original completed model reached EOF."
            assert len(processes) == len(published_results) == 1
            assert published_results[0]["content"] == original_terminal["content"]
            final = await client.get(f"/api/sessions/{session_id}")
            assert final.status_code == 200
            assert final.json()["messages"] == messages
            monkeypatch.setattr(database, "has_delivery_notice", read_notice)
            with pytest.raises(SessionOwnerUnavailable):
                await manager.retain_result_notice(session_id, "after-restoring-read", "No owner replay")
    finally:
        release.set()
        release_failure.set()
        monkeypatch.setattr(database, "has_delivery_notice", read_notice)
        for task in (chatting, receipt_wait, failing):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (chatting, receipt_wait, failing) if task is not None),
                             return_exceptions=True)
        app_agent.shutdown()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await asyncio.wait_for(manager.drain_turns(), 5)
        await asyncio.wait_for(manager.close_session_owners(), 5)
        manager.remove_broadcast("hold-committed-result")
        applications.shutdown()
        await database.close()

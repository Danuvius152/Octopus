"""An original mounted ask cancellation and its native completion consumers."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from server.agent_manager import AgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.delegations import DelegationManager
from server.deps import get_session_manager
from server.mcp_identity import mint, register_run, revoke_run, verify
from server.research import manager as research_module
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchReport
from server.routers.questions import router
from server.scheduler import ScheduleRunner
from tests import native_app_server as native_tests
from tests import wiki_api
from tests.callback_api import start_callback_api
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    start_output,
    terminal,
    until,
)
from tests.session_runtime import wait_dispatch_idle
from tests.test_app_response_recovery import native_app_http as native_app_http
from tests.test_ask_mcp import pending_question
from tests.test_scheduler import save_schedule
from tests.test_wiki_job_events import enqueue
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs
from tests.wiki_job_runtime import process

native_runtime = native_tests.native_runtime


async def cancel_original_input(manager, session, token, root, monkeypatch):
    """Use the mounted SDK cancellation boundary, retaining the original Owner."""
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, readiness_token=token)
    monkeypatch.setattr(settings, "port", api.port)
    response_ready, release_response = asyncio.Event(), asyncio.Event()
    post = httpx.AsyncClient.post
    async def hold_creation_response(self, url, **kwargs):
        response = await post(self, url, **kwargs)
        if str(url).endswith("/questions"):
            response_ready.set()
            await release_response.wait()
        return response
    monkeypatch.setattr(httpx.AsyncClient, "post", hold_creation_response)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/ask/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    rpc_id = mcp._request_id
                    calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Input for this work?"}]}, meta={
                        "callId": "consumer-input", "threadId": ROOT,
                        "x-codex-turn-metadata": {"thread_id": ROOT, "turn_id": "turn-1", "codex_version": "0.166.0"}}))
                    await asyncio.wait_for(response_ready.wait(), 3)
                    call, = manager._session_owners[id(session)].questions.mcp_calls.values()
                    pending = call.pending
                    assert pending is not None
                    try:
                        inject(root, {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
                            "id": "consumer-input", "type": "mcpToolCall", "server": "ask", "tool": "user",
                            "arguments": {}, "status": "inProgress"}}})
                        await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                            method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id))))
                        with pytest.raises(McpError, match="Request cancelled"):
                            await calling
                        release_response.set()
                        await asyncio.wait_for(call.consumed.wait(), 3)
                        return pending
                    finally:
                        release_response.set()
                        calling.cancel()
                        await asyncio.gather(calling, call.task, return_exceptions=True)
    finally:
        release_response.set()
        await api.stop()


@pytest.mark.parametrize("other_effect", [False, True])
async def test_input_gap_has_one_canonical_app_ask_status_history_outcome(native_app_http, tmp_path, monkeypatch, other_effect):
    client, agent, manager, database, starts, _, _ = native_app_http
    posting = asyncio.create_task(client.post("/apps/recover/agent/ask", json={"message": "Produce the original result"}))
    try:
        await until(lambda: any(session.app_id == "recover" and session._run.backend is not None
                    and session._run.backend.active_turn is not None for session in manager.sessions.values()))
        session = next(session for session in manager.sessions.values() if session.app_id == "recover")
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, session, token, tmp_path, monkeypatch)
        if other_effect:
            inject(tmp_path, child_start(), {"method": "item/started", "params": {"threadId": "child", "turnId": "child-turn", "item": {
                "id": "unconfirmed-write", "type": "mcpToolCall", "server": "connector", "tool": "write", "arguments": {}, "status": "inProgress"}}}, child_end())
        inject(tmp_path, item(ROOT, "turn-1", "final", "Canonical saved output"), terminal())
        response = await asyncio.wait_for(posting, 5)
        await wait_dispatch_idle(manager, session.id)
        saved = await database.read_session_result(session.id, pending.request_id)
        status = (await client.get(f"/apps/recover/agent/conversations/{session.id}/requests/{pending.request_id}")).json()
        assert saved.outcome.status == ("failed" if other_effect else "success")
        assert response.status_code == (502 if other_effect else 200)
        assert status["state"] == ("failed" if other_effect else "completed")
        assert status["outcome"] == saved.outcome.model_dump()
        assert status["reply"] == "Canonical saved output"
        history = await agent.get_conversation("recover", session.id)
        assert [row["text"] for row in history["messages"] if row["role"] == "assistant"] == ["Canonical saved output"]
        assert len(starts) == 1
    finally:
        await asyncio.gather(posting, return_exceptions=True)


async def test_exact_input_gap_keeps_named_delegation_original_result_consumption(native_runtime, tmp_path, monkeypatch):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    await manager.interrupt(parent.id)
    await AgentManager(database).create_agent(name="Input worker", backend="codex", user_id=parent.user_id)
    starts.clear()
    delegations = DelegationManager()
    delegations.bind(manager, database)
    try:
        work = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Input worker", request="Return once")
        child = manager.get_session(work.delegation_id)
        await until(lambda: child._run.backend is not None and child._run.backend.active_turn is not None)
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, child, token, tmp_path, monkeypatch)
        inject(tmp_path, item(ROOT, "turn-1", "final", "One delegated answer"), terminal())
        await wait_dispatch_idle(manager, child.id)
        await until(lambda: work.state == "completed")
        async with asyncio.timeout(3):
            while (delivered := await database.get_source_submission("delegation", f"agent-terminal:{work.work_id}", session_id=parent.id)) is None:
                await asyncio.sleep(0)
        assert delivered["payload"]["content"].endswith("One delegated answer")
        assert (await database.read_session_result(child.id, pending.request_id)).outcome.status == "success"
        assert len(starts) == 1
    finally:
        delegations.shutdown()


@pytest.mark.parametrize("other_effect", [False, True])
async def test_research_artifact_and_original_return_processing_stay_separate(native_runtime, tmp_path, monkeypatch, other_effect):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    generated = []
    async def pipeline(question, **kwargs):
        generated.append(question)
        return ResearchReport(question, "# Original report", [], [], [], 0, 0)
    monkeypatch.setattr(research_module, "run_research", pipeline)
    research = ResearchManager()
    research.bind(manager, database)
    try:
        job = await research.start(session.id, "Keep the report and process once")
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, session, token, tmp_path, monkeypatch)
        if other_effect:
            inject(tmp_path, {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
                "id": "unconfirmed-write", "type": "mcpToolCall", "server": "connector", "tool": "write", "arguments": {}, "status": "inProgress"}}})
        inject(tmp_path, item(ROOT, "turn-1", "final", "Report processed"), terminal())
        await research.wait_for_idle()
        saved = await database.get_research_job(job["id"])
        assert saved["status"] == "completed" and Path(saved["report_path"]).read_text() == "# Original report"
        assert saved["delivery_receipt"]["stage"] == ("failed" if other_effect else "processed")
        assert saved["delivery_receipt"]["request_id"] == pending.request_id
        assert generated == ["Keep the report and process once"] and len(starts) == 1
    finally:
        await research.shutdown()


async def test_native_input_gap_does_not_replace_wiki_effect_receipt(native_runtime, jobs, tmp_path, monkeypatch):
    _, _, _, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    manager = wiki_api.session_manager
    await AgentManager(manager.db).update_agent(jobs.registration["curator_agent_id"], user_id=jobs.user_id, backend="codex")
    starts.clear()
    job = enqueue(jobs)
    before = jobs.store.path("Library/note.md").read_bytes()
    processing = asyncio.create_task(process(jobs, job, manager))
    try:
        await until(lambda: any(session._run.backend is not None and session._run.backend.active_turn is not None
                               for session in manager.sessions.values()))
        session = next(session for session in manager.sessions.values() if session._run.backend is not None)
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, session, token, tmp_path, monkeypatch)
        inject(tmp_path, item(ROOT, "turn-1", "final", "Independent analysis saved. No publication was performed."), terminal())
        finished = await asyncio.wait_for(processing, 5)
        selected, = finished["items"]
        assert finished["status"] == "completed_with_attention" and selected["status"] == "attention"
        assert selected["worker_request"]["terminal"]["status"] == "success"
        assert selected["worker_request"]["terminal"]["recording_integrity"] == "incomplete"
        assert selected["worker_request"]["request_id"] == pending.request_id
        assert jobs.publication(job["id"], selected["doc"]) is None
        assert jobs.store.path("Library/note.md").read_bytes() == before and len(starts) == 1
    finally:
        await asyncio.gather(processing, return_exceptions=True)


@pytest.mark.parametrize("built", [False, True])
@pytest.mark.parametrize("other_effect", [False, True])
async def test_app_build_still_requires_its_entry_artifact(native_runtime, tmp_path, monkeypatch, built, other_effect):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    starts.clear()
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    session = None
    try:
        app = await apps.create_application(name="Original artifact gate", description="Build once", agent_id=parent.agent_id)
        session = manager.get_session(app["session_id"])
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        if built:
            (Path(app["app_dir"]) / app["entrypoint"]).write_text("<html>Original artifact</html>")
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, session, token, tmp_path, monkeypatch)
        if other_effect:
            inject(tmp_path, {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
                "id": "unconfirmed-write", "type": "mcpToolCall", "server": "connector", "tool": "write", "arguments": {}, "status": "inProgress"}}})
        inject(tmp_path, item(ROOT, "turn-1", "final", "Build work ended"), terminal())
        await wait_dispatch_idle(manager, session.id)
        async with asyncio.timeout(3):
            while (updated := await database.get_application(app["id"]))["status"] == "building":
                await asyncio.sleep(0)
        assert updated["status"] == ("ready" if built else "failed")
        assert (await database.read_session_result(session.id, pending.request_id)).outcome.status == ("failed" if other_effect else "success")
        assert len(starts) == 1
    finally:
        if session is not None:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
        apps.shutdown()


async def test_failed_native_schedule_consumes_only_its_original_fire(native_runtime, tmp_path, monkeypatch):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    starts.clear()
    runner = ScheduleRunner(manager, database)
    await save_schedule(database, schedule_id="original-native-fire", agent_id=parent.agent_id, name="One fire",
                        prompt="Run once", run_at="2026-11-01T01:00:00+00:00", created_at="2026-11-01")
    firing = asyncio.create_task(runner._fire("original-native-fire"))
    try:
        await until(lambda: any(session._run.backend is not None and session._run.backend.active_turn is not None
                               for session in manager.sessions.values()))
        session = next(session for session in manager.sessions.values() if session._run.backend is not None)
        token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
        pending = await cancel_original_input(manager, session, token, tmp_path, monkeypatch)
        inject(tmp_path, {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "id": "unconfirmed-write", "type": "mcpToolCall", "server": "connector", "tool": "write", "arguments": {}, "status": "inProgress"}}},
            item(ROOT, "turn-1", "final", "Original fire ended"), terminal())
        await asyncio.wait_for(firing, 5)
        saved = await database.read_session_result(session.id, pending.request_id)
        assert saved.outcome.status == "failed"
        assert await database.get_schedule_definition("original-native-fire") is None
        await runner._fire("original-native-fire")
        assert len(starts) == 1
    finally:
        await asyncio.gather(firing, return_exceptions=True)


@pytest.mark.parametrize("variant", ["exact", "new-version", "prerelease-version", "unknown-version", "missing-version",
                                    "unknown-meta", "wrong-call", "wrong-thread", "wrong-turn",
                                    "connector", "dynamic", "root-write", "child-write", "reader-cutoff", "wrong-scope",
                                    "answered", "normal-answer", "malformed-meta"])
async def test_cancelled_original_input_gap_keeps_raw_records_without_failing_saved_answer(
    native_runtime, tmp_path, monkeypatch, variant, short_process_shutdown,
):
    if variant == "reader-cutoff":
        from server.harness.run import HarnessRun

        handle = HarnessRun._handle_line
        async def truncated_reader(self, line):
            await handle(self, line)
            if '"method": "turn/completed"' in line:
                await asyncio.Event().wait()  # Real pipe reader cannot finish its prefix.
        monkeypatch.setattr(HarnessRun, "_handle_line", truncated_reader)
    manager, database, session, _, starts, events = native_runtime
    receipt = await start_output(native_runtime)
    token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
    extra_lease = None
    if variant == "wrong-scope":
        original_scope = verify(token)
        extra_lease = uuid.uuid4().hex
        register_run(extra_lease, session.id, session.user_id, original_scope.work_id)
        token = mint(session.id, user_id=session.user_id, run_lease_id=extra_lease, work_id=original_scope.work_id)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, readiness_token=token)
    monkeypatch.setattr(settings, "port", api.port)
    owner = manager._session_owners[id(session)]
    answer_observed = asyncio.Event()
    if variant == "answered":
        get = httpx.AsyncClient.get
        async def hold_answer_response(self, url, **kwargs):
            response = await get(self, url, **kwargs)
            if str(url).endswith("/answer") and response.status_code == 200:
                answer_observed.set()
                await asyncio.Event().wait()
            return response
        monkeypatch.setattr(httpx.AsyncClient, "get", hold_answer_response)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/ask/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    rpc_id = mcp._request_id
                    metadata = {
                        "callId": "exact-input-call", "threadId": ROOT,
                        "x-codex-turn-metadata": {"thread_id": ROOT, "turn_id": "turn-1", "codex_version": "0.166.0"},
                    }
                    if variant == "wrong-call":
                        metadata["callId"] = "another-call"
                    elif variant == "wrong-thread":
                        metadata["threadId"] = "another-thread"
                    elif variant == "wrong-turn":
                        metadata["x-codex-turn-metadata"]["turn_id"] = "another-turn"
                    elif variant == "new-version":
                        metadata["x-codex-turn-metadata"]["codex_version"] = "0.167.0"
                    elif variant == "prerelease-version":
                        metadata["x-codex-turn-metadata"]["codex_version"] = "0.168.0-alpha.1"
                    elif variant == "unknown-version":
                        metadata["x-codex-turn-metadata"]["codex_version"] = "unknown"
                    elif variant == "missing-version":
                        metadata["x-codex-turn-metadata"].pop("codex_version")
                    elif variant == "malformed-meta":
                        metadata["x-codex-turn-metadata"] = "unknown shape"
                    calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Original input?"}]},
                                                               meta=None if variant == "unknown-meta" else metadata))
                    try:
                        pending = await pending_question(manager, session)
                        call, = owner.questions.mcp_calls.values()
                        native_item = {
                            "id": "exact-input-call", "type": "mcpToolCall", "server": "ask", "tool": "user",
                            "arguments": {}, "status": "inProgress"}
                        if variant == "connector":
                            native_item.update(server="connector", tool="write")
                        elif variant == "dynamic":
                            native_item.update(type="dynamicToolCall", tool="exec")
                        inject(tmp_path, {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": native_item}})
                        await until(lambda: any(event.get("type") == "tool_use" for event in events))
                        if variant in {"answered", "normal-answer"}:
                            assert await manager.answer_question(session.id, pending.question_id, [{"text": "Accepted original answer"}])
                            if variant == "answered":
                                await asyncio.wait_for(answer_observed.wait(), 2)
                        if variant == "normal-answer":
                            await calling
                        else:
                            await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                                method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id))))
                            with pytest.raises(McpError, match="Request cancelled"):
                                await calling
                        await asyncio.gather(call.task, return_exceptions=True)
                        await until(lambda: call.id not in owner.questions.mcp_calls)
                        if variant in {"root-write", "child-write"}:
                            if variant == "child-write":
                                inject(tmp_path, child_start())
                            inject(tmp_path, {"method": "item/started", "params": {
                                "threadId": "child" if variant == "child-write" else ROOT,
                                "turnId": "child-turn" if variant == "child-write" else "turn-1",
                                "item": {"id": "write-call", "type": "mcpToolCall", "server": "connector", "tool": "write",
                                         "arguments": {}, "status": "inProgress"}}})
                            if variant == "child-write":
                                inject(tmp_path, child_end())
                        inject(tmp_path, item(ROOT, "turn-1", "answer", "The saved answer."), terminal())
                        await wait_dispatch_idle(manager, session.id)
                        saved = await database.read_session_result(session.id, receipt["request_id"])
                        qualified = variant in {"exact", "answered", "new-version", "prerelease-version", "unknown-version", "missing-version"}
                        assert saved.outcome.status == ("success" if qualified else "failed")
                        assert saved.fragments == ("The saved answer.",)
                        assert saved.outcome.native_status == "completed"
                        assert saved.outcome.recording_integrity == "incomplete"
                        assert saved.outcome.recording_issues == {"native_collection_incomplete": 1}
                        gap, = saved.outcome.recording_locations
                        if variant == "reader-cutoff":
                            assert "origin" not in gap and "local_input_cleanup" not in gap
                        else:
                            assert gap["missing_items"][0]["item_id"] == "exact-input-call"
                        if qualified:
                            proof, = gap["local_input_cleanup"]
                            assert proof["question_id"] == pending.question_id
                            assert proof["disposition"] == ("answered" if variant == "answered" else "closed")
                            assert proof["runner_retired"] is True
                            assert proof["provider_consumption"] == "unknown"
                            live_result, = [event for event in events if event.get("terminal")]
                            assert live_result["content"]["status"] == saved.outcome.status
                            if variant == "answered":
                                question_result = await database.get_question_result(session.id, pending.question_id)
                                assert question_result["resolution"]["disposition"] == "answered"
                                assert "Accepted original answer" in question_result["answer"]
                        else:
                            assert "local_input_cleanup" not in gap
                    finally:
                        calling.cancel()
                        await asyncio.gather(calling, return_exceptions=True)
    finally:
        await api.stop()
        if extra_lease is not None:
            revoke_run(extra_lease)


@pytest.mark.parametrize("unknown_scope", [False, True])
async def test_stop_keeps_original_app_capacity_until_mcp_waiter_cleanup_is_consumed(native_app_http, tmp_path, monkeypatch, unknown_scope):
    client, agent, manager, database, starts, _, _ = native_app_http
    posting = asyncio.create_task(client.post("/apps/recover/agent/ask", json={"message": "Wait for input"}))
    await until(lambda: bool(agent.list_conversations("recover")))
    sid = agent.list_conversations("recover")[0]["id"]
    session = manager.get_session(sid)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    token = next(entry.credential for entry in starts[-1].mcp_servers if entry.key == "ask")
    extra_lease = None
    if unknown_scope:
        original_scope = verify(token)
        extra_lease = uuid.uuid4().hex
        register_run(extra_lease, sid, session.user_id, original_scope.work_id)
        token = mint(sid, user_id=session.user_id, run_lease_id=extra_lease, work_id=original_scope.work_id)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, readiness_token=token)
    monkeypatch.setattr(settings, "port", api.port)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/ask/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    rpc_id = mcp._request_id
                    calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Do not retire early"}]}))
                    pending = await pending_question(manager, session)
                    owner = manager._session_owners[id(session)]
                    execution = owner.runtime.current
                    call, = owner.questions.mcp_calls.values()
                    released = pending.waiters_released.wait
                    async def hold_release():
                        entered.set()
                        await release.wait()
                        await released()
                    monkeypatch.setattr(pending.waiters_released, "wait", hold_release)
                    try:
                        await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                            method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id))))
                        with pytest.raises(McpError, match="Request cancelled"):
                            await calling
                        await asyncio.wait_for(entered.wait(), 2)
                        await manager.interrupt(sid)
                        await until(lambda: execution.retirement is not None and execution.retirement.done())
                        # Observe an Owner command after process retirement;
                        # task.done alone may precede its next advance cycle.
                        assert not await manager.answer_question(sid, pending.question_id, [{"text": "Too late"}])
                        assert not execution.retired.done(), "Execution retired before original MCP cleanup"
                        assert agent.list_conversations("recover")[0]["running"]
                        assert call.id in owner.questions.mcp_calls and not call.task.done()
                        release.set()
                        await asyncio.wait_for(call.consumed.wait(), 2)
                        await manager.await_session_execution(sid, timeout=5)
                        await until(lambda: not agent.list_conversations("recover")[0]["running"])
                        result = await database.read_session_result(sid, execution.request.request_id)
                        assert result.outcome.status == "interrupted"
                        assert result.outcome.code == "interrupted"
                    finally:
                        release.set()
                        calling.cancel()
                        await asyncio.gather(calling, call.task, return_exceptions=True)
    finally:
        release.set()
        await api.stop()
        await asyncio.gather(posting, return_exceptions=True)
        if extra_lease is not None:
            revoke_run(extra_lease)

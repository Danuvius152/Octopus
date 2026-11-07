"""Completion consumers verify artifacts/effects after original native retirement."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agent_manager import AgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.harness import codex_app_server, get_harness
from server.harness import run as run_module
from server.mcp_identity import scope_is_live, verify
from server.scheduler import ScheduleRunner
from server.wiki.jobs import WikiWorker
from tests import native_app_server as native_tests
from tests import wiki_api as wiki_api
from tests.native_app_server import (
    child_start,
    inject,
    question_request,
    terminal,
    until,
)
from tests.test_scheduler import save_schedule
from tests.test_wiki_completion_handoff import original_job
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs

native_runtime = native_tests.native_runtime


async def close_root(manager, session, end, root, monkeypatch):
    if end == "question":
        inject(root, question_request("effect-question"))
        await until(lambda: manager.get_pending_questions(session.id))
        assert manager.session_is_executing(session)
    if end == "notification":
        publisher = manager._session_owner(session).publisher
        publish = publisher._broadcast

        async def unavailable(event, **kwargs):
            if event.get("terminal"):
                raise OSError("Original browser notification unavailable")
            await publish(event, **kwargs)

        monkeypatch.setattr(publisher, "_broadcast", unavailable)
    if end in {"stop", "cancelled"}:
        await manager.interrupt(session.id)
    else:
        inject(root, terminal("completed" if end in {"notification", "question"} else end))


def outcome_status(end):
    return "failed" if end == "failed" else "interrupted" if end in {"stop", "cancelled"} else "success"


@pytest.fixture
async def retirement_gate(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    original = run_module.wait_group_exit

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        (tmp_path / "native-group-release").touch()
        return await original(*args, **kwargs)

    monkeypatch.setattr(run_module, "wait_group_exit", held)
    try:
        yield entered, release
    finally:
        release.set()
        (tmp_path / "native-group-release").touch()


@pytest.mark.parametrize("artifact", [False, True])
@pytest.mark.parametrize("end", ["completed", "failed", "stop", "notification", "question"])
async def test_app_build_checks_artifact_after_root_and_helper_retirement(native_runtime, retirement_gate, tmp_path, monkeypatch, artifact, end):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_group"
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    starts.clear()
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    completions = []

    async def completed(session_id, completion):
        completions.append(completion)

    manager.on_request_complete("artifact-proof", completed)
    try:
        app = await apps.create_application(name="Artifact", description="Build the real entrypoint", agent_id=parent.agent_id)
        session = manager.get_session(app["session_id"])
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        await until(lambda: (tmp_path / "native-group-ready").exists())
        run = session._run.backend
        scopes = [scope for entry in starts[-1].mcp_servers if entry.credential
                  and (scope := verify(entry.credential)) is not None]
        assert scopes and all(scope_is_live(scope) for scope in scopes)
        if artifact:
            (Path(app["app_dir"]) / app["entrypoint"]).write_text("<html>Built</html>")
        inject(tmp_path, child_start())
        await close_root(manager, session, end, tmp_path, monkeypatch)
        await asyncio.wait_for(retirement_gate[0].wait(), 3)
        assert not completions
        assert (await database.get_application(app["id"]))["status"] == "building"
        assert not run.question_admission_open
        assert all(not scope_is_live(scope) for scope in scopes)
        retirement_gate[1].set()
        await until(lambda: completions)
        assert len(completions) == len(starts) == 1
        assert completions[0].recording_state == "committed"
        assert completions[0].outcome.status == outcome_status(end)
        assert (completions[0].notification_state == "failed") == (end == "notification")
        async with asyncio.timeout(3):
            while (updated := await database.get_application(app["id"]))["status"] == "building":
                await asyncio.sleep(.005)
        assert updated["status"] == ("ready" if artifact else "failed")
        assert run.retirement_state == "proven"
        cards = [row["tool_input"] for row in await database.load_messages(session.id) if row["type"] == "native_subagent"]
        assert cards[-1]["status"] == "interrupted"
    finally:
        retirement_gate[1].set()
        apps.shutdown()


@pytest.mark.parametrize("end", ["completed", "failed", "stop", "notification", "question"])
async def test_schedule_records_original_result_and_archives_only_after_retirement(native_runtime, retirement_gate, tmp_path, monkeypatch, end):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_group"
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    starts.clear()
    runner = ScheduleRunner(manager, database)
    await save_schedule(database, schedule_id="native-schedule", agent_id=parent.agent_id,
        name="Native schedule", prompt="Use native waiting", interval_seconds=60, created_at="2026-11-01T00:00:00+00:00")
    operation = asyncio.create_task(runner._fire("native-schedule"))
    try:
        await until(lambda: any(s.origin == "schedule" and s._run.backend is not None
                               and s._run.backend.active_turn is not None for s in manager.sessions.values()))
        session = next(s for s in manager.sessions.values() if s.origin == "schedule")
        await until(lambda: (tmp_path / "native-group-ready").exists())
        run = session._run.backend
        scopes = [scope for entry in starts[-1].mcp_servers if entry.credential
                  and (scope := verify(entry.credential)) is not None]
        assert scopes and all(scope_is_live(scope) for scope in scopes)
        inject(tmp_path, child_start())
        await close_root(manager, session, end, tmp_path, monkeypatch)
        await asyncio.wait_for(retirement_gate[0].wait(), 3)
        assert not operation.done() and manager.get_session(session.id) is session
        assert all(not scope_is_live(scope) for scope in scopes)
        assert (await database.get_schedule_definition("native-schedule"))["last_run_at"] is None
        retirement_gate[1].set()
        await asyncio.wait_for(operation, 4)
        rows = await database.load_messages(session.id)
        outcomes = [row["content"] for row in rows if row["type"] == "result"]
        assert len(outcomes) == len(starts) == 1
        assert outcomes[0]["status"] == outcome_status(end)
        assert (await database.get_schedule_definition("native-schedule"))["last_run_at"] is not None
        assert run.retirement_state == "proven"
        # Stop deliberately pauses dispatch and leaves the conversation visible.
        assert (manager.get_session(session.id) is None) == (end != "stop")
    finally:
        retirement_gate[1].set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.fixture
async def native_wiki(jobs, client, tmp_path, monkeypatch):
    manager = wiki_api.session_manager
    await manager.db.update_agent(jobs.registration["curator_agent_id"], backend="codex")
    runtime = SimpleNamespace(manager=manager, jobs=jobs, client=client, starts=[], completions=[])

    def argv(context):
        runtime.starts.append(context)
        return [sys.executable, "-u", str(native_tests.PEER), str(tmp_path), "control_group"], {"cwd": context.working_dir}

    async def completed(session_id, completion):
        runtime.completions.append(completion)

    monkeypatch.setattr(codex_app_server, "build_argv", argv)
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(codex_app_server.profile(harness.profile),
        build_turn_argv=lambda context: codex_app_server.build_argv(context)))
    manager.on_request_complete("native-wiki-effect", completed)
    worker = WikiWorker()
    worker.manager = manager
    runtime.worker = worker
    try:
        yield runtime
    finally:
        (tmp_path / "native-group-release").touch()
        await manager.drain_turns(timeout=4)
        await manager.close_session_owners()


@pytest.mark.parametrize("effect,end", [(True, "completed"), (False, "completed"),
                                       (True, "failed"), (True, "cancelled"),
                                       (True, "notification"), (True, "question")])
async def test_wiki_grant_completion_retirement_and_effect_are_independent(native_wiki, retirement_gate, tmp_path, monkeypatch, effect, end):
    runtime = native_wiki
    job, original = original_job(runtime, "native-effect")
    operation = asyncio.create_task(runtime.worker.process(runtime.jobs, job["id"]))
    try:
        await until(lambda: runtime.starts and (tmp_path / "native-group-ready").exists())
        context = runtime.starts[0]
        bearer = next(entry.credential for entry in context.mcp_servers if entry.key == "wiki")
        scope = verify(bearer)
        assert scope is not None and scope_is_live(scope)
        session = runtime.manager.get_session(scope.session_id)
        headers = {"Authorization": "Bearer " + bearer}
        target = runtime.jobs.get(job["id"])["items"][0]
        read = await runtime.client.get("/api/wiki/read", params={"doc": target["doc"]}, headers=headers)
        assert read.status_code == 200
        payload = {"path": target["path"], "expected_revision": read.json()["revision"],
                   "key_note": "Preserve this exact argument and its condition.", "reason": "Verify the authorized original"}
        if effect:
            reply = await runtime.client.post("/api/wiki/organize", headers=headers, json=payload)
            assert reply.status_code == 200, reply.text
        output = runtime.jobs.store.path(target["path"]).read_bytes()
        inject(tmp_path, child_start())
        if end == "cancelled":
            runtime.jobs.mark_cancelled(job["id"])
        await close_root(runtime.manager, session, end, tmp_path, monkeypatch)
        await asyncio.wait_for(retirement_gate[0].wait(), 3)
        assert not operation.done() and not runtime.completions
        assert not scope_is_live(scope)
        denied = await runtime.client.post("/api/wiki/organize", headers=headers, json=payload)
        assert denied.status_code in {401, 403, 409}
        assert runtime.jobs.store.path(target["path"]).read_bytes() == output
        retirement_gate[1].set()
        await asyncio.wait_for(operation, 5)
        saved = runtime.jobs.get(job["id"])
        target = saved["items"][0]
        assert len(runtime.completions) == len(runtime.starts) == 1
        completion = runtime.completions[0]
        assert completion.outcome.status == outcome_status(end)
        assert (completion.notification_state == "failed") == (end == "notification")
        assert target["worker_request"]["retired"] and target["worker_request"]["recording_state"] == "committed"
        assert target["worker_request"]["terminal"] == completion.outcome.model_dump()
        complete = effect and outcome_status(end) == "success"
        assert (target["status"] == "done") == complete
        assert saved["status"] == ("cancelled" if end == "cancelled" else "completed" if complete else "completed_with_attention")
        assert runtime.jobs.store.path(target["path"]).read_bytes() == output
        if effect:
            assert runtime.jobs.publication(job["id"], target["doc"]) is not None
        else:
            assert not saved["receipts"] and output == original
    finally:
        retirement_gate[1].set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)

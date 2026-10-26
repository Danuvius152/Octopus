"""Tests for the /api/agents routes (agent-refactor.md §5.4 / §8)."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.deps import set_user_manager
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager
from server.users import UserManager
from tests.fake_run import FakeRunBase
from tests.test_session_retirement import _retire_original_test_process


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    owner = await users.create_user(username="agent-owner", password="test-password", is_admin=True)
    set_user_manager(users)
    token = await users.issue_token(owner["id"])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as c:
        yield c

    await runner.shutdown()
    await db.close()


async def _create_agent(client, name="Researcher", **extra):
    body = {"name": name, **extra}
    resp = await client.post("/api/agents", json=body, headers=client.headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Default Agent ---


@pytest.mark.asyncio
async def test_default_agent_present(client):
    resp = await client.get("/api/agents", headers=client.headers)
    assert resp.status_code == 200
    agents = resp.json()
    system = [a for a in agents if a["is_system"]]
    assert len(system) == 1
    assert system[0]["name"] == "Octo"
    # Default built-in MCP set: ask (user questions) + bg (cross-turn
    # shell) + ask_agent (delegate to another agent —
    # agent-collaboration.md §5.1). These are creation defaults; saved
    # agent configurations remain under the user's control.
    assert system[0]["mcp_servers"] == ["ask", "bg", "ask_agent", "research", "schedule"]


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/agents", headers={"Authorization": ""})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_default_agent_backend_is_claude(client):
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    system = next(a for a in agents if a["is_system"])
    assert system["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_new_agents_and_sessions_use_the_configured_backend(client, monkeypatch):
    from server.config import settings

    monkeypatch.setattr(settings, "default_backend", "codex")
    backends = await client.get("/api/backends", headers=client.headers)
    assert backends.json()["default"] == "codex"

    agent = await _create_agent(client, name="Configured Codex")
    assert agent["backend"] == "codex"
    session = await client.post(
        f"/api/agents/{agent['id']}/sessions", json={}, headers=client.headers,
    )
    assert session.status_code == 201
    assert session.json()["backend"] == "codex"

    explicit = await _create_agent(client, name="Explicit Claude", backend="claude-code")
    assert explicit["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_new_install_uses_configured_backend_without_overwriting_saved_agents(
    tmp_path, monkeypatch
):
    from server.config import settings

    monkeypatch.setattr(settings, "default_backend", "codex")
    path = str(tmp_path / "configured.db")
    db = Database(path)
    await db.initialize()
    try:
        owner = await UserManager(db).create_user(username="backend-owner", password="test-password")
        assert (await db.get_system_agent())["backend"] == "codex"
        agent = await AgentManager(db).create_agent(name="Manager default", user_id=owner["id"])
        assert agent["backend"] == "codex"
    finally:
        await db.close()

    monkeypatch.setattr(settings, "default_backend", "claude-code")
    db = Database(path)
    await db.initialize()
    try:
        assert (await db.get_system_agent())["backend"] == "codex"
        assert (await db.get_agent(agent["id"]))["backend"] == "codex"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agent_backend_create_and_update(client):
    a = await _create_agent(client, name="Codex Agent", backend="codex")
    assert a["backend"] == "codex"
    # round-trips through GET
    got = (await client.get(f"/api/agents/{a['id']}", headers=client.headers)).json()
    assert got["backend"] == "codex"
    # PATCH switches it back
    patched = await client.patch(
        f"/api/agents/{a['id']}", json={"backend": "claude-code"}, headers=client.headers
    )
    assert patched.status_code == 200
    assert patched.json()["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_session_inherits_agent_backend(client):
    """A session created without an explicit backend inherits the agent's
    default; an explicit per-session backend still wins."""
    a = await _create_agent(client, name="Codex Default", backend="codex")

    inherited = await client.post(
        f"/api/agents/{a['id']}/sessions", json={"name": "inherit"}, headers=client.headers
    )
    assert inherited.status_code == 201, inherited.text
    assert inherited.json()["backend"] == "codex"

    overridden = await client.post(
        f"/api/agents/{a['id']}/sessions",
        json={"name": "override", "backend": "claude-code"},
        headers=client.headers,
    )
    assert overridden.status_code == 201, overridden.text
    assert overridden.json()["backend"] == "claude-code"


# --- CRUD ---


@pytest.mark.asyncio
async def test_create_and_get_agent(client):
    agent = await _create_agent(
        client,
        name="Inbox Bot",
        system_prompt="You triage email.",
        model="claude-opus-4-7",
        tool_deny="Bash",
    )
    assert agent["name"] == "Inbox Bot"
    assert agent["system_prompt"] == "You triage email."
    assert agent["model"] == "claude-opus-4-7"
    assert agent["tool_allow"] == ""
    assert agent["tool_deny"] == "Bash"
    assert agent["is_system"] is False
    assert agent["active_session_count"] == 0

    got = await client.get(f"/api/agents/{agent['id']}", headers=client.headers)
    assert got.status_code == 200
    assert got.json()["id"] == agent["id"]


@pytest.mark.asyncio
async def test_get_unknown_agent_404(client):
    resp = await client.get("/api/agents/nope", headers=client.headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_duplicate_name_rejected(client):
    await _create_agent(client, name="Dup")
    resp = await client.post("/api/agents", json={"name": "Dup"}, headers=client.headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_agent(client):
    agent = await _create_agent(client, name="Patchable", model="claude-opus-4-7")
    resp = await client.patch(
        f"/api/agents/{agent['id']}",
        json={"system_prompt": "new prompt", "model": None},
        headers=client.headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["system_prompt"] == "new prompt"
    # Explicit null clears the nullable model field.
    assert data["model"] is None


@pytest.mark.asyncio
async def test_patch_duplicate_name_rejected(client):
    await _create_agent(client, name="First")
    second = await _create_agent(client, name="Second")
    resp = await client.patch(
        f"/api/agents/{second['id']}", json={"name": "First"}, headers=client.headers
    )
    assert resp.status_code == 400


# --- is_system protection ---


@pytest.mark.asyncio
async def test_default_agent_cannot_be_archived_or_deleted(client):
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    default = next(a for a in agents if a["is_system"])
    arch = await client.post(f"/api/agents/{default['id']}/archive", headers=client.headers)
    assert arch.status_code == 400
    dele = await client.delete(f"/api/agents/{default['id']}", headers=client.headers)
    assert dele.status_code == 400


# --- delete / archive ---


@pytest.mark.asyncio
async def test_delete_empty_agent(client):
    agent = await _create_agent(client, name="Throwaway")
    resp = await client.delete(f"/api/agents/{agent['id']}", headers=client.headers)
    assert resp.status_code == 204
    assert (await client.get(f"/api/agents/{agent['id']}", headers=client.headers)).status_code == 404


@pytest.mark.asyncio
async def test_delete_agent_with_sessions_refused(client):
    agent = await _create_agent(client, name="Busy")
    await client.post(
        f"/api/agents/{agent['id']}/sessions", json={"name": "s"}, headers=client.headers
    )
    resp = await client.delete(f"/api/agents/{agent['id']}", headers=client.headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_archive_agent_cascades_sessions(client):
    agent = await _create_agent(client, name="Archivable")
    sess = (
        await client.post(
            f"/api/agents/{agent['id']}/sessions", json={"name": "s"}, headers=client.headers
        )
    ).json()
    resp = await client.post(f"/api/agents/{agent['id']}/archive", headers=client.headers)
    assert resp.status_code == 200

    # Its session is no longer in the live list.
    live = (await client.get("/api/sessions", headers=client.headers)).json()
    assert sess["id"] not in [s["id"] for s in live]
    # But it's there as archived.
    arch = (
        await client.get("/api/sessions?include_archived=true", headers=client.headers)
    ).json()
    assert sess["id"] in [s["id"] for s in arch]


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_archive_agent_storage_refusal_keeps_agent_and_session_live(tmp_path, committed):
    proof, status, messages, artifacts = await _cold_archive_fault(
        tmp_path, "storage-after" if committed else "storage-before",
    )
    assert proof["selected_outcome"]["status"] == "interrupted"
    assert proof["selected_outcome"]["request_id"] == proof["request_id"]
    assert proof["model"]["pid"] == proof["process_group"] == proof["model"]["process_group"]
    assert (artifacts / "actual-commit-applied").exists() is committed
    if committed:
        assert status["execution"]["state"] == "done"
        assert status["outcome"] == proof["selected_outcome"]
        assert [message["content"] for message in messages if message["type"] == "result"] == [proof["selected_outcome"]]
    else:
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["recoverable_input"] is False
        assert "outcome" not in status
        assert not [message for message in messages if message["type"] == "result"]
    assert not (artifacts / "result-published").exists()


@pytest.mark.asyncio
async def test_archive_agent_retirement_refusal_keeps_agent_and_session_live(tmp_path):
    proof, status, messages, artifacts = await _cold_archive_fault(tmp_path, "retirement")
    assert proof["model"] is None, "This isolated case owns a stand-in Harness resource, not a model PID"
    assert status["execution"]["state"] == "done"
    assert status["outcome"]["status"] == "interrupted"
    assert status["outcome"]["request_id"] == proof["request_id"]
    assert [message["content"] for message in messages if message["type"] == "result"] == [status["outcome"]]
    assert (artifacts / "stop-attempts.jsonl").read_text().splitlines() == ["Backend retirement remains unconfirmed"]
    diagnostic = (artifacts / "service.log").read_text()
    assert "Backend retirement remains unconfirmed" in diagnostic and "Session owner failed" in diagnostic


async def _cold_archive_fault(tmp_path, mode):
    from fastapi import FastAPI

    from server.deps import get_session_manager
    from server.routers import sessions as sessions_mod
    from server.sessions import SessionManager
    from server.sessions.client_submissions import (
        submission_status,
        submit_client_message,
    )

    artifacts = tmp_path / "agent-archive-fault"
    artifacts.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
        str(Path(__file__).resolve()), "-o", "python_functions=_agent_archive_fault_child",
        "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "OCTOPUS_TEST_AGENT_ARCHIVE_FAULT": mode},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 15)
        (artifacts / "child.log").write_bytes(stdout + stderr)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        proof = json.loads((artifacts / "proof.json").read_text())
        assert proof["mode"] == mode and (artifacts / "archive-entered").exists()
        assert not (artifacts / "agent-archived").exists()
        assert not (artifacts / "session-archived").exists()
        if (artifacts / "archive-response.json").exists():
            refusal = json.loads((artifacts / "archive-response.json").read_text())
            assert refusal["status_code"] == 409
        else:
            assert not (artifacts / "archive-returned").exists()
        starts = (artifacts / "starts.jsonl").read_text().splitlines()
        assert len(starts) == 1
        db = Database(proof["database"])
        manager = SessionManager()
        try:
            await db.initialize()
            users = UserManager(db)
            set_user_manager(users)
            token = await users.issue_token(proof["user_id"])
            await manager.initialize(db)
            application = FastAPI()
            application.include_router(agents_mod.router)
            application.include_router(sessions_mod.router)
            application.dependency_overrides[get_session_manager] = lambda: manager
            with pytest.MonkeyPatch.context() as routes:
                routes.setattr(agents_mod, "_manager", AgentManager(db))
                async with AsyncClient(transport=ASGITransport(application), base_url="http://test",
                                       headers={"Authorization": f"Bearer {token}"}) as http:
                    agent = await http.get(f"/api/agents/{proof['agent_id']}", headers=http.headers)
                    assert agent.status_code == 200 and agent.json()["archived"] is False
                    detail = await http.get(f"/api/sessions/{proof['session_id']}", headers=http.headers)
                    assert detail.status_code == 200 and detail.json()["id"] == proof["session_id"]
            assert await db.get_agent(proof["agent_id"]) == proof["agent_row_before"]
            row = next(row for row in await db.load_sessions(include_archived=True) if row["id"] == proof["session_id"])
            assert row == proof["session_row_before"] and not row["archived"]
            assert manager.get_session(proof["session_id"]) is not None
            status = await submission_status(manager, proof["session_id"], "agent-original-input")
            assert status["accepted"] is True and status["request_id"] == proof["request_id"]
            assert status["input"]["content"] == "Archive this original work once"
            assert status["execution"]["execution_owner_request_id"] == proof["request_id"]
            duplicate = await submit_client_message(
                manager, proof["session_id"], "Archive this original work once", [],
                "agent-original-input", wiki_mode=None,
            )
            assert duplicate == status
            messages = await db.load_messages(proof["session_id"])
            assert [message["content"] for message in messages if message["role"] == "user"] == ["Archive this original work once"]
            assert [message["content"] for message in messages if message["role"] == "assistant"] == ["Original agent work is running."]
            assert manager.dispatch_snapshot(proof["session_id"])["items"] == []
            assert not manager.session_is_executing(manager.get_session(proof["session_id"]))
            if mode == "storage-before":
                with pytest.raises(ValueError, match="process exit is unconfirmed"):
                    await manager.start_message(proof["session_id"], "Do not replace the unknown original writer")
            assert (artifacts / "starts.jsonl").read_text().splitlines() == starts
            return proof, status, messages, artifacts
        finally:
            await db.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)
        if (artifacts / "model.json").exists():
            await _retire_original_test_process(json.loads((artifacts / "model.json").read_text()))


async def _agent_archive_fault_child(tmp_path, tmp_path_factory, monkeypatch):
    import logging

    from fastapi import FastAPI

    from server.deps import get_session_manager
    from server.harness import HarnessEvent, get_harness
    from server.routers import sessions as sessions_mod
    from server.sessions import SessionManager
    from server.sessions.client_submissions import submit_client_message

    artifacts = tmp_path_factory.getbasetemp().parent
    mode = os.environ["OCTOPUS_TEST_AGENT_ARCHIVE_FAULT"]
    database_path = tmp_path / "agent-archive.db"
    db = Database(str(database_path))
    await db.initialize()
    users = UserManager(db)
    owner = await users.create_user(username="archive-owner", password="test-password")
    await db.update_user_field(owner["id"], extra_roots=json.dumps([str(tmp_path)]))
    set_user_manager(users)
    token = await users.issue_token(owner["id"])
    manager = SessionManager()
    await manager.initialize(db)
    application = FastAPI()
    application.include_router(agents_mod.router)
    application.include_router(sessions_mod.router)
    application.dependency_overrides[get_session_manager] = lambda: manager
    agents_mod.set_manager(AgentManager(db))
    handler = logging.FileHandler(artifacts / "service.log")
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    logging.getLogger("server").addHandler(handler)
    ready = asyncio.Event()
    proof = {"database": str(database_path), "mode": mode, "model": None, "user_id": owner["id"]}

    def save_proof():
        (artifacts / "proof.json").write_text(json.dumps(proof))

    def record_start(prompt):
        with (artifacts / "starts.jsonl").open("a") as stream:
            stream.write(json.dumps(prompt) + "\n")

    async def observe(event):
        kind = event.get("type")
        if kind == "assistant_text" and event.get("content") == "Original agent work is running.":
            ready.set()
        if kind in {"result", "agent_archived", "session_archived"}:
            (artifacts / {"result": "result-published", "agent_archived": "agent-archived", "session_archived": "session-archived"}[kind]).touch()

    manager.on_broadcast("agent-archive-fault-proof", observe)
    if mode == "retirement":
        failure = RuntimeError("Backend retirement remains unconfirmed")

        class UnreapedRun(FakeRunBase):
            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                record_start(prompt)

            async def stream(self):
                yield HarnessEvent(type="text", content="Original agent work is running.")
                await asyncio.Event().wait()

            async def interrupt(self):
                pass

            async def stop(self):
                with (artifacts / "stop-attempts.jsonl").open("a") as stream:
                    stream.write(str(failure) + "\n")
                proof["selected_outcome"] = await db.get_committed_terminal(proof["session_id"], proof["request_id"])
                assert manager.get_session(proof["session_id"]) is not None
                assert not (await db.get_agent(proof["agent_id"]))["archived"]
                save_proof()
                raise failure

        monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: UnreapedRun())
    else:
        code = (
            "import json,os,sys,time\n"
            "from pathlib import Path\n"
            "with Path(sys.argv[2]).open('a') as stream: stream.write(sys.argv[3]+'\\n')\n"
            "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
            "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'process_group':os.getpgrp(),'start_time':fields[19]}))\n"
            "print(json.dumps({'type':'thread.started','thread_id':'agent-archive-thread'}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Original agent work is running.'}}),flush=True)\n"
            "time.sleep(60)\n"
        )

        def argv(context):
            return [sys.executable, "-u", "-c", code, str(artifacts / "model.json"), str(artifacts / "starts.jsonl"), json.dumps(context.prompt)], {"cwd": context.working_dir}

        harness = get_harness("codex")
        monkeypatch.setattr(harness, "profile", replace(
            harness.profile, build_turn_argv=argv, prepare_resume=None, activity_observer=None,
        ))

    async with AsyncClient(transport=ASGITransport(application), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as http:
        agent = await _create_agent(
            http, name="Original archive owner", backend="codex",
            mcp_servers=["ask", "bg", "ask_agent", "research", "schedule"],
        )
        created = await http.post(
            f"/api/agents/{agent['id']}/sessions", headers=http.headers,
            json={"name": "Original archive session", "working_dir": str(tmp_path), "backend": "codex"},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        admitted = await submit_client_message(
            manager, session_id, "Archive this original work once", [], "agent-original-input", wiki_mode=None,
        )
        assert admitted["accepted"] is True
        await asyncio.wait_for(ready.wait(), 5)
        original = await db.get_client_submission(session_id, "agent-original-input")
        proof.update(agent_id=agent["id"], session_id=session_id, request_id=admitted["request_id"],
                     agent_row_before=await db.get_agent(agent["id"]),
                     session_row_before=next(row for row in await db.load_sessions(include_archived=True) if row["id"] == session_id),
                     process_group=original["process_group"])
        if mode != "retirement":
            proof["model"] = json.loads((artifacts / "model.json").read_text())
        save_proof()
        if mode.startswith("storage-"):
            terminal = None
            execute, commit = db.conn.execute, db.conn.commit

            async def terminal_sql(sql, parameters=()):
                nonlocal terminal
                cursor = await execute(sql, parameters)
                if sql.startswith("INSERT INTO messages ") and parameters[3] == "result":
                    assert parameters[-1] == admitted["request_id"]
                    terminal = json.loads(parameters[4])
                return cursor

            async def lost_terminal_commit_acknowledgment():
                if terminal is None:
                    return await commit()
                proof["selected_outcome"] = terminal
                save_proof()
                if mode == "storage-after":
                    await commit()
                    (artifacts / "actual-commit-applied").touch()
                raise OSError("actual agent archive terminal commit acknowledgment lost")

            monkeypatch.setattr(db.conn, "execute", terminal_sql)
            monkeypatch.setattr(db.conn, "commit", lost_terminal_commit_acknowledgment)
        (artifacts / "archive-entered").touch()
        response = await http.post(f"/api/agents/{agent['id']}/archive", headers=http.headers)
        (artifacts / "archive-response.json").write_text(json.dumps({"status_code": response.status_code, "body": response.json()}))
        (artifacts / "archive-returned").touch()
        assert response.status_code != 200
        # The original failed resource remains owned; no later failure or map
        # clearing is used to turn a retirement refusal into a successful close.
        await asyncio.wait_for(asyncio.Event().wait(), 8)


@pytest.mark.asyncio
async def test_protected_agent_is_validated_before_session_eviction(client):
    system = await session_manager.db.get_system_agent()
    session = await session_manager.create_session(system["id"], "Protected queue")
    await session_manager.interrupt(session.id)
    receipt = await session_manager.start_message(session.id, "Queued result", source="bg")
    response = await client.post(f"/api/agents/{system['id']}/archive", headers=client.headers)
    assert response.status_code == 400
    assert session_manager.get_session(session.id) is session
    assert session_manager.dispatch_snapshot(session.id)["items"][0]["request_id"] == receipt.request_id


@pytest.mark.asyncio
async def test_cancelled_archive_waiter_still_finishes_agent_and_session_archive(client, monkeypatch):
    agent = await _create_agent(client, name="Owned archive")
    session = await session_manager.create_session(agent["id"], "Captured session")
    database = session_manager.db
    archive = database.archive_agent
    entered, release = asyncio.Event(), asyncio.Event()
    notices = []

    async def held_archive(agent_id):
        entered.set()
        await release.wait()
        await archive(agent_id)

    async def observe(message):
        if message.get("type") == "agent_archived" and message.get("agent_id") == agent["id"]:
            notices.append(message)

    monkeypatch.setattr(database, "archive_agent", held_archive)
    session_manager.on_broadcast("archive-completion-observer", observe)
    request = asyncio.create_task(client.post(f"/api/agents/{agent['id']}/archive", headers=client.headers))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert session_manager.get_session(session.id) is None
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        async with asyncio.timeout(1):
            while not (await session_manager.db.get_agent(agent["id"]))["archived"] or not notices:
                await asyncio.sleep(0)
        rows = await session_manager.db.load_sessions(include_archived=True)
        assert next(row for row in rows if row["id"] == session.id)["archived"]
        assert len(notices) == 1
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        session_manager.remove_broadcast("archive-completion-observer")


# --- agent-scoped sessions & schedules ---


@pytest.mark.asyncio
async def test_create_session_under_agent_inherits_agent_id(client):
    agent = await _create_agent(client, name="Owner")
    resp = await client.post(
        f"/api/agents/{agent['id']}/sessions", json={"name": "thread"}, headers=client.headers
    )
    assert resp.status_code == 201
    session = resp.json()
    assert session["agent_id"] == agent["id"]
    assert session["origin"] == "user"

    listed = await client.get(f"/api/agents/{agent['id']}/sessions", headers=client.headers)
    assert [s["id"] for s in listed.json()] == [session["id"]]


@pytest.mark.asyncio
async def test_session_create_unknown_agent_404(client):
    """404, not the 400 this used to answer.

    A session takes its owner from its agent, so the route now resolves the
    agent *within the caller's account* — and an agent belonging to somebody
    else has to be indistinguishable from one that does not exist
    (multi-tenancy.md §5). Two different statuses for the two cases is an id
    oracle, so both are 404, which is also what `/api/agents/{id}` answers.
    """
    resp = await client.post(
        "/api/sessions", json={"name": "x", "agent_id": "ghost"}, headers=client.headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_scoped_schedule_crud(client):
    agent = await _create_agent(client, name="Scheduled")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules",
        json={"name": "daily", "prompt": "summarize", "interval_seconds": 60},
        headers=client.headers,
    )
    assert resp.status_code == 201
    sched = resp.json()
    assert sched["agent_id"] == agent["id"]

    listed = await client.get(f"/api/agents/{agent['id']}/schedules", headers=client.headers)
    assert [s["id"] for s in listed.json()] == [sched["id"]]


# --- natural-language /schedule (from_text) ---


@pytest.mark.asyncio
async def test_schedule_from_text_rigid(client):
    """Explicit "<interval> <prompt>" needs no AI — the rigid fast path runs."""
    agent = await _create_agent(client, name="Rigid Sched")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={"text": "30m check the build"},
        headers=client.headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["interval_seconds"] == 1800
    assert data["cron"] is None
    assert data["prompt"] == "check the build"
    assert data["recurrence_label"] == "Every 30m"


async def test_rigid_schedule_does_not_require_model_authentication(client, monkeypatch):
    owner = (await session_manager.db.get_system_agent())["user_id"]
    await session_manager.db.save_credential("unavailable", "codex", "owned", "oauth", "", "now", user_id=owner)
    await session_manager.db.update_credential("unavailable", needs_reconnect=True)
    agent = await _create_agent(client, backend="codex", credential_id="unavailable")

    async def forbidden_oneshot(*args, **kwargs):
        pytest.fail("A rigid schedule must not invoke a model")

    monkeypatch.setattr("server.harness.Harness.run_oneshot", forbidden_oneshot)
    url = f"/api/agents/{agent['id']}/schedules/from_text"
    rigid = await client.post(url, json={"text": "30m check the build"})
    assert rigid.status_code == 201, rigid.text
    natural = await client.post(url, json={"text": "check the build every morning"})
    assert natural.status_code == 422, natural.text
    assert "unavailable" in natural.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_from_text_ai_cron(client, monkeypatch):
    """Natural language → AI parse → cron schedule (the harness one-shot mocked)."""
    from server.harness import get_harness

    async def fake_oneshot(ctx):
        await ctx.authorize_spawn()
        assert "America/Los_Angeles" in ctx.prompt
        return (
            '{"name":"Gmail summary","prompt":"summarize my unread email",'
            '"recurrence":{"kind":"cron","cron":"0 9 * * *"},'
            '"recurrence_label":"Every day at 9:00 AM"}'
        )

    monkeypatch.setattr(get_harness("claude-code"), "run_oneshot", fake_oneshot)

    agent = await _create_agent(client, name="NL Sched")
    origin = await session_manager.create_session(agent["id"], "schedule origin")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={
            "text": "summarize my unread email every morning at 9am",
            "timezone": "America/Los_Angeles",
            "session_id": origin.id,
        },
        headers=client.headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["cron"] == "0 9 * * *"
    assert data["timezone"] == "America/Los_Angeles"
    assert data["interval_seconds"] is None
    assert data["recurrence_label"] == "Every day at 9:00 AM"
    assert data["prompt"] == "summarize my unread email"
    # The session the command was issued from is remembered so each fire
    # appends the summary into that same conversation.
    assert data["origin_session_id"] == origin.id


@pytest.mark.asyncio
async def test_schedule_from_text_parse_error_is_422(client, monkeypatch):
    from server.harness import get_harness

    async def fake_oneshot(ctx):
        await ctx.authorize_spawn()
        return "I'm not sure what you mean."

    monkeypatch.setattr(get_harness("claude-code"), "run_oneshot", fake_oneshot)

    agent = await _create_agent(client, name="Bad Sched")
    resp = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={"text": "do something clever at some point"},
        headers=client.headers,
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_schedule_from_text_codex_agent_parses_on_codex(client, monkeypatch):
    """A Codex agent's NL /schedule parses on the CODEX harness, not Claude
    (D2: Codex supports one-shot). No backend-kind nulling — the agent's own
    model is passed, and the one-shot runs on `codex`."""
    from server.harness import get_harness

    agent = await _create_agent(
        client, name="Codex Sched", backend="codex", model="gpt-5.5"
    )

    seen: dict = {}

    async def fake_oneshot(ctx):
        await ctx.authorize_spawn()
        seen["model"] = ctx.model
        return (
            '{"name":"Daily","prompt":"do it","recurrence":'
            '{"kind":"cron","cron":"0 9 * * *"},"recurrence_label":"Daily 9am"}'
        )

    # Patch the CODEX harness's one-shot — proving the parse ran on codex.
    monkeypatch.setattr(get_harness("codex"), "run_oneshot", fake_oneshot)

    r = await client.post(
        f"/api/agents/{agent['id']}/schedules/from_text",
        json={"text": "do it every morning at 9am", "timezone": "UTC"},
        headers=client.headers,
    )
    assert r.status_code == 201, r.text
    assert seen["model"] == "gpt-5.5"  # the agent's own model, on its own harness


@pytest.mark.asyncio
async def test_schedule_from_text_unknown_agent(client):
    resp = await client.post(
        "/api/agents/nope/schedules/from_text",
        json={"text": "30m check the build"},
        headers=client.headers,
    )
    assert resp.status_code == 404


# --- Archive / restore ---


@pytest.mark.asyncio
async def test_unarchive_restores_an_agent(client):
    a = await _create_agent(client, name="Retired")
    assert (
        await client.post(f"/api/agents/{a['id']}/archive", headers=client.headers)
    ).status_code == 200
    assert [x["id"] for x in (await client.get("/api/agents", headers=client.headers)).json()
            if x["id"] == a["id"]] == []

    # The Archived tab lists it...
    archived = (
        await client.get(
            "/api/agents", params={"include_archived": "true"}, headers=client.headers
        )
    ).json()
    assert any(x["id"] == a["id"] and x["archived"] for x in archived)

    # ...and restores it.
    resp = await client.post(f"/api/agents/{a['id']}/unarchive", headers=client.headers)
    assert resp.status_code == 200
    assert resp.json()["archived"] is False
    live = (await client.get("/api/agents", headers=client.headers)).json()
    assert any(x["id"] == a["id"] for x in live)


@pytest.mark.asyncio
async def test_unarchive_refuses_when_the_name_was_taken(client):
    a = await _create_agent(client, name="Scout")
    await client.post(f"/api/agents/{a['id']}/archive", headers=client.headers)
    # The live-only unique index frees the name while it's archived.
    await _create_agent(client, name="Scout")

    resp = await client.post(f"/api/agents/{a['id']}/unarchive", headers=client.headers)
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unarchive_unknown_agent_is_404(client):
    resp = await client.post("/api/agents/ghost/unarchive", headers=client.headers)
    assert resp.status_code == 404


# --- Sidebar pins (sidebar-pins.md) ---


async def _live(client) -> list[dict]:
    return (await client.get("/api/agents", headers=client.headers)).json()


def _pinned_ids(agents: list[dict]) -> list[str]:
    """The sidebar as the client draws it: pinned, in pin order."""
    return [a["id"] for a in sorted(
        (a for a in agents if a["pinned"]), key=lambda a: a["pin_order"]
    )]


@pytest.mark.asyncio
async def test_new_agents_are_pinned_at_the_bottom(client):
    default = next(a for a in await _live(client) if a["is_system"])
    first = await _create_agent(client, name="First")
    second = await _create_agent(client, name="Second")
    assert first["pinned"] and second["pinned"]
    assert _pinned_ids(await _live(client)) == [default["id"], first["id"], second["id"]]


@pytest.mark.asyncio
async def test_unpinned_agent_stays_live_and_listed(client):
    a = await _create_agent(client, name="Quiet")
    resp = await client.post(f"/api/agents/{a['id']}/unpin", headers=client.headers)
    assert resp.status_code == 200
    assert resp.json()["pinned"] is False

    live = await _live(client)
    # Still a live agent — the page lists it, sessions can be made under it —
    # just not one of the sidebar's shortcuts.
    assert any(x["id"] == a["id"] and not x["archived"] for x in live)
    assert a["id"] not in _pinned_ids(live)
    resp = await client.post(
        f"/api/agents/{a['id']}/sessions", json={}, headers=client.headers
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_repinning_appends_to_the_bottom(client):
    a = await _create_agent(client, name="A")
    b = await _create_agent(client, name="B")
    await client.post(f"/api/agents/{a['id']}/unpin", headers=client.headers)
    resp = await client.post(f"/api/agents/{a['id']}/pin", headers=client.headers)
    assert resp.status_code == 200
    assert _pinned_ids(await _live(client))[-2:] == [b["id"], a["id"]]
    # Pinning something already pinned leaves its place alone.
    await client.post(f"/api/agents/{b['id']}/pin", headers=client.headers)
    assert _pinned_ids(await _live(client))[-2:] == [b["id"], a["id"]]


@pytest.mark.asyncio
async def test_default_agent_cannot_be_unpinned(client):
    default = next(a for a in await _live(client) if a["is_system"])
    resp = await client.post(f"/api/agents/{default['id']}/unpin", headers=client.headers)
    assert resp.status_code == 400
    assert "always pinned" in resp.json()["detail"]
    assert next(a for a in await _live(client) if a["is_system"])["pinned"] is True


@pytest.mark.asyncio
async def test_pin_refuses_archived_and_unknown_agents(client):
    a = await _create_agent(client, name="Shelved")
    await client.post(f"/api/agents/{a['id']}/archive", headers=client.headers)
    resp = await client.post(f"/api/agents/{a['id']}/pin", headers=client.headers)
    assert resp.status_code == 400
    assert "Restore" in resp.json()["detail"]
    assert (
        await client.post("/api/agents/ghost/pin", headers=client.headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_restoring_an_agent_pins_it(client):
    a = await _create_agent(client, name="Returning")
    await client.post(f"/api/agents/{a['id']}/unpin", headers=client.headers)
    await client.post(f"/api/agents/{a['id']}/archive", headers=client.headers)
    resp = await client.post(f"/api/agents/{a['id']}/unarchive", headers=client.headers)
    assert resp.json()["pinned"] is True
    assert _pinned_ids(await _live(client))[-1] == a["id"]


@pytest.mark.asyncio
async def test_reorder_pins(client):
    default = next(a for a in await _live(client) if a["is_system"])
    a = await _create_agent(client, name="A")
    b = await _create_agent(client, name="B")
    c = await _create_agent(client, name="C")

    resp = await client.put(
        "/api/agents/pin-order",
        json={"ids": [c["id"], default["id"], a["id"], b["id"]]},
        headers=client.headers,
    )
    assert resp.status_code == 200
    # The response is the whole live list, already in the new order.
    assert _pinned_ids(resp.json()) == [c["id"], default["id"], a["id"], b["id"]]
    assert _pinned_ids(await _live(client)) == [c["id"], default["id"], a["id"], b["id"]]


@pytest.mark.asyncio
async def test_reorder_keeps_pins_the_list_did_not_name(client):
    """A tab that last looked before another tab pinned something must not
    drop that pin when it reorders: the unnamed ones follow, in order."""
    default = next(a for a in await _live(client) if a["is_system"])
    a = await _create_agent(client, name="A")
    b = await _create_agent(client, name="B")
    resp = await client.put(
        "/api/agents/pin-order", json={"ids": [b["id"]]}, headers=client.headers
    )
    assert resp.status_code == 200
    assert _pinned_ids(resp.json()) == [b["id"], default["id"], a["id"]]


@pytest.mark.asyncio
async def test_reorder_rejects_unpinned_unknown_and_repeated_ids(client):
    a = await _create_agent(client, name="A")
    b = await _create_agent(client, name="B")
    await client.post(f"/api/agents/{b['id']}/unpin", headers=client.headers)

    for ids in ([a["id"], b["id"]], [a["id"], "ghost"], [a["id"], a["id"]]):
        resp = await client.put(
            "/api/agents/pin-order", json={"ids": ids}, headers=client.headers
        )
        assert resp.status_code == 400, ids


@pytest.mark.asyncio
async def test_agent_changes_are_broadcast(client, monkeypatch):
    """Every open client mirrors the agent list from these events, so a pin
    made in one tab shows in the others (sidebar-pins.md §7)."""
    events: list[dict] = []

    # `**_` so the double keeps matching `_broadcast`, which now takes the
    # frame's owner as a keyword (multi-tenancy.md §7). A stub that does not
    # accept what the real thing is called with is how the Telegram
    # Allow/Deny bug shipped green.
    async def capture(msg, **_):
        events.append(msg)

    monkeypatch.setattr(session_manager, "_broadcast", capture)

    a = await _create_agent(client, name="Loud")
    await client.post(f"/api/agents/{a['id']}/unpin", headers=client.headers)
    await client.patch(
        f"/api/agents/{a['id']}", json={"description": "d"}, headers=client.headers
    )
    await client.post(f"/api/agents/{a['id']}/pin", headers=client.headers)
    await client.post(f"/api/agents/{a['id']}/archive", headers=client.headers)
    await client.post(f"/api/agents/{a['id']}/unarchive", headers=client.headers)

    mine = [(e["type"], e["agent"]["pinned"]) for e in events
            if "agent" in e and e["agent_id"] == a["id"]]
    assert mine == [
        ("agent_created", True),
        ("agent_updated", False),
        ("agent_updated", False),
        ("agent_updated", True),
        ("agent_archived", True),
        ("agent_updated", True),
    ]
    # A failed request broadcasts nothing.
    events.clear()
    default = next(x for x in await _live(client) if x["is_system"])
    await client.post(f"/api/agents/{default['id']}/unpin", headers=client.headers)
    assert [e for e in events if "agent" in e] == []

    b = await _create_agent(client, name="Deletable")
    events.clear()
    assert (
        await client.delete(f"/api/agents/{b['id']}", headers=client.headers)
    ).status_code == 204
    assert [e["type"] for e in events if "agent" in e] == ["agent_deleted"]

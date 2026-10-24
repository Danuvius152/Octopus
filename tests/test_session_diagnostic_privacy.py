"""Local diagnostics preserve useful failures without copying credentials."""

import asyncio
import errno
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import deps
from server.agent_manager import AgentManager
from server.config import settings
from server.harness import get_harness
from server.routers import credentials as credentials_api
from server.session_manager import session_manager
from server.users import UserManager
from tests import test_auth_routes as auth_api
from tests.accounts import create_account
from tests.test_service_shutdown import synthetic_cli

auth_client = auth_api.client


@pytest.fixture
async def client(auth_client, tmp_path):
    http, users, db = auth_client
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    http.headers.update({"Authorization": f"Bearer {bearer}"})
    yield http, users, db


async def test_spawn_failure_keeps_local_traceback_and_details_without_credentials(client, monkeypatch, caplog):
    http, _users, db = client
    owner = (await db.get_system_agent())["user_id"]
    prompt = "ORIGINAL-LOCAL-DEBUG-PROMPT-46"
    token = "synthetic-local-diagnostic-token-46"
    monkeypatch.setattr(settings, "auth_token", token)
    detail = "provider rejected an oversized argument; inspect the final argv budget"
    failure = OSError(errno.E2BIG, f"{prompt} {detail}; token={token}")
    starts = []
    synthetic_cli(monkeypatch, starts)

    async def refused(*argv, **kwargs):
        raise failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refused)
    agent = await AgentManager(db).create_agent(name="Diagnostic owner", backend="codex", user_id=owner)
    session = await session_manager.create_session(agent["id"], "Private diagnostics", backend="codex")
    try:
        with caplog.at_level(logging.DEBUG, logger="server"):
            receipt = await session_manager.start_message(session.id, prompt)
            completion = await asyncio.wait_for(receipt.wait(), 5)
            await session_manager.drain_turns()
        assert completion.outcome.status == "failed"
        assert completion.outcome.code == "backend_exception"
        assert prompt in completion.outcome.message and detail in completion.outcome.message
        assert token not in completion.outcome.message
        assert token in str(failure)
        assert failure.errno == errno.E2BIG
        diagnostic = "\n".join(record.getMessage() for record in caplog.records
                               if record.name == "server.sessions.base")
        assert "OSError" in diagnostic and session.id in diagnostic and receipt.request_id in diagnostic
        assert "Traceback (most recent call last)" in diagnostic
        assert "owner_runtime.py" in diagnostic and "_start_original" in diagnostic
        assert prompt in diagnostic and detail in diagnostic
        assert token not in diagnostic and "[redacted]" in diagnostic
        detail = await http.get(f"/api/sessions/{session.id}", headers=http.headers)
        assert detail.status_code == 200, detail.text
        assert any(message["role"] == "user" and message["content"] == prompt for message in detail.json()["messages"])
    finally:
        await session_manager.drain_turns()


async def test_bound_credential_spawn_failure_masks_all_local_logs_without_changing_the_error(
    client, monkeypatch, caplog,
):
    http, _users, db = client
    owner = (await db.get_system_agent())["user_id"]
    token = "synthetic-installation-diagnostic-key-46"
    credential_secret = "quietmarble46sharedproofvalue"
    prompt = "ORIGINAL-BOUND-CREDENTIAL-PROMPT-46"
    detail = "provider rejected an oversized argument; inspect the final argv budget"
    cause_detail = "environment block inspection failed"
    monkeypatch.setattr(settings, "auth_token", token)
    failure_message = f"{prompt} {detail}; {credential_secret}"
    cause_message = f"{cause_detail}: {credential_secret}"
    failure = OSError(errno.E2BIG, failure_message)
    cause = RuntimeError(cause_message)
    launched_credentials = []
    harness = get_harness("claude-code")
    render_argv = harness.profile.build_turn_argv
    spawn_process = asyncio.create_subprocess_exec

    def executable_argv(context):
        argv, kwargs = render_argv(context)
        # Keep the real profile's credential rendering without requiring a
        # Claude installation: this test stops at the OS spawn boundary.
        return [sys.executable, *argv[1:]], kwargs

    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=executable_argv))

    async def refused(*argv, **kwargs):
        if "ANTHROPIC_API_KEY" not in kwargs.get("env", {}):
            return await spawn_process(*argv, **kwargs)
        launched_credentials.append(kwargs["env"]["ANTHROPIC_API_KEY"])
        raise failure from cause

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refused)
    credentials_api.set_db(db)
    try:
        created = await http.post(
            "/api/credentials",
            json={"backend": "claude-code", "label": "Diagnostic credential",
                  "auth_type": "api_key", "secret": credential_secret},
            headers=http.headers,
        )
        assert created.status_code == 201, created.text
        agent = await AgentManager(db).create_agent(name="Bound diagnostic owner", backend="claude-code", user_id=owner)
        session = await session_manager.create_session(
            agent["id"], "Bound private diagnostics", backend="claude-code",
            credential_id=created.json()["id"],
        )
        with caplog.at_level(logging.DEBUG, logger="server"):
            receipt = await session_manager.start_message(session.id, prompt)
            completion = await asyncio.wait_for(receipt.wait(), 5)
            await session_manager.drain_turns()
        assert launched_credentials == [credential_secret]
        assert completion.outcome.status == "failed"
        assert completion.outcome.code == "backend_exception"
        assert prompt in completion.outcome.message and detail in completion.outcome.message
        assert failure.args == (errno.E2BIG, failure_message)
        assert failure.errno == errno.E2BIG and failure.__cause__ is cause
        assert cause.args == (cause_message,)
        diagnostic = "\n".join(
            logging.Formatter().format(record) for record in caplog.records
            if record.name.startswith("server.")
        )
        assert "OSError" in diagnostic and "RuntimeError" in diagnostic
        assert session.id in diagnostic and receipt.request_id in diagnostic
        assert "Traceback (most recent call last)" in diagnostic
        assert "owner_runtime.py" in diagnostic and "_start_original" in diagnostic
        assert "The above exception was the direct cause" in diagnostic
        assert prompt in diagnostic and detail in diagnostic and cause_detail in diagnostic
        assert credential_secret not in diagnostic and "[redacted]" in diagnostic
        transcript = await http.get(
            f"/api/sessions/{session.id}", headers=http.headers,
        )
        assert transcript.status_code == 200, transcript.text
        assert any(message["role"] == "user" and message["content"] == prompt
                   for message in transcript.json()["messages"])
    finally:
        await session_manager.drain_turns()
        credentials_api.set_db(None)


async def test_failed_retirement_keeps_its_original_credential_diagnostics_after_binding_changes(
    tmp_path,
):
    from server.database import Database
    from server.sessions import SessionManager

    artifacts = tmp_path / "original-diagnostic-owner"
    artifacts.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
        str(Path(__file__).resolve()), "-o", "python_functions=_retirement_diagnostic_child",
        "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 15)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        proof = json.loads((artifacts / "proof.json").read_text())
        assert proof["binding_changed"] is True and proof["successor_accepted"] is True
        assert proof["failure_pristine"] is True
        assert json.loads((artifacts / "launches.json").read_text()) == ["retiredmarble46originalvalue"]
        diagnostic = (artifacts / "service.log").read_text()
        assert proof["session_id"] in diagnostic and proof["original_request_id"] in diagnostic
        assert "Traceback (most recent call last)" in diagnostic
        assert "late OS launch failure while confirming retirement" in diagnostic
        assert "environment inspection" in diagnostic
        assert "OSError" in diagnostic and "RuntimeError" in diagnostic
        assert "retiredmarble46originalvalue" not in diagnostic
        assert "newmarble46replacementvalue" not in diagnostic
        assert "synthetic-installation-retirement-key-46" not in diagnostic
        assert "[redacted]" in diagnostic
        db = Database(proof["database"])
        await db.initialize()
        deps.set_user_manager(UserManager(db))
        restored = SessionManager()
        try:
            await restored.initialize(db)
            row = next(row for row in await db.load_sessions() if row["id"] == proof["session_id"])
            assert row["credential_id"] == proof["replacement_credential_id"]
            messages = await db.load_messages(proof["session_id"])
            assert [message["content"] for message in messages if message["role"] == "user"] == ["initial request"]
            outcomes = [message["content"] for message in messages if message["type"] == "result"]
            assert len(outcomes) == 1
            assert outcomes[0]["status"] == "interrupted"
            assert outcomes[0]["request_id"] == proof["original_request_id"]
            assert not [outcome for outcome in outcomes if outcome["request_id"] == proof["successor_request_id"]]
            assert restored.dispatch_snapshot(proof["session_id"])["items"] == []
        finally:
            await db.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)


async def _retirement_diagnostic_child(tmp_path, tmp_path_factory, monkeypatch):
    """Natural owner failure retains the old credential until actual exit70."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from server.database import Database
    from server.deps import get_session_manager
    from server.routers.sessions import router
    from server.sessions import SessionManager

    artifacts = tmp_path_factory.getbasetemp().parent
    database_path = tmp_path / "diagnostic-owner.db"
    db = Database(str(database_path))
    await db.initialize()
    owner, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    application = FastAPI()
    application.include_router(credentials_api.router)
    application.include_router(router)
    application.dependency_overrides[get_session_manager] = lambda: manager
    token = "synthetic-installation-retirement-key-46"
    original_secret = "retiredmarble46originalvalue"
    replacement_secret = "newmarble46replacementvalue"
    detail = "late OS launch failure while confirming retirement"
    failure = OSError(errno.E2BIG, f"{detail}; {original_secret}")
    cause = RuntimeError(f"environment inspection: {original_secret}")
    entered, release = asyncio.Event(), asyncio.Event()
    launched_credentials = []
    monkeypatch.setattr(settings, "auth_token", token)
    harness = get_harness("claude-code")
    render_argv = harness.profile.build_turn_argv
    spawn_process = asyncio.create_subprocess_exec

    def executable_argv(context):
        argv, kwargs = render_argv(context)
        return [sys.executable, *argv[1:]], kwargs

    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=executable_argv))

    async def late_refusal(*argv, **kwargs):
        if "ANTHROPIC_API_KEY" not in kwargs.get("env", {}):
            return await spawn_process(*argv, **kwargs)
        launched_credentials.append(kwargs["env"]["ANTHROPIC_API_KEY"])
        (artifacts / "launches.json").write_text(json.dumps(launched_credentials))
        entered.set()
        await release.wait()
        raise failure from cause

    monkeypatch.setattr(asyncio, "create_subprocess_exec", late_refusal)
    credentials_api.set_db(db)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as http:
        credentials = []
        for secret in (original_secret, replacement_secret):
            created = await http.post(
                "/api/credentials",
                json={"backend": "claude-code", "label": "Retirement diagnostic credential",
                      "auth_type": "api_key", "secret": secret},
                headers=http.headers,
            )
            assert created.status_code == 201, created.text
            credentials.append(created.json()["id"])
        agent = await AgentManager(db).create_agent(name="Retirement diagnostic owner", backend="claude-code", user_id=owner["id"])
        session = await manager.create_session(
            agent["id"], "Retirement diagnostics", backend="claude-code", credential_id=credentials[0],
        )
        proof = {"database": str(database_path), "session_id": session.id,
                 "replacement_credential_id": credentials[1], "binding_changed": False,
                 "successor_accepted": False, "failure_pristine": False}

        class Witness(logging.Handler):
            def emit(self, record):
                proof["failure_pristine"] = (
                    failure.errno == errno.E2BIG and failure.__cause__ is cause
                    and original_secret in str(failure) and original_secret in str(cause)
                    and failure.args == (errno.E2BIG, f"{detail}; {original_secret}")
                    and cause.args == (f"environment inspection: {original_secret}",)
                )
                (artifacts / "proof.json").write_text(json.dumps(proof))

        logger = logging.getLogger("server")
        logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler(artifacts / "service.log")
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.addHandler(Witness())
        receipt = await manager.start_message(session.id, "initial request")
        proof["original_request_id"] = receipt.request_id
        await asyncio.wait_for(entered.wait(), 5)
        assert await manager.interrupt(session.id)
        rebound = await http.patch(
            f"/api/sessions/{session.id}", json={"credential_id": credentials[1]},
            headers=http.headers,
        )
        assert rebound.status_code == 200, rebound.text
        assert rebound.json()["credential_id"] == credentials[1]
        proof["binding_changed"] = True
        second = await manager.start_message(session.id, "use the replacement credential")
        assert second.accepted
        proof["successor_request_id"], proof["successor_accepted"] = second.request_id, True
        assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [second.request_id]
        (artifacts / "launches.json").write_text(json.dumps(launched_credentials))
        (artifacts / "proof.json").write_text(json.dumps(proof))
        release.set()
        # The original late spawn error fails the original retirement. No
        # second failure or manual map/task removal is used to reach exit70.
        await asyncio.wait_for(asyncio.Event().wait(), 8)


async def test_steer_pipe_failure_preserves_local_details_without_the_bound_credential(
    client, monkeypatch, caplog,
):
    http, _users, db = client
    owner = (await db.get_system_agent())["user_id"]
    token = "synthetic-installation-steer-key-46"
    credential_secret = "steermarble46boundvalue"
    prompt = "ORIGINAL-STEER-PIPE-PROMPT-46"
    detail = "stdin delivery failed before the frame was submitted"
    failure = OSError(errno.EPIPE, f"{detail}; {credential_secret}")
    cause = RuntimeError(f"pipe inspection: {credential_secret}")
    ready, rejected = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(settings, "auth_token", token)
    harness = get_harness("claude-code")
    render_argv = harness.profile.build_turn_argv
    code = (
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'assistant','message':{'content':["
        "{'type':'text','text':'ready for a steer'}]}}),flush=True)\n"
        "for line in sys.stdin: pass\n"
    )

    def executable_argv(context):
        _argv, kwargs = render_argv(context)
        return [sys.executable, "-u", "-c", code], kwargs

    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=executable_argv))
    pipe_write = asyncio.StreamWriter.write

    def rejected_write(writer, data):
        if prompt.encode() in data:
            rejected.set()
            raise failure from cause
        return pipe_write(writer, data)

    monkeypatch.setattr(asyncio.StreamWriter, "write", rejected_write)

    async def observe(event):
        if event.get("type") == "assistant_text":
            ready.set()

    session_manager.on_broadcast("diagnostic-steer-ready", observe)
    credentials_api.set_db(db)
    try:
        created = await http.post(
            "/api/credentials",
            json={"backend": "claude-code", "label": "Steer diagnostic credential",
                  "auth_type": "api_key", "secret": credential_secret},
            headers=http.headers,
        )
        assert created.status_code == 201, created.text
        agent = await AgentManager(db).create_agent(name="Steer diagnostic owner", backend="claude-code", user_id=owner)
        session = await session_manager.create_session(
            agent["id"], "Steer diagnostics", backend="claude-code", credential_id=created.json()["id"],
        )
        with caplog.at_level(logging.DEBUG, logger="server"):
            receipt = await session_manager.start_message(session.id, "initial request")
            await asyncio.wait_for(ready.wait(), 5)
            steer = await session_manager.start_message(session.id, prompt, steerable=True)
            assert steer.disposition == "steered"
            await asyncio.wait_for(rejected.wait(), 5)
            await session_manager.drain_turns()
        assert (await receipt.wait()).outcome.status == "interrupted"
        assert failure.errno == errno.EPIPE and failure.__cause__ is cause
        assert credential_secret in str(failure) and credential_secret in str(cause)
        diagnostic = "\n".join(
            logging.Formatter().format(record) for record in caplog.records
            if record.name.startswith("server.")
        )
        assert "Steer delivery unconfirmed" in diagnostic
        assert session.id in diagnostic and steer.request_id in diagnostic
        assert "Traceback (most recent call last)" in diagnostic
        assert detail in diagnostic and "pipe inspection" in diagnostic
        assert credential_secret not in diagnostic and "[redacted]" in diagnostic
        transcript = await http.get(
            f"/api/sessions/{session.id}", headers=http.headers,
        )
        assert transcript.status_code == 200, transcript.text
        assert any(message["role"] == "user" and message["content"] == prompt
                   for message in transcript.json()["messages"])
    finally:
        await session_manager.drain_turns()
        session_manager.remove_broadcast("diagnostic-steer-ready")
        credentials_api.set_db(None)

"""Tests for in-app Codex device-auth login (server/codex_login.py + routes).

The real `codex login --device-auth` is replaced by a fake CLI fixture, so
these run anywhere (no codex binary, no ChatGPT account). The fake emits the
same URL + code shape and writes auth.json on the success path."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import codex_login
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import credentials as credentials_mod
from server.session_manager import session_manager
from tests.accounts import create_account

_FAKE = os.path.join(os.path.dirname(__file__), "_fixtures", "fake_codex_login.py")


@pytest.fixture(autouse=True)
def _isolate_codex_home(tmp_path, monkeypatch):
    """Point CODEX_HOME root at a temp dir + route the manager at the fake CLI."""
    monkeypatch.setattr(settings, "codex_home_dir", str(tmp_path / "codex"))
    monkeypatch.setattr(
        codex_login,
        "build_codex_login_argv",
        lambda: [sys.executable, _FAKE, "login", "--device-auth"],
    )
    # Fresh manager state each test.
    monkeypatch.setattr(codex_login, "codex_login_manager", codex_login.CodexLoginManager())
    monkeypatch.delenv("CODEX_FAKE_LOGIN_MODE", raising=False)
    yield


@pytest.fixture
async def owned_manager():
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db)
    manager = codex_login.CodexLoginManager(db)
    try:
        yield manager, db, owner["id"]
    finally:
        await manager.shutdown()
        await db.close()


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db)
    await session_manager.initialize(db)
    credentials_mod.set_db(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as c:
        yield c, db
    await codex_login.codex_login_manager.shutdown()
    await db.close()


# --- manager unit tests ----------------------------------------------------


@pytest.mark.parametrize("output,complete", [("", False), ("https://auth.example/device", False), ("TEST-CODE9", False),
                                           ("https://auth.example/device TEST-CODE9", True)])
async def test_device_scrape_budget_requires_both_fields_and_confirms_process_exit(tmp_path, monkeypatch, output, complete):
    script = tmp_path / "partial_login.py"
    script.write_text(f"import time\nprint({output!r}, flush=True)\ntime.sleep(30)\n")
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(codex_login, "_SCRAPE_TIMEOUT_SECONDS", .15)
    manager = codex_login.CodexLoginManager()
    session = await manager.start("Partial device code")
    try:
        await asyncio.wait_for(asyncio.shield(session._watchdog), .5)
        if complete:
            assert session.state == codex_login.CodexLoginState.pending
            assert session._proc.returncode is None and not session._task.done()
            await manager.cancel(session.id)
        else:
            assert session.state == codex_login.CodexLoginState.error
            assert "device code in time" in session.message
            assert session._proc.returncode is not None and session._task.done()
        assert manager.drain_blockers() == ()
    finally:
        await manager.shutdown()


async def test_scrape_deadline_rechecks_complete_fields_instead_of_the_wakeup_event(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_login, "_SCRAPE_TIMEOUT_SECONDS", 0)
    manager = codex_login.CodexLoginManager()
    session = codex_login.CodexLoginSession("boundary", "credential", str(tmp_path), "Complete at deadline",
        verification_url="https://auth.example/device", user_code="TEST-CODE9")
    assert not session._scraped.is_set()
    await manager._scrape_watchdog(session)
    assert session.state == codex_login.CodexLoginState.pending


async def test_late_successful_drive_cannot_replace_scrape_timeout(tmp_path, monkeypatch):
    script = tmp_path / "late_login.py"
    script.write_text("import os,pathlib\npathlib.Path(os.environ['CODEX_HOME'],'auth.json').write_text('{\"tokens\":{\"access_token\":\"new\"}}')\nprint('https://auth.example/device',flush=True)\n")
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(codex_login, "_SCRAPE_TIMEOUT_SECONDS", .15)
    original_spawn = asyncio.create_subprocess_exec
    drive_waiting, release_drive, release_failure = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        wait = proc.wait
        calls = 0
        async def held_wait():
            nonlocal calls
            calls += 1
            if calls == 1:
                drive_waiting.set()
                await release_drive.wait()
            else:
                await release_failure.wait()
            return await wait()
        proc.wait = held_wait
        return proc
    monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", spawn)
    manager = codex_login.CodexLoginManager()
    session = await manager.start("Late successful CLI exit")
    try:
        await asyncio.wait_for(drive_waiting.wait(), 2)
        await asyncio.wait_for(session._scraped.wait(), 2)  # Timeout has won, cleanup is still suspended.
        release_drive.set()
        release_failure.set()
        await asyncio.wait_for(session._task, 2)
        assert session.state == codex_login.CodexLoginState.error
        assert "device code in time" in session.message
        await session._watchdog
        assert session._proc.returncode == 0 and manager.drain_blockers() == ()
    finally:
        release_drive.set()
        release_failure.set()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_start_scrapes_url_and_code(owned_manager):
    mgr, _, owner = owned_manager
    session = await mgr.start("My ChatGPT", user_id=owner)
    # `start` returns immediately (non-blocking); the URL+code are scraped by
    # the drive task. Wait for it, then assert what was captured.
    assert os.path.isdir(session._login_home)
    await session._task
    assert session.verification_url == "https://auth.openai.com/codex/device"
    assert session.user_code == "TEST-CODE9"


@pytest.mark.asyncio
async def test_success_writes_authjson_and_marks_success(owned_manager):
    mgr, _, owner = owned_manager
    session = await mgr.start("ok", user_id=owner)
    await session._task
    assert session.state == codex_login.CodexLoginState.success
    assert os.path.exists(os.path.join(session.codex_home, "auth.json"))


@pytest.mark.asyncio
async def test_failure_marks_error_and_cleans_dir(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "fail")
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("bad")
    await session._task
    assert session.state == codex_login.CodexLoginState.error
    assert not os.path.exists(session.codex_home)


@pytest.mark.asyncio
async def test_cancel_kills_and_cleans(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("hang")  # returns after scrape; proc still running
    assert session.state == codex_login.CodexLoginState.pending
    await mgr.cancel(session.id)
    assert session.state == codex_login.CodexLoginState.cancelled
    assert not os.path.exists(session.codex_home)


@pytest.mark.asyncio
async def test_shutdown_joins_login_and_watchdog_before_releasing_session(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("shutdown")
    try:
        await mgr.shutdown()
        assert session._proc.returncode is not None
        assert session._task.done() and session._watchdog.done()
        assert session.state == codex_login.CodexLoginState.cancelled
        assert mgr.get(session.id) is None
        with pytest.raises(RuntimeError, match="shutting down"):
            await mgr.start("too late")
        await mgr.shutdown()
    finally:
        codex_login._kill_process_group(session._proc)
        await asyncio.gather(session._task, session._watchdog, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_retains_a_login_until_its_process_exit_is_confirmed(monkeypatch):
    class UnconfirmedProcess:
        pid = 2**30
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.exited = asyncio.Event()

        def kill(self):
            pass

        async def wait(self):
            await self.exited.wait()
            return self.returncode

    proc = UnconfirmedProcess()

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_login, "_REAP_TIMEOUT_SECONDS", 0.03, raising=False)
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("unconfirmed")
    try:
        with pytest.raises(RuntimeError, match="did not confirm process exit"):
            await asyncio.wait_for(mgr.shutdown(), 0.3)
        assert mgr.get(session.id) is session
        assert not session._task.done()
        proc.returncode = -9
        proc.stdout.feed_eof()
        proc.exited.set()
        await mgr.shutdown()
        assert mgr.get(session.id) is None
    finally:
        proc.stdout.feed_eof()
        proc.exited.set()
        await asyncio.gather(session._task, session._watchdog, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_owns_login_spawn_before_its_process_handle_arrives(monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release_handle.wait()
        return proc

    monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", delayed_spawn)
    mgr = codex_login.CodexLoginManager()
    login = asyncio.create_task(mgr.start("spawning"))
    cleanup = None
    try:
        await spawned.wait()
        mgr.begin_shutdown()
        cleanup = asyncio.create_task(mgr.shutdown())
        await asyncio.sleep(0)
        assert not cleanup.done()
        release_handle.set()
        await cleanup
        session = await login
        assert session._task.done() and session._watchdog.done()
        assert processes[0].returncode is not None
        assert mgr.get(session.id) is None
    finally:
        release_handle.set()
        for proc in processes:
            codex_login._kill_process_group(proc)
        await asyncio.gather(login, *(task for task in [cleanup] if task is not None), return_exceptions=True)


# --- route tests -----------------------------------------------------------


async def test_original_login_publishes_without_any_status_observer(client):
    c, db = client
    response = await c.post("/api/credentials/codex/start", json={"label": "No polling"})
    assert response.status_code == 201
    login = codex_login.codex_login_manager.get(response.json()["login_id"])
    await asyncio.wait_for(asyncio.shield(login._task), 3)
    listed = (await c.get("/api/credentials")).json()
    assert [row["id"] for row in listed] == [login.credential_id]
    assert login.state == codex_login.CodexLoginState.success
    assert (await db.get_credential(login.credential_id))["label"] == "No polling"


async def test_overlapping_reauthorization_conflicts_until_original_cli_retires(client, monkeypatch):
    c, _ = client
    credential = await c.post("/api/credentials", json={"backend": "codex", "auth_type": "oauth", "label": "Existing", "secret": "home"})
    target = credential.json()["id"]
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    body = {"label": "Reauthorize", "reauth_credential_id": target}
    first = await c.post("/api/credentials/codex/start", json=body)
    assert first.status_code == 201
    first_id = first.json()["login_id"]
    second_id = None
    try:
        second = await c.post("/api/credentials/codex/start", json=body)
        if second.status_code == 201:
            second_id = second.json()["login_id"]
        assert second.status_code == 409
        await c.post("/api/credentials/codex/cancel", json={"login_id": first_id})
        again = await c.post("/api/credentials/codex/start", json=body)
        assert again.status_code == 201
        second_id = again.json()["login_id"]
    finally:
        await c.post("/api/credentials/codex/cancel", json={"login_id": first_id})
        if second_id:
            await c.post("/api/credentials/codex/cancel", json={"login_id": second_id})


@pytest.mark.asyncio
async def test_codex_login_route_end_to_end(client):
    c, db = client
    start = await c.post(
        "/api/credentials/codex/start", json={"label": "ChatGPT Plus"}, headers=c.headers
    )
    assert start.status_code == 201, start.text
    login_id = start.json()["login_id"]

    # Poll until the fake writes auth.json and the route persists the credential.
    # The URL + code surface via status (start is non-blocking).
    cred = None
    saw_code = False
    for _ in range(50):
        st = await c.get(f"/api/credentials/codex/{login_id}/status", headers=c.headers)
        assert st.status_code == 200
        data = st.json()
        if data.get("verification_url") and data.get("user_code"):
            saw_code = True
            assert data["verification_url"].startswith("https://")
        if data["state"] == "success":
            cred = data["credential"]
            break
        assert data["state"] != "error", data
        import asyncio

        await asyncio.sleep(0.05)
    assert saw_code, "URL + code never surfaced via status"
    assert cred is not None, "login never reached success"
    assert cred["backend"] == "codex"
    assert cred["auth_type"] == "oauth"

    # It shows up in the credential list, scoped to the codex backend.
    listed = (await c.get("/api/credentials", headers=c.headers)).json()
    assert any(x["id"] == cred["id"] and x["backend"] == "codex" for x in listed)

    # Deleting it removes the CODEX_HOME dir.
    row = await db.get_credential(cred["id"])
    assert row["user_id"] == (await db.get_system_agent())["user_id"]
    home = codex_login.codex_home_for(cred["id"])
    assert os.path.exists(os.path.join(home, "auth.json"))
    delr = await c.delete(f"/api/credentials/{cred['id']}", headers=c.headers)
    assert delr.status_code == 204
    assert not os.path.exists(home)


@pytest.mark.asyncio
async def test_codex_login_status_unknown(client):
    c, _ = client
    r = await c.get("/api/credentials/codex/nope/status", headers=c.headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_codex_home_credential_resolution(client):
    """The codex home_dir credential resolves deterministically and only when
    it's a completed login (auth.json present). Resolution now lives in
    resolve_credential_by_id(backend="codex") + _resolve_credential's
    session→agent precedence (the old _codex_home_for, folded in)."""
    from types import SimpleNamespace

    from server.harness import get_harness
    from server.session_manager import SessionManager

    c, db = client
    owner = (await db.get_system_agent())["user_id"]
    mgr = SessionManager()
    await mgr.initialize(db)
    await db.save_credential("credX", "codex", "owned", "oauth", "", "now", user_id=owner)
    codex = get_harness("codex")  # credential_style == "home_dir"
    sess = SimpleNamespace(credential_id="credX", id="s1", user_id=owner)

    # An explicit incomplete login must not fall back to a different host login.
    with pytest.raises(ValueError, match="completed login"):
        await mgr._resolve_credential(sess, None, codex)

    home = codex_login.codex_home_for("credX")
    os.makedirs(home, exist_ok=True)
    with pytest.raises(ValueError, match="completed login"):
        await mgr._resolve_credential(sess, None, codex)

    open(os.path.join(home, "auth.json"), "w").close()
    cred = await mgr._resolve_credential(sess, None, codex)
    assert cred is not None and cred.home_dir == home and cred.backend == "codex"

    # Falls back to the agent's default credential when the session has none.
    no_cred = SimpleNamespace(credential_id=None, id="s2", user_id=owner)
    cred2 = await mgr._resolve_credential(no_cred, {"credential_id": "credX"}, codex)
    assert cred2 is not None and cred2.home_dir == home
    # No credential anywhere → None.
    assert await mgr._resolve_credential(no_cred, None, codex) is None


@pytest.mark.asyncio
async def test_codex_login_cancel_route(client, monkeypatch):
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang")
    c, _ = client
    start = await c.post(
        "/api/credentials/codex/start", json={"label": "x"}, headers=c.headers
    )
    login_id = start.json()["login_id"]
    cancel = await c.post(
        "/api/credentials/codex/cancel", json={"login_id": login_id}, headers=c.headers
    )
    assert cancel.status_code == 204
    st = await c.get(f"/api/credentials/codex/{login_id}/status", headers=c.headers)
    assert st.json()["state"] == "cancelled"


@pytest.mark.parametrize("outcome", ["cancel", "failure", "spawn_failure", "invalid_auth", "scrape_timeout", "success"])
async def test_reauthorization_preserves_existing_home_and_only_publishes_valid_auth(tmp_path, monkeypatch, owned_manager, outcome):
    credential_id = "existing"
    home = Path(codex_login.codex_home_for(credential_id))
    home.mkdir(parents=True)
    originals = {
        "auth.json": b'{"tokens":{"access_token":"existing-auth"}}',
        "config.toml": b'model = "existing-model"\n',
        "sessions/2026/10/25/rollout-existing.jsonl": b'{"original":"native-record"}\n',
    }
    for relative, contents in originals.items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)

    # A failing or cancelled CLI may already have written files. Its write
    # authority must be confined to this login's staging home.
    script = tmp_path / "reauth.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
        "(home / 'auth.json').write_text('{\"tokens\":{\"access_token\":\"new-auth\"}}')\n"
        "(home / 'config.toml').write_text('damaged config')\n"
        "(home / 'sessions').mkdir(exist_ok=True)\n"
        "(home / 'sessions' / 'replacement.jsonl').write_text('wrong record')\n"
        + ("print('https://auth.example/device', flush=True)\n" if outcome == "scrape_timeout" else
           "print('https://auth.example/device TEST-CODE9', flush=True)\n")
        + ("time.sleep(30)\n" if outcome in {"cancel", "scrape_timeout"} else
           "sys.exit(1)\n" if outcome == "failure" else
           "(home / 'auth.json').write_text('not-json')\n" if outcome == "invalid_auth" else "")
    )
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    if outcome == "scrape_timeout":
        monkeypatch.setattr(codex_login, "_SCRAPE_TIMEOUT_SECONDS", .15)
    if outcome == "spawn_failure":
        async def fail_spawn(*args, **kwargs):
            raise OSError("synthetic spawn failure")

        monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", fail_spawn)

    mgr, db, owner = owned_manager
    await db.save_credential(credential_id, "codex", "Existing label", "oauth", "old", "now", user_id=owner)
    try:
        if outcome == "spawn_failure":
            with pytest.raises(RuntimeError, match="synthetic spawn failure"):
                await mgr.start("Reauthorize", reauth_credential_id=credential_id, user_id=owner)
        else:
            session = await mgr.start("Reauthorize", reauth_credential_id=credential_id, user_id=owner)
            assert session.codex_home == str(home)
            assert session._login_home != str(home)
            if outcome == "cancel":
                await asyncio.wait_for(session._scraped.wait(), 2)
                await mgr.cancel(session.id)
            else:
                await asyncio.wait_for(session._task, 2)
            assert session.state.value == (
                "success" if outcome == "success" else "cancelled" if outcome == "cancel" else "error"
            )
        for relative, contents in originals.items():
            if outcome == "success" and relative == "auth.json":
                assert b"new-auth" in (home / relative).read_bytes()
                assert (home / relative).stat().st_mode & 0o777 == 0o600
            else:
                assert (home / relative).read_bytes() == contents
        assert not (home / "sessions/replacement.jsonl").exists()
        assert sorted(path.name for path in home.parent.iterdir()) == [credential_id]
    finally:
        await mgr.shutdown()


async def test_codex_status_retries_only_observe_original_commit_and_current_availability(client):
    c, db = client
    owner = (await db.get_system_agent())["user_id"]
    started = await c.post("/api/credentials/codex/start", json={"label": "Retryable login"})
    session = codex_login.codex_login_manager.get(started.json()["login_id"])
    await asyncio.shield(session._task)
    path = f"/api/credentials/codex/{session.id}/status"
    replies = await asyncio.gather(*(c.get(path) for _ in range(5)))
    assert all(reply.status_code == 200 for reply in replies)
    assert all(reply.json()["credential"]["id"] == session.credential_id for reply in replies)
    assert len(await db.load_credentials(owner)) == 1
    # Discard all prior responses and retry after the original commit.
    assert (await c.get(path)).json()["credential"]["id"] == session.credential_id
    await db.delete_credential(session.credential_id)
    deleted = await c.get(path)
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "success"
    assert deleted.json()["credential"] is None
    assert await db.load_credentials(owner) == []


async def test_codex_status_is_not_readable_by_another_account(client, tmp_path):
    c, db = client
    owner = (await db.get_system_agent())["user_id"]
    other, other_bearer = await create_account(db, username="other-status-owner")
    assert other["id"] != owner
    session = codex_login.CodexLoginSession(
        id="private-login", credential_id="private-credential", codex_home=str(tmp_path / "private-home"),
        label="Private", state=codex_login.CodexLoginState.success, user_id=owner,
    )
    codex_login.codex_login_manager._sessions[session.id] = session
    response = await c.get(f"/api/credentials/codex/{session.id}/status", headers={"Authorization": f"Bearer {other_bearer}"})
    assert response.status_code == 404
    assert await db.load_credentials(owner) == []
    assert not Path(session.codex_home).exists()


async def test_new_login_launch_failure_removes_only_its_owned_home(monkeypatch):
    async def fail_spawn(*args, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", fail_spawn)
    mgr = codex_login.CodexLoginManager()
    try:
        with pytest.raises(RuntimeError, match="synthetic launch failure"):
            await mgr.start("new login")
        assert list(Path(codex_login.codex_home_root()).iterdir()) == []
    finally:
        await mgr.shutdown()


async def test_deleted_reauthorization_target_is_not_recreated_on_status(client, tmp_path):
    c, db = client
    owner = (await db.get_system_agent())["user_id"]
    session = codex_login.CodexLoginSession(
        id="deleted-reauth", credential_id="deleted-credential", codex_home=str(tmp_path / "deleted-home"),
        label="Deleted target", state=codex_login.CodexLoginState.success, user_id=owner, _reauth=True,
    )
    codex_login.codex_login_manager._sessions[session.id] = session
    response = await c.get(f"/api/credentials/codex/{session.id}/status")
    assert response.status_code == 200
    assert response.json()["state"] == "success"
    assert response.json()["credential"] is None
    assert await db.get_credential(session.credential_id, owner) is None


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
async def test_reauthorization_failure_keeps_credential_row_and_agent_binding(client, monkeypatch, outcome):
    from server.crypto import encrypt
    from server.users import UserManager

    c, db = client
    agent = await db.get_system_agent()
    owner = agent["user_id"]
    credential_id = "bound-reauth"
    home = Path(codex_login.codex_home_for(credential_id))
    home.mkdir(parents=True)
    original_auth = b'{"tokens":{"access_token":"original"}}'
    (home / "auth.json").write_bytes(original_auth)
    await db.save_credential(
        credential_id=credential_id, backend="codex", label="Existing", auth_type="oauth",
        secret_encrypted=encrypt(str(home), await UserManager(db).data_key_for(owner)),
        created_at="2026-10-25T00:00:00+00:00", user_id=owner,
    )
    await db.update_credential(credential_id, needs_reconnect=True, last_refresh_error_code="expired")
    await db.update_agent(agent["id"], credential_id=credential_id)
    original_row = await db.get_credential(credential_id, owner)
    monkeypatch.setenv("CODEX_FAKE_LOGIN_MODE", "hang" if outcome == "cancel" else "fail")
    response = await c.post("/api/credentials/codex/start", json={
        "label": "Replacement", "reauth_credential_id": credential_id,
    })
    assert response.status_code == 201
    login_id = response.json()["login_id"]
    session = codex_login.codex_login_manager.get(login_id)
    if outcome == "cancel":
        await c.post("/api/credentials/codex/cancel", json={"login_id": login_id})
    else:
        await asyncio.wait_for(session._task, 2)
    status = await c.get(f"/api/credentials/codex/{login_id}/status")
    assert status.json()["state"] == ("cancelled" if outcome == "cancel" else "error")
    assert await db.get_credential(credential_id, owner) == original_row
    assert (await db.get_agent(agent["id"], owner))["credential_id"] == credential_id
    assert (home / "auth.json").read_bytes() == original_auth


@pytest.fixture
def authorization_barrier(tmp_path, monkeypatch):
    script = tmp_path / "controlled_login.py"
    script.write_text(
        "import os, pathlib, time\n"
        "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
        "(home/'auth.json').write_text('{\"tokens\":{\"access_token\":\"controlled-new\"}}')\n"
        "print('https://auth.example/device TEST-CODE9', flush=True)\n"
        "while not (home/'authorize').exists(): time.sleep(.01)\n"
    )
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])


async def start_controlled(c, db, reauthorize):
    target = None
    if reauthorize:
        created = await c.post("/api/credentials", json={
            "backend": "codex", "auth_type": "oauth", "label": "Original", "secret": "old-home",
        })
        target = created.json()["id"]
        home = Path(codex_login.codex_home_for(target))
        home.mkdir(parents=True)
        (home / "auth.json").write_text('{"tokens":{"access_token":"old"}}')
        (home / "config.toml").write_text('model="keep"')
    response = await c.post("/api/credentials/codex/start", json={"label": "Started label", "reauth_credential_id": target})
    assert response.status_code == 201, response.text
    login = codex_login.codex_login_manager.get(response.json()["login_id"])
    await asyncio.wait_for(login._scraped.wait(), 3)
    return login


@pytest.mark.parametrize("reauthorize", [False, True])
@pytest.mark.parametrize("change", ["delete", "disable"])
async def test_revocation_before_publication_cannot_be_undone_by_late_login(client, authorization_barrier, reauthorize, change):
    c, db = client
    login = await start_controlled(c, db, reauthorize)
    if change == "delete":
        response = await c.delete(f"/api/credentials/{login.credential_id}")
        assert response.status_code == 204, response.text
        assert login.state == codex_login.CodexLoginState.cancelled
        assert await db.get_credential(login.credential_id) is None
        assert not Path(login.codex_home).exists()
    else:
        owner = (await db.get_system_agent())["user_id"]
        await db.set_user_disabled(owner, at="now")
        Path(login._login_home, "authorize").touch()
        await asyncio.wait_for(asyncio.shield(login._task), 3)
        assert login.state == codex_login.CodexLoginState.error
        if reauthorize:
            assert "old" in Path(login.codex_home, "auth.json").read_text()
        else:
            assert not Path(login.codex_home).exists()
    assert login.phase == codex_login.CodexLoginPhase.retired
    assert not codex_login.codex_login_manager.drain_blockers()


async def test_reauthorization_preserves_label_edits_and_different_targets_are_independent(client, authorization_barrier):
    c, db = client
    first = await start_controlled(c, db, True)
    second = await start_controlled(c, db, True)
    edited = await c.patch(f"/api/credentials/{first.credential_id}", json={"label": "Edited while authorizing"})
    assert edited.status_code == 200
    for login in (first, second):
        Path(login._login_home, "authorize").touch()
    await asyncio.gather(*(asyncio.shield(login._task) for login in (first, second)))
    row = await db.get_credential(first.credential_id)
    assert row["label"] == "Edited while authorizing"
    assert all(login.state == codex_login.CodexLoginState.success for login in (first, second))
    assert len(await db.load_credentials()) == 2


@pytest.mark.parametrize("reauthorize", [False, True])
@pytest.mark.parametrize("observer", ["status", "cancel", "delete", "shutdown", "disable"])
async def test_original_commit_survives_observer_cancellation_and_orders_later_deletion(
    client, authorization_barrier, monkeypatch, reauthorize, observer,
):
    c, db = client
    manager = codex_login.codex_login_manager
    login = await start_controlled(c, db, reauthorize)
    committing, release = asyncio.Event(), asyncio.Event()
    original = db.conn.commit
    execute = db.conn.execute
    armed = True
    material_written = False

    def publication_sql(sql, parameters=()):
        nonlocal material_written
        if sql.startswith(("INSERT INTO backend_credentials", "UPDATE backend_credentials SET")):
            material_written = True
        return execute(sql, parameters)

    async def commit():
        nonlocal armed
        if armed and material_written:
            armed = False
            committing.set()
            await release.wait()
        await original()

    monkeypatch.setattr(db.conn, "commit", commit)
    monkeypatch.setattr(db.conn, "execute", publication_sql)
    Path(login._login_home, "authorize").touch()
    waiting = None
    try:
        await asyncio.wait_for(committing.wait(), 3)
        if observer == "status":
            waiting = asyncio.create_task(c.get(f"/api/credentials/codex/{login.id}/status"))
        elif observer == "cancel":
            waiting = asyncio.create_task(c.post("/api/credentials/codex/cancel", json={"login_id": login.id}))
        elif observer == "delete":
            waiting = asyncio.create_task(c.delete(f"/api/credentials/{login.credential_id}"))
        elif observer == "disable":
            waiting = asyncio.create_task(db.set_user_disabled(login.user_id, at="now"))
        else:
            waiting = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert not waiting.done()
        if observer in {"status", "cancel"}:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        assert manager.drain_blockers()
        release.set()
        await asyncio.wait_for(asyncio.shield(login._task), 3)
        if observer in {"delete", "shutdown", "disable"}:
            result = await asyncio.wait_for(waiting, 3)
            if observer == "delete":
                assert result.status_code == 204
        assert login.state == codex_login.CodexLoginState.success
        row = await db.get_credential(login.credential_id)
        if observer == "delete":
            assert row is None and not Path(login.codex_home).exists()
        else:
            assert row is not None and row["status"] == "active"
            assert json.loads(Path(login.codex_home, "auth.json").read_text())["tokens"]["access_token"] == "controlled-new"
        if observer == "disable":
            with pytest.raises(ValueError, match="Account is unavailable"):
                await db.require_run_authority(login.user_id, login.credential_id, "codex")
        assert login.phase == codex_login.CodexLoginPhase.retired
    finally:
        release.set()
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)


async def test_known_metadata_rollback_restores_only_original_auth_and_keeps_native_history(client, authorization_barrier, monkeypatch):
    c, db = client
    login = await start_controlled(c, db, True)
    original = db.conn.execute

    def reject(sql, parameters=()):
        if sql.startswith("UPDATE backend_credentials SET"):
            raise sqlite3.IntegrityError("explicit metadata rejection")
        return original(sql, parameters)

    monkeypatch.setattr(db.conn, "execute", reject)
    Path(login._login_home, "authorize").touch()
    await asyncio.wait_for(asyncio.shield(login._task), 3)
    assert login.state == codex_login.CodexLoginState.error
    assert json.loads(Path(login.codex_home, "auth.json").read_text())["tokens"]["access_token"] == "old"
    assert Path(login.codex_home, "config.toml").read_text() == 'model="keep"'
    assert (await db.get_credential(login.credential_id))["label"] == "Original"


async def test_committed_login_retains_target_and_success_until_failed_staging_cleanup_is_confirmed(
    client, authorization_barrier, monkeypatch,
):
    c, db = client
    manager = codex_login.codex_login_manager
    login = await start_controlled(c, db, True)
    remove = codex_login.shutil.rmtree

    def unavailable(path, *args, **kwargs):
        if str(path) == login._login_home:
            raise OSError("staging cleanup unavailable")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", unavailable)
    Path(login._login_home, "authorize").touch()
    try:
        with pytest.raises(OSError, match="staging cleanup unavailable"):
            await asyncio.wait_for(asyncio.shield(login._task), 3)
        assert login.state == codex_login.CodexLoginState.success
        assert login.phase == codex_login.CodexLoginPhase.cleanup
        assert manager.drain_blockers()
        response = await c.post("/api/credentials/codex/start", json={"label": "Overlap", "reauth_credential_id": login.credential_id})
        assert response.status_code == 409
        status = (await c.get(f"/api/credentials/codex/{login.id}/status")).json()
        assert status["state"] == "success" and status["credential"]["id"] == login.credential_id
    finally:
        monkeypatch.setattr(codex_login.shutil, "rmtree", remove)
    await manager.cancel(login.id)
    assert login.state == codex_login.CodexLoginState.success
    assert login.phase == codex_login.CodexLoginPhase.retired
    assert not manager.drain_blockers()


async def test_successful_login_retires_descendants_before_releasing_its_target(client, tmp_path, monkeypatch):
    from server.process_groups import group_has_live_members_after_kill

    script = tmp_path / "login_with_descendant.py"
    script.write_text(
        "import os, pathlib, subprocess, sys\n"
        "home=pathlib.Path(os.environ['CODEX_HOME'])\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "(home/'descendant').write_text(str(child.pid))\n"
        "(home/'auth.json').write_text('{\"tokens\":{\"access_token\":\"valid\"}}')\n"
        "print('https://auth.example/device TEST-CODE9',flush=True)\n"
    )
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    c, _ = client
    response = await c.post("/api/credentials/codex/start", json={"label": "With helper"})
    login = codex_login.codex_login_manager.get(response.json()["login_id"])
    await asyncio.wait_for(asyncio.shield(login._task), 3)
    assert login.state == codex_login.CodexLoginState.success
    assert not group_has_live_members_after_kill(login._proc.pid)
    assert not codex_login.codex_login_manager.drain_blockers()


@pytest.mark.parametrize("failure,reauthorize", [
    ("commit-before", False), ("commit-after", False), ("commit-before", True),
    ("commit-after", True), ("file-ownership", True), ("ownership-check", True),
])
def test_uncertain_publication_uses_storage_fail_stop_without_guessing_or_overwriting_external_auth(tmp_path, failure, reauthorize):
    probe = Path(__file__).parent / "_fixtures" / "codex_publication_probe.py"
    result = subprocess.run([sys.executable, str(probe), str(tmp_path), failure, str(reauthorize)],
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 70, result.stderr
    assert not (tmp_path / "incorrect-success").exists()
    identity = json.loads((tmp_path / "identity.json").read_text())
    home = Path(identity["home"])
    material = json.loads((home / "auth.json").read_text())["tokens"]["access_token"]
    assert material == ("external" if failure == "file-ownership" else "fake")
    with sqlite3.connect(tmp_path / "storage.db") as reader:
        row = reader.execute("SELECT needs_reconnect FROM backend_credentials WHERE id=?", (identity["credential_id"],)).fetchone()
    if failure == "commit-after":
        assert row == (0,)
    else:
        assert row == ((1,) if reauthorize else None)
    if reauthorize:
        assert (home / "config.toml").read_text() == 'model="preserved"'


@pytest.mark.parametrize("reauthorize", [False, True])
async def test_deleted_committed_login_stays_historically_successful_without_recreating_home(client, authorization_barrier, reauthorize):
    c, db = client
    login = await start_controlled(c, db, reauthorize)
    Path(login._login_home, "authorize").touch()
    await asyncio.shield(login._task)
    assert (await c.delete(f"/api/credentials/{login.credential_id}")).status_code == 204
    for _ in range(2):
        response = await c.get(f"/api/credentials/codex/{login.id}/status")
        assert response.json()["state"] == "success" and response.json()["credential"] is None
        assert not Path(login.codex_home).exists()
        assert await db.get_credential(login.credential_id) is None


async def test_credential_deletion_keeps_home_cleanup_when_its_http_observer_is_cancelled(client, monkeypatch):
    import threading

    c, db = client
    started = await c.post("/api/credentials/codex/start", json={"label": "Delete original"})
    login = codex_login.codex_login_manager.get(started.json()["login_id"])
    await asyncio.shield(login._task)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    remove = codex_login.shutil.rmtree

    def held_remove(path, *args, **kwargs):
        if str(path) == login.codex_home:
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", held_remove)
    deleting = asyncio.create_task(c.delete(f"/api/credentials/{login.credential_id}"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await db.get_credential(login.credential_id) is None
        deleting.cancel()
        await asyncio.sleep(0)
        assert codex_login.codex_login_manager.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(deleting, return_exceptions=True)
    async with asyncio.timeout(3):
        while codex_login.codex_login_manager.drain_blockers():
            await asyncio.sleep(.01)
    assert not Path(login.codex_home).exists()


async def test_disable_while_delete_retires_login_refuses_formal_home_deletion(client, authorization_barrier, monkeypatch):
    import threading

    c, db = client
    login = await start_controlled(c, db, True)
    home = Path(login.codex_home)
    (home / "sessions").mkdir()
    (home / "sessions" / "original.jsonl").write_text("original native history")
    original_row = await db.get_credential(login.credential_id)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    remove = codex_login.shutil.rmtree

    def held_staging(path, *args, **kwargs):
        if str(path) == login._login_home:
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", held_staging)
    deleting = asyncio.create_task(c.delete(f"/api/credentials/{login.credential_id}"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await db.set_user_disabled(login.user_id, at="now")
    finally:
        release.set()
    response = await deleting
    assert response.status_code == 403, response.text
    assert await db.get_credential(login.credential_id) == original_row
    assert (home / "sessions" / "original.jsonl").read_text() == "original native history"
    assert (home / "config.toml").read_text() == 'model="keep"'
    assert json.loads((home / "auth.json").read_text())["tokens"]["access_token"] == "old"
    assert not codex_login.codex_login_manager.drain_blockers()


async def test_delete_retry_finishes_original_home_cleanup_after_confirmed_row_deletion(client, monkeypatch):
    c, db = client
    started = await c.post("/api/credentials/codex/start", json={"label": "Retry cleanup"})
    login = codex_login.codex_login_manager.get(started.json()["login_id"])
    await asyncio.shield(login._task)
    remove = codex_login.shutil.rmtree

    def failed_home(path, *args, **kwargs):
        if str(path) == login.codex_home:
            raise OSError("formal home cleanup unavailable")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", failed_home)
    try:
        with pytest.raises(OSError, match="formal home cleanup unavailable"):
            await c.delete(f"/api/credentials/{login.credential_id}")
        assert await db.get_credential(login.credential_id) is None
        assert codex_login.codex_login_manager.drain_blockers()
        assert Path(login.codex_home).exists()
    finally:
        monkeypatch.setattr(codex_login.shutil, "rmtree", remove)
    response = await c.delete(f"/api/credentials/{login.credential_id}")
    assert response.status_code == 204, response.text
    assert not Path(login.codex_home).exists()
    assert not codex_login.codex_login_manager.drain_blockers()
    assert (await c.get(f"/api/credentials/codex/{login.id}/status")).json()["state"] == "success"


async def test_shutdown_finishes_original_deleted_credential_cleanup_after_transient_failure(client, monkeypatch):
    c, db = client
    started = await c.post("/api/credentials/codex/start", json={"label": "Shutdown cleanup"})
    login = codex_login.codex_login_manager.get(started.json()["login_id"])
    await asyncio.shield(login._task)
    remove = codex_login.shutil.rmtree

    def failed_home(path, *args, **kwargs):
        if str(path) == login.codex_home:
            raise OSError("formal home cleanup unavailable")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", failed_home)
    try:
        with pytest.raises(OSError):
            await c.delete(f"/api/credentials/{login.credential_id}")
    finally:
        monkeypatch.setattr(codex_login.shutil, "rmtree", remove)
    await codex_login.codex_login_manager.shutdown()
    assert not Path(login.codex_home).exists()
    assert await db.get_credential(login.credential_id) is None
    assert not codex_login.codex_login_manager.drain_blockers()


async def test_failed_login_cleanup_retry_has_a_bounded_wait_and_keeps_original_target(client, authorization_barrier, monkeypatch):
    import threading

    c, db = client
    manager = codex_login.codex_login_manager
    login = await start_controlled(c, db, True)
    remove = codex_login.shutil.rmtree
    retrying, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    attempts = 0

    def held_retry(path, *args, **kwargs):
        nonlocal attempts
        if str(path) == login._login_home:
            attempts += 1
            if attempts == 1:
                raise OSError("first cleanup failed")
            loop.call_soon_threadsafe(retrying.set)
            release.wait(3)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(codex_login.shutil, "rmtree", held_retry)
    Path(login._login_home, "authorize").touch()
    with pytest.raises(OSError, match="first cleanup failed"):
        await asyncio.shield(login._task)
    monkeypatch.setattr(codex_login, "_REAP_TIMEOUT_SECONDS", .05)
    cancelling = asyncio.create_task(manager.cancel(login.id))
    try:
        await asyncio.wait_for(retrying.wait(), 1)
        with pytest.raises(RuntimeError, match="cleanup did not exit"):
            await asyncio.wait_for(asyncio.shield(cancelling), .5)
        assert manager.drain_blockers()
        assert manager.target_for(login.credential_id, login.user_id) is login
    finally:
        release.set()
        await asyncio.gather(cancelling, return_exceptions=True)
    await manager.cancel(login.id)
    assert login.state == codex_login.CodexLoginState.success
    assert not manager.drain_blockers()


async def test_staging_stat_error_retains_original_login_and_cleanup_responsibility(client, authorization_barrier, monkeypatch):
    c, db = client
    manager = codex_login.codex_login_manager
    login = await start_controlled(c, db, True)
    stat = os.stat

    def denied(path, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(path) == login._login_home:
            raise PermissionError("Cannot inspect original staging")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    Path(login._login_home, "authorize").touch()
    try:
        with pytest.raises(PermissionError, match="inspect original staging"):
            await asyncio.wait_for(asyncio.shield(login._task), 3)
        assert os.lstat(login._login_home)
        assert manager.target_for(login.credential_id, login.user_id) is login
        assert login.state == codex_login.CodexLoginState.success
        assert manager.drain_blockers()
    finally:
        monkeypatch.setattr(os, "stat", stat)
    await manager.cancel(login.id)
    assert not Path(login._login_home).exists() and not manager.drain_blockers()


async def test_formal_home_stat_error_keeps_committed_deletion_proof_for_http_retry(client, monkeypatch):
    c, db = client
    manager = codex_login.codex_login_manager
    started = await c.post("/api/credentials/codex/start", json={"label": "Inspect deletion"})
    login = manager.get(started.json()["login_id"])
    await asyncio.shield(login._task)
    stat = os.stat

    def denied(path, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(path) == login.codex_home:
            raise PermissionError("Cannot inspect original home")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    try:
        with pytest.raises(PermissionError, match="inspect original home"):
            await c.delete(f"/api/credentials/{login.credential_id}")
        assert await db.get_credential(login.credential_id) is None
        assert os.lstat(login.codex_home)
        assert manager.owns_credential(login.credential_id, login.user_id)
        assert manager.drain_blockers()
    finally:
        monkeypatch.setattr(os, "stat", stat)
    assert (await c.delete(f"/api/credentials/{login.credential_id}")).status_code == 204
    assert not Path(login.codex_home).exists() and not manager.drain_blockers()


@pytest.mark.parametrize("failure", ["invalid-auth", "metadata"])
async def test_login_failure_never_signals_a_group_after_confirmed_retirement(client, authorization_barrier, monkeypatch, failure):
    c, db = client
    login = await start_controlled(c, db, True)
    observed = set()
    signals_after_retirement = []
    wait_group = codex_login.wait_group_exit
    kill_group = os.killpg
    execute = db.conn.execute

    async def confirmed(pid, *args, **kwargs):
        await wait_group(pid, *args, **kwargs)
        observed.add(pid)

    def signal_group(pid, sig):
        if pid in observed:
            signals_after_retirement.append((pid, sig))
        return kill_group(pid, sig)

    def reject(sql, parameters=()):
        if sql.startswith("UPDATE backend_credentials SET"):
            raise sqlite3.IntegrityError("Confirmed publication refusal")
        return execute(sql, parameters)

    monkeypatch.setattr(codex_login, "wait_group_exit", confirmed)
    monkeypatch.setattr(os, "killpg", signal_group)
    if failure == "metadata":
        monkeypatch.setattr(db.conn, "execute", reject)
    else:
        Path(login._login_home, "auth.json").write_text("invalid authorization")
    Path(login._login_home, "authorize").touch()
    await asyncio.wait_for(asyncio.shield(login._task), 3)
    assert login.state == codex_login.CodexLoginState.error
    assert login._proc.pid in observed
    assert signals_after_retirement == []
    assert Path(login.codex_home, "config.toml").read_text() == 'model="keep"'


async def test_scrape_watchdog_cannot_reclassify_an_operation_already_publishing(client, tmp_path, monkeypatch):
    c, db = client
    script = tmp_path / "authorized_without_device_code.py"
    script.write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['CODEX_HOME'],'auth.json').write_text('{\"tokens\":{\"access_token\":\"valid\"}}')\n"
        "print('https://auth.example/device',flush=True)\n"
    )
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    entered, release = asyncio.Event(), asyncio.Event()
    expire_watchdog = asyncio.Event()
    wait_for = asyncio.wait_for
    commit, execute = db.conn.commit, db.conn.execute
    publication = False

    async def controlled_deadline(awaitable, timeout):
        if timeout != codex_login._SCRAPE_TIMEOUT_SECONDS:
            return await wait_for(awaitable, timeout)
        observed = asyncio.create_task(awaitable)
        deadline = asyncio.create_task(expire_watchdog.wait())
        try:
            done, _ = await asyncio.wait({observed, deadline}, return_when=asyncio.FIRST_COMPLETED)
            if observed in done:
                return observed.result()
            raise TimeoutError
        finally:
            for task in (observed, deadline):
                if not task.done():
                    task.cancel()
            await asyncio.gather(observed, deadline, return_exceptions=True)

    def publication_sql(sql, parameters=()):
        nonlocal publication
        if sql.startswith("INSERT INTO backend_credentials"):
            publication = True
        return execute(sql, parameters)

    async def held_commit():
        if publication:
            entered.set()
            await release.wait()
        await commit()

    monkeypatch.setattr(db.conn, "execute", publication_sql)
    monkeypatch.setattr(db.conn, "commit", held_commit)
    monkeypatch.setattr(asyncio, "wait_for", controlled_deadline)
    started = await c.post("/api/credentials/codex/start", json={"label": "Already publishing"})
    login = codex_login.codex_login_manager.get(started.json()["login_id"])
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert login.phase == codex_login.CodexLoginPhase.publishing and not login.device_code_ready
        expire_watchdog.set()
        await wait_for(asyncio.shield(login._watchdog), 1)
        assert login.state == codex_login.CodexLoginState.pending
        assert login._watchdog.done()
    finally:
        release.set()
        await asyncio.shield(login._task)
        await asyncio.shield(login._watchdog)
    assert login.state == codex_login.CodexLoginState.success

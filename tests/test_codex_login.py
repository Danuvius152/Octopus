"""Tests for in-app Codex device-auth login (server/codex_login.py + routes).

The real `codex login --device-auth` is replaced by a fake CLI fixture, so
these run anywhere (no codex binary, no ChatGPT account). The fake emits the
same URL + code shape and writes auth.json on the success path."""

import asyncio
import os
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
    codex_login.codex_login_manager._sessions.clear()
    monkeypatch.delenv("CODEX_FAKE_LOGIN_MODE", raising=False)
    yield


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
    await db.close()


# --- manager unit tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_start_scrapes_url_and_code():
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("My ChatGPT")
    # `start` returns immediately (non-blocking); the URL+code are scraped by
    # the drive task. Wait for it, then assert what was captured.
    assert os.path.isdir(session.codex_home)
    await session._task
    assert session.verification_url == "https://auth.openai.com/codex/device"
    assert session.user_code == "TEST-CODE9"


@pytest.mark.asyncio
async def test_success_writes_authjson_and_marks_success():
    mgr = codex_login.CodexLoginManager()
    session = await mgr.start("ok")
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


@pytest.mark.parametrize("outcome", ["cancel", "failure", "spawn_failure", "invalid_auth", "success"])
async def test_reauthorization_preserves_existing_home_and_only_publishes_valid_auth(tmp_path, monkeypatch, outcome):
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
        "print('https://auth.example/device TEST-CODE9', flush=True)\n"
        + ("time.sleep(30)\n" if outcome == "cancel" else
           "sys.exit(1)\n" if outcome == "failure" else
           "(home / 'auth.json').write_text('not-json')\n" if outcome == "invalid_auth" else "")
    )
    monkeypatch.setattr(codex_login, "build_codex_login_argv", lambda: [sys.executable, str(script)])
    if outcome == "spawn_failure":
        async def fail_spawn(*args, **kwargs):
            raise OSError("synthetic spawn failure")

        monkeypatch.setattr(codex_login.asyncio, "create_subprocess_exec", fail_spawn)

    mgr = codex_login.CodexLoginManager()
    try:
        if outcome == "spawn_failure":
            with pytest.raises(RuntimeError, match="synthetic spawn failure"):
                await mgr.start("Reauthorize", reauth_credential_id=credential_id)
        else:
            session = await mgr.start("Reauthorize", reauth_credential_id=credential_id)
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


async def test_codex_status_retries_and_concurrent_pollers_return_one_owned_credential(client, tmp_path):
    c, db = client
    owner = (await db.get_system_agent())["user_id"]
    session = codex_login.CodexLoginSession(
        id="successful-login", credential_id="one-credential", codex_home=str(tmp_path / "login-home"),
        label="Retryable login", state=codex_login.CodexLoginState.success, user_id=owner,
    )
    codex_login.codex_login_manager._sessions[session.id] = session
    path = f"/api/credentials/codex/{session.id}/status"
    replies = await asyncio.gather(*(c.get(path) for _ in range(5)))
    assert all(reply.status_code == 200 for reply in replies)
    assert all(reply.json()["credential"]["id"] == session.credential_id for reply in replies)
    assert len(await db.load_credentials(owner)) == 1
    # Discard all prior responses and retry after persistence.
    assert (await c.get(path)).json()["credential"]["id"] == session.credential_id
    await db.delete_credential(session.credential_id)
    deleted = await c.get(path)
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "error"
    assert deleted.json()["credential"] is None
    assert "no longer available" in deleted.json()["message"]
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
    assert not session.persisted


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
    assert response.json()["state"] == "error"
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

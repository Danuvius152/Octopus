"""Tests for in-app Codex device-auth login (server/codex_login.py + routes).

The real `codex login --device-auth` is replaced by a fake CLI fixture, so
these run anywhere (no codex binary, no ChatGPT account). The fake emits the
same URL + code shape and writes auth.json on the success path."""

import asyncio
import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from server import codex_login
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import credentials as credentials_mod
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
async def test_codex_home_credential_resolution():
    """The codex home_dir credential resolves deterministically and only when
    it's a completed login (auth.json present). Resolution now lives in
    resolve_credential_by_id(style="home_dir") + _resolve_credential's
    session→agent precedence (the old _codex_home_for, folded in)."""
    from types import SimpleNamespace

    from server.harness import get_harness
    from server.session_manager import SessionManager

    mgr = SessionManager()
    codex = get_harness("codex")  # credential_style == "home_dir"
    sess = SimpleNamespace(credential_id="credX", id="s1", user_id=None)

    # No dir yet → fall back to host login (None).
    assert await mgr._resolve_credential(sess, None, codex) is None

    home = codex_login.codex_home_for("credX")
    os.makedirs(home, exist_ok=True)
    # Dir exists but no auth.json (interrupted login) → still None.
    assert await mgr._resolve_credential(sess, None, codex) is None

    open(os.path.join(home, "auth.json"), "w").close()
    cred = await mgr._resolve_credential(sess, None, codex)
    assert cred is not None and cred.home_dir == home and cred.backend == "codex"

    # Falls back to the agent's default credential when the session has none.
    no_cred = SimpleNamespace(credential_id=None, id="s2", user_id=None)
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

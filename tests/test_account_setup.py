"""First setup creates owned resources before opening ordinary business."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.config import settings
from server.database import Database
from server.mcp_identity import mint, verify
from server.routers import auth
from server.session_manager import SessionManager
from server.users import UserManager
from server.workspace import paths_for
from tests.test_service_runtime import installation_settings


def setup_app(database):
    app = FastAPI()
    app.include_router(auth.router)
    users = UserManager(database)
    app.dependency_overrides[deps.get_user_manager] = lambda: users
    return app


@pytest.fixture
async def installations(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "setup-test-master-key")
    monkeypatch.setattr(settings, "auth_token", "setup-proof-only")
    path = str(tmp_path / "shared.db")
    first, second = Database(path), Database(path)
    await first.initialize()
    await second.initialize()
    clients = []
    try:
        for database in (first, second):
            clients.append(AsyncClient(transport=ASGITransport(app=setup_app(database)), base_url="http://setup"))
        yield clients, first
    finally:
        for client in clients:
            await client.aclose()
        await second.close()
        await first.close()


async def test_empty_database_waits_for_account_before_creating_default_agent(tmp_path):
    database = Database(str(tmp_path / "fresh.db"))
    await database.initialize()
    try:
        assert await database.load_agents(include_archived=True) == []
    finally:
        await database.close()


@pytest.mark.parametrize("proof", [None, "Bearer wrong-proof", "Basic setup-proof-only"])
async def test_setup_requires_its_configured_proof_without_creating_resources(installations, proof):
    clients, database = installations
    headers = {} if proof is None else {"Authorization": proof}
    response = await clients[0].post("/api/auth/setup", headers=headers, json={
        "username": "owner", "password": "first-account-password",
    })
    assert response.status_code == 401
    assert await database.list_users() == []
    assert await database.load_agents(include_archived=True) == []
    assert not Path(settings.users_root).exists()


@pytest.mark.parametrize("configuration", ["empty", "missing"])
async def test_unconfigured_setup_proof_keeps_the_installation_closed(installations, monkeypatch, configuration):
    from server.config import Settings

    clients, database = installations
    if configuration == "missing":
        monkeypatch.delenv("OCTOPUS_AUTH_TOKEN")
        proof = Settings(_env_file=None).auth_token
    else:
        proof = ""
    monkeypatch.setattr(settings, "auth_token", proof)
    response = await clients[0].post("/api/auth/setup", headers={"Authorization": "Bearer changeme"},
                                   json={"username": "owner", "password": "first-account-password"})
    assert response.status_code == 503
    assert await database.list_users() == []
    assert await database.load_agents(include_archived=True) == []
    assert not Path(settings.users_root).exists()
    deps.set_user_manager(UserManager(database))
    assert await deps.resolve_principal("setup-proof-only") is None


async def test_two_connections_compete_for_one_owned_first_account(installations):
    clients, database = installations
    responses = await asyncio.gather(*(
        client.post("/api/auth/setup", headers={"Authorization": "Bearer setup-proof-only"},
                    json={"username": name, "password": "first-account-password"})
        for client, name in zip(clients, ("first", "second"), strict=True)
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    accepted = next(response.json() for response in responses if response.status_code == 200)
    users = await database.list_users()
    assert len(users) == 1 and users[0]["id"] == accepted["user_id"]
    assert users[0]["is_admin"] and users[0]["dek_wrapped"]
    agents = await database.load_agents(include_archived=True)
    assert len(agents) == 1 and agents[0]["user_id"] == users[0]["id"]
    assert agents[0]["is_system"] and agents[0]["name"] == "Octo"
    assert paths_for(users[0]["id"]).workspace.is_dir()
    assert (paths_for(users[0]["id"]).agents / agents[0]["id"] / "memory").is_dir()
    before = users[0]["dek_wrapped"], agents
    login = await clients[0].post("/api/auth/login", json={
        "username": users[0]["username"], "password": "first-account-password",
    })
    assert login.status_code == 200 and login.json()["user_id"] == users[0]["id"]
    repeated = await clients[0].post("/api/auth/setup", headers={"Authorization": "Bearer setup-proof-only"},
                                   json={"username": "third", "password": "another-password"})
    assert repeated.status_code == 409
    assert ((await database.list_users())[0]["dek_wrapped"], await database.load_agents(include_archived=True)) == before


async def test_login_provisions_workspace_after_commit_before_first_mkdir_interruption(installations, tmp_path):
    clients, database = installations
    completed = subprocess.run([sys.executable, "-c", """
import asyncio,os,sys
from pathlib import Path
from server.config import settings
from server.database import Database
from server.users import UserManager
mkdir=os.mkdir
def interrupted(path,*args,**kwargs):
    candidate=Path(path)
    if candidate.name=='workspace' and candidate.parent.parent==Path(settings.users_root):
        os._exit(91)
    return mkdir(path,*args,**kwargs)
os.mkdir=interrupted
async def create():
    database=Database(sys.argv[1])
    await database.initialize()
    await UserManager(database).setup(username='owner',password='first-account-password')
asyncio.run(create())
""", str(tmp_path / "shared.db")], cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, OCTOPUS_MASTER_KEY="setup-test-master-key"),
        capture_output=True, text=True, timeout=10)
    assert completed.returncode == 91, completed.stderr
    users = await database.list_users()
    assert len(users) == 1
    before = users[0]["dek_wrapped"], await database.load_agents(include_archived=True)
    workspace = paths_for(users[0]["id"]).workspace
    assert not workspace.exists()
    response = await clients[0].post("/api/auth/login", json={
        "username": "owner", "password": "first-account-password",
    })
    assert response.status_code == 200 and response.json()["user_id"] == users[0]["id"]
    assert workspace.is_dir()
    probe = workspace / "usable.txt"
    probe.write_text("Owned workspace is writable after login")
    assert probe.read_text() == "Owned workspace is writable after login"
    assert ((await database.list_users())[0]["dek_wrapped"], await database.load_agents(include_archived=True)) == before


async def test_lost_setup_response_is_recoverable_by_ordinary_login(installations):
    clients, database = installations
    app = setup_app(database)

    async def lose_response(scope, receive, send):
        async def interrupted(message):
            if message["type"] == "http.response.start":
                raise ConnectionResetError("Response lost after the setup operation completed")
            await send(message)
        await app(scope, receive, interrupted)

    async with AsyncClient(transport=ASGITransport(app=lose_response), base_url="http://setup") as client:
        with pytest.raises(ConnectionResetError):
            await client.post("/api/auth/setup", headers={"Authorization": "Bearer setup-proof-only"},
                              json={"username": "owner", "password": "first-account-password"})
    users = await database.list_users()
    assert len(users) == 1 and users[0]["is_admin"]
    original = users[0]["dek_wrapped"], await database.load_agents(include_archived=True)
    login = await clients[0].post("/api/auth/login", json={"username": "owner", "password": "first-account-password"})
    assert login.status_code == 200 and login.json()["user_id"] == users[0]["id"]
    retry = await clients[0].post("/api/auth/setup", headers={"Authorization": "Bearer setup-proof-only"},
                                json={"username": "other", "password": "another-password"})
    assert retry.status_code == 409 and await database.count_users() == 1
    assert ((await database.list_users())[0]["dek_wrapped"], await database.load_agents(include_archived=True)) == original


@pytest.mark.parametrize("retained", ["agents_dir", "wiki_dir", "applications_dir", "attachments_dir",
                                     "large_prompts_dir", "codex_home_dir", "research_dir", "users_root", "database"])
async def test_setup_never_adopts_retained_business_files_or_rows(installations, retained):
    clients, database = installations
    source = None
    if retained == "database":
        await database.save_agent(agent_id="retained-agent", name="Original", created_at="original", updated_at="original")
    else:
        directory = Path(getattr(settings, retained))
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / (".first-account-upgrade.json" if retained == "users_root" else "original.data")
        source.write_bytes(b"retained original evidence")
    before = await database.load_agents(include_archived=True)
    response = await clients[0].post("/api/auth/setup", headers={"Authorization": "Bearer setup-proof-only"},
                                    json={"username": "owner", "password": "first-account-password"})
    assert response.status_code == 409, response.text
    assert await database.count_users() == 0
    assert await database.load_agents(include_archived=True) == before
    if source is not None:
        assert source.read_bytes() == b"retained original evidence"


async def test_setup_secret_and_signed_ownerless_scope_have_no_business_identity(installations):
    _, database = installations
    deps.set_user_manager(UserManager(database))
    signed = mint("diagnostic", user_id=None)
    assert verify(signed) is not None
    assert await deps.resolve_principal("setup-proof-only") is None
    assert await deps.resolve_principal(signed) is None


async def test_ownerless_background_admission_cannot_create_a_session(installations, tmp_path):
    _, database = installations
    await database.save_agent(agent_id="legacy", name="Retained", created_at="original", updated_at="original")
    manager = SessionManager()
    await manager.initialize(database)
    try:
        with pytest.raises(ValueError):
            await manager.create_session(agent_id="legacy", working_dir=str(tmp_path))
        assert manager.sessions == {} and await database.load_sessions() == []
    finally:
        await manager.close_session_owners()


async def test_auth_state_is_unavailable_when_account_service_is_unbound(monkeypatch):
    monkeypatch.setattr(deps, "_user_manager", None)
    app = FastAPI()
    app.include_router(auth.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://setup") as client:
        response = await client.get("/api/auth/state")
    assert response.status_code == 503


def test_real_empty_service_rejects_setup_credentials_on_business_and_websocket_entries(tmp_path):
    config, configuration = installation_settings(tmp_path)
    result = subprocess.run([sys.executable, "-c", """
import json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items(): setattr(settings,name,value)
settings.auth_token='setup-proof-only'
settings.master_key='setup-test-master-key'
from server.main import app
from server.mcp_identity import mint
with TestClient(app) as client:
    for token in ('setup-proof-only',mint('diagnostic',user_id=None)):
        headers={'Authorization':'Bearer '+token}
        for path in ('/api/sessions','/api/agents','/api/applications','/api/backends/codex/models','/mcp/research/mcp'):
            response=client.get(path,headers=headers)
            assert response.status_code==401,(path,response.status_code)
        try:
            with client.websocket_connect('/ws?token='+token) as websocket:
                websocket.receive_json()
                raise AssertionError('Ownerless websocket received business data')
        except WebSocketDisconnect as error:
            assert error.code==4001,error.code
    assert client.get('/api/auth/state').json()=={'accounts_exist':False}
print('Setup proof and signed ownerless scopes cannot enter business on a real empty service.')
""", str(configuration)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "cannot enter business" in result.stdout
    import sqlite3
    with sqlite3.connect(Path(config.db_path).as_uri() + "?mode=ro", uri=True) as reader:
        for table in ("users", "agents", "sessions", "applications", "client_submissions"):
            assert reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not Path(config.users_root).exists()

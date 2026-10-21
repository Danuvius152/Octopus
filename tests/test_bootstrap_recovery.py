"""First-account facts survive interruption as one database commit."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import crypto, deps
from server.bootstrap import BootstrapError, bootstrap_first_account
from server.config import settings
from server.connector_manager import ConnectorManager
from server.crypto import decrypt, encrypt
from server.database import Database
from server.main import app
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager
from server.wiki import account_upgrade
from server.wiki.registry import registry
from server.wiki.store import WikiError
from server.workspace import WorkspaceError, confine, paths_for
from tests.test_bootstrap import legacy as legacy
from tests.test_wiki_upgrade import registered as registered


@pytest.mark.parametrize("fault", ["exit", "before_commit", "after_commit", "cancelled_commit", "sql"])
async def test_first_account_commit_already_contains_keys_ownership_and_roots(legacy, tmp_path, fault):
    database, agent = legacy
    await database.append_message("s-old", 0, "assistant", "text", "Before bootstrap")
    configuration = tmp_path / "bootstrap-settings.json"
    configuration.write_text(json.dumps({name: getattr(settings, name) for name in (
        "auth_token", "master_key", "master_key_file", "users_root", "agents_dir", "wiki_dir",
    )}))
    program = """
import asyncio,json,os,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[2]).read_text()).items(): setattr(settings,name,value)
from server.bootstrap import bootstrap_first_account
from server.database import Database

async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    inserted=False
    execute,commit=database.conn.execute,database.conn.commit
    fault=sys.argv[3]
    async def observe(sql,parameters=()):
        nonlocal inserted
        cursor=await execute(sql,parameters)
        if sql.lstrip().startswith('INSERT INTO users '): inserted=True
        if fault=='sql' and sql.startswith('UPDATE credential_secrets '):
            raise OSError('secret write receipt lost')
        return cursor
    async def crash_after_account_commit():
        if inserted and fault=='before_commit': raise OSError('account commit not confirmed')
        await commit()
        if inserted:
            if fault=='after_commit': raise OSError('account commit receipt lost')
            if fault=='cancelled_commit': raise asyncio.CancelledError('account commit receipt cancelled')
            os._exit(71)
    database.conn.execute=observe
    database.conn.commit=crash_after_account_commit
    await bootstrap_first_account(database,username='owner',password='password1')
    raise AssertionError('account commit crash was not reached')

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "legacy.db"), str(configuration), fault],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == (71 if fault == "exit" else 70), result.stderr
    await database.close()
    restarted = Database(str(tmp_path / "legacy.db"))
    await restarted.initialize()
    try:
        await restarted.append_message("s-old", 1, "assistant", "text", "After restart")
        assert [row["content"] for row in await restarted.load_messages("s-old")] == ["Before bootstrap", "After restart"]
        users = UserManager(restarted)
        if fault in {"before_commit", "sql"}:
            assert await restarted.count_users() == 0
            assert await restarted.get_session_owner("s-old") is None
            credential = await restarted.get_credential("cred-1")
            assert credential["user_id"] is None
            assert decrypt(credential["secret_encrypted"], "the-old-token") == "sk-ant-the-real-key"
            return
        user = await users.authenticate("owner", "password1")
        credential = await restarted.get_credential("cred-1", user["id"])
        assert credential is not None, "the account commit must include credential adoption"
        assert decrypt(credential["secret_encrypted"], users.data_key(user)) == "sk-ant-the-real-key"
        assert await restarted.get_session_owner("s-old") == user["id"]
        assert (await restarted.get_agent(agent["id"]))["user_id"] == user["id"]
        assert "/home/somebody/a-repo" in json.loads(user["extra_roots"])
    finally:
        await restarted.close()


async def test_opt_out_keeps_unadopted_secrets_under_their_original_key(legacy):
    database, _ = legacy
    result = await bootstrap_first_account(database, username="owner", password="password1", adopt=False)
    retained = await database.get_credential("cred-1")
    assert retained["user_id"] is None
    assert decrypt(retained["secret_encrypted"], "the-old-token") == "sk-ant-the-real-key"
    assert await database.get_credential("cred-1", result["user_id"]) is None


def test_startup_finishes_agent_memory_handoff_after_the_account_commit(tmp_path):
    from tests.test_service_runtime import installation_settings

    config, configuration = installation_settings(tmp_path)
    environment_file = tmp_path / "startup.env"
    environment_file.write_text("OCTOPUS_AUTH_TOKEN=memory-handoff-old-20261020\n")
    environment = {name: value for name, value in os.environ.items() if not name.lower().startswith("octopus_")}
    environment.update(
        OCTOPUS_ENV_FILE=str(environment_file),
        CODEX_HOME=str(tmp_path / "default-codex"),
        CLAUDE_CONFIG_DIR=str(tmp_path / "default-claude"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    common = """
import asyncio,json,os,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items(): setattr(settings,name,value)
"""
    interrupted = common + """
from server.bootstrap import bootstrap_first_account
from server.database import Database
async def main():
    database=Database(settings.db_path); await database.initialize()
    agent=await database.get_system_agent()
    memory=Path(settings.agents_dir)/agent['id']/'memory'
    (memory/'nested').mkdir(parents=True)
    (memory/'MEMORY.md').write_bytes(b'Original index\\n')
    (memory/'nested/proof.md').write_bytes(b'Original retained fact\\n')
    execute,commit=database.conn.execute,database.conn.commit
    created=False
    async def observe(sql,params=()):
        nonlocal created
        cursor=await execute(sql,params)
        if sql.startswith('INSERT INTO users '): created=True
        return cursor
    async def stop_after_account():
        await commit()
        if created: os._exit(71)
    database.conn.execute=observe
    database.conn.commit=stop_after_account
    await bootstrap_first_account(database,username='owner',password='password1')
    raise AssertionError('the account commit checkpoint was not reached')
asyncio.run(main())
"""
    committed = subprocess.run([sys.executable, "-c", interrupted, str(configuration)], cwd=tmp_path,
                               env=environment, capture_output=True, text=True, timeout=10)
    assert committed.returncode == 71, committed.stderr
    retained = list(Path(config.agents_dir).glob("*/memory/MEMORY.md"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"Original index\n"
    assert (retained[0].parent / "nested/proof.md").read_bytes() == b"Original retained fact\n"
    restart = common + """
from server.main import app
from server import deps
from server.workspace import paths_for
async def main():
    async with app.router.lifespan_context(app):
        db=deps.get_session_manager().db
        agents=await db.load_agents(include_archived=True)
        agent=next(row for row in agents if row['user_id'] is not None)
        memory=paths_for(agent['user_id']).agents/agent['id']/'memory'
        files={str(path.relative_to(memory)):path.read_text() for path in memory.rglob('*.md')}
        Path(sys.argv[2]).write_text(json.dumps({'ready':app.state.service_health.state,'files':files}))
asyncio.run(main())
"""
    evidence = tmp_path / "startup-observation.json"
    restarted = subprocess.run([sys.executable, "-c", restart, str(configuration), str(evidence)], cwd=tmp_path,
                               env=environment, capture_output=True, text=True, timeout=10)
    assert restarted.returncode == 0, restarted.stderr
    observed = json.loads(evidence.read_text())
    assert observed["ready"] == "ready"
    assert observed["files"] == {"MEMORY.md": "Original index\n", "nested/proof.md": "Original retained fact\n"}


async def test_resume_adopts_ownerless_secrets_already_rekeyed_by_an_older_version(registered, monkeypatch):
    database, _vault, config, note = registered
    await database.save_connector_installation(
        installation_id="old-connector", kind="github", label="Old installation", auth_type="oauth",
        secret_encrypted=encrypt("old-connector-token", "the-old-token"), created_at="2026-10-20",
    )
    publish = account_upgrade.publish

    def pause_publication(*_):
        raise WikiError("Publication paused before its first effect", 409)

    monkeypatch.setattr(account_upgrade, "publish", pause_publication)
    with pytest.raises(BootstrapError, match="resume"):
        await bootstrap_first_account(database, username="owner", password="password1", session_manager=session_manager)
    users = UserManager(database)
    user = await users.authenticate("owner", "password1")
    credential = await database.get_credential("cred-1", user["id"])
    connector_secret = await database.get_connector_secret("old-connector")
    agent = await database.get_system_agent(user["id"])
    source_memory = Path(settings.agents_dir) / agent["id"] / "memory"
    target_memory = paths_for(user["id"]).agents / agent["id"] / "memory"
    shutil.copytree(target_memory, source_memory)
    memory_bytes = (source_memory / "MEMORY.md").read_bytes()
    target_identity = target_memory.stat().st_ino
    # Old releases committed DEK rekey before adopting the NULL-owner rows.
    async with database.transaction() as tx:
        await tx.execute("UPDATE backend_credentials SET user_id=NULL WHERE id='cred-1'")
        await tx.execute("UPDATE connector_installations SET user_id=NULL WHERE id='old-connector'")
        await tx.execute("UPDATE agents SET user_id=NULL WHERE id=?", (agent["id"],))
    from server.memory_handoff import handoff_agent_memory
    async with database._bootstrap_lock:
        assert await handoff_agent_memory(database) == 0
    assert (source_memory / "MEMORY.md").read_bytes() == memory_bytes
    monkeypatch.setattr(account_upgrade, "publish", publish)
    monkeypatch.setattr(deps, "_user_manager", users)
    monkeypatch.setattr(auth_router, "_db", database)
    headers = {"Authorization": f"Bearer {await users.issue_token(user['id'])}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resumed = await client.post("/api/auth/bootstrap/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["wiki"]["vault_id"] == config["vault_id"]
    assert registry.store(user["id"]).read("Library/note.md")["id"] == note["id"]
    restored_credential = await database.get_credential("cred-1", user["id"])
    restored_connector = await database.get_connector_installation("old-connector", user["id"])
    assert restored_credential["secret_encrypted"] == credential["secret_encrypted"]
    assert restored_connector["user_id"] == user["id"]
    assert await database.get_connector_secret("old-connector") == connector_secret
    assert decrypt(restored_credential["secret_encrypted"], users.data_key(user)) == "sk-ant-the-real-key"
    assert decrypt(connector_secret, users.data_key(user)) == "old-connector-token"
    assert account_upgrade.pending_upgrade() is None
    assert (await database.get_agent(agent["id"]))["user_id"] == user["id"]
    assert not source_memory.exists()
    assert (target_memory / "MEMORY.md").read_bytes() == memory_bytes
    assert target_memory.stat().st_ino == target_identity


@pytest.mark.parametrize("ancestor", [False, True])
async def test_adopted_root_cannot_follow_a_later_symlink_retarget(legacy, tmp_path, ancestor):
    database, _ = legacy
    original, replacement, link = (tmp_path / name for name in ("original", "replacement", "alias"))
    original.mkdir()
    replacement.mkdir()
    link.symlink_to(original, target_is_directory=True)
    working_dir = link / "repo" if ancestor else link
    expected = original / "repo" if ancestor else original
    await database.update_session_field("s-old", working_dir=str(working_dir))
    result = await bootstrap_first_account(database, username="owner", password="password1")
    roots = json.loads((await database.get_user(result["user_id"]))["extra_roots"])
    link.unlink()
    link.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        confine(str(working_dir), user_id=result["user_id"], extra_roots=roots)
    assert roots == [str(expected)]
    assert confine(str(expected), user_id=result["user_id"], extra_roots=roots) == str(expected)


@pytest.mark.parametrize("missing_ancestor", [False, True])
async def test_adopted_missing_root_does_not_authorize_a_later_symlink(legacy, tmp_path, missing_ancestor):
    database, _ = legacy
    missing, outside = tmp_path / "missing", tmp_path / "outside"
    working_dir = missing / "repo" if missing_ancestor else missing
    await database.update_session_field("s-old", working_dir=str(working_dir))
    result = await bootstrap_first_account(database, username="owner", password="password1")
    roots = json.loads((await database.get_user(result["user_id"]))["extra_roots"])
    assert roots == [str(working_dir)]
    assert confine(str(working_dir), user_id=result["user_id"], extra_roots=roots) == str(working_dir)

    outside.mkdir()
    destination = outside / "repo" if missing_ancestor else outside
    if missing_ancestor:
        destination.mkdir()
    missing.symlink_to(outside, target_is_directory=True)
    for requested in (working_dir, destination):
        with pytest.raises(WorkspaceError):
            confine(str(requested), user_id=result["user_id"], extra_roots=roots)


@pytest.mark.parametrize("missing_ancestor", [False, True])
async def test_resume_does_not_reapprove_an_adopted_roots_new_target(registered, tmp_path, monkeypatch, missing_ancestor):
    database, _vault, _config, _note = registered
    missing, outside = tmp_path / "missing", tmp_path / "outside"
    working_dir = missing / "repo" if missing_ancestor else missing
    await database.update_session_field("s-old", working_dir=str(working_dir))
    publish = account_upgrade.publish

    def pause_publication(*_):
        raise WikiError("Publication paused before its first effect", 409)

    monkeypatch.setattr(account_upgrade, "publish", pause_publication)
    with pytest.raises(BootstrapError, match="resume"):
        await bootstrap_first_account(database, username="owner", password="password1", session_manager=session_manager)
    users = UserManager(database)
    owner = await users.authenticate("owner", "password1")
    captured_roots = json.loads(owner["extra_roots"])
    assert str(working_dir) in captured_roots
    outside.mkdir()
    destination = outside / "repo" if missing_ancestor else outside
    if missing_ancestor:
        destination.mkdir()
    missing.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(account_upgrade, "publish", publish)
    monkeypatch.setattr(deps, "_user_manager", users)
    monkeypatch.setattr(auth_router, "_db", database)
    headers = {"Authorization": f"Bearer {await users.issue_token(owner['id'])}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resumed = await client.post("/api/auth/bootstrap/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    roots = json.loads((await database.get_user(owner["id"]))["extra_roots"])
    assert roots == captured_roots
    for requested in (working_dir, destination):
        with pytest.raises(WorkspaceError):
            confine(str(requested), user_id=owner["id"], extra_roots=roots)
    assert account_upgrade.pending_upgrade() is None


async def test_bootstrap_prepares_key_and_resolved_roots_outside_sql(legacy, tmp_path, monkeypatch):
    database, _ = legacy
    monkeypatch.setattr(settings, "master_key", "")
    monkeypatch.setattr(settings, "master_key_file", str(tmp_path / "private/master.key"))
    crypto._MASTER_CACHE.clear()
    load_key, resolve = crypto.master_key, Path.resolve
    resolutions = 0

    def outside_transaction_key():
        assert not database.conn.in_transaction, "master key loading ran inside the SQL transaction"
        return load_key()

    def outside_transaction_resolve(path, *args, **kwargs):
        nonlocal resolutions
        assert not database.conn.in_transaction, "filesystem resolution ran inside the SQL transaction"
        resolutions += 1
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(crypto, "master_key", outside_transaction_key)
    monkeypatch.setattr(Path, "resolve", outside_transaction_resolve)
    result = await bootstrap_first_account(database, username="owner", password="password1")
    assert resolutions > 0
    credential = await database.get_credential("cred-1", result["user_id"])
    user = await database.get_user(result["user_id"])
    assert decrypt(credential["secret_encrypted"], UserManager(database).data_key(user)) == "sk-ant-the-real-key"


async def test_bootstrap_rejects_a_working_root_changed_during_preparation(legacy, tmp_path, monkeypatch):
    database, _ = legacy
    original, changed = tmp_path / "original-root", tmp_path / "changed-root"
    await database.update_session_field("s-old", working_dir=str(original))
    entered, release = threading.Event(), threading.Event()
    resolve = Path.resolve

    def hold_resolution(path, *args, **kwargs):
        if path == original and not entered.is_set():
            assert not database.conn.in_transaction
            entered.set()
            assert release.wait(5), "working-root preparation was not released"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", hold_resolution)
    command = asyncio.create_task(bootstrap_first_account(database, username="owner", password="password1"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await database.update_session_field("s-old", working_dir=str(changed))
        release.set()
        with pytest.raises(BootstrapError, match="roots changed") as failure:
            await command
        assert failure.value.status_code == 409
        assert await database.count_users() == 0
        credential = await database.get_credential("cred-1")
        assert credential["user_id"] is None
        assert decrypt(credential["secret_encrypted"], "the-old-token") == "sk-ant-the-real-key"
        retried = await bootstrap_first_account(database, username="owner", password="password1")
        assert retried["extra_roots"] == [str(changed)]
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


async def test_resume_preserves_roots_changed_during_preparation(registered, tmp_path, monkeypatch):
    database, _vault, _config, _note = registered
    publish = account_upgrade.publish

    def pause_publication(*_):
        raise WikiError("Publication paused before its first effect", 409)

    monkeypatch.setattr(account_upgrade, "publish", pause_publication)
    with pytest.raises(BootstrapError, match="resume"):
        await bootstrap_first_account(database, username="owner", password="password1", session_manager=session_manager)
    monkeypatch.setattr(account_upgrade, "publish", publish)
    owner = await UserManager(database).authenticate("owner", "password1")
    original, changed = tmp_path / "original-grant", tmp_path / "changed-grant"
    await database.update_user_field(owner["id"], extra_roots=json.dumps([str(original)]))
    # Older releases could leave session adoption pending. Resolving this
    # newly granted root must not overwrite a concurrent admin grant change.
    pending = tmp_path / "pending-legacy-root"
    async with database.transaction() as tx:
        await tx.execute("UPDATE sessions SET user_id=NULL,working_dir=? WHERE id='s-old'", (str(pending),))
    entered, release = threading.Event(), threading.Event()
    resolve = Path.resolve

    def hold_resolution(path, *args, **kwargs):
        if path == pending and not entered.is_set():
            assert not database.conn.in_transaction
            entered.set()
            assert release.wait(5), "existing-root preparation was not released"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", hold_resolution)
    command = asyncio.create_task(account_upgrade.resume_upgrade(database, owner, session_manager))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await database.update_user_field(owner["id"], extra_roots=json.dumps([str(changed)]))
        release.set()
        with pytest.raises(BootstrapError, match="resume") as failure:
            await command
        assert isinstance(failure.value.__cause__, BootstrapError)
        assert failure.value.__cause__.status_code == 409
        assert json.loads((await database.get_user(owner["id"]))["extra_roots"]) == [str(changed)]
        assert account_upgrade.pending_upgrade() is not None
        await account_upgrade.resume_upgrade(database, owner, session_manager)
        roots = json.loads((await database.get_user(owner["id"]))["extra_roots"])
        assert str(changed) in roots and str(original) not in roots
        assert str(pending) in roots
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.parametrize("key_owner", ["legacy", "master"])
async def test_bootstrap_keeps_installation_client_secrets_readable_under_the_master_key(legacy, key_owner):
    database, _ = legacy
    connectors = ConnectorManager(database)
    if key_owner == "master":
        await connectors.set_client_creds("github", "client-id", "client-secret")
    else:
        await database.set_connector_oauth_client("github", "client-id", encrypt("client-secret", "the-old-token"), "2026-10-20")
    result = await bootstrap_first_account(database, username="owner", password="password1")
    assert await connectors.resolve_client_creds("github") == ("client-id", "client-secret")
    assert result["reencrypted"]["connector_oauth_clients"] == (1 if key_owner == "legacy" else 0)


async def test_cancelled_bootstrap_waiter_keeps_journal_command_serialized_through_publication(registered, monkeypatch):
    database, _, _, _ = registered
    entered, release = threading.Event(), threading.Event()
    fsync = os.fsync

    def hold_preparation(descriptor):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not entered.is_set() and any(part.startswith(".wiki-adoption-") for part in path.parts):
            entered.set()
            assert release.wait(5), "migration preparation was not released"
        return fsync(descriptor)

    monkeypatch.setattr(os, "fsync", hold_preparation)
    first = asyncio.create_task(bootstrap_first_account(database, username="owner", password="password1"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        original = account_upgrade.pending_upgrade()["upgrade_id"]
        first.cancel("HTTP waiter disconnected")
        second = asyncio.create_task(bootstrap_first_account(database, username="other", password="password2"))
        done, _ = await asyncio.wait({second}, timeout=.03)
        assert not done
        assert account_upgrade.pending_upgrade()["upgrade_id"] == original
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(BootstrapError) as rejected:
            await second
        assert rejected.value.status_code == 409
        assert await database.count_users() == 1
        assert account_upgrade.pending_upgrade(include_complete=True)["upgrade_id"] == original
        assert account_upgrade.pending_upgrade() is None
        user = await UserManager(database).authenticate("owner", "password1")
        assert await database.get_credential("cred-1", user["id"]) is not None
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)

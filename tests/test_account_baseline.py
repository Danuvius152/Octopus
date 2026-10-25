"""Retained installation evidence is refused; current owned facts survive reopening."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_database_format import refuse_start
from tests.test_service_runtime import installation_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry", ["database", "service"])
@pytest.mark.parametrize("empty_main", [False, True])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_missing_or_empty_database_with_retained_sidecar_is_refused(tmp_path, entry, empty_main, suffix):
    config, configuration = installation_settings(tmp_path)
    database = Path(config.db_path)
    if empty_main:
        database.touch()
    sidecar = Path(str(database) + suffix)
    sidecar.write_bytes(b"retained recovery input\x00\xff")
    refuse_start(configuration, entry, tmp_path)
    assert database.exists() is empty_main
    if empty_main:
        assert database.read_bytes() == b""
    assert sidecar.read_bytes() == b"retained recovery input\x00\xff"
    assert not Path(config.master_key_file).exists()


@pytest.mark.parametrize("entry", ["database", "service"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_database_alias_cannot_hide_target_recovery_files(tmp_path, entry, suffix):
    config, configuration = installation_settings(tmp_path)
    actual = tmp_path / "actual.db"
    actual.touch()
    alias = Path(config.db_path)
    alias.symlink_to(actual.name)
    sidecar = Path(str(actual) + suffix)
    sidecar.write_bytes(b"original target recovery input\x00\xff")
    refuse_start(configuration, entry, tmp_path)
    assert alias.is_symlink() and os.readlink(alias) == actual.name
    assert actual.read_bytes() == b""
    assert sidecar.read_bytes() == b"original target recovery input\x00\xff"
    assert not Path(config.master_key_file).exists()


@pytest.mark.parametrize("entry", ["database", "service"])
def test_broken_database_alias_is_preserved_and_refused(tmp_path, entry):
    config, configuration = installation_settings(tmp_path)
    actual = tmp_path / "absent-original.db"
    alias = Path(config.db_path)
    alias.symlink_to(actual.name)
    refuse_start(configuration, entry, tmp_path)
    assert alias.is_symlink() and os.readlink(alias) == actual.name
    assert not actual.exists()
    assert not Path(config.master_key_file).exists()


@pytest.mark.parametrize("resource", ["users_root", "agents_dir", "applications_dir", "wiki_dir",
                                     "attachments_dir", "large_prompts_dir", "codex_home_dir", "research_dir"])
def test_empty_database_with_retained_resource_is_refused_before_key_creation(tmp_path, resource):
    config, configuration = installation_settings(tmp_path)
    root = Path(getattr(config, resource))
    root.mkdir(parents=True)
    original = root / "retained-original.bin"
    original.write_bytes(b"retained resource\x00\xff")
    refuse_start(configuration, "service", tmp_path)
    assert original.read_bytes() == b"retained resource\x00\xff"
    assert not Path(config.db_path).exists()
    assert not Path(config.master_key_file).exists()


@pytest.fixture
async def owned_installation(tmp_path, monkeypatch):
    from server.config import settings
    from server.crypto import encrypt
    from server.database import Database
    from server.users import UserManager
    from tests.accounts import create_account

    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "")
    approved = tmp_path / "approved-project"
    approved.mkdir()
    database = Database(config.db_path)
    await database.initialize()
    try:
        user, token = await create_account(database, extra_roots=[approved])
        agent = await database.get_system_agent(user["id"])
        await database.update_agent(agent["id"], mcp_servers=[], system_prompt="Keep my saved instructions")
        memory = Path(config.users_root) / user["id"] / "agents" / agent["id"] / "memory"
        (memory / "empty").mkdir()
        (memory / "nested").mkdir()
        for name in (".memory-handoff-stage", ".memory-retirement.json", ".memory-retirement.tmp"):
            (memory / "nested" / name).write_bytes(b"ordinary owned memory\x00\xff")
        (memory / "MEMORY.md").write_bytes(b"Keep my current account memory\n")
        ciphertext = encrypt("original-owned-credential", UserManager(database).data_key(user))
        await database.save_credential("current-secret", "codex", "Saved", "api_key", ciphertext, "now", user_id=user["id"])
        original_user = await database.get_user(user["id"])
        original_agent = await database.get_agent(agent["id"])
    finally:
        await database.close()
    files = {path: path.read_bytes() for path in [Path(config.db_path), Path(config.master_key_file),
                                                *memory.rglob("*")] if path.is_file()}
    return config, configuration, original_user, original_agent, token, files


@pytest.mark.parametrize("artifact", ["journal", "complete-journal", "wiki-stage", "wiki-marker",
                                     "owned-wiki-marker", "memory-stage", "memory-receipt",
                                     "memory-receipt-temp", "legacy-memory"])
@pytest.mark.parametrize("broken_link", [False, True])
async def test_handoff_protocol_inputs_are_refused_before_storage_changes(tmp_path, owned_installation, artifact, broken_link):
    config, configuration, user, agent, _, files = owned_installation
    paths = {
        "journal": Path(config.users_root) / ".first-account-upgrade.json",
        "complete-journal": Path(config.users_root) / ".first-account-upgrade.json",
        "wiki-stage": Path(config.users_root) / ".wiki-adoption-unknown",
        "wiki-marker": Path(config.wiki_dir) / ".upgrade-owner.json",
        "owned-wiki-marker": Path(config.users_root) / user["id"] / "wiki" / ".upgrade-owner.json",
        "memory-stage": Path(config.users_root) / user["id"] / "agents" / agent["id"] / ".memory-handoff-stage",
        "memory-receipt": Path(config.agents_dir) / agent["id"] / ".memory-retirement.json",
        "memory-receipt-temp": Path(config.agents_dir) / agent["id"] / ".memory-retirement.tmp",
        "legacy-memory": Path(config.agents_dir) / agent["id"] / "memory",
    }
    path = paths[artifact]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b'{"version":1,"phase":"complete"}' if artifact == "complete-journal" else b"unknown retained protocol input\x00\xff"
    if broken_link:
        path.symlink_to(tmp_path / "absent-original")
    elif artifact in {"wiki-stage", "memory-stage", "legacy-memory"}:
        path.mkdir()
        (path / "original.bin").write_bytes(content)
    else:
        path.write_bytes(content)
    inode = path.lstat().st_ino
    refuse_start(configuration, "service", tmp_path)
    assert path.lstat().st_ino == inode
    if broken_link:
        assert path.is_symlink() and os.readlink(path) == str(tmp_path / "absent-original")
    else:
        assert (path / "original.bin" if path.is_dir() else path).read_bytes() == content
    assert {path: path.read_bytes() for path in files} == files


@pytest.mark.parametrize("db_path_style", ["absolute", "relative", "symlink"])
async def test_current_owned_service_restarts_preserve_memory_keys_and_grants(tmp_path, owned_installation, db_path_style):
    from server.database import Database

    config, configuration, user, agent, token, files = owned_installation
    values = json.loads(configuration.read_text())
    if db_path_style == "relative":
        values["db_path"] = Path(config.db_path).name
    elif db_path_style == "symlink":
        alias = tmp_path / "alias.db"
        alias.symlink_to(Path(config.db_path).name)
        values["db_path"] = str(alias)
    configuration.write_text(json.dumps(values))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"owner": user["id"], "agent": agent["id"], "token": token}))
    for index in range(2):
        result = subprocess.run([sys.executable, "-c", """
import asyncio,json,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
settings.master_key=''
from server.main import app
import httpx
expected=json.loads(Path(sys.argv[2]).read_text())
async def start():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
            headers={'Authorization':'Bearer '+expected['token']}) as client:
            agents=await client.get('/api/agents')
            assert agents.status_code==200
            assert [agent['id'] for agent in agents.json()]==[expected['agent']]
            workspace=await client.get('/api/auth/workspace')
            assert workspace.status_code==200 and workspace.json()['confined'] is True
asyncio.run(start())
""", str(configuration), str(expected)], cwd=tmp_path,
            env=dict(os.environ, PYTHONPATH=str(ROOT), OCTOPUS_ENV_FILE=str(tmp_path / "retained.env")),
            capture_output=True, text=True, timeout=15)
        log = tmp_path / f"owned-restart-{index}.log"
        log.write_text(result.stdout + result.stderr)
        assert result.returncode == 0, f"Owned service restart failed; inspect {log}"
        assert {path: path.read_bytes() for path in files if path != Path(config.db_path)} == {
            path: content for path, content in files.items() if path != Path(config.db_path)}
        database = Database(config.db_path)
        await database.initialize()
        try:
            assert await database.get_user(user["id"]) == user
            assert await database.get_agent(agent["id"]) == agent
            credential = await database.get_credential("current-secret", user["id"])
            assert credential is not None
        finally:
            await database.close()
    assert not Path(config.agents_dir).exists()


@pytest.mark.parametrize("container", ["user", "owned-agent", "legacy-agent"])
async def test_wrong_type_state_container_is_refused_before_database_preparation(tmp_path, owned_installation, container):
    import shutil

    config, configuration, user, agent, _, files = owned_installation
    container_path = {
        "user": Path(config.users_root) / user["id"],
        "owned-agent": Path(config.users_root) / user["id"] / "agents" / agent["id"],
        "legacy-agent": Path(config.agents_dir) / agent["id"],
    }[container]
    # Retain current memory outside the invalid container, as an operator's
    # original backup. Only the malformed protocol position is inspected.
    if container_path.exists():
        backup = tmp_path / "original-container-backup"
        shutil.move(container_path, backup)
        for path in list(files):
            if path.is_relative_to(container_path):
                files[backup / path.relative_to(container_path)] = files.pop(path)
    container_path.parent.mkdir(parents=True, exist_ok=True)
    container_path.write_bytes(b"retained invalid container\x00\xff")
    identity = container_path.lstat().st_ino
    refuse_start(configuration, "service", tmp_path)
    assert container_path.lstat().st_ino == identity
    assert container_path.read_bytes() == b"retained invalid container\x00\xff"
    assert {path: path.read_bytes() for path in files} == files


async def test_account_baseline_preserves_installation_markers_in_state_roots(owned_installation):
    from server.account_baseline import require_account_installation

    config, _, user, _, _, files = owned_installation
    roots = [Path(config.users_root), Path(config.agents_dir),
             Path(config.users_root) / user["id"] / "agents"]
    markers = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".octopus-installation"
        marker.write_bytes(b"existing installation reference\n")
        markers.append(marker)
    require_account_installation(config)
    assert all(marker.read_bytes() == b"existing installation reference\n" for marker in markers)
    assert {path: path.read_bytes() for path in files} == files


async def test_independent_database_does_not_inspect_unrelated_installation_files(tmp_path, monkeypatch):
    from server.config import settings
    from server.database import Database

    unrelated = tmp_path / "another-installation"
    unrelated.mkdir()
    journal = unrelated / ".first-account-upgrade.json"
    journal.write_bytes(b"retained other installation input\x00\xff")
    monkeypatch.setattr(settings, "users_root", str(unrelated))
    database = Database(str(tmp_path / "standalone.db"))
    await database.initialize()
    try:
        assert await database.load_agents() == []
    finally:
        await database.close()
    assert journal.read_bytes() == b"retained other installation input\x00\xff"

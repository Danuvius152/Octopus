"""Committed ownership, full-tree publication and interrupted source retirement."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from server.agent_manager import AgentError, AgentManager
from server.agent_memory import agent_memory_dir
from server.bootstrap import bootstrap_first_account
from server.config import settings
from server.database import Database
from server.memory_handoff import MemoryHandoffConflict, handoff_agent_memory
from server.session_manager import SessionManager
from server.users import UserManager
from tests.test_bootstrap import legacy as legacy
from tests.test_service_runtime import installation_settings

_FILES = {"MEMORY.md": b"Original index\n", "nested/proof.md": b"Original retained fact\x00\xff\n"}


def _put_memory(root: Path, files: dict[str, bytes] = _FILES) -> None:
    (root / "empty").mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
async def committed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "master_key", "test-memory-handoff-key")
    database = Database(str(tmp_path / "main.db"))
    await database.initialize()
    user = await UserManager(database).create_user(username="owner", password="password1", is_admin=True)
    await database.save_agent(agent_id="memory-agent", name="Memory agent", created_at="2026-10-20",
                              updated_at="2026-10-20", user_id=user["id"])
    source, target = agent_memory_dir("memory-agent"), agent_memory_dir("memory-agent", user["id"])
    _put_memory(source)
    try:
        yield database, user, source, target
    finally:
        await database.close()


@pytest.mark.parametrize("destination", ["missing", "empty", "identical", "different", "partial", "extra", "empty-directory"])
async def test_destination_classification_preserves_whole_trees(committed, destination):
    database, user, source, target = committed
    if destination != "missing":
        target.mkdir(parents=True)
    if destination == "identical":
        _put_memory(target)
    elif destination == "different":
        _put_memory(target, {"MEMORY.md": b"New destination bytes"})
    elif destination == "partial":
        _put_memory(target, {"MEMORY.md": _FILES["MEMORY.md"]})
    elif destination == "extra":
        _put_memory(target, {**_FILES, "new.md": b"Unrelated addition"})
    elif destination == "empty-directory":
        (target / "unrelated").mkdir()
    before = _files(target)
    async with database._bootstrap_lock:
        if destination in {"different", "partial", "extra", "empty-directory"}:
            with pytest.raises(MemoryHandoffConflict, match="different or incomplete"):
                await handoff_agent_memory(database, user_id=user["id"])
            assert _files(source) == _FILES and _files(target) == before
            assert database.health.state == "starting"
            assert not (source.parent / ".memory-retirement.json").exists()
            return
        assert await handoff_agent_memory(database, user_id=user["id"]) == 1
        assert await handoff_agent_memory(database, user_id=user["id"]) == 0
    assert not source.exists() and source.parent.is_dir()
    assert _files(target) == _FILES and (target / "empty").is_dir()
    assert not list(target.parent.glob(".memory-*"))
    assert not list(source.parent.glob(".memory-*"))


async def test_only_exact_committed_owner_authorizes_memory_including_archived(committed):
    database, user, source, target = committed
    await database.archive_agent("memory-agent")
    for identity, owner in (("null-agent", None), ("foreign-agent", "other-owner")):
        await database.save_agent(agent_id=identity, name=identity, user_id=owner,
                                  created_at="2026-10-20", updated_at="2026-10-20")
        _put_memory(agent_memory_dir(identity))
    legacy = source.parent.parent
    (legacy / ".octopus-installation").write_bytes(b"installation-reference\n")
    _put_memory(legacy / "unknown" / "memory")
    (source.parent / "provider-state").write_bytes(b"not memory")
    async with database._bootstrap_lock:
        assert await handoff_agent_memory(database, user_id=user["id"]) == 1
    assert _files(target) == _FILES
    assert (legacy / ".octopus-installation").read_bytes() == b"installation-reference\n"
    assert (source.parent / "provider-state").read_bytes() == b"not memory"
    for identity in ("null-agent", "foreign-agent", "unknown"):
        assert _files(legacy / identity / "memory") == _FILES
    assert (await database.get_agent("memory-agent"))["archived"]


async def test_destination_appearing_before_atomic_publication_is_a_no_effect_conflict(committed, monkeypatch):
    database, user, source, target = committed
    rename = os.rename

    def external_destination(before, after, *args, **kwargs):
        if Path(before).name == ".memory-handoff-stage":
            _put_memory(target, {"new.md": b"new destination must survive"})
        return rename(before, after, *args, **kwargs)

    monkeypatch.setattr(os, "rename", external_destination)
    async with database._bootstrap_lock:
        with pytest.raises(MemoryHandoffConflict, match="appeared"):
            await handoff_agent_memory(database, user_id=user["id"])
    assert database.health.state == "starting"
    assert _files(source) == _FILES
    assert _files(target) == {"new.md": b"new destination must survive"}
    assert not (source.parent / ".memory-retirement.json").exists()


@pytest.mark.parametrize("link", ["source-root", "source-file", "target-root"])
async def test_link_conflicts_preserve_memory_and_provider_state(committed, tmp_path, link):
    database, user, source, target = committed
    provider = tmp_path / "provider-home"
    _put_memory(provider, {"auth.json": b"provider auth remains here"})
    if link == "source-root":
        shutil.rmtree(source)
        source.symlink_to(provider, target_is_directory=True)
    elif link == "source-file":
        (source / "auth.json").symlink_to(provider / "auth.json")
    else:
        target.parent.mkdir(parents=True)
        target.symlink_to(provider, target_is_directory=True)
    async with database._bootstrap_lock:
        with pytest.raises(MemoryHandoffConflict):
            await handoff_agent_memory(database, user_id=user["id"])
    assert _files(provider) == {"auth.json": b"provider auth remains here"}


@pytest.mark.parametrize("separate_device", [False, True])
async def test_one_stage_copy_protocol_on_actual_same_and_different_devices(tmp_path, monkeypatch, separate_device):
    with tempfile.TemporaryDirectory(prefix="octopus-memory-", dir="/dev/shm" if separate_device else tmp_path) as destination:
        monkeypatch.setattr(settings, "users_root", str(Path(destination) / "users"))
        monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
        monkeypatch.setattr(settings, "master_key", "memory-device-test-key")
        database = Database(str(tmp_path / "main.db"))
        await database.initialize()
        agent = await database.get_system_agent()
        source = agent_memory_dir(agent["id"])
        _put_memory(source)
        assert (os.stat(source).st_dev != os.stat(destination).st_dev) is separate_device
        copyfile = shutil.copyfile
        copied = []

        def copy_to_target_filesystem(before, after, *args, **kwargs):
            result = copyfile(before, after, *args, **kwargs)
            copied.append(Path(after))
            assert Path(after).parent.name in {".memory-handoff-stage", "nested"}
            assert os.stat(after).st_dev == os.stat(destination).st_dev
            return result

        monkeypatch.setattr(shutil, "copyfile", copy_to_target_filesystem)
        try:
            summary = await bootstrap_first_account(database, username="owner", password="password1")
            assert len(copied) == len(_FILES)
            assert summary["agent_memory_moved"] == 1
            assert _files(agent_memory_dir(agent["id"], summary["user_id"])) == _FILES
            assert not source.exists()
        finally:
            await database.close()


_COMMON = """
import asyncio,json,os,shutil,stat,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items(): setattr(settings,name,value)
"""

_INTERRUPTED_BOOTSTRAP = _COMMON + """
from server.bootstrap import bootstrap_first_account
from server.database import Database
fault,behavior=sys.argv[2:4]
def stop(point):
    if fault==point:
        if behavior=='io': raise OSError('memory durability receipt unavailable')
        os._exit(71)
async def main():
    database=Database(settings.db_path); await database.initialize()
    agent=await database.get_system_agent()
    source=Path(settings.agents_dir)/agent['id']/'memory'
    (source/'nested').mkdir(parents=True); (source/'empty').mkdir()
    (source/'MEMORY.md').write_bytes(b'Original index\\n')
    (source/'nested/proof.md').write_bytes(b'Original retained fact\\x00\\xff\\n')
    unknown=source.parent.parent/'unknown'
    unknown.mkdir(); (unknown/'unrelated').write_bytes(b'unknown state')
    if len(sys.argv)>4 and sys.argv[4]=='wiki':
        from server.wiki.registry import registry
        from server.wiki.history import WikiHistory
        vault=Path(settings.default_working_dir)/'vault'
        (vault/'Library').mkdir(parents=True)
        (vault/'Library/note.md').write_bytes(b'# Original note\\nOriginal fact.\\n')
        config=registry.configure(None,vault,agent['id'])
        store=registry.store(None)
        original=store.read('Library/note.md')
        changed=WikiHistory(store).write('Library/note.md',b'# Original note\\nRetained edited fact.\\n',original['revision'],'human','retained before adoption')
        Path(sys.argv[1]).with_name('wiki-proof.json').write_text(json.dumps({'vault':config['vault_id'],'doc':original['id'],
            'revision':changed['revision'],'change':changed['change_id'],'history':len(WikiHistory(store).history('Library/note.md')['items'])}))
    copyfile,rename,link,unlink,rmdir,fsync=shutil.copyfile,os.rename,os.link,os.unlink,os.rmdir,os.fsync
    target=None
    phase='copying'
    def copy(before,after,*args,**kwargs):
        result=copyfile(before,after,*args,**kwargs)
        if '.memory-handoff-stage' in Path(after).parts: stop('stage_copy')
        return result
    def publish(before,after,*args,**kwargs):
        nonlocal target,phase
        if Path(before).name=='.memory-handoff-stage':
            target=Path(after); stop('before_publish')
            result=rename(before,after,*args,**kwargs)
            phase='published'; stop('after_publish')
            return result
        return rename(before,after,*args,**kwargs)
    def receipt(before,after,*args,**kwargs):
        result=link(before,after,*args,**kwargs)
        if Path(after).name=='.memory-retirement.json': stop('receipt_publish')
        return result
    def remove(path,*args,**kwargs):
        nonlocal phase
        path=Path(path)
        if path.name=='.memory-retirement.json': stop('before_receipt_unlink')
        result=unlink(path,*args,**kwargs)
        if path.name=='.memory-retirement.json':
            phase='receipt_removed'; stop('after_receipt_unlink')
        elif path.is_relative_to(source): stop('first_unlink')
        return result
    def remove_dir(path,*args,**kwargs):
        nonlocal phase
        result=rmdir(path,*args,**kwargs)
        if Path(path)==source:
            phase='source_removed'; stop('last_delete')
        return result
    def sync(fd):
        path=Path(os.readlink('/proc/self/fd/'+str(fd)))
        directory=stat.S_ISDIR(os.fstat(fd).st_mode)
        if '.memory-handoff-stage' in path.parts:
            stop('stage_directory_sync' if directory else 'stage_file_sync')
        if directory and (path/'.memory-handoff-stage').is_dir(): stop('stage_parent_sync')
        if not directory and path.name=='.memory-retirement.tmp': stop('receipt_file_sync')
        if target is not None and path==target.parent and phase=='published': stop('publish_parent_sync')
        if (path==source.parent and source.exists() and (source.parent/'.memory-retirement.json').is_file()
            and not (source.parent/'.memory-retirement.tmp').exists()): stop('receipt_publish_parent_sync')
        if path==source.parent and phase=='source_removed': stop('source_parent_sync')
        if path==source.parent and phase=='receipt_removed': stop('receipt_parent_sync')
        return fsync(fd)
    shutil.copyfile,os.rename,os.link,os.unlink,os.rmdir,os.fsync=copy,publish,receipt,remove,remove_dir,sync
    await bootstrap_first_account(database,username='owner',password='password1')
    raise AssertionError('memory interruption checkpoint was not reached: '+fault)
asyncio.run(main())
"""

_STARTUP = _COMMON + """
from server.main import app
from server import deps
from server.workspace import paths_for
published=[]
rename=os.rename
def observe(before,after,*args,**kwargs):
    if Path(before).name=='.memory-handoff-stage': published.append(str(after))
    return rename(before,after,*args,**kwargs)
os.rename=observe
async def main():
    async with app.router.lifespan_context(app):
        db=deps.get_session_manager().db
        agent=next(row for row in await db.load_agents(include_archived=True) if row['user_id'] is not None)
        source=Path(settings.agents_dir)/agent['id']/'memory'
        target=paths_for(agent['user_id']).agents/agent['id']/'memory'
        files={str(path.relative_to(target)):path.read_bytes().hex() for path in target.rglob('*') if path.is_file()}
        Path(sys.argv[2]).write_text(json.dumps({'ready':app.state.service_health.state,'files':files,
            'owner':agent['user_id'],'agent':agent['id'],'source':str(source),'target':str(target),'published':published}))
asyncio.run(main())
"""

_RECEIPT_DIRECTORY_FAILURE = _COMMON + """
receipt=next(Path(settings.agents_dir).glob('*/.memory-retirement.json'))
failed=receipt.parent if sys.argv[3]=='agent' else receipt.parent.parent
fsync=os.fsync
def unavailable(fd):
    path=Path(os.readlink('/proc/self/fd/'+str(fd)))
    if stat.S_ISDIR(os.fstat(fd).st_mode) and path==failed:
        raise OSError('retirement receipt namespace durability still unavailable')
    return fsync(fd)
os.fsync=unavailable
""" + _STARTUP.removeprefix(_COMMON)

_RETAINED_DIRECTORY_FAILURE = _COMMON + """
failed=Path(sys.argv[3])
fsync=os.fsync
def unavailable(fd):
    path=Path(os.readlink('/proc/self/fd/'+str(fd)))
    if stat.S_ISDIR(os.fstat(fd).st_mode) and path==failed:
        raise OSError('retirement deletion namespace durability still unavailable')
    return fsync(fd)
os.fsync=unavailable
""" + _STARTUP.removeprefix(_COMMON)

_NEW_AGENT_WITHOUT_LEGACY = _COMMON + """
from server.agent_manager import AgentManager
from server.database import Database
from server.users import UserManager
from server.agent_memory import agent_memory_dir
async def main():
    database=Database(settings.db_path); await database.initialize()
    owner=await UserManager(database).create_user(username='owner',password='password1',is_admin=True)
    agent=await AgentManager(database).create_agent(name='New owned memory',user_id=owner['id'])
    source=agent_memory_dir(agent['id'])
    target=agent_memory_dir(agent['id'],owner['id'])
    (target/'MEMORY.md').write_bytes(b'New owned memory\\n')
    Path(sys.argv[2]).write_text(json.dumps({'source':str(source),'target':str(target)}))
    await database.close()
asyncio.run(main())
"""

_WIKI_RESUME = _COMMON + """
from httpx import ASGITransport,AsyncClient
from server.main import app
from server import deps
from server.users import UserManager
from server.workspace import paths_for
from server.wiki.registry import registry
from server.wiki.history import WikiHistory
async def main():
    async with app.router.lifespan_context(app):
        db=deps.get_session_manager().db
        users=UserManager(db)
        owner=await users.authenticate('owner','password1')
        agent=await db.get_system_agent(owner['id'])
        memory=paths_for(owner['id']).agents/agent['id']/'memory'
        inode=memory.stat().st_ino
        token=await users.issue_token(owner['id'])
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/api/auth/bootstrap/resume',headers={'Authorization':'Bearer '+token})
        assert response.status_code==int(sys.argv[3]),response.text
        store=registry.store(owner['id'])
        note=store.read('Library/note.md')
        history=WikiHistory(store).history('Library/note.md')['items']
        assert memory.stat().st_ino==inode
        Path(sys.argv[2]).write_text(json.dumps({'ready':app.state.service_health.state,'vault':registry.config(owner['id'])['vault_id'],
            'doc':note['id'],'revision':note['revision'],'changes':[row['id'] for row in history],
            'files':{str(path.relative_to(memory)):path.read_bytes().hex() for path in memory.rglob('*') if path.is_file()}}))
asyncio.run(main())
"""


def _process_installation(tmp_path):
    config, configuration = installation_settings(tmp_path)
    environment_file = tmp_path / "startup.env"
    environment_file.write_text("OCTOPUS_AUTH_TOKEN=memory-crash-test-token\n")
    environment = {key: value for key, value in os.environ.items() if not key.lower().startswith("octopus_")}
    environment.update(OCTOPUS_ENV_FILE=str(environment_file), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       CODEX_HOME=str(tmp_path / "provider-codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "provider-claude"))
    for name in ("provider-codex", "provider-claude"):
        _put_memory(tmp_path / name, {"auth.json": b"provider data"})
    return config, configuration, environment


def _child(program, configuration, environment, *arguments, timeout=15):
    return subprocess.run([sys.executable, "-c", program, str(configuration), *map(str, arguments)],
                          cwd=configuration.parent, env=environment, capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("point", ["stage_copy", "after_publish", "first_unlink"])
def test_public_startup_recovers_memory_handoff_phases(tmp_path, point):
    config, configuration, environment = _process_installation(tmp_path)
    # Real crashes cover preparation, publication and source retirement. The
    # receipt tests below cover persistent I/O refusal and later recovery.
    result = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, point, "crash")
    assert result.returncode == 71, result.stderr
    observation = tmp_path / "observed.json"
    restarted = _child(_STARTUP, configuration, environment, observation)
    assert restarted.returncode == 0, restarted.stderr
    observed = json.loads(observation.read_text())
    assert observed["ready"] == "ready"
    assert observed["files"] == {name: data.hex() for name, data in _FILES.items()}
    assert not Path(observed["source"]).exists()
    target = Path(observed["target"])
    assert (target / "empty").is_dir()
    assert not list(target.parent.glob(".memory-*"))
    assert not list(Path(observed["source"]).parent.glob(".memory-*"))
    if point != "stage_copy":
        assert not observed["published"]
    assert (Path(config.agents_dir) / "unknown/unrelated").read_bytes() == b"unknown state"
    for name in ("provider-codex", "provider-claude"):
        assert _files(tmp_path / name) == {"auth.json": b"provider data"}
    # Once all three phases prove the same recovered state, one healthy repeat
    # verifies startup idempotence without another cold boot for every fault.
    if point == "after_publish":
        repeated = _child(_STARTUP, configuration, environment, observation)
        assert repeated.returncode == 0, repeated.stderr
        assert json.loads(observation.read_text())["published"] == []


@pytest.mark.parametrize("change", ["source-file", "source-addition", "source-directory", "target-file", "target-addition"])
def test_retirement_recovery_retains_new_or_changed_content_and_closes_startup(tmp_path, change):
    config, configuration, environment = _process_installation(tmp_path)
    interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "first_unlink", "crash")
    assert interrupted.returncode == 71, interrupted.stderr
    source = next(Path(config.agents_dir).glob("*/memory"))
    target = next(Path(config.users_root).glob("*/agents/*/memory"))
    if change == "source-file":
        (source / "nested/proof.md").write_bytes(b"new source fact")
    elif change == "source-addition":
        (source / "new.md").write_bytes(b"new source addition")
    elif change == "source-directory":
        (source / "new-empty").mkdir()
    elif change == "target-file":
        (target / "nested/proof.md").write_bytes(b"new target fact")
    else:
        (target / "new.md").write_bytes(b"new target addition")
    source_files, target_files = _files(source), _files(target)
    receipt = source.parent / ".memory-retirement.json"
    proof = receipt.read_bytes()
    observation = tmp_path / "admitted.json"
    restarted = _child(_STARTUP, configuration, environment, observation)
    assert restarted.returncode != 0 and "MemoryHandoffConflict" in restarted.stderr
    assert not observation.exists()
    assert _files(source) == source_files and _files(target) == target_files
    assert receipt.read_bytes() == proof


def test_receipt_in_retained_legacy_agent_root_refuses_a_changed_destination_root(tmp_path):
    config, configuration, environment = _process_installation(tmp_path)
    interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "first_unlink", "crash")
    assert interrupted.returncode == 71, interrupted.stderr
    source = next(Path(config.agents_dir).glob("*/memory"))
    target = next(Path(config.users_root).glob("*/agents/*/memory"))
    receipt = source.parent / ".memory-retirement.json"
    before = _files(source), _files(target), receipt.read_bytes()
    original_settings = configuration.read_bytes()
    alternate = tmp_path / "changed-users-root"
    changed = json.loads(original_settings)
    changed["users_root"] = str(alternate)
    configuration.write_text(json.dumps(changed))
    observation = tmp_path / "admitted.json"
    refused = _child(_STARTUP, configuration, environment, observation)
    assert refused.returncode != 0 and "MemoryHandoffConflict" in refused.stderr
    assert not observation.exists()
    assert (_files(source), _files(target), receipt.read_bytes()) == before
    assert not list(alternate.glob("*/agents/*/memory/*"))
    configuration.write_bytes(original_settings)
    recovered = _child(_STARTUP, configuration, environment, observation)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(observation.read_text())["files"] == {name: data.hex() for name, data in _FILES.items()}
    assert not source.exists() and not receipt.exists()


@pytest.mark.parametrize("failed_directory", ["agent", "legacy-root"])
def test_repeated_receipt_directory_sync_failure_never_retires_source_before_confirmation(
    tmp_path, failed_directory,
):
    with tempfile.TemporaryDirectory(prefix="octopus-receipt-", dir=tmp_path) as destination:
        config, configuration, environment = _process_installation(tmp_path)
        values = json.loads(configuration.read_text())
        values["users_root"] = str(Path(destination) / "users")
        configuration.write_text(json.dumps(values))
        interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "receipt_publish_parent_sync", "io")
        assert interrupted.returncode == 70, interrupted.stderr
        source = next(Path(config.agents_dir).glob("*/memory"))
        target = next(Path(values["users_root"]).glob("*/agents/*/memory"))
        assert source.stat().st_dev == target.stat().st_dev
        receipt = source.parent / ".memory-retirement.json"
        proof = receipt.read_bytes()
        assert receipt.stat().st_dev == source.stat().st_dev
        source_directories = {path.relative_to(source) for path in source.rglob("*") if path.is_dir()}
        assert not (source.parent / ".memory-retirement.tmp").exists()
        assert _files(source) == _FILES and _files(target) == _FILES
        observation = tmp_path / "admitted.json"
        target_identity = target.stat().st_ino
        for _ in range(2):
            restarted = _child(_RECEIPT_DIRECTORY_FAILURE, configuration, environment, observation, failed_directory)
            assert restarted.returncode == 70, restarted.stderr
            assert not observation.exists()
            assert _files(source) == _FILES and _files(target) == _FILES
            assert {path.relative_to(source) for path in source.rglob("*") if path.is_dir()} == source_directories
            assert receipt.read_bytes() == proof
            assert not (source.parent / ".memory-retirement.tmp").exists()
        recovered = _child(_STARTUP, configuration, environment, observation)
        assert recovered.returncode == 0, recovered.stderr
        observed = json.loads(observation.read_text())
        assert observed["ready"] == "ready" and observed["published"] == []
        assert observed["files"] == {name: data.hex() for name, data in _FILES.items()}
        assert target.stat().st_ino == target_identity
        assert not source.exists() and not receipt.exists()


@pytest.mark.parametrize("failed_directory", ["agent", "legacy-root"])
def test_repeated_receipt_deletion_sync_failure_keeps_fresh_startup_closed(
    tmp_path, failed_directory,
):
    with tempfile.TemporaryDirectory(prefix="octopus-retirement-", dir=tmp_path) as destination:
        config, configuration, environment = _process_installation(tmp_path)
        values = json.loads(configuration.read_text())
        values["users_root"] = str(Path(destination) / "users")
        configuration.write_text(json.dumps(values))
        interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "receipt_parent_sync", "io")
        assert interrupted.returncode == 70, interrupted.stderr
        target = next(Path(values["users_root"]).glob("*/agents/*/memory"))
        source = Path(config.agents_dir) / target.parent.name / "memory"
        receipt = source.parent / ".memory-retirement.json"
        assert source.parent.is_dir() and not source.exists() and not receipt.exists()
        assert source.parent.stat().st_dev == target.stat().st_dev
        assert not (source.parent / ".memory-retirement.tmp").exists()
        assert _files(target) == _FILES
        target_identity = target.stat().st_ino
        failed = source.parent if failed_directory == "agent" else source.parent.parent
        observation = tmp_path / "admitted.json"
        for _ in range(2):
            restarted = _child(_RETAINED_DIRECTORY_FAILURE, configuration, environment, observation, failed)
            assert restarted.returncode == 70, restarted.stderr
            assert not observation.exists()
            assert _files(target) == _FILES and target.stat().st_ino == target_identity
            assert source.parent.is_dir() and not source.exists() and not receipt.exists()
        recovered = _child(_STARTUP, configuration, environment, observation)
        assert recovered.returncode == 0, recovered.stderr
        observed = json.loads(observation.read_text())
        assert observed["ready"] == "ready" and observed["published"] == []
        assert observed["files"] == {name: data.hex() for name, data in _FILES.items()}
        updated = {**_FILES, "nested/proof.md": b"New memory after complete retirement\n"}
        (target / "nested/proof.md").write_bytes(updated["nested/proof.md"])
        repeated = _child(_STARTUP, configuration, environment, observation)
        assert repeated.returncode == 0, repeated.stderr
        observed = json.loads(observation.read_text())
        assert observed["ready"] == "ready" and observed["published"] == []
        assert observed["files"] == {name: data.hex() for name, data in updated.items()}
        assert target.stat().st_ino == target_identity
        assert source.parent.is_dir() and not source.exists() and not receipt.exists()


def test_new_owned_agent_without_legacy_directory_needs_no_legacy_namespace_creation(tmp_path):
    _config, configuration, environment = _process_installation(tmp_path)
    evidence = tmp_path / "new-agent.json"
    created = _child(_NEW_AGENT_WITHOUT_LEGACY, configuration, environment, evidence)
    assert created.returncode == 0, created.stderr
    new_agent = json.loads(evidence.read_text())
    source, target = Path(new_agent["source"]), Path(new_agent["target"])
    assert not source.parent.exists()
    observation = tmp_path / "admitted.json"
    restarted = _child(_RETAINED_DIRECTORY_FAILURE, configuration, environment, observation, source.parent)
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(observation.read_text())["ready"] == "ready"
    assert not source.parent.exists()
    assert _files(target) == {"MEMORY.md": b"New owned memory\n"}


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_receipt_recovery_rejects_non_file_temporary_without_deleting_evidence(tmp_path, kind):
    config, configuration, environment = _process_installation(tmp_path)
    interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "receipt_publish", "crash")
    assert interrupted.returncode == 71, interrupted.stderr
    source = next(Path(config.agents_dir).glob("*/memory"))
    target = next(Path(config.users_root).glob("*/agents/*/memory"))
    receipt = source.parent / ".memory-retirement.json"
    temporary = source.parent / ".memory-retirement.tmp"
    temporary.unlink()
    if kind == "directory":
        temporary.mkdir()
    else:
        os.mkfifo(temporary)
    mode = stat.S_IFMT(temporary.lstat().st_mode)
    before = _files(source), _files(target), receipt.read_bytes()
    inode = target.stat().st_ino
    observation = tmp_path / "admitted.json"
    restarted = _child(_STARTUP, configuration, environment, observation, timeout=5)
    assert restarted.returncode not in {0, 70}, restarted.stderr
    assert "MemoryHandoffConflict" in restarted.stderr
    assert not observation.exists()
    assert (_files(source), _files(target), receipt.read_bytes()) == before
    assert stat.S_IFMT(temporary.lstat().st_mode) == mode
    if kind == "directory":
        temporary.rmdir()
    else:
        temporary.unlink()
    resumed = _child(_STARTUP, configuration, environment, observation)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(observation.read_text())["files"] == {name: data.hex() for name, data in _FILES.items()}
    assert target.stat().st_ino == inode and not source.exists() and not receipt.exists()


def test_retirement_receipt_binding_is_required_before_any_further_deletion(tmp_path):
    config, configuration, environment = _process_installation(tmp_path)
    interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "first_unlink", "crash")
    assert interrupted.returncode == 71, interrupted.stderr
    source = next(Path(config.agents_dir).glob("*/memory"))
    target = next(Path(config.users_root).glob("*/agents/*/memory"))
    receipt = source.parent / ".memory-retirement.json"
    record = json.loads(receipt.read_text())
    observation = tmp_path / "admitted.json"
    # Every refusal proves no effects, so the six bindings share one interrupted
    # installation. Each admission check still runs in a fresh startup process.
    for binding in ("owner", "agent_id", "source", "target", "version", "tree"):
        changed = record | {binding: {} if binding == "tree" else "changed"}
        receipt.write_text(json.dumps(changed))
        before = _files(source), _files(target), receipt.read_bytes()
        restarted = _child(_STARTUP, configuration, environment, observation)
        assert restarted.returncode != 0 and "MemoryHandoffConflict" in restarted.stderr, (binding, restarted.stderr)
        assert not observation.exists(), binding
        assert (_files(source), _files(target), receipt.read_bytes()) == before, binding


def test_authenticated_wiki_resume_after_memory_unlink_crash_preserves_original_history_and_journal(tmp_path):
    config, configuration, environment = _process_installation(tmp_path)
    interrupted = _child(_INTERRUPTED_BOOTSTRAP, configuration, environment, "first_unlink", "crash", "wiki")
    assert interrupted.returncode == 71, interrupted.stderr
    proof = json.loads((tmp_path / "wiki-proof.json").read_text())
    journal_path = Path(config.users_root) / ".first-account-upgrade.json"
    journal = json.loads(journal_path.read_text())
    assert journal["phase"] == "account_created"
    observation = tmp_path / "wiki-observation.json"
    resumed = _child(_WIKI_RESUME, configuration, environment, observation, 200)
    assert resumed.returncode == 0, resumed.stderr
    observed = json.loads(observation.read_text())
    assert observed["ready"] == "ready"
    assert all(observed[key] == proof[key] for key in ("vault", "doc", "revision"))
    assert len(observed["changes"]) == proof["history"] and proof["change"] in observed["changes"]
    assert observed["files"] == {name: data.hex() for name, data in _FILES.items()}
    complete = json.loads(journal_path.read_text())
    assert complete["upgrade_id"] == journal["upgrade_id"] and complete["phase"] == "complete"
    assert (Path(config.default_working_dir) / "vault/Library/note.md").read_bytes() == b"# Original note\nRetained edited fact.\n"
    repeated = _child(_WIKI_RESUME, configuration, environment, observation, 404)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(observation.read_text()) == observed
    assert json.loads(journal_path.read_text())["upgrade_id"] == journal["upgrade_id"]


@pytest.mark.parametrize("operation", ["create", "unarchive", "delete"])
async def test_memory_crud_contention_refuses_before_database_or_filesystem_effect(committed, operation):
    database, user, source, target = committed
    await database.archive_agent("memory-agent")
    manager = AgentManager(database)
    before = await database.get_agent("memory-agent"), _files(source), _files(target)
    async with database._bootstrap_lock:
        with pytest.raises(AgentError, match="busy"):
            if operation == "create":
                await manager.create_agent(name="Must not exist", user_id=user["id"])
            elif operation == "unarchive":
                await manager.unarchive_agent("memory-agent", user["id"])
            else:
                await manager.delete_agent("memory-agent")
    assert (await database.get_agent("memory-agent"), _files(source), _files(target)) == before
    assert await database.get_agent_by_name("Must not exist", user_id=user["id"]) is None


async def test_memory_crud_cannot_queue_behind_a_previously_waiting_bootstrap(committed):
    database, user, source, target = committed
    entered, release = asyncio.Event(), asyncio.Event()
    lock = database._bootstrap_lock
    await lock.acquire()

    async def installation_command():
        async with lock:
            entered.set()
            await release.wait()

    bootstrap = asyncio.create_task(installation_command())
    await asyncio.sleep(0)
    mutation = asyncio.create_task(AgentManager(database).create_agent(name="Must not queue", user_id=user["id"]))
    # The mutation's owned command is ready before the older lock waiter is
    # awakened. It observes an unlocked lock with that waiter still queued.
    await asyncio.sleep(0)
    lock.release()
    try:
        await entered.wait()
        completed, _ = await asyncio.wait({mutation}, timeout=.5)
        assert completed == {mutation}
        with pytest.raises(AgentError, match="busy"):
            await mutation
        assert await database.get_agent_by_name("Must not queue", user_id=user["id"]) is None
        assert _files(source) == _FILES and not target.exists()
    finally:
        release.set()
        await asyncio.gather(bootstrap, mutation, return_exceptions=True)


@pytest.mark.parametrize("operation", ["unarchive", "delete"])
async def test_owned_pending_memory_blocks_row_changes_but_null_legacy_memory_remains_usable(committed, operation):
    database, user, source, target = committed
    await database.archive_agent("memory-agent")
    manager = AgentManager(database)
    with pytest.raises(AgentError, match="incomplete"):
        if operation == "unarchive":
            await manager.unarchive_agent("memory-agent", user["id"])
        else:
            await manager.delete_agent("memory-agent")
    assert (await database.get_agent("memory-agent"))["archived"]
    assert _files(source) == _FILES and not target.exists()
    async with database.transaction() as transaction:
        await transaction.execute("UPDATE agents SET user_id=NULL WHERE id='memory-agent'")
    if operation == "unarchive":
        assert not (await manager.unarchive_agent("memory-agent"))["archived"]
        assert _files(source) == _FILES
    else:
        await manager.delete_agent("memory-agent")
        assert await database.get_agent("memory-agent") is None and not source.parent.exists()


async def test_unarchive_during_handoff_never_waits_for_command_draining_lifecycle(committed):
    database, user, source, target = committed
    await database.archive_agent("memory-agent")
    manager = SessionManager()
    await manager.initialize(database)
    manager._legacy_transition = True
    async with database._bootstrap_lock:
        with pytest.raises(AgentError, match="handoff"):
            await asyncio.wait_for(manager.unarchive_agent("memory-agent", user["id"]), 1)
    manager._legacy_transition = False
    async with database._bootstrap_lock:
        with pytest.raises(AgentError, match="busy"):
            await asyncio.wait_for(manager.unarchive_agent("memory-agent", user["id"]), 1)
    assert (await database.get_agent("memory-agent"))["archived"]
    assert _files(source) == _FILES and not target.exists()


async def test_cancelled_handoff_waiter_joins_copy_before_releasing_installation_command(committed, monkeypatch):
    database, user, source, target = committed
    entered, release = threading.Event(), threading.Event()
    copyfile = shutil.copyfile

    def paused_copy(before, after, *args, **kwargs):
        result = copyfile(before, after, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(shutil, "copyfile", paused_copy)

    async def command():
        async with database._bootstrap_lock:
            await handoff_agent_memory(database, user_id=user["id"])

    pending = asyncio.create_task(command())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and database._bootstrap_lock.locked()
        with pytest.raises(AgentError, match="busy"):
            await AgentManager(database).delete_agent("memory-agent")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not database._bootstrap_lock.locked() and not source.exists()
    assert _files(target) == _FILES


@pytest.mark.parametrize("operation", ["create", "unarchive", "delete"])
async def test_entered_memory_crud_finishes_before_bootstrap_and_other_crud_has_no_effect(legacy, operation):
    database, _system = legacy
    await database.save_agent(agent_id="entered-agent", name="Entered agent", created_at="2026-10-20", updated_at="2026-10-20")
    source = agent_memory_dir("entered-agent")
    _put_memory(source)
    await database.archive_agent("entered-agent")
    manager = AgentManager(database)
    from server.session_manager import session_manager

    # A genuine second SQLite writer holds the mutation inside its complete
    # command, leaving the asyncio loop free to admit the bootstrap contender.
    with sqlite3.connect(database._db_path) as outside:
        outside.execute("BEGIN IMMEDIATE")
        if operation == "create":
            mutation = asyncio.create_task(manager.create_agent(name="Entered create"))
        elif operation == "unarchive":
            mutation = asyncio.create_task(session_manager.unarchive_agent("entered-agent", None))
        else:
            mutation = asyncio.create_task(manager.delete_agent("entered-agent"))
        async with asyncio.timeout(2):
            while not database._bootstrap_lock.locked():
                if mutation.done():
                    await mutation
                await asyncio.sleep(.001)
        assert database._bootstrap_lock.locked()
        bootstrap = asyncio.create_task(bootstrap_first_account(
            database, username="owner", password="password1", session_manager=session_manager,
        ))
        try:
            await asyncio.sleep(0)
            assert not bootstrap.done() and not mutation.done()
            with pytest.raises(AgentError, match="busy"):
                await manager.create_agent(name="Refused create")
            assert outside.execute("SELECT count(*) FROM users").fetchone()[0] == 0
            assert outside.execute("SELECT count(*) FROM agents WHERE name='Refused create'").fetchone()[0] == 0
        finally:
            outside.rollback()
    result, summary = await asyncio.wait_for(asyncio.gather(mutation, bootstrap), 5)
    assert not database._bootstrap_lock.locked()
    if operation == "delete":
        assert result is None and await database.get_agent("entered-agent") is None
        assert not source.parent.exists()
    else:
        identity = result["id"] if operation == "create" else "entered-agent"
        current = await database.get_agent(identity)
        assert current["user_id"] == summary["user_id"] and not current["archived"]
        memory = agent_memory_dir(identity, summary["user_id"])
        assert memory.is_dir()
        if operation == "unarchive":
            assert _files(memory) == _FILES
    assert await database.get_agent_by_name("Refused create") is None

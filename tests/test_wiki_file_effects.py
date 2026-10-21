"""Authority file effects cannot publish success without durable confirmation."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_service_runtime import installation_settings

_SETUP = r'''
import asyncio,json,os,sys
from pathlib import Path
from server.config import Settings,settings
from server.wiki.registry import WikiRegistry
from server.wiki.history import WikiHistory
from server.wiki import app_bridge
base=Path(sys.argv[1]).parent
os.environ['CODEX_HOME']=str(base/'default-codex')
os.environ['CLAUDE_CONFIG_DIR']=str(base/'default-claude')
config=Settings(_env_file=None,**json.loads(Path(sys.argv[1]).read_text()))
for name,value in config.model_dump().items():setattr(settings,name,value)
root=base/'viewer.data/vaults/obv/repo'
root.mkdir(parents=True,exist_ok=True)
target=root/'note.md'
target.write_bytes(b'# Original\n')
registry=WikiRegistry()
registry.configure(None,root,'curator','viewer')
app_bridge.registry=registry
store=registry.store(None)
history=WikiHistory(store)
marker=Path(sys.argv[2])
'''


def _process(config_file, marker, operation):
    return subprocess.run(
        [sys.executable, "-c", _SETUP + operation, str(config_file), str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )


def test_external_body_sync_failure_retains_intent_before_recording_success(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
async def main():
    mutation=await app_bridge.begin(
        {'id':'viewer','app_dir':str(base/'viewer')},'POST','save',{'vault':'obv'},
        json.dumps({'path':'note.md','content':'# Changed\n','revision':store.read('note.md')['revision']}).encode())
    assert mutation is not None
    target.write_bytes(b'# Changed\n')
    fsync=os.fsync
    def unavailable(fd):
        if Path(os.readlink(f'/proc/self/fd/{fd}'))==root:
            marker.with_suffix('.sync').touch()
            raise OSError('external body directory confirmation lost')
        return fsync(fd)
    os.fsync=unavailable
    try:await mutation.finish()
    except Exception:pass
    marker.touch()
asyncio.run(main())
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    state = Path(config.wiki_dir)
    assert (config_file.parent / "viewer.data/vaults/obv/repo/note.md").read_bytes() == b"# Changed\n"
    retained = json.loads((state / "external-change.json").read_text())
    assert retained["paths"] == ["note.md"]
    with sqlite3.connect(state / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes WHERE status='applied'").fetchone()[0] == 0


@pytest.mark.parametrize("effect", ["rewrite", "replace"])
@pytest.mark.parametrize("fault", ["file", "directory", None])
def test_same_byte_external_effect_is_confirmed_before_retiring_intent(tmp_path, effect, fault):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, f"effect={effect!r}\nfault={fault!r}\n" + r'''
async def main():
    original=target.read_bytes()
    mutation=await app_bridge.begin(
        {'id':'viewer','app_dir':str(base/'viewer')},'POST','save',{'vault':'obv'},
        json.dumps({'path':'note.md','content':original.decode(),'revision':store.read('note.md')['revision']}).encode())
    assert mutation is not None
    inode=target.stat().st_ino
    if effect=='rewrite':
        target.write_bytes(original)
    else:
        temporary=target.with_suffix('.replacement')
        temporary.write_bytes(original)
        os.replace(temporary,target)
        assert target.stat().st_ino!=inode
    assert target.read_bytes()==original
    fsync=os.fsync
    confirmed=[]
    def unavailable(fd):
        path=Path(os.readlink(f'/proc/self/fd/{fd}'))
        confirmed.append(str(path))
        if (fault=='file' and path==target) or (fault=='directory' and path==root):
            marker.with_suffix('.sync').touch()
            raise OSError('same-byte effect confirmation lost')
        return fsync(fd)
    os.fsync=unavailable
    try:await mutation.finish()
    except Exception:pass
    marker.write_text(json.dumps(confirmed))
    registry.close()
asyncio.run(main())
''')
    state = Path(config.wiki_dir)
    root = config_file.parent / "viewer.data/vaults/obv/repo"
    assert (root / "note.md").read_bytes() == b"# Original\n"
    with sqlite3.connect(state / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes").fetchone()[0] == 0
    if fault is not None:
        assert marker.with_suffix(".sync").is_file(), result.stderr
        assert result.returncode == 70, result.stderr
        assert not marker.exists()
        assert json.loads((state / "external-change.json").read_text())["paths"] == ["note.md"]
    else:
        assert result.returncode == 0, result.stderr
        confirmed = json.loads(marker.read_text())
        assert str(root / "note.md") in confirmed and str(root) in confirmed
        assert not (state / "external-change.json").exists()


def test_history_reads_do_not_create_or_confirm_a_blob_directory(tmp_path):
    _, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
if history.blobs.exists():history.blobs.rmdir()
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==store.state:
        raise OSError('read unexpectedly confirmed blob namespace')
    return fsync(fd)
os.fsync=unavailable
fresh=WikiHistory(store)
assert fresh.history('note.md')['items']==[]
assert fresh.snapshots()['items']==[]
assert not fresh.blobs.exists()
os.fsync=fsync
assert fresh.cleanup()['removed_objects']==0
assert not fresh.blobs.exists()
original=target.read_bytes()
row=fresh.record('note.md',original,original,'human','First retained text','applied')
assert (fresh.blobs/row['before_hash']).read_bytes()==original
assert len(fresh.history('note.md')['items'])==1
registry.close()
marker.touch()
''')
    assert result.returncode == 0 and marker.is_file(), result.stderr


def test_first_blob_directory_is_confirmed_before_a_prepared_reference(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
if history.blobs.exists():history.blobs.rmdir()
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==store.state and history.blobs.exists():
        marker.with_suffix('.sync').touch()
        raise OSError('new blob directory confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:history.record('note.md',target.read_bytes(),b'# Proposal\n','human','First prepared text','prepared')
except Exception:pass
marker.touch()
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70 and not marker.exists(), result.stderr
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes").fetchone()[0] == 0


def test_external_intent_retirement_sync_failure_never_returns_success(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
async def main():
    mutation=await app_bridge.begin(
        {'id':'viewer','app_dir':str(base/'viewer')},'POST','save',{'vault':'obv'},
        json.dumps({'path':'note.md','content':'# Changed\n','revision':store.read('note.md')['revision']}).encode())
    target.write_bytes(b'# Changed\n')
    journal=store.state/'external-change.json'
    fsync=os.fsync
    def unavailable(fd):
        if Path(os.readlink(f'/proc/self/fd/{fd}'))==store.state and not journal.exists():
            marker.with_suffix('.sync').touch()
            raise OSError('external intent retirement confirmation lost')
        return fsync(fd)
    os.fsync=unavailable
    try:await mutation.finish()
    except Exception:pass
    marker.touch()
asyncio.run(main())
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes WHERE status='applied'").fetchone()[0] == 1


@pytest.mark.parametrize("missing_marker", [False, True])
def test_vault_claim_sync_failure_cannot_open_a_writer(tmp_path, missing_marker):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, f"missing_marker={missing_marker!r}\n" + r'''
registry.close()
registry.resume_access()
if missing_marker:(root/'.octopus-vault').unlink()
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==root:
        marker.with_suffix('.sync').touch()
        raise OSError('vault claim namespace confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:registry.store(None)
except Exception:pass
marker.touch()
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    root = config_file.parent / "viewer.data/vaults/obv/repo"
    assert (root / ".octopus-vault").read_text().strip() == str(Path(config.wiki_dir).resolve())
    assert json.loads((Path(config.wiki_dir) / "config.json").read_text())["root"] == str(root)


def test_blob_publication_sync_failure_retains_bytes_without_a_success_receipt(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==history.blobs:
        marker.with_suffix('.sync').touch()
        raise OSError('blob namespace confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:history.record('note.md',b'# Original\n',b'# Proposal\n','human','Blob proof','prepared')
except Exception:pass
marker.touch()
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    assert b"# Original\n" in [path.read_bytes() for path in (Path(config.wiki_dir) / "blobs").iterdir()]
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes").fetchone()[0] == 0


@pytest.mark.parametrize("already_retired", [False, True])
def test_snapshot_retirement_confirms_tombstone_before_discarding_manifest(tmp_path, already_retired):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, f"already_retired={already_retired!r}\n" + r'''
identifier='a'*32
manifest=store.state/'snapshots'/f'{identifier}.json'
manifest.parent.mkdir(exist_ok=True)
raw=json.dumps({'id':identifier,'created_at':'2026-10-20T00:00:00+00:00','reason':'Historical snapshot','files':{}})
manifest.write_text(raw)
if already_retired:
    history.retire_snapshot(identifier,'Initial retirement')
    manifest.write_text(raw)
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==store.state/'retired-snapshots':
        marker.with_suffix('.sync').touch()
        raise OSError('snapshot retirement namespace confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:history.retire_snapshot(identifier,'Confirm retirement')
except Exception:pass
marker.touch()
''')
    assert marker.with_suffix(".sync").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    state = Path(config.wiki_dir)
    assert (state / "snapshots" / ("a" * 32 + ".json")).is_file()
    assert json.loads((state / "retired-snapshots" / ("a" * 32 + ".json")).read_text())["recoverable"] is False


def test_failed_derived_cleanup_export_keeps_confirmed_history_usable(tmp_path):
    _, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, r'''
from server.wiki import retention
atomic=retention.atomic
def unavailable(path,raw):
    if path.name=='last-cleanup.json':
        marker.with_suffix('.export').touch()
        raise OSError('derived cleanup export unavailable')
    return atomic(path,raw)
retention.atomic=unavailable
try:history.cleanup()
except OSError:pass
else:raise AssertionError('derived export failure was not observed')
assert store.health.cause is None
history.write('note.md',b'# Still usable\n',store.read('note.md')['revision'],'human','After export failure')
assert store.read('note.md')['content']=='# Still usable\n'
assert len(history.history('note.md')['items'])==1
registry.close()
marker.touch()
''')
    assert result.returncode == 0, result.stderr
    assert marker.with_suffix(".export").is_file() and marker.is_file()


@pytest.mark.parametrize("delete", [False, True])
def test_body_confirmation_failure_preserves_exact_evidence_across_cold_reopens(tmp_path, delete):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = _process(config_file, marker, f"delete={delete!r}\n" + r'''
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==root:
        marker.with_suffix('.sync').touch()
        raise OSError('body namespace confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:history.write('note.md',None if delete else b'# Confirmed bytes\n',store.read('note.md')['revision'],'human','Original operation')
except Exception:pass
marker.touch()
''')
    assert result.returncode == 70 and marker.with_suffix(".sync").is_file(), result.stderr
    assert not marker.exists()
    state = Path(config.wiki_dir)
    root = config_file.parent / "viewer.data/vaults/obv/repo"
    expected = None if delete else b"# Confirmed bytes\n"
    inode = (root / "note.md").stat().st_ino if not delete else None
    config_bytes = (state / "config.json").read_bytes()
    with sqlite3.connect(state / "index.sqlite3") as reader:
        change = reader.execute("SELECT id,before_hash,after_hash,status FROM changes").fetchone()
        identity = reader.execute("SELECT document_id FROM change_retention WHERE change_id=?", (change[0],)).fetchone()[0]
    assert change[3] == "prepared"
    assert (state / "blobs" / change[1]).read_bytes() == b"# Original\n"
    cold_setup = _SETUP[:_SETUP.index("root=base/'viewer.data")]
    cold_operation = r'''
from server.wiki.recovery import Metadata
registry=WikiRegistry()
metadata=Metadata(registry,None)
assert metadata.snapshot()['root_available']
with metadata.connect() as db:
    changes=[dict(row) for row in db.execute('SELECT id,before_hash,after_hash,status FROM changes')]
    identities=[row[0] for row in db.execute('SELECT document_id FROM change_retention')]
Path(sys.argv[2]).write_text(json.dumps({'changes':changes,'identities':identities}))
registry.close()
'''
    for _ in range(2):
        reopened = subprocess.run(
            [sys.executable, "-c", cold_setup + cold_operation, str(config_file), str(marker)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
        )
        assert reopened.returncode == 0, reopened.stderr
        observed = json.loads(marker.read_text())
        assert observed["changes"] == [dict(zip(("id", "before_hash", "after_hash", "status"), change, strict=True))]
        assert observed["identities"] == [identity]
        assert (state / "config.json").read_bytes() == config_bytes
        assert ((root / "note.md").read_bytes() if (root / "note.md").exists() else None) == expected
        if inode is not None:
            assert (root / "note.md").stat().st_ino == inode

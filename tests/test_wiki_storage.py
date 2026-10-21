"""Wiki authority connections use the same durable-write contract."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.config import settings
from server.wiki.recovery import Metadata
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError
from tests.test_service_runtime import installation_settings


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    root.mkdir()
    registry = WikiRegistry()
    registry.configure(None, root, "curator")
    try:
        yield registry, registry.store(None)
    finally:
        registry.close()


@pytest.mark.parametrize("entry", ["store", "inbox", "recovery_writer"])
def test_each_authoritative_wiki_connection_confirms_extra_durability(registered, entry):
    registry, store = registered
    if entry == "store":
        context = store.connect()
    elif entry == "inbox":
        context = store.events.journal.connect()
    else:
        context = Metadata(registry, None).connect(write=True)
    with context as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 3


@pytest.mark.parametrize("entry", ["store", "inbox", "recovery_writer"])
@pytest.mark.parametrize("committed", [False, True])
def test_unknown_wiki_commit_exits_before_an_outer_handler_can_admit_more_work(tmp_path, entry, committed):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    program = """
import json,os,sqlite3,sys
from pathlib import Path
base=Path(sys.argv[1]).parent
os.environ['CODEX_HOME']=str(base/'default-codex')
os.environ['CLAUDE_CONFIG_DIR']=str(base/'default-claude')
from server.config import Settings,settings
from server.wiki.registry import WikiRegistry
from server.wiki.recovery import Metadata
config=Settings(_env_file=None,**json.loads(Path(sys.argv[1]).read_text()))
for name,value in config.model_dump().items(): setattr(settings,name,value)
root=base/'vault'
root.mkdir()
registry=WikiRegistry()
registry.configure(None,root,'curator')
store=registry.store(None)
original_connect=sqlite3.connect
class FaultyConnection(sqlite3.Connection):
    def commit(self):
        if not self.in_transaction:
            return super().commit()
        Path(sys.argv[4]+'.commit-started').touch()
        if sys.argv[3]=='True':
            super().commit()
        raise OSError('authoritative commit acknowledgment unavailable')
    def __exit__(self,error_type,error,traceback):
        if error_type is None:
            self.commit()
        else:
            self.rollback()
        return False
def connect(*args,**kwargs):
    kwargs['factory']=FaultyConnection
    return original_connect(*args,**kwargs)
sqlite3.connect=connect
try:
    if sys.argv[2]=='store':
        context=store.connect()
    elif sys.argv[2]=='inbox':
        context=store.events.journal.connect()
    else:
        context=Metadata(registry,None).connect(write=True)
    with context as db:
        db.execute("INSERT INTO app_trash(trash_id,path,document_id,management) VALUES('commit-proof','Library/proof.md',NULL,NULL)")
except Exception:
    pass
Path(sys.argv[4]).touch()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(config_file), entry, str(committed), str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert marker.with_name(marker.name + ".commit-started").is_file(), result.stderr
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM app_trash WHERE trash_id='commit-proof'").fetchone()[0] == int(committed)
    assert result.returncode == 70, result.stderr
    assert not marker.exists()


def test_unconfirmed_body_directory_sync_exits_after_the_real_replace(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    program = """
import json,os,sys
from pathlib import Path
base=Path(sys.argv[1]).parent
os.environ['CODEX_HOME']=str(base/'default-codex')
os.environ['CLAUDE_CONFIG_DIR']=str(base/'default-claude')
from server.config import Settings,settings
from server.wiki.registry import WikiRegistry
from server.wiki.history import WikiHistory
config=Settings(_env_file=None,**json.loads(Path(sys.argv[1]).read_text()))
for name,value in config.model_dump().items(): setattr(settings,name,value)
root=base/'vault'
root.mkdir()
target=root/'note.md'
target.write_bytes(b'# Original body\\n')
registry=WikiRegistry()
registry.configure(None,root,'curator')
store=registry.store(None)
original=store.read('note.md')
history=WikiHistory(store)
fsync=os.fsync
def unconfirmed_directory(descriptor):
    if Path(os.readlink(f'/proc/self/fd/{descriptor}'))==root:
        Path(sys.argv[2]+'.effect-started').touch()
        raise OSError('body replacement directory sync unavailable')
    return fsync(descriptor)
os.fsync=unconfirmed_directory
try:
    history.write('note.md',b'# Published body\\n',original['revision'],'human','publish')
except Exception:
    pass
Path(sys.argv[2]).touch()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(config_file), str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert marker.with_name(marker.name + ".effect-started").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    assert (Path(config_file).parent / "vault/note.md").read_bytes() == b"# Published body\n"
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        assert reader.execute("SELECT count(*) FROM changes WHERE status='prepared'").fetchone()[0] == 1


def test_registered_ledger_loss_never_initializes_replacement_identities(registered):
    registry, store = registered
    state, root = store.state, store.root
    registration = (state / "config.json").read_bytes()
    registry.close()
    store.db_path.unlink()
    fresh = WikiRegistry()
    try:
        with pytest.raises(WikiError, match="ledger|database") as failure:
            fresh.store(None)
        assert failure.value.status == 503
        assert not store.db_path.exists()
        assert (state / "config.json").read_bytes() == registration
        assert root.is_dir()
    finally:
        fresh.close()


def registration_process(config_file, marker, operation):
    """Exercise public registration/file owners in a fresh isolated process."""
    prefix = """
import json,os,sqlite3,sys
from pathlib import Path
base=Path(sys.argv[1]).parent
os.environ['CODEX_HOME']=str(base/'default-codex')
os.environ['CLAUDE_CONFIG_DIR']=str(base/'default-claude')
from server.config import Settings,settings
from server.wiki.registry import WikiRegistry
from server.wiki.history import WikiHistory
config=Settings(_env_file=None,**json.loads(Path(sys.argv[1]).read_text()))
for name,value in config.model_dump().items(): setattr(settings,name,value)
root=base/'vault'
root.mkdir(exist_ok=True)
state=Path(config.wiki_dir)
marker=Path(sys.argv[2])
registry=WikiRegistry()
"""
    return subprocess.run(
        [sys.executable, "-c", prefix + operation, str(config_file), str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )


def test_first_registration_publishes_a_complete_bound_ledger_before_configuration(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "registration-proof.json"
    interrupted = registration_process(config_file, marker, """
replace=os.replace
def stop_before_registration(source,destination):
    if Path(destination)==state/'config.json':
        proof={'database_present':(state/'index.sqlite3').is_file()}
        if proof['database_present']:
            with sqlite3.connect((state/'index.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                proof['binding']=db.execute('SELECT root,vault_id FROM filesystem_state WHERE singleton=1').fetchone()
                proof['protocol']=db.execute("SELECT version FROM wiki_protocols WHERE name='registration'").fetchone()[0]
        marker.write_text(json.dumps(proof))
        os._exit(71)
    return replace(source,destination)
os.replace=stop_before_registration
registry.configure(None,root,'curator')
""")
    assert interrupted.returncode == 71, interrupted.stderr
    proof = json.loads(marker.read_text())
    assert proof["database_present"], "config publication was attempted before creating its authority ledger"
    assert proof["protocol"] == 1
    assert proof["binding"][0] == str(Path(config_file).parent / "vault")
    assert not (Path(config.wiki_dir) / "config.json").exists()
    retried = registration_process(config_file, marker, """
result=registry.configure(None,root,'curator')
marker.write_text(json.dumps(result))
registry.close()
""")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(marker.read_text())["vault_id"] == proof["binding"][1]


def test_unconfirmed_registration_sync_exits_with_the_published_generation_retained(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    result = registration_process(config_file, marker, """
original=registry.configure(None,root,'curator')
marker.with_suffix('.before').write_text(json.dumps(original))
fsync=os.fsync
def fail_registration_sync(descriptor):
    if Path(os.readlink(f'/proc/self/fd/{descriptor}'))==state:
        marker.with_suffix('.sync-started').touch()
        raise OSError('registration namespace sync unavailable')
    return fsync(descriptor)
os.fsync=fail_registration_sync
try:
    registry.configure(None,root,'replacement-curator')
except Exception:
    pass
marker.touch()
""")
    assert marker.with_suffix(".sync-started").is_file(), result.stderr
    before = json.loads(marker.with_suffix(".before").read_text())
    published = json.loads((Path(config.wiki_dir) / "config.json").read_text())
    assert published["curator_agent_id"] == "replacement-curator"
    assert published["registration_generation"] != before["registration_generation"]
    assert published["vault_id"] == before["vault_id"]
    assert result.returncode == 70, result.stderr
    assert not marker.exists()


def test_body_delete_confirms_its_directory_before_publishing_applied(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    result = registration_process(config_file, marker, """
target=root/'note.md'
target.write_bytes(b'# Original body\\n')
registry.configure(None,root,'curator')
store=registry.store(None)
original=store.read('note.md')
fsync=os.fsync
def fail_deleted_directory(descriptor):
    if Path(os.readlink(f'/proc/self/fd/{descriptor}'))==root and not target.exists():
        marker.with_suffix('.sync-started').touch()
        raise OSError('deleted body namespace sync unavailable')
    return fsync(descriptor)
os.fsync=fail_deleted_directory
try:
    WikiHistory(store).write('note.md',None,original['revision'],'human','delete')
except Exception:
    pass
marker.touch()
""")
    assert not (Path(config_file).parent / "vault/note.md").exists(), result.stderr
    assert marker.with_suffix(".sync-started").is_file(), "deletion was published without confirming the directory"
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as reader:
        change = reader.execute("SELECT status,before_hash FROM changes WHERE path='note.md' AND after_hash IS NULL").fetchone()
    assert change is not None and change[0] == "prepared"
    assert (Path(config.wiki_dir) / "blobs" / change[1]).read_bytes() == b"# Original body\n"
    assert result.returncode == 70, result.stderr
    assert not marker.exists()

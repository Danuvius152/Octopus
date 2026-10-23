"""Registration alone owns publication of a complete initial Wiki ledger."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from server.wiki.registry import WikiRegistry
from server.wiki.schema import VERSION
from server.wiki.store import WikiError, WikiStore
from tests.test_service_runtime import installation_settings
from tests.test_wiki_storage import registered as registered_fixture
from tests.test_wiki_storage import registration_process

registered = registered_fixture


def test_direct_ordinary_store_has_no_missing_database_creation_permission(tmp_path):
    root, state = tmp_path / "vault", tmp_path / "state"
    root.mkdir()
    with pytest.raises(WikiError, match="ledger database"):
        WikiStore(root, state, "ordinary-reopen")
    assert not state.exists()


@pytest.mark.parametrize("damage", ["missing", "empty", "missing_schema", "future_schema", "root", "vault"])
def test_retained_registration_never_repairs_an_incomplete_or_mismatched_ledger(registered, damage):
    registry, store = registered
    root, state, path = store.root, store.state, store.db_path
    body = root / "note.md"
    body.write_bytes(b"# Preserved original\n")
    config = (state / "config.json").read_bytes()
    registry.close()
    if damage in {"missing", "empty"}:
        path.unlink()
        if damage == "empty":
            path.touch()
    else:
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA recursive_triggers=ON")
            if damage == "missing_schema":
                db.execute("DROP TABLE wiki_schema")
            elif damage == "future_schema":
                db.execute("INSERT INTO wiki_schema VALUES(999,'future')")
            elif damage == "root":
                db.execute("UPDATE filesystem_state SET root='/another-vault'")
            else:
                db.execute("UPDATE filesystem_state SET vault_id='another-identity'")
    original = path.read_bytes() if path.exists() else None
    fresh = WikiRegistry()
    try:
        for operation in (lambda: fresh.store(None), lambda: fresh.configure(None, root, "curator")):
            with pytest.raises(WikiError) as failure:
                operation()
            assert failure.value.status in {409, 503}
            assert (path.read_bytes() if path.exists() else None) == original
            assert (state / "config.json").read_bytes() == config
            assert body.read_bytes() == b"# Preserved original\n"
    finally:
        fresh.close()


def test_unpublished_private_database_never_grants_ordinary_creation_or_identity(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "unpublished.json"
    failed = registration_process(config_file, marker, """
link=os.link
def stop_before_link(source,destination,*args,**kwargs):
    if Path(destination)==state/'index.sqlite3':
        with sqlite3.connect(Path(source).as_uri()+'?mode=ro',uri=True) as db:
            proof={'binding':db.execute('SELECT root,vault_id FROM filesystem_state').fetchone(),
                'schema':db.execute('SELECT max(version) FROM wiki_schema').fetchone()[0]}
        marker.write_text(json.dumps(proof))
        os._exit(71)
    return link(source,destination,*args,**kwargs)
os.link=stop_before_link
registry.configure(None,root,'curator')
""")
    assert failed.returncode == 71, failed.stderr
    proof = json.loads(marker.read_text())
    assert proof["schema"] == VERSION
    state = Path(config.wiki_dir)
    assert not (state / "index.sqlite3").exists()
    assert not (state / "config.json").exists()
    assert list(state.glob(".index-*.sqlite3"))
    retried = registration_process(config_file, marker, """
from server.wiki.store import WikiError
try: registry.store(None)
except WikiError as error: assert error.status==404
else: raise AssertionError('An unpublished ledger became an ordinary Store')
result=registry.configure(None,root,'curator')
marker.write_text(json.dumps(result))
registry.close()
""")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(marker.read_text())["vault_id"] != proof["binding"][1]


@pytest.mark.parametrize("existing", [False, True])
def test_registration_confirms_state_ancestors_before_admitting_writers(tmp_path, existing):
    config, config_file = installation_settings(tmp_path / "installation")
    config.wiki_dir = str(config_file.parent / "new-state/ancestor/ledger")
    config_file.write_text(config.model_dump_json())
    marker = tmp_path / "continued"
    failed = registration_process(config_file, marker, f"existing={existing!r}\n" + r'''
if existing:registry.configure(None,root,'curator')
fsync=os.fsync
def unavailable(fd):
    if Path(os.readlink(f'/proc/self/fd/{fd}'))==state.parent.parent:
        marker.with_suffix('.sync').touch()
        raise OSError('registration ancestor namespace confirmation lost')
    return fsync(fd)
os.fsync=unavailable
try:registry.configure(None,root,'curator')
except Exception:pass
marker.touch()
''')
    assert marker.with_suffix(".sync").is_file(), failed.stderr
    assert failed.returncode == 70, failed.stderr
    assert not marker.exists()
    state = Path(config.wiki_dir)
    with sqlite3.connect(state / "index.sqlite3") as db:
        root, vault_id = db.execute("SELECT root,vault_id FROM filesystem_state").fetchone()
    old_config = (state / "config.json").read_bytes() if existing else None
    for _ in range(2):
        resumed = registration_process(config_file, marker, r'''
result=registry.configure(None,root,'curator')
marker.write_text(json.dumps(result))
registry.close()
''')
        assert resumed.returncode == 0, resumed.stderr
        observed = json.loads(marker.read_text())
        assert (observed["root"], observed["vault_id"]) == (root, vault_id)
        if old_config is not None:
            assert (state / "config.json").read_bytes() == old_config


def test_published_database_is_resynchronized_before_retry_can_publish_configuration(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "published.json"
    failed = registration_process(config_file, marker, """
link=os.link
def stop_after_link(source,destination,*args,**kwargs):
    result=link(source,destination,*args,**kwargs)
    if Path(destination)==state/'index.sqlite3':
        with sqlite3.connect(Path(destination).as_uri()+'?mode=ro',uri=True) as db:
            marker.write_text(json.dumps({'binding':db.execute('SELECT root,vault_id FROM filesystem_state').fetchone()}))
        os._exit(71)
    return result
os.link=stop_after_link
registry.configure(None,root,'curator')
""")
    assert failed.returncode == 71, failed.stderr
    proof = json.loads(marker.read_text())
    assert (Path(config.wiki_dir) / "index.sqlite3").is_file()
    assert not (Path(config.wiki_dir) / "config.json").exists()
    retried = registration_process(config_file, marker, """
fsync,replace=os.fsync,os.replace
synced=set()
def confirm(descriptor):
    result=fsync(descriptor)
    synced.add(Path(os.readlink(f'/proc/self/fd/{descriptor}')))
    return result
def publish(source,destination):
    if Path(destination)==state/'config.json':
        assert state/'index.sqlite3' in synced and state in synced
    return replace(source,destination)
os.fsync,os.replace=confirm,publish
result=registry.configure(None,root,'curator')
marker.write_text(json.dumps(result))
registry.close()
""")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(marker.read_text())["vault_id"] == proof["binding"][1]


def test_initial_database_schema_uses_the_confirmed_extra_connection(tmp_path):
    _config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "initial-sync"
    result = registration_process(config_file, marker, """
connect=sqlite3.connect
class InspectConnection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        if sql.startswith('CREATE TABLE IF NOT EXISTS wiki_schema'):
            assert super().execute('PRAGMA synchronous').fetchone()[0]==3
            marker.touch()
        return super().execute(sql,*args,**kwargs)
def inspect(*args,**kwargs):
    kwargs['factory']=InspectConnection
    return connect(*args,**kwargs)
sqlite3.connect=inspect
registry.configure(None,root,'curator')
registry.close()
""")
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_first_publication_never_overwrites_a_database_that_appears_at_link(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "conflict"
    result = registration_process(config_file, marker, """
from server.wiki.store import WikiError
link=os.link
def conflict(source,destination,*args,**kwargs):
    if Path(destination)==state/'index.sqlite3':
        Path(destination).write_bytes(b'retained competing evidence')
    return link(source,destination,*args,**kwargs)
os.link=conflict
try: registry.configure(None,root,'curator')
except WikiError as error:
    assert error.status==409
    marker.touch()
else: raise AssertionError('First publication overwrote another database')
registry.close()
""")
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert (Path(config.wiki_dir) / "index.sqlite3").read_bytes() == b"retained competing evidence"
    assert not (Path(config.wiki_dir) / "config.json").exists()


def test_two_processes_share_one_registration_lock_and_one_vault_identity(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    first, second = tmp_path / "first", tmp_path / "second"
    release = tmp_path / "release"
    first_operation = """
import time
from server.wiki.store import WikiError
link=os.link
def pause_link(source,destination,*args,**kwargs):
    if Path(destination)==state/'index.sqlite3':
        marker.with_suffix('.entered').touch()
        deadline=time.monotonic()+4
        while not RELEASE.exists():
            assert time.monotonic()<deadline
            time.sleep(0.005)
    return link(source,destination,*args,**kwargs)
os.link=pause_link
try: result=registry.configure(None,root,'curator')
except WikiError as error:
    assert error.status==503
    result=registry.config(None)
marker.write_text(json.dumps(result))
registry.close()
""".replace("RELEASE", "Path(" + repr(str(release)) + ")")
    second_operation = """
from server.wiki.store import WikiError
link=os.link
def observe_link(source,destination,*args,**kwargs):
    if Path(destination)==state/'index.sqlite3': marker.with_suffix('.linked').touch()
    return link(source,destination,*args,**kwargs)
os.link=observe_link
marker.with_suffix('.requested').touch()
try: result=registry.configure(None,root,'curator')
except WikiError as error:
    # The first process can be opening its committed Store while this
    # registration checks the same range. Its nonblocking claim may reject us.
    assert error.status==503 or (error.status==409 and
        str(error).startswith('An overlapping resource claim is in progress;'))
    result=registry.config(None)
marker.write_text(json.dumps(result))
registry.close()
"""

    def await_file(path):
        for _attempt in range(600):
            if path.exists():
                return
            threading.Event().wait(0.005)
        pytest.fail("The registration child did not reach its checkpoint")

    with ThreadPoolExecutor(2) as workers:
        creating = workers.submit(registration_process, config_file, first, first_operation)
        competing = None
        try:
            await_file(first.with_suffix(".entered"))
            competing = workers.submit(registration_process, config_file, second, second_operation)
            await_file(second.with_suffix(".requested"))
            assert not second.with_suffix(".linked").exists()
        finally:
            release.touch()
        one = creating.result(timeout=5)
        assert one.returncode == 0, one.stderr
        assert competing is not None
        two = competing.result(timeout=5)
        assert two.returncode == 0, two.stderr
    assert not second.with_suffix(".linked").exists()
    assert json.loads(first.read_text()) == json.loads(second.read_text())
    config_path = Path(config.wiki_dir) / "config.json"
    assert json.loads(config_path.read_text()) == json.loads(first.read_text())
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as db:
        binding = db.execute("SELECT root,vault_id FROM filesystem_state").fetchone()
    assert list(binding) == [str(Path(config_file).parent / "vault"), json.loads(first.read_text())["vault_id"]]

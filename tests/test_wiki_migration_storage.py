"""First-account publication confirms namespaces across real process exits."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from server.config import settings
from server.wiki.history import WikiHistory
from server.wiki.registry import registry
from tests.test_bootstrap import legacy as legacy
from tests.test_wiki_upgrade import registered as registered

_CHILD = '''
import asyncio,json,os,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
settings.db_path=sys.argv[2]
from server.database import Database
from server.bootstrap import bootstrap_first_account
from server.session_manager import session_manager
from server.users import UserManager
from server.wiki import account_upgrade as upgrade
from server.wiki.registry import registry
from server.wiki.history import WikiHistory
async def complete(db):
    if await db.count_users():
        user=await UserManager(db).authenticate('owner','password1')
        return await upgrade.resume_upgrade(db,user,session_manager)
    return await bootstrap_first_account(db,username='owner',password='password1',session_manager=session_manager)
async def main():
    db=Database(sys.argv[2]);await db.initialize()
    registry.bind_health(db.health)
    await session_manager.initialize(db)
    marker=Path(sys.argv[3])
    try:
'''


def _run(configuration, database, marker, operation):
    program = _CHILD + textwrap.indent(operation, "        ") + "\n    finally:await db.close()\nasyncio.run(main())\n"
    return subprocess.run(
        [sys.executable, "-c", program, str(configuration), database._db_path, str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("fault", ["stage_directory", "prepared_ancestor", "published_directory", "before_retirement", "retirement_directory"])
async def test_migration_storage_failure_exits_and_original_journal_resumes(registered, tmp_path, fault):
    database, vault, config, note = registered
    source = registry.state(None)
    proof = source / "retained/nested/proof.txt"
    proof.parent.mkdir(parents=True)
    proof.write_bytes(b"Retained migration evidence\n")
    history = WikiHistory(registry.store(None))
    raw = (vault / note["path"]).read_bytes()
    change = history.record(note["path"], raw, raw, "human", "Retained history", "applied")
    registry.close()
    configuration = tmp_path / "migration-settings.json"
    configuration.write_text(json.dumps({name: getattr(settings, name) for name in (
        "auth_token", "master_key", "master_key_file", "users_root", "agents_dir", "wiki_dir",
    )}))
    marker = tmp_path / "observation.json"
    interruption = f"fault={fault!r}\n" + r'''
fsync,replace=os.fsync,os.replace
source=Path(settings.wiki_dir)
def fail():
    marker.with_suffix('.fault').write_text(json.dumps(upgrade.pending_upgrade()))
    raise OSError('authoritative migration confirmation lost')
def lost_sync(fd):
    path=Path(os.readlink(f'/proc/self/fd/{fd}'))
    journal=upgrade.pending_upgrade()
    if fault=='stage_directory' and path.name=='nested' and any(part.startswith('.wiki-adoption-') for part in path.parts):fail()
    if fault=='prepared_ancestor' and journal and journal['phase']=='prepared' and path==upgrade.journal_path().parent.parent:fail()
    if fault=='published_directory' and journal and journal['phase']=='publishing' and journal.get('user_id'):
        destination=registry.state(journal['user_id'])
        if destination.is_dir() and path==destination.parent:fail()
    if fault=='retirement_directory' and path==source and not (source/'config.json').exists():fail()
    return fsync(fd)
def lost_replace(before,after):
    if fault=='before_retirement' and Path(before)==source/'config.json' and Path(after).name=='config.legacy-adopted.json':fail()
    return replace(before,after)
os.fsync=lost_sync
os.replace=lost_replace
try:await complete(db)
except Exception:pass
marker.touch()
'''
    first = _run(configuration, database, marker, interruption)
    assert first.returncode == 70, first.stderr
    assert marker.with_suffix(".fault").is_file() and not marker.exists()
    pending = json.loads((Path(settings.users_root) / ".first-account-upgrade.json").read_text())
    assert pending["phase"] == {
        "stage_directory": "preparing", "prepared_ancestor": "prepared", "published_directory": "publishing",
        "before_retirement": "published", "retirement_directory": "published",
    }[fault]
    assert await database.count_users() == int(fault not in {"stage_directory", "prepared_ancestor"})
    assert (source / "config.json").exists() == (fault != "retirement_directory")
    assert proof.read_bytes() == b"Retained migration evidence\n"
    assert (vault / note["path"]).read_bytes() == raw

    repeated = _run(configuration, database, marker, interruption)
    assert repeated.returncode == 70, repeated.stderr
    assert not marker.exists()
    again = json.loads((Path(settings.users_root) / ".first-account-upgrade.json").read_text())
    assert again["upgrade_id"] == pending["upgrade_id"] and again["phase"] == pending["phase"]

    if fault in {"stage_directory", "prepared_ancestor"}:
        gated = _run(configuration, database, marker, r'''
await db.close()
from server.main import app
from server.wiki.store import WikiError
async with app.router.lifespan_context(app):
    receipt=await session_manager.start_message('s-old','Must remain closed during recovery')
    assert receipt.disposition=='closed'
    try:registry.store(None)
    except WikiError as error:assert error.status==503
    else:raise AssertionError('pending migration admitted a Wiki writer')
marker.with_suffix('.gated').touch()
''')
        assert gated.returncode == 0, gated.stderr
        assert marker.with_suffix(".gated").is_file()

    completed = _run(configuration, database, marker, r'''
result=await complete(db)
store=registry.store(result['user_id'])
document=store.read('Library/note.md')
result={'user_id':result['user_id'],'document_id':document['id'],'revision':document['revision'],
        'vault_id':registry.config(result['user_id'])['vault_id'],
        'upgrade_id':upgrade.pending_upgrade(include_complete=True)['upgrade_id'],
        'changes':[row['id'] for row in WikiHistory(store).history('Library/note.md')['items']]}
assert upgrade.pending_upgrade() is None
marker.write_text(json.dumps(result))
registry.close()
''')
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(marker.read_text())
    assert observed["upgrade_id"] == pending["upgrade_id"]
    assert (observed["document_id"], observed["revision"], observed["vault_id"]) == (note["id"], note["revision"], config["vault_id"])
    assert change["id"] in observed["changes"]
    assert not (source / "config.json").exists()
    assert (Path(settings.users_root) / observed["user_id"] / "wiki/retained/nested/proof.txt").read_bytes() == b"Retained migration evidence\n"
    assert (vault / note["path"]).read_bytes() == raw

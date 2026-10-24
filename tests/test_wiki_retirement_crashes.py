"""Real exits cannot expose half-retired old task history in an owned WAL."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_format_preparation import facts, prepared_facts
from tests.test_wiki_retirement_boundaries import old_history as old_history
from tests.test_wiki_retirement_eligibility import insert_job, original_execution


@pytest.mark.parametrize("phase", ["after_delete", "before_version", "commit_ack"])
async def test_retiring_history_exposes_only_complete_old_or_current_facts(old_history, tmp_path, phase):
    root, state, registration, _, articles = old_history
    identifier = "retired-before-real-exit"
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA recursive_triggers=ON")
        insert_job(db, articles[0], identifier, execution=original_execution(identifier))
        db.execute("UPDATE jobs SET session_id=NULL WHERE id=?", (identifier,))
        insert_job(db, articles[0], "unproven-retained", item_status="pending", job_status="interrupted")
        db.commit()
        original = facts(db)
        sequence = db.execute("SELECT rowid FROM jobs WHERE id=?", (identifier,)).fetchone()[0]
    bodies = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}
    config = (state / "config.json").read_bytes()
    proof, admitted, model = (tmp_path / name for name in ("retirement-exit", "admitted", "model-started"))
    program = """
import json,os,sqlite3,subprocess,sys
from pathlib import Path
from urllib.parse import parse_qs,unquote,urlsplit
from server.config import settings
configuration,root,state,vault,phase,proof,admitted,model=sys.argv[1:]
for name,value in json.loads(Path(configuration).read_text()).items():setattr(settings,name,value)
root,state,proof,admitted,model=map(Path,(root,state,proof,admitted,model))
popen=subprocess.Popen
def reject_model(command,*args,**kwargs):
    if isinstance(command,(list,tuple)) and Path(command[0]).name in {'codex','claude'}:
        model.touch();raise AssertionError('Storage retirement must not invoke a model')
    return popen(command,*args,**kwargs)
subprocess.Popen=reject_model
connect=sqlite3.connect
class FaultConnection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        normalized=' '.join(sql.upper().split())
        if phase=='before_version' and normalized.startswith('INSERT INTO WIKI_SCHEMA'):
            proof.write_text(phase);os._exit(71)
        result=super().execute(sql,*args,**kwargs)
        if phase=='after_delete' and normalized.startswith('DELETE FROM JOBS WHERE ID IN'):
            proof.write_text(phase);os._exit(71)
        return result
    def commit(self):
        active=self.in_transaction
        super().commit()
        if active and phase=='commit_ack':
            proof.write_text(phase)
            raise OSError('History retirement committed; acknowledgement unavailable')
def observed(*args,**kwargs):
    raw=str(args[0]);uri=urlsplit(raw)
    location=unquote(uri.path) if raw.startswith('file:') else raw
    if location!=':memory:' and Path(location).resolve()==state/'index.sqlite3' and parse_qs(uri.query).get('mode',['rw'])[0]!='ro':
        kwargs['factory']=FaultConnection
    return connect(*args,**kwargs)
sqlite3.connect=observed
from server.wiki.store import WikiStore
try:WikiStore(root,state,vault,defer_recovery=True).close()
except Exception:pass
admitted.touch()
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path / "settings.json"), str(root), str(state),
                             registration["vault_id"], phase, str(proof), str(admitted), str(model)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
                            env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env"),
                                     CODEX_HOME=str(tmp_path / "child-codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "child-claude")))
    assert result.returncode == (70 if phase == "commit_ack" else 71), result.stdout + result.stderr
    assert proof.read_text() == phase and not admitted.exists() and not model.exists()
    committed = prepared_facts(original)
    for table, column in {"jobs": 0, "job_requests": 2, "wiki_job_state": 0, "wiki_job_outbox": 1}.items():
        committed[table] = [row for row in committed[table] if row[column] != identifier]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == (14 if phase == "commit_ack" else 13)
        assert facts(db) == (committed if phase == "commit_ack" else original)
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        with current.connect() as db:
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 14
            assert facts(db) == committed
            assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()[0] == sequence
            assert {row[0] for row in db.execute("SELECT id FROM jobs")} == {"unproven-retained"}
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as repeated:
        with repeated.connect() as db:
            assert facts(db) == committed
    assert (state / "config.json").read_bytes() == config
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")} == bodies

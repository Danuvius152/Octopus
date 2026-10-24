"""One supported preparation preserves owned facts; ordinary reopen validates."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from server.wiki import grants, job_events, maintenance_inputs, protocols
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account

FACTS = ("document_identities", "documents", "managed_notes", "material_origins", "uid_aliases",
         "source_bindings", "source_binding_head", "source_binding_observations", "wiki_protocols",
         "wiki_job_controls", "wiki_job_state", "wiki_job_outbox", "wiki_job_subscribers",
         "wiki_job_deliveries", "operation_receipts", "job_publications", "jobs", "job_requests")


def facts(db):
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    result = {}
    for name in FACTS:
        if name in present:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')
                       if row[1] not in {"admission_sequence", "item_match_sequence"}]
            selected = ",".join('"' + column + '"' for column in columns)
            result[name] = sorted(map(tuple, db.execute(f'SELECT {selected} FROM "{name}"')), key=repr)
    return result


def prepared_facts(original, *, initialized_jobs=False):
    result = dict(original)
    if "jobs" in result:
        result["jobs"] = []
        for row in original["jobs"]:
            values = list(row)
            # This independent baseline has one pending item, no legacy flags
            # and an unknown extension. Preparation only appends its version.
            values[5] = json.dumps([item | {"version": 1} for item in json.loads(values[5])], ensure_ascii=False)
            result["jobs"].append(tuple(values))
    if initialized_jobs:
        result["wiki_protocols"] = sorted([*result["wiki_protocols"], ("maintenance_jobs", 1)], key=repr)
    return result


def retained_baseline(root, state, registration, owner, *, with_jobs, version=12):
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as original:
        identity = original.execute("SELECT * FROM document_identities WHERE path='source.md'").fetchone()
        document = original.execute("SELECT * FROM documents WHERE path='source.md'").fetchone()
    temporary = state / "baseline12.sqlite3"
    fixture = "wiki-baseline-v12-with-jobs.sql" if with_jobs else "wiki-baseline-v12.sql"
    with closing(sqlite3.connect(temporary)) as db:
        db.row_factory = sqlite3.Row
        db.executescript((Path(__file__).parent / "fixtures" / fixture).read_text())
        db.execute("PRAGMA recursive_triggers=ON")
        if version == 13:
            assert with_jobs
            db.execute("INSERT INTO wiki_schema VALUES(13,'original-format-13')")
        db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                   (str(root), registration["vault_id"]))
        db.execute("INSERT INTO document_identities VALUES(?,?,?)", identity)
        db.execute("INSERT INTO documents VALUES(" + ",".join("?" for _ in document) + ")", document)
        if with_jobs:
            items = json.dumps([{"doc": document[0], "path": "source.md", "revision": document[2],
                                 "status": "pending", "original_unknown": {"bytes": "原始未知事实"}}], ensure_ascii=False)
            db.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,created_at,updated_at)
                          VALUES('original-job','original-request','original-digest','{"kind":"curation"}',
                                 'queued',?,'original-time','original-time')""", (items,))
            db.execute("INSERT INTO job_requests VALUES('original-request','original-digest','original-job')")
            db.execute("INSERT INTO wiki_job_controls VALUES('original-job','original-control','original-control-digest')")
            db.execute("""INSERT INTO operation_receipts(id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details) VALUES('original-receipt','original-job',?,?,
                          'source.md',?,?,'no_change','保留原始理由',NULL,'original-time','{}')""",
                       (document[0], document[0], document[2], document[2]))
            db.execute("""INSERT INTO wiki_job_subscribers VALUES('original-subscriber','original-job',?,
                          'human','original-browser','original-notice','original-client','original-time')""", (owner,))
            db.execute("UPDATE wiki_job_deliveries SET acknowledged=1,outcome='delivered'")
            assert db.execute("SELECT count(*) FROM wiki_job_deliveries WHERE acknowledged=1").fetchone()[0] == 1
        db.commit()
    temporary.replace(path)


@pytest.mark.parametrize("origin", ["new", "12_without_jobs", "12_with_jobs", "13_with_jobs"])
async def test_new_and_supported_owned_formats_share_the_complete_current_contract(registered_account, origin):
    root, state, registration, owner = registered_account
    if origin != "new":
        retained_baseline(root, state, registration, owner, with_jobs=origin != "12_without_jobs",
                          version=13 if origin == "13_with_jobs" else 12)
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        before = facts(db)
        versions = list(db.execute("SELECT * FROM wiki_schema ORDER BY version"))
    original_body = (root / "source.md").read_bytes()
    original_config = (state / "config.json").read_bytes()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        jobs = WikiJobs(owner, store=store, registration=registration)
        with store.connect() as db:
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 14
            if origin == "new":
                assert [row[0] for row in db.execute("SELECT version FROM wiki_schema")] == [14]
            else:
                assert list(map(tuple, db.execute("SELECT * FROM wiki_schema WHERE version<>14 ORDER BY version"))) == versions
            after = facts(db)
            for name, rows in prepared_facts(before, initialized_jobs=origin == "12_without_jobs").items():
                assert after[name] == rows
            if origin in {"12_with_jobs", "13_with_jobs"}:
                assert db.execute("SELECT admission_sequence FROM jobs WHERE id='original-job'").fetchone()[0] == 1
            assert maintenance_inputs.require(db)
            grants.require(db)
            job_events.require(db)
        assert protocols.registration_preflight(state, complete=True)["vault_id"] == registration["vault_id"]
        if origin in {"12_with_jobs", "13_with_jobs"}:
            assert jobs.get("original-job")["request_key"] == "original-request"
        current = store.read("source.md")
        requester = job_events.Requester(owner, "human", "format-browser", "format-request", "format-client")
        accepted = jobs.enqueue("format-contract", {"kind": "curation"}, [{"doc": current["id"], "path": current["path"],
                                "revision": current["revision"], "status": "pending"}], requester=requester)
        assert accepted["grant"]["vault_id"] == registration["vault_id"]
        assert accepted["grant"]["selected_documents"] == [{"doc_id": current["id"], "path_at_admission": "source.md",
                                                          "observed_revision": current["revision"], "current_path": "source.md"}]
        with store.connect() as db:
            subscriber = db.execute("SELECT id FROM wiki_job_subscribers WHERE job_id=? AND owner=?", (accepted["id"], owner)).fetchone()[0]
            event = db.execute("SELECT event_id FROM wiki_job_deliveries WHERE subscriber_id=?", (subscriber,)).fetchone()[0]
            job_events.acknowledge(db, event, subscriber, "delivered")
        assert jobs.enqueue("format-contract", {"kind": "curation"}, [{"doc": current["id"], "path": current["path"],
                            "revision": current["revision"], "status": "pending"}], requester=requester)["id"] == accepted["id"]
        with store.connect() as db:
            assert db.execute("SELECT acknowledged,outcome FROM wiki_job_deliveries WHERE event_id=? AND subscriber_id=?",
                              (event, subscriber)).fetchone()[:] == (1, "delivered")
    assert (root / "source.md").read_bytes() == original_body
    assert (state / "config.json").read_bytes() == original_config


async def test_healthy_current_reopen_does_not_run_bootstrap_or_change_nonce(registered_account, monkeypatch):
    root, state, registration, owner = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        original_nonce = db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0]
        original_versions = list(db.execute("SELECT * FROM wiki_schema"))
    preparation = []
    connect = sqlite3.connect

    def observed(*args, **kwargs):
        db = connect(*args, **kwargs)
        raw = str(args[0])
        location = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
        if location != ":memory:" and Path(location).resolve() == path:
            def trace(sql):
                normalized = " ".join(sql.lower().split())
                if normalized.startswith(("create ", "alter ", "drop ")) or (
                        "wiki_schema" in normalized and normalized.startswith(("insert ", "update ", "delete ", "replace "))) or (
                        "from wiki_schema where version=" in normalized):
                    preparation.append(sql)
            db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", observed)
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        WikiJobs(owner, store=store, registration=registration)
        WikiJobs(owner, store=store, registration=registration)
        assert preparation == []
        with store.connect() as db:
            assert db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0] == original_nonce
            assert list(map(tuple, db.execute("SELECT * FROM wiki_schema"))) == original_versions


@pytest.mark.parametrize("origin", ["new", "12_without_jobs", "12_with_jobs", "13_with_jobs"])
@pytest.mark.parametrize("phase", ["during_prepare", "before_version", "commit_ack"])
async def test_preparation_crash_exposes_only_a_complete_old_or_current_format(registered_account, tmp_path, origin, phase):
    root, state, registration, owner = registered_account
    if origin == "new":
        state = state / "unpublished"
        state.mkdir()
        original = None
        versions = []
    else:
        retained_baseline(root, state, registration, owner, with_jobs=origin != "12_without_jobs",
                          version=13 if origin == "13_with_jobs" else 12)
        with closing(sqlite3.connect(state / "index.sqlite3")) as db:
            original, versions = facts(db), list(db.execute("SELECT * FROM wiki_schema ORDER BY version"))
    path = state / "index.sqlite3"
    proof, admitted = tmp_path / "preparation-fault", tmp_path / "admitted-after-preparation-fault"
    body = (root / "source.md").read_bytes()
    result = subprocess.run([sys.executable, "-c", """
import json,os,sqlite3,sys
from pathlib import Path
from urllib.parse import parse_qs,unquote,urlsplit
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
from server.service_health import ServiceHealth
from server.wiki.store import WikiStore
root,state=Path(sys.argv[2]),Path(sys.argv[3])
vault,origin,phase=sys.argv[4:7]
proof,admitted=Path(sys.argv[7]),Path(sys.argv[8])
path=state/'index.sqlite3'
connect=sqlite3.connect
def stop():
    proof.write_text(phase)
    os._exit(71)
class FaultConnection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        normalized=' '.join(sql.upper().split())
        version=normalized.startswith('INSERT INTO WIKI_SCHEMA')
        if phase=='before_version' and version:stop()
        result=super().execute(sql,*args,**kwargs)
        during=(normalized.startswith('CREATE TABLE APP_TRASH') if origin=='new' else
                normalized.startswith('CREATE TABLE "JOBS"') if origin=='12_without_jobs' else
                normalized.startswith('INSERT INTO PREPARED_JOBS'))
        if phase=='during_prepare' and during:stop()
        return result
    def commit(self):
        active=self.in_transaction
        super().commit()
        if active and phase=='commit_ack':
            proof.write_text(phase)
            raise OSError('Actual Wiki format commit succeeded; acknowledgment unavailable')
def observed(*args,**kwargs):
    raw=str(args[0])
    uri=urlsplit(raw)
    location=unquote(uri.path) if raw.startswith('file:') else raw
    mode=parse_qs(uri.query).get('mode',['rw'])[0]
    if location!=':memory:' and Path(location).resolve()==path and mode!='ro':kwargs['factory']=FaultConnection
    return connect(*args,**kwargs)
sqlite3.connect=observed
try:
    if origin=='new':WikiStore.create_database(root,state,vault,path,health=ServiceHealth())
    else:WikiStore(root,state,vault,defer_recovery=True).close()
except Exception:
    pass
admitted.touch()
""", str(tmp_path / "settings.json"), str(root), str(state), registration["vault_id"], origin, phase,
        str(proof), str(admitted)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env"),
                 CODEX_HOME=str(tmp_path / "child-codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "child-claude")))
    assert result.returncode == (70 if phase == "commit_ack" else 71), result.stdout + result.stderr
    assert proof.read_text() == phase and not admitted.exists()
    with closing(sqlite3.connect(path)) as db:
        if origin == "new" and phase != "commit_ack":
            assert db.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] == 0
        else:
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == (14 if phase == "commit_ack" else
                                                                                     13 if origin == "13_with_jobs" else 12)
            if original is not None:
                after = facts(db)
                expected = prepared_facts(original, initialized_jobs=origin == "12_without_jobs") if phase == "commit_ack" else original
                for name, rows in expected.items():
                    assert after[name] == rows
                assert list(db.execute("SELECT * FROM wiki_schema WHERE version<>14 ORDER BY version")) == versions
    if origin == "new":
        from server.service_health import ServiceHealth

        WikiStore.create_database(root, state, registration["vault_id"], path, health=ServiceHealth())
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as reopened:
        with reopened.connect() as db:
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 14
            if original is not None:
                after = facts(db)
                for name, rows in prepared_facts(original, initialized_jobs=origin == "12_without_jobs").items():
                    assert after[name] == rows
    assert (root / "source.md").read_bytes() == body

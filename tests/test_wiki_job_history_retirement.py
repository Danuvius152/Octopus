"""Ticket 15's isolated storage preparation and history preservation contract.

The input is a frozen format-13 ledger, not the current schema relabelled 13.
Its only job is unversioned and superficially completed, without original
execution or settlement proof. Its no_change receipt is the article's sole
qualified review baseline. Reopening must retain the job, persist version 1,
and preserve that article and proof without starting maintenance or a model.

The article producer only supplies real file/index/source facts. It never
creates the target ledger or its historical job. No future product API is
assumed, and jobs.get() is deliberately absent from the persisted-item check.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

VAULT_ID = "30000000-0000-4000-8000-000000000015"
JOB_ID = "retained-unversioned-without-execution-proof"
RECEIPT_ID = "only-qualified-review-receipt"
ARTICLE_PATH = "Library/preserved.md"

# These contain the real article's file identity, extracted content, source
# observations, chapter index and navigation facts. No jobs, grants, requests,
# executions, receipts or schema/version rows come from the current producer.
ARTICLE_TABLES = (
    "document_identities", "documents", "material_origins", "raw_materials",
    "source_bindings", "source_binding_observations", "source_binding_head",
    "uid_aliases", "wiki_uid_claims", "wiki_lookup_names", "wiki_edges",
    "wiki_edge_lookups", "search_documents", "search_sections", "search_fts",
    "watch_directories", "catalog_state", "index_state",
)
PRESERVED_TABLES = (
    "document_identities", "documents", "material_origins", "raw_materials",
    "source_bindings", "source_binding_observations", "source_binding_head",
    "operation_receipts", "job_requests", "wiki_job_state", "wiki_job_outbox",
)


def frozen_layout():
    relative = Path("tests/fixtures/wiki-baseline-v12-with-jobs.sql")
    return next(parent / relative for parent in Path(__file__).resolve().parents
                if (parent / relative).is_file())


def columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def copy_article_table(producer, target, table):
    # Select the frozen target's columns, even if the current producer later
    # adds columns. In particular, never copy its sqlite_schema or wiki_schema.
    names = (["rowid"] if table == "search_fts" else []) + columns(target, table)
    selected = ",".join('"' + name + '"' for name in names)
    if table in {"catalog_state", "index_state"}:
        target.execute(f"DELETE FROM {table}")
    target.executemany(f"INSERT INTO {table}({selected}) VALUES({','.join('?' for _ in names)})",
                       producer.execute(f"SELECT {selected} FROM {table}"))


def original_facts(db):
    # Preserve the original columns while permitting a future format to append
    # its current qualification representation to operation_receipts.
    result = {}
    for table in PRESERVED_TABLES:
        names = columns(db, table)
        selected = ",".join('"' + name + '"' for name in names)
        result[table] = (selected, sorted(map(tuple, db.execute(f"SELECT {selected} FROM {table}")), key=repr))
    return result


def test_reopen_keeps_unproven_completed_legacy_job_and_its_only_article_baseline(tmp_path, monkeypatch):
    from server.harness.harness import Harness
    from server.harness.run import HarnessRun
    from server.wiki.jobs import WikiJobs, WikiWorker
    from server.wiki.maintenance_state import status
    from server.wiki.store import WikiStore

    calls = {"maintenance": 0, "model": 0}

    def forbidden(kind):
        def reject(*args, **kwargs):
            calls[kind] += 1
            raise AssertionError(f"Storage reopening started new {kind}")
        return reject

    for owner, name, kind in (
        (WikiJobs, "enqueue", "maintenance"),
        (WikiWorker, "process", "maintenance"),
        (Harness, "create_run", "model"),
        (Harness, "run_oneshot", "model"),
        (HarnessRun, "start", "model"),
    ):
        monkeypatch.setattr(owner, name, forbidden(kind))

    root, producer_state, state = (tmp_path / name for name in ("vault", "article-producer", "retained-13"))
    (root / "Library").mkdir(parents=True)
    article_file = root / ARTICLE_PATH
    body = "# Preserved article\nOriginal words 原文、观点与表达保持。\n".encode()
    article_file.write_bytes(body)

    # A separate temporary Store produces genuine article/source/index rows.
    # Its current layout is never used as the historical input layout.
    with closing(WikiStore(root, producer_state, VAULT_ID, create=True)) as producer:
        article = producer.read(ARTICLE_PATH)
    assert article_file.read_bytes() == body

    state.mkdir()
    path = state / "index.sqlite3"
    registration = {"root": str(root.resolve()), "vault_id": VAULT_ID,
                    "curator_agent_id": "isolated-curator", "app_id": None,
                    "registration_protocol": 1,
                    "registration_generation": "15000000000040008000000000000000"}
    config = json.dumps(registration, ensure_ascii=False).encode()
    (state / "config.json").write_bytes(config)
    item = {"doc": article["id"], "path": ARTICLE_PATH, "revision": article["revision"],
            "input_revision": article["revision"], "status": "done", "index_complete": True,
            "output": ARTICLE_PATH, "output_revision": article["revision"],
            "operation": "no_change", "receipt_id": RECEIPT_ID, "output_change_id": None,
            "original_extension": {"text": "保全未知扩展", "values": [None, 0, False]}}

    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(producer_state / "index.sqlite3")) as source:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=ON")
        db.executescript(frozen_layout().read_text())
        assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 12
        # The frozen with-jobs layout already has registration_config=1. The
        # actual 12->13 preparation changes no DDL or existing facts here; its
        # only remaining step is appending the format receipt below.
        assert db.execute("SELECT version FROM wiki_protocols WHERE name='registration_config'").fetchone()[0] == 1
        db.execute("INSERT INTO wiki_schema VALUES(13,'original-format-13')")
        db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                   (str(root.resolve()), VAULT_ID))
        for table in ARTICLE_TABLES:
            copy_article_table(source, db, table)
        db.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,created_at,updated_at)
                      VALUES(?,?,?,'{"kind":"curation"}','completed',?,'original-created','original-updated')""",
                   (JOB_ID, "original-request", "original-request-digest", json.dumps([item], ensure_ascii=False)))
        db.execute("INSERT INTO job_requests VALUES('original-request','original-request-digest',?)", (JOB_ID,))
        db.execute("""INSERT INTO operation_receipts(id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details)
                      VALUES(?,?,?,?,?,?,?,'no_change','Original review retained',NULL,'original-receipt','{}')""",
                   (RECEIPT_ID, JOB_ID, article["id"], article["id"], ARTICLE_PATH,
                    article["revision"], article["revision"]))
        db.commit()
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 1
        raw_job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (JOB_ID,)).fetchone())
        assert json.loads(raw_job["items"]) == [item] and "version" not in item
        assert raw_job["session_id"] is None
        assert db.execute("SELECT settlement_phase FROM wiki_job_state WHERE job_id=?", (JOB_ID,)).fetchone()[0] == "none"
        # No worker_request, previous refs, payload attempt or terminal is
        # supplied. The event trigger's derived attempt is not ending proof.
        before = original_facts(db)

    with closing(WikiStore(root, state, VAULT_ID)) as reopened:
        # Capture immediately after storage preparation, before observations
        # can call any read-time normalizer. jobs.get() would hide this defect.
        with reopened.connect() as db:
            retained = db.execute("SELECT * FROM jobs WHERE id=?", (JOB_ID,)).fetchone()
            assert retained is not None, "No original execution/settlement proof authorizes deleting this job"
            persisted_items = json.loads(retained["items"])
            assert db.execute("SELECT id FROM jobs").fetchall()[0][0] == JOB_ID
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
            assert retained["status"] == "completed" and retained["session_id"] is None
            assert json.loads(retained["payload"]) == {"kind": "curation"}
            assert db.execute("SELECT settlement_phase FROM wiki_job_state WHERE job_id=?", (JOB_ID,)).fetchone()[0] == "none"

        # This only reconciles the already indexed unchanged file. It starts
        # neither a maintenance job nor a model, and must preserve its facts.
        reopened.refresh()
        observed = status(reopened, registration, "isolated-owner", doc=article["id"])
        assert observed["ready"] and observed["total"] == 1
        current = observed["items"][0]
        assert current["current"] == {"id": article["id"], "path": ARTICLE_PATH, "revision": article["revision"]}
        assert current["status"] == "unchanged"
        assert current["baseline"] == {"id": RECEIPT_ID, "path": ARTICLE_PATH,
                                       "output_revision": article["revision"], "operation": "no_change"}
        assert current["review"]["baseline_receipt_id"] == RECEIPT_ID
        assert current["attempts"] == [] and current["attempt_count"] == 0
        with reopened.connect() as db:
            for table, (selected, rows) in before.items():
                assert sorted(map(tuple, db.execute(f"SELECT {selected} FROM {table}")), key=repr) == rows, table
            assert [row[0] for row in db.execute("SELECT path FROM maintenance_accepted_paths WHERE doc_id=?",
                                                (article["id"],))] == [ARTICLE_PATH]

    assert article_file.read_bytes() == body
    assert (state / "config.json").read_bytes() == config
    assert calls == {"maintenance": 0, "model": 0}
    assert persisted_items == [item | {"version": 1}], "Retained legacy items must be converted in persistent storage"

    with closing(WikiStore(root, state, VAULT_ID)) as repeated:
        with repeated.connect() as db:
            assert json.loads(db.execute("SELECT items FROM jobs WHERE id=?", (JOB_ID,)).fetchone()[0]) == persisted_items
            for table, (selected, rows) in before.items():
                assert sorted(map(tuple, db.execute(f"SELECT {selected} FROM {table}")), key=repr) == rows, table
    assert article_file.read_bytes() == body
    assert (state / "config.json").read_bytes() == config
    assert calls == {"maintenance": 0, "model": 0}

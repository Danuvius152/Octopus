"""A proven old subset retires without changing unknown work or article proof."""

import hashlib
import json
import sqlite3
from contextlib import closing

from server.models import TurnOutcome
from server.wiki import schema
from server.wiki.jobs import WikiJobs
from server.wiki.maintenance_state import status
from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_job_history_retirement import (
    ARTICLE_PATH,
    ARTICLE_TABLES,
    copy_article_table,
    frozen_layout,
)


def original_execution(label, *, phase="settled", retired=True):
    request = "original-request-" + label
    result = {"attempt": 1, "session_id": "original-session-" + label,
              "request_id": request, "work_id": "wiki-" + request, "phase": phase,
              "recording_state": "committed" if phase == "settled" else "unconfirmed", "retired": retired}
    if phase == "settled":
        result["terminal"] = TurnOutcome(request_id=request, status="success").model_dump()
    return result


def insert_job(db, article, identifier, *, current=False, execution=None, previous=None, job_status="completed", item_status="done"):
    receipt_id = "receipt-" + identifier
    item = {"doc": article["id"], "path": ARTICLE_PATH, "revision": article["revision"],
            "input_revision": article["revision"], "status": item_status,
            "index_complete": item_status == "done", "output": ARTICLE_PATH,
            "output_revision": article["revision"], "operation": "no_change", "receipt_id": receipt_id,
            "output_change_id": None, "original_extension": {"text": identifier, "values": [None, False, 0]}}
    if current:
        item["version"] = 1
    if execution is not None:
        item["worker_request"] = execution
    if previous is not None:
        item["previous_worker_requests"] = [previous]
    payload = {"kind": "curation", "request": {"paths": [ARTICLE_PATH], "scope": "Original review " + identifier}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    db.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,session_id,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,'original-created','original-updated')""",
               (identifier, "key-" + identifier, hashlib.sha256(raw.encode()).hexdigest(), raw, job_status,
                json.dumps([item], ensure_ascii=False), execution["session_id"] if execution else None))
    db.execute("INSERT INTO job_requests VALUES(?,?,?)", ("key-" + identifier, WikiJobs.request_hash(payload["request"]), identifier))
    if item_status == "done":
        db.execute("""INSERT INTO operation_receipts(
            id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details)
            VALUES(?,?,?,?,?,?,?,'no_change','Original article proof',NULL,'original-receipt','{}')""",
                   (receipt_id, identifier, article["id"], article["id"], ARTICLE_PATH,
                    article["revision"], article["revision"]))
    return item


async def test_only_proven_closed_old_jobs_retire_and_future_failure_remains_visible(registered_account):
    root, state, registration, owner = registered_account
    (root / "Library").mkdir()
    body = "# Preserved article\n原文、来源和审阅证明保持。\n".encode()
    article_file = root / ARTICLE_PATH
    article_file.write_bytes(body)
    with closing(WikiStore(root, state, registration["vault_id"])) as producer:
        producer.refresh()
        article = producer.read(ARTICLE_PATH)
        with producer.connect() as db:
            assert db.execute("SELECT path FROM documents WHERE id=?", (article["id"],)).fetchone()[0] == ARTICLE_PATH
    original_config = (state / "config.json").read_bytes()
    staged = state / "mixed-format13.sqlite3"
    originals = {}
    with closing(sqlite3.connect(staged)) as db, closing(sqlite3.connect(state / "index.sqlite3")) as producer:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=ON")
        db.executescript(frozen_layout().read_text())
        db.execute("INSERT INTO wiki_schema VALUES(13,'original-format-13')")
        db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                   (str(root), registration["vault_id"]))
        for table in ARTICLE_TABLES:
            copy_article_table(producer, db, table)
        originals["current-completed"] = insert_job(db, article, "current-completed", current=True,
                                                   execution=original_execution("current-completed"))
        originals["old-unproven"] = insert_job(db, article, "old-unproven")
        originals["old-running"] = insert_job(db, article, "old-running", job_status="running", item_status="pending",
                                             execution=original_execution("old-running", phase="accepted", retired=False))
        originals["old-unknown"] = insert_job(db, article, "old-unknown", job_status="completed_with_attention",
                                             execution=original_execution("old-unknown", phase="unknown"))
        originals["old-previous-unknown"] = insert_job(db, article, "old-previous-unknown",
            execution=original_execution("old-previous-unknown"), previous=original_execution("previous-batch", phase="unknown"))
        originals["old-delivery-unknown"] = insert_job(db, article, "old-delivery-unknown",
                                                       execution=original_execution("old-delivery-unknown"))
        db.execute("""INSERT INTO wiki_job_subscribers VALUES(
            'pending-subscriber','old-delivery-unknown',?,'human','','original-notice','','original-time')""", (owner,))
        assert db.execute("SELECT count(*) FROM wiki_job_deliveries WHERE acknowledged=0").fetchone()[0] == 1
        retired = {"old-closed-a", "old-closed-b"}
        for identifier in sorted(retired):
            originals[identifier] = insert_job(db, article, identifier, execution=original_execution(identifier))
        original_sequences = {identifier: 11 * (ordinal + 1) for ordinal, identifier in enumerate(originals)}
        for identifier, sequence in original_sequences.items():
            db.execute("UPDATE jobs SET rowid=? WHERE id=?", (sequence, identifier))
        old_maximum = db.execute("SELECT max(rowid) FROM jobs").fetchone()[0]
        original_rows = {row["id"]: dict(row) for row in db.execute("SELECT * FROM jobs")}
        assert db.execute("SELECT settlement_phase FROM wiki_job_state WHERE job_id='old-closed-b'").fetchone()[0] == "none"
        retained_events = {table: list(map(tuple, db.execute(
            f"SELECT * FROM {table} WHERE job_id NOT IN ('old-closed-a','old-closed-b') ORDER BY rowid")))
            for table in ("wiki_job_state", "wiki_job_outbox")}
        before_receipts = list(map(tuple, db.execute("SELECT * FROM operation_receipts ORDER BY rowid")))
        db.commit()
    staged.replace(state / "index.sqlite3")

    with closing(WikiStore(root, state, registration["vault_id"])) as reopened:
        reopened.refresh()
        observed = status(reopened, registration, owner, doc=article["id"])
        assert observed["ready"] and observed["items"], observed
        current = observed["items"][0]
        assert current["status"] == "unchanged"
        assert current["baseline"]["id"] == "receipt-old-closed-b"
        assert current["source_warnings"] == {}
        with reopened.connect() as db:
            remaining = {row[0] for row in db.execute("SELECT id FROM jobs")}
            assert remaining == set(originals) - retired, "Only proven closed old jobs should retire after verification"
            for identifier in remaining:
                row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())
                for name, value in original_rows[identifier].items():
                    if name != "items":
                        assert row[name] == value, (identifier, name)
                assert row["admission_sequence"] == original_sequences[identifier]
                assert json.loads(row["items"]) == [originals[identifier] | {"version": 1}]
            for table, rows in retained_events.items():
                assert list(map(tuple, db.execute(f"SELECT * FROM {table} ORDER BY rowid"))) == rows
            assert not db.execute("SELECT 1 FROM job_requests WHERE job_id IN ('old-closed-a','old-closed-b')").fetchone()
            columns = [row[1] for row in db.execute("PRAGMA table_info(operation_receipts)") if row[1] != "item_match_sequence"]
            assert list(map(tuple, db.execute("SELECT " + ",".join(columns) + " FROM operation_receipts ORDER BY rowid"))) == before_receipts
            assert [row[0] for row in db.execute("SELECT path FROM maintenance_accepted_paths WHERE doc_id=?", (article["id"],))] == [ARTICLE_PATH]

        jobs = WikiJobs(owner, store=reopened, registration=registration)
        failed = jobs.enqueue("post-retirement-failure", {"kind": "curation"}, [
            {"doc": article["id"], "path": ARTICLE_PATH, "revision": article["revision"], "status": "failed"}])
        jobs.update(failed["id"], status="failed")
        current = status(reopened, registration, owner, doc=article["id"])["items"][0]
        assert current["baseline"]["id"] == "receipt-old-closed-b"
        assert any(item["job_id"] == failed["id"] for item in current["attempts"])
        with reopened.connect() as db:
            assert db.execute("SELECT admission_sequence FROM jobs WHERE id=?", (failed["id"],)).fetchone()[0] > old_maximum
            assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt-old-closed-b'").fetchone()[0] == old_maximum
            retained_current_events = {table: list(map(tuple, db.execute(f"SELECT * FROM {table} ORDER BY rowid")))
                                       for table in ("wiki_job_state", "wiki_job_outbox")}
    with closing(WikiStore(root, state, registration["vault_id"])) as cold:
        cold.refresh()
        current = status(cold, registration, owner, doc=article["id"])["items"][0]
        assert current["baseline"]["id"] == "receipt-old-closed-b"
        assert any(item["job_id"] == failed["id"] for item in current["attempts"])
        with cold.connect() as db:
            db.execute("DROP TABLE maintenance_projection")
        schema.initialize(cold)
        rebuilt = status(cold, registration, owner, doc=article["id"])["items"][0]
        assert rebuilt["baseline"] == current["baseline"]
        assert rebuilt["source_warnings"] == current["source_warnings"]
        assert rebuilt["attempts"] == current["attempts"]
        with cold.connect() as db:
            for table, rows in retained_current_events.items():
                assert list(map(tuple, db.execute(f"SELECT * FROM {table} ORDER BY rowid"))) == rows
            assert not db.execute("SELECT 1 FROM jobs WHERE id IN ('old-closed-a','old-closed-b')").fetchone()
            assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt-old-closed-b'").fetchone()[0] == old_maximum
    assert article_file.read_bytes() == body
    assert (state / "config.json").read_bytes() == original_config

"""Article creation proof and historical output roots outlive raw task history."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from server.wiki import grants, schema
from server.wiki.history import WikiHistory
from server.wiki.maintenance_state import status
from server.wiki.retention import collect
from server.wiki.store import WikiStore, revision
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.wiki_current_storage import ARTICLE_PATH, insert_job, original_execution
from tests.wiki_current_storage import current_history as current_history


async def test_bound_creation_proof_survives_reopen_without_fabricated_publication(current_history):
    root, state, registration, owner, articles = current_history
    article = articles[0]
    identifier, change = "original-bound-creation", "original-creation-change"
    reference = original_execution(identifier)
    created = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("BEGIN IMMEDIATE")
        item = insert_job(db, article, identifier, execution=reference)
        creation = {"purpose": "requested_knowledge_page", "paths": [ARTICLE_PATH]}
        request = {"material": "", "scope": "Original requested page", "paths": [], "target": None,
                   "sources": [], "actor": "human", "creation": creation}
        payload = {"kind": "curation", **request, "request": request}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        db.execute("UPDATE jobs SET payload=?,payload_hash=?,session_id=NULL WHERE id=?",
                   (encoded, hashlib.sha256(encoded.encode()).hexdigest(), identifier))
        from server.wiki.jobs import WikiJobs

        db.execute("UPDATE job_requests SET request_hash=? WHERE job_id=?", (WikiJobs.request_hash(request), identifier))
        reserved, = grants.issue(db, registration, identifier, [], creation=creation,
                                outputs=[{"path": ARTICLE_PATH, "path_key": str(root / ARTICLE_PATH)}])
        db.execute("INSERT INTO changes VALUES(?,?,NULL,?,'curator','Original creation','prepared',?)",
                   (change, ARTICLE_PATH, article["revision"], created))
        db.execute("INSERT INTO change_retention VALUES(?,?,0,1,NULL)", (change, article["id"]))
        db.execute("UPDATE wiki_path_reservations SET state='prepared',document_id=?,change_id=?,output_revision=? WHERE id=?",
                   (article["id"], change, article["revision"], reserved["reservation_id"]))
        db.execute("UPDATE changes SET status='applied' WHERE id=?", (change,))
        db.execute("UPDATE wiki_path_reservations SET state='bound' WHERE id=?", (reserved["reservation_id"],))
        grants.bind_document(db, identifier, article["id"], ARTICLE_PATH, article["revision"], "created", reserved["reservation_id"])
        details = {"creation_reservation_id": reserved["reservation_id"], "input_source_doc": None,
                   "input_source_revision": None, "execution": {key: reference[key] for key in ("attempt", "session_id", "request_id")}}
        db.execute("UPDATE operation_receipts SET input_revision=NULL,operation='publish',change_id=?,details=? WHERE job_id=?",
                   (change, json.dumps(details), identifier))
        item.update(kind="creation", reservation_id=reserved["reservation_id"], input_revision=None,
                    output_change_id=change, operation="publish")
        db.execute("UPDATE jobs SET items=? WHERE id=?", (json.dumps([item]), identifier))
        preserved = {table: list(map(tuple, db.execute("SELECT " + ",".join(row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] != "item_match_sequence") + f" FROM {table} ORDER BY rowid")))
                     for table in ("wiki_path_reservations", "changes", "change_retention", "operation_receipts")}
        assert not db.execute("SELECT 1 FROM job_publications WHERE job_id=?", (identifier,)).fetchone()
        db.commit()
    body = (root / ARTICLE_PATH).read_bytes()
    for reopening in range(2):
        with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
            current.refresh()
            if reopening:
                with current.connect() as db:
                    db.execute("DROP TABLE maintenance_projection")
                schema.initialize(current)
            observed = status(current, registration, owner, doc=article["id"])["items"][0]
            assert observed["baseline"]["id"] == "receipt-" + identifier
            assert observed["baseline"]["operation"] == "publish"
            assert observed["status"] == "unchanged"
            with current.connect() as db:
                assert db.execute("SELECT id FROM jobs WHERE id=?", (identifier,)).fetchone()[0] == identifier
                assert not db.execute("SELECT 1 FROM job_publications WHERE job_id=?", (identifier,)).fetchone()
                for table, rows in preserved.items():
                    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] != "item_match_sequence"]
                    assert list(map(tuple, db.execute("SELECT " + ",".join(columns) + f" FROM {table} ORDER BY rowid"))) == rows
                assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()[0] is not None
    assert (root / ARTICLE_PATH).read_bytes() == body


async def test_recent_no_change_output_remains_a_blob_root_after_reopen(current_history):
    root, state, registration, _, articles = current_history
    identifier = "recent-no-change"
    original = (root / ARTICLE_PATH).read_bytes()
    digest = revision(original)
    created = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        insert_job(db, articles[0], identifier, execution=original_execution(identifier))
        db.execute("UPDATE jobs SET session_id=NULL WHERE id=?", (identifier,))
        db.execute("UPDATE operation_receipts SET created_at=? WHERE job_id=?", (created, identifier))
        db.commit()
    blobs = state / "blobs"
    blobs.mkdir(exist_ok=True)
    (blobs / digest).write_bytes(original)
    orphan = revision(b"Unreferenced object")
    (blobs / orphan).write_bytes(b"Unreferenced object")
    changed = original + b"A later human edit changes the current revision.\n"
    (root / ARTICLE_PATH).write_bytes(changed)
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        assert current.read(ARTICLE_PATH)["revision"] != digest
        with current.connect() as db:
            assert db.execute("SELECT 1 FROM jobs WHERE id=?", (identifier,)).fetchone() is not None
            assert db.execute("SELECT job_id FROM operation_receipts WHERE id=?", ("receipt-" + identifier,)).fetchone()[0] == identifier
        result = collect(WikiHistory(current))
        assert result["removed_objects"] >= 1
        assert not (blobs / orphan).exists()
        assert (blobs / digest).read_bytes() == original
    assert (root / ARTICLE_PATH).read_bytes() == changed

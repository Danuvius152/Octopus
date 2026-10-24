"""Finite retirement follows execution proof, never a released live pointer."""

import json
import sqlite3
from contextlib import closing

import pytest

from server.config import settings
from server.database import Database
from server.wiki import job_details, job_events
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiError, WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_job_history_retirement import (
    ARTICLE_PATH,
    ARTICLE_TABLES,
    copy_article_table,
    frozen_layout,
)
from tests.test_wiki_retirement_eligibility import insert_job, original_execution


@pytest.fixture
async def old_history(registered_account):
    root, state, registration, owner = registered_account
    (root / "Library").mkdir()
    for path in (ARTICLE_PATH, "Library/second.md"):
        (root / path).write_text("# Original\nKeep the original argument and its conditions.\n")
    with closing(WikiStore(root, state, registration["vault_id"])) as producer:
        producer.refresh()
        articles = [producer.read(path) for path in (ARTICLE_PATH, "Library/second.md")]
    staged = state / "boundary-format13.sqlite3"
    with closing(sqlite3.connect(staged)) as target, closing(sqlite3.connect(state / "index.sqlite3")) as source:
        target.row_factory = sqlite3.Row
        target.executescript(frozen_layout().read_text())
        target.execute("PRAGMA recursive_triggers=ON")
        target.execute("INSERT INTO wiki_schema VALUES(13,'original-format-13')")
        target.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                       (str(root), registration["vault_id"]))
        for table in ARTICLE_TABLES:
            copy_article_table(source, target, table)
        target.commit()
    staged.replace(state / "index.sqlite3")
    return root, state, registration, owner, articles


def retained(store, identifier):
    with store.connect() as db:
        return db.execute("SELECT 1 FROM jobs WHERE id=?", (identifier,)).fetchone() is not None


@pytest.mark.parametrize("contradictory_receipt", [False, True])
async def test_rejected_execution_with_its_own_operation_receipt_is_retained(old_history, contradictory_receipt):
    root, state, registration, _, articles = old_history
    identifier = "not-admitted"
    reference = {"attempt": 1, "session_id": "original-session", "request_id": "original-request",
                 "work_id": "wiki-original-request", "phase": "rejected", "disposition": "not_admitted"}
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        insert_job(db, articles[0], identifier, execution=reference, job_status="cancelled", item_status="pending")
        db.execute("UPDATE wiki_job_state SET state_version=state_version+1,settlement_phase='confirmed' WHERE job_id=?", (identifier,))
        if contradictory_receipt:
            db.execute("""INSERT INTO operation_receipts(
                id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details)
                VALUES('contradictory-operation',?,?,?,?,?,?,'no_change','Original operation',NULL,'time',?)""",
                (identifier, articles[0]["id"], articles[0]["id"], ARTICLE_PATH, articles[0]["revision"],
                 articles[0]["revision"], json.dumps({"execution": {key: reference[key] for key in ("attempt", "session_id", "request_id")}})))
        db.commit()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        assert retained(current, identifier) is contradictory_receipt
        with current.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts WHERE id='contradictory-operation'").fetchone()[0] == int(contradictory_receipt)
    assert (root / ARTICLE_PATH).read_text().startswith("# Original\n")


@pytest.mark.parametrize("pointer", [None, "unrelated-session"])
async def test_completed_execution_can_release_its_live_session_pointer(old_history, pointer):
    root, state, registration, _, articles = old_history
    identifier = "normal-completion"
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        insert_job(db, articles[0], identifier, execution=original_execution(identifier))
        db.execute("UPDATE jobs SET session_id=? WHERE id=?", (pointer, identifier))
        db.commit()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        assert retained(current, identifier) is (pointer is not None)
        with current.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()[0] == 1


@pytest.mark.parametrize("contradictory_terminal", [False, True])
async def test_shared_previous_execution_allows_each_items_original_result(old_history, contradictory_terminal):
    root, state, registration, _, articles = old_history
    identifier = "retried-batch"
    execution = original_execution(identifier) | {"attempt": 2}
    previous = original_execution("original-batch")
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        first = insert_job(db, articles[0], identifier, execution=execution,
                           previous=previous | {"item_result": {"path": ARTICLE_PATH, "status": "failed", "error": "First item result"}})
        second = first | {"doc": articles[1]["id"], "path": "Library/second.md", "revision": articles[1]["revision"],
                          "input_revision": articles[1]["revision"], "output": "Library/second.md",
                          "output_revision": articles[1]["revision"], "receipt_id": "second-receipt"}
        original = previous | {"item_result": {"path": "Library/second.md", "status": "attention", "error": "Second item result"}}
        if contradictory_terminal:
            original["terminal"] = previous["terminal"] | {"status": "failed"}
        second["previous_worker_requests"] = [original]
        db.execute("UPDATE jobs SET items=? WHERE id=?", (json.dumps([first, second]), identifier))
        db.execute("UPDATE wiki_job_state SET state_version=state_version+1,attempt=2 WHERE job_id=?", (identifier,))
        db.execute("""INSERT INTO operation_receipts(
            id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details)
            VALUES('second-receipt',?,?,?,'Library/second.md',?,?,'no_change','Original second output',NULL,'time','{}')""",
            (identifier, articles[1]["id"], articles[1]["id"], articles[1]["revision"], articles[1]["revision"]))
        db.commit()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        assert retained(current, identifier) is contradictory_terminal
        with current.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()[0] == 2


async def test_original_chat_card_survives_verified_wiki_history_retirement(old_history):
    root, state, registration, owner, articles = old_history
    identifier = "original-chat-task"
    database = Database(settings.db_path)
    try:
        await database.initialize()
        agent = await database.get_system_agent(owner)
        await database.save_session("original-card-chat", "Original chat", str(root), "original-time",
                                    agent_id=agent["id"], user_id=owner, backend="codex")
        with closing(sqlite3.connect(state / "index.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("BEGIN IMMEDIATE")
            insert_job(db, articles[0], identifier, execution=original_execution(identifier))
            db.execute("UPDATE jobs SET session_id=NULL WHERE id=?", (identifier,))
            job_events.subscribe(db, identifier, job_events.Requester(owner, "chat", "original-card-chat", "origin-request"))
            delivery, = job_events.pending(db)
            db.commit()
        assert await database.receive_wiki_job_notice(registration["vault_id"], owner, delivery) == "delivered"
        original = await database.wiki_job_cards("original-card-chat", owner)
        assert len(original) == 1 and original[0]["job_id"] == identifier
        with closing(sqlite3.connect(state / "index.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA recursive_triggers=ON")
            job_events.acknowledge(db, delivery["event_id"], delivery["subscriber_id"], "delivered")
            db.commit()
        with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
            assert not retained(current, identifier)
            with pytest.raises(WikiError) as missing:
                job_details.detail(WikiJobs(owner, store=current, registration=registration), identifier)
            assert missing.value.status == 404
            with current.connect() as db:
                for table in ("wiki_job_state", "wiki_job_outbox", "wiki_job_subscribers"):
                    assert db.execute(f"SELECT count(*) FROM {table} WHERE job_id=?", (identifier,)).fetchone()[0] == 0
        assert await database.wiki_job_cards("original-card-chat", owner) == original
        assert await database.wiki_job_cards("original-card-chat", "foreign-account") == []
    finally:
        await database.close()

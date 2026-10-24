"""Current registered Wiki facts for restart and publication regressions."""

import hashlib
import json
from contextlib import closing

import pytest

from server.models import TurnOutcome
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account

ARTICLE_PATH = "Library/article.md"

FACTS = ("document_identities", "documents", "managed_notes", "material_origins", "uid_aliases",
         "source_bindings", "source_binding_head", "source_binding_observations", "wiki_protocols",
         "wiki_job_controls", "wiki_job_state", "wiki_job_outbox", "wiki_job_subscribers",
         "wiki_job_deliveries", "operation_receipts", "job_publications", "jobs", "job_requests")


def facts(db):
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    result = {}
    for name in FACTS:
        if name in present:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
            selected = ",".join('"' + column + '"' for column in columns)
            result[name] = sorted(map(tuple, db.execute(f'SELECT {selected} FROM "{name}"')), key=repr)
    return result


def original_execution(label, *, phase="settled", retired=True):
    request = "original-request-" + label
    result = {"attempt": 1, "session_id": "original-session-" + label,
              "request_id": request, "work_id": "wiki-" + request, "phase": phase,
              "recording_state": "committed" if phase == "settled" else "unconfirmed", "retired": retired}
    if phase == "settled":
        result["terminal"] = TurnOutcome(request_id=request, status="success").model_dump()
    return result

def insert_job(db, article, identifier, *, execution=None, previous=None, job_status="completed", item_status="done"):
    receipt_id = "receipt-" + identifier
    item = {"doc": article["id"], "path": ARTICLE_PATH, "revision": article["revision"],
            "input_revision": article["revision"], "status": item_status,
            "index_complete": item_status == "done", "output": ARTICLE_PATH,
            "output_revision": article["revision"], "operation": "no_change", "receipt_id": receipt_id,
            "output_change_id": None, "original_extension": {"text": identifier, "values": [None, False, 0]}}
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


@pytest.fixture
async def current_history(registered_account):
    root, state, registration, owner = registered_account
    (root / "Library").mkdir()
    for path in (ARTICLE_PATH, "Library/second.md"):
        (root / path).write_text("# Original\nKeep the original argument and its conditions.\n")
    with closing(WikiStore(root, state, registration["vault_id"])) as store:
        store.refresh()
        articles = [store.read(path) for path in (ARTICLE_PATH, "Library/second.md")]
    return root, state, registration, owner, articles

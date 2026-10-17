"""Durable operation receipts and maintenance assignment context."""

import json
import uuid
from datetime import UTC, datetime

from .store import WikiError, revision


def publication(task, store, document=None, item_doc=None):
    if task is None or not task.job_id:
        return None
    from .jobs import WikiJobs

    job = WikiJobs(task.user_id).get(task.job_id)
    if job["status"] != "running" or job["session_id"] != task.session_id:
        raise WikiError("This maintenance assignment is no longer active", 403)
    selected = [item for item in job["items"] if item["doc"] == item_doc or document and item["path"] == document["path"]]
    if not selected and len(job["items"]) == 1:
        selected = job["items"]
    if len(selected) != 1 or selected[0]["path"] not in task.writable:
        raise WikiError("Identify the assigned input item for this operation", 422)
    item = selected[0]
    if document is None:
        from .tasks import tasks

        document = store.read(item["doc"], length=1)
        tasks.require_read(task, store, document["path"], document["revision"])
    return {"job_id": task.job_id, "session_id": task.session_id, "source_doc": item["doc"], "source_path": item["path"], "input_revision": document["revision"]}


def record(db, *, doc_id, path, before, after, operation, reason, publication=None, change_id=None, details=None):
    if publication:
        if not db.execute("SELECT 1 FROM jobs WHERE id=? AND session_id=? AND status='running'", (publication["job_id"], publication["session_id"])).fetchone():
            raise WikiError("This maintenance assignment is no longer active", 403)
    identifier = uuid.uuid4().hex
    details = dict(details or {})
    if publication:
        details |= {"input_source_doc": publication["source_doc"], "input_source_revision": publication["input_revision"]}
    db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        identifier, publication["job_id"] if publication else None,
        publication["source_doc"] if publication else None, doc_id, path,
        before, after, operation, reason, change_id, datetime.now(UTC).isoformat(), json.dumps(details, ensure_ascii=False),
    ))
    return identifier


def management(store, path, text, *, replaced=()):
    """Revoke only expired legacy locators; retain the unchanged regions."""
    with store.connect() as db:
        row = db.execute("SELECT * FROM managed_notes WHERE path=?", (path,)).fetchone()
    if row is None:
        return None
    regions = [part for part in json.loads(row["regions"]) if text.count(part["raw"]) == 1]
    base = text
    for part in regions:
        base = base.replace(part["raw"], "", 1)
    metadata = json.loads(row["metadata"])
    for field in replaced:
        metadata.pop(field, None)
    return dict(row) | {"regions": json.dumps(regions, ensure_ascii=False), "metadata": json.dumps(metadata, ensure_ascii=False), "base_hash": revision(base.encode()), "rendered_revision": revision(text.encode())}


def replaced_regions(store, path, text, replaced):
    """Only trusted, unchanged legacy tag/alias additions may be removed."""
    with store.connect() as db:
        row = db.execute("SELECT regions FROM managed_notes WHERE path=?", (path,)).fetchone()
    edits = []
    for part in json.loads(row[0]) if row else []:
        if part["role"] in set(replaced) & {"tags", "aliases"}:
            if text.count(part["raw"]) != 1:
                raise WikiError("A user edited a Weber section; preserve it and review before replacing metadata", 409)
            start = text.index(part["raw"])
            edits.append((start, start + len(part["raw"]), ""))
    return sorted(edits)

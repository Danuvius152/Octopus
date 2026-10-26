"""Durable operation receipts and maintenance assignment context."""

import json
import uuid
from datetime import UTC, datetime

from . import job_items
from .store import WikiError, revision


def publication(task, store, document=None, item_doc=None, *, path=None, reservation_id=None):
    if task is None or not task.job_id:
        return None
    from . import grants

    execution = ({"attempt": task.assigned_attempt, "session_id": task.session_id,
                  "request_id": task.request_id} if task.assigned_attempt is not None and task.request_id else None)
    with store.connect() as db:
        db.execute("BEGIN")
        grants.active(db, task, store)
        if path is not None:
            reservation = grants.authorize(db, task, store, path, create=True)
            if reservation_id is not None and reservation["id"] != reservation_id:
                raise WikiError("This reservation does not authorize the requested path", 403)
            return {"job_id": task.job_id, "session_id": task.session_id, "source_doc": None,
                    "source_path": path, "input_revision": None, "authority": task,
                    "reservation_id": reservation["id"], "execution": execution}
        job = job_items.decode(db.execute("SELECT items FROM jobs WHERE id=?", (task.job_id,)).fetchone()[0])
        bound = db.execute("SELECT reservation_id FROM wiki_grant_documents WHERE job_id=? AND doc_id=?",
                           (task.job_id, document["id"])).fetchone() if document else None
    selected = [item for item in job if item.get("doc") == item_doc and item_doc is not None
                or document and item.get("doc") == document["id"]
                or bound and bound[0] and item.get("reservation_id") == bound[0]]
    if not selected and len(job) == 1:
        selected = job
    if len(selected) != 1:
        raise WikiError("Identify the assigned input item for this operation", 422)
    item = selected[0]
    if document is None:
        from .tasks import tasks

        document = store.read(item["doc"], length=1)
        tasks.require_read(task, store, document["path"], document["revision"])
    with store.connect() as db:
        grants.authorize(db, task, store, document["path"])
    return {"job_id": task.job_id, "session_id": task.session_id, "source_doc": item.get("doc") or document["id"],
            "source_path": document["path"], "input_revision": document["revision"], "authority": task,
            "execution": execution}


def record(db, *, doc_id, path, before, after, operation, reason, publication=None, change_id=None, details=None, store=None):
    if publication:
        from . import grants

        grants.check_write(db, store, publication, path, change_id=change_id, doc_id=doc_id)
    identifier = uuid.uuid4().hex
    details = dict(details or {})
    from .readback import details as reading_details

    details = reading_details(db, publication, doc_id, before, after, details)
    if publication:
        if publication.get("execution") is not None:
            details["execution"] = publication["execution"]
        if publication.get("supporting_navigation"):
            # Keep the exact supporting association out of the primary item
            # field, which F09 uses to attribute maintenance attempts/baselines.
            details["supporting_navigation"] = {
                "target": {key: value for key, value in publication["supporting_navigation"]["target"].items()
                           if key not in {"physical_path", "file_key"}},
                "inputs": [{key: value for key, value in proof.items() if key not in {"physical_path", "file_key"}}
                           for proof in publication["supporting_navigation"]["inputs"]],
            }
        else:
            details |= {"input_source_doc": publication["source_doc"], "input_source_revision": publication["input_revision"]}
        if publication.get("reservation_id"):
            details["creation_reservation_id"] = publication["reservation_id"]
    db.execute("""INSERT INTO operation_receipts(
        id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
        identifier, publication["job_id"] if publication else None,
        (publication["source_doc"] or doc_id) if publication and not publication.get("supporting_navigation") else None, doc_id, path,
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

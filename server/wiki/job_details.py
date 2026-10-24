"""Read-only projections of the original item, execution and file receipts."""

from . import grants, job_items
from .history import WikiHistory
from .store import WikiError, revision


def receipt_attempt(receipt):
    return receipt["details"].get("execution", {}).get("attempt")


def actions(jobs, job):
    stopped = job["status"] not in {"queued", "running"}
    reason = None
    if stopped:
        try:
            jobs.require_retired(job)
        except WikiError as error:
            if error.status != 409:
                raise
            reason = str(error)
    settled = job["settlement_phase"] not in {"pending", "attention", "unconfirmed"}
    scope = (job["grant"] is not None and not grants.scope_blocked(job.get("error"))
             and job["grant"]["registration_generation"] == jobs.registration["registration_generation"])
    unfinished = any(item["status"] != "done" and not job_items.unresolved(item)
                     and item["status"] not in {"unknown", "verifying", "excluded"}
                     and not item.get("receipt_id") and not item.get("output_change_id") for item in job["items"])
    return {"verify": stopped and reason is None, "retry": stopped and reason is None and settled and scope and unfinished,
            "readmit": stopped and reason is None and settled and not scope,
            "blocked_reason": reason}


def detail(jobs, job_id, *, attempt=None, offset=0, limit=30):
    job = jobs.get(job_id)
    selected = job["attempt"] if attempt is None else attempt
    attempts = {job["attempt"]}
    attempts.update(int(value) for value in job["attempt_errors"])
    attempts.update(ref["attempt"] for ref in job_items.references(job["items"]) if isinstance(ref.get("attempt"), int))
    attempts.update(value for receipt in job["receipts"] if isinstance(value := receipt_attempt(receipt), int))
    if selected not in attempts:
        raise WikiError("Maintenance attempt changed; refresh the task details", 409)
    items = []
    error = (job["error"] if selected == job["attempt"] and job["error"] is not None
             else job["attempt_errors"].get(str(selected)))
    for item in job["items"]:
        references = [*item.get("previous_worker_requests", []), *([item["worker_request"]] if item.get("worker_request") else [])]
        reference = next((ref for ref in reversed(references) if ref.get("attempt") == selected), None)
        if selected != job["attempt"] and reference is None:
            continue
        evidence = reference.get("item_result", item) if reference and selected != job["attempt"] else item
        items.append({"key": item.get("reservation_id") or item.get("doc") or item["path"],
                      "doc": item.get("doc"), "path": evidence.get("path", item["path"]),
                      "status": evidence["status"], "error": evidence.get("error"), "execution": reference})
    outputs = []
    with jobs.store.connect() as db:
        for receipt in job["receipts"]:
            original_attempt = receipt_attempt(receipt)
            if original_attempt != selected and not (original_attempt is None and selected == job["attempt"]):
                continue
            current = db.execute("SELECT i.path,d.revision FROM document_identities i LEFT JOIN documents d ON d.id=i.id WHERE i.id=?",
                                 (receipt["doc_id"],)).fetchone()
            outputs.append({"receipt_id": receipt["id"], "doc": receipt["doc_id"], "path": receipt["path"],
                            "revision": receipt["output_revision"], "current_path": current["path"] if current else None,
                            "current_revision": current["revision"] if current else None, "operation": receipt["operation"],
                            "status": receipt["status"], "reason": receipt["reason"], "change_id": receipt["change_id"],
                            "attempt": original_attempt, "details": receipt["details"]})
    return {"job_id": job_id, "vault_id": jobs.registration["vault_id"], "state_version": job["state_version"],
            "current_attempt": job["attempt"], "attempt": selected, "attempts": sorted(attempts), "status": job["status"],
            "settlement_phase": job["settlement_phase"], "error": error,
            "items": items[offset:offset + limit], "outputs": outputs[offset:offset + limit],
            "next_offset": offset + limit if max(len(items), len(outputs)) > offset + limit else None,
            "actions": actions(jobs, job)}


def output_page(jobs, job_id, receipt_id, *, attempt, view="history", start=0, length=16000, expected_revision=None):
    job = jobs.get(job_id)
    receipt = next((row for row in job["receipts"] if row["id"] == receipt_id), None)
    if receipt is None:
        raise WikiError("Original output receipt not found", 404)
    original_attempt = receipt_attempt(receipt)
    if original_attempt != attempt and not (original_attempt is None and attempt == job["attempt"]):
        raise WikiError("This receipt belongs to another maintenance attempt", 409)
    if view == "current":
        document, raw = jobs.store.read_snapshot(receipt["doc_id"], length=1, expected_revision=expected_revision)
        path, digest = document["path"], document["revision"]
    else:
        digest = receipt["output_revision"]
        if not digest or not (WikiHistory(jobs.store).blobs / digest).is_file():
            raise WikiError("Historical output bytes are unavailable; the original receipt is retained", 410)
        path = receipt["path"]
        raw = (WikiHistory(jobs.store).blobs / digest).read_bytes()
        if revision(raw) != digest:
            raise WikiError("Historical output hash mismatch; current content was preserved", 409)
    text = raw.decode("utf-8", errors="replace")
    end = min(start + length, len(text))
    return {"receipt_id": receipt_id, "doc": receipt["doc_id"], "path": path, "revision": digest, "view": view,
            "content": text[start:end], "start": start, "next_start": end if end < len(text) else None, "characters": len(text)}

"""Persist an exact worker request before admission; late facts use its coordinates."""

import asyncio
import json
from datetime import UTC, datetime

from . import job_items
from .maintenance_contract import MAINTENANCE_OPERATIONS
from .registry import registry
from .store import WikiError


def item_key(item):
    return ("reservation", item["reservation_id"]) if item.get("kind") == "creation" else ("document", item["doc"])


def coordinates(reference):
    return tuple(reference.get(key) for key in ("attempt", "session_id", "request_id"))


def is_current(job, reference):
    return (reference.get("attempt") == job["attempt"]
            and any(coordinates(item.get("worker_request", {})) == coordinates(reference) for item in job["items"]))


def control_jobs(captured):
    """Reacquire control metadata in the same vault, without renewing write grants."""
    if not captured.store._retired and registry.config(captured.user_id) == captured.registration:
        return captured
    from .jobs import WikiJobs

    current = WikiJobs(captured.user_id)
    if (current.store.state.resolve() != captured.store.state.resolve()
            or current.registration["vault_id"] != captured.registration["vault_id"]):
        raise WikiError("Original worker control belongs to another vault", 409)
    return current


def prepare(jobs, job, batch, session_id, request_id):
    """One final writer CAS, before either assignment or start_message exists."""
    reference = {"attempt": job["attempt"], "session_id": session_id, "request_id": request_id,
                 "work_id": f"wiki-{request_id}", "phase": "prepared"}
    expected = {item_key(item): item for item in batch}
    if not batch or len(expected) != len(batch) or len(batch) > 3:
        raise ValueError("Worker admission needs its exact bounded batch")
    with registry._account_lock(jobs.user_id), registry.registration_lock(jobs.user_id), jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if registry.config(jobs.user_id) != jobs.registration:
            raise WikiError("Wiki registration changed before worker admission", 409)
        current = jobs.get(job["id"], db=db)
        if ((current["attempt"], current["status"], current["session_id"])
                != (job["attempt"], "running", job["session_id"])):
            raise WikiError("Original maintenance attempt changed before worker admission", 409)
        selected = [item for item in current["items"] if item_key(item) in expected]
        if len(selected) != len(batch):
            raise WikiError("Original worker batch changed before admission", 409)
        for item in selected:
            if item != expected[item_key(item)] or item["status"] != "pending":
                raise WikiError("Original worker item changed before admission", 409)
            prior = item.get("worker_request")
            if prior and prior.get("phase") not in {"settled", "rejected"}:
                raise WikiError("Verify the original worker request before another admission", 409)
            if prior:
                item.setdefault("previous_worker_requests", []).append(prior)
            item["worker_request"] = dict(reference)
        db.execute("UPDATE jobs SET session_id=?,items=?,updated_at=? WHERE id=?", (
            session_id, job_items.encode(current["items"]), datetime.now(UTC).isoformat(), job["id"],
        ))
    # Only reflect a prepared intent in the caller after its durable commit.
    for item in batch:
        prior = item.get("worker_request")
        if prior:
            item.setdefault("previous_worker_requests", []).append(prior)
        item["worker_request"] = dict(reference)
    job["session_id"] = session_id
    return reference


def transition(jobs, job_id, reference, phase, *, recording_state=None, disposition=None, terminal=None, retired=False):
    """Merge only original execution metadata, never stale item outcomes or status."""
    jobs = control_jobs(jobs)
    allowed = {
        "prepared": {"prepared", "accepted", "rejected", "unknown", "settled"},
        "accepted": {"accepted", "unknown", "settled"},
        "unknown": {"unknown", "accepted", "rejected", "settled"},
        "rejected": {"rejected"}, "settled": {"settled"},
    }
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = jobs.get(job_id, db=db)
        if current["attempt"] != reference["attempt"]:
            raise WikiError("Original worker attempt was superseded", 409)
        matched = [item for item in current["items"]
                   if coordinates(item.get("worker_request", {})) == coordinates(reference)]
        if not matched:
            raise WikiError("Original worker session/request was superseded", 409)
        changed = False
        for item in matched:
            before = json.dumps(item, ensure_ascii=False, sort_keys=True)
            prior = item["worker_request"]
            if phase not in allowed.get(prior.get("phase"), set()):
                raise WikiError("Original worker handoff phase changed", 409)
            following = prior | {"phase": phase}
            if recording_state is not None:
                following["recording_state"] = recording_state
            if disposition is not None:
                following["disposition"] = disposition
            if terminal is not None:
                if terminal.get("request_id") != reference["request_id"] or phase != "settled":
                    raise WikiError("Original worker terminal does not match its execution", 409)
                following["terminal"] = terminal
            if retired:
                following["retired"] = True
            if phase == "unknown":
                item["status"] = "attention"
                item["error"] = "Original request admission or result recording is unconfirmed; verify it without rerunning the model"
            item["worker_request"] = following
            changed |= json.dumps(item, ensure_ascii=False, sort_keys=True) != before
        if changed:
            db.execute("UPDATE jobs SET items=?,updated_at=? WHERE id=?", (
                job_items.encode(current["items"]), datetime.now(UTC).isoformat(), job_id,
            ))
    reference.update(phase=phase)
    if recording_state is not None:
        reference["recording_state"] = recording_state
    if disposition is not None:
        reference["disposition"] = disposition
    if terminal is not None:
        reference["terminal"] = terminal
    if retired:
        reference["retired"] = True
    return reference


def require_completion(reference, completion):
    request_id = reference["request_id"]
    if (completion.input_request_id != request_id or completion.request_id != request_id
            or completion.outcome is not None and completion.outcome.request_id != request_id):
        raise WikiError("Original worker completion request does not match its prepared intent", 409)


async def retire(manager, reference):
    """Join the original work's captured resources, including a held backend."""
    from ..bg_tasks import bg_task_manager
    from ..mcp_identity import close_work
    from ..research import research_manager

    work_id = reference["work_id"]
    close_work(work_id)

    async def sessions():
        affected = await manager.close_work(work_id)
        await manager.finish_work_cancellation([affected])

    await join_retirement(bg_task_manager.cancel_work(work_id), research_manager.cancel_work(work_id), sessions())


async def join_retirement(*operations):
    """Report failures after every captured cleanup and join has finished."""
    results = await asyncio.gather(*operations, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Original maintenance resource retirement failed", errors)


async def cancel_requests(manager, references):
    from ..mcp_identity import close_work

    operations = []
    for reference in references:
        if reference.get("session_id") and reference.get("request_id") and reference.get("phase") != "rejected":
            operations.append(manager.cancel_request(reference["session_id"], reference["request_id"]))
            if work_id := reference.get("work_id"):
                close_work(work_id)
                operations.append(retire(manager, reference))
    await join_retirement(*operations)


def reconcile_cancelled_effects(jobs, captured, *, manager):
    """Verify accepted effects and repair derived indexing, with no new work.

    Operation receipts remain unchanged. Items record the actual verification
    result; a saved framework input still awaits its original organization.
    """
    from .history import WikiHistory

    # Verification must not run the history writer's implicit recovery of
    # prepared changes. Such a proposal remains uncertain and retained.
    with WikiHistory(jobs.store).locked(shared=True), registry._account_lock(jobs.user_id), registry.registration_lock(jobs.user_id):
        if registry.config(jobs.user_id) != jobs.registration:
            raise WikiError("Wiki registration changed before cancellation verification", 409)
        job = jobs.get(captured["id"])
        if (job["attempt"], job["status"]) != (captured["attempt"], "cancelled"):
            raise WikiError("Original cancellation changed before output verification", 409)
        jobs.require_retired(job, manager=manager)
        before = json.dumps(job["items"], ensure_ascii=False)
        verified = True
        applied = [receipt for receipt in job["receipts"] if receipt["status"] == "applied"]
        for item in job["items"]:
            item.pop("cancellation_effect_error", None)
            if item["status"] == "excluded":
                # Its original receipts are still verified below; finite
                # re-admission has removed this item from result settlement.
                continue
            if item.get("kind") == "creation":
                accepted = any(receipt["details"].get("creation_reservation_id") == item["reservation_id"]
                               or (item.get("doc") and receipt["item_doc"] == item["doc"]
                                   and receipt["operation"] in MAINTENANCE_OPERATIONS) for receipt in applied)
                recovered = accepted and jobs.settle_item(job["id"], item)
            elif item.get("doc"):
                accepted = any(receipt["item_doc"] == item["doc"] for receipt in applied)
                recovered = accepted and jobs.settle_item(job["id"], item)
            else:
                recovered = False
            if recovered and not item.get("index_complete"):
                verified = False

        latest = {}
        for receipt in job["receipts"]:
            if receipt["doc_id"]:
                latest[receipt["doc_id"]] = receipt
            with jobs.store.connect() as db:
                change = db.execute("SELECT status FROM changes WHERE id=?", (receipt["change_id"],)).fetchone() if receipt["change_id"] else None
            if receipt["change_id"] and (change is None or change["status"] != "applied"):
                verified = False

        for receipt in latest.values():
            related = [item for item in job["items"] if item.get("doc") and item["doc"] in {receipt["item_doc"], receipt["doc_id"]}]
            try:
                if receipt["status"] != "applied":
                    raise WikiError("Original operation has no confirmed applied marker", 409)
                if receipt["output_revision"] is None:
                    with jobs.store.connect() as db:
                        identity = db.execute("SELECT path FROM document_identities WHERE id=?", (receipt["doc_id"],)).fetchone()
                    if (not receipt["change_id"] or identity is None or identity["path"] is not None
                            or jobs.store.path(receipt["path"]).exists()):
                        raise WikiError("Original removal could not be verified", 409)
                    continue
                output = jobs.store.read(receipt["doc_id"], length=1, expected_revision=receipt["output_revision"])
                if output["path"] != receipt["path"]:
                    raise WikiError("Original output path or identity changed", 409)
                with jobs.store.connect() as db:
                    jobs.store.require_indexed(db, output["path"])
                    db.execute("UPDATE job_publications SET indexed=1 WHERE job_id=? AND change_id=?",
                               (job["id"], receipt["change_id"]))
                if receipt["operation"] == "ingest":
                    for item in related:
                        if not jobs.publication(job["id"], item.get("doc")):
                            item.update(output=output["path"], output_revision=output["revision"],
                                        receipt_id=receipt["id"], operation="ingest", index_complete=True)
            except WikiError as exc:
                verified = False
                for item in related or job["items"][:1]:
                    item.update(index_complete=False, cancellation_effect_error=str(exc))
                    if item["status"] != "excluded":
                        item["status"] = "attention"
        with jobs.store.connect() as db:
            if db.execute("SELECT 1 FROM job_publications p JOIN changes c ON c.id=p.change_id WHERE p.job_id=? AND c.status='applied' AND p.indexed=0 LIMIT 1",
                          (job["id"],)).fetchone():
                verified = False
        after = json.dumps(job["items"], ensure_ascii=False)
        if after != before:
            with jobs.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = jobs.get(job["id"], db=db)
                jobs.require_retired(current, manager=manager)
                if ((current["attempt"], current["status"]) != (job["attempt"], "cancelled")
                        or json.dumps(current["items"], ensure_ascii=False) != before
                        or registry.config(jobs.user_id) != jobs.registration):
                    raise WikiError("Original cancellation changed during output verification", 409)
                db.execute("UPDATE jobs SET items=? WHERE id=?", (job_items.encode(job["items"]), job["id"]))
        return verified

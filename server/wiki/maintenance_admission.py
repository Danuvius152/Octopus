"""Final checked admission commits observations, aliases and provenance together."""

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from ..mcp_identity import McpScope, scope_is_live
from . import maintenance_selections as selections
from .formats import is_markdown_maintenance
from .history import WikiHistory
from .maintenance_contract import DAILY_SCOPE, PURPOSE, actor_for, digest
from .maintenance_projection import dirty_ids, settle
from .maintenance_view import MaintenanceSnapshot
from .registry import registry
from .store import WikiError
from .tasks import tasks


def prepare(jobs, review):
    """All synchronizing document reads finish before the final writer begins."""
    try:
        current = jobs.store.read(review["doc"], length=1)
    except WikiError as exc:
        if exc.status != 404:
            raise
        raise selections.error("source_missing", "The observed document identity is no longer current", 409) from exc
    if current["id"] != review["doc"]:
        raise selections.error("identity_changed", "The selected identity was replaced", 409)
    settle(jobs.store, doc=current["id"])


def _authorize(jobs, db, key, request, review, task, mcp_scope: McpScope | None):
    if mcp_scope is not None and (task is None or not scope_is_live(mcp_scope)
            or mcp_scope.wiki_task_id is None
            or tasks.active.get(mcp_scope.wiki_task_id) is not task
            or (mcp_scope.session_id, mcp_scope.user_id) != (task.session_id, task.user_id)):
        raise WikiError("The maintenance request's original model run is no longer authorized", 403)
    if task is not None:
        if (mcp_scope is None
                or task.user_id != jobs.user_id or actor_for(task) != request["actor"]
                or task.mode not in {"auto", "maintain"}):
            raise WikiError("The maintenance entry turn is no longer authorized", 403)
        if task.registration is not None and task.registration != jobs.registration:
            raise WikiError("The maintenance assignment registration changed", 403)
    tasks.require_selection(task, request["paths"], jobs.store, checked=True, db=db)
    observation = {name: value for name, value in review.items() if name != "review_key"}
    expected = "wiki-daily-v1:" + digest([jobs.user_id, jobs.registration, request["actor"], PURPOSE, observation])
    if key != review["review_key"] or key != expected:
        raise selections.error("review_mismatch", "Use the matching complete observation and review key", 409)


def _current(jobs, db, key, request, review, task, mcp_scope: McpScope | None):
    """Uses only the final connection; no file/index synchronization or new writer."""
    _authorize(jobs, db, key, request, review, task, mcp_scope)
    previous = jobs.previous_request(key, request, db=db)
    current = db.execute("SELECT id,path,revision,quality FROM documents WHERE id=?", (review["doc"],)).fetchone()
    if current is None:
        raise selections.error("source_missing", "The observed identity is no longer current", 409)
    if not is_markdown_maintenance(current["path"]):
        raise WikiError("Checked maintenance requires a current Markdown note", 422)
    if dirty_ids(db, identifiers=[current["id"]], limit=1):
        raise selections.error("maintenance_updating", "Maintenance observations changed before the final snapshot", 503)
    snapshot = MaintenanceSnapshot(jobs.store, jobs.registration, jobs.user_id, db, request["actor"], identifiers=[current["id"]])
    projected = snapshot.project(current["id"])
    if previous:
        accepted = any(row["job_id"] == previous["id"] and row["output_revision"] == current["revision"]
                       for row in snapshot.accepted.get(current["id"], ()))
        if accepted:
            if not snapshot.ready or projected["source_warnings"] or projected["attempt_count"]:
                raise selections.error("review_changed", "The prior output's current dependencies or index need review", 409)
            return dict(current), previous
        if previous["status"] in {"queued", "running", "attention", "failed", "cancelled"}:
            landed = any(row["job_id"] == previous["id"] and row["doc_id"] == current["id"]
                         and row["output_revision"] == current["revision"] and row["change_status"] == "applied"
                         for row in snapshot.receipts)
            if current["revision"] == review["revision"] or landed:
                return dict(current), previous
        raise selections.error("review_changed", "The historical request does not certify the current document", 409)
    if not snapshot.ready:
        raise selections.error("maintenance_updating", "Current index and maintenance facts must settle before admission", 503)
    if current["revision"] != review["revision"]:
        raise selections.error("revision_changed", "Document revision changed; observe this item again", 409)
    if current["path"] != review["path"] or projected["review"] != review:
        raise selections.error("review_changed", "Identity, baseline or direct dependencies changed; observe this item again", 409)
    if not projected["actionable"]:
        raise selections.error("not_actionable", "This document is unchanged, unverified or already has retained work", 409)
    return dict(current), None


def commit(jobs, key, request, review, task, *, selection_id=None, selection_ordinal=None, mcp_scope: McpScope | None = None, requester=None):
    """Caller holds history/account/registration locks; this is the final TX."""
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        selections.require(db)
        if selection_id is not None:
            selections.check_item(db, selection_id, selection_ordinal, review, jobs.registration, jobs.user_id, request["actor"])
        current, previous = _current(jobs, db, key, request, review, task, mcp_scope)
        created = previous is None
        if created:
            payload = {"kind": "curation", "new_only": False, "material": "", "scope": DAILY_SCOPE,
                       "paths": request["paths"], "target": None, "sources": [], "actor": request["actor"], "request": request}
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            job_id, now = uuid.uuid4().hex, datetime.now(UTC).isoformat()
            items = [{"status": "pending", "path": current["path"], "doc": current["id"],
                      "revision": current["revision"], "quality": current["quality"]}]
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (job_id, key, hashlib.sha256(raw.encode()).hexdigest(), raw, "queued",
                        json.dumps(items, ensure_ascii=False), None, None, now, now))
            db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, jobs.request_hash(request), job_id))
            from . import grants

            grants.issue(db, jobs.registration, job_id, items, policy=task.mode if task else request["actor"])
        else:
            job_id = previous["id"]
        if selection_id is not None:
            selections.accept(db, job_id, selection_id, selection_ordinal, review)
        jobs.subscribe(db, job_id, requester)
        result = jobs.get(job_id, db=db)
    return result, created


def admit(jobs, key, material, paths, target, sources, actor, created_changes, review, task, *, selection_id=None, selection_ordinal=None, mcp_scope: McpScope | None = None, requester=None):
    from .jobs import worker

    if material or target or sources or created_changes or paths != [review["path"]] or not key.strip():
        raise WikiError("A checked daily review selects one existing document only", 422)
    request = {"material": material, "scope": DAILY_SCOPE, "paths": paths, "target": target,
               "sources": sources, "actor": actor, "created_changes": created_changes, "review": review}
    # Construction is already complete. History precedes account; config stays
    # shared-locked from its final generation check through commit.
    with jobs.store.borrow(), WikiHistory(jobs.store).locked():
        prepare(jobs, review)
        with registry._account_lock(jobs.user_id), registry.registration_lock(jobs.user_id):
            registration = registry.config(jobs.user_id)
            if registration != jobs.registration or jobs.store.registration_generation != registration["registration_generation"]:
                raise selections.error("registration_changed", "Wiki registration changed before admission", 409)
            try:
                result, created = commit(jobs, key, request, review, task,
                                         selection_id=selection_id, selection_ordinal=selection_ordinal,
                                         mcp_scope=mcp_scope, requester=requester)
            except sqlite3.DatabaseError as exc:
                raise selections.unavailable() from exc
    if created:
        worker.wake(jobs.user_id)
    return result

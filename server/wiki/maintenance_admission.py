"""Final checked admission commits observations, aliases and provenance together."""

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from ..mcp_identity import McpScope
from . import job_events, job_items
from . import maintenance_selections as selections
from .formats import is_markdown_maintenance
from .history import WikiHistory
from .maintenance_contract import DAILY_SCOPE, PURPOSE, digest
from .maintenance_projection import dirty_ids, settle
from .maintenance_view import MaintenanceSnapshot
from .registry import registry
from .store import WikiError
from .tasks import tasks


def submit(user_id, key, material, scope, paths, target, sources, created_changes=None, *,
           caller, **options):
    from .jobs import WikiJobs
    from .maintenance_contract import actor_for

    with registry.using(user_id) as (store, registration):
        with registry.registered(user_id, store, registration, caller=caller):
            task = caller.task(user_id, registration, write=True)
            tasks.require_submitter(task, registration)
        return WikiJobs(user_id, store=store, registration=registration, caller=caller).maintain(
            key, material, scope, paths, target, sources, actor_for(task, caller.app_id), created_changes,
            selection_task=task, mcp_scope=caller.scope, requester=job_events.requester_for(user_id, task, caller.app_id), **options)


def submit_batch(user_id, selection_id, ordinals, *, caller):
    from .jobs import WikiJobs
    from .maintenance_contract import actor_for

    with registry.using(user_id) as (store, registration):
        with registry.registered(user_id, store, registration, caller=caller):
            task = caller.task(user_id, registration, write=True)
            tasks.require_submitter(task, registration)
        return admit_batch(WikiJobs(user_id, store=store, registration=registration, caller=caller), selection_id,
                           ordinals, actor_for(task, caller.app_id), task, mcp_scope=caller.scope,
                           requester=job_events.requester_for(user_id, task, caller.app_id))


def curate(user_id, key, scope, *, caller):
    from .jobs import WikiJobs

    with registry.using(user_id) as (store, registration):
        with registry.registered(user_id, store, registration, caller=caller):
            caller.task(user_id, registration, human_only=True)
        return WikiJobs(user_id, store=store, registration=registration, caller=caller).curate(
            key, scope, requester=job_events.requester_for(user_id, app_id=caller.app_id))


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
    if task is not None and task.user_id != jobs.user_id:
        raise WikiError("The maintenance request belongs to another account", 403)
    tasks.require_submission(task, request, jobs.store, db, mcp_scope, jobs.registration, checked=True)
    observation = {name: value for name, value in review.items() if name != "review_key"}
    expected = "wiki-daily-v1:" + digest([jobs.user_id, jobs.registration, request["actor"], PURPOSE, observation])
    if key != review["review_key"] or key != expected:
        raise selections.error("review_mismatch", "Use the matching complete observation and review key", 409)


def _current(jobs, db, key, request, review, task, mcp_scope: McpScope | None):
    """Uses only the final connection; no file/index synchronization or new writer."""
    _authorize(jobs, db, key, request, review, task, mcp_scope)
    try:
        previous = jobs.previous_request(key, request, db=db)
    except WikiError as error:
        if error.status != 409 or request["target"] not in (None, ""):
            raise
        # Checked review has always accepted both absent-target representations.
        # Match only this no-op field; retain every original ledger hash.
        equivalent = request | {"target": "" if request["target"] is None else None}
        previous = jobs.previous_request(key, equivalent, db=db)
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
        if previous["status"] in {"queued", "running", "attention", "failed", "cancelled", "interrupted", "completed_with_attention"}:
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


def _insert(jobs, db, key, request, items, task, *, aliases=()):
    """One checked job, exact grant and per-document replay keys in this TX."""
    from . import grants

    payload = {"kind": "curation", "new_only": False, "material": "", "scope": DAILY_SCOPE,
               "paths": request["paths"], "target": None, "sources": [], "actor": request["actor"], "request": request}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    job_id, now = uuid.uuid4().hex, datetime.now(UTC).isoformat()
    db.execute("INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (job_id, key, hashlib.sha256(raw.encode()).hexdigest(), raw, "queued", job_items.encode(items), None, None, now, now))
    for alias_key, alias_request in [(key, request), *aliases]:
        db.execute("INSERT INTO job_requests VALUES(?,?,?)", (alias_key, jobs.request_hash(alias_request), job_id))
    grants.issue(db, jobs.registration, job_id, items, policy=task.mode if task else request["actor"])
    return job_id


def commit(jobs, key, request, review, task, *, selection_id=None, selection_ordinal=None, mcp_scope: McpScope | None = None, requester=None):
    """Caller holds history/account/registration locks; this is the final TX."""
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        selections.require(db)
        if selection_id is not None:
            selections.check_item(db, selection_id, selection_ordinal, review, jobs.registration, jobs.user_id, request["actor"])
        current, previous = _current(jobs, db, key, request, review, task, mcp_scope)
        if previous is None:
            items = [{"status": "pending", "path": current["path"], "doc": current["id"],
                      "revision": current["revision"], "quality": current["quality"]}]
            job_id = _insert(jobs, db, key, request, items, task)
        else:
            job_id = previous["id"]
        if selection_id is not None:
            selections.accept(db, job_id, selection_id, selection_ordinal, review)
        jobs.subscribe(db, job_id, requester)
        result = jobs.get(job_id, db=db)
    return result


def _request(review, actor, target=None):
    return {"material": "", "scope": DAILY_SCOPE, "paths": [review["path"]], "target": target,
            "sources": [], "actor": actor, "created_changes": {}, "review": review}


def admit(jobs, key, material, paths, target, sources, actor, created_changes, review, task, *, selection_id=None, selection_ordinal=None, mcp_scope: McpScope | None = None, requester=None):
    from .jobs import worker

    if material or target or sources or created_changes or paths != [review["path"]] or not key.strip():
        raise WikiError("A checked daily review selects one existing document only", 422)
    request = _request(review, actor, target)
    # Construction is already complete. History precedes account; config stays
    # shared-locked from its final generation check through commit.
    with jobs.store.borrow(), WikiHistory(jobs.store).locked():
        prepare(jobs, review)
        with registry.registered(jobs.user_id, jobs.store, jobs.registration, caller=jobs.caller):
            try:
                result = commit(jobs, key, request, review, task,
                                selection_id=selection_id, selection_ordinal=selection_ordinal,
                                mcp_scope=mcp_scope, requester=requester)
            except sqlite3.DatabaseError as exc:
                raise selections.unavailable() from exc
    if result["status"] == "queued":
        worker.wake(jobs.user_id)
    return result


def admit_batch(jobs, selection_id, ordinals, actor, task, *, mcp_scope=None, requester=None):
    """Admit up to three frozen observations atomically; replay each original key."""
    from .jobs import worker

    if not 1 <= len(ordinals) <= 3 or len(set(ordinals)) != len(ordinals):
        raise WikiError("Choose one to three distinct selection ordinals", 422)
    with jobs.store.borrow(), WikiHistory(jobs.store).locked():
        with jobs.store.connect() as db:
            selections.header(db, selection_id, jobs.registration, jobs.user_id, actor)
            reviews = [selections.item(db, selection_id, ordinal)["review"] for ordinal in ordinals]
        for review in reviews:
            prepare(jobs, review)
        with registry.registered(jobs.user_id, jobs.store, jobs.registration, caller=jobs.caller) as registration:
            with jobs.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                states = []
                for ordinal, review in zip(ordinals, reviews, strict=True):
                    selections.check_item(db, selection_id, ordinal, review, registration, jobs.user_id, actor)
                    request = _request(review, actor)
                    current, previous = _current(jobs, db, review["review_key"], request, review, task, mcp_scope)
                    states.append((ordinal, review, request, current, previous))
                fresh = [row for row in states if row[4] is None]
                job_id = None
                if fresh:
                    request = {"material": "", "scope": DAILY_SCOPE, "paths": [row[3]["path"] for row in fresh],
                               "target": None, "sources": [], "actor": actor, "created_changes": {},
                               "reviews": [row[1] for row in fresh]}
                    tasks.require_submission(task, request, jobs.store, db, mcp_scope, registration, checked=True)
                    key = "wiki-daily-batch-v1:" + digest([row[1]["review_key"] for row in fresh])
                    items = [{"status": "pending", "path": row[3]["path"], "doc": row[3]["id"],
                              "revision": row[3]["revision"], "quality": row[3]["quality"]} for row in fresh]
                    job_id = _insert(jobs, db, key, request, items, task,
                                     aliases=[(row[1]["review_key"], row[2]) for row in fresh])
                selected, results = [], {}
                for ordinal, review, _, _, previous in states:
                    target = previous["id"] if previous else job_id
                    selections.accept(db, target, selection_id, ordinal, review)
                    if target not in results:
                        jobs.subscribe(db, target, requester)
                        results[target] = jobs.get(target, db=db)
                    item = next(item for item in results[target]["items"] if item.get("doc") == review["doc"])
                    selected.append({"ordinal": ordinal, "review_key": review["review_key"],
                                     "job_id": target, "reused": previous is not None, **{key: item[key] for key in (
                        "doc", "path", "status", "revision", "output_revision", "receipt_id") if key in item}})
                result = {"aggregation_scope": "associated_jobs", "jobs": [jobs.summary(job) for job in results.values()], "selected_items": selected}
    if any(job["status"] == "queued" for job in result["jobs"]):
        worker.wake(jobs.user_id)
    return result

"""Finite preparation of old job history; current content proof is not a job."""

import copy
import json

from pydantic import ValidationError

from ..models import TurnOutcome
from . import (
    grants,
    job_events,
    job_execution,
    job_items,
    maintenance_inputs,
    maintenance_selections,
)
from .history import WikiHistory
from .store import WikiError


def previous_items(raw):
    """Only the schema preparation owner converts the admitted 12/13 encoding."""
    try:
        return _previous_items(raw)
    except (ValueError, TypeError, RecursionError) as error:
        raise WikiError("Wiki previous job membership is malformed; durable evidence retained", 503) from error


def _previous_items(raw):
    result = []
    for value in job_items.parse(raw):
        if not isinstance(value, dict) or type(value.get("version", 1)) is not int or value.get("version", 1) != 1:
            raise WikiError("Wiki previous job membership is malformed; durable evidence retained", 503)
        item = copy.deepcopy(value)
        if (not isinstance(item.get("legacy_evidence", {}), dict)
                or not isinstance(item.get("previous_worker_requests", []), list)
                or any(not isinstance(ref, dict) for ref in item.get("previous_worker_requests", []))
                or "worker_request" in item and not isinstance(item["worker_request"], dict)):
            raise WikiError("Wiki previous job membership is malformed; durable evidence retained", 503)
        if "version" not in item and item.get("status") not in job_items.ITEM_STATES:
            item["legacy_evidence"] = item.get("legacy_evidence", {}) | {"status": item.get("status")}
            item["status"] = "unknown"
        legacy = {key: item.pop(key) for key in job_items.LEGACY_FIELDS if key in item}
        if legacy:
            item["legacy_evidence"] = item.get("legacy_evidence", {}) | legacy
            failure = legacy.get("recording_failure")
            if isinstance(failure, dict) and failure:
                prior = item.get("worker_request", {})
                old = failure | {"phase": "unknown"}
                if prior and any(prior.get(key) != failure.get(key) for key in ("attempt", "session_id", "request_id")):
                    item.setdefault("previous_worker_requests", []).append(old)
                else:
                    item["worker_request"] = prior | old
            if legacy.get("recording_failure"):
                item["status"] = "attention"
            if legacy.get("execution_unknown"):
                item["status"] = "unknown"
            elif legacy.get("recording_recovery") or legacy.get("index_recovery"):
                item["status"] = "verifying"
            if failure and not isinstance(failure, dict):
                item["status"] = "unknown"
            if legacy.get("cancelled_before_admission") and not item.get("worker_request"):
                if not isinstance(legacy["cancelled_before_admission"], dict):
                    raise WikiError("Wiki previous cancellation is malformed; durable evidence retained", 503)
                item["worker_request"] = legacy["cancelled_before_admission"] | {
                    "phase": "rejected", "disposition": "not_admitted",
                }
        for reference in [*item.get("previous_worker_requests", []), *([item["worker_request"]] if "worker_request" in item else [])]:
            if not isinstance(reference, dict):
                raise WikiError("Wiki previous job execution is malformed; durable evidence retained", 503)
            reference.setdefault("phase", "unknown")
        item["version"] = 1
        result.append(job_items.normalize(item))
    return result


def old_jobs(db):
    """Classify the original encoding before normalization changes its evidence."""
    result = set()
    for row in db.execute("SELECT id,items FROM jobs"):
        previous_items(row["items"])
        original = job_items.parse(row["items"])
        if original and all("version" not in item or any(key in item for key in job_items.LEGACY_FIELDS) for item in original):
            result.add(row["id"])
    return result


def _reference_closed(reference, job):
    attempt = reference.get("attempt")
    if type(attempt) is not int or not 0 <= attempt <= job["attempt"]:
        return False
    if reference.get("phase") == "rejected":
        if reference.get("disposition") not in {"closed", "not_admitted"} or reference.get("terminal") is not None:
            return False
        if not any(reference.get(key) for key in ("session_id", "request_id", "work_id")):
            return job["status"] == "cancelled" and job["settlement_phase"] == "confirmed" and attempt == 0 and job["session_id"] is None
    if any(not isinstance(reference.get(key), str) or not reference[key] for key in ("session_id", "request_id", "work_id")):
        return False
    if reference["work_id"] != "wiki-" + reference["request_id"]:
        return False
    if reference.get("phase") == "rejected":
        return True
    terminal = reference.get("terminal")
    if (reference.get("phase") != "settled" or reference.get("recording_state") != "committed"
            or reference.get("retired") is not True or not isinstance(terminal, dict)
            or terminal.get("recording_integrity") != "complete"):
        return False
    try:
        outcome = TurnOutcome.model_validate(terminal)
    except ValidationError:
        return False
    return outcome.request_id == reference["request_id"]


def _executions_closed(job):
    seen, sessions, attempts = {}, set(), set()
    for item in job["items"]:
        references = [*item.get("previous_worker_requests", []), *([item["worker_request"]] if item.get("worker_request") else [])]
        if not references:
            return False
        for reference in references:
            if not _reference_closed(reference, job):
                return False
            key = tuple(reference.get(field) for field in ("attempt", "session_id", "request_id"))
            # A shared batch has one execution proof, but retry retains each
            # item's own result inside that reference.
            proof = {field: reference.get(field) for field in (
                "attempt", "session_id", "request_id", "work_id", "phase",
                "recording_state", "disposition", "terminal", "retired",
            )}
            if key in seen and seen[key] != proof:
                return False
            seen[key] = proof
            attempts.add(reference["attempt"])
        current = item.get("worker_request")
        if current and current.get("session_id"):
            sessions.add(current["session_id"])
        if item["status"] == "done":
            if (item.get("index_complete") is not True or not current
                    or job["status"] != "cancelled" and current.get("terminal", {}).get("status") != "success"):
                return False
        elif item["status"] == "failed":
            if not current or current.get("terminal", {}).get("status") not in {"failed", "interrupted"}:
                return False
        elif item["status"] != "excluded":
            if not (job["status"] == "cancelled" and current and current.get("phase") == "rejected"):
                return False
        if item.get("cancellation_effect_error"):
            return False
    return job["attempt"] in attempts and (job["session_id"] is None or job["session_id"] in sessions)


def _effects_closed(db, job):
    references = {tuple(ref.get(key) for key in ("attempt", "session_id", "request_id")): ref
                  for item in job["items"] for ref in [*item.get("previous_worker_requests", []), item.get("worker_request", {})]}
    for row in db.execute("""SELECT r.*,c.status AS change_status,t.resolved_at,
        coalesce(p.indexed,CASE WHEN reservation.state='bound' THEN 1 END) AS indexed
        FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id
        LEFT JOIN change_retention t ON t.change_id=r.change_id LEFT JOIN job_publications p ON p.change_id=r.change_id
        LEFT JOIN wiki_path_reservations reservation ON reservation.change_id=r.change_id AND reservation.job_id=r.job_id
          AND reservation.document_id=r.doc_id AND reservation.output_revision=r.output_revision
          AND reservation.purpose=CASE WHEN r.operation='ingest' THEN 'framework_material' ELSE 'requested_knowledge_page' END
        WHERE r.job_id=?""", (job["id"],)):
        if row["change_id"] and not (row["change_status"] == "applied" and row["indexed"] == 1
                                     or row["change_status"] == "rejected" and row["resolved_at"] is not None):
            return False
        try:
            details = json.loads(row["details"])
        except (ValueError, TypeError):
            return False
        if not isinstance(details, dict):
            return False
        if "execution" in details and details["execution"] is not None:
            execution = details["execution"]
            if (not isinstance(execution, dict) or type(execution.get("attempt")) is not int
                    or any(not isinstance(execution.get(key), str) or not execution[key] for key in ("session_id", "request_id"))
                    or tuple(execution.get(key) for key in ("attempt", "session_id", "request_id")) not in references):
                return False
            original = references[tuple(execution[key] for key in ("attempt", "session_id", "request_id"))]
            if original.get("phase") == "rejected":
                return False
    for item in job["items"]:
        if item["status"] == "done" and not db.execute("""SELECT 1 FROM operation_receipts
            WHERE job_id=? AND item_doc=? AND path IS ? AND output_revision IS ? AND item_match_sequence=? LIMIT 1""",
            (job["id"], item.get("doc"), item.get("output"), item.get("output_revision"), job["admission_sequence"])).fetchone():
            return False
        for candidate in item.get("candidate_changes", []):
            row = db.execute("SELECT resolved_at FROM change_retention WHERE change_id=?", (candidate,)).fetchone()
            if row is None or row[0] is None:
                return False
    if db.execute("""SELECT 1 FROM job_publications p LEFT JOIN changes c ON c.id=p.change_id
        LEFT JOIN change_retention t ON t.change_id=p.change_id WHERE p.job_id=?
        AND (c.id IS NULL OR NOT (c.status='applied' AND p.indexed=1
            OR c.status='rejected' AND t.resolved_at IS NOT NULL)) LIMIT 1""", (job["id"],)).fetchone():
        return False
    if db.execute("""SELECT 1 FROM changes c LEFT JOIN change_retention t ON t.change_id=c.id
        WHERE c.reason=? AND (c.status NOT IN ('applied','rejected')
            OR c.status='rejected' AND t.resolved_at IS NULL) LIMIT 1""", ("Ingest job " + job["id"],)).fetchone():
        return False
    for row in db.execute("""SELECT p.operation,t.resolved_at FROM change_proposals p
        LEFT JOIN change_retention t ON t.change_id=p.change_id
        WHERE json_valid(p.operation) AND json_extract(p.operation,'$.job_id')=?""", (job["id"],)):
        if row["resolved_at"] is None:
            return False
    if db.execute("""SELECT 1 FROM wiki_path_reservations r LEFT JOIN changes c ON c.id=r.change_id
        WHERE r.job_id=? AND (r.state='prepared' OR r.state='bound' AND (coalesce(c.status,'')<>'applied'
            OR r.purpose NOT IN ('framework_material','requested_knowledge_page'))) LIMIT 1""", (job["id"],)).fetchone():
        return False
    return True


def _eligible(db, store, job):
    if job["status"] not in {"completed", "completed_with_attention", "failed", "cancelled"}:
        return False
    if job["settlement_phase"] != ("confirmed" if job["status"] == "cancelled" else "none"):
        return False
    if not _executions_closed(job) or not _effects_closed(db, job):
        return False
    if db.execute("""SELECT 1 FROM wiki_job_outbox WHERE job_id=? AND (
        json_extract(body,'$.job_id') IS NOT job_id OR json_extract(body,'$.state_version') IS NOT state_version)
        LIMIT 1""", (job["id"],)).fetchone():
        return False
    if not db.execute("""SELECT 1 FROM wiki_job_outbox o JOIN wiki_job_state s
        ON s.job_id=o.job_id AND s.state_version=o.state_version WHERE o.job_id=?
        AND json_extract(o.body,'$.status') IS ? AND json_extract(o.body,'$.attempt') IS s.attempt
        AND json_extract(o.body,'$.settlement_phase') IS s.settlement_phase""", (job["id"], job["status"])).fetchone():
        return False
    if db.execute("""SELECT 1 FROM wiki_job_deliveries d JOIN wiki_job_outbox o ON o.event_id=d.event_id
        WHERE o.job_id=? AND (d.acknowledged<>1 OR d.outcome NOT IN ('delivered','unavailable')) LIMIT 1""", (job["id"],)).fetchone():
        return False
    try:
        job_execution.require_retired(store, job)
    except WikiError as error:
        if error.status == 409:
            return False
        raise
    return True


def retire(db, store, original_old):
    """Remove only proved old jobs in the original format-preparation transaction."""
    selected = []
    for identifier in original_old:
        row = db.execute("""SELECT j.*,s.attempt,s.settlement_phase FROM jobs j
            JOIN wiki_job_state s ON s.job_id=j.id WHERE j.id=?""", (identifier,)).fetchone()
        job = dict(row) | {"items": job_items.decode(row["items"])}
        if _eligible(db, store, job):
            selected.append(job)
    if not selected:
        return
    for job in selected:
        WikiHistory(store).preserve_candidate_owners(db, job)
    identifiers = [job["id"] for job in selected]
    placeholders = ",".join("?" for _ in identifiers)
    # The existing owner rebuilds live-job projections. Qualified receipt
    # facts survive this finite handoff; ordinary DELETE still revokes them.
    maintenance_inputs.discard_derived(db)
    guards = {name: job_events.TRIGGERS[name] for name in (
        "wiki_job_state_delete_guard", "wiki_job_outbox_delete_guard",
        "wiki_job_subscribers_delete_guard", "wiki_job_deliveries_delete_guard")}
    guards |= {table + "_delete": grants.TRIGGERS[table + "_delete"] for table in grants.TABLES}
    guards["maintenance_evidence_keep"] = maintenance_selections.TRIGGERS["maintenance_evidence_keep"]
    for name in guards:
        db.execute('DROP TRIGGER "' + name + '"')
    db.execute("DELETE FROM wiki_job_deliveries WHERE event_id IN (SELECT event_id FROM wiki_job_outbox WHERE job_id IN ("
               + placeholders + "))", identifiers)
    for table in ("wiki_job_outbox", "wiki_job_subscribers", "wiki_job_state", "wiki_job_controls", "job_requests",
                  "job_selection_evidence", "wiki_grant_readmissions", "wiki_grant_documents", "wiki_job_grants"):
        db.execute("DELETE FROM " + table + " WHERE job_id IN (" + placeholders + ")", identifiers)
    db.execute("""DELETE FROM wiki_path_reservations WHERE state IN ('reserved','released') AND job_id IN (""" + placeholders + ")"
               + " AND id NOT IN (SELECT reservation_id FROM wiki_grant_documents WHERE reservation_id IS NOT NULL)", identifiers)
    db.execute("DELETE FROM jobs WHERE id IN (" + placeholders + ")", identifiers)
    for declaration in guards.values():
        db.execute(declaration)
    maintenance_inputs.initialize(db)

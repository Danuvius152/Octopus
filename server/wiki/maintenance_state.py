"""Read-only maintenance comparisons. Observations never grant write authority."""

import base64
import hashlib
import json
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from . import catalog
from .formats import is_markdown_maintenance
from .links import note_name
from .read_response import COMPACT_READ_MAX_BYTES, model_result_bytes
from .store import WikiError

PURPOSE = "daily-review-v1"
DAILY_SCOPE = "按当前用途复核当前人工版本，只修确有必要且获准的派生信息；保留原文、未知字段及删除结果，短记录不强套文章结构；相关旧文只读，不复刻敏感值。"
# Legacy organize records use 'save' with a real worker publication. Ordinary
# saves have no owning job/item and can never pass the settled-output checks.
MAINTENANCE_OPERATIONS = frozenset({"save", "organize", "normalize", "publish", "restructure", "patch", "confirm_sources", "no_change"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def exact_document(db, selector):
    """An explicit identity never falls back to another same-spelling note."""
    alias = db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (selector.casefold(),)).fetchone()
    identifier = alias[0] if alias else selector
    identity = db.execute("SELECT id FROM document_identities WHERE id=?", (identifier,)).fetchone()
    if identity is None:
        identity = db.execute("SELECT id FROM document_identities WHERE path=?", (selector,)).fetchone()
    if identity:
        row = db.execute("SELECT id,path,revision FROM documents WHERE id=?", (identity[0],)).fetchone()
        return row if row is not None and is_markdown_maintenance(row["path"]) else None
    return None


def selected_document(db, selector):
    row = exact_document(db, selector)
    if row is not None:
        return row
    # Retired identities and explicit paths must not become fuzzy title hits.
    if "/" in selector or selector.casefold().startswith("urn:uuid:") or re.fullmatch(r"[0-9a-fA-F]{32}", selector):
        return None
    matches = [r for r in db.execute("SELECT id,path,revision,title FROM documents")
               if is_markdown_maintenance(r["path"])
               and selector in {PurePosixPath(r["path"]).name, r["title"]}]
    if len(matches) > 1:
        raise WikiError("The selected name is ambiguous; use its exact current path", 403)
    return matches[0] if matches else None


class MaintenanceSnapshot:
    """One SQLite read transaction supplies generation, counts and page data."""

    def __init__(self, store, registration, user_id, db, actor="human"):
        self.store, self.registration, self.user_id, self.db = store, registration, user_id, db
        self.actor = actor
        index = dict(db.execute("SELECT * FROM index_state WHERE singleton=1").fetchone())
        stream = dict(db.execute("SELECT * FROM filesystem_state WHERE singleton=1").fetchone())
        errors = db.execute("SELECT count(*) FROM documents WHERE quality='error'").fetchone()[0]
        pending = max(int(store.events.needs_reconciliation), stream["received_seq"] - stream["applied_seq"])
        unavailable = bool(index["error"] or stream["uncertain"] or stream["armed_epoch"])
        self.index = {
            "index_epoch": index["epoch"], "pending_changes": pending,
            "index_error_count": errors + int(unavailable),
            "index_status": "unavailable" if unavailable else "updating" if pending else "partial" if errors else "ready",
        }
        self.ready = self.index["index_status"] == "ready"
        self.documents = {r["id"]: dict(r) for r in db.execute("SELECT id,path,revision FROM documents")
                          if is_markdown_maintenance(r["path"])}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.jobs = {}
        jobs = [dict(r) for r in db.execute("SELECT rowid AS sequence,id,status,items,updated_at FROM jobs ORDER BY rowid")] if "jobs" in tables else []
        for row in jobs:
            self.jobs[row["id"]] = row | {"items": json.loads(row["items"])}
        self.receipts = [dict(r) for r in db.execute("""SELECT r.rowid AS sequence,r.id,r.job_id,r.item_doc,r.doc_id,
            r.path,r.input_revision,r.output_revision,r.operation,r.change_id,c.status AS change_status,p.indexed
            FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id
            LEFT JOIN job_publications p ON p.change_id=r.change_id ORDER BY r.rowid DESC""")] if "operation_receipts" in tables else []
        conflicts = [dict(r) for r in db.execute("""SELECT p.change_id,r.document_id,r.resolved_at FROM change_proposals p
            JOIN change_retention r ON r.change_id=p.change_id WHERE r.resolved_at IS NULL""")]
        self.conflicts = {row["document_id"] for row in conflicts}
        self.accepted = {}
        for receipt in self.receipts:
            if self.accepts(receipt):
                self.accepted.setdefault(receipt["doc_id"], []).append(receipt)
        # Raw job items are only hashed in memory. They are never projected.
        self.generation = digest([user_id, registration, actor, self.index, sorted(self.documents.values(), key=lambda r: r["id"]), self.receipts, jobs, conflicts])

    def accepts(self, receipt):
        if receipt["operation"] not in MAINTENANCE_OPERATIONS or not receipt["job_id"]:
            return False
        if receipt["change_id"] and (receipt["change_status"] != "applied" or receipt["indexed"] != 1):
            return False
        job = self.jobs.get(receipt["job_id"])
        if not job:
            return False
        for item in job["items"]:
            if (item.get("doc") != receipt["item_doc"] or item.get("status") != "done"
                    or item.get("index_complete") is not True or item.get("recording_failure")
                    or item.get("index_recovery") or item.get("output_revision") != receipt["output_revision"]
                    or item.get("output") != receipt["path"]):
                continue
            if item.get("receipt_id") and item["receipt_id"] != receipt["id"]:
                continue
            if receipt["change_id"] and item.get("output_change_id") != receipt["change_id"]:
                continue
            return True
        return False

    def attempts(self, identifier, baseline):
        result = []
        related = {r["job_id"]: r["item_doc"] for r in self.receipts if r["doc_id"] == identifier and r["job_id"]}
        baseline_job = self.jobs.get(baseline["job_id"], {}) if baseline else {}
        for job in reversed(list(self.jobs.values())):
            for item in job["items"]:
                related_ids = {identifier}
                if related.get(job["id"]):
                    related_ids.add(related[job["id"]])
                if item.get("doc") not in related_ids:
                    continue
                code = None
                if item.get("recording_failure"):
                    code = "recording_unconfirmed"
                elif item.get("index_recovery"):
                    code = "index_recovery"
                elif item.get("status") != "done" and job["status"] in {"queued", "running"}:
                    code = "active"
                elif (item.get("status") != "done" and job["status"] in {"interrupted", "failed", "attention", "completed_with_attention"}
                      and job["sequence"] >= baseline_job.get("sequence", 0)):
                    code = "execution_unfinished"
                elif item.get("status") in {"attention", "failed"} and job["status"] != "cancelled" and job["sequence"] >= baseline_job.get("sequence", 0):
                    code = "attention"
                if code:
                    result.append({"job_id": job["id"], "status": job["status"], "recovery": code})
        if identifier in self.conflicts:
            result.append({"recovery": "conflict"})
        for receipt in self.receipts:
            if (receipt["doc_id"] != identifier or not receipt["job_id"]
                    or receipt["operation"] not in MAINTENANCE_OPERATIONS
                    or not receipt["change_id"] or baseline and receipt["sequence"] <= baseline["sequence"]):
                continue
            if receipt["change_status"] != "applied" or receipt["indexed"] != 1:
                result.append({"job_id": receipt["job_id"], "recovery": "publication_unconfirmed" if receipt["change_status"] != "applied" else "index_recovery"})
        return result

    def dependencies(self, identifier):
        refs = catalog.dependencies(self.db, identifier)
        values = [(r["doc"], r["current_revision"]) for r in refs]
        warnings = {status: sum(r["status"] == status for r in refs) for status in ("changed", "missing", "version_unknown")}
        return digest(values), {k: v for k, v in warnings.items() if v}

    def impact(self, identifier):
        current = self.documents[identifier]
        old_paths = {r["path"] for r in self.accepted.get(identifier, []) if r["path"] != current["path"]}
        if not old_paths:
            return []
        result = []
        for edge in self.db.execute("""SELECT e.*,d.path AS source_path FROM wiki_edges e JOIN documents d ON d.id=e.source_doc
                WHERE e.relation IN ('wikilink','markdown_link') ORDER BY d.path,e.source_doc,e.ordinal"""):
            try:
                url = urlsplit(edge["resource"])
            except ValueError:
                continue
            if url.scheme or url.netloc:
                continue
            path = unquote(url.path)
            candidates = {posixpath.normpath(posixpath.join(str(PurePosixPath(edge["source_path"]).parent), path))}
            if edge["relation"] == "wikilink":
                candidates.add(path.lstrip("/"))
            matched = next((old for old in sorted(old_paths) if any(
                candidate + suffix == old for candidate in candidates for suffix in ("", ".md", ".markdown"))), None)
            if matched is None and edge["relation"] == "wikilink" and "/" not in path:
                others = [r[0] for r in catalog.name_matches(self.db, note_name(path)) if r[0] != identifier]
                if not others:
                    matched = next((old for old in sorted(old_paths) if note_name(old) == note_name(path)), None)
            if matched is not None:
                # Never echo raw resource/query/fragment/label/location values.
                result.append({"source_doc": edge["source_doc"], "source_path": edge["source_path"],
                               "matched_path": matched, "relation": edge["relation"], "status": edge["status"]})
        return result

    def project(self, identifier):
        current = self.documents[identifier]
        accepted = self.accepted.get(identifier, [])
        baseline = accepted[0] if accepted else None
        maintenance_evidence = any(r["doc_id"] == identifier and r["job_id"] and r["operation"] in MAINTENANCE_OPERATIONS for r in self.receipts)
        status = "unchanged" if baseline and baseline["output_revision"] == current["revision"] else "changed" if baseline else "unknown" if maintenance_evidence else "unreviewed"
        dependency_generation, warnings = self.dependencies(identifier)
        attempts = self.attempts(identifier, baseline)
        observation = {"doc": identifier, "path": current["path"], "revision": current["revision"],
                       "baseline_receipt_id": baseline["id"] if baseline else None, "dependency_generation": dependency_generation}
        observation["review_key"] = "wiki-daily-v1:" + digest([self.user_id, self.registration, self.actor, PURPOSE, observation])
        return {"current": dict(current), "baseline": {k: baseline[k] for k in ("id", "path", "output_revision", "operation")} if baseline else None,
                "status": status, "path_changed": bool(baseline and baseline["path"] != current["path"]),
                "source_warnings": warnings, "attempts": attempts[:3], "attempt_count": len(attempts),
                "actionable": self.ready and not attempts and (status in {"changed", "unreviewed"} or status == "unchanged" and bool(warnings)),
                "historical_path_links": len(self.impact(identifier)), "review": observation}


def _cursor(value, filters, generation):
    if value is None:
        return 0
    try:
        if len(value) > 2048:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()))
        if decoded["filters"] != filters or type(decoded["offset"]) is not int or decoded["offset"] < 0:
            raise ValueError()
        observed_generation = decoded["generation"]
        if not isinstance(observed_generation, str) or not re.fullmatch(r"[0-9a-f]{64}", observed_generation):
            raise ValueError()
    except (ValueError, KeyError, TypeError) as exc:
        raise WikiError("Invalid maintenance cursor or changed filters", 422) from exc
    if observed_generation != generation:
        raise WikiError("Maintenance state changed; restart this observation", 409)
    return decoded["offset"]


def status(store, registration, user_id, *, doc=None, directory="Library", view="state", limit=20, cursor=None, actor="human"):
    if view not in {"state", "links"} or not 1 <= limit <= 50 or (view == "links" and doc is None):
        raise WikiError("Invalid maintenance status selection", 422)
    scope = PurePosixPath(directory)
    if not directory or scope.is_absolute() or ".." in scope.parts or "." in scope.parts or "\\" in directory:
        raise WikiError("Use a literal vault-relative directory", 422)
    directory = scope.as_posix()
    with store.operation(), store.connect() as db:
        db.execute("BEGIN")
        snapshot = MaintenanceSnapshot(store, registration, user_id, db, actor)
        filters = digest({"doc": doc, "directory": directory, "view": view, "contract": PURPOSE})
        offset = _cursor(cursor, filters, snapshot.generation)
        response_scope = "selected_document" if doc is not None else directory
        if not snapshot.ready:
            return {"ready": False, **snapshot.index, "items": [], "total": None, "next_cursor": None,
                    "generation": snapshot.generation, "scope": response_scope}
        if doc is not None:
            row = exact_document(db, doc)
            if row is None:
                raise WikiError("Current Markdown document not found", 404)
            identifier = row["id"]
            values = snapshot.impact(identifier) if view == "links" else [snapshot.project(identifier)]
        else:
            values = []
            for row in sorted(snapshot.documents.values(), key=lambda r: (r["path"], r["id"])):
                if not row["path"].startswith(directory.rstrip("/") + "/"):
                    continue
                projected = snapshot.project(row["id"])
                if projected["status"] != "unchanged" or projected["path_changed"] or projected["source_warnings"] or projected["attempt_count"]:
                    values.append(projected)
        selected = values[offset:offset + limit]

        def response():
            end = offset + len(selected)
            next_cursor = base64.urlsafe_b64encode(json.dumps({"generation": snapshot.generation, "filters": filters, "offset": end}, separators=(",", ":")).encode()).decode() if end < len(values) else None
            return {"ready": True, **snapshot.index, "items": selected, "total": len(values), "next_cursor": next_cursor,
                    "generation": snapshot.generation, "scope": response_scope, "view": view,
                    "coverage": "Indexed current state and known accepted receipt paths; not a before-move snapshot or write grant."}

        result = response()
        while model_result_bytes(result) > COMPACT_READ_MAX_BYTES:
            if len(selected) <= 1:
                raise WikiError("Maintenance metadata exceeds its response limit", 422)
            selected.pop()
            result = response()
        return result


def check_submission(jobs, key, request, review, task):
    """Called inside the existing admission lock, separately from legacy keys."""
    from .registry import registry
    from .tasks import tasks

    if (request["material"] or request["target"] or request["sources"] or request["created_changes"]
            or request["paths"] != [review["path"]]):
        raise WikiError("A checked daily review selects one existing document only", 422)
    if task is not None:
        if not any(active is task for active in tuple(tasks.active.values())) or task.mode not in {"auto", "maintain"}:
            raise WikiError("The maintenance entry turn is no longer authorized", 403)
    if jobs.registration != registry.config(jobs.user_id):
        raise WikiError("The Wiki registration changed before admission", 409)
    tasks.require_selection(task, request["paths"], jobs.store, checked=True)
    observation = {name: value for name, value in review.items() if name != "review_key"}
    expected = "wiki-daily-v1:" + digest([jobs.user_id, jobs.registration, request["actor"], PURPOSE, observation])
    if key != review["review_key"] or key != expected:
        raise WikiError("Use the matching current daily review observation and key", 409)
    previous = jobs.previous_request(key, request)
    try:
        current = jobs.store.read(review["doc"], length=1)
    except WikiError as error:
        raise WikiError("The observed document identity is no longer current", 409) from error
    if current["id"] != review["doc"]:
        raise WikiError("The observed document identity changed", 409)
    if not is_markdown_maintenance(current["path"]):
        raise WikiError("Checked maintenance requires a current Markdown note", 422)
    with jobs.store.connect() as db:
        db.execute("BEGIN")
        snapshot = MaintenanceSnapshot(jobs.store, jobs.registration, jobs.user_id, db, request["actor"])
        projected = snapshot.project(current["id"])
        if previous:
            # A successful operation changes its own revision/baseline. Replay
            # checks its real output, never the new-admission predicate below.
            accepted = any(r["job_id"] == previous["id"] and r["output_revision"] == current["revision"]
                           for r in snapshot.accepted.get(current["id"], []))
            if accepted:
                if not snapshot.ready or projected["source_warnings"] or projected["attempt_count"]:
                    raise WikiError("The prior output's current index or direct dependencies need review", 409)
                return previous
            if previous["status"] in {"queued", "running", "attention", "failed", "cancelled"}:
                landed = any(r["job_id"] == previous["id"] and r["doc_id"] == current["id"]
                             and r["output_revision"] == current["revision"] and r["change_status"] == "applied"
                             for r in snapshot.receipts)
                if current["revision"] == review["revision"] or landed:
                    return previous  # Retained work, not a new success claim.
            raise WikiError("The historical request does not certify the current document", 409)
        if not snapshot.ready:
            raise WikiError("The current index must settle before maintenance admission", 503)
        if projected["review"] != review or current["path"] != review["path"]:
            raise WikiError("Document or maintenance baseline changed; observe it again", 409)
        if not projected["actionable"]:
            raise WikiError("This document is unchanged, unverified or already has retained work", 409)
    return None

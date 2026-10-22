"""Batch facts and token-checked maintenance summaries, without actor grants."""

from collections import defaultdict

from . import maintenance_dependencies, maintenance_inputs, source_bindings
from .formats import is_markdown_maintenance
from .index_state import summary as index_summary
from .maintenance_contract import MAINTENANCE_OPERATIONS
from .store import WikiError

BATCH_SIZE = 512


def bounds(directory):
    lower = directory.rstrip("/") + "/"
    return lower, lower[:-1] + "0"


def dirty_ids(db, *, identifiers=None, directory="Library", limit=None):
    if identifiers is not None:
        if not identifiers:
            return []
        parameters = list(identifiers)
        sql = "SELECT doc_id FROM maintenance_dirty WHERE doc_id IN (" + ",".join("?" for _ in parameters) + ")"
    else:
        parameters = list(bounds(directory))
        # Old/current paths are maintained in the same fact-mutation TX. A
        # dirty note elsewhere cannot force a scan of this directory's notes.
        sql = """SELECT DISTINCT p.doc_id FROM maintenance_dirty_paths p
                 JOIN maintenance_dirty d ON d.doc_id=p.doc_id WHERE p.path>=? AND p.path<?"""
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    return [row[0] for row in db.execute(sql, parameters)]


def readiness(db, store):
    maintenance_inputs.require(db)
    source_bindings.validate(db)
    index = index_summary(db, store.events.needs_reconciliation)
    return {name: index[name] for name in ("index_epoch", "pending_changes", "index_error_count", "index_status")}


class Batch:
    """One read snapshot, indexed exact identities, and no raw job history."""

    def __init__(self, db, identifiers):
        if not db.in_transaction:
            raise ValueError("Maintenance facts require an explicit snapshot")
        self.identifiers = list(identifiers)
        with_jobs = maintenance_inputs.require(db)
        if not maintenance_inputs.ready(db, self.identifiers):
            raise WikiError("Maintenance job normalization is updating", 503)
        db.execute("CREATE TEMP TABLE IF NOT EXISTS maintenance_requested_documents(doc_id TEXT PRIMARY KEY) WITHOUT ROWID")
        db.execute("DELETE FROM maintenance_requested_documents")
        db.executemany("INSERT OR IGNORE INTO maintenance_requested_documents VALUES(?)", [(value,) for value in self.identifiers])
        self.documents = {row["id"]: dict(row) for row in db.execute("""SELECT d.id,d.path,d.revision
            FROM maintenance_requested_documents wanted JOIN documents d ON d.id=wanted.doc_id""")
                          if is_markdown_maintenance(row["path"])}
        self.receipts = [dict(row) for row in db.execute("""SELECT r.rowid AS sequence,r.id,r.job_id,r.item_doc,r.doc_id,
            r.path,r.input_revision,r.output_revision,r.operation,r.change_id,c.status AS change_status,
            coalesce(p.indexed,CASE WHEN reservation.state='bound' THEN 1 END) AS indexed
            FROM maintenance_requested_documents wanted CROSS JOIN operation_receipts r
            ON r.doc_id=wanted.doc_id LEFT JOIN changes c ON c.id=r.change_id
            LEFT JOIN job_publications p ON p.change_id=r.change_id
            LEFT JOIN wiki_path_reservations reservation ON reservation.change_id=r.change_id
              AND reservation.job_id=r.job_id AND reservation.document_id=r.doc_id
              AND reservation.output_revision=r.output_revision AND reservation.purpose='requested_knowledge_page'""")]
        self.receipts_by_doc = defaultdict(list)
        self.related_inputs = defaultdict(set)
        for receipt in self.receipts:
            self.receipts_by_doc[receipt["doc_id"]].append(receipt)
            if receipt["job_id"] and receipt["item_doc"]:
                self.related_inputs[receipt["doc_id"]].add((receipt["job_id"], receipt["item_doc"]))
        for values in self.receipts_by_doc.values():
            values.sort(key=lambda row: row["sequence"], reverse=True)
        for row in db.execute("""SELECT r.document_id,p.job_id,p.source_doc
            FROM maintenance_requested_documents wanted CROSS JOIN change_retention r
            ON r.document_id=wanted.doc_id JOIN job_publications p ON p.change_id=r.change_id"""):
            self.related_inputs[row["document_id"]].add((row["job_id"], row["source_doc"]))
        self.conflicts = {row[0] for row in db.execute("""SELECT r.document_id
            FROM maintenance_requested_documents wanted CROSS JOIN change_retention r
            ON r.document_id=wanted.doc_id JOIN change_proposals p ON p.change_id=r.change_id
            WHERE r.resolved_at IS NULL""")}
        self.jobs, self.items = {}, []
        if with_jobs:
            related = """SELECT m.job_id FROM maintenance_requested_documents wanted
                         JOIN maintenance_job_members m ON m.doc_id=wanted.doc_id"""
            self.jobs = {row["id"]: dict(row) for row in db.execute(
                "SELECT j.rowid AS sequence,j.id,j.status FROM jobs j WHERE j.id IN (" + related + ")")}
            # Only this batch's direct inputs and exact receipt/publication
            # inputs can affect its summaries. Loading every item of a shared
            # job here repeats that whole job once per intersecting batch.
            db.execute("""CREATE TEMP TABLE IF NOT EXISTS maintenance_requested_inputs(
                job_id TEXT,item_doc TEXT,PRIMARY KEY(job_id,item_doc)) WITHOUT ROWID""")
            db.execute("DELETE FROM maintenance_requested_inputs")
            pairs = {pair for values in self.related_inputs.values() for pair in values if pair[0] and pair[1]}
            db.executemany("INSERT INTO maintenance_requested_inputs VALUES(?,?)", sorted(pairs))
            self.items = [dict(row) for row in db.execute("""SELECT i.*
                FROM maintenance_requested_documents wanted CROSS JOIN maintenance_job_items i
                ON i.item_doc=wanted.doc_id UNION SELECT i.*
                FROM maintenance_requested_inputs wanted CROSS JOIN maintenance_job_items i
                ON i.item_doc=wanted.item_doc AND i.job_id=wanted.job_id""")]
        self.items_by_doc, self.items_by_job_doc = defaultdict(list), defaultdict(list)
        ordinary, changes = set(), set()
        for item in self.items:
            self.items_by_doc[item["item_doc"]].append(item)
            self.items_by_job_doc[item["job_id"], item["item_doc"]].append(item)
            if (item["status"] != "done" or not item["index_complete"] or item["recording_failure"]
                    or item["index_recovery"] or not item["output_revision_valid"]
                    or item["receipt_required"] and item["receipt_id"] is None):
                continue
            receipt_key = item["receipt_id"] if item["receipt_required"] else None
            key = (item["job_id"], item["item_doc"], item["output_path"], item["output_revision"], receipt_key)
            ordinary.add(key)
            changes.add((*key, item["output_change_id"]))
        self.accepted = defaultdict(list)
        for receipt in self.receipts:
            if receipt["operation"] not in MAINTENANCE_OPERATIONS or receipt["job_id"] not in self.jobs:
                continue
            change = receipt["change_id"]
            if change and (receipt["change_status"] != "applied" or receipt["indexed"] != 1):
                continue
            prefix = (receipt["job_id"], receipt["item_doc"], receipt["path"], receipt["output_revision"])
            keys = [(*prefix, identifier, change) if change else (*prefix, identifier)
                    for identifier in (None, receipt["id"])]
            if any(key in (changes if change else ordinary) for key in keys):
                self.accepted[receipt["doc_id"]].append(receipt)
        for values in self.accepted.values():
            values.sort(key=lambda row: row["sequence"], reverse=True)
        self.dependencies = maintenance_dependencies.read(db, self.documents)

    def attempts(self, identifier, baseline):
        items = {(item["job_id"], item["ordinal"]): item for item in self.items_by_doc.get(identifier, ())}
        for key in self.related_inputs.get(identifier, ()):
            for item in self.items_by_job_doc.get(key, ()):
                items[item["job_id"], item["ordinal"]] = item
        ordered = sorted(items.values(), key=lambda item: (-self.jobs[item["job_id"]]["sequence"], item["ordinal"]))
        cutoff = self.jobs[baseline["job_id"]]["sequence"] if baseline else 0
        result = []
        for item in ordered:
            job = self.jobs[item["job_id"]]
            code = None
            if item["recording_failure"]:
                code = "recording_unconfirmed"
            elif item["index_recovery"]:
                code = "index_recovery"
            elif item["status"] != "done" and job["status"] in {"queued", "running"}:
                code = "active"
            elif (item["status"] != "done" and job["status"] in {"interrupted", "failed", "attention", "completed_with_attention"}
                  and job["sequence"] >= cutoff):
                code = "execution_unfinished"
            elif item["status"] in {"attention", "failed"} and job["status"] != "cancelled" and job["sequence"] >= cutoff:
                code = "attention"
            if code:
                result.append({"job_id": job["id"], "status": job["status"], "recovery": code})
        if identifier in self.conflicts:
            result.append({"recovery": "conflict"})
        for receipt in self.receipts_by_doc.get(identifier, ()):
            if (not receipt["job_id"] or receipt["operation"] not in MAINTENANCE_OPERATIONS or not receipt["change_id"]
                    or baseline and receipt["sequence"] <= baseline["sequence"]):
                continue
            if receipt["change_status"] != "applied" or receipt["indexed"] != 1:
                result.append({"job_id": receipt["job_id"], "recovery": "publication_unconfirmed" if receipt["change_status"] != "applied" else "index_recovery"})
        return result

    def calculate(self):
        """Only detached scalar maps are used; no DB work while calculating."""
        result: dict[str, dict[str, object] | None] = {}
        for identifier in self.identifiers:
            current = self.documents.get(identifier)
            if current is None:
                result[identifier] = None
                continue
            accepted = self.accepted.get(identifier, [])
            baseline = accepted[0] if accepted else None
            evidence = any(row["job_id"] and row["operation"] in MAINTENANCE_OPERATIONS
                           for row in self.receipts_by_doc.get(identifier, ()))
            status = "unchanged" if baseline and baseline["output_revision"] == current["revision"] else "changed" if baseline else "unknown" if evidence else "unreviewed"
            generation, warnings = self.dependencies[identifier]
            attempts = self.attempts(identifier, baseline)
            path_changed = bool(baseline and baseline["path"] != current["path"])
            stored = {"current": current, "baseline": {key: baseline[key] for key in ("id", "path", "output_revision", "operation")} if baseline else None,
                      "status": status, "path_changed": path_changed, "source_warnings": warnings,
                      "attempts": attempts[:3], "attempt_count": len(attempts),
                      "actionable": not attempts and (status in {"changed", "unreviewed"} or status == "unchanged" and bool(warnings)),
                      "dependency_generation": generation}
            result[identifier] = {"path": current["path"], "status": status,
                                  "candidate": status != "unchanged" or path_changed or bool(warnings) or bool(attempts),
                                  "summary": stored, "accepted_paths": [row["path"] for row in accepted]}
        return result


def settle(store, *, doc=None, directory="Library", retries=3):
    """Finite rounds of dirty batches; continuous invalidation stays updating."""
    from .maintenance_state import exact_document

    for _ in range(retries):
        with store.connect() as db:
            db.execute("BEGIN")
            if readiness(db, store)["index_status"] not in {"ready", "partial"}:
                return False
            selected = exact_document(db, doc) if doc is not None else None
            identifiers = [selected["id"]] if selected is not None else [] if doc is not None else None
            dirty = dirty_ids(db, identifiers=identifiers, directory=directory)
        if not dirty:
            return True
        for first in range(0, len(dirty), BATCH_SIZE):
            chosen = dirty[first:first + BATCH_SIZE]
            if not maintenance_inputs.normalize(store, chosen, retries=1):
                continue
            with store.connect() as db:
                db.execute("BEGIN")
                captured = maintenance_inputs.capture_documents(db, chosen)
                if captured is None or not captured["tokens"]:
                    continue
                batch = Batch(db, captured["tokens"])
            summaries = batch.calculate()
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                maintenance_inputs.publish_documents(db, captured, summaries)
    return False

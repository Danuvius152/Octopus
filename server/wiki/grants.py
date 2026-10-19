"""Durable exact write grants; task text and tool arguments never issue one."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

from . import maintenance_inputs, protocols
from .formats import is_markdown_maintenance
from .store import WikiError, asset_reason, revision

TABLES = {
    "wiki_job_grants": """CREATE TABLE wiki_job_grants(
        job_id TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL CHECK(version=2),
        vault_id TEXT NOT NULL,registration_generation TEXT NOT NULL,
        creation_purpose TEXT CHECK(creation_purpose IS NULL OR creation_purpose='requested_knowledge_page'),
        policy TEXT NOT NULL,created_at TEXT NOT NULL)""",
    "wiki_grant_documents": """CREATE TABLE wiki_grant_documents(
        job_id TEXT NOT NULL,doc_id TEXT NOT NULL,path_at_admission TEXT NOT NULL,
        observed_revision TEXT NOT NULL,origin TEXT NOT NULL CHECK(origin IN ('selected','material','created')),
        reservation_id TEXT,PRIMARY KEY(job_id,doc_id))""",
    "wiki_path_reservations": """CREATE TABLE wiki_path_reservations(
        id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,path TEXT NOT NULL,path_key TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose IN ('framework_material','requested_knowledge_page')),
        state TEXT NOT NULL CHECK(state IN ('reserved','prepared','blocked','bound','released')),
        document_id TEXT,change_id TEXT,output_revision TEXT,
        UNIQUE(job_id,path),UNIQUE(change_id),
        CHECK((state IN ('reserved','released') AND document_id IS NULL AND change_id IS NULL AND output_revision IS NULL)
          OR (state IN ('prepared','blocked','bound') AND document_id IS NOT NULL AND change_id IS NOT NULL AND output_revision IS NOT NULL)))""",
    "wiki_grant_readmissions": """CREATE TABLE wiki_grant_readmissions(
        job_id TEXT NOT NULL,registration_generation TEXT NOT NULL,request_hash TEXT NOT NULL,
        request TEXT NOT NULL,previous_error TEXT,created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,registration_generation))""",
}
INDEXES = {
    "wiki_reservation_live_path": "CREATE UNIQUE INDEX wiki_reservation_live_path ON wiki_path_reservations(path_key) WHERE state IN ('reserved','prepared','blocked')",
    "wiki_reservation_job": "CREATE INDEX wiki_reservation_job ON wiki_path_reservations(job_id,purpose,path)",
    "wiki_grant_document_identity": "CREATE INDEX wiki_grant_document_identity ON wiki_grant_documents(doc_id,job_id)",
    "wiki_grant_admission_path": "CREATE INDEX wiki_grant_admission_path ON wiki_grant_documents(job_id,path_at_admission,doc_id)",
}
WRITER = """SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;"""
TRIGGERS = {}
for table in TABLES:
    key = "job_id" if table == "wiki_job_grants" else "id" if table == "wiki_path_reservations" else None
    same = (f"{key}=new.{key}" if key else "job_id=new.job_id AND doc_id=new.doc_id"
            if table == "wiki_grant_documents" else "job_id=new.job_id AND registration_generation=new.registration_generation")
    name = table + "_insert"
    owner = "" if table == "wiki_job_grants" else """SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant owner is missing') END;"""
    if table == "wiki_grant_documents":
        owner += """SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM documents WHERE id=new.doc_id)
          OR (new.origin='selected' AND new.reservation_id IS NOT NULL)
          OR (new.origin='created' AND new.reservation_id IS NULL)
          OR (new.reservation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM wiki_path_reservations r
            WHERE r.id=new.reservation_id AND r.job_id=new.job_id AND r.state='bound'
              AND r.document_id=new.doc_id AND r.path=new.path_at_admission AND r.output_revision=new.observed_revision
              AND r.purpose=CASE new.origin WHEN 'material' THEN 'framework_material' ELSE 'requested_knowledge_page' END))
          THEN RAISE(ABORT,'Wiki grant document requires its original identity proof') END;"""
    if table == "wiki_path_reservations":
        owner += """SELECT CASE WHEN new.state<>'reserved' OR (new.purpose='requested_knowledge_page'
          AND NOT EXISTS(SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id AND creation_purpose='requested_knowledge_page'))
          THEN RAISE(ABORT,'Wiki reservation requires its admitted creation purpose') END;"""
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN {WRITER}
        SELECT CASE WHEN EXISTS(SELECT 1 FROM {table} WHERE {same})
        THEN RAISE(ABORT,'Wiki grant facts cannot be replaced') END; {owner} END"""
    for action in ("UPDATE", "DELETE"):
        if table == "wiki_path_reservations" and action == "UPDATE":
            continue
        name = table + "_" + action.lower()
        TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE {action} ON {table} BEGIN {WRITER}
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END"""
TRIGGERS["wiki_reservation_transition"] = f"""CREATE TRIGGER wiki_reservation_transition
    BEFORE UPDATE ON wiki_path_reservations BEGIN {WRITER}
    SELECT CASE WHEN new.id<>old.id OR new.job_id<>old.job_id OR new.path<>old.path
      OR new.path_key<>old.path_key OR new.purpose<>old.purpose
      OR (old.document_id IS NOT NULL AND (new.document_id IS NOT old.document_id
        OR new.change_id IS NOT old.change_id OR new.output_revision IS NOT old.output_revision))
      OR NOT ((old.state='reserved' AND new.state IN ('prepared','released'))
        OR (old.state='released' AND new.state='reserved')
        OR (old.state='prepared' AND new.state IN ('bound','blocked'))
        OR (old.state='blocked' AND new.state='bound'))
      THEN RAISE(ABORT,'Invalid Wiki reservation transition') END;
    SELECT CASE WHEN new.state='prepared' AND NOT EXISTS(SELECT 1 FROM changes c
      JOIN change_retention t ON t.change_id=c.id WHERE c.id=new.change_id AND c.path=new.path
        AND c.before_hash IS NULL AND c.after_hash=new.output_revision AND c.status='prepared'
        AND t.document_id=new.document_id)
      THEN RAISE(ABORT,'Wiki reservation requires its actual prepared change') END; END"""
JOB_TRIGGER = {
    "wiki_grant_job_owner": f"""CREATE TRIGGER wiki_grant_job_owner BEFORE INSERT ON wiki_job_grants
        BEGIN {WRITER} SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant requires its durable job') END; END""",
}


def unavailable():
    return WikiError("Wiki grant ledger is missing or incompatible; durable authority retained", 503)


def scope_blocked(error):
    try:
        detail = json.loads(error or "null")
    except (TypeError, ValueError):
        return False
    return isinstance(detail, dict) and detail.get("code") == "scope_review_required"


def review_required(db, job, reason):
    """Retain the original work and show a proposal, never silently grant it."""
    try:
        items = json.loads(job["items"])
        paths = list(dict.fromkeys(item["path"] for item in items if isinstance(item, dict) and isinstance(item.get("path"), str)))
    except (ValueError, TypeError, KeyError):
        paths = []
    return json.dumps({"code": "scope_review_required", "reason": reason,
                       "original_request_key": job["request_key"],
                       "proposed_grant": {"paths": paths, "allow_new_pages": False}}, ensure_ascii=False)


def current_generation(db, grant):
    accepted = db.execute("SELECT registration_generation FROM wiki_grant_readmissions WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                          (grant["job_id"],)).fetchone()
    return accepted[0] if accepted else grant["registration_generation"]


def block_ungranted(db, registration=None):
    require(db)
    invalid = "g.job_id IS NULL"
    params: tuple[str, ...] = ()
    if registration is not None:
        invalid += """ OR g.vault_id<>? OR coalesce((SELECT r.registration_generation
            FROM wiki_grant_readmissions r WHERE r.job_id=g.job_id ORDER BY r.rowid DESC LIMIT 1),g.registration_generation)<>?"""
        params = registration["vault_id"], registration["registration_generation"]
    rows = db.execute("""SELECT j.* FROM jobs j LEFT JOIN wiki_job_grants g ON g.job_id=j.id
        WHERE j.status IN ('queued','running','interrupted') AND (""" + invalid + ")", params).fetchall()
    for job in rows:
        db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (
            review_required(db, job, "A current trusted V2 scope is required; re-admit this original job"), job["id"]))


def migrate_legacy(db, store):
    """One V9→V10 transaction; use durable facts without observing vault files."""
    if not maintenance_inputs.jobs_initialized(db):
        return
    registration = {"vault_id": getattr(store, "vault_id", None),
                    "registration_generation": getattr(store, "registration_generation", None)}
    for job in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','interrupted')").fetchall():
        try:
            payload = json.loads(job["payload"])
            items = json.loads(job["items"])
            request = payload["request"]
            if (job["status"] != "queued" or not all(registration.values())
                    or not isinstance(request, dict) or not isinstance(items, list)
                    or hashlib.sha256(job["payload"].encode()).hexdigest() != job["payload_hash"]
                    or any(payload.get(key) != request.get(key) for key in ("material", "paths", "target", "actor"))
                    or payload.get("creation") is not None):
                raise ValueError("The original admission or registration cannot be proved")
            alias = db.execute("SELECT request_hash,job_id FROM job_requests WHERE request_key=?", (job["request_key"],)).fetchone()
            request_hash = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if alias and (alias["job_id"] != job["id"] or alias["request_hash"] != request_hash):
                raise ValueError("The original idempotency evidence does not agree")
            if (db.execute("SELECT 1 FROM operation_receipts WHERE job_id=? LIMIT 1", (job["id"],)).fetchone()
                    or db.execute("SELECT 1 FROM job_publications WHERE job_id=? LIMIT 1", (job["id"],)).fetchone()):
                raise ValueError("Prior worker outputs require scope and receipt review")
            material = payload.get("material", "")
            if not isinstance(material, str):
                raise ValueError("The material is not an original submission")
            reservation = None
            material_document = None
            if material and items and not payload.get("paths"):
                if len(items) != 1 or items[0].get("status") != "pending":
                    raise ValueError("Prior material work needs item-by-item scope review")
                item = items[0]
                material_document = db.execute("SELECT id,path,revision FROM documents WHERE id=?", (item["doc"],)).fetchone()
                digest = hashlib.sha256(material.encode()).hexdigest()
                if (material_document is None or not _literal_target(material_document["path"], existing=True)
                        or material_document["revision"] != digest or item.get("revision") != digest):
                    raise ValueError("The original material identity and exact bytes cannot be proved")
            elif material and not items and not payload.get("paths"):
                path = payload.get("target") or f"Library/Inbox/{job['id']}.md"
                if (not _literal_target(path) or PurePosixPath(path).suffix.lower() != ".md"
                        or db.execute("SELECT 1 FROM documents WHERE path=?", (path,)).fetchone()):
                    raise ValueError("The material target already needs identity verification")
                reservation = {"path": path, "path_key": os.path.normcase(str(store.root / path))}
            elif not material and items and payload.get("paths") == [item["path"] for item in items]:
                if len({item["doc"] for item in items}) != len(items):
                    raise ValueError("The selected identities are not distinct")
                for item in items:
                    current = db.execute("SELECT path FROM documents WHERE id=?", (item["doc"],)).fetchone()
                    if (item.get("status") != "pending" or not item.get("revision") or not _literal_target(item["path"], existing=True)
                            or current is None or not _literal_target(current["path"], existing=True)):
                        raise ValueError("An original selected identity cannot be proved")
            else:
                raise ValueError("The old work was broad, partially executed or had no finite selection")
            issue(db, registration, job["id"], [] if material_document else items, policy="legacy-explicit", material=reservation)
            if material_document:
                bind_document(db, job["id"], material_document["id"], material_document["path"], material_document["revision"], "material")
        except (ValueError, TypeError, KeyError) as error:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (review_required(db, job, str(error)), job["id"]))


def _literal_target(path, *, existing=False):
    return (isinstance(path, str) and path.startswith("Library/") and "\\" not in path
            and all(part not in {"", ".", ".."} for part in path.split("/"))
            and is_markdown_maintenance(path) and (existing or not asset_reason(path)))


def _sql(value):
    return " ".join(value.split()).rstrip(";")


def initialize(db):
    if not db.in_transaction:
        raise ValueError("Grant initialization requires the schema transaction")
    for sql in (*TABLES.values(), *INDEXES.values(), *TRIGGERS.values()):
        db.execute(sql)
    db.execute("INSERT INTO wiki_protocols VALUES('wiki_grants',2)")
    if maintenance_inputs.jobs_initialized(db):
        attach_jobs(db)
    require(db)


def attach_jobs(db):
    if not db.in_transaction or not maintenance_inputs.jobs_initialized(db):
        raise ValueError("Grant job guards belong to the actual job initialization transaction")
    for sql in JOB_TRIGGER.values():
        db.execute(sql)


def require(db):
    protocols.require(db, "wiki_grants", 2)
    initialized = maintenance_inputs.jobs_initialized(db)
    expected = TABLES | INDEXES | TRIGGERS | (JOB_TRIGGER if initialized else {})
    names = expected.keys() | JOB_TRIGGER.keys()
    actual = {row[0]: _sql(row[1]) for row in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")", tuple(names))}
    if actual != {name: _sql(sql) for name, sql in expected.items()}:
        raise unavailable()
    if not initialized and db.execute("SELECT 1 FROM wiki_job_grants LIMIT 1").fetchone():
        raise unavailable()


def targets(store, paths):
    """Prepare literal/physical names before entering the admission writer."""
    if len(paths) > 16 or len(set(paths)) != len(paths):
        raise WikiError("Use at most 16 distinct exact creation paths", 422)
    result = []
    for path in paths:
        logical = PurePosixPath(path)
        if (not path.startswith("Library/") or logical.suffix.lower() != ".md"
                or not is_markdown_maintenance(path) or asset_reason(path)):
            raise WikiError("New pages require an exact maintainable Library Markdown path", 422)
        target = store.path(path)
        if target.exists():
            raise WikiError("The requested creation target is already occupied", 409)
        result.append({"path": path, "path_key": os.path.normcase(str(target))})
    if len({item["path_key"] for item in result}) != len(result):
        raise WikiError("Creation paths identify the same physical target", 409)
    return result


def principal(task, scope, registration, actor):
    """Recheck the original requesting run in the final admission transaction."""
    from ..mcp_identity import scope_is_live
    from .maintenance_contract import actor_for
    from .tasks import tasks

    tasks.require_submitter(task, registration)
    if task is None:
        if scope is not None:
            raise WikiError("The original Wiki entry turn has ended", 403)
        return actor
    if (scope is None or not scope_is_live(scope) or tasks.active.get(scope.wiki_task_id) is not task
            or (scope.session_id, scope.user_id) != (task.session_id, task.user_id)
            or task.mode not in {"auto", "maintain"} or task.job_id
            or actor_for(task) != actor
            or task.registration is not None and task.registration != registration):
        raise WikiError("The original Wiki entry turn is no longer authorized", 403)
    return task.mode


def creation_policy(task, creation):
    if creation is None:
        return
    if task is not None:
        from .tasks import tasks

        tasks.require_submitter(task)
        if task.mode == "maintain":
            if not creation.get("paths"):
                raise WikiError("Select the new page's exact target in the original maintenance command", 403)
            tasks.require_selection(task, creation["paths"])


def issue(db, registration, job_id, items, *, policy="human", creation=None, outputs=(), material=None):
    require(db)
    if not db.in_transaction:
        raise ValueError("Grant issuance requires the final job admission transaction")
    db.execute("INSERT INTO wiki_job_grants VALUES(?,2,?,?,?,?,?)", (
        job_id, registration["vault_id"], registration["registration_generation"],
        creation["purpose"] if creation else None, policy, datetime.now(UTC).isoformat()))
    for item in items:
        if item.get("doc") and item.get("revision"):
            bind_document(db, job_id, item["doc"], item["path"], item["revision"], "selected")
    if material is not None:
        reserve(db, job_id, material, "framework_material")
    return [reserve(db, job_id, output, "requested_knowledge_page") for output in outputs]


def bind_document(db, job_id, doc_id, path, revision, origin, reservation_id=None):
    existing = db.execute("SELECT 1 FROM wiki_grant_documents WHERE job_id=? AND doc_id=?", (job_id, doc_id)).fetchone()
    if not existing:
        db.execute("INSERT INTO wiki_grant_documents VALUES(?,?,?,?,?,?)", (job_id, doc_id, path, revision, origin, reservation_id))


def reserve(db, job_id, target, purpose):
    identifier = uuid.uuid4().hex
    try:
        db.execute("INSERT INTO wiki_path_reservations VALUES(?,?,?,?,?,'reserved',NULL,NULL,NULL)",
                   (identifier, job_id, target["path"], target["path_key"], purpose))
    except sqlite3.IntegrityError as exc:
        raise WikiError("This creation path is reserved by another accepted job", 409) from exc
    return {"kind": "creation", "reservation_id": identifier, "path": target["path"], "status": "pending"}


def reacquire(db, reservations):
    """Prepared path checks precede this same-transaction reacquire and queue."""
    if not db.in_transaction:
        raise ValueError("Reservation reacquisition requires the retry transaction")
    try:
        for row in reservations:
            changed = db.execute("UPDATE wiki_path_reservations SET state='reserved' WHERE id=? AND job_id=? AND state='released'",
                                 (row["id"], row["job_id"])).rowcount
            if changed != 1:
                raise WikiError("The original creation reservation changed before retry", 409)
    except sqlite3.IntegrityError as error:
        raise WikiError("The original creation path is now reserved by another accepted job", 409) from error


def view(db, job_id, *, document_ids=None, reservation_ids=None):
    require(db)
    grant = db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (job_id,)).fetchone()
    if grant is None:
        return None
    columns = "SELECT g.*,d.path AS current_path,r.path AS creation_path "
    joins = "LEFT JOIN documents d ON d.id=g.doc_id LEFT JOIN wiki_path_reservations r ON r.id=g.reservation_id "
    select = columns + "FROM wiki_grant_documents g " + joins
    reserved = "SELECT * FROM wiki_path_reservations WHERE job_id=? AND purpose='requested_knowledge_page'"
    if document_ids is None and reservation_ids is None:
        documents = [dict(row) for row in db.execute(select + "WHERE g.job_id=? ORDER BY g.doc_id", (job_id,))]
        reservations = [dict(row) for row in db.execute(reserved + " ORDER BY path", (job_id,))]
    else:
        reservations = [dict(row) for row in db.execute(reserved + " AND id IN ("
                        + ",".join("?" for _ in reservation_ids) + ") ORDER BY path", (job_id, *reservation_ids))] if reservation_ids else []
        # A reservation can add only its own verified output identity. Current
        # occupants of captured paths never become members of this batch.
        selected = tuple(dict.fromkeys([*(document_ids or ()),
                         *(row["document_id"] for row in reservations if row["state"] == "bound")]))
        # Drive the query from the small immutable batch, rather than scanning
        # every grant in a wide job before filtering those same three IDs.
        chosen = "WITH chosen(doc_id) AS (VALUES " + ",".join("(?)" for _ in selected) + ") "
        documents = [dict(row) for row in db.execute(chosen + columns
                     + "FROM chosen s CROSS JOIN wiki_grant_documents g ON g.job_id=? AND g.doc_id=s.doc_id "
                     + joins + "ORDER BY g.doc_id", (*selected, job_id))] if selected else []
    allowed = [row["path"] for row in reservations if row["state"] == "reserved"]
    return {"version": 2, "vault_id": grant["vault_id"], "registration_generation": current_generation(db, grant),
            "job_id": job_id, "selected_documents": [{key: row[key] for key in ("doc_id", "path_at_admission", "observed_revision")}
                                                      | {"current_path": row["current_path"]} for row in documents],
            "allow_new_pages": bool(allowed), "allowed_new_paths": allowed,
            "creation_purpose": grant["creation_purpose"], "reservations": reservations}


def active(db, task, store, *, require_scope=True):
    from ..mcp_identity import scope_is_live
    from .registry import registry
    from .tasks import tasks

    require(db)
    root = task.active_task or task
    scope = task.call_scope
    if require_scope and (scope is None or not scope_is_live(scope)
            or tasks.active.get(scope.wiki_task_id) is not root
            or (scope.session_id, scope.user_id) != (task.session_id, task.user_id)):
        raise WikiError("The original maintenance run is no longer authorized", 403)
    row = db.execute("SELECT status,session_id FROM jobs WHERE id=?", (task.job_id,)).fetchone()
    config = registry.config(task.user_id)
    grant = db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (task.job_id,)).fetchone()
    if (row is None or row["status"] != "running" or row["session_id"] != task.session_id
            or task.mode != "maintain" or task.agent_id != config["curator_agent_id"]
            or grant is None or grant["vault_id"] != store.vault_id
            or current_generation(db, grant) != config["registration_generation"]
            or store.registration_generation != config["registration_generation"]
            or task.registration is not None and task.registration != config):
        raise WikiError("This exact maintenance assignment is no longer active", 403)
    if task.assigned_request_id is not None:
        state = db.execute("SELECT attempt FROM wiki_job_state WHERE job_id=?", (task.job_id,)).fetchone()
        if (task.request_id != task.assigned_request_id or state is None
                or state[0] != task.assigned_attempt):
            raise WikiError("This original maintenance request or attempt is no longer active", 403)


def authorize(db, task, store, path, *, create=False):
    active(db, task, store)
    if not is_markdown_maintenance(path) or create and asset_reason(path):
        raise WikiError("This is outside Markdown maintenance scope", 403)
    granted = view(db, task.job_id, document_ids=task.document_ids, reservation_ids=task.reservation_ids)
    current = db.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
    if not create and current and any(item["doc_id"] == current["id"] for item in granted["selected_documents"]):
        return None
    if create and path in granted["allowed_new_paths"]:
        return next(row for row in granted["reservations"] if row["path"] == path)
    if any(item["path_at_admission"] == path or item["current_path"] == path for item in granted["selected_documents"]):
        raise WikiError("The assigned document identity was deleted or replaced", 403)
    raise WikiError("This target has no current exact document or creation grant", 403)


def navigation(db, task, store, document, inputs):
    """Internal evidence for fixed links on an existing, unassigned index."""
    context = {"job_id": task.job_id, "session_id": task.session_id, "authority": task,
               "source_doc": None, "source_path": document["path"], "input_revision": None,
               "supporting_navigation": {"target": {"doc": document["id"], "path": document["path"],
                                                      "revision": document["revision"]}, "inputs": inputs}}
    for proof in [context["supporting_navigation"]["target"], *inputs]:
        try:
            target = store.path(proof["path"])
            proof["physical_path"] = str(target)
            proof["file_key"] = store.file_key(target.stat())
        except OSError as exc:
            raise WikiError("A supporting navigation document disappeared", 409) from exc
    check_navigation(db, store, context, document["path"])
    return context


def check_navigation(db, store, context, path, doc_id=None):
    task = context["authority"]
    active(db, task, store)
    supporting = context["supporting_navigation"]
    target = supporting["target"]
    if target["path"] != path or doc_id is not None and target["doc"] != doc_id:
        raise WikiError("The supporting navigation target identity changed", 403)
    row = db.execute("SELECT metadata FROM documents WHERE id=? AND path=?", (target["doc"], path)).fetchone()
    owned = db.execute("SELECT metadata FROM managed_notes WHERE path=?", (path,)).fetchone()
    metadata = (json.loads(row[0]) if row else {}) | (json.loads(owned[0]) if owned else {})
    if (row is None or not is_markdown_maintenance(path) or asset_reason(path)
            or metadata.get("doc_role") != "navigation"
            and PurePosixPath(path).name.casefold() not in {"index.md", "readme.md", "目录.md", "索引.md"}):
        raise WikiError("Supporting links require an existing Markdown navigation page", 403)
    granted = view(db, task.job_id, document_ids=task.document_ids, reservation_ids=task.reservation_ids)
    selected = {item["doc_id"] for item in granted["selected_documents"]}
    if not supporting["inputs"] or any(proof["doc"] not in selected for proof in supporting["inputs"]):
        raise WikiError("Navigation links must refer to this batch's actual assigned identities", 403)
    # An editor can replace A without delivering its notification yet. Check
    # the captured physical identities and bytes as well as the indexed IDs.
    for proof in [target, *supporting["inputs"]]:
        current = db.execute("""SELECT d.revision,i.file_key FROM documents d
            JOIN document_identities i ON i.id=d.id AND i.path=d.path
            WHERE d.id=? AND d.path=?""", (proof["doc"], proof["path"])).fetchone()
        try:
            file = store.path(proof["path"])
            unchanged = (current is not None and current["revision"] == proof["revision"]
                         and current["file_key"] == proof["file_key"] == store.file_key(file.stat())
                         and str(file) == proof["physical_path"] and revision(file.read_bytes()) == proof["revision"])
        except (OSError, WikiError):
            unchanged = False
        if not unchanged:
            raise WikiError("A supporting navigation document identity or revision changed", 409)


def prepare_creation(db, context, store, path, document_id, change_id, output_revision):
    require(db)
    grant = db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (context["job_id"],)).fetchone()
    if not grant or grant["vault_id"] != store.vault_id or current_generation(db, grant) != store.registration_generation:
        raise WikiError("The creation grant belongs to another registration", 403)
    reservation = db.execute("SELECT * FROM wiki_path_reservations WHERE id=?", (context["reservation_id"],)).fetchone()
    if (reservation is None or reservation["job_id"] != context["job_id"] or reservation["path"] != path
            or reservation["state"] != "reserved"):
        raise WikiError("This one-time creation reservation is unavailable", 403)
    if os.path.normcase(str(store.path(path))) != reservation["path_key"]:
        raise WikiError("The physical creation target changed after admission", 409)
    if context.get("authority"):
        actual = authorize(db, context["authority"], store, path, create=True)
        if actual["id"] != reservation["id"]:
            raise WikiError("The creation reservation belongs to another assignment", 403)
    elif reservation["purpose"] != "framework_material":
        raise WikiError("Knowledge pages require an active worker grant", 403)
    else:
        job = db.execute("SELECT status FROM jobs WHERE id=?", (context["job_id"],)).fetchone()
        if job is None or job["status"] not in {"queued", "running"}:
            raise WikiError("This material admission is no longer active", 403)
    db.execute("UPDATE wiki_path_reservations SET state='prepared',document_id=?,change_id=?,output_revision=? WHERE id=?",
               (document_id, change_id, output_revision, reservation["id"]))


def check_write(db, store, context, path, *, change_id=None, doc_id=None):
    require(db)
    if context.get("supporting_navigation"):
        return check_navigation(db, store, context, path, doc_id)
    task = context.get("authority")
    if task is not None:
        active(db, task, store)
    elif context.get("framework_material") is not True:
        raise WikiError("This write has no original worker authority", 403)
    if context.get("reservation_id"):
        row = db.execute("SELECT * FROM wiki_path_reservations WHERE id=?", (context["reservation_id"],)).fetchone()
        if (row is None or row["job_id"] != context["job_id"] or row["path"] != path
                or row["state"] != "prepared" or row["change_id"] != change_id
                or doc_id is not None and row["document_id"] != doc_id):
            raise WikiError("This creation operation is not the authorized prepared operation", 403)
        if os.path.normcase(str(store.path(path))) != row["path_key"]:
            raise WikiError("The physical creation target changed after preparation", 409)
        if task is not None and row["id"] not in task.reservation_ids:
            raise WikiError("This creation operation is outside the current batch", 403)
        if task is None:
            job = db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if row["purpose"] != "framework_material" or not job or job[0] not in {"queued", "running"}:
                raise WikiError("This material job is no longer active", 403)
    else:
        authorize(db, task, store, path)


def settle_creation(db, store, change_id):
    row = db.execute("""SELECT r.*,c.status AS change_status,c.after_hash,t.document_id AS recorded_document
        FROM wiki_path_reservations r JOIN changes c ON c.id=r.change_id
        JOIN change_retention t ON t.change_id=c.id WHERE r.change_id=?""", (change_id,)).fetchone()
    if row is None or row["state"] == "bound":
        return
    if row["change_status"] != "applied":
        if row["change_status"] != "prepared" and row["state"] == "prepared":
            db.execute("UPDATE wiki_path_reservations SET state='blocked' WHERE id=?", (row["id"],))
        return
    current = db.execute("SELECT id,revision FROM documents WHERE path=?", (row["path"],)).fetchone()
    if (current is None or current["id"] != row["document_id"] or row["recorded_document"] != row["document_id"]
            or current["revision"] != row["output_revision"] or row["after_hash"] != row["output_revision"]):
        raise WikiError("Creation recording requires verification of the original output identity", 409)
    store.require_indexed(db, row["path"])
    db.execute("UPDATE wiki_path_reservations SET state='bound' WHERE id=?", (row["id"],))
    bind_document(db, row["job_id"], row["document_id"], row["path"], row["output_revision"],
                  "material" if row["purpose"] == "framework_material" else "created", row["id"])

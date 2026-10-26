"""Transactional projection inputs; durable jobs remain owned by WikiJobs.

All fact writers must enable recursive_triggers so REPLACE invalidates the
deleted identities too. Generated triggers abort writes that disable it.
"""

import json
import uuid

from . import job_items, protocols

VERSION = "maintenance-inputs-v11"
PREFIX = "maintenance_input_"
TABLES = {
    "maintenance_input_state": """CREATE TABLE maintenance_input_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL,
        rebuild_nonce TEXT NOT NULL,generation INTEGER NOT NULL)""",
    "maintenance_dirty": """CREATE TABLE maintenance_dirty(
        doc_id TEXT PRIMARY KEY,cause_generation INTEGER NOT NULL)""",
    "maintenance_dirty_paths": """CREATE TABLE maintenance_dirty_paths(
        doc_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(doc_id,path))""",
    "maintenance_job_members": """CREATE TABLE maintenance_job_members(
        job_id TEXT NOT NULL,doc_id TEXT NOT NULL,
        PRIMARY KEY(job_id,doc_id))""",
    "maintenance_job_dirty": """CREATE TABLE maintenance_job_dirty(
        job_id TEXT PRIMARY KEY,cause_generation INTEGER NOT NULL)""",
    "maintenance_job_items": """CREATE TABLE maintenance_job_items(
        job_id TEXT NOT NULL,ordinal INTEGER NOT NULL,item_doc TEXT,status TEXT,
        index_complete INTEGER NOT NULL,recording_failure INTEGER NOT NULL,
        index_recovery INTEGER NOT NULL,output_path TEXT,output_revision TEXT,
        output_revision_valid INTEGER NOT NULL,receipt_id TEXT,receipt_required INTEGER NOT NULL,
        output_change_id TEXT,PRIMARY KEY(job_id,ordinal))""",
    "maintenance_projection": """CREATE TABLE maintenance_projection(
        doc_id TEXT PRIMARY KEY,version TEXT NOT NULL,input_generation INTEGER NOT NULL,
        path TEXT NOT NULL,candidate INTEGER NOT NULL,status TEXT NOT NULL,summary_json TEXT NOT NULL)""",
    "maintenance_accepted_paths": """CREATE TABLE maintenance_accepted_paths(
        doc_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(doc_id,path))""",
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS maintenance_dirty_scope ON maintenance_dirty_paths(path,doc_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_members_document ON maintenance_job_members(doc_id,job_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_items_document ON maintenance_job_items(item_doc,job_id,ordinal)",
    "CREATE INDEX IF NOT EXISTS maintenance_candidate_order ON maintenance_projection(candidate,path,doc_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_path_order ON maintenance_projection(path,doc_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_receipt_document ON operation_receipts(doc_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_receipt_change ON operation_receipts(change_id,job_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_publication_change ON job_publications(change_id,job_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_retention_document ON change_retention(document_id,change_id)",
    "CREATE INDEX IF NOT EXISTS maintenance_binding_target ON source_bindings(target_doc,source_doc)",
    "CREATE INDEX IF NOT EXISTS maintenance_dependency_edges ON wiki_edges(source_doc,relation,ordinal)",
    "CREATE INDEX IF NOT EXISTS maintenance_document_errors ON documents(id) WHERE quality='error' OR json_extract(metadata, '$.frontmatter_error') IS NOT NULL",
)
VIEWS = {
    "maintenance_member_sources": """CREATE VIEW maintenance_member_sources AS
        SELECT j.id AS job_id,json_extract(i.value,'$.doc') AS doc_id
            FROM jobs j,json_each(j.items) i WHERE json_type(i.value,'$.doc')='text'
        UNION SELECT r.job_id,r.item_doc FROM operation_receipts r JOIN jobs j ON j.id=r.job_id
            WHERE r.item_doc IS NOT NULL
        UNION SELECT r.job_id,r.doc_id FROM operation_receipts r JOIN jobs j ON j.id=r.job_id
        UNION SELECT p.job_id,p.source_doc FROM job_publications p JOIN jobs j ON j.id=p.job_id
        UNION SELECT p.job_id,r.document_id FROM job_publications p
            JOIN jobs j ON j.id=p.job_id JOIN change_retention r ON r.change_id=p.change_id""",
}
JOB_COLUMNS = (
    ("id", "TEXT", 0, None, 1, 0), ("request_key", "TEXT", 1, None, 0, 0),
    ("payload_hash", "TEXT", 1, None, 0, 0), ("payload", "TEXT", 1, None, 0, 0),
    ("status", "TEXT", 1, None, 0, 0), ("items", "TEXT", 1, None, 0, 0),
    ("error", "TEXT", 0, None, 0, 0), ("session_id", "TEXT", 0, None, 0, 0),
    ("created_at", "TEXT", 1, None, 0, 0), ("updated_at", "TEXT", 1, None, 0, 0),
)
REQUEST_COLUMNS = (
    ("request_key", "TEXT", 0, None, 1, 0),
    ("request_hash", "TEXT", 1, None, 0, 0), ("job_id", "TEXT", 1, None, 0, 0),
)


def unavailable(message):
    from .store import WikiError

    return WikiError("Maintenance projection " + message + "; durable state retained", 503)


def names(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _shape(db, table):
    return tuple((r[1], r[2].upper(), r[3], r[4], r[5], r[6])
                 for r in db.execute(f"PRAGMA table_xinfo({table})"))


def validate_jobs(db):
    columns = (("id", "TEXT", 1, None, 0, 0), *JOB_COLUMNS[1:],
               ("attempt_errors", "TEXT", 1, "'{}'", 0, 0),
               ("admission_sequence", "INTEGER", 0, None, 1, 0))
    if _shape(db, "jobs") != columns:
        raise unavailable("job ledger is missing or incompatible")
    if _shape(db, "job_requests") != REQUEST_COLUMNS:
        raise unavailable("job ledger is missing or incompatible")
    unique_request = any(row[2] and not row[4] and
                         [column[2] for column in db.execute("SELECT * FROM pragma_index_info(?)", (row[1],))] == ["request_key"]
                         for row in db.execute("PRAGMA index_list(jobs)"))
    if not unique_request:
        raise unavailable("job ledger constraints are incompatible")


def require_jobs(db):
    protocols.require(db, "maintenance_jobs")
    validate_jobs(db)

def require(db):
    protocols.require(db, "maintenance_inputs")
    require_jobs(db)


def _mark_docs(selection):
    return f"""INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM ({selection}) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        """


def _invalidate_jobs(selection, *, documents=None):
    affected = documents if documents is not None else f"SELECT doc_id FROM maintenance_job_members WHERE job_id IN ({selection})"
    return _mark_docs(affected) + f"""
        INSERT INTO maintenance_job_dirty
            SELECT j.id,s.generation FROM jobs j CROSS JOIN maintenance_input_state s
            WHERE j.id IN ({selection})
            ON CONFLICT(job_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        DELETE FROM maintenance_job_items WHERE job_id IN ({selection}) AND NOT EXISTS
            (SELECT 1 FROM jobs WHERE id=maintenance_job_items.job_id);
        DELETE FROM maintenance_job_dirty WHERE job_id IN ({selection}) AND NOT EXISTS
            (SELECT 1 FROM jobs WHERE id=maintenance_job_dirty.job_id);
        UPDATE operation_receipts SET item_match_sequence=NULL
            WHERE job_id IN ({selection}) AND item_match_sequence IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM jobs WHERE id=operation_receipts.job_id);
    """


def _updated_job_documents():
    """Item changes affect their input and exact outputs; job state affects all."""
    whole_job = " OR ".join(f"old.{column} IS NOT new.{column}"
                            for column in ("id", "payload", "status", "error", "session_id"))
    return f"""WITH changed_items AS MATERIALIZED (
        SELECT * FROM (SELECT key,value FROM json_each(old.items)
            EXCEPT SELECT key,value FROM json_each(new.items))
        UNION SELECT * FROM (SELECT key,value FROM json_each(new.items)
            EXCEPT SELECT key,value FROM json_each(old.items))
    ), changed_documents AS MATERIALIZED (
        SELECT json_extract(value,'$.doc') AS doc_id FROM changed_items
    ) SELECT doc_id FROM maintenance_job_members WHERE job_id=new.id AND (
        {whole_job}
        OR EXISTS(SELECT 1 FROM changed_documents WHERE doc_id IS NULL)
        OR doc_id IN (
            SELECT doc_id FROM changed_documents
            UNION SELECT doc_id FROM operation_receipts WHERE job_id=new.id
                AND item_doc IN (SELECT doc_id FROM changed_documents)
            UNION SELECT r.document_id FROM job_publications p
                JOIN change_retention r ON r.change_id=p.change_id WHERE p.job_id=new.id
                AND p.source_doc IN (SELECT doc_id FROM changed_documents)))"""


def _refresh_jobs(selection):
    """Invalidate both sides of real relationship changes, writing only deltas."""
    members = f"SELECT job_id,doc_id FROM maintenance_member_sources WHERE job_id IN ({selection})"
    return _invalidate_jobs(selection) + f"""
        DELETE FROM maintenance_job_members WHERE job_id IN ({selection})
            AND (job_id,doc_id) NOT IN ({members});
        INSERT OR IGNORE INTO maintenance_job_members {members};
    """ + _mark_docs(f"SELECT doc_id FROM maintenance_job_members WHERE job_id IN ({selection})")


def _input_check(value):
    states = ",".join(repr(state) for state in sorted(job_items.ITEM_STATES))
    phases = ",".join(repr(phase) for phase in sorted(job_items.EXECUTION_PHASES))
    retired = " OR ".join(f"json_type(i.value,'$.{field}') IS NOT NULL" for field in job_items.RETIRED_FIELDS)
    return f"""SELECT CASE WHEN json_valid({value})=0 THEN
        RAISE(ABORT,'maintenance job membership requires valid JSON') END;
        SELECT CASE WHEN json_type({value})<>'array' THEN
        RAISE(ABORT,'maintenance job membership requires an array') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each({value}) i WHERE
            CASE WHEN i.type<>'object' THEN 1 ELSE
            coalesce(json_type(i.value,'$.doc') NOT IN ('text','null'),0) END)
        THEN RAISE(ABORT,'maintenance job membership requires object items and exact identities') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each({value}) i WHERE
            coalesce(json_type(i.value,'$.version')<>'integer',1)
            OR json_extract(i.value,'$.version')<>1
            OR coalesce(json_extract(i.value,'$.status') NOT IN ({states}),1)
            OR ({retired})
            OR CASE WHEN json_type(i.value,'$.worker_request') IS NULL THEN 0
                WHEN json_type(i.value,'$.worker_request')<>'object' THEN 1
                ELSE coalesce(json_extract(i.value,'$.worker_request.phase') NOT IN ({phases}),1) END
            OR CASE WHEN json_type(i.value,'$.previous_worker_requests') IS NULL THEN 0
                WHEN json_type(i.value,'$.previous_worker_requests')<>'array' THEN 1
                ELSE EXISTS(SELECT 1 FROM json_each(i.value,'$.previous_worker_requests') r WHERE
                    CASE WHEN r.type<>'object' THEN 1
                    ELSE coalesce(json_extract(r.value,'$.phase') NOT IN ({phases}),1) END) END)
        THEN RAISE(ABORT,'maintenance job membership requires the current item protocol') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM json_tree({value})
        WHERE key IS NOT NULL GROUP BY parent,key HAVING count(*)>1)
        THEN RAISE(ABORT,'maintenance job membership contains duplicate keys') END;"""


def _affected_changes(changes):
    return f"""SELECT job_id FROM operation_receipts WHERE change_id IN ({changes}) AND job_id IS NOT NULL
               UNION SELECT job_id FROM job_publications WHERE change_id IN ({changes})"""


def triggers():
    result = {}
    bump = "UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;"
    writer_check = """SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;"""
    member_change = "old.id IS NOT new.id OR (old.items IS NOT new.items AND " + " IS NOT ".join(
        f"(SELECT json_group_array(json_extract(value,'$.doc')) FROM json_each({side}.items))"
        for side in ("old", "new")) + ")"

    def add(table, action, body, *, before=False, columns=(), condition=None):
        name = PREFIX + table + "_" + action.lower() + ("_check" if before else "")
        # REPLACE deletes conflicting rows before the final INSERT/UPDATE.
        # With recursive triggers disabled, those implicit deletes are invisible.
        # ABORT also rolls them back when a direct writer violates this protocol.
        changes = " OR ".join(f"old.{column} IS NOT new.{column}" for column in columns) if action == "UPDATE" else ""
        predicate = " AND ".join(f"({part})" for part in (changes, condition) if part)
        changed = " WHEN " + predicate if predicate else ""
        result[name] = f"CREATE TRIGGER {name} {'BEFORE' if before else 'AFTER'} {action} ON {table}{changed} BEGIN {writer_check}{body} END"

    for action in ("INSERT", "UPDATE", "DELETE"):
        sides = ("old", "new") if action == "UPDATE" else ("old",) if action == "DELETE" else ("new",)
        for table in ("wiki_job_grants", "wiki_grant_documents", "wiki_path_reservations", "wiki_grant_readmissions"):
            jobs = " UNION ".join(f"SELECT {side}.job_id" for side in sides)
            affected = (" UNION ".join(f"SELECT {side}.doc_id AS doc_id" for side in sides)
                        if table == "wiki_grant_documents" else
                        " UNION ".join(f"SELECT {side}.document_id AS doc_id" for side in sides)
                        if table == "wiki_path_reservations" else
                        f"SELECT doc_id FROM maintenance_job_members WHERE job_id IN ({jobs})")
            # Grant facts do not add members: items, receipts and publications
            # own those relations. A selected-document grant affects only that
            # document; job-wide policy and reservation changes affect members.
            add(table, action, bump + _mark_docs(affected))
        ids = " UNION ".join(f"SELECT {side}.id AS doc_id" for side in sides)
        incoming = (f"{ids} UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN ({ids})"
                    f" UNION SELECT source_doc FROM source_bindings WHERE target_doc IN ({ids})")
        add("documents", action, bump + _mark_docs(incoming),
            columns=("id", "path", "revision", "title", "kind", "text", "pages", "quality", "metadata"))
        for table in ("wiki_edges", "source_bindings", "source_binding_head", "source_binding_observations"):
            add(table, action, bump + _mark_docs(" UNION ".join(f"SELECT {side}.source_doc AS doc_id" for side in sides)))
        # Inbox receipt/application watermarks affect live readiness, not the
        # semantic generation. Applying real changes invalidates their facts.
        for table in ("wiki_lookup_names", "index_state"):
            add(table, action, bump, columns=("epoch", "error") if table == "index_state" else ())

        receipt_docs = " UNION ".join(f"SELECT {side}.{column} AS doc_id" for side in sides for column in ("doc_id", "item_doc"))
        receipt_jobs = " UNION ".join(f"SELECT {side}.job_id" for side in sides)
        revoke = ("UPDATE operation_receipts SET item_match_sequence=NULL WHERE id=new.id AND item_match_sequence IS NOT NULL;"
                  if action != "DELETE" else "")
        add("operation_receipts", action, revoke + bump + _mark_docs(receipt_docs) + _refresh_jobs(receipt_jobs),
            columns=("id", "job_id", "item_doc", "doc_id", "path", "input_revision", "output_revision",
                     "operation", "reason", "change_id", "created_at", "details"))

        for table in ("changes", "change_retention", "change_proposals", "job_publications"):
            column = "id" if table == "changes" else "change_id"
            changes = " UNION ".join(f"SELECT {side}.{column}" for side in sides)
            affected = (f"SELECT document_id AS doc_id FROM change_retention WHERE change_id IN ({changes})"
                        f" UNION SELECT doc_id FROM operation_receipts WHERE change_id IN ({changes})"
                        f" UNION SELECT item_doc FROM operation_receipts WHERE change_id IN ({changes})"
                        f" UNION SELECT source_doc FROM job_publications WHERE change_id IN ({changes})")
            if table == "change_retention":
                affected += " UNION " + " UNION ".join(f"SELECT {side}.document_id" for side in sides)
            if table == "job_publications":
                affected += " UNION " + " UNION ".join(f"SELECT {side}.source_doc" for side in sides)
            body = bump + _mark_docs(affected)
            if table != "change_proposals":
                jobs = _affected_changes(changes)
                if table == "job_publications":
                    jobs += " UNION " + " UNION ".join(f"SELECT {side}.job_id" for side in sides)
                body += _refresh_jobs(jobs)
            add(table, action, body)
        if action != "DELETE":
            # Every direct UPDATE must honor the writer protocol, including
            # timestamp-only writes. Only changed JSON needs full validation.
            add("jobs", action, _input_check("new.items") if action == "INSERT" else "", before=True)
        jobs = " UNION ".join(f"SELECT {side}.id" for side in sides)
        add("jobs", action, bump + (_invalidate_jobs(jobs, documents=_updated_job_documents()) if action == "UPDATE" else _refresh_jobs(jobs)),
            columns=("id", "payload", "status", "items", "error", "session_id"),
            condition=f"NOT ({member_change})" if action == "UPDATE" else None)
    # Progress changes item state, not its document relationships. Keep the
    # ordinary invalidation/version path; traverse relation sources only
    # when the item identities actually change. Both triggers share the TX.
    name = PREFIX + "jobs_update_members"
    result[name] = f"CREATE TRIGGER {name} AFTER UPDATE ON jobs WHEN {member_change} BEGIN {writer_check}{bump}" + _refresh_jobs("SELECT old.id UNION SELECT new.id") + " END"
    name = PREFIX + "jobs_items_update_check"
    result[name] = f"""CREATE TRIGGER {name} BEFORE UPDATE OF items ON jobs
        WHEN old.items IS NOT new.items BEGIN {writer_check}{_input_check('new.items')} END"""
    result[PREFIX + "receipt_item_match"] = f"""CREATE TRIGGER {PREFIX}receipt_item_match
        AFTER UPDATE OF item_match_sequence ON operation_receipts
        WHEN old.item_match_sequence IS NOT new.item_match_sequence BEGIN {writer_check}{bump}""" + _mark_docs(
            "SELECT old.doc_id AS doc_id UNION SELECT new.doc_id UNION SELECT old.item_doc UNION SELECT new.item_doc") + " END"
    # Every invalidated document keeps both its live and projected scope paths.
    # Put this at the dirty-row boundary so callers evaluate their selection once.
    for action in ("INSERT", "UPDATE"):
        name = PREFIX + "dirty_paths_" + action.lower()
        result[name] = f"""CREATE TRIGGER {name} AFTER {action} ON maintenance_dirty BEGIN {writer_check}
            INSERT INTO maintenance_dirty_paths
                SELECT id,path FROM documents WHERE id=new.doc_id
                ON CONFLICT(doc_id,path) DO NOTHING;
            INSERT INTO maintenance_dirty_paths
                SELECT doc_id,path FROM maintenance_projection WHERE doc_id=new.doc_id
                ON CONFLICT(doc_id,path) DO NOTHING; END"""
    return result


def _canonical(sql):
    return " ".join(sql.split())


def _detach_triggers(db):
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        if row[0].startswith(PREFIX):
            quoted = row[0].replace('"', '""')
            db.execute(f'DROP TRIGGER "{quoted}"')


def discard_derived(db):
    """Remove only this disposable projection, never a protocol or job ledger."""
    _detach_triggers(db)
    for name in VIEWS:
        db.execute(f"DROP VIEW IF EXISTS {name}")
    for name in TABLES:
        db.execute(f"DROP TABLE IF EXISTS {name}")


def _compatible(db):
    expected = triggers()
    actual = {r[0]: r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    trigger_rows = {r[0]: r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'") if r[0].startswith(PREFIX)}
    views = {r[0]: r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='view'")}
    reset = (any(name not in actual or _canonical(actual[name]) != _canonical(sql) for name, sql in TABLES.items())
             or any(_canonical(views.get(name, "")) != _canonical(sql) for name, sql in VIEWS.items())
             or set(trigger_rows) != set(expected)
             or any(_canonical(trigger_rows.get(name, "")) != _canonical(sql) for name, sql in expected.items()))
    if not reset:
        state = db.execute("SELECT version FROM maintenance_input_state WHERE singleton=1").fetchone()
        reset = state is None or state[0] != VERSION
    return not reset


def prepare_repair(db):
    """Detach invalid cache triggers before other derived repairs perform DML.

    The schema initializer owns one transaction for detaching, repairing the
    dependencies, and rebuilding inputs. Missing durable ledgers fail before
    any trigger is removed; an interrupted repair restores the original schema.
    """
    if not db.in_transaction:
        raise ValueError("Maintenance repair requires an explicit transaction")
    require(db)
    dependencies = {"search_documents", "search_sections", "search_fts", "raw_materials",
                    "wiki_edges", "wiki_edge_lookups", "wiki_lookup_names", "catalog_state",
                    "catalog_dirty", "wiki_uid_claims", "wiki_uid_dirty"}
    if not _compatible(db) or not dependencies.issubset(names(db)):
        _detach_triggers(db)


def initialize(db):
    require(db)
    expected = triggers()
    reset = not _compatible(db)
    if reset:
        discard_derived(db)
        for sql in TABLES.values():
            db.execute(sql)
        for sql in VIEWS.values():
            db.execute(sql)
        db.execute("INSERT INTO maintenance_input_state VALUES(1,?,?,1)", (VERSION, uuid.uuid4().hex))
        # Validate retained input before JSON traversal. Future ledger mutations
        # are guarded by the corresponding BEFORE triggers.
        for row in db.execute("SELECT items FROM jobs"):
            _decode_items(row[0])
        for statement in _refresh_jobs("SELECT id FROM jobs").split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT OR IGNORE INTO maintenance_dirty SELECT id,1 FROM documents")
        db.execute("INSERT OR IGNORE INTO maintenance_dirty_paths SELECT id,path FROM documents")
        for sql in expected.values():
            db.execute(sql)
    existing_indexes = {row[0]: row[1] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")}
    for sql in INDEXES:
        expected_sql = sql.replace(" IF NOT EXISTS", "")
        name = expected_sql.split()[2]
        if _canonical(existing_indexes.get(name) or "") != _canonical(expected_sql):
            db.execute(f'DROP INDEX IF EXISTS "{name}"')
            db.execute(expected_sql)
    return reset


def _decode_items(raw):
    return job_items.decode(raw)


def _string(value):
    return value if isinstance(value, str) else None


def capture_jobs(db, doc_ids=None):
    """Decode each changed relevant job once in the caller's read snapshot."""
    if not db.in_transaction:
        raise ValueError("Maintenance capture requires an explicit snapshot")
    require(db)
    nonce = state(db)["rebuild_nonce"]
    if not db.execute("SELECT 1 FROM maintenance_job_dirty LIMIT 1").fetchone():
        return {"nonce": nonce, "jobs": []}
    condition, parameters = "", ()
    if doc_ids is not None:
        if not doc_ids:
            return {"nonce": nonce, "jobs": []}
        condition = " WHERE j.id IN (SELECT job_id FROM maintenance_job_members WHERE doc_id IN (" + ",".join("?" for _ in doc_ids) + "))"
        parameters = tuple(doc_ids)
    rows = db.execute("""SELECT j.admission_sequence AS sequence,j.id,j.status,j.items,d.cause_generation
                         FROM maintenance_job_dirty d JOIN jobs j ON j.id=d.job_id""" + condition, parameters).fetchall()
    captured = []
    for row in rows:
        items = []
        for ordinal, item in enumerate(_decode_items(row["items"])):
            output_revision = item.get("output_revision")
            items.append((row["id"], ordinal, item.get("doc"), _string(item.get("status")),
                          int(item.get("index_complete") is True), int(bool(job_items.unresolved(item)) or item.get("status") == "unknown"),
                          int(item.get("status") in {"verifying", "unknown"}), _string(item.get("output")), _string(output_revision),
                          int(output_revision is None or isinstance(output_revision, str)), _string(item.get("receipt_id")),
                          int(bool(item.get("receipt_id"))), _string(item.get("output_change_id"))))
        captured.append({"job_id": row["id"], "token": row["cause_generation"],
                         "sequence": row["sequence"], "status": row["status"], "items": items})
    return {"nonce": nonce, "jobs": captured}


def state(db):
    return dict(db.execute("SELECT * FROM maintenance_input_state WHERE singleton=1").fetchone())


def _match_receipts(db, job):
    """Persist exact item matching; applied/indexed/bound remain separate facts."""
    rows = db.execute("""SELECT r.id,r.item_match_sequence,CASE WHEN EXISTS(
        SELECT 1 FROM maintenance_job_items i WHERE i.job_id=r.job_id AND i.item_doc=r.item_doc
        AND i.status='done' AND i.index_complete=1 AND i.recording_failure=0 AND i.index_recovery=0
        AND i.output_revision_valid=1 AND i.output_path IS r.path AND i.output_revision IS r.output_revision
        AND (i.receipt_required=0 OR i.receipt_id IS NOT NULL AND i.receipt_id=r.id)
        AND (r.change_id IS NULL OR r.change_id='' OR i.output_change_id IS r.change_id))
        THEN ? END AS matched FROM operation_receipts r WHERE r.job_id=?""", (job["sequence"], job["job_id"]))
    changes = [(row["matched"], row["id"]) for row in rows if row["matched"] != row["item_match_sequence"]]
    db.executemany("UPDATE operation_receipts SET item_match_sequence=? WHERE id=?", changes)


def publish_jobs(db, captured):
    """Caller owns BEGIN IMMEDIATE; newer job tokens and document dirt survive."""
    if not db.in_transaction:
        raise ValueError("Maintenance publication requires an explicit transaction")
    require(db)
    if state(db)["rebuild_nonce"] != captured["nonce"]:
        return False
    for job in captured["jobs"]:
        row = db.execute("SELECT cause_generation FROM maintenance_job_dirty WHERE job_id=?", (job["job_id"],)).fetchone()
        if row is None or row[0] != job["token"]:
            return False
    for job in captured["jobs"]:
        db.execute("DELETE FROM maintenance_job_items WHERE job_id=?", (job["job_id"],))
        db.executemany("INSERT INTO maintenance_job_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", job["items"])
        _match_receipts(db, job)
        db.execute("DELETE FROM maintenance_job_dirty WHERE job_id=? AND cause_generation=?", (job["job_id"], job["token"]))
    return True


def ready(db, doc_ids):
    require(db)
    if not doc_ids or not db.execute("SELECT 1 FROM maintenance_job_dirty LIMIT 1").fetchone():
        return True
    row = db.execute("""SELECT 1 FROM maintenance_job_members m JOIN maintenance_job_dirty d ON d.job_id=m.job_id
                        WHERE m.doc_id IN (""" + ",".join("?" for _ in doc_ids) + ") LIMIT 1", tuple(doc_ids)).fetchone()
    return row is None


def capture_documents(db, doc_ids):
    """Return updating (None) until this same snapshot has normalized inputs."""
    if not db.in_transaction:
        raise ValueError("Maintenance capture requires an explicit snapshot")
    if not ready(db, doc_ids):
        return None
    placeholders = ",".join("?" for _ in doc_ids)
    rows = db.execute(f"SELECT doc_id,cause_generation FROM maintenance_dirty WHERE doc_id IN ({placeholders})", tuple(doc_ids)) if doc_ids else ()
    return {"nonce": state(db)["rebuild_nonce"], "tokens": {row[0]: row[1] for row in rows}}


def normalize(store, doc_ids=None, *, retries=3):
    """Bounded read/compute/write retries; unrelated jobs are left untouched."""
    for _ in range(retries):
        with store.connect() as db:
            db.execute("BEGIN")
            captured = capture_jobs(db, doc_ids)
        if not captured["jobs"]:
            return True
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if publish_jobs(db, captured):
                return True
    return False


def publish_documents(db, captured, summaries):
    """Publish only a complete computed set at the captured per-document tokens."""
    if not db.in_transaction:
        raise ValueError("Maintenance publication requires an explicit transaction")
    if captured is None:
        return False
    tokens = captured["tokens"]
    if set(tokens) != set(summaries):
        raise ValueError("A projection result must cover exactly its captured dirty documents")
    if state(db)["rebuild_nonce"] != captured["nonce"] or not ready(db, tokens):
        return False
    for identifier, token in tokens.items():
        row = db.execute("SELECT cause_generation FROM maintenance_dirty WHERE doc_id=?", (identifier,)).fetchone()
        if row is None or row[0] != token:
            return False
    for identifier, summary in summaries.items():
        db.execute("DELETE FROM maintenance_projection WHERE doc_id=?", (identifier,))
        db.execute("DELETE FROM maintenance_accepted_paths WHERE doc_id=?", (identifier,))
        if summary is not None:
            db.execute("INSERT INTO maintenance_projection VALUES(?,?,?,?,?,?,?)",
                       (identifier, VERSION, tokens[identifier], summary["path"], int(summary["candidate"]),
                        summary["status"], json.dumps(summary["summary"], ensure_ascii=False, separators=(",", ":"))))
            db.executemany("INSERT INTO maintenance_accepted_paths VALUES(?,?)",
                           [(identifier, path) for path in sorted(set(summary["accepted_paths"]))])
        db.execute("DELETE FROM maintenance_dirty WHERE doc_id=? AND cause_generation=?", (identifier, tokens[identifier]))
        db.execute("DELETE FROM maintenance_dirty_paths WHERE doc_id=?", (identifier,))
    return True

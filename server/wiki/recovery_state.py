"""Durable recovery phases; clearing uncertainty never opens an unfinished handoff."""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime

from . import protocols, storage

TABLES = {
    "filesystem_recoveries": """CREATE TABLE filesystem_recoveries(
        id TEXT PRIMARY KEY NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),
        vault_id TEXT NOT NULL,root TEXT NOT NULL,registration TEXT NOT NULL,
        observer_epoch TEXT NOT NULL,root_generation TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('prepared','mapping_applied','rebuilding','handoff','complete')),
        uncertainty_generation INTEGER NOT NULL DEFAULT 0,
        cutoff INTEGER NOT NULL,plan_hash TEXT,plan TEXT CHECK(plan IS NULL OR json_valid(plan)),
        approved_hash TEXT,evidence TEXT NOT NULL CHECK(json_valid(evidence)),ledger_hash TEXT,
        rebuild_cursor INTEGER NOT NULL DEFAULT 0,mapping_receipt TEXT,completion_receipt TEXT,
        updated_at TEXT NOT NULL)""",
    "filesystem_recovery_audit": """CREATE TABLE filesystem_recovery_audit(
        id TEXT PRIMARY KEY NOT NULL,recovery_id TEXT NOT NULL,generation INTEGER NOT NULL,
        kind TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
}
INDEXES = {
    "filesystem_recovery_active": "CREATE UNIQUE INDEX filesystem_recovery_active ON filesystem_recoveries((1)) WHERE phase<>'complete'",
    "filesystem_recovery_audits": "CREATE INDEX filesystem_recovery_audits ON filesystem_recovery_audit(recovery_id,generation,kind)",
}
WRITER = """SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;"""
TRIGGERS = {}
for table in TABLES:
    name = table + "_insert_guard"
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN {WRITER}
        SELECT CASE WHEN EXISTS(SELECT 1 FROM {table} WHERE id=new.id)
        THEN RAISE(ABORT,'Filesystem recovery facts cannot be replaced') END; END"""
    name = table + "_delete_guard"
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE DELETE ON {table} BEGIN {WRITER}
        SELECT RAISE(ABORT,'Filesystem recovery facts are retained'); END"""
TRIGGERS["filesystem_recovery_audit_immutable"] = f"""CREATE TRIGGER filesystem_recovery_audit_immutable
    BEFORE UPDATE ON filesystem_recovery_audit BEGIN {WRITER}
    SELECT RAISE(ABORT,'Filesystem recovery audit is immutable'); END"""
TRIGGERS["filesystem_recovery_update_guard"] = f"""CREATE TRIGGER filesystem_recovery_update_guard
    BEFORE UPDATE ON filesystem_recoveries BEGIN {WRITER}
    SELECT CASE WHEN new.id<>old.id OR new.root<>old.root OR new.vault_id<>old.vault_id
      OR old.phase='complete'
      OR new.generation<old.generation OR new.generation>old.generation+1
      OR (new.generation>old.generation AND new.phase<>'prepared')
      OR (new.generation=old.generation AND (new.evidence<>old.evidence OR new.registration<>old.registration
        OR new.observer_epoch<>old.observer_epoch OR new.root_generation<>old.root_generation
        OR new.rebuild_cursor<old.rebuild_cursor
        OR new.uncertainty_generation<old.uncertainty_generation
        OR (old.plan IS NOT NULL AND (new.plan IS NOT old.plan OR new.plan_hash IS NOT old.plan_hash OR new.cutoff<>old.cutoff))
        OR (old.mapping_receipt IS NOT NULL AND new.mapping_receipt IS NOT old.mapping_receipt)
        OR (old.phase='prepared' AND new.phase NOT IN ('prepared','mapping_applied'))
        OR (old.phase='mapping_applied' AND new.phase NOT IN ('mapping_applied','rebuilding','handoff'))
        OR (old.phase='rebuilding' AND new.phase NOT IN ('rebuilding','handoff'))
        OR (old.phase='handoff' AND new.phase NOT IN ('handoff','complete'))))
      OR (new.phase<>'prepared' AND (new.plan_hash IS NULL OR new.approved_hash IS NOT new.plan_hash OR new.mapping_receipt IS NULL))
      OR (new.phase='complete' AND new.completion_receipt IS NULL)
      THEN RAISE(ABORT,'Invalid filesystem recovery transition') END; END"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def unavailable():
    from .store import WikiError

    return WikiError("Filesystem recovery ledger is missing or incompatible; durable evidence retained", 503)


def initialize(db):
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    if set(TABLES) & present or db.execute("SELECT 1 FROM wiki_protocols WHERE name='filesystem_recovery'").fetchone():
        raise unavailable()
    for sql in (TABLES | INDEXES | TRIGGERS).values():
        db.execute(sql)
    db.execute("INSERT INTO wiki_protocols VALUES('filesystem_recovery',1)")


def require(db):
    protocols.require(db, "filesystem_recovery")
    actual = dict(db.execute("SELECT name,sql FROM sqlite_master"))
    canonical = lambda sql: re.sub(r"\s+", "", sql or "")  # noqa: E731
    if any(canonical(actual.get(name)) != canonical(sql) for name, sql in (TABLES | INDEXES | TRIGGERS).items()):
        raise unavailable()


def active(db):
    return db.execute("SELECT * FROM filesystem_recoveries WHERE phase<>'complete'").fetchone()


def access(db, root, vault_id, permit=None, *, health):
    """The persistent phase is the admission authority, including cold opens."""
    from .store import WikiError

    try:
        protocols.require(db, "filesystem_recovery")
        row = active(db)
        if permit is not None:
            permit.check(db)
        elif row is not None:
            raise WikiError("Filesystem recovery is unfinished (" + row["phase"] + "); ordinary access stays closed", 503)
        if row is not None and (row["root"], row["vault_id"]) != (str(root), vault_id):
            raise WikiError("Filesystem recovery binding changed; evidence retained", 503)
    except sqlite3.DatabaseError as exc:
        storage.check_error(exc, db, health)
        raise unavailable() from exc


def audit(db, recovery_id, generation, kind, body):
    raw = encoded(body)
    identifier = uuid.uuid4().hex
    db.execute("INSERT INTO filesystem_recovery_audit VALUES(?,?,?,?,?,?,?)", (
        identifier, recovery_id, generation, kind, raw, hashlib.sha256(raw.encode()).hexdigest(), now(),
    ))
    return identifier


def update(db, row, **fields):
    allowed = {"generation", "registration", "observer_epoch", "root_generation", "phase", "uncertainty_generation", "cutoff",
               "plan_hash", "plan", "approved_hash", "evidence", "ledger_hash", "rebuild_cursor", "mapping_receipt", "completion_receipt"}
    if not fields.keys() <= allowed:
        raise ValueError("Unsupported filesystem recovery update")
    fields["updated_at"] = now()
    changed = db.execute("UPDATE filesystem_recoveries SET " + ",".join(key + "=?" for key in fields)
                         + " WHERE id=? AND generation=? AND phase=?", (*fields.values(), row["id"], row["generation"], row["phase"]))
    if changed.rowcount != 1:
        from .store import WikiError

        raise WikiError("Filesystem recovery generation or phase changed", 409)

"""Durable ordered observations and append-only accepted job provenance."""

import base64
import binascii
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from pydantic import ValidationError

from ..config import settings
from . import maintenance_inputs, protocols
from .contracts import ReviewObservation
from .index_state import maintenance_blockers
from .maintenance_contract import PURPOSE, digest
from .maintenance_projection import PENDING_REVIEW, bounds, settle
from .maintenance_view import MaintenanceSnapshot
from .model_response import page as model_page
from .store import WikiError

VERSION = 1
TABLES = {
    "maintenance_selections": """CREATE TABLE maintenance_selections(
        id TEXT PRIMARY KEY NOT NULL,owner_ref TEXT NOT NULL,vault_id TEXT NOT NULL,
        registration_generation TEXT NOT NULL,actor TEXT NOT NULL,purpose TEXT NOT NULL,
        filters TEXT NOT NULL CHECK(json_valid(filters)),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),version INTEGER NOT NULL CHECK(version=1))""",
    "maintenance_selection_items": """CREATE TABLE maintenance_selection_items(
        selection_id TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal>=0),doc_id TEXT NOT NULL,
        path TEXT NOT NULL,revision TEXT NOT NULL,review_json TEXT NOT NULL CHECK(json_valid(review_json)),
        PRIMARY KEY(selection_id,ordinal),UNIQUE(selection_id,doc_id),
        CHECK(json_type(review_json,'$.doc') IS 'text' AND json_extract(review_json,'$.doc')=doc_id),
        CHECK(json_type(review_json,'$.path') IS 'text' AND json_extract(review_json,'$.path')=path),
        CHECK(json_type(review_json,'$.revision') IS 'text' AND json_extract(review_json,'$.revision')=revision),
        CHECK(json_type(review_json,'$.review_key') IS 'text'))""",
    "job_selection_evidence": """CREATE TABLE job_selection_evidence(
        job_id TEXT NOT NULL,selection_id TEXT NOT NULL,selection_ordinal INTEGER NOT NULL,
        doc_id TEXT NOT NULL,review_key TEXT NOT NULL,accepted_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_id,selection_ordinal))""",
}
INDEXES = {
    "maintenance_selection_expiration": "CREATE INDEX maintenance_selection_expiration ON maintenance_selections(expires_at,id)",
    "maintenance_evidence_selection": "CREATE INDEX maintenance_evidence_selection ON job_selection_evidence(selection_id,selection_ordinal)",
}
TRIGGERS = {
    "maintenance_selection_insert": """CREATE TRIGGER maintenance_selection_insert
        BEFORE INSERT ON maintenance_selections BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM maintenance_selections WHERE id=new.id)
            THEN RAISE(ABORT,'Selection observations are immutable') END; END""",
    "maintenance_selection_item_insert": """CREATE TRIGGER maintenance_selection_item_insert
        BEFORE INSERT ON maintenance_selection_items BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM maintenance_selection_items WHERE selection_id=new.selection_id
            AND (ordinal=new.ordinal OR doc_id=new.doc_id))
            THEN RAISE(ABORT,'Selection items are immutable') END; END""",
    "maintenance_evidence_insert": """CREATE TRIGGER maintenance_evidence_insert
        BEFORE INSERT ON job_selection_evidence BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END; END""",
    "maintenance_selection_immutable": """CREATE TRIGGER maintenance_selection_immutable
        BEFORE UPDATE ON maintenance_selections BEGIN SELECT RAISE(ABORT,'Selection observations are immutable'); END""",
    "maintenance_selection_item_immutable": """CREATE TRIGGER maintenance_selection_item_immutable
        BEFORE UPDATE ON maintenance_selection_items BEGIN SELECT RAISE(ABORT,'Selection items are immutable'); END""",
    "maintenance_selection_keep": """CREATE TRIGGER maintenance_selection_keep BEFORE DELETE ON maintenance_selections
        WHEN EXISTS(SELECT 1 FROM job_selection_evidence WHERE selection_id=old.id)
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence retained'); END""",
    "maintenance_selection_item_keep": """CREATE TRIGGER maintenance_selection_item_keep BEFORE DELETE ON maintenance_selection_items
        WHEN EXISTS(SELECT 1 FROM job_selection_evidence WHERE selection_id=old.selection_id AND selection_ordinal=old.ordinal)
        BEGIN SELECT RAISE(ABORT,'Accepted selection item retained'); END""",
    "maintenance_selection_item_owner": """CREATE TRIGGER maintenance_selection_item_owner BEFORE INSERT ON maintenance_selection_items
        WHEN NOT EXISTS(SELECT 1 FROM maintenance_selections WHERE id=new.selection_id AND new.ordinal<candidate_count)
        BEGIN SELECT RAISE(ABORT,'Selection item has no matching header'); END""",
    "maintenance_evidence_owner": """CREATE TRIGGER maintenance_evidence_owner BEFORE INSERT ON job_selection_evidence
        WHEN NOT EXISTS(SELECT 1 FROM maintenance_selection_items i JOIN maintenance_selections s ON s.id=i.selection_id
            WHERE i.selection_id=new.selection_id AND i.ordinal=new.selection_ordinal AND i.doc_id=new.doc_id
            AND json_extract(i.review_json,'$.review_key')=new.review_key)
        OR NOT EXISTS(SELECT 1 FROM wiki_protocols WHERE name='maintenance_jobs' AND version=1)
        BEGIN SELECT RAISE(ABORT,'Accepted evidence has no matching job and observation'); END""",
    "maintenance_evidence_immutable": """CREATE TRIGGER maintenance_evidence_immutable BEFORE UPDATE ON job_selection_evidence
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence is immutable'); END""",
    "maintenance_evidence_keep": """CREATE TRIGGER maintenance_evidence_keep BEFORE DELETE ON job_selection_evidence
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence retained'); END""",
}
JOB_TRIGGER = {"maintenance_evidence_job_owner": """CREATE TRIGGER maintenance_evidence_job_owner
    BEFORE INSERT ON job_selection_evidence WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=new.job_id)
    BEGIN SELECT RAISE(ABORT,'Accepted evidence has no matching job'); END"""}


def utcnow():
    return datetime.now(UTC)


class SelectionError(WikiError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message, status)
        self.response = {"code": code}


def error(code: str, message: str, status: int = 422) -> SelectionError:
    return SelectionError(code, message, status)


def unavailable():
    return error("selection_unavailable", "Selection ledger is missing or incompatible; durable observations retained", 503)


def _sql(value):
    return " ".join(value.split()).rstrip(";")


def initialize(db):
    """Create observations and their job guards in the empty-layout transaction."""
    if not db.in_transaction:
        raise ValueError("Selection initialization requires an explicit transaction")
    for sql in (*TABLES.values(), *INDEXES.values(), *TRIGGERS.values(), *JOB_TRIGGER.values()):
        db.execute(sql)
    db.execute("INSERT INTO wiki_protocols VALUES('maintenance_selections',1)")
    require(db)


def require(db):
    protocols.require(db, "maintenance_selections")
    maintenance_inputs.require_jobs(db)
    expected = TABLES | INDEXES | TRIGGERS | JOB_TRIGGER
    names = expected.keys()
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE name IN (" +
                      ",".join("?" for _ in names) + ")", tuple(names))
    actual = {row[0]: _sql(row[1]) for row in rows}
    if actual != {name: _sql(sql) for name, sql in expected.items()}:
        raise unavailable()


def context(registration, user_id, actor):
    return {"owner_ref": "legacy" if user_id is None else "user:" + user_id,
            "vault_id": registration["vault_id"],
            "registration_generation": registration["registration_generation"],
            "actor": actor, "purpose": PURPOSE}


def _limit(limit):
    value = settings.wiki_selection_page_size if limit is None else limit
    if type(value) is not int or not 1 <= value <= settings.wiki_selection_max_page_size:
        raise error("selection_page_limit", "Use a page size within the configured selection limit")
    return value


def _directory(directory):
    path = PurePosixPath(directory)
    if not directory or path.is_absolute() or any(part in {".", ".."} for part in directory.split("/")) or "\\" in directory:
        raise error("selection_directory_invalid", "Use a literal vault-relative directory")
    return path.as_posix()


def header(db, selection_id, registration, user_id, actor):
    require(db)
    row = db.execute("SELECT * FROM maintenance_selections WHERE id=?", (selection_id,)).fetchone()
    if row is None or row["owner_ref"] != context(registration, user_id, actor)["owner_ref"]:
        raise error("selection_not_found", "Maintenance selection not found", 404)
    expected = context(registration, user_id, actor)
    if row["vault_id"] != expected["vault_id"] or row["registration_generation"] != expected["registration_generation"]:
        raise error("registration_changed", "The Wiki registration changed; create a new selection", 409)
    if row["actor"] != expected["actor"] or row["purpose"] != expected["purpose"]:
        raise error("selection_scope_mismatch", "This selection belongs to another actor or purpose", 403)
    if row["version"] != VERSION:
        raise unavailable()
    try:
        expiration = datetime.fromisoformat(row["expires_at"])
        if expiration.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise unavailable() from exc
    if utcnow() >= expiration:
        raise error("selection_expired", "Selection expired; create a new observation", 410)
    return dict(row)


def _cursor(selection, offset):
    if offset is None:
        return None
    return base64.urlsafe_b64encode(json.dumps({"id": selection["id"], "context": digest(selection),
                                               "ordinal": offset}, separators=(",", ":")).encode()).decode()


def _offset(selection, cursor):
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        offset = value["ordinal"]
        if (value["id"] != selection["id"] or value["context"] != digest(selection)
                or type(offset) is not int or not 0 <= offset <= selection["candidate_count"]):
            raise ValueError()
        return offset
    except (KeyError, ValueError, TypeError, binascii.Error) as exc:
        raise error("selection_cursor_invalid", "Cursor does not match this frozen selection") from exc


def _item(row):
    try:
        review = ReviewObservation.model_validate_json(row["review_json"]).model_dump()
    except ValidationError as exc:
        raise unavailable() from exc
    if (review["doc"], review["path"], review["revision"]) != (row["doc_id"], row["path"], row["revision"]):
        raise unavailable()
    return {"ordinal": row["ordinal"], "doc_id": row["doc_id"], "path": row["path"],
            "observed_revision": row["revision"], "review": review}


def item(db, selection_id, ordinal):
    if type(ordinal) is not int or ordinal < 0:
        raise error("selection_ordinal_invalid", "Use the selected item's exact ordinal")
    row = db.execute("SELECT * FROM maintenance_selection_items WHERE selection_id=? AND ordinal=?",
                     (selection_id, ordinal)).fetchone()
    if row is None:
        raise error("selection_ordinal_invalid", "Selection item ordinal does not exist")
    return _item(row)


def _page(db, selection, *, limit=None, cursor=None, deferred=None):
    count = _limit(limit)
    offset = _offset(selection, cursor)
    rows = db.execute("""SELECT * FROM maintenance_selection_items
        WHERE selection_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?""", (selection["id"], offset, count)).fetchall()
    expected = list(range(offset, min(offset + count, selection["candidate_count"])))
    if [row["ordinal"] for row in rows] != expected:
        raise unavailable()
    items = [_item(row) for row in rows]
    base = {key: selection[key] for key in ("id", "vault_id", "registration_generation", "created_at", "expires_at", "candidate_count")}
    base |= {"purpose": selection["purpose"], "filters": json.loads(selection["filters"])}
    if deferred is not None:
        base |= {"deferred_count": len(deferred), "deferred": deferred[:3], "deferred_truncated": len(deferred) > 3}
    return model_page(items, base, offset, selection["candidate_count"], lambda following: _cursor(selection, following))


def create(store, registration, user_id, *, actor="human", directory="Library", limit=None, authorize=None):
    """Caller holds history/account/registration locks, before any final write TX."""
    directory, limit = _directory(directory), _limit(limit)
    store.synchronize()
    settled = settle(store, directory=directory)
    try:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db)
            snapshot = MaintenanceSnapshot(store, registration, user_id, db, actor)
            if not settled or not snapshot.scope_ready(directory=directory):
                raise error("selection_updating", "Current maintenance observations must settle before selecting", 503)
            rows = db.execute(f"""SELECT summary_json FROM maintenance_projection
                WHERE {PENDING_REVIEW} AND path>=? AND path<? ORDER BY path,doc_id LIMIT ?""",
                              (*bounds(directory), settings.wiki_selection_max_candidates + 1)).fetchall()
            if len(rows) > settings.wiki_selection_max_candidates:
                raise error("selection_limit_exceeded", "Too many candidates; narrow the directory before creating a selection")
            observed = [json.loads(row["summary_json"]) for row in rows]
            blocked = maintenance_blockers(db, [value["current"]["id"] for value in observed if value["actionable"]])
            executable, deferred = [], []
            for value in observed:
                if value["actionable"] and value["current"]["id"] not in blocked:
                    executable.append(value)
                else:
                    deferred.append({key: value[key] for key in ("current", "status", "source_warnings", "attempts", "attempt_count")})
            now = utcnow()
            selection = {"id": uuid.uuid4().hex, **context(registration, user_id, actor),
                         "filters": json.dumps({"directory": directory}, sort_keys=True),
                         "created_at": now.isoformat(),
                         "expires_at": (now + timedelta(seconds=settings.wiki_selection_ttl_seconds)).isoformat(),
                         "candidate_count": len(executable), "version": VERSION}
            db.execute("INSERT INTO maintenance_selections VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(selection.values()))
            for ordinal, value in enumerate(executable):
                review = snapshot.review(value)
                db.execute("INSERT INTO maintenance_selection_items VALUES(?,?,?,?,?,?)",
                           (selection["id"], ordinal, review["doc"], review["path"], review["revision"],
                            json.dumps(review, ensure_ascii=False, sort_keys=True)))
            result = _page(db, selection, limit=limit, deferred=deferred)
            if authorize is not None:
                authorize()
            return result
    except (sqlite3.DatabaseError, KeyError, json.JSONDecodeError) as exc:
        raise unavailable() from exc


def read(store, registration, user_id, selection_id, *, actor="human", limit=None, cursor=None):
    try:
        with store.connect() as db:
            db.execute("BEGIN")
            return _page(db, header(db, selection_id, registration, user_id, actor), limit=limit, cursor=cursor)
    except sqlite3.DatabaseError as exc:
        raise unavailable() from exc


def validate_pair(selection_id, ordinal, review):
    if (selection_id is None) != (ordinal is None):
        raise error("selection_pair_required", "Supply selection ID and ordinal together")
    if selection_id is not None and review is None:
        raise error("selection_review_required", "Selection admission requires the complete frozen review")


def check_item(db, selection_id, ordinal, review, registration, user_id, actor):
    header(db, selection_id, registration, user_id, actor)
    selected = item(db, selection_id, ordinal)
    if selected["review"] != review:
        raise error("selection_review_mismatch", "Use the complete review from this selection item", 409)
    return selected


def accept(db, job_id, selection_id, ordinal, review):
    if not db.in_transaction:
        raise ValueError("Selection provenance must commit with job admission")
    db.execute("""INSERT INTO job_selection_evidence VALUES(?,?,?,?,?,?)
        ON CONFLICT(job_id,selection_id,selection_ordinal) DO NOTHING""",
               (job_id, selection_id, ordinal, review["doc"], review["review_key"], utcnow().isoformat()))


def evidence(db, job_id):
    require(db)
    result = []
    for row in db.execute("SELECT * FROM job_selection_evidence WHERE job_id=? ORDER BY accepted_at,selection_id,selection_ordinal", (job_id,)):
        value = dict(row)
        value["review"] = item(db, row["selection_id"], row["selection_ordinal"])["review"]
        result.append(value)
    return result


def cleanup(db):
    """Remove expired unaccepted observations; accepted evidence stays immutable."""
    if not db.in_transaction:
        raise ValueError("Selection cleanup requires an explicit transaction")
    require(db)
    condition = """SELECT id FROM maintenance_selections s WHERE expires_at<=?
                   AND NOT EXISTS(SELECT 1 FROM job_selection_evidence e WHERE e.selection_id=s.id)"""
    cutoff = utcnow().isoformat()
    db.execute("DELETE FROM maintenance_selection_items WHERE selection_id IN (" + condition + ")", (cutoff,))
    db.execute("DELETE FROM maintenance_selections WHERE id IN (" + condition + ")", (cutoff,))

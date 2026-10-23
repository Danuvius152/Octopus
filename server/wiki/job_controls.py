"""Transport receipts for explicit job controls, committed with their effect.

The job remains the outcome authority. A receipt only remembers which original
control was accepted, so a lost HTTP response cannot start another attempt.
"""

import hashlib
import json

from .store import WikiError

TABLE = """CREATE TABLE wiki_job_controls(
    job_id TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
    PRIMARY KEY(job_id,operation_id))"""


def initialize(db):
    db.execute(TABLE)


def require(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='wiki_job_controls'").fetchone()
    if row is None or "".join(row[0].split()) != "".join(TABLE.split()):
        raise WikiError("Wiki control receipts are unavailable; original work retained", 503)


def identity(operation_id, action, request):
    if operation_id is None:
        return None
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
        raise WikiError("A control requires a stable operation identity", 422)
    raw = json.dumps([action, request], ensure_ascii=False, sort_keys=True)
    return operation_id, hashlib.sha256(raw.encode()).hexdigest()


def replay(db, job_id, control):
    if control is None:
        return False
    require(db)
    row = db.execute("SELECT request_hash FROM wiki_job_controls WHERE job_id=? AND operation_id=?",
                     (job_id, control[0])).fetchone()
    if row is not None and row[0] != control[1]:
        raise WikiError("This operation identity already names another control", 409)
    return row is not None


def record(db, job_id, control):
    if control is not None and not replay(db, job_id, control):
        if not db.in_transaction:
            raise ValueError("Control receipt must commit with its effect")
        db.execute("INSERT INTO wiki_job_controls VALUES(?,?,?)", (job_id, *control))

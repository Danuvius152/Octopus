"""Transport receipts for explicit job controls, committed with their effect.

The job remains the outcome authority. A receipt only remembers which original
control was accepted, so a lost HTTP response cannot start another attempt.
"""

import hashlib
import json

from .store import WikiError


async def control(user_id, job_id, action, options, *, manager=None, mcp_scope=None, app_id=None):
    """Authorize and control the original job; dispatch and retirement stay owned."""
    from . import job_events
    from .job_returns import dispatch_gate
    from .jobs import WikiJobs, _io, worker
    from .registry import registry
    from .tasks import tasks

    jobs = await _io(WikiJobs, user_id)

    def authorize():
        with registry.registered(user_id, jobs.store, jobs.registration):
            task = tasks.caller(user_id, mcp_scope, jobs.registration, app_id=app_id,
                                write=True, human_only=action == "readmit")
            jobs.require_job_caller(job_id, task, mcp_scope)
            return task

    with jobs.store.borrow():
        task = await _io(authorize)
        requester = job_events.requester_for(user_id, task, app_id)
        if action == "cancel":
            result = await jobs.cancel(job_id, worker.manager, task=task, mcp_scope=mcp_scope, **options)
        elif action == "verify":
            result = await jobs.verify(job_id, worker.manager, task=task, mcp_scope=mcp_scope, **options)
        elif action in {"retry", "readmit"}:
            async with dispatch_gate(manager, user_id):
                if action == "retry":
                    result = await _io(jobs.retry, job_id, requester=None if task and task.job_id else requester,
                                       task=task, mcp_scope=mcp_scope, **options)
                else:
                    result = await _io(jobs.readmit, job_id, requester=requester, **options)
        else:
            raise ValueError("Unknown Wiki job control")
    return ({key: result[key] for key in ("id", "status", "state_version", "attempt", "settlement_phase", "progress")}
            if task is not None else result)

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

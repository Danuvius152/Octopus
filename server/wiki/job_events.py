"""Job control and transactional notices; jobs and terminal records stay authoritative."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import job_items, maintenance_inputs, protocols
from .store import WikiError

TABLES = {
    "wiki_job_state": """CREATE TABLE wiki_job_state(
        job_id TEXT PRIMARY KEY NOT NULL,state_version INTEGER NOT NULL CHECK(state_version>0),
        attempt INTEGER NOT NULL CHECK(attempt>=0),settlement_phase TEXT NOT NULL
        CHECK(settlement_phase IN ('none','pending','confirmed','attention')))""",
    "wiki_job_outbox": """CREATE TABLE wiki_job_outbox(
        event_id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,state_version INTEGER NOT NULL,
        body TEXT NOT NULL CHECK(json_valid(body)),created_at TEXT NOT NULL,
        UNIQUE(job_id,state_version))""",
    "wiki_job_subscribers": """CREATE TABLE wiki_job_subscribers(
        id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,owner TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('chat','app','schedule','human')),
        target TEXT NOT NULL,request_id TEXT NOT NULL,client_id TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(job_id,owner,kind,target,client_id))""",
    "wiki_job_deliveries": """CREATE TABLE wiki_job_deliveries(
        event_id TEXT NOT NULL,subscriber_id TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
        outcome TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(event_id,subscriber_id))""",
}
INDEXES = {
    "wiki_job_delivery_pending": "CREATE INDEX wiki_job_delivery_pending ON wiki_job_deliveries(event_id,subscriber_id) WHERE acknowledged=0",
    "wiki_job_subscriber_target": "CREATE INDEX wiki_job_subscriber_target ON wiki_job_subscribers(owner,target,job_id)",
}
WRITER = """SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;"""
TRIGGERS = {}
for table in TABLES:
    key = ("event_id=new.event_id AND subscriber_id=new.subscriber_id" if table == "wiki_job_deliveries"
           else "job_id=new.job_id" if table == "wiki_job_state"
           else "event_id=new.event_id" if table == "wiki_job_outbox" else "id=new.id")
    name = table + "_insert_guard"
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN {WRITER}
        SELECT CASE WHEN EXISTS(SELECT 1 FROM {table} WHERE {key})
        THEN RAISE(ABORT,'Wiki job event facts cannot be replaced') END; END"""
    name = table + "_delete_guard"
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE DELETE ON {table} BEGIN {WRITER}
        SELECT RAISE(ABORT,'Wiki job event facts are retained'); END"""
for table in ("wiki_job_outbox", "wiki_job_subscribers"):
    name = table + "_update_guard"
    TRIGGERS[name] = f"""CREATE TRIGGER {name} BEFORE UPDATE ON {table} BEGIN {WRITER}
        SELECT RAISE(ABORT,'Wiki job event facts are immutable'); END"""
TRIGGERS["wiki_job_state_update_guard"] = f"""CREATE TRIGGER wiki_job_state_update_guard
    BEFORE UPDATE ON wiki_job_state BEGIN {WRITER}
    SELECT CASE WHEN new.job_id<>old.job_id OR new.state_version<>old.state_version+1
      OR new.attempt<old.attempt OR new.attempt>old.attempt+1
      THEN RAISE(ABORT,'Invalid Wiki job state transition') END; END"""
TRIGGERS["wiki_job_delivery_update_guard"] = f"""CREATE TRIGGER wiki_job_delivery_update_guard
    BEFORE UPDATE ON wiki_job_deliveries BEGIN {WRITER}
    SELECT CASE WHEN new.event_id<>old.event_id OR new.subscriber_id<>old.subscriber_id
      OR old.acknowledged=1 OR new.acknowledged<>1
      THEN RAISE(ABORT,'Wiki delivery acknowledgement cannot be replaced') END; END"""
TRIGGERS["wiki_job_outbox_deliver"] = """CREATE TRIGGER wiki_job_outbox_deliver
    AFTER INSERT ON wiki_job_outbox BEGIN
    INSERT INTO wiki_job_deliveries(event_id,subscriber_id)
      SELECT new.event_id,id FROM wiki_job_subscribers WHERE job_id=new.job_id; END"""
TRIGGERS["wiki_job_subscriber_current"] = """CREATE TRIGGER wiki_job_subscriber_current
    AFTER INSERT ON wiki_job_subscribers BEGIN
    INSERT INTO wiki_job_deliveries(event_id,subscriber_id)
      SELECT o.event_id,new.id FROM wiki_job_outbox o JOIN wiki_job_state s
        ON s.job_id=o.job_id AND s.state_version=o.state_version WHERE o.job_id=new.job_id; END"""
TRIGGERS["wiki_job_delivery_owner"] = """CREATE TRIGGER wiki_job_delivery_owner
    BEFORE INSERT ON wiki_job_deliveries WHEN NOT EXISTS(
      SELECT 1 FROM wiki_job_outbox o JOIN wiki_job_subscribers s ON s.job_id=o.job_id
      WHERE o.event_id=new.event_id AND s.id=new.subscriber_id)
    BEGIN SELECT RAISE(ABORT,'Wiki delivery requires its original subscriber'); END"""


def canonical(sql):
    return re.sub(r"\s+", "", sql or "")


def unavailable():
    return WikiError("Wiki job notification ledger is missing or incompatible; durable state retained", 503)


def _emit(job, *, legacy=False):
    # Full paths/identities are never shortened. Oversize references are omitted
    # explicitly; the existing full job endpoint retains the complete evidence.
    # Applied receipts remain visible even when cancellation prevents a later
    # item update. Prepared receipts are counted separately, never as outputs.
    state = "json_extract(value,'$.status')" if legacy else job_items.sql_state()
    output_state = "json_extract(i.value,'$.status')" if legacy else job_items.sql_state("i.value")
    applied = f"""SELECT r.doc_id AS doc,r.path AS path,r.output_revision AS revision
        FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id
        WHERE r.job_id=j.id AND r.output_revision IS NOT NULL AND coalesce(c.status,'applied')='applied'
        UNION SELECT coalesce(json_extract(i.value,'$.output_doc'),json_extract(i.value,'$.doc')),
          json_extract(i.value,'$.output'),json_extract(i.value,'$.output_revision')
        FROM json_each(j.items) i WHERE json_type(i.value,'$.output')='text'
          AND json_type(i.value,'$.output_revision')='text'
          AND {output_state}='done'"""
    reference = "json_object('doc_id',doc,'path',path,'revision',revision)"
    refs = f"""SELECT {reference} AS value FROM ({applied})
        WHERE length(CAST({reference} AS BLOB))<=1024 ORDER BY path,doc,revision LIMIT 10"""
    outputs = f"(SELECT count(*) FROM ({applied}))"
    recording_pending = ("json_extract(value,'$.recording_failure.request_id')" if legacy
                         else job_items.sql_pending_request())
    recording_filter = "json_type(value,'$.recording_failure')='object'" if legacy else "1"
    attention = "='attention'" if legacy else " IN ('attention','unknown','verifying','excluded')"
    return f"""INSERT INTO wiki_job_outbox
        SELECT lower(hex(randomblob(16))),j.id,s.state_version,
          json_object('job_id',j.id,'state_version',s.state_version,'attempt',s.attempt,
            'status',j.status,'settlement_phase',s.settlement_phase,'worker_session_id',j.session_id,
            'scope',substr(coalesce(json_extract(j.payload,'$.scope'),''),1,512),
            'scope_truncated',length(coalesce(json_extract(j.payload,'$.scope'),''))>512,
            'progress',json_object('total',json_array_length(j.items),
              'completed',(SELECT count(*) FROM json_each(j.items) WHERE {state}='done'),
              'attention',(SELECT count(*) FROM json_each(j.items) WHERE {state}{attention}),
              'failed',(SELECT count(*) FROM json_each(j.items) WHERE {state}='failed')),
            'outcome_refs',json((SELECT coalesce(json_group_array(json(value)),'[]') FROM ({refs}))),
            'outcome_count',{outputs},
            'outcome_refs_truncated',{outputs}>(SELECT count(*) FROM ({refs})),
            'effects',json_object(
              'recorded',(SELECT count(*) FROM operation_receipts WHERE job_id=j.id),
              'applied',(SELECT count(*) FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id
                WHERE r.job_id=j.id AND coalesce(c.status,'applied')='applied'),
              'index_pending',(SELECT count(*) FROM job_publications p JOIN changes c ON c.id=p.change_id
                WHERE p.job_id=j.id AND p.indexed=0 AND c.status='applied')),
            'recording_pending',(SELECT count(DISTINCT {recording_pending})
              FROM json_each(j.items) WHERE {recording_filter}),
            'error',substr(j.error,1,512),'error_truncated',length(coalesce(j.error,''))>512,
            'created_at',j.created_at,'updated_at',j.updated_at),
          strftime('%Y-%m-%dT%H:%M:%fZ','now')
        FROM jobs j JOIN wiki_job_state s ON s.job_id=j.id WHERE j.id={job};"""


def _bump(selection):
    return f"UPDATE wiki_job_state SET state_version=state_version+1 WHERE job_id IN ({selection});"


def job_triggers(*, legacy=False):
    unresolved = "unconfirmed" if legacy else "attention"
    result = {
        "wiki_job_state_owner": """CREATE TRIGGER wiki_job_state_owner BEFORE INSERT ON wiki_job_state
            WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=new.job_id)
            BEGIN SELECT RAISE(ABORT,'Wiki job state requires its job'); END""",
        "wiki_job_subscriber_owner": """CREATE TRIGGER wiki_job_subscriber_owner BEFORE INSERT ON wiki_job_subscribers
            WHEN NOT EXISTS(SELECT 1 FROM jobs WHERE id=new.job_id)
            BEGIN SELECT RAISE(ABORT,'Wiki subscriber requires its job'); END""",
        "wiki_job_insert_state": f"""CREATE TRIGGER wiki_job_insert_state AFTER INSERT ON jobs BEGIN {WRITER}
            UPDATE wiki_job_state SET state_version=state_version+1 WHERE job_id=new.id;
            INSERT INTO wiki_job_state
              SELECT new.id,1,CASE WHEN new.status='queued' THEN 0 ELSE 1 END,
                CASE WHEN new.status='cancelled' THEN '{unresolved}' ELSE 'none' END
              WHERE NOT EXISTS(SELECT 1 FROM wiki_job_state WHERE job_id=new.id); END""",
        "wiki_job_update_state": f"""CREATE TRIGGER wiki_job_update_state AFTER UPDATE ON jobs
            WHEN new.status IS NOT old.status OR new.items IS NOT old.items
              OR new.error IS NOT old.error OR new.session_id IS NOT old.session_id
            BEGIN {WRITER}
            UPDATE wiki_job_state SET state_version=state_version+1,
              attempt=attempt+CASE WHEN old.status='queued' AND new.status='running' THEN 1 ELSE 0 END,
              settlement_phase=CASE WHEN new.status='cancelled' AND old.status<>'cancelled' THEN 'pending'
                WHEN new.status='queued' AND old.status<>'queued' THEN 'none' ELSE settlement_phase END
              WHERE job_id=new.id; END""",
        "wiki_job_state_emit_insert": "CREATE TRIGGER wiki_job_state_emit_insert AFTER INSERT ON wiki_job_state BEGIN " + _emit("new.job_id", legacy=legacy) + " END",
        "wiki_job_state_emit_update": "CREATE TRIGGER wiki_job_state_emit_update AFTER UPDATE ON wiki_job_state BEGIN " + _emit("new.job_id", legacy=legacy) + " END",
    }
    for table in ("operation_receipts", "job_publications"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            sides = ("old", "new") if action == "UPDATE" else ("old",) if action == "DELETE" else ("new",)
            jobs = " UNION ".join(f"SELECT {side}.job_id" for side in sides)
            columns = ("id", "job_id", "item_doc", "doc_id", "path", "input_revision", "output_revision",
                       "operation", "reason", "change_id", "created_at", "details") if table == "operation_receipts" else (
                           "job_id", "source_doc", "input_revision", "change_id", "output_path", "output_revision", "indexed")
            changed = " WHEN " + " OR ".join(f"new.{key} IS NOT old.{key}" for key in columns) if action == "UPDATE" else ""
            name = f"wiki_job_event_{table}_{action.lower()}"
            result[name] = f"CREATE TRIGGER {name} AFTER {action} ON {table}{changed} BEGIN {WRITER}" + _bump(jobs) + " END"
    result["wiki_job_event_change_status"] = f"""CREATE TRIGGER wiki_job_event_change_status
        AFTER UPDATE OF status ON changes WHEN new.status IS NOT old.status BEGIN {WRITER}""" + _bump(
            "SELECT job_id FROM operation_receipts WHERE change_id=new.id UNION SELECT job_id FROM job_publications WHERE change_id=new.id"
        ) + " END"
    return result


def initialize(db):
    """The schema owner calls this once, in its versioned migration transaction."""
    if not db.in_transaction:
        raise ValueError("Wiki job events require an explicit migration transaction")
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    if set(TABLES) & present or db.execute("SELECT 1 FROM wiki_protocols WHERE name='wiki_job_events'").fetchone():
        raise unavailable()
    for sql in TABLES.values():
        db.execute(sql)
    for sql in INDEXES.values():
        db.execute(sql)
    for sql in TRIGGERS.values():
        db.execute(sql)
    db.execute("INSERT INTO wiki_protocols VALUES('wiki_job_events',2)")
    if maintenance_inputs.jobs_initialized(db):
        attach_jobs(db)


def attach_jobs(db):
    """Install job-dependent triggers only after the actual jobs DDL owner commits them."""
    for sql in job_triggers().values():
        db.execute(sql)
    db.execute("""INSERT INTO wiki_job_state
        SELECT id,1,CASE WHEN status='queued' THEN 0 ELSE 1 END,
          CASE WHEN status='cancelled' THEN 'attention' ELSE 'none' END FROM jobs""")


def require(db, *, legacy=False):
    protocols.require(db, "wiki_job_events", 1 if legacy else 2)
    with_jobs = maintenance_inputs.jobs_initialized(db)
    tables = TABLES | ({"wiki_job_state": TABLES["wiki_job_state"].replace("'attention'", "'unconfirmed'")} if legacy else {})
    expected = tables | INDEXES | TRIGGERS | (job_triggers(legacy=legacy) if with_jobs else {})
    actual = dict(db.execute("SELECT name,sql FROM sqlite_master"))
    if any(canonical(actual.get(name)) != canonical(sql) for name, sql in expected.items()):
        raise unavailable()
    installed = {name for name in actual if name in job_triggers()}
    if installed != (set(job_triggers()) if with_jobs else set()):
        raise unavailable()
    if with_jobs and db.execute("SELECT 1 FROM jobs j LEFT JOIN wiki_job_state s ON s.job_id=j.id WHERE s.job_id IS NULL LIMIT 1").fetchone():
        raise unavailable()


def upgrade(db):
    """One forward transaction preserves events and upgrades their current view."""
    require(db, legacy=True)
    with_jobs = maintenance_inputs.jobs_initialized(db)
    for name in TRIGGERS | (job_triggers() if with_jobs else {}):
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute("ALTER TABLE wiki_job_state RENAME TO wiki_job_state_v1")
    db.execute(TABLES["wiki_job_state"])
    db.execute("""INSERT INTO wiki_job_state SELECT job_id,state_version,attempt,
        CASE WHEN settlement_phase='unconfirmed' THEN 'attention' ELSE settlement_phase END FROM wiki_job_state_v1""")
    db.execute("DROP TABLE wiki_job_state_v1")
    for sql in TRIGGERS.values():
        db.execute(sql)
    if with_jobs:
        for sql in job_triggers().values():
            db.execute(sql)
    db.execute("UPDATE wiki_protocols SET version=2 WHERE name='wiki_job_events'")


def metadata(db, job_id):
    row = db.execute("SELECT state_version,attempt,settlement_phase FROM wiki_job_state WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise unavailable()
    return dict(row)


@dataclass(frozen=True)
class Requester:
    owner: str | None
    kind: str
    target: str = ""
    request_id: str = ""
    client_id: str = ""


def from_task(task):
    """The host captured this turn's original entry before any model ran."""
    return Requester(task.user_id, "schedule" if task.origin == "schedule" else "chat",
                     task.session_id, task.request_id, task.app_id or "")


def subscribe(db, job_id, requester):
    """Caller has verified the principal and holds the admission writer TX."""
    if requester is None:
        return None
    if not db.in_transaction or not isinstance(requester, Requester):
        raise ValueError("Subscription requires a trusted requester and admission transaction")
    owner = requester.owner if requester.owner is not None else "legacy"
    identity = [job_id, owner, requester.kind, requester.target, requester.client_id]
    identifier = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    row = db.execute("SELECT * FROM wiki_job_subscribers WHERE id=?", (identifier,)).fetchone()
    if row is None:
        db.execute("INSERT INTO wiki_job_subscribers VALUES(?,?,?,?,?,?,?,?)",
                   (identifier, job_id, owner, requester.kind, requester.target, requester.request_id,
                    requester.client_id, datetime.now(UTC).isoformat()))
    # Repeated subscriptions do not create a new version or an extra receiver.
    # A subscriber arriving after other receivers ACKed still gets this snapshot.
    db.execute("""INSERT INTO wiki_job_deliveries(event_id,subscriber_id)
        SELECT o.event_id,? FROM wiki_job_outbox o JOIN wiki_job_state s
          ON s.job_id=o.job_id AND s.state_version=o.state_version WHERE o.job_id=?
          AND NOT EXISTS(SELECT 1 FROM wiki_job_deliveries d WHERE d.event_id=o.event_id AND d.subscriber_id=?)""",
               (identifier, job_id, identifier))
    return identifier


def acknowledge(db, event_id, subscriber_id, outcome):
    row = db.execute("SELECT acknowledged,outcome FROM wiki_job_deliveries WHERE event_id=? AND subscriber_id=?",
                     (event_id, subscriber_id)).fetchone()
    if row is None:
        raise WikiError("Wiki delivery not found", 404)
    if row["acknowledged"]:
        if row["outcome"] != outcome:
            raise WikiError("Wiki delivery acknowledgement conflicts with its original result", 409)
        return
    db.execute("UPDATE wiki_job_deliveries SET acknowledged=1,outcome=? WHERE event_id=? AND subscriber_id=?",
               (outcome, event_id, subscriber_id))


def settlement(db, job_id, attempt, phase):
    """Change only the captured cancellation; a later attempt cannot be settled."""
    current = metadata(db, job_id)
    row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if current["attempt"] != attempt or row is None or row[0] != "cancelled":
        raise WikiError("The original maintenance cancellation changed", 409)
    # A slower cancellation observer may still hold an earlier unconfirmed
    # result. Confirmation is final for this attempt; retry clears it through
    # the separate jobs transition before another attempt can be claimed.
    if phase == current["settlement_phase"] or current["settlement_phase"] == "confirmed":
        return
    db.execute("UPDATE wiki_job_state SET state_version=state_version+1,settlement_phase=? WHERE job_id=? AND attempt=?",
               (phase, job_id, attempt))


def pending(db, limit=50, *, after=None, through=None):
    conditions, parameters = ["d.acknowledged=0"], []
    if after is not None:
        conditions.append("(o.rowid>? OR (o.rowid=? AND s.id>?))")
        parameters.extend((after[0], *after))
    if through is not None:
        conditions.append("o.rowid<=?")
        parameters.append(through)
    rows = db.execute("""SELECT o.rowid AS outbox_order,o.event_id,o.job_id,o.state_version,o.body,s.id AS subscriber_id,
        s.owner,s.kind,s.target,s.request_id,s.client_id FROM wiki_job_deliveries d
        JOIN wiki_job_outbox o ON o.event_id=d.event_id JOIN wiki_job_subscribers s ON s.id=d.subscriber_id
        WHERE """ + " AND ".join(conditions) + " ORDER BY o.rowid,s.id LIMIT ?", (*parameters, limit)).fetchall()
    return [dict(row) for row in rows]

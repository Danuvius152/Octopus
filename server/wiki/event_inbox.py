"""Durable receipt, atomic application, and idempotent ack of filesystem events.

The vault lock and observer lease serialize producers. Intake commits on its
own connection, before the index transaction. Applied markers use the index
connection, so rollback cannot lose receipt or commit replay a deletion twice.
"""

import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime

from ..service_health import ServiceHealth
from . import storage
from .store import WikiError

PROTOCOL = 1


class EventInbox:
    def __init__(self, db_path, root, vault_id, *, recovery=None, health=None):
        self.health = health if health is not None else ServiceHealth()
        self.db_path = db_path
        self.root = str(root)
        self.vault_id = vault_id
        self.recovery = recovery
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,?,?,?)", (PROTOCOL, self.root, self.vault_id))
            self.check(db, allow_armed=recovery is not None)

    @contextlib.contextmanager
    def connect(self):
        with storage.connect(self.db_path, self.health) as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def state(self, db):
        """Validate receipt continuity without discarding uncertain evidence."""
        state = db.execute("SELECT * FROM filesystem_state WHERE singleton=1").fetchone()
        if state is None or (state["protocol"], state["root"], state["vault_id"]) != (PROTOCOL, self.root, self.vault_id):
            raise WikiError("Filesystem inbox binding/protocol changed; durable evidence retained", 503)
        if state["applied_seq"] > state["received_seq"]:
            raise WikiError("Invalid filesystem inbox watermark; durable evidence retained", 503)
        pending = db.execute("SELECT count(*),min(seq),max(seq) FROM filesystem_inbox WHERE status='pending'").fetchone()
        count = state["received_seq"] - state["applied_seq"]
        if pending[0] != count or (count and (pending[1], pending[2]) != (state["applied_seq"] + 1, state["received_seq"])):
            raise WikiError("Filesystem inbox receipt sequence is incomplete; durable evidence retained", 503)
        return state

    def check(self, db, *, allow_armed=False):
        from . import recovery_state

        state = self.state(db)
        recovery_state.access(db, self.root, self.vault_id, self.recovery, health=self.health)
        if state["uncertain"] and not (self.recovery is not None and self.recovery.intake):
            raise WikiError("Filesystem history is uncertain; inspect the durable inbox before reopening writes: " + state["uncertain"], 503)
        if state["armed_epoch"] and not allow_armed:
            raise WikiError("Filesystem reception was interrupted before durable receipt; history is uncertain", 503)
        return state

    def arm(self, epoch):
        with self.connect() as db:
            self.check(db)
            db.execute("UPDATE filesystem_state SET armed_epoch=? WHERE singleton=1", (epoch,))

    def receive(self, records, epoch, root_generation, *, armed=True, batch_id=None):
        payload = json.dumps({"protocol": PROTOCOL, "root": self.root, "vault_id": self.vault_id, "root_generation": root_generation, "records": records}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        batch_id = batch_id or uuid.uuid4().hex
        with self.connect() as db:
            state = self.check(db, allow_armed=armed)
            if armed and state["armed_epoch"] != epoch:
                raise WikiError("Filesystem receipt has no matching armed read", 503)
            previous = db.execute("SELECT seq,payload_hash FROM filesystem_inbox WHERE batch_id=?", (batch_id,)).fetchone()
            if previous is not None:
                if previous["payload_hash"] != digest:
                    raise WikiError("Filesystem batch ID reused with different payload; evidence retained", 503)
                seq = previous["seq"]
            elif records:
                seq = state["received_seq"] + 1
                db.execute("INSERT INTO filesystem_inbox VALUES(?,?,?,?,?,?,?,?)", (seq, batch_id, epoch, root_generation, payload, digest, "pending", datetime.now(UTC).isoformat()))
                db.execute("UPDATE filesystem_state SET received_seq=? WHERE singleton=1", (seq,))
            else:
                seq = None
            if armed:
                db.execute("UPDATE filesystem_state SET armed_epoch=NULL WHERE singleton=1")
        return seq

    def pending(self):
        with self.connect() as db:
            self.check(db)
            return self.pending_rows(db)

    def pending_rows(self, db):
        """Also usable by owner metadata inspection on its read-only snapshot."""
        state = self.state(db)
        rows = db.execute("SELECT * FROM filesystem_inbox WHERE status='pending' ORDER BY seq").fetchall()
        result = []
        for row in rows:
            raw = row["payload"]
            if row["seq"] <= state["applied_seq"] or hashlib.sha256(raw.encode()).hexdigest() != row["payload_hash"]:
                raise WikiError("Filesystem inbox payload/watermark conflict; evidence retained", 503)
            try:
                payload = json.loads(raw)
                if (payload["protocol"], payload["root"], payload["vault_id"], payload["root_generation"]) != (PROTOCOL, self.root, self.vault_id, row["root_generation"]) or not isinstance(payload["records"], list):
                    raise ValueError("binding/protocol")
            except (ValueError, KeyError, TypeError) as exc:
                raise WikiError("Invalid filesystem receipt; durable evidence retained", 503) from exc
            result.append((dict(row), payload["records"]))
        return result

    def applied(self, sequences):
        with self.connect() as db:
            self.check(db)
            if not sequences:
                return False
            placeholders = ",".join("?" for _ in sequences)
            rows = db.execute(f"SELECT status FROM filesystem_inbox WHERE seq IN ({placeholders})", sequences).fetchall()
        if len(rows) != len(sequences):
            raise WikiError("Filesystem receipt missing; evidence retained", 503)
        if all(row[0] == "applied" for row in rows):
            return True
        if any(row[0] == "applied" for row in rows):
            raise WikiError("Partially applied filesystem batch group; evidence retained", 503)
        return False

    def mark_applied(self, db, sequences):
        if not sequences:
            return
        state = self.check(db)
        selected = sorted(set(sequences))
        pending = [row[0] for row in db.execute("SELECT seq FROM filesystem_inbox WHERE status='pending' AND seq<=? ORDER BY seq", (selected[-1],))]
        if pending != selected or selected[0] <= state["applied_seq"]:
            raise WikiError("Filesystem inbox application order changed; evidence retained", 503)
        placeholders = ",".join("?" for _ in selected)
        db.execute(f"UPDATE filesystem_inbox SET status='applied' WHERE seq IN ({placeholders})", selected)
        db.execute("UPDATE filesystem_state SET applied_seq=? WHERE singleton=1", (selected[-1],))

    def acknowledge(self, sequences):
        if not sequences:
            return
        # Keep receipt identities/hashes so a resent ID cannot carry new data.
        with self.connect() as db:
            self.check(db)
            placeholders = ",".join("?" for _ in sequences)
            if db.execute(f"SELECT 1 FROM filesystem_inbox WHERE seq IN ({placeholders}) AND status<>'applied'", sequences).fetchone():
                raise WikiError("Cannot acknowledge unapplied filesystem events", 503)
            db.execute(f"UPDATE filesystem_inbox SET payload='' WHERE seq IN ({placeholders}) AND status='applied'", sequences)

    def uncertain(self, reason):
        with self.connect() as db:
            if self.recovery is not None and self.recovery.intake:
                self.recovery.uncertain(db, reason)
                return
            db.execute("UPDATE filesystem_state SET uncertain=? WHERE singleton=1", (reason,))
        raise WikiError("Filesystem history is uncertain; durable evidence retained: " + reason, 503)

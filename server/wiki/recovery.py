"""Owner-controlled recovery of uncertain filesystem history, without body writes.

The handle owns reception before normal writers drain. Plans bind exact current
revisions and conservative identities; the existing Store owns derived indexing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..service_installation import InstallationError, guard_vault
from . import catalog, source_bindings, storage
from . import format as document_format
from . import recovery_state as ledger
from .event_inbox import EventInbox
from .events import FileChanges, VaultEvents
from .extract import SUPPORTED, extract
from .history import WikiHistory, atomic
from .store import DocumentTooLarge, WikiError, WikiStore, read_document_bytes, revision

logger = logging.getLogger(__name__)


def basis(db):
    """Identity/source coordinates and hashes, never a second copy of bodies."""
    result = {}
    for table, fields in {
        "document_identities": "id,path,file_key",
        "source_bindings": "id,source_doc,selector,reference,base,policy,target_doc,proof",
        "source_binding_head": "source_doc,observation_id,pending_plan",
        "uid_aliases": "uid,doc_id",
        "documents": "id,path,revision",
    }.items():
        result[table] = [tuple(row) for row in db.execute(f"SELECT {fields} FROM {table} ORDER BY 1")]
    for table, key, value in (("material_origins", "doc_id", "state"),
                               ("managed_notes", "path", "base_hash,rendered_revision,regions,metadata")):
        result[table] = [(row[0], ledger.digest(tuple(row)[1:])) for row in db.execute(f"SELECT {key},{value} FROM {table} ORDER BY 1")]
    return result


class Metadata:
    """Read a registered owner's durable state without constructing a Store."""

    def __init__(self, registry, user_id, *, health=None):
        self.registry, self.user_id = registry, user_id
        self.health = health if health is not None else registry.health
        self.state = registry.state(user_id).resolve()
        self.config = registry.config(user_id, require_root=False)
        self.root = Path(self.config["root"])
        self.db_path = self.state / "index.sqlite3"
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise WikiError("Filesystem recovery requires the retained registered database", 503)
        self.inbox = EventInbox.__new__(EventInbox)
        self.inbox.db_path, self.inbox.root = self.db_path, str(self.root)
        self.inbox.vault_id, self.inbox.recovery = self.config["vault_id"], None
        self.inbox.health = self.health

    @contextlib.contextmanager
    def connect(self, *, write=False):
        from . import protocols
        from .schema import require_current_format, validate_filesystem_ledger

        try:
            if write:
                with storage.connect(self.db_path, self.health, write=False) as retained:
                    retained.execute("BEGIN")
                    require_current_format(retained)
                    protocols.require(retained, "registration_config")
            with storage.connect(self.db_path, self.health, write=write) as db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                require_current_format(db)
                protocols.require(db, "registration_config")
                validate_filesystem_ledger(db)
                ledger.require(db)
                source_bindings.validate(db)
                self.inbox.pending_rows(db)
                yield db
        except sqlite3.DatabaseError as exc:
            raise WikiError("Filesystem recovery metadata is unavailable; database retained: " + str(exc), 503) from exc

    def check_binding(self):
        if self.registry.state(self.user_id).resolve() != self.state or self.registry.config(self.user_id) != self.config:
            raise WikiError("Filesystem recovery registration changed; create a new reviewed plan", 409)

    def record(self, db, recovery_id=None):
        if recovery_id is not None:
            try:
                if uuid.UUID(recovery_id).hex != recovery_id:
                    raise ValueError()
            except (ValueError, AttributeError):
                raise WikiError("Filesystem recovery not found", 404) from None
            row = db.execute("SELECT * FROM filesystem_recoveries WHERE id=?", (recovery_id,)).fetchone()
            if row is None:
                raise WikiError("Filesystem recovery not found", 404)
        else:
            row = ledger.active(db) or db.execute("SELECT * FROM filesystem_recoveries ORDER BY updated_at DESC,id DESC LIMIT 1").fetchone()
        if row is not None and (row["root"], row["vault_id"]) != (str(self.root), self.config["vault_id"]):
            raise WikiError("Filesystem recovery belongs to another binding", 409)
        return row

    def snapshot(self, *, full=False):
        with self.connect() as db:
            state = dict(self.inbox.state(db))
            pending = self.inbox.pending_rows(db)
            row = self.record(db)
            result = {"vault_id": self.config["vault_id"], "registration_generation": self.config["registration_generation"],
                      "root_available": self.root.is_dir() and not self.root.is_symlink(), "filesystem": state,
                      "pending_batches": len(pending), "pending": [{key: batch[key] for key in
                          ("seq", "batch_id", "observer_epoch", "root_generation", "payload_hash")} for batch, _ in pending[:10]],
                      "pending_truncated": len(pending) > 10,
                      "recovery": summary(row) if row else None}
            if full:
                result |= {"identity_source_evidence": basis(db),
                           "pending": [{"receipt": batch, "records": records} for batch, records in pending],
                           "pending_truncated": False}
        return result


def summary(row):
    if row is None:
        return None
    result = {key: row[key] for key in ("generation", "phase", "plan_hash", "cutoff", "rebuild_cursor", "mapping_receipt", "completion_receipt")}
    result["recovery_id"] = row["id"]
    plan = json.loads(row["plan"]) if row["plan"] else {"files": []}
    result["files_count"] = len(plan["files"])
    result["retired_count"] = len(plan.get("retired", []))
    selected, size = [], 0
    for entry in plan["files"]:
        wanted = {key: entry[key] for key in ("path", "revision", "document_id", "retained")}
        cost = len(ledger.encoded(wanted).encode())
        if len(selected) < 30 and size + cost <= 16000:
            selected.append(wanted)
            size += cost
    result |= {"files": selected, "files_truncated": len(selected) != len(plan["files"]),
               "plan_url": f"/api/wiki/recovery/{row['id']}/plan", "approved": row["approved_hash"] == row["plan_hash"] and row["plan_hash"] is not None}
    return result


@dataclass(eq=False)
class Permit:
    handle: Handle
    generation: int
    intake: bool = True
    active: bool = True

    @property
    def manager(self):
        return self.handle.manager

    @property
    def observer(self):
        return self.handle.observer

    @property
    def recovery_id(self):
        return self.handle.recovery_id

    @property
    def config(self):
        return self.handle.metadata.config

    def check(self, db):
        h = self.handle
        h.metadata.check_binding()
        row = h.metadata.record(db, h.recovery_id)
        if (not self.active or not self.manager.owns(h) or self not in (h.permit, h.handoff_permit)
                or self.generation != row["generation"] or self.observer._closed
                or (row["observer_epoch"], row["root_generation"], row["registration"])
                != (self.observer.epoch, self.observer.root_generation, self.config["registration_generation"])
                or not self.intake and row["phase"] not in {"handoff", "complete"}):
            raise WikiError("Filesystem recovery capability or observer generation changed", 503)
        return row

    def uncertain(self, db, reason):
        row = self.check(db)
        ledger.audit(db, row["id"], row["generation"], "uncertain", {"reason": reason, "received_seq": self.handle.metadata.inbox.state(db)["received_seq"]})
        if row["phase"] != "complete":
            ledger.update(db, row, uncertainty_generation=row["uncertainty_generation"] + 1)
        db.execute("UPDATE filesystem_state SET uncertain=? WHERE singleton=1", (reason[:2048],))


class Handle:
    def __init__(self, manager, metadata):
        self.manager, self.metadata = manager, metadata
        self.observer = VaultEvents.acquire(metadata.root, metadata.state, health=metadata.health)
        self.recovery_id = ""
        self.permit: Permit | None = None
        self.handoff_permit: Permit | None = None
        self.store = None
        self.stop = threading.Event()
        self.thread = None
        self.error = None
        self.published = False

    def drain(self):
        with self.observer._lock:
            try:
                changes = self.observer.drain()
                self.observer.queued.merge(changes)
                self.observer._uncertain_reason = None
            except BaseException as exc:
                self.error = exc
                raise

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop.clear()

        def run():
            while not self.stop.wait(0.05):
                try:
                    self.drain()
                except Exception:
                    logger.exception("Filesystem recovery intake requires a new owner plan")
                    return

        self.thread = threading.Thread(target=run, name="wiki-recovery-intake", daemon=True)
        self.thread.start()

    def stop_pump(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(10)
            if self.thread.is_alive():
                raise WikiError("Filesystem recovery intake retirement remains unconfirmed", 503)
            self.thread = None

    def close(self):
        self.stop_pump()
        if self.store is not None and not self.published:
            self.store.close()
            self.store = None
        if self.permit is not None:
            self.permit.active = False
        if self.handoff_permit is not None:
            self.handoff_permit.active = False
        self.observer.release()


class RecoveryManager:
    def __init__(self, registry):
        self.registry = registry
        self.health = registry.health
        self._lock = threading.RLock()
        self._handles = {}
        self._gates = {}
        self._closing = False
        self._drained = False
        self._close_gate = threading.Lock()

    def key(self, user_id):
        return user_id, str(self.registry.state(user_id).resolve())

    @contextlib.contextmanager
    def gate(self, user_id):
        with self.registry._lock, self._lock:
            self._require_open()
            incarnation = self.registry._incarnation
            gate = self._gates.setdefault(self.key(user_id), threading.RLock())
        with gate:
            with self.registry._lock, self._lock:
                self._require_open()
                if incarnation != self.registry._incarnation:
                    raise WikiError("Filesystem recovery lifecycle changed before admission", 503)
            yield

    def _require_open(self):
        self.health.require_healthy()
        if (self._closing or self.registry._closing or self.registry._stopped
                or self.registry.health is not self.health):
            raise WikiError("Filesystem recovery cleanup is in progress", 503)

    def resume(self):
        with self._lock:
            if not self._drained or self._handles:
                raise WikiError("Filesystem recovery cleanup is not confirmed", 503)
            self._closing = False
            self._drained = False

    def is_drained(self):
        with self._lock:
            return self._closing and self._drained and not self._handles

    def owns(self, handle):
        with self._lock:
            return self._handles.get(self.key(handle.metadata.user_id)) is handle

    def inspect(self, user_id):
        return Metadata(self.registry, user_id, health=self.health).snapshot()

    def status(self, user_id, recovery_id):
        metadata = Metadata(self.registry, user_id, health=self.health)
        with metadata.connect() as db:
            return summary(metadata.record(db, recovery_id))

    def full_plan(self, user_id, recovery_id):
        metadata = Metadata(self.registry, user_id, health=self.health)
        with metadata.connect() as db:
            row = metadata.record(db, recovery_id)
            if row["plan"] is None:
                raise WikiError("Filesystem recovery observation is unfinished", 409)
            plan = json.loads(row["plan"])
            if ledger.digest(plan) != row["plan_hash"]:
                raise WikiError("Filesystem recovery plan hash changed; evidence retained", 503)
            return {"plan_hash": row["plan_hash"], "plan": plan}

    def export(self, user_id):
        metadata = Metadata(self.registry, user_id, health=self.health)
        raw = (ledger.encoded(metadata.snapshot(full=True)) + "\n").encode()
        folder = metadata.state / "recovery-exports"
        folder.mkdir(exist_ok=True, mode=0o700)
        if folder.is_symlink() or folder.resolve().parent != metadata.state:
            raise WikiError("Filesystem recovery export directory escaped the account state", 409)
        path = folder / (uuid.uuid4().hex + ".json")
        atomic(path, raw)
        path.chmod(0o600)
        return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def flush(self, user_id):
        with self._lock:
            handle = self._handles.get(self.key(user_id))
        if handle is None:
            raise WikiError("Filesystem recovery has no live receiver; create a new plan", 409)
        handle.drain()

    def _begin(self, metadata, *, renewed=False):
        metadata.check_binding()
        if metadata.root.is_symlink() or metadata.root.resolve() != metadata.root:
            raise WikiError("Filesystem recovery root changed", 409)
        try:
            guard_vault(settings, metadata.root, metadata.state, health=self.health)
        except InstallationError as error:
            raise WikiError(str(error), 503) from error
        key = self.key(metadata.user_id)
        with metadata.connect() as db:
            state = metadata.inbox.state(db)
            if ledger.active(db) is None and not (state["uncertain"] or state["armed_epoch"]):
                raise WikiError("No uncertain filesystem history requires recovery", 409)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                handle = Handle(self, metadata)
                self._handles[key] = handle
        h = handle
        h.stop_pump()  # Keep the FD/watches; rotate the durable generation serially.
        if h.store is not None:
            h.store.close()
            h.store = None
        h.metadata = metadata
        if h.observer.mode != "inotify" or h.observer._closed:
            raise WikiError("Filesystem recovery requires a live continuous observer; ordinary access stays closed", 503)
        with h.observer._lock, self.registry.registration_lock(metadata.user_id), metadata.connect(write=True) as db:
            metadata.check_binding()
            state = metadata.inbox.state(db)
            previous = ledger.active(db)
            if previous is None and not (state["uncertain"] or state["armed_epoch"]):
                raise WikiError("No uncertain filesystem history requires recovery", 409)
            if previous is not None and (previous["root"], previous["vault_id"]) != (str(metadata.root), metadata.config["vault_id"]):
                raise WikiError("Filesystem recovery root or vault changed", 409)
            evidence = {"filesystem": dict(state), "observer_epoch": h.observer.epoch,
                        "root_generation": h.observer.root_generation,
                        "previous_recovery": {key: previous[key] for key in ("id", "generation", "phase", "plan_hash", "mapping_receipt", "completion_receipt")} if previous else None,
                        "pending_receipts": [{key: row[key] for key in ("seq", "batch_id", "payload_hash", "observer_epoch", "root_generation")}
                                             for row, _ in metadata.inbox.pending_rows(db)]}
            # Earlier plans/receipts remain in the immutable audit on replan.
            # The same recovery ID advances a generation, never repeats an old mapping.
            identifier = previous["id"] if previous else uuid.uuid4().hex
            generation = previous["generation"] + 1 if previous else 1
            fields = {"generation": generation, "registration": metadata.config["registration_generation"],
                      "observer_epoch": h.observer.epoch, "root_generation": h.observer.root_generation,
                      "phase": "prepared", "uncertainty_generation": 0, "cutoff": state["received_seq"],
                      "plan_hash": None, "plan": None, "approved_hash": None, "evidence": ledger.encoded(evidence),
                      "ledger_hash": None, "rebuild_cursor": 0, "mapping_receipt": None, "completion_receipt": None}
            if previous:
                ledger.audit(db, identifier, previous["generation"], "replan", {"previous": dict(previous)})
                ledger.update(db, previous, **fields)
            else:
                fields = {"id": identifier, "vault_id": metadata.config["vault_id"], "root": str(metadata.root),
                          "registration": metadata.config["registration_generation"], **fields, "updated_at": ledger.now()}
                db.execute("INSERT INTO filesystem_recoveries(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")", tuple(fields.values()))
            ledger.audit(db, identifier, generation, "intake_start", evidence)
            db.execute("UPDATE filesystem_state SET armed_epoch=NULL,uncertain=? WHERE singleton=1",
                       (state["uncertain"] or "Interrupted reception retained for owner recovery",))
        with h.observer._lock:
            if h.permit:
                h.permit.active = False
            if h.handoff_permit:
                h.handoff_permit.active = False
            h.recovery_id, h.permit, h.handoff_permit = identifier, Permit(h, generation), None
            h.error = None
            h.observer.journal = EventInbox(metadata.db_path, metadata.root, metadata.config["vault_id"],
                                            recovery=h.permit, health=self.health)
            h.observer._uncertain_reason = None
        h.start()  # Reception precedes any blocking/closing of normal Stores.
        self.registry.block(metadata.user_id)
        stat = metadata.root.stat()
        generation = f"{stat.st_dev}:{stat.st_ino}"
        if generation != h.observer.root_generation:
            if renewed:
                raise WikiError("Filesystem root changed again during recovery observation; create a new plan", 409)
            h.stop_pump()
            try:
                h.drain()
                with metadata.connect(write=True) as db:
                    h.permit.uncertain(db, "Registered root generation changed; a new observer and owner observation are required")
                # block() confirmed normal writers. The observer itself checks
                # that no unregistered captured Store still owns a lease.
                h.observer.retire_exclusive()
            except BaseException:
                if not h.observer._closed:
                    h.start()
                raise
            h.permit.active = False
            with self._lock:
                self._handles.pop(key)
            # The closed epoch is retained in the old intent/audit. A new
            # generation explicitly covers this unknown replacement interval;
            # none of its move cookies or identity guesses are reused.
            return self._begin(metadata, renewed=True)
        return h

    @staticmethod
    def _file(root, relative, *, body=False):
        path = root / relative
        if not path.is_relative_to(root) or any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root)):
            raise WikiError("Recovery file changed or escaped the vault", 409)
        stat = path.stat(follow_symlinks=False)
        stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise WikiError("Recovery file changed or escaped the vault", 409)
        raw = b""
        if path.suffix.lower() == ".pdf":
            digest = revision(f"excluded-pdf:{stat.st_mtime_ns}:{stat.st_size}".encode())
        else:
            if body:
                try:
                    raw = read_document_bytes(path, label=relative)
                except DocumentTooLarge:
                    raw = None  # Known resource refusal, not a missing or changed file.
            if body and raw is not None:
                digest = revision(raw)
            else:
                sha = hashlib.sha256()
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        sha.update(block)
                digest = sha.hexdigest()
        after = path.stat(follow_symlinks=False)
        if stamp != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size):
            raise WikiError("Recovery file changed while observed; create a new plan", 409)
        return {"path": relative, "revision": digest, "file_key": WikiStore.file_key(stat), "stamp": list(stamp)}, raw

    def _scan(self, h):
        files, directories = [], []

        def visit(directory):
            h.observer.watch(directory)
            with h.observer._lock:
                if directory not in h.observer._directories.values():
                    raise WikiError("Recovery directory watch is unavailable; evidence retained", 503)
            directories.append(directory.relative_to(h.metadata.root).as_posix())
            with os.scandir(directory) as children:
                entries = sorted(children, key=lambda entry: entry.name)
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in SUPPORTED:
                    relative = Path(entry.path).relative_to(h.metadata.root).as_posix()
                    files.append(self._file(h.metadata.root, relative)[0])

        with h.observer.watching():
            visit(h.metadata.root)
        return sorted(files, key=lambda item: item["path"]), directories

    def plan(self, user_id, retain=()):
        with self.gate(user_id):
            metadata = Metadata(self.registry, user_id, health=self.health)
            h = self._begin(metadata)
            files, directories = self._scan(h)
            h.drain()
            with metadata.connect() as db:
                row = h.permit.check(db)
                state = metadata.inbox.state(db)
                observed = metadata.inbox.pending_rows(db)
                before = {"basis": basis(db), "filesystem": dict(state),
                          "receipts": [{key: batch[key] for key in ("seq", "batch_id", "payload_hash", "observer_epoch", "root_generation")} for batch, _ in observed]}
                identities = {entry[0]: entry[1] for entry in before["basis"]["document_identities"]}
                heads = {entry[0]: entry[1] for entry in before["basis"]["source_binding_head"]}
                cutoff, uncertainty_generation = state["received_seq"], row["uncertainty_generation"]
            requested = {entry["path"]: entry for entry in retain}
            if len(requested) != len(retain) or len({entry["doc"] for entry in retain}) != len(retain):
                raise WikiError("Recovery mappings must be one-to-one", 422)
            by_path = {entry["path"]: entry for entry in files}
            for path, entry in requested.items():
                if entry["doc"] not in identities or path not in by_path or by_path[path]["revision"] != entry["expected_revision"]:
                    raise WikiError("Recovery mapping identity/path/revision changed", 409)
            for entry in files:
                old = requested.get(entry["path"])
                entry.update(document_id=old["doc"] if old else uuid.uuid4().hex, retained=old is not None,
                             old_path=identities[old["doc"]] if old else None,
                             prior_observation_id=heads.get(old["doc"]) if old else None)
            retired = [{"doc": doc, "path": path} for doc, path in identities.items() if path is not None and doc not in {entry["doc"] for entry in retain}]
            plan = {"version": 1, "recovery_id": h.recovery_id, "generation": h.permit.generation,
                    "vault_id": metadata.config["vault_id"], "root": str(metadata.root), "registration": metadata.config["registration_generation"],
                    "observer_epoch": h.observer.epoch, "root_generation": h.observer.root_generation,
                    "uncertainty_generation": uncertainty_generation, "cutoff": cutoff,
                    "ledger_hash": ledger.digest(before["basis"]), "before_evidence_hash": ledger.digest(before),
                    "files": files, "directories": directories, "retired": retired,
                    "received_batches": [{"receipt": batch, "records": records} for batch, records in observed],
                    "warning": "Unobserved history cannot be reconstructed. Old identities remain tombstones unless explicitly retained; bytes and original source targets are preserved."}
            with self.registry.registration_lock(user_id), metadata.connect(write=True) as db:
                current = h.permit.check(db)
                if current["uncertainty_generation"] != uncertainty_generation:
                    raise WikiError("Filesystem recovery observed another loss; create a new plan", 409)
                ledger.audit(db, h.recovery_id, h.permit.generation, "before_mapping", before)
                ledger.audit(db, h.recovery_id, h.permit.generation, "plan", plan)
                ledger.update(db, current, cutoff=cutoff, plan=ledger.encoded(plan), plan_hash=ledger.digest(plan), ledger_hash=plan["ledger_hash"])
                return summary(metadata.record(db, h.recovery_id))

    def _current(self, user_id, recovery_id, plan_hash):
        metadata = Metadata(self.registry, user_id, health=self.health)
        metadata.check_binding()
        with metadata.connect() as db:
            row = metadata.record(db, recovery_id)
            if (row["registration"] != metadata.config["registration_generation"] or row["plan_hash"] != plan_hash
                    or row["plan"] is None or ledger.digest(json.loads(row["plan"])) != plan_hash):
                raise WikiError("Filesystem recovery plan or registration changed", 409)
        with self._lock:
            h = self._handles.get(self.key(user_id))
        if row["phase"] != "complete" and (h is None or h.recovery_id != recovery_id or h.permit.generation != row["generation"]):
            raise WikiError("Filesystem recovery lost its observer; re-observe and approve a new generation of this recovery", 409)
        if h is not None and h.error is not None:
            raise WikiError("Filesystem recovery reception is uncertain; create a new reviewed plan", 503) from h.error
        return metadata, row, h

    def approve(self, user_id, recovery_id, plan_hash):
        with self.gate(user_id):
            metadata, original, h = self._current(user_id, recovery_id, plan_hash)
            if original["phase"] == "complete":
                return summary(original)
            with self.registry.registration_lock(user_id), metadata.connect(write=True) as db:
                row = h.permit.check(db)
                if row["plan_hash"] != plan_hash:
                    raise WikiError("Filesystem recovery plan changed", 409)
                if row["approved_hash"] != plan_hash:
                    ledger.audit(db, recovery_id, row["generation"], "owner_approval", {"plan_hash": plan_hash})
                    ledger.update(db, row, approved_hash=plan_hash)
                return summary(metadata.record(db, recovery_id))

    @staticmethod
    def _affects(record, path):
        relative = record.get("path", "")
        return record.get("type") == "overflow" or not relative or path == relative or path.startswith(relative.rstrip("/") + "/")

    def _fence(self, h, row, plan, *, before_mapping=False):
        h.metadata.check_binding()
        stat = h.metadata.root.stat()
        if (h.metadata.root.is_symlink() or f"{stat.st_dev}:{stat.st_ino}" != plan["root_generation"]
                or row["uncertainty_generation"] != plan["uncertainty_generation"]):
            raise WikiError("Filesystem recovery root or uncertainty changed; replan required", 409)
        with h.metadata.connect() as db:
            current = h.permit.check(db)
            if (current["generation"] != row["generation"] or current["plan_hash"] != row["plan_hash"]
                    or current["uncertainty_generation"] != plan["uncertainty_generation"]):
                raise WikiError("Filesystem recovery observation changed; replan required", 409)
            pending = h.metadata.inbox.pending_rows(db)
            if before_mapping and ledger.digest(basis(db)) != plan["ledger_hash"]:
                raise WikiError("Filesystem recovery identity/source evidence changed", 409)
        later = [record for batch, records in pending if batch["seq"] > plan["cutoff"] for record in records]
        if before_mapping:
            if any(self._affects(record, entry["path"]) for record in later for entry in plan["files"]):
                raise WikiError("Filesystem events changed a planned object; review a new plan", 409)
            for entry in plan["files"]:
                current, _ = self._file(h.metadata.root, entry["path"])
                if any(current[key] != entry[key] for key in ("revision", "file_key", "stamp")):
                    raise WikiError("Filesystem recovery file revision or identity changed", 409)
        return later

    def _store(self, h):
        if h.store is None:
            def captured(store):
                store.registration_generation = h.metadata.config["registration_generation"]

            h.store = WikiStore(h.metadata.root, h.metadata.state, h.metadata.config["vault_id"],
                                on_construct=captured, recovery=h.permit, health=self.health)
        return h.store

    def _reload(self, h):
        """Reconstruct only receipts beyond the approved cutoff, in their order."""
        with h.observer._lock, h.metadata.connect() as db:
            pending = h.metadata.inbox.pending_rows(db)
            h.observer.queued = FileChanges()
            h.observer._changes = FileChanges()
            h.observer._moves.clear()
            previous_epoch = None
            for row, records in pending:
                if previous_epoch != row["observer_epoch"] and h.observer._moves:
                    raise WikiError("Filesystem recovery cannot pair moves across observer epochs", 409)
                previous_epoch = row["observer_epoch"]
                for record in records:
                    h.observer.process_record(record, replay=True)
                h.observer._changes.receipts.append(row["seq"])
            h.observer.queued.merge(h.observer._changes)
            h.observer._changes = FileChanges()

    def _map(self, h, row, plan):
        store = self._store(h)
        h.drain()
        self._fence(h, row, plan, before_mapping=True)
        with WikiHistory(store).locked(), self.registry.registration_lock(h.metadata.user_id), h.observer._lock:
            h.drain()
            self._fence(h, row, plan, before_mapping=True)
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = h.permit.check(db)
                if current["phase"] != "prepared" or current["approved_hash"] != current["plan_hash"] or current["plan_hash"] != ledger.digest(plan):
                    raise WikiError("Filesystem recovery needs approval of this exact plan", 409)
                if current["uncertainty_generation"] != plan["uncertainty_generation"]:
                    raise WikiError("Filesystem recovery observed another loss; replan required", 409)
                if ledger.digest(basis(db)) != plan["ledger_hash"]:
                    raise WikiError("Filesystem recovery ledger changed before mapping", 409)
                management = {}
                for entry in plan["files"]:
                    if entry["retained"] and entry["old_path"]:
                        old = db.execute("SELECT * FROM managed_notes WHERE path=?", (entry["old_path"],)).fetchone()
                        if old and old["rendered_revision"] == entry["revision"]:
                            management[entry["document_id"]] = dict(old)
                for relative in [value[0] for value in db.execute("SELECT path FROM document_identities WHERE path IS NOT NULL")]:
                    store.detach_path(db, relative)
                # An old unresolved path must not bind to a replacement after
                # an unknown interval. Existing bound/unknown proofs stay put.
                db.execute("UPDATE source_bindings SET policy='unknown' WHERE policy='unresolved'")
                for entry in plan["files"]:
                    if entry["retained"]:
                        changed = db.execute("UPDATE document_identities SET path=?,file_key=? WHERE id=? AND path IS NULL",
                                             (entry["path"], entry["file_key"], entry["document_id"]))
                        if changed.rowcount != 1:
                            raise WikiError("Filesystem recovery retained identity changed", 409)
                    else:
                        db.execute("INSERT INTO document_identities VALUES(?,?,?)", (entry["document_id"], entry["path"], entry["file_key"]))
                    # A later received edit may skip the recovery rebuild.
                    # Its ordinary replay must still use the approved source
                    # policy, rather than resolve old names as new references.
                    prior = source_bindings.observation(db, entry["prior_observation_id"]) if entry["prior_observation_id"] else None
                    source_bindings.stage(db, prior or {
                        "version": 1, "id": uuid.uuid4().hex, "source_doc": entry["document_id"],
                        "path": entry["path"], "revision": entry["revision"],
                        "context": f"recovery:{h.recovery_id}:{h.permit.generation}:{entry['document_id']}",
                        "entries": [], "confirmed": False, "unknown_current": True,
                    })
                    if entry["document_id"] in management:
                        managed = management[entry["document_id"]]
                        db.execute("INSERT INTO managed_notes VALUES(?,?,?,?,?)", (
                            entry["path"], managed["base_hash"], managed["rendered_revision"], managed["regions"], managed["metadata"],
                        ))
                db.execute("DELETE FROM watch_directories")
                db.executemany("INSERT INTO watch_directories VALUES(?)", [(path,) for path in plan["directories"]])
                covered = [batch["seq"] for batch, _ in h.metadata.inbox.pending_rows(db) if batch["seq"] <= plan["cutoff"]]
                h.observer.journal.mark_applied(db, covered)
                body = {"plan_hash": current["plan_hash"], "cutoff": plan["cutoff"],
                        "before_ledger_hash": plan["ledger_hash"], "covered": covered,
                        "mapping": [{key: entry[key] for key in ("document_id", "path", "revision", "retained")} for entry in plan["files"]]}
                ledger.audit(db, current["id"], current["generation"], "mapping", body)
                ledger.update(db, current, phase="mapping_applied", mapping_receipt=ledger.digest(body))
                db.execute("UPDATE index_state SET epoch=epoch+1,checked_at=NULL,error=NULL WHERE singleton=1")
            self._reload(h)

    def _prepare(self, h, entry):
        observed, raw = self._file(h.metadata.root, entry["path"], body=True)
        if any(observed[key] != entry[key] for key in ("revision", "file_key", "stamp")):
            raise WikiError("Recovery output changed after mapping", 409)
        if raw is None:
            return None, {"resource_error": DocumentTooLarge(entry["path"])}
        try:
            extracted = extract(raw, Path(entry["path"]).suffix.lower())
        except Exception as exc:
            extracted = {"text": "", "pages": [], "quality": "error", "metadata": {"error": f"{type(exc).__name__}: {exc}"}}
        parsed = document_format.parse(extracted["text"])
        # Extraction and source-plan calculation happen on a read snapshot,
        # outside the short index writer transaction needed by intake.
        with h.metadata.connect() as db:
            managed = db.execute("SELECT * FROM managed_notes WHERE path=?", (entry["path"],)).fetchone()
            supplied = source_bindings.metadata(db, entry["document_id"], entry["path"], raw, dict(managed) if managed else None)
            prior = source_bindings.observation(db, entry["prior_observation_id"]) if entry["prior_observation_id"] else None
            policy = {"context": f"recovery:{h.recovery_id}:{h.permit.generation}:{entry['document_id']}",
                      "mode": "continuation" if prior else "unknown", "prior": prior}
            draft = source_bindings.incoming(db, entry["document_id"], entry["path"], entry["revision"], source_bindings.entries(supplied), policy)
        return raw, {"extracted": extracted, "parsed": parsed, "source_draft": draft}

    def _rebuild(self, h, row, plan):
        store = self._store(h)
        self._fence(h, row, plan)
        end = min(len(plan["files"]), row["rebuild_cursor"] + 16)
        for index in range(row["rebuild_cursor"], end):
            entry = plan["files"][index]
            skipped = False
            try:
                raw, prepared = self._prepare(h, entry)
            except (WikiError, OSError):
                h.drain()
                later = self._fence(h, row, plan)
                if not any(self._affects(record, entry["path"]) for record in later):
                    with h.metadata.connect(write=True) as db:
                        h.permit.uncertain(db, "A mapped file changed outside the approved reception prefix")
                    raise WikiError("A mapped file needs a new owner observation; no model or body write was attempted", 409) from None
                skipped = True
            with WikiHistory(store).locked(), self.registry.registration_lock(h.metadata.user_id), store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = h.permit.check(db)
                if current["rebuild_cursor"] != index or current["phase"] not in {"mapping_applied", "rebuilding"}:
                    raise WikiError("Filesystem recovery rebuild cursor changed", 409)
                if current["uncertainty_generation"] != plan["uncertainty_generation"]:
                    raise WikiError("Filesystem recovery observed another loss; replan required", 409)
                if not skipped:
                    stat = (h.metadata.root / entry["path"]).stat()
                    if list((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)) != entry["stamp"]:
                        raise WikiError("Filesystem recovery file changed before indexing", 409)
                    if "resource_error" in prepared:
                        store.record_resource_error(db, entry["path"], prepared["resource_error"], stat=stat)
                        ledger.audit(db, current["id"], current["generation"], "resource_rejected", {"path": entry["path"], "code": "document_too_large"})
                    else:
                        store._index(db, entry["path"], raw=raw, prepared=prepared)
                else:
                    ledger.audit(db, current["id"], current["generation"], "pending_after_cutoff", {"path": entry["path"]})
                ledger.update(db, current, phase="rebuilding", rebuild_cursor=index + 1)
        with h.metadata.connect() as db:
            current = h.permit.check(db)
        if current["rebuild_cursor"] == len(plan["files"]):
            self._ready_handoff(h, current, plan)

    def _ready_handoff(self, h, row, plan):
        store = self._store(h)
        with WikiHistory(store).locked(), h.observer._lock, self.registry.registration_lock(h.metadata.user_id):
            h.drain()
            self._fence(h, row, plan)
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = h.permit.check(db)
                if current["rebuild_cursor"] != len(plan["files"]) or current["phase"] not in {"mapping_applied", "rebuilding"}:
                    raise WikiError("Filesystem recovery rebuild is unfinished", 409)
                if current["uncertainty_generation"] != plan["uncertainty_generation"]:
                    raise WikiError("Filesystem recovery observed another loss; replan required", 409)
                # Validate retained targets against the immutable before audit.
                original = db.execute("SELECT body,sha256 FROM filesystem_recovery_audit WHERE recovery_id=? AND generation=? AND kind='before_mapping'",
                                      (current["id"], current["generation"])).fetchone()
                if original is None or hashlib.sha256(original["body"].encode()).hexdigest() != original["sha256"]:
                    raise WikiError("Filesystem recovery source evidence is missing", 503)
                for binding in json.loads(original["body"])["basis"]["source_bindings"]:
                    if binding[5] in {"bound", "unknown", "external"}:
                        actual = db.execute("SELECT id,source_doc,selector,reference,base,policy,target_doc,proof FROM source_bindings WHERE id=?", (binding[0],)).fetchone()
                        if actual is None or tuple(actual) != tuple(binding):
                            raise WikiError("Filesystem recovery changed an original source target", 503)
                catalog.finish(db)
                state = h.metadata.inbox.state(db)
                if state["applied_seq"] < plan["cutoff"] or state["armed_epoch"]:
                    raise WikiError("Filesystem recovery reception or covered prefix is unfinished", 503)
                ledger.update(db, current, phase="handoff")
                # This clears only the old uncertainty. The same transaction
                # keeps handoff durable, so both warm and cold admission deny.
                db.execute("UPDATE filesystem_state SET uncertain=NULL WHERE singleton=1")
                db.execute("UPDATE index_state SET checked_at=?,error=NULL WHERE singleton=1", (ledger.now(),))
            h.observer.needs_reconciliation = False

    def _handoff(self, h, row, plan):
        with h.observer._lock, self.registry.registration_lock(h.metadata.user_id):
            h.drain()
            self._fence(h, row, plan)
            h.handoff_permit = Permit(h, row["generation"], intake=False)
            receiver = EventInbox(h.metadata.db_path, h.metadata.root, h.metadata.config["vault_id"],
                                  recovery=h.handoff_permit, health=self.health)
            h.observer.journal = receiver
            self._reload(h)
            # Exercise the ordinary durable arm/receive path without inventing
            # a filesystem notification or consuming a post-cutoff receipt.
            receiver.arm(h.observer.epoch)
            receiver.receive([], h.observer.epoch, h.observer.root_generation, armed=True)
            h.drain()
            receiver.pending()
            with h.metadata.connect(write=True) as db:
                current = h.handoff_permit.check(db)
                self._fence(h, current, plan)
                state = h.metadata.inbox.state(db)
                if state["uncertain"] or state["armed_epoch"]:
                    raise WikiError("Filesystem recovery handoff reception is uncertain", 503)
                body = {"plan_hash": current["plan_hash"], "mapping_receipt": current["mapping_receipt"],
                        "observer_epoch": h.observer.epoch, "root_generation": h.observer.root_generation,
                        "received_seq": state["received_seq"], "applied_seq": state["applied_seq"]}
                ledger.audit(db, current["id"], current["generation"], "complete", body)
                ledger.update(db, current, phase="complete", completion_receipt=ledger.digest(body))

    def _finish(self, metadata, row, h):
        metadata.check_binding()
        stat = metadata.root.stat()
        if metadata.root.is_symlink() or f"{stat.st_dev}:{stat.st_ino}" != row["root_generation"]:
            raise WikiError("Filesystem recovery root changed after completion; evidence retained", 409)
        with metadata.connect() as db:
            current = metadata.record(db, row["id"])
            receipt = db.execute("SELECT body,sha256 FROM filesystem_recovery_audit WHERE recovery_id=? AND generation=? AND kind='complete'",
                                 (current["id"], current["generation"])).fetchone()
            if (current["phase"] != "complete" or receipt is None or receipt["sha256"] != current["completion_receipt"]
                    or hashlib.sha256(receipt["body"].encode()).hexdigest() != receipt["sha256"]):
                raise WikiError("Filesystem recovery completion receipt is unavailable", 503)
            state = metadata.inbox.state(db)
            if state["uncertain"] or state["armed_epoch"]:
                raise WikiError("Filesystem history became uncertain again; its new evidence is retained", 503)
        if h is not None:
            store = self._store(h)
            with h.observer._lock:
                h.observer.journal.recovery = None
                self.registry.publish_recovery(metadata.user_id, store, h.permit)
                h.published = True
            h.stop_pump()
            h.store = None  # Ownership transferred to the normal registry.
            h.permit.active = False
            if h.handoff_permit:
                h.handoff_permit.active = False
            h.observer.release()
            with self._lock:
                self._handles.pop(self.key(metadata.user_id), None)
        return summary(current)

    def apply(self, user_id, recovery_id, plan_hash):
        with self.gate(user_id):
            metadata, row, h = self._current(user_id, recovery_id, plan_hash)
            if row["phase"] == "complete":
                return self._finish(metadata, row, h)
            if row["approved_hash"] != plan_hash:
                raise WikiError("Approve the exact exported recovery plan before applying it", 409)
            h.drain()
            plan = json.loads(row["plan"])
            if row["phase"] == "prepared":
                self._map(h, row, plan)
            elif row["phase"] in {"mapping_applied", "rebuilding"}:
                self._rebuild(h, row, plan)
            elif row["phase"] == "handoff":
                self._handoff(h, row, plan)
            with metadata.connect() as db:
                current = metadata.record(db, recovery_id)
            return self._finish(metadata, current, h) if current["phase"] == "complete" else summary(current)

    def close(self):
        with self._close_gate:
            self._close()

    def _close(self):
        with self._lock:
            self._closing = True
            self._drained = False
            captured = list(self._gates.items())
        for key, gate in captured:
            with gate:
                with self._lock:
                    handle = self._handles.get(key)
                if handle is not None:
                    handle.close()
                    with self._lock:
                        self._handles.pop(key, None)
        with self._lock:
            self._drained = True

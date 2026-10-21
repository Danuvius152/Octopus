"""Durable mutation journal, content-addressed originals and vault locking."""

import base64
import contextlib
import fcntl
import json
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import origins, source_bindings
from .storage import file_effect
from .store import WikiError, is_maintainable_text, revision

_owners = threading.local()
_UNCHANGED = object()


def history_text(path: str) -> bool:
    return is_maintainable_text(path)


def atomic(path: Path, raw: bytes, *, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class WikiHistory:
    def __init__(self, store):
        self.store = store
        self.state = store.state
        self.blobs = self.state / "blobs"

    @contextlib.contextmanager
    def under_existing_lock(self, handle, lease=None):
        with self.store.operation(lease), self._under_existing_lock(handle):
            yield

    @contextlib.contextmanager
    def _under_existing_lock(self, handle):
        """The app bridge transfers its already-held exclusive flock to a worker."""
        if handle.closed:
            raise RuntimeError("The external vault lock has already been released")
        held: set[str] = getattr(_owners, "held", set())
        _owners.held = held | {str(self.state.resolve())}
        try:
            yield
        finally:
            _owners.held = held

    @contextlib.contextmanager
    def locked(self, *, shared=False):
        with self.store.operation(), self._locked(shared=shared):
            yield

    @contextlib.contextmanager
    def _locked(self, *, shared=False):
        held: set[str] = getattr(_owners, "held", set())
        key = str(self.state.resolve())
        if key in held:
            yield
            return
        with (self.state / "vault.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            _owners.held = held | {key}
            try:
                if not shared:
                    self.recover()
                yield
            finally:
                _owners.held = held
                fcntl.flock(handle, fcntl.LOCK_UN)

    def blob(self, raw: bytes | None) -> str | None:
        if raw is None:
            return None
        digest = revision(raw)
        path = self.blobs / digest
        with self.store.operation(), file_effect(self.store.health):
            self.blobs.mkdir(exist_ok=True)
            sync_directory(self.state)
            if not path.exists():
                atomic(path, raw)
            else:
                # A prior process may have lost the directory-sync receipt.
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
                sync_directory(self.blobs)
        return digest

    def record(self, *args, **kwargs):
        with self.locked():
            return self._record(*args, **kwargs)

    def _record(
        self,
        path,
        before,
        after,
        actor,
        reason,
        status,
        management=_UNCHANGED,
        proposal=None,
        before_management=_UNCHANGED,
        document_id=None,
        change_id=None,
        publication=None,
        operation="save",
        operation_details=None,
        origin_state=origins.UNCHANGED,
        before_origin=origins.UNCHANGED,
        origin_builder=None,
        source_plan=None,
        before_source_plan=None,
        source_transition=None,
    ):
        if change_id:
            with self.store.connect() as db:
                previous_record = db.execute(
                    "SELECT * FROM changes WHERE id=?", (change_id,)
                ).fetchone()
            if previous_record:
                return dict(previous_record)
        row = {
            "id": change_id or uuid.uuid4().hex,
            "path": path,
            "before_hash": self.blob(before)
            if history_text(path)
            else revision(before)
            if before is not None
            else None,
            "after_hash": self.blob(after)
            if history_text(path)
            else revision(after)
            if after is not None
            else None,
            "actor": actor,
            "reason": reason,
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self.store.connect() as db:
            if publication:
                from . import grants

                if publication.get("authority"):
                    if publication.get("supporting_navigation"):
                        grants.check_navigation(db, self.store, publication, path)
                    else:
                        grants.active(db, publication["authority"], self.store)
                    if not publication.get("reservation_id") and not publication.get("supporting_navigation"):
                        grants.authorize(db, publication["authority"], self.store, path)
            identity = db.execute(
                "SELECT id FROM document_identities WHERE path=?", (path,)
            ).fetchone()
            if document_id is None:
                document_id = identity[0] if identity else uuid.uuid4().hex
                if identity is None and history_text(path):
                    db.execute(
                        "INSERT INTO document_identities VALUES(?,?,?)",
                        (document_id, path, ""),
                    )
            previous = db.execute(
                "SELECT * FROM managed_notes WHERE path=?", (path,)
            ).fetchone()
            before_state = (
                (dict(previous) if previous else None)
                if before_management is _UNCHANGED
                else before_management
            )
            after_state = (
                (dict(previous) if previous else None)
                if management is _UNCHANGED
                else management
            )
            db.execute(
                "INSERT INTO changes VALUES(:id,:path,:before_hash,:after_hash,:actor,:reason,:status,:created_at)",
                row,
            )
            db.execute(
                "INSERT INTO change_retention VALUES(?,?,?,?,NULL)",
                (
                    row["id"],
                    document_id,
                    int(
                        row["before_hash"] is None
                        or (self.blobs / row["before_hash"]).is_file()
                    ),
                    int(
                        row["after_hash"] is None
                        or (self.blobs / row["after_hash"]).is_file()
                    ),
                ),
            )
            db.execute(
                "INSERT INTO change_management VALUES(?,?,?)",
                (row["id"], json.dumps(before_state), json.dumps(after_state)),
            )
            if origin_builder is not None:
                if origin_state is not origins.UNCHANGED or before_origin is not origins.UNCHANGED:
                    raise WikiError("Choose a material transition or an explicit recovery state", 409)
                before_origin, origin_state = origin_builder(db, document_id, before, after)
            origins.record(
                db, row["id"], document_id, row["before_hash"], row["after_hash"],
                before_state=before_origin, after_state=origin_state,
            )
            source_bindings.record(db, row["id"], document_id, path, before, after,
                                   before_state, after_state, selected=source_plan,
                                   before_selected=before_source_plan, transition=source_transition)
            if status == "applied":
                origins.apply(db, row["id"])
                source_bindings.apply(db, row["id"])
            if publication and not publication.get("supporting_navigation") and publication["source_doc"] is not None:
                first = db.execute("SELECT input_revision FROM job_publications WHERE job_id=? AND source_doc=? ORDER BY rowid LIMIT 1", (publication["job_id"], publication["source_doc"])).fetchone()
                db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,0)", (
                    publication["job_id"], publication["source_doc"], first[0] if first else publication["input_revision"],
                    row["id"], path, row["after_hash"],
                ))
            if status == "prepared":
                from .operations import record

                if publication and publication.get("reservation_id"):
                    from . import grants

                    grants.prepare_creation(db, publication, self.store, path, document_id, row["id"], row["after_hash"])
                record(db, doc_id=document_id, path=path, before=row["before_hash"], after=row["after_hash"], operation=operation, reason=reason, publication=publication, change_id=row["id"], details=operation_details, store=self.store)
            if proposal is not None:
                db.execute(
                    "INSERT INTO change_proposals VALUES(?,?)",
                    (row["id"], json.dumps(proposal, ensure_ascii=False)),
                )
        return row

    def status(self, change_id, value):
        with self.store.connect() as db:
            db.execute("UPDATE changes SET status=? WHERE id=?", (value, change_id))
            if value == "applied":
                change = db.execute(
                    "SELECT path,after_hash FROM changes WHERE id=?", (change_id,)
                ).fetchone()
                origins.apply(db, change_id)
                source_bindings.apply(db, change_id)
                if change and change["after_hash"] is None and not self.store.path(change["path"]).is_file():
                    self.store.detach_path(db, change["path"])
                    return
                entry = db.execute(
                    "SELECT after_state FROM change_management WHERE change_id=?",
                    (change_id,),
                ).fetchone()
                if entry:
                    state = json.loads(entry[0])
                    if state:
                        db.execute(
                            "INSERT OR REPLACE INTO managed_notes VALUES(:path,:base_hash,:rendered_revision,:regions,:metadata)",
                            state,
                        )
                    else:
                        db.execute(
                            "DELETE FROM managed_notes WHERE path=(SELECT path FROM changes WHERE id=?)",
                            (change_id,),
                        )

    def recover(self):
        if str(self.state.resolve()) not in getattr(_owners, "held", set()):
            # Direct recovery enters the same exclusive owner as writer/read
            # recovery. Nested synchronization must not replay the journal.
            with self.locked():
                return
        external = self.state / "external-change.json"
        if external.exists():
            entry = json.loads(external.read_text())
            if entry.get("version") != 2:
                entry = self.legacy_external_entry(
                    {
                        path: digest
                        for path, digest in entry["before"].items()
                        if history_text(path)
                    },
                    self.inventory(),
                    entry["actor"],
                    entry["reason"] + "; recovered external mutation",
                )
                # Persist stable source identities and claims before moving
                # anything. Every subsequent replay uses the same change IDs.
                with file_effect(self.store.health):
                    atomic(external, json.dumps(entry).encode())
            self.finish_external(entry)
            with file_effect(self.store.health):
                external.unlink()
                sync_directory(self.state)
            self.store.refresh()
        with self.store.connect() as db:
            pending = db.execute(
                "SELECT * FROM changes WHERE status='prepared'"
            ).fetchall()
        for row in pending:
            path = self.store.path(row["path"])
            current = revision(path.read_bytes()) if path.is_file() else None
            self.status(
                row["id"],
                "applied"
                if current == row["after_hash"]
                else "interrupted"
                if current == row["before_hash"]
                else "conflict",
            )

    def record_external(self, before, after, actor, reason):
        with self.locked():
            self.finish_external(self.legacy_external_entry(before, after, actor, reason))

    def legacy_deleted_reservation(self, db, identity, path, digest):
        """Recognize an identity created solely by the old deletion-half record."""
        if identity["file_key"] or identity["origin"] is not None:
            return False
        doc_id = identity["id"]
        if (
            db.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone()
            or db.execute("SELECT 1 FROM uid_aliases WHERE doc_id=?", (doc_id,)).fetchone()
            or db.execute("SELECT 1 FROM operation_receipts WHERE doc_id=?", (doc_id,)).fetchone()
        ):
            return False
        changes = db.execute(
            """SELECT c.path,c.before_hash,c.after_hash,c.status FROM changes c
            JOIN change_retention r ON r.change_id=c.id WHERE r.document_id=?""",
            (doc_id,),
        ).fetchall()
        return bool(changes) and all(
            (row["path"], row["before_hash"], row["after_hash"], row["status"])
            == (path, digest, None, "applied")
            for row in changes
        )

    def legacy_move_source(self, db, old, new, digest, identities):
        source, target = identities.get(old), identities.get(new)
        if source is None:
            return target
        reserved = self.legacy_deleted_reservation(db, source, old, digest)
        if target is None or target["id"] == source["id"]:
            if not reserved:
                return source
        else:
            actual_key = self.store.file_key(self.store.path(new).stat())
            # A real source inode still identifies an unrecorded move, including
            # an unindexed edit. A stale destination binding cannot override it.
            if source["file_key"] and source["file_key"].split(":")[:2] == actual_key.split(":")[:2]:
                return source
            # The old handler can reserve the now absent source path after the
            # real identity has already moved. Keep that verified current target;
            # preserve the reservation's history without moving or merging it.
            if reserved and target["file_key"] == actual_key and db.execute(
                "SELECT 1 FROM documents WHERE id=? AND path=?", (target["id"], new),
            ).fetchone():
                return target
        raise WikiError(
            f"Ambiguous external move identity for {old} -> {new}; journal and identities retained", 409,
        )

    def legacy_external_entry(self, before, after, actor, reason):
        """Upgrade legacy inventories, including an already partly applied move.

        An absent old path is not an explicit absence of origin state. A unique
        exact move may already have transferred its identity to the new path.
        Capture that binding once; finish_external then owns replay and indexing.
        """
        removed, added = set(before) - set(after), set(after) - set(before)
        moves = {}
        for old in sorted(removed):
            candidates = [new for new in added if after[new] == before[old]]
            if len(candidates) == 1 and sum(digest == before[old] for digest in before.values()) == 1:
                moves[old] = candidates[0]
        captured = {}
        with self.store.connect() as db:
            ownership = {
                row["path"]: dict(row)
                for row in db.execute("SELECT * FROM managed_notes")
            }
            source_states = {
                row["path"]: dict(row) | {"origin": origins.get(db, row["id"])}
                for row in db.execute("SELECT id,path,file_key FROM document_identities WHERE path IS NOT NULL")
            }
            for path, digest in before.items():
                raw = (self.blobs / digest).read_bytes()
                moved_to = moves.get(path)
                binding = self.legacy_move_source(db, path, moved_to, digest, source_states) if moved_to else source_states.get(path)
                identity, origin = (binding["id"], binding["origin"]) if binding else (None, None)
                if identity is None:
                    identity = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO document_identities VALUES(?,?,?)", (identity, path, ""),
                    )
                managed = ownership.get(binding["path"] if binding else path)
                captured[path] = {
                    "hash": digest, "content": base64.b64encode(raw).decode(),
                    "document_id": identity,
                    "management": managed | {"path": path} if managed else None,
                    "origin_state": origins.preimage(origin, identity, digest),
                }
        return {
            "version": 2, "id": uuid.uuid4().hex, "before": captured,
            "paths": sorted(set(before) | set(after)), "actor": actor, "reason": reason,
        }

    def write(self, relative, raw, expected_revision, actor, reason):
        with self.locked():
            return self.write_locked(relative, raw, expected_revision, actor, reason)

    def write_locked(
        self, relative, raw, expected_revision, actor, reason, management=_UNCHANGED,
        publication=None, restore_document_id=None, target_document_id=None,
        operation="save",
        operation_details=None,
        origin_state=origins.UNCHANGED,
        origin_builder=None,
        source_plan=None,
    ):
        # Consume earlier notifications before attaching/restoring a binding.
        # In particular, a journalled delete must not later retire its successor.
        self.store.synchronize(dirty=False)
        if target_document_id is not None:
            target = self.store.resolve_identity(target_document_id)
            if target is None or target["id"] != target_document_id or target["path"] is None:
                raise WikiError("The selected candidate document is unavailable; evidence retained for manual review", 409)
            relative = target["path"]
            if isinstance(management, dict):
                management = {**management, "path": relative}
        path = self.store.path(relative)
        if path.exists() and not path.is_file():
            raise WikiError("The target is not a regular file", 409)
        before = path.read_bytes() if path.is_file() else None
        identity = path.stat() if before is not None else None
        current = revision(before) if before is not None else None
        if current != expected_revision:
            row = self.record(relative, before, raw, actor, reason, "conflict", proposal={
                "job_id": publication["job_id"], "source_doc": publication["source_doc"], "source_path": publication["source_path"],
                "expected_revision": expected_revision,
            } if publication else None)
            raise WikiError(
                f"Revision conflict; proposal retained as change {row['id']}", 409
            )
        if before is None and raw is not None:
            with self.store.connect() as db:
                restored = db.execute(
                    "SELECT path FROM document_identities WHERE id=?", (restore_document_id,)
                ).fetchone() if restore_document_id else None
                self.store.detach_path(db, relative)
                if restored is not None and restored["path"] in {None, relative}:
                    db.execute(
                        "UPDATE document_identities SET path=?,file_key='' WHERE id=?",
                        (relative, restore_document_id),
                    )
        if raw is None and management is _UNCHANGED:
            management = None
        if operation != "save" and before is not None and before == raw:
            from .operations import record

            document = self.store.read(relative, length=1, expected_revision=current)
            with self.store.connect() as db:
                receipt = record(db, doc_id=document["id"], path=relative, before=current, after=current, operation=operation, reason=reason, publication=publication, details=operation_details, store=self.store)
            return {"path": relative, "revision": current, "change_id": None, "receipt_id": receipt, "changed": False, **self.store.index_state()}
        row = self.record(relative, before, raw, actor, reason, "prepared", management, publication=publication, operation=operation, operation_details=operation_details, origin_state=origin_state, origin_builder=origin_builder, source_plan=source_plan)
        # flock coordinates Octopus writers, not an external editor. Preparing
        # the durable journal can take time: validate both identity and bytes
        # again before changing the live file. This is not a filesystem CAS;
        # an editor ignoring the lock can still race the final syscall.
        def check_target():
            if publication:
                from . import grants

                with self.store.connect() as db:
                    grants.check_write(db, self.store, publication, relative, change_id=row["id"])
            live = None
            try:
                target = self.store.path(relative)
                live = target.read_bytes() if target.is_file() else None
                stat = target.stat() if target.exists() else None
                def fingerprint(value):
                    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if value else None
                unchanged = target == path and live == before and fingerprint(stat) == fingerprint(identity)
            except (OSError, WikiError):
                unchanged = False
            if not unchanged:
                self.status(row["id"], "conflict")
                # Keep the original preimage and proposal, and separately
                # retain the observed human bytes. Recovery never applies a
                # conflict, including one whose bytes equal our proposed bytes.
                self.record(relative, live, raw, actor, reason + "; target changed during journal preparation", "conflict", proposal={
                    "job_id": publication["job_id"], "source_doc": publication["source_doc"], "source_path": publication["source_path"],
                    "expected_revision": expected_revision,
                } if publication else None)
                raise WikiError(f"Target changed during journal preparation; proposal retained as change {row['id']}", 409)
        check_target()
        with file_effect(self.store.health):
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, raw, before_replace=check_target)
            # A new nested path also publishes its ancestor directory entries.
            for directory in path.parents:
                sync_directory(directory)
                if directory == self.store.root:
                    break
        self.status(row["id"], "applied")
        if raw is not None and history_text(relative):
            with self.store.connect() as db:
                if publication:
                    self.store.require_indexed(db, relative)
                    db.execute("UPDATE job_publications SET indexed=1 WHERE change_id=?", (row["id"],))
                    if publication.get("reservation_id"):
                        from . import grants

                        grants.settle_creation(db, self.store, row["id"])
                else:
                    self.store._index(db, relative)
        elif raw is None:
            with self.store.connect() as db:
                db.execute("DELETE FROM documents WHERE path=?", (relative,))
                from . import catalog

                catalog.finish(db)
        with self.store.connect() as db:
            receipt = db.execute("SELECT id FROM operation_receipts WHERE change_id=?", (row["id"],)).fetchone()
        return {"change_id": row["id"], "path": relative, "revision": row["after_hash"], "receipt_id": receipt[0] if receipt else None, "changed": True, **self.store.index_state()}

    def history(self, path, offset=0, limit=50):
        with self.locked():
            return self._history(path, offset, limit)

    def candidate_recovery_error(self, db, row, proposal):
        if not proposal or proposal.get("operation") != "reread-and-organize":
            return None
        source_doc = proposal.get("source_doc")
        if source_doc is None:
            # Legacy rereads inferred retention ownership from a mutable path.
            # The job's exact candidate association can prove the selected ID;
            # neither current path occupants nor matching bytes can prove it.
            job = None
            if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='jobs'").fetchone():
                job = db.execute("SELECT items FROM jobs WHERE id=?", (proposal.get("job_id"),)).fetchone()
            owners = {
                item["doc"] for item in json.loads(job["items"]) if row["id"] in item.get("candidate_changes", [])
            } if job else set()
            if len(owners) != 1:
                return "Legacy candidate ownership cannot be verified; evidence retained for manual review"
            source_doc = owners.pop()
        if source_doc != row["document_id"]:
            return "Candidate belongs to a different selected document; evidence retained for manual review"
        return None

    def preserve_candidate_owners(self, db, job):
        """Retain proven legacy ownership before re-admission replaces items."""
        candidates = {change for item in job["items"] for change in item.get("candidate_changes", [])}
        for change in candidates:
            row = db.execute(
                "SELECT p.change_id AS id,p.operation,r.document_id FROM change_proposals p "
                "JOIN change_retention r ON r.change_id=p.change_id WHERE p.change_id=?", (change,),
            ).fetchone()
            if row is None:
                continue
            try:
                proposal = json.loads(row["operation"])
            except (ValueError, TypeError):
                continue
            if (not isinstance(proposal, dict) or proposal.get("operation") != "reread-and-organize"
                    or proposal.get("job_id") != job["id"] or proposal.get("source_doc") is not None):
                continue
            if self.candidate_recovery_error(db, row, proposal) is None:
                proposal["source_doc"] = row["document_id"]
                db.execute("UPDATE change_proposals SET operation=? WHERE change_id=?",
                           (json.dumps(proposal, ensure_ascii=False), change))

    def _history(self, path, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid history pagination", 422)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT c.*,r.document_id,r.before_available,r.after_available,r.resolved_at,p.operation AS proposal_operation FROM changes c JOIN change_retention r ON r.change_id=c.id LEFT JOIN change_proposals p ON p.change_id=c.id WHERE path=? ORDER BY created_at DESC,c.rowid DESC LIMIT ? OFFSET ?",
                (path, limit + 1, offset),
            ).fetchall()
            recovery_errors = {
                row["id"]: self.candidate_recovery_error(db, row, json.loads(row["proposal_operation"]))
                for row in rows[:limit] if row["proposal_operation"] is not None and not row["resolved_at"]
            }
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item.pop("document_id")
            proposal = item.pop("proposal_operation")
            item["recoverable"] = {
                side: bool(row[f"{side}_available"])
                and (
                    row[f"{side}_hash"] is None
                    or (self.blobs / row[f"{side}_hash"]).is_file()
                )
                for side in ("before", "after")
            }
            if proposal is not None and not row["resolved_at"]:
                item["proposed_operation"] = json.loads(proposal)
            if proposal is not None:
                item["recoverable"]["after"] = False
            if error := recovery_errors.get(row["id"]):
                item["recoverable"] = {"before": False, "after": False}
                item["recovery_error"] = error
            if (
                proposal is None
                and not row["resolved_at"]
                and row["status"] == "conflict"
                and row["after_hash"]
                and item["recoverable"]["after"]
            ):
                raw = (self.blobs / row["after_hash"]).read_bytes()
                item["proposed_content"] = raw.decode("utf-8", errors="replace")[:24000]
            items.append(item)
        return {
            "items": items,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def restore(self, change_id, expected_revision, side, actor):
        with self.locked():
            return self._restore(change_id, expected_revision, side, actor)

    def _restore(self, change_id, expected_revision, side, actor):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT c.*,r.document_id,r.before_available,r.after_available FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE c.id=?",
                (change_id,),
            ).fetchone()
        if row is None:
            raise WikiError("Change not found", 404)
        if side not in {"before", "after"}:
            raise WikiError("Restore side must be before or after")
        with self.store.connect() as db:
            entry = db.execute(
                "SELECT operation FROM change_proposals WHERE change_id=?", (change_id,)
            ).fetchone()
            proposal = json.loads(entry["operation"]) if entry else None
            if error := self.candidate_recovery_error(db, row, proposal):
                raise WikiError(error, 409)
        if proposal and side == "after":
            raise WikiError(
                "This is a structured proposal; re-read and organize after review", 409
            )
        digest = row[f"{side}_hash"]
        if not row[f"{side}_available"] or (
            digest and not (self.blobs / digest).is_file()
        ):
            raise WikiError("This recovery version has expired or is unavailable", 410)
        raw = (self.blobs / digest).read_bytes() if digest else None
        if raw is not None and revision(raw) != digest:
            raise WikiError("Recovery content hash mismatch; current document and evidence retained", 409)
        with self.locked():
            with self.store.connect() as db:
                entry = db.execute(
                    f"SELECT {side}_state FROM change_management WHERE change_id=?",
                    (change_id,),
                ).fetchone()
                origin_state = origins.change_state(db, change_id, side)
                selected_sources = source_bindings.change_plan(db, change_id, side)
            origins.bound(origin_state, row["document_id"], digest)

            def restored_origin(db, document_id, before, after):
                # Restored bytes may form a copy while the source identity is
                # still active elsewhere. Preserve its payload without giving
                # the copy the source's material qualification.
                return origins.UNCHANGED, origins.preimage(origin_state, document_id, digest)

            management = json.loads(entry[0]) if entry else _UNCHANGED
            result = self.write_locked(
                row["path"],
                raw,
                expected_revision,
                actor,
                f"Restore {change_id} {side} as a new revision",
                management,
                restore_document_id=row["document_id"],
                target_document_id=row["document_id"] if proposal and proposal.get("operation") == "reread-and-organize" else None,
                origin_builder=restored_origin,
                source_plan={"plan": selected_sources},
            )
            self.resolve_proposals([change_id])
            return result

    def resolve_proposals(self, change_ids):
        with self.locked():
            with self.store.connect() as db:
                for change_id in change_ids:
                    db.execute(
                        "UPDATE change_retention SET resolved_at=? WHERE change_id=? AND EXISTS(SELECT 1 FROM changes WHERE id=? AND status='conflict')",
                        (datetime.now(UTC).isoformat(), change_id, change_id),
                    )

    def text_paths(self):
        result = []
        for directory, dirs, files in os.walk(self.store.root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(self.store.root).as_posix()
                if (
                    not name.startswith(".")
                    and not path.is_symlink()
                    and history_text(relative)
                ):
                    result.append(relative)
        return result

    def capture_external(self, paths=None):
        """Temporary crash-safe preimages; unchanged text never enters blobs."""
        self.store.synchronize()
        result = {}
        with self.store.connect() as db:
            for relative in self.text_paths() if paths is None else paths:
                path = self.store.path(relative)
                if not history_text(relative) or not path.is_file():
                    continue
                raw = path.read_bytes()
                identity = self.store.identity(db, relative, path.stat())
                owned = db.execute(
                    "SELECT * FROM managed_notes WHERE path=?", (relative,)
                ).fetchone()
                result[relative] = {
                    "hash": revision(raw),
                    "content": base64.b64encode(raw).decode(),
                    "document_id": identity,
                    "management": dict(owned) if owned else None,
                    "origin_state": origins.preimage(origins.get(db, identity), identity, revision(raw)),
                    "source_plan": source_bindings.capture(db, identity, relative, raw, dict(owned) if owned else None),
                }
        return result

    def finish_external(self, entry):
        # Older persisted journals can contain claims for bytes edited before
        # capture. Recheck at consumption as well as at new capture boundaries.
        before = {
            path: old | {"origin_state": origins.preimage(
                old.get("origin_state"), old["document_id"], old["hash"],
            )}
            for path, old in entry["before"].items()
        }
        paths = self.text_paths() if entry["paths"] is None else entry["paths"]
        after = {
            relative: self.store.path(relative).read_bytes()
            for relative in paths
            if history_text(relative) and self.store.path(relative).is_file()
        }
        if not self.store.root.is_dir() or self.store.root.is_symlink():
            raise WikiError("Vault root is unavailable; external mutation evidence retained", 503)
        # Sapphire owns the file effect; Octopus confirms its durability before
        # publishing applied history or retiring the external intent.
        # A same-byte rewrite still has a file effect. The intent's paths
        # determine confirmation; byte differences determine history records.
        affected = set(before) | set(paths)
        with file_effect(self.store.health):
            directories = set()
            for relative in affected:
                path = self.store.path(relative)
                if relative in after:
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
                for directory in path.parents:
                    if directory.is_dir():
                        directories.add(directory)
                    if directory == self.store.root:
                        break
            for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
                sync_directory(directory)
        # Rename is trusted from the request, with bytes validated after success.
        moves = []
        for old in set(before) - set(after):
            candidates = [
                new
                for new in set(after) - set(before)
                if revision(after[new]) == before[old]["hash"]
            ]
            if (
                len(candidates) == 1
                and sum(v["hash"] == before[old]["hash"] for v in before.values()) == 1
            ):
                moves.append((old, candidates[0]))
        # A verified legacy rename can be implemented as copy/unlink. Preserve
        # that one transition in event order; do not discard unrelated deletes
        # or a later replacement of its destination.
        restored = entry.get("restored")
        policies = entry.get("source_policies")
        if policies is None:
            policies = {}
            for relative in after:
                if relative in before and before[relative]["hash"] == revision(after[relative]) and not (restored and restored["path"] == relative):
                    # Legacy journals inventory the whole vault. A note that
                    # did not change keeps its exact current observation;
                    # recovering another file cannot replace its source proof.
                    continue
                old_path = next((old for old, new in moves if new == relative), relative)
                if restored and restored["path"] == relative:
                    raw_plan = restored.get("source_plan")
                    prior = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
                    mode = "selected"
                elif old_path in before:
                    prior = before[old_path].get("source_plan")
                    mode = "continuation"
                else:
                    prior = None
                    mode = "fresh" if entry.get("source_binding_protocol") == 1 else "unknown"
                policies[relative] = {"mode": mode, "prior": prior, "context": entry["id"] + ":external:" + relative}
            entry["source_policies"] = policies
            journal = self.state / "external-change.json"
            if journal.is_file() and json.loads(journal.read_text()).get("id") == entry["id"]:
                with file_effect(self.store.health):
                    atomic(journal, json.dumps(entry).encode())
        self.store.synchronize(
            dirty=False, removed_paths=set(before) - set(after),
            journal_moves=[(old, new, before[old]["document_id"]) for old, new in moves],
            source_plans=policies,
        )
        with self.store.connect() as db:
            for old, new in moves:
                expected = before[old]["document_id"]
                source = db.execute("SELECT id FROM document_identities WHERE path=?", (old,)).fetchone()
                target = db.execute("SELECT id FROM document_identities WHERE path=?", (new,)).fetchone()
                if source and source[0] == expected:
                    self.store.move_identity(old, new, db)
                elif not target or target[0] != expected:
                    raise WikiError(
                        f"External move identity changed for {old} -> {new}; journal retained", 409,
                    )
        if entry["reason"] == "delete":
            # A crash may precede the HTTP receipt. Recover the app's durable
            # trash record by original path and verified bytes, not its name.
            trash_root = Path(entry["trash_root"])
            for relative, old in before.items():
                if relative in after:
                    continue
                candidates = []
                for info_file in trash_root.glob("*/info.json"):
                    if info_file.is_symlink() or info_file.parent.is_symlink():
                        continue
                    info = json.loads(info_file.read_text())
                    archive = info_file.parent / "file"
                    if (
                        info.get("path") == relative
                        and archive.is_file()
                        and not archive.is_symlink()
                        and revision(archive.read_bytes()) == old["hash"]
                    ):
                        candidates.append(info_file.parent.name)
                if len(candidates) == 1:
                    with self.store.connect() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO app_trash(trash_id,path,document_id,management,origin_state,source_plan) VALUES(?,?,?,?,?,?)",
                            (
                                candidates[0],
                                relative,
                                old["document_id"],
                                json.dumps(old["management"]),
                                json.dumps(old.get("origin_state")),
                                json.dumps(old.get("source_plan")),
                            ),
                        )
        if (
            restored
            and restored["path"] not in before
            and restored["path"] in after
            and restored.get("document_id")
        ):
            with self.store.connect() as db:
                identity = db.execute(
                    "SELECT path FROM document_identities WHERE id=?",
                    (restored["document_id"],),
                ).fetchone()
                if identity is None or identity["path"] not in {None, restored["path"]}:
                    raise WikiError("Restored document identity changed; journal retained", 409)
                current = db.execute(
                    "SELECT id FROM document_identities WHERE path=?",
                    (restored["path"],),
                ).fetchone()
                if current is None or current["id"] != restored["document_id"]:
                    # Synchronization may have indexed the returned file as a
                    # new document. Retire that provisional identity and its
                    # derived rows before reattaching the durable trash ID.
                    self.store.detach_path(db, restored["path"])
                db.execute(
                    "UPDATE document_identities SET path=?,file_key=? WHERE id=?",
                    (
                        restored["path"],
                        self.store.file_key(self.store.path(restored["path"]).stat()),
                        restored["document_id"],
                    ),
                )
                if restored.get("management"):
                    managed = json.loads(restored["management"])
                    if managed:
                        db.execute(
                            "INSERT OR REPLACE INTO managed_notes VALUES(:path,:base_hash,:rendered_revision,:regions,:metadata)",
                            managed,
                        )
                db.execute(
                    "DELETE FROM app_trash WHERE trash_id=?", (entry["trash_id"],)
                )
        for relative in sorted(set(before) | set(after)):
            previous = before.get(relative)
            raw = after.get(relative)
            if (previous["hash"] if previous else None) == (
                revision(raw) if raw is not None else None
            ) and not (restored and restored["path"] == relative):
                continue
            doc_id = (
                previous["document_id"]
                if previous
                else next(
                    (
                        before[old]["document_id"]
                        for old, new in moves
                        if new == relative
                    ),
                    None,
                )
            )
            moved_from = next((old for old, new in moves if new == relative), None)
            origin_state = origins.UNCHANGED
            if moved_from is not None:
                origin_state = before[moved_from].get("origin_state")
            elif restored and restored["path"] == relative and previous is None:
                encoded_origin = restored.get("origin_state")
                doc_id = restored["document_id"]
                origin_state = origins.preimage(
                    json.loads(encoded_origin) if encoded_origin is not None else None,
                    doc_id, revision(raw),
                )
            self.record(
                relative,
                base64.b64decode(previous["content"]) if previous else None,
                raw,
                entry["actor"],
                entry["reason"],
                "applied",
                before_management=previous["management"] if previous else None,
                before_origin=previous.get("origin_state") if previous else None,
                origin_state=origin_state,
                document_id=doc_id,
                change_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{entry['id']}:{relative}"
                ).hex,
                before_source_plan={"plan": previous.get("source_plan")} if previous else {"plan": None},
                source_transition=policies.get(relative, {"mode": "unknown", "context": entry["id"] + ":deleted:" + relative}),
            )
            if raw is None and not any(old == relative for old, _ in moves):
                with self.store.connect() as db:
                    self.store.detach_path(db, relative)
            elif raw is not None:
                with self.store.connect() as db:
                    self.store._index(db, relative, raw=raw)

    def inventory(self):
        return {
            relative: self.blob(self.store.path(relative).read_bytes())
            for relative in self.text_paths()
        }

    def snapshot(self, reason):
        raise WikiError(
            "Full-vault snapshot creation is retired; use per-text change history", 410
        )

    def snapshots(self):
        with self.locked():
            return self._snapshots()

    def _snapshots(self):
        directory = self.state / "snapshots"
        retired = {
            path.stem: json.loads(path.read_text())
            for path in (self.state / "retired-snapshots").glob("*.json")
        }
        return {
            "items": [
                json.loads(path.read_text()) | {"recoverable": True}
                for path in sorted(
                    directory.glob("*.json"),
                    key=lambda p: p.stat().st_mtime_ns,
                    reverse=True,
                )
                if path.stem not in retired
            ]
            + list(retired.values())
        }

    def cleanup(self, now=None):
        from .retention import collect

        return collect(self, now)

    def retire_snapshot(self, snapshot_id, reason):
        from .retention import retire

        return retire(self, snapshot_id, reason)

    def restore_snapshot(self, snapshot_id, paths, expected_revisions):
        with self.locked():
            return self._restore_snapshot(snapshot_id, paths, expected_revisions)

    def _restore_snapshot(self, snapshot_id, paths, expected_revisions):
        if len(snapshot_id) != 32 or any(
            c not in "0123456789abcdef" for c in snapshot_id
        ):
            raise WikiError("Invalid snapshot id")
        file = self.state / "snapshots" / f"{snapshot_id}.json"
        if (self.state / "retired-snapshots" / file.name).is_file():
            raise WikiError("This full-vault snapshot has been retired", 410)
        if not file.is_file():
            raise WikiError("Snapshot not found", 404)
        manifest = json.loads(file.read_text())
        if (
            not paths
            or len(paths) != len(set(paths))
            or set(paths) != set(expected_revisions)
        ):
            raise WikiError("Select unique paths with an expected revision for each")
        with self.locked():
            for relative in paths:
                if relative not in manifest["files"]:
                    raise WikiError("Path not present in this snapshot", 404)
                path = self.store.path(relative)
                current = revision(path.read_bytes()) if path.is_file() else None
                if current != expected_revisions[relative]:
                    raise WikiError(f"Snapshot restore conflict: {relative}", 409)
                digest = manifest["files"][relative]
                if (
                    not (self.blobs / digest).is_file()
                    or revision((self.blobs / digest).read_bytes()) != digest
                ):
                    raise WikiError("Snapshot recovery bytes are unavailable", 410)
            results = []
            for relative in paths:
                raw = (self.blobs / manifest["files"][relative]).read_bytes()
                results.append(
                    self.write_locked(
                        relative,
                        raw,
                        expected_revisions[relative],
                        "human",
                        f"Restore snapshot {snapshot_id}",
                        manifest.get("managed_notes", {}).get(relative),
                        origin_state=None,
                        source_plan={"plan": None},
                    )
                )
            return {"items": results}

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

from .store import WikiError, is_maintainable_text, revision

_owners = threading.local()
_UNCHANGED = object()


def history_text(path: str) -> bool:
    return is_maintainable_text(path)


def atomic(path: Path, raw: bytes) -> None:
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


class WikiHistory:
    def __init__(self, store):
        self.store = store
        self.state = store.state
        self.blobs = self.state / "blobs"
        self.blobs.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def under_existing_lock(self, handle):
        """The app bridge transfers its already-held exclusive flock to a worker."""
        if handle.closed:
            raise RuntimeError("The external vault lock has already been released")
        held = getattr(_owners, "held", set())
        _owners.held = held | {str(self.state.resolve())}
        try:
            yield
        finally:
            _owners.held = held

    @contextlib.contextmanager
    def locked(self, *, shared=False):
        held = getattr(_owners, "held", set())
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
        if not path.exists():
            atomic(path, raw)
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
                running = db.execute("SELECT 1 FROM jobs WHERE id=? AND session_id=? AND status='running'", (publication["job_id"], publication["session_id"])).fetchone()
                if not running:
                    raise WikiError("This maintenance assignment is no longer active", 403)
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
            if publication:
                first = db.execute("SELECT input_revision FROM job_publications WHERE job_id=? AND source_doc=? ORDER BY rowid LIMIT 1", (publication["job_id"], publication["source_doc"])).fetchone()
                db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,0)", (
                    publication["job_id"], publication["source_doc"], first[0] if first else publication["input_revision"],
                    row["id"], path, row["after_hash"],
                ))
            if status == "prepared":
                from .operations import record

                record(db, doc_id=document_id, path=path, before=row["before_hash"], after=row["after_hash"], operation=operation, reason=reason, publication=publication, change_id=row["id"], details=operation_details)
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
        external = self.state / "external-change.json"
        if external.exists():
            entry = json.loads(external.read_text())
            if entry.get("version") == 2:
                self.finish_external(entry)
            else:
                self.record_external(
                    {
                        path: digest
                        for path, digest in entry["before"].items()
                        if history_text(path)
                    },
                    self.inventory(),
                    entry["actor"],
                    entry["reason"] + "; recovered external mutation",
                )
            external.unlink()
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
        # Unambiguous exact-content moves cover Sapphire rename and Git renames;
        # inode-based moves are additionally detected by the store's refresh.
        with self.store.connect() as db:
            ownership = {
                row["path"]: dict(row)
                for row in db.execute("SELECT * FROM managed_notes")
            }
        removed = set(before) - set(after)
        added = set(after) - set(before)
        for old in sorted(removed):
            candidates = [new for new in added if after[new] == before[old]]
            if (
                len(candidates) == 1
                and sum(before[path] == before[old] for path in removed) == 1
            ):
                self.store.move_identity(old, candidates[0])
        for path in sorted(set(before) | set(after)):
            previous, current = before.get(path), after.get(path)
            if previous != current:
                self.record(
                    path,
                    (self.blobs / previous).read_bytes() if previous else None,
                    (self.blobs / current).read_bytes() if current else None,
                    actor,
                    reason,
                    "applied",
                    before_management=ownership.get(path),
                )

    def write(self, relative, raw, expected_revision, actor, reason):
        with self.locked():
            return self.write_locked(relative, raw, expected_revision, actor, reason)

    def write_locked(
        self, relative, raw, expected_revision, actor, reason, management=_UNCHANGED,
        publication=None, restore_document_id=None,
        operation="save",
        operation_details=None,
    ):
        path = self.store.path(relative)
        if path.exists() and not path.is_file():
            raise WikiError("The target is not a regular file", 409)
        before = path.read_bytes() if path.is_file() else None
        current = revision(before) if before is not None else None
        if current != expected_revision:
            row = self.record(relative, before, raw, actor, reason, "conflict", proposal={
                "job_id": publication["job_id"], "source_path": publication["source_path"],
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
                receipt = record(db, doc_id=document["id"], path=relative, before=current, after=current, operation=operation, reason=reason, publication=publication, details=operation_details)
            return {"path": relative, "revision": current, "change_id": None, "receipt_id": receipt, "changed": False, **self.store.index_state()}
        row = self.record(relative, before, raw, actor, reason, "prepared", management, publication=publication, operation=operation, operation_details=operation_details)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, raw)
        self.status(row["id"], "applied")
        if raw is not None and history_text(relative):
            with self.store.connect() as db:
                if publication:
                    self.store.require_indexed(db, relative)
                    db.execute("UPDATE job_publications SET indexed=1 WHERE change_id=?", (row["id"],))
                else:
                    self.store._index(db, relative)
        elif raw is None:
            with self.store.connect() as db:
                db.execute("DELETE FROM documents WHERE path=?", (relative,))
        with self.store.connect() as db:
            receipt = db.execute("SELECT id FROM operation_receipts WHERE change_id=?", (row["id"],)).fetchone()
        return {"change_id": row["id"], "path": relative, "revision": row["after_hash"], "receipt_id": receipt[0] if receipt else None, "changed": True, **self.store.index_state()}

    def history(self, path, offset=0, limit=50):
        with self.locked():
            return self._history(path, offset, limit)

    def _history(self, path, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid history pagination", 422)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT c.*,r.before_available,r.after_available,r.resolved_at FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE path=? ORDER BY created_at DESC,c.rowid DESC LIMIT ? OFFSET ?",
                (path, limit + 1, offset),
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["recoverable"] = {
                side: bool(row[f"{side}_available"])
                and (
                    row[f"{side}_hash"] is None
                    or (self.blobs / row[f"{side}_hash"]).is_file()
                )
                for side in ("before", "after")
            }
            with self.store.connect() as db:
                proposal = db.execute(
                    "SELECT operation FROM change_proposals WHERE change_id=?",
                    (row["id"],),
                ).fetchone()
            if proposal and not row["resolved_at"]:
                item["proposed_operation"] = json.loads(proposal[0])
            if proposal:
                item["recoverable"]["after"] = False
            if (
                not proposal
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
            proposal = db.execute(
                "SELECT 1 FROM change_proposals WHERE change_id=?", (change_id,)
            ).fetchone()
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
        with self.locked():
            with self.store.connect() as db:
                entry = db.execute(
                    f"SELECT {side}_state FROM change_management WHERE change_id=?",
                    (change_id,),
                ).fetchone()
            management = json.loads(entry[0]) if entry else _UNCHANGED
            result = self.write_locked(
                row["path"],
                raw,
                expected_revision,
                actor,
                f"Restore {change_id} {side} as a new revision",
                management,
                restore_document_id=row["document_id"],
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
                }
        return result

    def finish_external(self, entry):
        before = entry["before"]
        paths = self.text_paths() if entry["paths"] is None else entry["paths"]
        after = {
            relative: self.store.path(relative).read_bytes()
            for relative in paths
            if history_text(relative) and self.store.path(relative).is_file()
        }
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
        for old, new in moves:
            self.store.move_identity(old, new)
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
                            "INSERT OR REPLACE INTO app_trash VALUES(?,?,?,?)",
                            (
                                candidates[0],
                                relative,
                                old["document_id"],
                                json.dumps(old["management"]),
                            ),
                        )
        restored = entry.get("restored")
        if (
            restored
            and restored["path"] not in before
            and restored["path"] in after
            and restored.get("document_id")
        ):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE document_identities SET path=NULL WHERE path=?",
                    (restored["path"],),
                )
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
            ):
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
            self.record(
                relative,
                base64.b64decode(previous["content"]) if previous else None,
                raw,
                entry["actor"],
                entry["reason"],
                "applied",
                before_management=previous["management"] if previous else None,
                document_id=doc_id,
                change_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{entry['id']}:{relative}"
                ).hex,
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
                    )
                )
            return {"items": results}

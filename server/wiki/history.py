"""Durable mutation journal, content-addressed originals and vault locking."""

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .store import WikiError, revision

_owners = threading.local()
_UNCHANGED = object()


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
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS changes (
              id TEXT PRIMARY KEY,path TEXT NOT NULL,before_hash TEXT,after_hash TEXT,
              actor TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS change_management (
              change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT
            )""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS change_proposals (change_id TEXT PRIMARY KEY,operation TEXT NOT NULL)"
            )

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

    def record(
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
    ):
        row = {
            "id": uuid.uuid4().hex,
            "path": path,
            "before_hash": self.blob(before),
            "after_hash": self.blob(after),
            "actor": actor,
            "reason": reason,
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self.store.connect() as db:
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
                "INSERT INTO change_management VALUES(?,?,?)",
                (row["id"], json.dumps(before_state), json.dumps(after_state)),
            )
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
            after = self.inventory()
            self.record_external(
                entry["before"],
                after,
                entry["actor"],
                entry["reason"] + "; recovered external mutation",
            )
            external.unlink()
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
        self, relative, raw, expected_revision, actor, reason, management=_UNCHANGED
    ):
        path = self.store.path(relative)
        if path.exists() and not path.is_file():
            raise WikiError("The target is not a regular file", 409)
        before = path.read_bytes() if path.is_file() else None
        current = revision(before) if before is not None else None
        if current != expected_revision:
            row = self.record(relative, before, raw, actor, reason, "conflict")
            raise WikiError(
                f"Revision conflict; proposal retained as change {row['id']}", 409
            )
        row = self.record(relative, before, raw, actor, reason, "prepared", management)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, raw)
        self.status(row["id"], "applied")
        return {"change_id": row["id"], "path": relative, "revision": row["after_hash"]}

    def history(self, path, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid history pagination", 422)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM changes WHERE path=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (path, limit + 1, offset),
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            with self.store.connect() as db:
                proposal = db.execute(
                    "SELECT operation FROM change_proposals WHERE change_id=?",
                    (row["id"],),
                ).fetchone()
            if proposal:
                item["proposed_operation"] = json.loads(proposal[0])
            if not proposal and row["status"] == "conflict" and row["after_hash"]:
                raw = (self.blobs / row["after_hash"]).read_bytes()
                item["proposed_content"] = raw.decode("utf-8", errors="replace")[:24000]
            items.append(item)
        return {
            "items": items,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def restore(self, change_id, expected_revision, side, actor):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM changes WHERE id=?", (change_id,)
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
            )
            return result

    def inventory(self):
        result = {}
        for directory, dirs, files in os.walk(self.store.root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d != ".git" and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                result[path.relative_to(self.store.root).as_posix()] = self.blob(
                    path.read_bytes()
                )
        return result

    def snapshot(self, reason):
        with self.locked():
            manifest = {
                "id": uuid.uuid4().hex,
                "created_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "files": self.inventory(),
            }
            with self.store.connect() as db:
                manifest["managed_notes"] = {
                    row["path"]: dict(row)
                    for row in db.execute("SELECT * FROM managed_notes")
                }
            directory = self.state / "snapshots"
            directory.mkdir(exist_ok=True)
            atomic(
                directory / f"{manifest['id']}.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
            )
            # Verify all snapshot bytes before reporting a recoverable snapshot.
            for digest in manifest["files"].values():
                if revision((self.blobs / digest).read_bytes()) != digest:
                    raise WikiError("Snapshot verification failed", 500)
            return manifest

    def snapshots(self):
        directory = self.state / "snapshots"
        return {
            "items": [
                json.loads(path.read_text())
                for path in sorted(
                    directory.glob("*.json"),
                    key=lambda p: p.stat().st_mtime_ns,
                    reverse=True,
                )
            ]
        }

    def restore_snapshot(self, snapshot_id, paths, expected_revisions):
        if len(snapshot_id) != 32 or any(
            c not in "0123456789abcdef" for c in snapshot_id
        ):
            raise WikiError("Invalid snapshot id")
        file = self.state / "snapshots" / f"{snapshot_id}.json"
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

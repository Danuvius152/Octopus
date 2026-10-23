"""Files own content; SQLite retains identity, history and protocol ledgers."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..service_health import ServiceHealth
from . import catalog, portable_metadata, search_index, source_bindings, storage, uids
from . import format as document_format
from .extract import HTML_BODY_VERSION, SUPPORTED, extract
from .formats import MARKDOWN_QUERY_CONTRACT, MARKDOWN_SQL

if TYPE_CHECKING:
    from .raw_blocks import RawCoordinates


class WikiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(eq=False)
class StoreBorrow:
    store: Any
    active: bool = True


def revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def asset_reason(relative: str) -> str | None:
    """Classify known asset directories by purpose, independently of suffix."""
    path = PurePosixPath(relative)
    if path.suffix.lower() == ".pdf":
        return "PDF processing is explicitly excluded"
    if path.parts[0].casefold() == "codex skills":
        return "Agent tool resources"
    if any(
        part.casefold() in {"tmp", "temp", "static", "assets", "node_modules"}
        for part in path.parts[:-1]
    ):
        return "Temporary extraction or supporting static asset"
    return None


def is_maintainable_text(relative: str) -> bool:
    """Text qualification, independent of navigation class and file existence.

    Explicitly assigned Library Markdown in an assets directory remains a note
    for maintenance/history. Temporary and agent tool resources remain excluded;
    this predicate grants neither a task assignment nor permission to create.
    """
    path = PurePosixPath(relative)
    if path.suffix.lower() not in {".md", ".markdown", ".txt", ".html", ".htm"}:
        return False
    if not asset_reason(relative):
        return True
    directories = {part.casefold() for part in path.parts[:-1]}
    return (
        path.parts[0] == "Library"
        and path.suffix.lower() in {".md", ".markdown"}
        and "assets" in directories
        and not directories.intersection({"tmp", "temp", "static", "node_modules", "codex skills"})
    )


def enumeration_error(error: OSError) -> None:
    raise error


def vault_read(function):
    @functools.wraps(function)
    def locked(self, *args, **kwargs):
        with self.operation():
            return read_locked(self, *args, **kwargs)

    def read_locked(self, *args, **kwargs):
        from .history import WikiHistory

        if function.__name__ != "refresh":
            try:
                self.synchronize()
            except WikiError as error:
                if error.status != 503 or function.__name__ not in {"search", "documents"}:
                    raise
        history = WikiHistory(self)
        with history.locked(shared=function.__name__ != "refresh"):
            try:
                return function(self, *args, **kwargs)
            except (OSError, WikiError, sqlite3.DatabaseError) as error:
                if function.__name__ != "refresh":
                    raise
                with self.connect() as db:
                    db.execute("UPDATE index_state SET error=? WHERE singleton=1", (str(error),))
                self.events.needs_reconciliation = True
                raise WikiError("Wiki reconciliation unavailable; existing index retained: " + str(error), 503) from error

    return locked


class WikiStore:
    registration_generation: str | None = None

    def _prepare(self, root, state, vault_id, health, recovery):
        self.health = health if health is not None else ServiceHealth()
        self._lifecycle = threading.Condition()
        self._local = threading.local()
        self._users = 0
        self._retired = False
        self._events_released = True
        self._recovery = recovery
        self._schema_ready = False
        # One immutable raw coordinate map for the last-read revision. No text,
        # parser tokens, document authority or read coverage survives here.
        self._raw_coordinates: tuple[str, RawCoordinates] | None = None
        self.root = root.resolve()
        self.state = state
        self.vault_id = vault_id
        self.db_path = state / "index.sqlite3"
        self._create_database = False

    @classmethod
    def create_database(cls, root: Path, state: Path, vault_id: str, path: Path, *, health: ServiceHealth) -> None:
        """Build an unpublished initial ledger, using the existing schema."""
        from .schema import initialize

        candidate = cls.__new__(cls)
        candidate._prepare(root, state, vault_id, health, None)
        candidate.db_path = path
        candidate._create_database = True
        initialize(candidate)
        with candidate.connect() as db:
            db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)", (str(candidate.root), vault_id))
            db.execute("INSERT INTO wiki_protocols VALUES('registration_config',1)")
        candidate._create_database = False

    def __init__(self, root: Path, state: Path, vault_id: str, *, on_construct=None, recovery=None,
                 health: ServiceHealth | None = None, create: bool = False, defer_recovery: bool = False) -> None:
        self._prepare(root, state, vault_id, health, recovery)
        if on_construct is not None:
            on_construct(self)
        if not create and (not self.db_path.is_file() or self.db_path.is_symlink()):
            raise WikiError("Wiki ledger database is missing; retained registration cannot create a replacement", 503)
        if not create:
            from . import protocols

            binding = protocols.registration_preflight(state.resolve(), health=self.health)
            if binding is not None and binding != {"root": str(self.root), "vault_id": vault_id}:
                raise WikiError("Wiki ledger root or vault binding changed; durable state retained", 503)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        from .events import VaultEvents
        from .schema import initialize

        try:
            self.events = VaultEvents.acquire(self.root, state, health=self.health)
        except OSError as exc:
            raise WikiError(f"Wiki observer unavailable; another process may own this index: {exc}", 503) from exc
        self._events_released = False
        self._events_finalizer = weakref.finalize(self, self.events.release)
        try:
            self._create_database = create
            initialize(self)
        except sqlite3.DatabaseError as exc:
            self.release_events()
            raise WikiError(f"Wiki initialization failed; durable state retained: {exc}", 503) from exc
        except BaseException:
            self.release_events()
            raise
        finally:
            self._create_database = False
        self._synchronizing = threading.local()
        try:
            self._schema_ready = True
            with self.connect():
                pass  # Check the durable handoff gate before ordinary initialization.
            if recovery is not None:
                if self.events is not recovery.observer:
                    raise WikiError("Filesystem recovery observer changed", 503)
                return
            self.events.bind_journal(self.db_path, self.vault_id)
            with self.connect() as db:
                checked = db.execute("SELECT checked_at FROM index_state WHERE singleton=1").fetchone()[0]
                watched = [row[0] for row in db.execute("SELECT path FROM watch_directories")]
            if not defer_recovery:
                from .history import WikiHistory

                WikiHistory(self).recover()
            if checked is None and not defer_recovery:
                self.refresh()
            else:
                for directory in watched:
                    self.events.watch(self.root / directory)
                self.events.needs_reconciliation = True
        except BaseException:
            self.release_events()
            raise

    def release_events(self):
        if not self._events_released:
            self.events.release()
            self._events_finalizer.detach()
            self._events_released = True

    @contextlib.contextmanager
    def borrow(self):
        """A captured operation owns its observer until it releases the lease."""
        with self._lifecycle:
            if self._retired:
                raise WikiError("This Wiki Store has retired; acquire the current registration", 503)
            if self._recovery is None and getattr(getattr(getattr(self, "events", None), "journal", None), "recovery", None) is not None:
                raise WikiError("Filesystem recovery owns this observer; ordinary access stays closed", 503)
            self._users += 1
            lease = StoreBorrow(self)
        try:
            yield lease
        finally:
            with self._lifecycle:
                lease.active = False
                self._users -= 1
                try:
                    if not self._users and self._retired:
                        self.release_events()
                finally:
                    self._lifecycle.notify_all()

    @contextlib.contextmanager
    def operation(self, lease: StoreBorrow | None = None):
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield self
            finally:
                self._local.depth -= 1
            return
        if lease is not None:
            with self._lifecycle:
                if lease.store is not self or not lease.active:
                    raise WikiError("Wiki operation lease has expired", 503)
                self._users += 1
            self._local.depth = 1
            try:
                yield self
            finally:
                self._local.depth = 0
                with self._lifecycle:
                    self._users -= 1
                    try:
                        if not self._users and self._retired:
                            self.release_events()
                    finally:
                        self._lifecycle.notify_all()
            return
        with self.borrow():
            self._local.depth = 1
            try:
                yield self
            finally:
                self._local.depth = 0

    def retire(self) -> None:
        with self._lifecycle:
            self._retired = True
            if not self._users:
                self.release_events()

    def close(self) -> None:
        self.retire()
        with self._lifecycle:
            while self._users:
                self._lifecycle.wait()
            # The last user may have failed to release the observer. Waking
            # from its notification is not evidence that cleanup succeeded.
            self.release_events()

    def index_state(self) -> dict[str, Any]:
        with self.operation():
            return self._index_state()

    def _index_state(self, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        from .index_state import DOCUMENT_ERROR, summary

        with self.connect() if db is None else contextlib.nullcontext(db) as connection:
            if db is None:
                connection.execute("BEGIN")
            state = summary(connection, self.events.needs_reconciliation)
            errors = [{"path": row["path"], "error": json.loads(row["metadata"]).get("frontmatter_error") or json.loads(row["metadata"]).get("error", "Extraction failed")} for row in connection.execute(f"SELECT path,metadata FROM documents WHERE {DOCUMENT_ERROR}")]
        errors.extend({"path": "", "error": error} for error in state.pop("index_diagnostics"))
        state.pop("index_error_count")
        return {**state, "index_errors": errors, "observer_mode": self.events.mode}

    def synchronize(self, *, dirty=True, removed_paths=frozenset(), journal_moves=(), source_plans=None) -> None:
        with self.operation():
            self._synchronize(dirty=dirty, removed_paths=removed_paths, journal_moves=journal_moves, source_plans=source_plans)

    def _synchronize(self, *, dirty=True, removed_paths=frozenset(), journal_moves=(), source_plans=None) -> None:
        """Consume only known changes; full compensation is a background job."""
        if getattr(self._synchronizing, "active", False):
            return
        from .history import WikiHistory

        with WikiHistory(self).locked():
            self._synchronizing.active = True
            try:
                events = self.pending_changes(removed_paths=removed_paths)
                changed = events.paths
                source_plans = source_plans or {}
                changed.update(source_plans)
                if dirty:
                    with self.connect() as db:
                        changed.update(row[0] for row in db.execute("SELECT path FROM documents WHERE signature=''").fetchall())
                if self.events.needs_reconciliation and (not self.root.is_dir() or self.root.is_symlink()):
                    with self.connect() as db:
                        db.execute("UPDATE index_state SET error='Vault unavailable; existing index retained' WHERE singleton=1")
                    return
                if not changed:
                    with self.connect() as db:
                        catalog.finish(db)
                        if self.events.journal is not None:
                            self.events.journal.mark_applied(db, events.receipts)
                    self.acknowledge_changes()
                    return
                if not self.root.is_dir() or self.root.is_symlink():
                    raise WikiError("Vault unavailable; existing index retained", 503)
                paths = set(changed)
                with self.connect() as db:
                    paths.update(self.apply_lifecycle(db, events.lifecycle, journal_moves))
                    for relative in sorted(changed):
                        target = self.root if relative == "" else self.observed_path(relative)
                        if target is not None and target.is_dir():
                            for directory, dirs, files in os.walk(target, followlinks=False, onerror=enumeration_error):
                                dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()]
                                self.events.watch(Path(directory))
                                paths.update((Path(directory) / name).relative_to(self.root).as_posix() for name in files if not name.startswith("."))
                        prefix = relative.rstrip("/") + "/"
                        paths.update(row[0] for row in db.execute("SELECT path FROM document_identities WHERE substr(path,1,?)=?", (len(prefix), prefix)))
                    # Moves must be discovered before missing identities detach.
                    for relative in sorted(paths):
                        target = self.observed_path(relative)
                        if target is not None and target.is_file() and target.suffix.lower() in SUPPORTED and not any(part.startswith(".") for part in PurePosixPath(relative).parts):
                            current = db.execute("SELECT signature FROM documents WHERE path=?", (relative,)).fetchone()
                            stat = target.stat()
                            if relative in source_plans or relative in events.written or current is None or current[0] != f"{stat.st_mtime_ns}:{stat.st_size}:v3":
                                self._index(db, relative, finalize=False, source_policy=source_plans.get(relative))
                    for relative in sorted(paths):
                        target = self.observed_path(relative)
                        if target is None or not target.is_file() or any(part.startswith(".") for part in PurePosixPath(relative).parts):
                            self.detach_path(db, relative)
                    catalog.finish(db)
                    if self.events.journal is not None:
                        self.events.journal.mark_applied(db, events.receipts)
                self.acknowledge_changes()
            except (OSError, WikiError, sqlite3.DatabaseError) as error:
                self.events.needs_reconciliation = True
                with self.connect() as db:
                    db.execute("UPDATE index_state SET error=? WHERE singleton=1", (str(error),))
                if journal_moves and isinstance(error, WikiError) and error.status == 409:
                    raise
                raise WikiError("Known Wiki change awaits reconciliation: " + str(error), 503) from error
            finally:
                self._synchronizing.active = False

    @contextlib.contextmanager
    def connect(self):
        with self.operation():
            with storage.connect(self.db_path, self.health, create=self._create_database) as db:
                if self._schema_ready:
                    from . import recovery_state

                    recovery_state.access(db, self.root, self.vault_id, self._recovery, health=self.health)
                yield db

    def path(self, relative: str) -> Path:
        logical = PurePosixPath(relative)
        if (
            not relative
            or logical.is_absolute()
            or "\\" in relative
            or any(p in {"", ".", "..", ".git"} for p in relative.split("/"))
        ):
            raise WikiError("Invalid vault-relative path", 404)
        target = self.root.joinpath(*logical.parts)
        if not target.resolve().is_relative_to(self.root):
            raise WikiError("Path escapes vault", 404)
        for parent in [target, *target.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise WikiError("Symlink access is not supported", 404)
        return target

    def observed_path(self, relative: str) -> Path | None:
        """Unsupported external entries cannot become indexed documents."""
        try:
            return self.path(relative)
        except WikiError:
            return None

    @staticmethod
    def file_key(stat) -> str:
        return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}"

    def pending_changes(self, *, removed_paths=frozenset()):
        """Keep observed lifecycles until the index transaction commits."""
        self.events.reconcile_applied()
        queued = self.events.drain()
        if queued.pending_moves:
            with self.connect() as db:
                active = {row[0] for row in db.execute("SELECT path FROM document_identities WHERE path IS NOT NULL AND file_key<>''")}
            self.events.settle_inactive(active, removed_paths)
            queued = self.events.drain()
        self.events.queued.merge(queued)
        if queued.pending_moves:
            raise WikiError("A filesystem move awaits its matching notification", 503)
        return self.events.queued

    def acknowledge_changes(self):
        from .events import FileChanges

        if self.events.journal is not None:
            self.events.journal.acknowledge(self.events.queued.receipts)
        self.events.queued = FileChanges()

    @staticmethod
    def event_affects(event, path):
        return event.path == path or (event.directory and (not event.path or path.startswith(event.path.rstrip("/") + "/")))

    def journal_transitions(self, db, lifecycle, moves):
        transitions = {}
        for old, new, expected in moves:
            # Already-applied journals and matched kernel renames need no
            # interpretation. Empty-key legacy reservations are not sources.
            source = db.execute("SELECT id,file_key FROM document_identities WHERE path=?", (old,)).fetchone()
            if source is None or source["id"] != expected or not source["file_key"]:
                continue
            source_events = [(index, event) for index, event in enumerate(lifecycle) if self.event_affects(event, old)]
            if any(event.kind == "move" for _index, event in source_events):
                continue
            ended = [(index, event) for index, event in source_events if event.kind == "delete"]
            if not ended:
                continue
            target = db.execute("SELECT id FROM document_identities WHERE path=?", (new,)).fetchone()
            if len(ended) != 1 or any(self.event_affects(event, new) for event in lifecycle) or (target is not None and target["id"] != expected):
                raise WikiError("Ambiguous external copy/unlink move; journal and filesystem events retained", 409)
            transitions[(ended[0][0], old)] = (new, expected)
        return transitions

    def apply_lifecycle(self, db, lifecycle, journal_moves=()):
        """Replay observed bindings in order, before indexing terminal bytes.

        The destination may already have moved or been deleted again. Its
        terminal inode cannot validate an earlier rename; the matched kernel
        cookie provides that transition. Hidden temporary sources have no
        binding, so an atomic save keeps its destination identity.
        """
        paths = set()
        transitions = self.journal_transitions(db, lifecycle, journal_moves)
        for index, event in enumerate(lifecycle):
            old, new = event.path, event.destination
            prefix = old.rstrip("/") + "/"
            bound = [row[0] for row in db.execute(
                "SELECT path FROM document_identities WHERE (? OR path=? OR (? AND substr(path,1,?)=?)) AND (? OR file_key<>'')",
                (old == "" and event.directory, old, event.directory, len(prefix), prefix, event.kind != "move"),
            ).fetchall()]
            for old_path in bound:
                paths.add(old_path)
                trusted = transitions.get((index, old_path))
                current = db.execute("SELECT id FROM document_identities WHERE path=?", (old_path,)).fetchone()
                if trusted and current is not None and current["id"] == trusted[1]:
                    self.move_identity(old_path, trusted[0], db, observed=True)
                    paths.add(trusted[0])
                elif event.kind == "delete":
                    self.detach_path(db, old_path)
                elif event.kind == "move":
                    new_path = new + old_path[len(old):]
                    self.move_identity(old_path, new_path, db, observed=True)
                    paths.add(new_path)
        return paths

    def move_identity(self, old: str, new: str, db=None, *, observed=False) -> None:
        """Move trusted identity/ownership without touching any note bytes."""
        if db is None:
            with self.connect() as connection:
                self.move_identity(old, new, connection, observed=observed)
            return
        if not observed:
            self.path(old)
        # Observed intermediate names may now be missing, hidden, symlinks or
        # unsupported API coordinates. Only terminal paths are opened/indexed.
        target = None if observed else self.path(new)
        identity = db.execute(
            "SELECT id,file_key FROM document_identities WHERE path=?", (old,)
        ).fetchone()
        if identity is None:
            return
        # A prior deleted document at the destination keeps its ID as a tombstone.
        displaced = db.execute("SELECT id FROM document_identities WHERE path=?", (new,)).fetchone()
        if displaced and displaced[0] != identity[0]:
            self.detach_path(db, new)
        db.execute(
            "UPDATE document_identities SET path=?,file_key=? WHERE id=?",
            (new, identity["file_key"] if target is None else self.file_key(target.stat()), identity[0]),
        )
        db.execute(
            "UPDATE documents SET path=?,signature='' WHERE id=?", (new, identity[0])
        )
        owned = db.execute(
            "SELECT 1 FROM managed_notes WHERE path=?", (old,)
        ).fetchone()
        if owned:
            db.execute("DELETE FROM managed_notes WHERE path=?", (new,))
            db.execute("UPDATE managed_notes SET path=? WHERE path=?", (new, old))

    def identity(self, db, relative, stat) -> str:
        key = self.file_key(stat)
        identity = db.execute(
            "SELECT id FROM document_identities WHERE path=?", (relative,)
        ).fetchone()
        if identity is None:
            candidates = [
                row
                for row in db.execute(
                    "SELECT id,path FROM document_identities WHERE file_key=? AND path IS NOT NULL",
                    (key,),
                )
                if not self.path(row["path"]).exists()
            ]
            if len(candidates) == 1:
                self.move_identity(candidates[0]["path"], relative, db)
                identity = candidates[0]
        if identity:
            doc_id = identity["id"]
            db.execute(
                "UPDATE document_identities SET file_key=? WHERE id=?", (key, doc_id)
            )
        else:
            doc_id = uuid.uuid5(uuid.UUID(self.vault_id), relative).hex
            if db.execute(
                "SELECT 1 FROM document_identities WHERE id=?", (doc_id,)
            ).fetchone():
                doc_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO document_identities VALUES(?,?,?)", (doc_id, relative, key)
            )
        return doc_id

    def detach_path(self, db, relative):
        """Observed deletion ends a binding; historical IDs remain tombstones."""
        from . import origins

        origins.detach(db, relative)
        db.execute(
            "UPDATE document_identities SET path=NULL,file_key='' WHERE path=?",
            (relative,),
        )
        if db.execute("DELETE FROM documents WHERE path=?", (relative,)).rowcount:
            db.execute("UPDATE index_state SET epoch=epoch+1 WHERE singleton=1")
            db.execute("UPDATE wiki_edges SET status='missing' WHERE target_doc IN (SELECT id FROM document_identities WHERE path IS NULL)")
        db.execute("DELETE FROM managed_notes WHERE path=?", (relative,))

    def _index(self, db: sqlite3.Connection, relative: str, *, raw: bytes | None = None, finalize: bool = True, source_policy=None, prepared=None) -> sqlite3.Row:
        path = self.path(relative)
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            raise WikiError("Document not found", 404)
        stat = path.stat()
        # Excluded PDFs are navigation assets: never load attachment bytes to
        # update the index after an unrelated text save.
        if raw is None:
            raw = b"" if path.suffix.lower() == ".pdf" else path.read_bytes()
        digest = revision(f"excluded-pdf:{stat.st_mtime_ns}:{stat.st_size}".encode()) if path.suffix.lower() == ".pdf" else revision(raw)
        doc_id = self.identity(db, relative, stat)
        from . import origins

        origins.observe(db, doc_id, digest)
        signature = f"{stat.st_mtime_ns}:{stat.st_size}:v3"
        existing = db.execute(
            "SELECT * FROM documents WHERE path=?", (relative,)
        ).fetchone()
        previous_metadata = json.loads(existing["metadata"]) if existing is not None else {}
        if prepared is not None and "source_draft" in prepared:
            draft = prepared["source_draft"]
            if (draft["source_doc"], draft["path"], draft["revision"]) != (doc_id, relative, digest):
                raise WikiError("Prepared recovery source proof changed", 409)
            source_bindings.stage(db, draft)
        elif source_policy is not None:
            managed = db.execute("SELECT * FROM managed_notes WHERE path=?", (relative,)).fetchone()
            supplied = source_bindings.metadata(db, doc_id, relative, raw, dict(managed) if managed else None)
            source_bindings.stage(db, source_bindings.incoming(
                db, doc_id, relative, digest, source_bindings.entries(supplied), source_policy))
        if (
            prepared is None and source_policy is None
            and
            existing is not None
            and existing["id"] == doc_id
            and existing["signature"]
            and existing["revision"] == digest
            and previous_metadata.get("pipeline_version")
            == "octopus-extract-v3"
            and previous_metadata.get("format_version") == document_format.VERSION
            and previous_metadata.get("metadata_validation_version") == portable_metadata.VERSION
            and (path.suffix.lower() not in {".html", ".htm"}
                 or previous_metadata.get("html_body_version") == HTML_BODY_VERSION)
        ):
            from . import materials

            materials.index(db, existing, raw)
            db.execute(
                "UPDATE documents SET signature=? WHERE id=?",
                (signature, existing["id"]),
            )
            if not search_index.current(db, existing):
                search_index.index(db, existing, json.loads(existing["metadata"]))
                db.execute("UPDATE index_state SET epoch=epoch+1 WHERE singleton=1")
            if finalize:
                catalog.finish(db)
            return db.execute(
                "SELECT * FROM documents WHERE id=?", (existing["id"],)
            ).fetchone()
        try:
            extracted = prepared["extracted"] if prepared is not None else extract(raw, path.suffix.lower())
        except Exception as exc:
            extracted = {
                "text": "",
                "pages": [],
                "quality": "error",
                "metadata": {"error": f"{type(exc).__name__}: {exc}"},
            }
        text = extracted["text"]
        metadata = dict(extracted["metadata"])
        metadata["metadata_validation_version"] = portable_metadata.VERSION
        if path.suffix.lower() == ".pdf":
            metadata["revision_kind"] = "stat"
        portable = re.search(r"<!-- octopus-wiki\s+(.*?)\s*-->", text, re.S)
        if portable:
            try:
                supplied = portable_metadata.validate(json.loads(portable[1]))
                # Extraction quality and tool versions cannot be supplied by a document.
                metadata |= {
                    k: v for k, v in supplied.items() if k not in extracted["metadata"]
                }
            except (ValueError, TypeError, RecursionError):
                metadata["metadata_error"] = "Invalid octopus-wiki metadata"
        kind = metadata.get("kind", "original")
        if kind not in {"original", "source", "concept", "topic", "submission"}:
            kind = "original"
        exclusion = asset_reason(relative)
        if exclusion:
            kind = "asset"
            metadata["asset_reason"] = exclusion
        parsed = prepared["parsed"] if prepared is not None else document_format.parse(text)
        if parsed.error and existing is not None and existing["id"] == doc_id:
            if not previous_metadata.get("frontmatter_error") or previous_metadata.get("frontmatter_retained_from"):
                for name in ("title", "description", "type", "status", "resource", "uid",
                             "generated", "x_wiki", "sources", "verified", "tags", "aliases", "doc_role"):
                    if name in previous_metadata:
                        metadata[name] = previous_metadata[name]
                metadata["frontmatter_retained_from"] = previous_metadata.get("frontmatter_retained_from") or {
                    "doc": doc_id, "revision": existing["revision"],
                }
        projection = parsed.metadata(metadata)
        if parsed.error and metadata.get("frontmatter_retained_from"):
            projection["doc_role"] = metadata.get("doc_role", "unknown")
        metadata |= projection
        if kind == "original":
            kind = {"source": "source", "reference": "source", "concept": "concept", "navigation": "topic", "synthesis": "topic"}.get(metadata.get("doc_role"), kind)
        uids.observe(db, doc_id, metadata)
        title = metadata.get("title") or parsed.title(path.stem)
        db.execute(
            """INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET path=excluded.path,revision=excluded.revision,
                title=excluded.title,kind=excluded.kind,text=excluded.text,pages=excluded.pages,
                quality=excluded.quality,metadata=excluded.metadata,signature=excluded.signature""",
            (
                doc_id,
                relative,
                digest,
                title,
                kind,
                text,
                json.dumps(extracted["pages"]),
                extracted["quality"],
                json.dumps(metadata, ensure_ascii=False),
                signature,
            ),
        )
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        assert row is not None
        managed = db.execute("SELECT metadata FROM managed_notes WHERE path=?", (relative,)).fetchone()
        catalog.index(db, row, metadata | (json.loads(managed[0]) if managed else {}))
        search_index.index(db, row, metadata, parsed=parsed)
        from . import materials

        materials.index(db, row, raw)
        db.execute("UPDATE index_state SET epoch=epoch+1 WHERE singleton=1")
        if finalize:
            catalog.finish(db)
        return db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    @vault_read
    def refresh(self) -> None:
        from . import materials

        if not self.root.is_dir() or self.root.is_symlink():
            raise WikiError("Vault unavailable; existing index retained", 503)
        # Receipt must commit independently before any application transaction.
        queued = self.pending_changes()
        with self.connect() as db:
            self.apply_lifecycle(db, queued.lifecycle)
            found = set()
            if self.events.mode == "inotify":
                self.events.error = None
            for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=enumeration_error):
                self.events.watch(Path(directory))
                db.execute("INSERT OR IGNORE INTO watch_directories VALUES(?)", (Path(directory).relative_to(self.root).as_posix(),))
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not d.startswith(".") and not (Path(directory) / d).is_symlink()
                )
                for name in sorted(files):
                    path = Path(directory) / name
                    if (
                        name.startswith(".")
                        or path.is_symlink()
                        or path.suffix.lower() not in SUPPORTED
                    ):
                        continue
                    relative = path.relative_to(self.root).as_posix()
                    if self.observed_path(relative) is None:
                        continue
                    found.add(relative)
                    stat = path.stat()
                    old = db.execute(
                        "SELECT d.signature,n.version,p.revision,p.ownership,coalesce(m.regions,''),NOT EXISTS(SELECT 1 FROM search_sections s WHERE s.doc_id=d.id) OR EXISTS(SELECT 1 FROM search_sections s LEFT JOIN search_fts f ON f.rowid=s.id WHERE s.doc_id=d.id AND f.rowid IS NULL),json_extract(d.metadata,'$.format_version'),json_extract(d.metadata,'$.html_body_version'),o.state FROM documents d LEFT JOIN search_documents n ON n.doc_id=d.id LEFT JOIN raw_materials p ON p.doc_id=d.id LEFT JOIN managed_notes m ON m.path=d.path LEFT JOIN material_origins o ON o.doc_id=d.id WHERE d.path=?", (relative,)
                    ).fetchone()
                    if relative in queued.written or old is None or old[0] != f"{stat.st_mtime_ns}:{stat.st_size}:v3" or old[1] != search_index.VERSION or old[5] or old[6] != document_format.VERSION or (path.suffix.lower() in {".html", ".htm"} and old[7] != HTML_BODY_VERSION) or (not asset_reason(relative) and (old[2] is None or old[3] != materials.ownership(old[4], old[8]))):
                        self._index(db, relative, finalize=False)
            # Discover moves before detaching missing paths. This also covers
            # missing files when only the durable ledger survived a rebuild.
            for row in db.execute("SELECT path FROM document_identities WHERE path IS NOT NULL").fetchall():
                observed = self.observed_path(row["path"])
                if observed is None or not observed.is_file() or any(part.startswith(".") for part in PurePosixPath(row["path"]).parts):
                    self.detach_path(db, row["path"])
            for row in db.execute("SELECT id,path FROM documents").fetchall():
                if row["path"] not in found:
                    db.execute("DELETE FROM documents WHERE id=?", (row["id"],))
            catalog.finish(db)
            db.execute("UPDATE index_state SET checked_at=?,error=NULL WHERE singleton=1", (datetime.now(UTC).isoformat(),))
            if self.events.journal is not None:
                self.events.journal.mark_applied(db, queued.receipts)
        self.acknowledge_changes()
        self.events.needs_reconciliation = bool(self.events.error)

    def rebuild_search(self) -> None:
        """Rebuild only derived chapters/FTS, retaining all durable ledgers."""
        from .history import WikiHistory

        with WikiHistory(self).locked():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DROP TRIGGER IF EXISTS search_section_delete")
                db.execute("DROP TRIGGER IF EXISTS search_document_delete")
                for name in ("search_fts", "search_sections", "search_documents"):
                    db.execute(f"DROP TABLE IF EXISTS {name}")
                search_index.initialize(db)
            self.refresh()

    def require_indexed(self, db, path):
        """A published revision is complete only after usable extraction and FTS."""
        row = self._index(db, path)
        if row["quality"] == "error":
            raise WikiError("Published text awaits index recovery: " + json.loads(row["metadata"]).get("error", "Extraction failed"), 503)
        if not search_index.current(db, row) or not db.execute(
            "SELECT 1 FROM search_sections WHERE doc_id=? LIMIT 1", (row["id"],)
        ).fetchone():
            raise WikiError("Published text awaits chapter index recovery", 503)
        return row

    def indexed_metadata(self, row):
        """Navigation metadata only; current evidence is read separately."""
        native = json.loads(row["metadata"])
        if "managed_metadata" in row.keys():
            supplied = json.loads(row["managed_metadata"] or "{}")
        else:
            with self.connect() as db:
                managed = db.execute("SELECT metadata FROM managed_notes WHERE path=?", (row["path"],)).fetchone()
            supplied = json.loads(managed[0]) if managed else {}
        merged = native | supplied
        for field in ("tags", "aliases"):
            if field in native or field in supplied:
                merged[field] = list(dict.fromkeys([*native.get(field, []), *supplied.get(field, [])]))
        return merged

    def public(self, row: sqlite3.Row, *, raw: bytes | None = None, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        metadata = json.loads(row["metadata"])
        from .sections import managed_metadata

        if raw is None:
            raw = b"" if Path(row["path"]).suffix.lower() == ".pdf" else self.path(row["path"]).read_bytes()
        managed, stale = managed_metadata(self, row["path"], raw, db=db)
        native_fields = {field: metadata.get(field, []) for field in ("tags", "aliases")}
        metadata |= managed
        for field, original in native_fields.items():
            if original or field in managed:
                metadata[field] = list(dict.fromkeys([*original, *managed.get(field, [])]))
        with self.connect() if db is None else contextlib.nullcontext(db) as db:
            refs = catalog.dependencies(db, row["id"])
            metadata["source_refs"] = refs
            for ref in refs:
                source_id = ref.get("doc") or ""
                # A missing durable ID must stay missing; a reused path is not its source.
                identified = bool(re.fullmatch(r"[0-9a-f]{32}", source_id))
                source = db.execute(
                    "SELECT revision,path,signature FROM documents WHERE "
                    + ("id=?" if identified else "path=?"),
                    (source_id if identified else source_id or ref.get("path"),),
                ).fetchone()
                current = source["revision"] if source else None
                if source:
                    try:
                        file = self.path(source["path"])
                        stat = file.stat()
                        if (
                            f"{stat.st_mtime_ns}:{stat.st_size}:v3"
                            != source["signature"]
                        ):
                            current = revision(f"excluded-pdf:{stat.st_mtime_ns}:{stat.st_size}".encode()) if file.suffix.lower() == ".pdf" else revision(file.read_bytes())
                    except (OSError, WikiError):
                        current = None
                if source:
                    ref["path"] = source["path"]
                    for original in metadata.get("sources", []):
                        if original.get("doc") == source_id:
                            original["path"] = source["path"]
                if ref.get("revision") and ref["status"] not in {"external", "unknown"} and current != ref.get("revision"):
                    stale = True
                    ref["status"] = "changed" if current else "missing"
                ref["current_revision"] = current
        return {
            k: row[k] for k in ("id", "path", "revision", "title", "kind", "quality")
        } | {
            "metadata": metadata,
            "pages": json.loads(row["pages"]),
            "stale": stale,
            "characters": len(row["text"]),
        }

    def navigation(self, row):
        """Index-only projection; it does not grant a content read receipt."""
        with self.connect() as db:
            refs = catalog.dependencies(db, row["id"])
        return {key: row[key] for key in ("id", "path", "revision", "title", "kind", "quality")} | {
            "metadata": self.indexed_metadata(row) | {"source_refs": refs},
            "pages": json.loads(row["pages"]), "characters": len(row["text"]),
            "stale": any(ref["status"] == "changed" or ref["status"] == "missing" and ref["revision"] for ref in refs),
            "freshness": "indexed",
        }

    @vault_read
    def search(
        self,
        query: str,
        limit: int = 12,
        offset: int = 0,
        kind: str | None = None,
        include_assets: bool = False,
        directory: str | None = None,
        intent: str | None = None,
        view: str = "full",
        cursor: str | None = None,
        *, model_response: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50 or offset < 0 or len(query) > 1000:
            raise WikiError("Invalid search pagination or query length", 422)
        if not query.strip():
            raise WikiError("Search query is required")
        if directory:
            self.path(directory.rstrip("/"))
        from .retrieval import search

        try:
            return search(self, query, limit, offset, kind, include_assets, directory, intent, view, cursor, model_response)
        except sqlite3.DatabaseError as exc:
            raise WikiError(f"Chapter search failed; rebuild derived search index: {exc}", 503) from exc

    @vault_read
    def documents(
        self,
        *,
        kind=None,
        quality=None,
        source=None,
        offset=0,
        limit=30,
        include_assets=False,
        directory=None,
        view="full",
        cursor=None,
        model_response=False,
    ):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid document pagination", 422)
        if view not in {"full", "compact"}:
            raise WikiError("Unknown document view", 422)
        if directory:
            self.path(directory.rstrip("/"))
        from .retrieval import compact, continuation, pagination

        offset, epoch, fingerprint = pagination(self, cursor, offset, {"kind": kind, "quality": quality, "source": source, "assets": include_assets, "directory": directory, "view": view, "query_contract": MARKDOWN_QUERY_CONTRACT})
        with self.connect() as db:
            filters, arguments = [MARKDOWN_SQL], []
            if not include_assets and kind != "asset":
                filters.append("d.kind<>'asset'")
            if directory:
                # substr treats '%' and '_' as literal directory characters.
                prefix = directory.rstrip("/") + "/"
                filters.append("substr(d.path,1,?)=?")
                arguments.extend([len(prefix), prefix])
            for column, value in (("kind", kind), ("quality", quality)):
                if value:
                    filters.append(f"d.{column}=?")
                    arguments.append(value)
            if source:
                # Managed sources override native sources, as public() does.
                # Resolve stable IDs to their current path for renamed sources.
                filters.append("EXISTS(SELECT 1 FROM wiki_edges e LEFT JOIN documents src ON src.id=e.target_doc WHERE e.source_doc=d.id AND e.relation='derived_from' AND (e.target_doc=? OR src.path=? OR e.resource=?))")
                arguments.extend([source, source, source])
            relation = " FROM documents d LEFT JOIN managed_notes m ON m.path=d.path"
            predicate = " WHERE " + " AND ".join(filters) if filters else ""
            total = db.execute("SELECT count(*)" + relation + predicate, arguments).fetchone()[0]
            rows = db.execute(
                "SELECT d.*,m.metadata AS managed_metadata" + relation + predicate + " ORDER BY d.title,d.path LIMIT ? OFFSET ?",
                [*arguments, limit, offset],
            ).fetchall()
            items = [compact(self, row) if view == "compact" else self.navigation(row) for row in rows]
        from .read_response import compact_index

        state = self.index_state()
        result = {
            "items": items,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
            "next_cursor": continuation(offset + limit if offset + limit < total else None, epoch, fingerprint),
            "view": view,
            **(compact_index(state) if view == "compact" else state),
        }
        if model_response:
            from .model_response import page

            return page(items, result, offset, total, lambda following: continuation(following, epoch, fingerprint))
        return result

    @vault_read
    def graph(self, doc, offset=0, limit=100, cursor=None, *, model_response=False):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid graph pagination", 422)
        center = self.read(doc, length=1)
        return catalog.graph(self, center["id"], offset, limit, cursor, model_response=model_response)

    def read(
        self,
        doc: str,
        *,
        start: int = 0,
        length: int = 16000,
        expected_revision: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        document, _ = self.read_snapshot(
            doc, start=start, length=length, expected_revision=expected_revision, page=page
        )
        return document

    @vault_read
    def read_snapshot(
        self,
        doc: str,
        *,
        start: int = 0,
        length: int = 16000,
        expected_revision: str | None = None,
        page: int | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        """Return the public read and the exact bytes from which it was indexed."""
        if start < 0 or not 1 <= length <= 24000 or (page is not None and page < 1):
            raise WikiError("Invalid read range", 422)
        identity = self.resolve_identity(doc)
        with self.connect() as db:
            if identity is not None and identity[1] is None:
                raise WikiError("Document not found", 404)
            relative = identity[1] if identity else doc
            file = self.path(relative)
            if not file.is_file() or file.suffix.lower() not in SUPPORTED:
                if not self.root.is_dir():
                    raise WikiError("Vault unavailable; existing index retained", 503)
                raise WikiError("Document not found", 404)
            raw = b"" if file.suffix.lower() == ".pdf" else file.read_bytes()
            row = self._index(db, relative, raw=raw)
            if identity is not None and row["id"] != identity["id"]:
                raise WikiError("Document identity changed during read", 409)
            if expected_revision and expected_revision != row["revision"]:
                raise WikiError("Document changed; read the current revision", 409)
            text = row["text"]
            page_end = len(text)
            page_offset = 0
            if page is not None:
                pages = json.loads(row["pages"])
                if page < 1 or page > len(pages):
                    raise WikiError("PDF page not found", 404)
                selected = pages[page - 1]
                page_offset = selected["start"]
                start += selected["start"]
                page_end = selected["end"]
            start = min(start, page_end)
            end = min(page_end, start + length)
            return self.public(row, raw=raw, db=db) | {
                "content": text[start:end],
                "page": page,
                "start": start - page_offset,
                "end": end - page_offset,
                "next_start": end - page_offset if end < page_end else None,
                "line_start": text[:start].count("\n") + 1,
                "line_end": text[:end].count("\n") + 1,
                "freshness": "live", **self._index_state(db=db),
            }, raw

    def resolve_identity(self, doc):
        with self.connect() as db:
            alias = db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (doc.casefold(),)).fetchone()
            identifier = alias[0] if alias else doc
            return db.execute("SELECT id,path FROM document_identities WHERE id=?", (identifier,)).fetchone() or db.execute("SELECT id,path FROM document_identities WHERE path=?", (doc,)).fetchone()

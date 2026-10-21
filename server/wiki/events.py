"""Filesystem change notifications; compensation scans stay outside queries.

Linux uses the nonblocking inotify ABI documented in inotify(7). Other
platforms, unavailable watches and overflow use the registry's periodic
reconciliation. A notification is a hint to read a file, never its content.
Observed delete/move order also ends document bindings. A scan cannot recover
an unobserved unlink/recreate, or distinguish it from an atomic save.
"""

import ctypes
import fcntl
import io
import os
import select
import struct
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HEADER = struct.Struct("iIII")
_MODIFICATIONS = 0x00000FCE
_ONLY_DIRECTORY = 0x01000000
_NO_SYMLINKS = 0x02000000
_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_UNMOUNT = 0x00002000
_OBSERVERS: weakref.WeakValueDictionary[tuple[int, str, str], "VaultEvents"] = weakref.WeakValueDictionary()
_OBSERVERS_LOCK = threading.RLock()


@dataclass
class FileEvent:
    kind: str
    path: str
    destination: str = ""
    directory: bool = False
    watches: tuple[int, ...] = ()


@dataclass
class FileChanges:
    paths: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    lifecycle: list[FileEvent] = field(default_factory=list)
    pending_moves: bool = False
    receipts: list[int] = field(default_factory=list)

    @property
    def moves(self) -> list[tuple[str, str, bool]]:
        return [(event.path, event.destination, event.directory)
                for event in self.lifecycle if event.kind == "move"]

    def merge(self, other: "FileChanges") -> None:
        self.paths.update(other.paths)
        self.written.update(other.written)
        self.lifecycle.extend(other.lifecycle)
        self.receipts.extend(value for value in other.receipts if value not in self.receipts)


class VaultEvents:
    @classmethod
    def acquire(cls, root: Path, state: Path, *, health):
        # Registry replacement may retain old operation leases. They must share
        # both the observer and uncommitted events, rather than replay a deletion
        # against the successor identity. One process owns this state at a time.
        key = (os.getpid(), str(root), str(state.resolve()))
        with _OBSERVERS_LOCK:
            observer = _OBSERVERS.get(key)
            if observer is not None and not observer._closed and observer.health is not health:
                from .store import WikiError

                raise WikiError("Filesystem observer belongs to another service generation", 503)
            if observer is None or observer._closed:
                owner = (state / "observer.lock").open("a+b")
                try:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observer = cls(root, health=health)
                except BaseException:
                    owner.close()
                    raise
                observer._owner = owner
                _OBSERVERS[key] = observer
            observer._references += 1
            return observer

    def __init__(self, root: Path, *, health=None):
        from ..service_health import ServiceHealth

        self.health = health if health is not None else ServiceHealth()
        self.root = root
        self._lock = threading.RLock()
        self._directories: dict[int, Path] = {}
        self._moves: dict[int, tuple[FileEvent, float]] = {}
        self._changes = FileChanges()
        self.queued = FileChanges()
        self._references = 0
        self._closed = False
        self._release_error: BaseException | None = None
        self._owner: io.BufferedRandom | None = None
        self.epoch = uuid.uuid4().hex
        self.journal = None
        self.root_generation = "unavailable"
        self._uncertain_reason: str | None = None
        self._fd = -1
        self._finalizer = None
        self.error: str | None = None
        self.needs_reconciliation = False
        self._libc = None
        if sys.platform == "linux":
            self._libc = ctypes.CDLL(None, use_errno=True)
            self._libc.inotify_init1.argtypes = [ctypes.c_int]
            self._libc.inotify_init1.restype = ctypes.c_int
            self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self._libc.inotify_add_watch.restype = ctypes.c_int
            self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            self._libc.inotify_rm_watch.restype = ctypes.c_int
            self._fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self._fd >= 0:
                self._finalizer = weakref.finalize(self, os.close, self._fd)
            else:
                self.error = os.strerror(ctypes.get_errno())
        self.watch(root)

    def bind_journal(self, db_path, vault_id):
        from .event_inbox import EventInbox

        with self._lock:
            if self.journal is not None:
                if (self.journal.db_path, self.journal.vault_id) != (db_path, vault_id) or self.journal.health is not self.health:
                    from .store import WikiError

                    raise WikiError("Filesystem observer binding changed", 503)
                return
            journal = EventInbox(db_path, self.root, vault_id, health=self.health)
            rows = journal.pending()
            previous_epoch = None
            for row, records in rows:
                if previous_epoch != row["observer_epoch"] and self._moves:
                    journal.uncertain("Unmatched move crossed observer epochs")
                previous_epoch = row["observer_epoch"]
                for record in records:
                    self.process_record(record, replay=True)
                self._changes.receipts.append(row["seq"])
            if self._uncertain_reason:
                journal.uncertain(self._uncertain_reason)
            if self._moves:
                journal.uncertain("Unmatched move from a closed observer; no cross-epoch cookie pairing is permitted")
            if rows and any(row["root_generation"] != self.root_generation for row, _records in rows) and not any(event.kind == "delete" and event.directory and not event.path for event in self._changes.lifecycle):
                journal.uncertain("Root generation changed without a received root transition")
            self.queued.merge(self._changes)
            self._changes = FileChanges()
            self.journal = journal

    def reconcile_applied(self):
        if self.journal is not None and self.journal.applied(self.queued.receipts):
            self.journal.acknowledge(self.queued.receipts)
            self.queued = FileChanges()

    def persist(self, records, *, armed=False):
        if self.journal is not None:
            seq = self.journal.receive(records, self.epoch, self.root_generation, armed=armed)
            if seq is not None:
                self._changes.receipts.append(seq)

    @property
    def mode(self) -> str:
        return "inotify" if self._fd >= 0 else "periodic"

    def watch(self, directory: Path) -> None:
        with self._lock:
            if self._fd < 0 or self._libc is None:
                return
            descriptor = self._libc.inotify_add_watch(
                self._fd, os.fsencode(directory),
                _MODIFICATIONS | _ONLY_DIRECTORY | _NO_SYMLINKS,
            )
            if descriptor < 0:
                self.needs_reconciliation = True
                self.error = os.strerror(ctypes.get_errno())
            else:
                self._directories[descriptor] = directory
                if directory == self.root:
                    stat = directory.stat()
                    self.root_generation = f"{stat.st_dev}:{stat.st_ino}"

    def drop_watches(self, descriptors) -> None:
        for descriptor in descriptors:
            if descriptor in self._directories:
                self._directories.pop(descriptor)
                if self._libc is not None and self._fd >= 0:
                    self._libc.inotify_rm_watch(self._fd, descriptor)

    def settle_inactive(self, active_paths: set[str], removed_paths=frozenset()) -> None:
        """A journal already ended/moved these bindings before notifications.

        Do not replay an old move-out deletion against a newly attached file.
        Still retain the event's order relative to the rest of the batch.
        """
        with self._lock:
            cookies = {id(event): cookie for cookie, (event, _observed) in self._moves.items()}
            active = set(active_paths)
            for event in self._changes.lifecycle:
                prefix = event.path + "/"
                bound = {path for path in active if path == event.path or (event.directory and (not event.path or path.startswith(prefix)))}
                if event.kind == "pending":
                    if not bound or bound <= removed_paths:
                        record = {"type": "settle", "cookie": cookies[id(event)], "path": event.path}
                        self.persist([record])
                        self.process_record(record)
                if event.kind == "delete":
                    active.difference_update(bound)
                elif event.kind == "move":
                    active.difference_update(bound)
                    active.update(event.destination + path[len(event.path):] for path in bound)

    def process_record(self, record, *, replay=False):
        from .store import WikiError

        if not isinstance(record, dict):
            raise WikiError("Invalid durable filesystem record", 503)
        if record.get("type") == "overflow":
            self.needs_reconciliation = True
            self._uncertain_reason = "Filesystem notifications were lost or unmounted"
            return
        if record.get("type") == "settle":
            previous = self._moves.pop(record.get("cookie"), None)
            if previous is None or previous[0].path != record.get("path"):
                raise WikiError("Filesystem move resolution does not match its received source", 503)
            previous[0].kind = "delete"
            if not replay:
                self.drop_watches(previous[0].watches)
            return
        if record.get("type") != "kernel" or not isinstance(record.get("path"), str) or not isinstance(record.get("mask"), int) or not isinstance(record.get("cookie"), int):
            raise WikiError("Invalid durable filesystem notification", 503)
        path, mask, cookie = record["path"], record["mask"], record["cookie"]
        relative = Path(path)
        changed = self._changes
        if not path:
            if mask & 0x00000C00:
                self.needs_reconciliation = True
                changed.paths.add("")
                changed.lifecycle.append(FileEvent("delete", "", directory=True))
                if not replay:
                    self.drop_watches(tuple(self._directories))
            return
        visible = not any(part.startswith(".") for part in relative.parts)
        if visible:
            changed.paths.add(path)
        if visible and mask & 0x0000000E:
            changed.written.add(path)
        if cookie and mask & 0x00000040:
            is_directory = bool(mask & 0x40000000)
            target = self.root / path
            watches = tuple(watched for watched, old in self._directories.items() if old.is_relative_to(target)) if is_directory and not replay else ()
            event = FileEvent("pending", path, directory=is_directory, watches=watches)
            self._moves[cookie] = (event, time.monotonic())
            changed.lifecycle.append(event)
        elif cookie and mask & 0x00000080:
            previous = self._moves.pop(cookie, None)
            if previous is not None:
                event = previous[0]
                event.kind = "move"
                event.destination = path
                if event.directory and not replay:
                    old_directory = self.root / event.path
                    for watched, old in list(self._directories.items()):
                        if old.is_relative_to(old_directory):
                            self._directories[watched] = self.root / path / old.relative_to(old_directory)
        elif visible and mask & 0x00000600:
            changed.lifecycle.append(FileEvent("delete", path, directory=bool(mask & 0x40000000) or bool(record.get("self"))))

    def drain(self) -> FileChanges:
        with self._lock:
            if self._fd < 0:
                return FileChanges()
            changed = self._changes
            ready = self.journal is None or bool(select.select([self._fd], [], [], 0)[0])
            records: list[dict[str, Any]] = []
            record: dict[str, Any]
            if ready and self.journal is not None:
                self.journal.arm(self.epoch)
            while ready:
                try:
                    data = os.read(self._fd, 65536)
                except BlockingIOError:
                    break
                except OSError as error:
                    self.error = str(error)
                    self.needs_reconciliation = True
                    self._uncertain_reason = "Filesystem read failed: " + str(error)
                    break
                if not data:
                    break
                offset = 0
                while offset + _HEADER.size <= len(data):
                    descriptor, mask, cookie, length = _HEADER.unpack_from(data, offset)
                    offset += _HEADER.size
                    name = os.fsdecode(data[offset:offset + length].split(b"\0", 1)[0])
                    offset += length
                    directory = self._directories.get(descriptor)
                    if mask & (_OVERFLOW | _UNMOUNT):
                        record = {"type": "overflow"}
                        records.append(record)
                        self.process_record(record)
                    if mask & _IGNORED:
                        self._directories.pop(descriptor, None)
                        if directory == self.root:
                            self.needs_reconciliation = True
                    if directory is None:
                        continue
                    target = directory / name if name else directory
                    relative = target.relative_to(self.root)
                    record = {"type": "kernel", "path": relative.as_posix() if relative.parts else "", "mask": mask, "cookie": cookie, "self": not name}
                    records.append(record)
                    self.process_record(record)
            if ready:
                self.persist(records, armed=True)
            if self._uncertain_reason and self.journal is not None:
                self.journal.uncertain(self._uncertain_reason)
            cutoff = time.monotonic() - 1
            for cookie, (event, observed) in list(self._moves.items()):
                if observed < cutoff:
                    record = {"type": "settle", "cookie": cookie, "path": event.path}
                    self.persist([record])
                    self.process_record(record)
            if self._moves:
                # Keep the position of MOVED_FROM across reads. Publishing later
                # events first could attach a replacement before this move ends.
                return FileChanges(pending_moves=True)
            self._changes = FileChanges()
        return changed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._finalizer is not None:
                self._finalizer()
            self._fd = -1
            self._directories.clear()
            self._moves.clear()
            self._changes = FileChanges()
            self.queued = FileChanges()
            if self._owner is not None:
                self._owner.close()
                self._owner = None

    def release(self) -> None:
        with _OBSERVERS_LOCK:
            if self._release_error is not None:
                raise RuntimeError("Filesystem observer cleanup remains unconfirmed") from self._release_error
            if self._references > 0:
                self._references -= 1
            if not self._references:
                try:
                    self.close()
                except BaseException as error:
                    self._release_error = error
                    raise
                for key, observer in list(_OBSERVERS.items()):
                    if observer is self:
                        del _OBSERVERS[key]

    def retire_exclusive(self) -> None:
        """Retire a replaced root only after every other observer lease left."""
        from .store import WikiError

        with _OBSERVERS_LOCK:
            if self._closed or self._references != 1:
                raise WikiError("Previous filesystem observer leases have not retired; recovery stays closed", 503)
            self.release()

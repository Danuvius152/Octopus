"""Filesystem change notifications; compensation scans stay outside queries.

Linux uses the nonblocking inotify ABI documented in inotify(7). Other
platforms, unavailable watches and overflow use the registry's periodic
reconciliation. A notification is a hint to read a file, never its content.
"""

import ctypes
import os
import struct
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

_HEADER = struct.Struct("iIII")
_MODIFICATIONS = 0x00000FCE
_ONLY_DIRECTORY = 0x01000000
_NO_SYMLINKS = 0x02000000
_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_UNMOUNT = 0x00002000


@dataclass
class FileChanges:
    paths: set[str] = field(default_factory=set)
    moves: list[tuple[str, str, bool]] = field(default_factory=list)
    written: set[str] = field(default_factory=set)


class VaultEvents:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._directories: dict[int, Path] = {}
        self._moves: dict[int, tuple[str, bool, float]] = {}
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
            self._fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self._fd >= 0:
                self._finalizer = weakref.finalize(self, os.close, self._fd)
            else:
                self.error = os.strerror(ctypes.get_errno())
        self.watch(root)

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

    def drain(self) -> FileChanges:
        changed = FileChanges()
        with self._lock:
            if self._fd < 0:
                return changed
            while True:
                try:
                    data = os.read(self._fd, 65536)
                except BlockingIOError:
                    break
                except OSError as error:
                    self.error = str(error)
                    self.needs_reconciliation = True
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
                        self.needs_reconciliation = True
                    if mask & _IGNORED:
                        self._directories.pop(descriptor, None)
                        if directory == self.root:
                            self.needs_reconciliation = True
                    if directory is None:
                        continue
                    target = directory / name if name else directory
                    relative = target.relative_to(self.root)
                    if not relative.parts:
                        if mask & 0x00000C00:
                            self.needs_reconciliation = True
                        continue
                    if any(part.startswith(".") for part in relative.parts):
                        continue
                    path = relative.as_posix()
                    changed.paths.add(path)
                    if mask & 0x0000000E:
                        changed.written.add(path)
                    if cookie and mask & 0x00000040:
                        self._moves[cookie] = (path, bool(mask & 0x40000000), time.monotonic())
                        changed.paths.discard(path)
                    elif cookie and mask & 0x00000080:
                        previous = self._moves.pop(cookie, None)
                        if previous is not None:
                            changed.moves.append((previous[0], path, previous[1]))
                            changed.paths.add(previous[0])
                            if previous[1]:
                                old_directory = self.root / previous[0]
                                for watched, old in list(self._directories.items()):
                                    if old.is_relative_to(old_directory):
                                        self._directories[watched] = self.root / path / old.relative_to(old_directory)
            cutoff = time.monotonic() - 1
            changed.paths.update(entry[0] for entry in self._moves.values() if entry[2] < cutoff)
            self._moves = {cookie: entry for cookie, entry in self._moves.items() if entry[2] >= cutoff}
        return changed

    def close(self) -> None:
        with self._lock:
            if self._finalizer is not None:
                self._finalizer()
            self._fd = -1
            self._directories.clear()
            self._moves.clear()

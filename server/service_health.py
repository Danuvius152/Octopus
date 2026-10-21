"""One process-wide boundary for untrustworthy authoritative storage."""

import os
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, NoReturn

_JOURNAL_SOCKET = "/run/systemd/journal/socket"


class ServiceUnavailable(RuntimeError):
    pass


class ServiceHealth:
    def __init__(self, *, exit_process: Callable[[int], NoReturn] = os._exit):
        self._lock = threading.Lock()
        self._cause: BaseException | None = None
        self._exit_process = exit_process
        self._state: Literal["starting", "ready", "stopping", "fatal"] = "starting"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def open_admission(self) -> None:
        with self._lock:
            if self._state != "starting":
                raise ServiceUnavailable(f"Cannot open service admission while {self._state}")
            self._state = "ready"

    def begin_shutdown(self) -> None:
        with self._lock:
            if self._state != "fatal":
                self._state = "stopping"

    @property
    def cause(self) -> BaseException | None:
        with self._lock:
            return self._cause

    def require_healthy(self) -> None:
        with self._lock:
            if self._cause is not None:
                raise ServiceUnavailable("Service storage is unavailable; this process is stopping") from self._cause

    def fail(self, cause: BaseException) -> NoReturn:
        with self._lock:
            first = self._cause is None
            if first:
                self._cause = cause
            self._state = "fatal"
        if first:
            # journald's existing datagram receiver is optional. Never send
            # exception messages (SQL values, paths and credentials may occur
            # there), enter the synchronous logging chain, or wait for space.
            try:
                caller = sys._getframe(1)
                location = f"{os.path.basename(caller.f_code.co_filename)}:{caller.f_lineno}"
                del caller
                name = type(cause).__name__.replace("\n", "_").replace("\r", "_")[:128]
                record = (f"PRIORITY=2\nSYSLOG_IDENTIFIER=octopus\nOCTOPUS_CAUSE={name}\n"
                          f"MESSAGE=Authoritative storage failed at {location} ({name}); exit=70\n")
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as diagnostic:
                    diagnostic.sendto(record.encode(), _JOURNAL_SOCKET)
            except (OSError, ValueError):
                pass
        # Never depend on a business lock, cleanup, logging flush or the event
        # loop. The supervisor observes main's exit and retires its cgroup.
        self._exit_process(70)
        raise SystemExit(70)


@contextmanager
def file_effect(health: ServiceHealth) -> Iterator[None]:
    """The caller scopes this to an actual authoritative file write or sync."""
    health.require_healthy()
    try:
        yield
    except OSError as error:
        health.fail(error)

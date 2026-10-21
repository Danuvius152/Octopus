"""Synchronous Wiki authority connections and narrowly scoped file effects."""

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from ..service_health import ServiceHealth
from ..service_health import file_effect as file_effect


def check_error(error: sqlite3.Error, db: sqlite3.Connection, health: ServiceHealth) -> None:
    """Classify before a protocol boundary converts SQLite errors to WikiError."""
    if isinstance(error, sqlite3.IntegrityError):
        return
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    if primary in {sqlite3.SQLITE_ERROR, sqlite3.SQLITE_SCHEMA, sqlite3.SQLITE_TOOBIG,
                   sqlite3.SQLITE_MISMATCH, sqlite3.SQLITE_RANGE}:
        return
    if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and not db.in_transaction:
        return
    health.fail(error)


@contextlib.contextmanager
def connect(path: Path, health: ServiceHealth, *, write: bool = True,
            create: bool = False, timeout: float = 30) -> Iterator[sqlite3.Connection]:
    """Return the original connection; success includes confirmed end and close.

    BEGIN remains with the existing SQL owner. Only an explicit creation owner
    may use mode=rwc; ordinary reopen and schema migration use mode=rw.
    """
    health.require_healthy()
    if create and not write:
        raise ValueError("A read-only Wiki connection cannot create a ledger")
    try:
        mode = "rwc" if create else "rw" if write else "ro"
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode,
                             uri=True, timeout=timeout)
    except BaseException as error:
        health.fail(error)
    try:
        try:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("PRAGMA synchronous=EXTRA")
                if db.execute("PRAGMA synchronous").fetchone()[0] != 3:
                    raise RuntimeError("Wiki SQLite connection did not confirm synchronous=EXTRA")
            db.execute("PRAGMA recursive_triggers=ON")
        except BaseException as error:
            code = getattr(error, "sqlite_errorcode", None)
            primary = code & 0xFF if isinstance(code, int) else None
            # These connection-local PRAGMAs have no business effects. A
            # refused schema read may use the same confirmed end as body errors.
            if not (isinstance(error, sqlite3.Error) and primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    and not db.in_transaction):
                health.fail(error)
            raise
        yield db
    except BaseException as error:
        if isinstance(error, sqlite3.Error):
            check_error(error, db, health)
        try:
            db.rollback()
        except BaseException as failure:
            health.fail(failure)
        raise
    else:
        try:
            db.commit()
        except BaseException as failure:
            health.fail(failure)
    finally:
        try:
            db.close()
        except BaseException as failure:
            health.fail(failure)

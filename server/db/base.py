from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from ..account_baseline import require_account_database
from ..service_health import ServiceHealth
from .projection import RecordProjection
from .schema import initialize_main_storage, require_current_main_format
from .transaction import Transaction

logger = logging.getLogger(__name__)


def _load_json_list(raw: Any) -> list[Any]:
    """A JSON-list column as a list, whatever shape the column is in.

    Columns like `agents.subagents` are additive: rows that predate them read
    back NULL, and a hand-edited row can hold anything. A decode failure
    degrades to "none defined" rather than breaking the agent.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []

class DatabaseBase:
    """Connection, transactions and format preparation — everything the domain
    mixins stand on.

    Separate from them so the lifecycle has one home: opening the file, the
    pragmas, the short transaction boundary, and the current storage format. A mixin inherits
    this rather than declaring the attributes it borrows, which keeps the type
    checker honest about what it may touch.
    """

    # Complete SELECTs and field mappings share one explicit declaration.
    # Conversion remains local to the domain; partial queries stay independent.

    def _row_to_application(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._APPLICATION_COLS.read(row)
        record.update(archived=bool(record["archived"]), pinned=bool(record["pinned"]))
        record["deletion"] = json.loads(record["deletion"]) if record["deletion"] else None
        return record

    def _row_to_bg_task(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._BG_TASK_COLS.read(row)
        record.update(stdout=record["stdout"] or "", stderr=record["stderr"] or "", truncated=bool(record["truncated"]))
        return record

    def _row_to_connector(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._CONNECTOR_COLS.read(row)
        try:
            record["scopes"] = json.loads(record["scopes"]) if record["scopes"] else []
        except (json.JSONDecodeError, TypeError):
            record["scopes"] = []
        record.update(enable_by_default=bool(record["enable_by_default"]), needs_reconnect=bool(record["needs_reconnect"]))
        return record

    def _row_to_research_job(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._RESEARCH_COLS.read(row)
        record["delivery_receipt"] = json.loads(record["delivery_receipt"]) if record["delivery_receipt"] else None
        return record

    # Column lists and canned SQL shared by the domain mixins.
    #
    # They live on the base rather than each mixin because several genuinely
    # cross domains: deleting a session has to clear its background tasks,
    # research jobs and application threads, so `sessions` needs those column
    # lists too. Putting them here states that overlap once instead of
    # duplicating three tuples, and every mixin inherits this class anyway.
    _CREDENTIAL_COLS = RecordProjection(
        ("id", "c.id"), ("backend", "c.backend"), ("label", "c.label"), ("auth_type", "c.auth_type"),
        ("secret_encrypted", "s.secret_encrypted"), ("created_at", "c.created_at"), ("status", "c.status"),
        ("token_expires_at", "c.token_expires_at"), ("needs_reconnect", "c.needs_reconnect"),
        ("last_refresh_error_code", "c.last_refresh_error_code"), ("user_id", "c.user_id"),
    )
    _CONNECTOR_COLS = RecordProjection(
        "id", "kind", "label", "auth_type", "external_account_id", "scopes", "enable_by_default",
        "needs_reconnect", "token_expires_at", "last_refresh_error_code", "created_at", "user_id",
    )
    _CUSTOM_COLS = RecordProjection(
        "kind", "display_name", "authorize_url", "token_url", "scopes", "pkce", "api_base", "created_at", "updated_at",
    )
    _AGENT_COLS = RecordProjection(
        *((name, f"a.{name}") for name in (
            "id", "name", "description", "avatar", "system_prompt", "model", "credential_id", "mcp_servers",
            "tool_allow", "tool_deny", "is_system", "archived", "created_at", "updated_at", "backend", "subagents",
            "pinned", "pin_order", "user_id", "reasoning_effort", "service_tier", "context_settings_revision",
        )),
        ("active_session_count", "(SELECT COUNT(*) FROM sessions s WHERE s.agent_id = a.id AND s.archived = 0)"),
    )
    _BG_TASK_COLS = RecordProjection(
        "id", "session_id", "command", "description", "working_dir", "status", "exit_code", "stdout", "stderr",
        "truncated", "started_at", "completed_at", "idle_timeout_seconds", "timeout_seconds", "stop_reason", "work_id", "knowledge_context",
        "delivery_disposition", "delivery_error",
    )
    _RESEARCH_COLS = RecordProjection(
        "id", "session_id", "question", "status", "phase", "error", "report_path", "cost", "created_at", "completed_at",
        "injection_status", "injected_at", "knowledge_context", "delivery_receipt",
    )
    _APPLICATION_COLS = RecordProjection(
        "id", "name", "description", "icon", "icon_src", "agent_id", "session_id", "app_dir", "entrypoint", "status", "error",
        "archived", "created_at", "updated_at", "last_built_at", "pinned", "pin_order", "user_id", "deletion",
    )

    # The position after every existing one, for a row being pinned now: it
    # lands at the bottom of the sidebar. A subquery rather than a Python
    # read-then-write so the value is computed inside the statement that
    # uses it. `{table}` is always a literal from this module.
    _NEXT_PIN_ORDER = "(SELECT COALESCE(MAX(pin_order), 0) + 1 FROM {table})"

    def __init__(self, db_path: str, *, health: ServiceHealth | None = None) -> None:
        self._db_path = db_path
        self.health = health if health is not None else ServiceHealth()
        self._conn: aiosqlite.Connection | None = None
        self._closed: bool = False
        self._close_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        # Agent defaults, live session overrides and request capture share
        # this short boundary across all managers using this database.
        self.configuration_lock = asyncio.Lock()
        self._agent_memory_lock = asyncio.Lock()
        self._write_owner: asyncio.Task[Any] | None = None
        # Work that has to unwind before the connection goes away; see
        # `add_close_hook`.
        self._close_hooks: list[Callable[[], Awaitable[object]]] = []
        self._session_delete_hooks: list[Callable[[str], None]] = []

    def drain_blockers(self) -> tuple[str, ...]:
        if self._conn is None or self._write_owner or self._write_lock.locked() or self._agent_memory_lock.locked() or self.configuration_lock.locked():
            return ("writer or exclusive operation",)
        return ()

    @asynccontextmanager
    async def transaction(self, *, write: bool = True, timeout: float = 5.0) -> AsyncIterator[Transaction]:
        """Own BEGIN, SQL, cursors and commit/rollback under one total deadline.

        Helpers receive this transaction explicitly. A public operation cannot
        recursively enter another public operation on the same connection.
        """
        self.health.require_healthy()
        if self._write_owner is asyncio.current_task():
            raise RuntimeError("Nested database operation; pass the current transaction to its SQL helper")
        deadline = asyncio.get_running_loop().time() + timeout
        async with asyncio.timeout_at(deadline):
            await self._write_lock.acquire()
            try:
                self.health.require_healthy()
                await self._ensure_connected()
                self._write_owner = asyncio.current_task()
                tx = Transaction(self.conn, self.health, deadline)
                try:
                    await tx.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                except sqlite3.OperationalError as error:
                    if (getattr(error, "sqlite_errorcode", 0) & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and not self.conn.in_transaction:
                        raise
                    self.health.fail(error)
                except BaseException as error:
                    self.health.fail(error)
                try:
                    yield tx
                except BaseException as error:
                    storage_failure = isinstance(error, (OSError, sqlite3.DatabaseError)) and not isinstance(error, sqlite3.IntegrityError)
                    if storage_failure:
                        self.health.fail(error)
                    try:
                        await tx.finish()
                        await tx._wait(self.conn.rollback())
                    except BaseException as failure:
                        self.health.fail(failure)
                    raise
                else:
                    try:
                        await tx.finish()
                        await tx._wait(self.conn.commit())
                    except BaseException as failure:
                        self.health.fail(failure)
                    if tx.cancelled is not None:
                        raise tx.cancelled
            finally:
                self._write_owner = None
                self._write_lock.release()

    async def inspect(self) -> None:
        """Check retained storage before creating a key or opening its writer."""
        self.health.require_healthy()
        await asyncio.to_thread(require_current_main_format, self._db_path)
        await asyncio.to_thread(require_account_database, self._db_path)

    async def initialize(self, *, timeout: float = 15.0) -> None:
        """Own startup SQL under one deadline, before any public operation."""
        self.health.require_healthy()
        if self._conn is not None or self._closed:
            raise RuntimeError("A Database can only be initialized once")
        deadline = asyncio.get_running_loop().time() + timeout
        async with asyncio.timeout_at(deadline):
            async with self._write_lock:
                self._write_owner = asyncio.current_task()
                try:
                    await self.inspect()
                    self._conn = await aiosqlite.connect(self._db_path)
                    mode = await self.conn.execute("PRAGMA journal_mode=WAL")
                    mode_value = await mode.fetchone()
                    if mode_value is None or mode_value[0].lower() != ("memory" if self._db_path == ":memory:" else "wal"):
                        raise RuntimeError("Runtime database requires WAL journaling")
                    await mode.close()
                    await self.conn.execute("PRAGMA foreign_keys=ON")
                    await self.conn.execute("PRAGMA busy_timeout=5000")
                    await self.conn.execute("PRAGMA synchronous=FULL")
                    durability = await self.conn.execute("PRAGMA synchronous")
                    durability_value = await durability.fetchone()
                    if durability_value is None or durability_value[0] != 2:
                        raise RuntimeError("Runtime database requires synchronous=FULL")
                    await durability.close()
                    tx = Transaction(self.conn, self.health, deadline)
                    await tx.execute("BEGIN IMMEDIATE")
                    await initialize_main_storage(tx)
                    await tx.finish()
                    await tx._wait(self.conn.commit())
                    if tx.cancelled is not None:
                        raise tx.cancelled
                except BaseException as error:
                    # No producer can use a partially prepared or still-busy
                    # connection. Startup never tries online storage repair.
                    self.health.fail(error)
                finally:
                    self._write_owner = None

    async def _set_pinned(self, tx: Transaction, table: str, row_id: str, pinned: bool) -> None:
        """Pin a row to the sidebar (at the bottom) or unpin it.

        Unpinning leaves `pin_order` alone — it only means something while
        pinned, and pinning again always appends, because an item coming back
        to the sidebar is news and belongs where the eye lands last.
        """
        if pinned:
            await tx.execute(
                f"UPDATE {table} SET pinned = 1, "
                f"pin_order = {self._NEXT_PIN_ORDER.format(table=table)} "
                "WHERE id = ? AND pinned = 0",
                (row_id,),
            )
        else:
            await tx.execute(
                f"UPDATE {table} SET pinned = 0 WHERE id = ?", (row_id,)
            )

    async def _reorder_pins(self, tx: Transaction, table: str, ordered_ids: list[str]) -> None:
        """Lay the pinned rows out in `ordered_ids` order.

        Any pinned, live row the list doesn't name — pinned from another tab
        after this one last looked — keeps its relative order and follows
        the named ones, so a reorder never drops someone else's pin.
        """
        cursor = await tx.execute(
            f"SELECT id FROM {table} WHERE pinned = 1 AND archived = 0 "
            "ORDER BY pin_order, created_at, id"
        )
        named = set(ordered_ids)
        rest = [r[0] for r in await cursor.fetchall() if r[0] not in named]
        for position, row_id in enumerate([*ordered_ids, *rest], start=1):
            await tx.execute(
                f"UPDATE {table} SET pin_order = ? WHERE id = ?",
                (position, row_id),
            )

    async def _ensure_connected(self) -> None:
        # A retired connection never reopens. A late consumer exits through
        # cancellation instead of creating an unowned SQLite worker thread.
        if self._closed:
            raise asyncio.CancelledError("Database is closed")
        assert self._conn is not None, "Database not initialized"

    def add_close_hook(self, hook: Callable[[], Awaitable[object]]) -> None:
        """Register work to unwind *before* this connection goes away.

        The database is the resource; whoever holds long-running work against
        it has to be given the chance to finish that work while the connection
        still answers. A turn in flight is the case this exists for: cancelled
        after the connection is gone, its own `finally` — which records and
        broadcasts — has nothing to await and nothing to cancel it, so the event
        loop stops making progress instead of failing. Cancelled *before*, it
        unwinds normally.

        Hooks run in reverse registration order, like a stack of context
        managers. A failed hook keeps the resource open and remains registered
        so a later close can confirm teardown before retiring the connection.
        """
        self._close_hooks.append(hook)

    async def close(self, *, timeout: float = 5.0) -> None:
        """Drain registered owners, then retire the connection once."""
        if self._write_owner is asyncio.current_task():
            raise RuntimeError("Cannot close a database from its active transaction")
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close(timeout), name="database-close")
            self._close_task.add_done_callback(self._observe_close)
        await asyncio.shield(self._close_task)

    @staticmethod
    def _observe_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Database owners did not drain; connection retained for retry",
                         exc_info=(type(error), error, error.__traceback__))

    async def _close(self, timeout: float) -> None:
        """A hook failure retains the resource; an uncertain driver exit is fatal."""
        failures: list[Exception] = []
        for hook in reversed(tuple(self._close_hooks)):
            try:
                await hook()
            except Exception as error:
                logger.exception("Database teardown remains unconfirmed")
                failures.append(error)
            else:
                self._close_hooks.remove(hook)
        if failures:
            raise RuntimeError("Database teardown remains unconfirmed; connection kept open") from failures[0]
        # All public writes are already committed. The same lock joins an
        # in-flight transaction and prevents new work during driver close.
        deadline = asyncio.get_running_loop().time() + timeout
        async with asyncio.timeout_at(deadline):
            async with self._write_lock:
                if self._conn is None:
                    return
                self.health.require_healthy()
                self._closed = True
                try:
                    await self._conn.close()
                except BaseException as error:
                    self.health.fail(error)
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    async def _count(self, tx: Transaction, sql: str, params: tuple[Any, ...] = ()) -> int:
        """One `SELECT COUNT(*)`, as an int.

        An aggregate returns exactly one row, which is what the callers that
        used to `execute` / `fetchone` / `row[0]` were each relying on without
        saying so. Said here once, and phrased as the one thing they all want.
        """
        cursor = await tx.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

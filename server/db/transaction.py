"""SQL for one short runtime-database operation; the Database owns its lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import Any

import aiosqlite

from ..service_health import ServiceHealth


class TransactionCursor:
    """A cursor's driver work has the same owner and deadline as its SQL."""

    def __init__(self, owner: Transaction, cursor: aiosqlite.Cursor):
        self._owner = owner
        self._cursor = cursor
        self._closed = False

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    async def fetchone(self):
        return await self._owner._wait(self._cursor.fetchone())

    async def fetchall(self):
        return await self._owner._wait(self._cursor.fetchall())

    async def fetchmany(self, size: int):
        return await self._owner._wait(self._cursor.fetchmany(size))

    async def close(self) -> None:
        if not self._closed:
            await self._owner._wait(self._cursor.close())
            self._closed = True


class Transaction:
    def __init__(self, connection: aiosqlite.Connection, health: ServiceHealth, deadline: float):
        self._connection = connection
        self._health = health
        self._deadline = deadline
        self._cursors: list[TransactionCursor] = []
        self.cancelled: asyncio.CancelledError | None = None

    async def _wait[T](self, operation: Awaitable[T]) -> T:
        # Cancelling a waiter must not discard the driver's acknowledgment.
        # The outer transaction's original timeout still cancels at its total
        # deadline. A stuck worker then enters fatal instead of ordinary close.
        if asyncio.get_running_loop().time() >= self._deadline:
            self._health.fail(TimeoutError("Database operation exceeded its total deadline"))
        driver = asyncio.ensure_future(operation)
        while True:
            try:
                return await asyncio.shield(driver)
            except asyncio.CancelledError as error:
                if driver.cancelled() or asyncio.get_running_loop().time() >= self._deadline:
                    self._health.fail(error)
                if self.cancelled is None:
                    self.cancelled = error

    async def execute(self, sql: str, parameters: Iterable[Any] = ()) -> TransactionCursor:
        raw = await self._wait(self._connection.execute(sql, parameters))
        cursor = TransactionCursor(self, raw)
        self._cursors.append(cursor)
        return cursor

    async def fetch_one(self, sql: str, parameters: Iterable[Any] = ()):
        cursor = await self.execute(sql, parameters)
        return await cursor.fetchone()

    async def execute_many(self, sql: str, parameters: Iterable[Iterable[Any]]) -> None:
        raw = await self._wait(self._connection.executemany(sql, parameters))
        self._cursors.append(TransactionCursor(self, raw))

    async def fetch_all(self, sql: str, parameters: Iterable[Any] = ()):
        cursor = await self.execute(sql, parameters)
        return await cursor.fetchall()

    async def finish(self) -> None:
        # In particular, RETURNING must be exhausted and closed before COMMIT.
        for cursor in self._cursors:
            if not cursor._closed:
                await cursor.fetchall()
                await cursor.close()
        self._cursors.clear()

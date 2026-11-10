"""Applications — the agent-built web apps.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
from typing import Any

from .base import DatabaseBase
from .transaction import Transaction


class ApplicationDeleting(ValueError):
    """Admission refused while the application's deletion remains unfinished."""


async def require_application_writable(tx: Transaction, session_id: str) -> None:
    blocked = await tx.fetch_one(
        "SELECT 1 FROM applications a JOIN sessions s ON "
        "(a.id=s.app_id OR a.session_id=s.id) AND a.user_id IS s.user_id "
        "WHERE s.id=? AND a.deletion IS NOT NULL LIMIT 1", (session_id,),
    )
    if blocked:
        raise ApplicationDeleting("Application deletion is unfinished; retry deletion before writing")


async def retain_application_session_owner(tx: Transaction, session_id: str) -> str | None:
    """Resolve and retain the known relationship within the caller's commit."""
    rows = await tx.fetch_all(
        "SELECT s.app_id, a.id FROM sessions s LEFT JOIN applications a ON "
        "a.session_id=s.id AND a.user_id IS s.user_id WHERE s.id=?", (session_id,),
    )
    owners = {value for row in rows for value in row if value is not None}
    if len(owners) > 1:
        raise ValueError("Application builder has multiple owners")
    if not owners:
        return None
    app_id = owners.pop()
    await tx.execute("UPDATE sessions SET app_id=? WHERE id=? AND app_id IS NULL", (app_id, session_id))
    return app_id


class ApplicationsMixin(DatabaseBase):

    async def require_application_session_writable(self, session_id: str) -> None:
        async with self.transaction(write=False) as tx:
            await require_application_writable(tx, session_id)

    async def set_application_deletion(self, app_id: str, state: dict[str, Any]) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute("UPDATE applications SET deletion=? WHERE id=?", (json.dumps(state), app_id))

    async def application_session_ids(self, app_id: str) -> list[str]:
        """Exact current and retained build/conversation relationships, no cwd inference."""
        async with self.transaction(write=False) as tx:
            rows = await tx.fetch_all(
                "SELECT s.id FROM sessions s JOIN applications a ON "
                "(s.app_id=a.id OR s.id=a.session_id) AND s.user_id IS a.user_id WHERE a.id=?",
                (app_id,),
            )
            return [row[0] for row in rows]

    async def retain_application_session_owner(self, session_id: str) -> str | None:
        """Keep a legacy builder's known relationship before replacing it."""
        async with self.transaction(write=True) as tx:
            return await retain_application_session_owner(tx, session_id)


    async def save_application(
        self,
        *,
        app_id: str,
        name: str,
        app_dir: str,
        created_at: str,
        user_id: str | None = None,
        updated_at: str,
        description: str = "",
        icon: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        entrypoint: str = "index.html",
        status: str = "building",
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO applications "
                "(id, name, description, icon, agent_id, session_id, app_dir, "
                " entrypoint, status, created_at, updated_at, user_id, "
                " pinned, pin_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, "
                f" {self._NEXT_PIN_ORDER.format(table='applications')})",
                (
                    app_id,
                    name,
                    description,
                    icon,
                    agent_id,
                    session_id,
                    app_dir,
                    entrypoint,
                    status,
                    created_at,
                    updated_at,
                    user_id,
                ),
            )

    async def load_applications(
        self,
        *,
        include_archived: bool = False,
        only_archived: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            clauses: list[str] = []
            params: list[Any] = []
            if only_archived:
                clauses.append("archived = 1")
            elif not include_archived:
                clauses.append("archived = 0")
            if user_id is not None:
                clauses.append("user_id = ?")
                params.append(user_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            cursor = await tx.execute(
                f"SELECT {self._APPLICATION_COLS} FROM applications{where} "
                "ORDER BY created_at ASC",
                params,
            )
            rows = await cursor.fetchall()
            return [self._row_to_application(r) for r in rows]

    async def get_application(
        self, app_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One application, scoped when a scope is given — `None` for "belongs
        to someone else" as well as "does not exist"."""
        async with self.transaction(write=False) as tx:
            sql = f"SELECT {self._APPLICATION_COLS} FROM applications WHERE id = ?"
            params: list[Any] = [app_id]
            if user_id is not None:
                sql += " AND user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql, params)
            row = await cursor.fetchone()
            return self._row_to_application(row) if row else None

    async def get_application_by_name(
        self,
        name: str,
        *,
        include_archived: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Live applications only by default — an archived app doesn't hold its
        name (the unique index is live-only), so creating a replacement with
        the same name is allowed."""
        async with self.transaction(write=False) as tx:
            query = (
                f"SELECT {self._APPLICATION_COLS} FROM applications "
                "WHERE name = ? COLLATE NOCASE"
            )
            params: list[Any] = [name]
            if user_id is not None:
                # A name is taken *for this account*, not for the box.
                query += " AND user_id = ?"
                params.append(user_id)
            if not include_archived:
                query += " AND archived = 0"
            cursor = await tx.execute(query, params)
            row = await cursor.fetchone()
            return self._row_to_application(row) if row else None

    async def update_application(self, app_id: str, **fields: Any) -> None:
        """Patch any of: name, description, icon, icon_src, agent_id,
        session_id, entrypoint, status, error, updated_at, last_built_at.

        Anything not on the list is ignored rather than written — which is why
        `icon_src` has to be added here explicitly even though only `_evaluate`
        ever sets it. (The API models keep it out of Create/Update, so this
        list is not the thing protecting it from clients.)
        """
        async with self.transaction(write=True) as tx:
            allowed = {
                "name", "description", "icon", "icon_src", "agent_id",
                "session_id", "entrypoint", "status", "error", "archived",
                "updated_at", "last_built_at",
            }
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            await tx.execute(
                f"UPDATE applications SET {set_clause} WHERE id = ?",
                list(updates.values()) + [app_id],
            )

    async def set_application_pinned(self, app_id: str, pinned: bool) -> None:
        """Put the application in the sidebar (at the bottom) or take it out."""
        async with self.transaction(write=True) as tx:
            await self._set_pinned(tx, "applications", app_id, pinned)

    async def reorder_application_pins(
        self, ordered_ids: list[str], *, user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate, reorder and capture changed rows in the same commit."""
        async with self.transaction(write=True) as tx:
            scope = "archived=0" + (" AND user_id=?" if user_id is not None else "")
            params = (user_id,) if user_id is not None else ()
            rows = await tx.fetch_all(f"SELECT {self._APPLICATION_COLS} FROM applications WHERE {scope} ORDER BY created_at ASC", params)
            before = {row["id"]: row for row in map(self._row_to_application, rows)}
            stray = [identifier for identifier in ordered_ids if identifier not in before or not before[identifier]["pinned"]]
            if stray:
                raise ValueError(f"Not a pinned application: {', '.join(stray)}")
            await self._reorder_pins(tx, "applications", ordered_ids)
            rows = await tx.fetch_all(f"SELECT {self._APPLICATION_COLS} FROM applications WHERE {scope} ORDER BY created_at ASC", params)
            after = [self._row_to_application(row) for row in rows]
            changed = [row for row in after if row["pin_order"] != before[row["id"]]["pin_order"]]
            return after, changed

    async def delete_application(self, app_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM applications WHERE id = ?", (app_id,)
            )
            return cursor.rowcount > 0

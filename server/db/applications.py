"""Applications — the agent-built web apps.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class ApplicationsMixin(DatabaseBase):


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

    async def reorder_application_pins(self, ordered_ids: list[str]) -> None:
        """The sidebar order of the pinned applications — see `_reorder_pins`."""
        async with self.transaction(write=True) as tx:
            await self._reorder_pins(tx, "applications", ordered_ids)

    async def delete_application(self, app_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM applications WHERE id = ?", (app_id,)
            )
            return cursor.rowcount > 0

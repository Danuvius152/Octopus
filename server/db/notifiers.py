"""Outbound notifiers.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .base import DatabaseBase
from .projection import RecordProjection

_NOTIFIER_COLS = RecordProjection("id", "type", "label", "config", "enabled", "created_at", "user_id")


def _notifier_row(row: Sequence[Any]) -> dict[str, Any]:
    record = _NOTIFIER_COLS.read(row)
    record["config"] = json.loads(record["config"]) if record["config"] else {}
    record["enabled"] = bool(record["enabled"])
    return record


class NotifiersMixin(DatabaseBase):
    # --- Notifiers ---

    async def save_notifier(
        self,
        notifier_id: str,
        type: str,
        label: str,
        config: dict[str, Any],
        created_at: str,
        enabled: bool = True,
        user_id: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO notifiers "
                "(id, type, label, config, enabled, created_at, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    notifier_id,
                    type,
                    label,
                    json.dumps(config),
                    int(enabled),
                    created_at,
                    user_id,
                ),
            )

    async def load_notifiers(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every notifier, or one account's. The owner is on the row because
        the manager needs it: an event must only reach its own account's
        targets (multi-tenancy.md §7)."""
        async with self.transaction(write=False) as tx:
            sql = f"SELECT {_NOTIFIER_COLS} FROM notifiers"
            params: list[Any] = []
            if user_id is not None:
                sql += " WHERE user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql + " ORDER BY created_at", params)
            rows = await cursor.fetchall()
            return [_notifier_row(row) for row in rows]

    async def delete_notifier(self, notifier_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM notifiers WHERE id = ?", (notifier_id,)
            )
            return cursor.rowcount > 0

    async def update_notifier(
        self, notifier_id: str, *, user_id: str | None = None, **fields: Any,
    ) -> dict[str, Any] | None:
        """Capture this account's receipt under the original short transaction.

        Returning through the context confirms COMMIT before the caller can
        observe the row. Empty updates only read existence in the same scope.
        """
        allowed = {"label", "enabled", "config"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
                updates[k] = int(bool(v))
            elif k == "config":
                updates[k] = json.dumps(v)
            else:
                updates[k] = v
        where = "id = ?"
        scope = [notifier_id]
        if user_id is not None:
            where += " AND user_id = ?"
            scope.append(user_id)
        async with self.transaction(write=bool(updates)) as tx:
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                cursor = await tx.execute(
                    f"UPDATE notifiers SET {set_clause} WHERE {where} RETURNING {_NOTIFIER_COLS}",
                    [*updates.values(), *scope],
                )
            else:
                cursor = await tx.execute(f"SELECT {_NOTIFIER_COLS} FROM notifiers WHERE {where}", scope)
            row = await cursor.fetchone()
            return _notifier_row(row) if row is not None else None

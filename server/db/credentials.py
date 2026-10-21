"""Backend credentials and their split-out secrets.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .base import DatabaseBase
from .transaction import Transaction


class CredentialsMixin(DatabaseBase):

    def _row_to_credential(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "backend": row[1],
            "label": row[2],
            "auth_type": row[3],
            "secret_encrypted": row[4],
            "created_at": row[5],
            "status": row[6] or "active",
            "token_expires_at": row[7],
            "needs_reconnect": bool(row[8]),
            "last_refresh_error_code": row[9],
            "user_id": row[10],
        }

    async def save_credential(
        self,
        credential_id: str,
        backend: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
        user_id: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO backend_credentials "
                "(id, backend, label, auth_type, secret_encrypted, created_at, "
                " status, needs_reconnect, user_id) "
                "VALUES (?, ?, ?, ?, '', ?, 'active', 0, ?)",
                (
                    credential_id,
                    backend,
                    label,
                    auth_type,
                    created_at,
                    user_id,
                ),
            )
            await tx.execute(
                "INSERT OR REPLACE INTO credential_secrets "
                "(credential_id, secret_encrypted) VALUES (?, ?)",
                (credential_id, secret_encrypted),
            )

    async def load_credentials(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every sign-in, or only one account's (multi-tenancy.md §5).

        `user_id=None` is "no scoping asked for" — the scheduler resolving a
        credential for somebody's fire, and the pre-accounts install.
        """
        async with self.transaction(write=False) as tx:
            cols = ", ".join(self._CREDENTIAL_COLS)
            sql = (
                f"SELECT {cols} FROM backend_credentials c "
                "LEFT JOIN credential_secrets s ON s.credential_id = c.id"
            )
            params: list[Any] = []
            if user_id is not None:
                sql += " WHERE c.user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql + " ORDER BY c.created_at", params)
            rows = await cursor.fetchall()
            return [self._row_to_credential(row) for row in rows]

    async def get_credential(
        self, credential_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One sign-in — `None` for "belongs to someone else" as much as for
        "does not exist", which a caller must not be able to tell apart."""
        async with self.transaction(write=False) as tx:
            return await self._get_credential(tx, credential_id, user_id)

    async def _get_credential(
        self, tx: Transaction, credential_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        cols = ", ".join(self._CREDENTIAL_COLS)
        sql = (
            f"SELECT {cols} FROM backend_credentials c "
            "LEFT JOIN credential_secrets s ON s.credential_id = c.id "
            "WHERE c.id = ?"
        )
        params: list[Any] = [credential_id]
        if user_id is not None:
            sql += " AND c.user_id = ?"
            params.append(user_id)
        cursor = await tx.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    async def reauthorize_credential(
        self, credential_id: str, *, user_id: str | None, backend: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Check the target and replace its secret under the same write owner.

        OAuth exchange happens outside storage. Deletion or account adoption
        during that await must not turn a validated target into an unscoped
        write. None here names the install owner, not an unfiltered query.
        """
        async with self.transaction(write=True) as tx:
            if user_id is None and await self._count(tx, "SELECT COUNT(*) FROM users"):
                return None
            existing = await self._get_credential(tx, credential_id, user_id)
            if (existing is None or existing["user_id"] != user_id
                    or existing["backend"] != backend):
                return None
            await self._update_credential(tx, credential_id, **fields)
            return await self._get_credential(tx, credential_id, user_id)

    async def update_credential(self, credential_id: str, **fields: Any) -> None:
        async with self.transaction(write=True) as tx:
            return await self._update_credential(tx, credential_id, **fields)

    async def _update_credential(self, tx: Transaction, credential_id: str, **fields: Any) -> None:
        meta_allowed = {
            "label",
            "status",
            "token_expires_at",
            "needs_reconnect",
            "last_refresh_error_code",
        }
        # Nullable columns need to be writable to NULL (e.g. clearing a
        # stale `last_refresh_error_code` after a successful refresh).
        # Callers that want to leave a column alone should just not pass it.
        nullable_meta = {"token_expires_at", "last_refresh_error_code"}
        meta_updates = {
            k: v
            for k, v in fields.items()
            if k in meta_allowed and (v is not None or k in nullable_meta)
        }
        if "needs_reconnect" in meta_updates and meta_updates["needs_reconnect"] is not None:
            meta_updates["needs_reconnect"] = int(bool(meta_updates["needs_reconnect"]))

        secret_value = fields.get("secret_encrypted")

        if meta_updates:
            set_clause = ", ".join(f"{k} = ?" for k in meta_updates)
            values = list(meta_updates.values()) + [credential_id]
            await tx.execute(
                f"UPDATE backend_credentials SET {set_clause} WHERE id = ?",
                values,
            )

        if secret_value is not None:
            await tx.execute(
                "INSERT OR REPLACE INTO credential_secrets "
                "(credential_id, secret_encrypted) VALUES (?, ?)",
                (credential_id, secret_value),
            )


    async def delete_credential(self, credential_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            # ON DELETE CASCADE on credential_secrets handles the secret row.
            cursor = await tx.execute(
                "DELETE FROM backend_credentials WHERE id = ?", (credential_id,)
            )
            return cursor.rowcount > 0

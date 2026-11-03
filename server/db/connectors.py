"""Connector installations, their secrets, OAuth clients and custom kinds.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..connectors.grants import GrantDisposition, grant_stamp
from .base import DatabaseBase
from .users import owner_is_available


class ConnectorsMixin(DatabaseBase):

    async def authorize_connector_installation(
        self, *, installation_id: str, kind: str, label: str,
        secret_encrypted: str, created_at: str, external_account_id: str | None,
        scopes: list[str], token_expires_at: str | None, user_id: str | None,
        require_custom_definition: bool,
        expected_existing_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit one OAuth authorization, preserving the existing identity.

        A captured reauthorization target must still exist. First authorizations
        resolve a conflicting identity inside this same metadata/secret commit.
        Empty provider identities remain separate installations.
        """
        async with self.transaction(write=True) as tx:
            if not await owner_is_available(tx, user_id):
                raise ValueError("Account is unavailable")
            if require_custom_definition and await tx.fetch_one(
                "SELECT 1 FROM custom_connectors WHERE kind=?", (kind,),
            ) is None:
                raise ValueError("Custom connector definition no longer exists")
            if expected_existing_id is not None:
                existing = await tx.fetch_one(
                    "SELECT id FROM connector_installations WHERE id=? AND user_id IS ? "
                    "AND kind=? AND external_account_id=?",
                    (expected_existing_id, user_id, kind, external_account_id),
                )
                if existing is None:
                    raise ValueError("connector installation not found")
            row = await tx.fetch_one(
                "INSERT INTO connector_installations "
                "(id, kind, label, auth_type, external_account_id, scopes, "
                "enable_by_default, needs_reconnect, token_expires_at, created_at, user_id) "
                "VALUES (?, ?, ?, 'oauth', ?, ?, 0, 0, ?, ?, ?) "
                "ON CONFLICT(COALESCE(user_id, ''), kind, external_account_id) "
                "WHERE external_account_id IS NOT NULL DO UPDATE SET "
                "label=excluded.label, scopes=excluded.scopes, "
                "token_expires_at=excluded.token_expires_at, "
                "needs_reconnect=0, last_refresh_error_code=NULL "
                f"RETURNING {self._CONNECTOR_COLS}",
                (installation_id, kind, label, external_account_id, json.dumps(scopes),
                 token_expires_at, created_at, user_id),
            )
            assert row is not None
            installation = self._row_to_connector(row)
            await tx.execute(
                "INSERT INTO connector_installation_secrets(installation_id, secret_encrypted) "
                "VALUES (?, ?) ON CONFLICT(installation_id) DO UPDATE SET "
                "secret_encrypted=excluded.secret_encrypted",
                (installation["id"], secret_encrypted),
            )
            return installation


    async def save_connector_installation(
        self,
        *,
        installation_id: str,
        kind: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
        external_account_id: str | None = None,
        scopes: list[str] | None = None,
        enable_by_default: bool = False,
        token_expires_at: str | None = None,
        user_id: str | None = None,
        require_available_owner: bool = False,
    ) -> dict[str, Any]:
        async with self.transaction(write=True) as tx:
            if require_available_owner and not await owner_is_available(tx, user_id):
                raise ValueError("Account is unavailable")
            await tx.execute(
                "INSERT INTO connector_installations "
                "(id, kind, label, auth_type, external_account_id, scopes, "
                " enable_by_default, needs_reconnect, token_expires_at, created_at, "
                " user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    installation_id, kind, label, auth_type, external_account_id,
                    json.dumps(scopes) if scopes is not None else None,
                    int(bool(enable_by_default)), token_expires_at, created_at,
                    user_id,
                ),
            )
            await tx.execute(
                "INSERT OR REPLACE INTO connector_installation_secrets "
                "(installation_id, secret_encrypted) VALUES (?, ?)",
                (installation_id, secret_encrypted),
            )
            row = await tx.fetch_one(f"SELECT {self._CONNECTOR_COLS} FROM connector_installations WHERE id=?", (installation_id,))
            assert row is not None
            return self._row_to_connector(row)

    async def load_connector_installations(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every installation, or one account's (multi-tenancy.md §5).

        `user_id=None` is "no scoping asked for" — the token-refresh sweep at
        boot, which walks everybody's.
        """
        async with self.transaction(write=False) as tx:
            sql = f"SELECT {self._CONNECTOR_COLS} FROM connector_installations"
            params: list[Any] = []
            if user_id is not None:
                sql += " WHERE user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql + " ORDER BY created_at", params)
            rows = await cursor.fetchall()
            return [self._row_to_connector(row) for row in rows]

    async def get_connector_installation(
        self, installation_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One installation — `None` for "belongs to someone else" as much as
        for "does not exist"."""
        async with self.transaction(write=False) as tx:
            sql = (
                f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
                "WHERE id = ?"
            )
            params: list[Any] = [installation_id]
            if user_id is not None:
                sql += " AND user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql, params)
            row = await cursor.fetchone()
            return self._row_to_connector(row) if row else None

    async def get_connector_installation_by_account(
        self, kind: str, external_account_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Look up by (kind, external account) — the dedup key the install
        flow upserts on. Per account, matching the unique index: two people
        connecting the same GitHub account each get their own installation,
        because the tokens are theirs separately (multi-tenancy.md §5)."""
        async with self.transaction(write=False) as tx:
            sql = (
                f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
                "WHERE kind = ? AND external_account_id = ? AND user_id IS ?"
            )
            params: list[Any] = [kind, external_account_id, user_id]
            cursor = await tx.execute(sql, params)
            row = await cursor.fetchone()
            return self._row_to_connector(row) if row else None

    async def get_connector_secret(self, installation_id: str) -> str | None:
        """The encrypted token blob — only the internal /token route reads
        this."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT secret_encrypted FROM connector_installation_secrets "
                "WHERE installation_id = ?",
                (installation_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def reject_connector_grant(
        self, installation_id: str, user_id: str | None, stamp: str, code: str,
    ) -> tuple[GrantDisposition, bool]:
        async with self.transaction(write=True) as tx:
            row = await tx.fetch_one(
                "SELECT i.id,i.user_id,i.kind,i.auth_type,i.needs_reconnect,s.secret_encrypted,c.api_base "
                "FROM connector_installations i "
                "JOIN connector_installation_secrets s ON s.installation_id=i.id "
                "LEFT JOIN custom_connectors c ON c.kind=i.kind WHERE i.id=?", (installation_id,))
            if row is None or row[1] != user_id or not await owner_is_available(tx, user_id):
                return "gone", False
            identity = dict(zip(("id", "user_id", "kind", "auth_type"), row[:4], strict=True))
            if grant_stamp(identity, row[5], row[6]) != stamp:
                return "obsolete", False
            changed = not row[4]
            if changed:
                await tx.execute("UPDATE connector_installations SET needs_reconnect=1,last_refresh_error_code=? WHERE id=?",
                                 (code, installation_id))
            return "applied", changed

    async def update_connector_installation(
        self, installation_id: str, *, require_available_owner: bool = False, **fields: Any,
    ) -> dict[str, Any] | None:
        async with self.transaction(write=True) as tx:
            # Batch kind deletion does not take individual manager locks.
            # Check existence in the same transaction as metadata and secret
            # writes, so a late update cannot recreate an orphan secret.
            existing = await tx.fetch_one("SELECT user_id FROM connector_installations WHERE id = ?", (installation_id,))
            if existing is None:
                return None
            if require_available_owner and not await owner_is_available(tx, existing[0]):
                raise ValueError("Account is unavailable")
            meta_allowed = {
                "label",
                "external_account_id",
                "scopes",
                "enable_by_default",
                "needs_reconnect",
                "token_expires_at",
                "last_refresh_error_code",
            }
            # Nullable columns must be writable to NULL (e.g. clearing a stale
            # last_refresh_error_code after a good refresh). Columns omitted by
            # the caller are left untouched.
            nullable_meta = {
                "external_account_id",
                "scopes",
                "token_expires_at",
                "last_refresh_error_code",
            }
            meta_updates = {
                k: v
                for k, v in fields.items()
                if k in meta_allowed and (v is not None or k in nullable_meta)
            }
            if "scopes" in meta_updates and meta_updates["scopes"] is not None:
                meta_updates["scopes"] = json.dumps(meta_updates["scopes"])
            for boolish in ("enable_by_default", "needs_reconnect"):
                if boolish in meta_updates and meta_updates[boolish] is not None:
                    meta_updates[boolish] = int(bool(meta_updates[boolish]))

            secret_value = fields.get("secret_encrypted")

            if meta_updates:
                set_clause = ", ".join(f"{k} = ?" for k in meta_updates)
                values = list(meta_updates.values()) + [installation_id]
                await tx.execute(
                    f"UPDATE connector_installations SET {set_clause} WHERE id = ?",
                    values,
                )

            if secret_value is not None:
                await tx.execute(
                    "INSERT OR REPLACE INTO connector_installation_secrets "
                    "(installation_id, secret_encrypted) VALUES (?, ?)",
                    (installation_id, secret_value),
                )
            row = await tx.fetch_one(f"SELECT {self._CONNECTOR_COLS} FROM connector_installations WHERE id=?", (installation_id,))
            assert row is not None
            return self._row_to_connector(row)


    async def delete_connector_installation(self, installation_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            # ON DELETE CASCADE drops the secret row and any agent_connectors links.
            cursor = await tx.execute(
                "DELETE FROM connector_installations WHERE id = ?", (installation_id,)
            )
            return cursor.rowcount > 0
    # --- per-kind OAuth client credentials (in-app config) ----------------

    async def set_connector_oauth_client(
        self, kind: str, client_id: str, client_secret_encrypted: str, now: str
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO connector_oauth_clients "
                "(kind, client_id, client_secret_encrypted, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET "
                "client_id=excluded.client_id, "
                "client_secret_encrypted=excluded.client_secret_encrypted, "
                "updated_at=excluded.updated_at",
                (kind, client_id, client_secret_encrypted, now, now),
            )

    async def get_connector_oauth_client(
        self, kind: str
    ) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT kind, client_id, client_secret_encrypted "
                "FROM connector_oauth_clients WHERE kind = ?",
                (kind,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "kind": row[0],
                "client_id": row[1],
                "client_secret_encrypted": row[2],
            }

    async def delete_connector_oauth_client(self, kind: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM connector_oauth_clients WHERE kind = ?", (kind,)
            )
            return cursor.rowcount > 0

    async def delete_connector_installations_by_kind(self, kind: str) -> int:
        """Delete every installation of a kind (cascades to secrets +
        agent_connectors). Used when a custom connector is removed."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM connector_installations WHERE kind = ?", (kind,)
            )
            return cursor.rowcount
    # --- custom (user-defined) connector definitions ----------------------

    def _row_to_custom(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._CUSTOM_COLS.read(row)
        try:
            record["scopes"] = json.loads(record["scopes"]) if record["scopes"] else []
        except (json.JSONDecodeError, TypeError):
            record["scopes"] = []
        record["pkce"] = bool(record["pkce"])
        return record

    async def save_custom_connector(
        self,
        *,
        kind: str,
        display_name: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
        pkce: bool,
        api_base: str,
        now: str,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO custom_connectors "
                "(kind, display_name, authorize_url, token_url, scopes, pkce, "
                " api_base, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET "
                "display_name=excluded.display_name, "
                "authorize_url=excluded.authorize_url, token_url=excluded.token_url, "
                "scopes=excluded.scopes, pkce=excluded.pkce, "
                "api_base=excluded.api_base, updated_at=excluded.updated_at",
                (
                    kind, display_name, authorize_url, token_url,
                    json.dumps(scopes), int(bool(pkce)), api_base, now, now,
                ),
            )

    async def get_custom_connector(self, kind: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._CUSTOM_COLS} FROM custom_connectors WHERE kind = ?",
                (kind,),
            )
            row = await cursor.fetchone()
            return self._row_to_custom(row) if row else None

    async def list_custom_connectors(self) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._CUSTOM_COLS} FROM custom_connectors ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [self._row_to_custom(row) for row in rows]

    async def delete_custom_connector(self, kind: str) -> bool:
        """Retire a definition and all local authorization relationships once."""
        async with self.transaction(write=True) as tx:
            if await tx.fetch_one("SELECT 1 FROM custom_connectors WHERE kind=?", (kind,)) is None:
                return False
            # Secret and agent links cascade from installation deletion.
            await tx.execute("DELETE FROM connector_installations WHERE kind = ?", (kind,))
            await tx.execute("DELETE FROM connector_oauth_clients WHERE kind = ?", (kind,))
            cursor = await tx.execute(
                "DELETE FROM custom_connectors WHERE kind = ?", (kind,)
            )
            return cursor.rowcount > 0

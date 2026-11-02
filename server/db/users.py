"""Accounts, invites and bearer tokens.

One slice of the persistence layer (§3 A4's one-file-per-subject split). The
rows here are the control plane: who exists, who may create an account, and
which bearers are currently good for which account.

Nothing in this file decides policy — no password is hashed here, no token is
minted here, no expiry is judged here. That lives in `server/users.py`, so the
rules exist once and this file stays a set of statements about tables.
"""

from __future__ import annotations

from typing import Any

from .agents import create_default_agent
from .base import DatabaseBase
from .transaction import Transaction


def _row_to_user(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "dek_wrapped": row[3],
        "is_admin": bool(row[4]),
        "extra_roots": row[5],
        "created_at": row[6],
        "disabled_at": row[7],
    }


_USER_COLUMNS = (
    "id, username, password_hash, dek_wrapped, is_admin, extra_roots, "
    "created_at, disabled_at"
)


async def owner_is_available(tx: Transaction, user_id: str | None) -> bool:
    """Business admission requires a current account in the same transaction."""
    if user_id is None:
        return False
    row = await tx.fetch_one("SELECT disabled_at FROM users WHERE id=?", (user_id,))
    return row is not None and not row[0]


class UsersMixin(DatabaseBase):
    # -- users --------------------------------------------------------------

    async def _has_setup_data(self, tx: Transaction) -> bool:
        """Fresh schema records are allowed; retained business rows are not."""
        tables = await tx.fetch_all(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT GLOB 'sqlite_*'"
        )
        for (name,) in tables:
            quoted = name.replace('"', '""')
            if await tx.fetch_one(f'SELECT 1 FROM "{quoted}" LIMIT 1') is not None:
                return True
        return False

    async def create_user(
        self,
        *,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: str,
        is_admin: bool = False,
        dek_wrapped: str | None = None,
    ) -> dict[str, Any]:
        async with self.transaction(write=True) as tx:
            return await self._create_account(tx, user_id=user_id, username=username, password_hash=password_hash,
                                           created_at=created_at, is_admin=is_admin, dek_wrapped=dek_wrapped)

    async def _create_account(self, tx: Transaction, *, user_id: str, username: str, password_hash: str,
                              created_at: str, is_admin: bool = False, dek_wrapped: str | None = None) -> dict[str, Any]:
        user = await self._create_user(tx, user_id=user_id, username=username, password_hash=password_hash,
                                       created_at=created_at, is_admin=is_admin, dek_wrapped=dek_wrapped)
        await create_default_agent(tx, user_id=user_id, created_at=created_at)
        return user

    async def _create_user(self, tx: Transaction, *, user_id: str, username: str, password_hash: str,
                           created_at: str, is_admin: bool = False, dek_wrapped: str | None = None) -> dict[str, Any]:
        await tx.execute(
            "INSERT INTO users (id, username, password_hash, dek_wrapped, "
            " is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, dek_wrapped, int(is_admin), created_at),
        )
        created = await self._get_user(tx, user_id)
        assert created is not None
        return created

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_user(tx, user_id)

    async def _get_user(self, tx: Transaction, user_id: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Case-insensitively, because the column is NOCASE and a login that
        only matched the exact casing would reject the right person."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            )
            row = await cursor.fetchone()
            return _row_to_user(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at"
            )
            return [_row_to_user(r) for r in await cursor.fetchall()]

    async def count_users(self) -> int:
        async with self.transaction(write=False) as tx:
            return await self._count(tx, "SELECT COUNT(*) FROM users")

    async def update_user_field(self, user_id: str, **fields: Any) -> None:
        """Set named columns. Same shape as `update_session_field` — the
        caller names what it means and the SQL is assembled once."""
        async with self.transaction(write=True) as tx:
            if not fields:
                return
            assignments = ", ".join(f"{name} = ?" for name in fields)
            await tx.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                (*fields.values(), user_id),
            )

    async def change_password(
        self, user_id: str, password_hash: str, *, at: str, keep: str | None = None
    ) -> int:
        """Commit the new password and other-device revocation together."""
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            return await self._revoke_all_tokens(tx, user_id, at=at, keep=keep)


    # -- invites ------------------------------------------------------------

    async def create_invite(
        self,
        *,
        code: str,
        created_by: str | None,
        created_at: str,
        expires_at: str | None = None,
        max_uses: int = 1,
    ) -> dict[str, Any]:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO invites (code, created_by, created_at, expires_at, "
                " max_uses) VALUES (?, ?, ?, ?, ?)",
                (code, created_by, created_at, expires_at, max_uses),
            )
            found = await self._get_invite(tx, code)
            assert found is not None
            return found

    async def get_invite(self, code: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_invite(tx, code)

    async def _get_invite(self, tx: Transaction, code: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            "SELECT code, created_by, created_at, expires_at, max_uses, "
            " used_count, revoked_at FROM invites WHERE code = ?",
            (code,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "code": row[0],
            "created_by": row[1],
            "created_at": row[2],
            "expires_at": row[3],
            "max_uses": row[4],
            "used_count": row[5],
            "revoked_at": row[6],
        }

    async def list_invites(self) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute("SELECT code FROM invites ORDER BY created_at")
            codes = [r[0] for r in await cursor.fetchall()]
            out = []
            for code in codes:
                invite = await self._get_invite(tx, code)
                if invite:
                    out.append(invite)
            return out

    async def claim_invite(self, code: str) -> bool:
        """Consume one use, atomically.

        The `used_count < max_uses` lives in the UPDATE rather than in a
        read-then-write, so two registrations racing for the last use of a code
        cannot both win — SQLite settles it and the loser sees 0 rows changed.
        """
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE invites SET used_count = used_count + 1 "
                " WHERE code = ? AND revoked_at IS NULL AND used_count < max_uses",
                (code,),
            )
            return (cursor.rowcount or 0) > 0

    async def revoke_invite(self, code: str, *, at: str) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE invites SET revoked_at = ? WHERE code = ? AND revoked_at IS NULL",
                (at, code),
            )

    # -- tokens -------------------------------------------------------------

    async def store_token(
        self,
        *,
        token_hash: str,
        user_id: str,
        kind: str,
        created_at: str,
        expires_at: str | None = None,
        label: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO auth_tokens (token_hash, user_id, kind, label, "
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, user_id, kind, label, created_at, expires_at),
            )

    async def get_token(self, token_hash: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_token(tx, token_hash)

    async def _get_token(self, tx: Transaction, token_hash: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            "SELECT token_hash, user_id, kind, label, created_at, expires_at, "
            " last_seen_at, revoked_at FROM auth_tokens WHERE token_hash = ?",
            (token_hash,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "token_hash": row[0],
            "user_id": row[1],
            "kind": row[2],
            "label": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "last_seen_at": row[6],
            "revoked_at": row[7],
        }

    async def get_token_user(self, token_hash: str) -> dict[str, Any] | None:
        """Read the bearer and its current account together, without a write."""
        async with self.transaction(write=False) as tx:
            columns = ", ".join(f"u.{column.strip()}" for column in _USER_COLUMNS.split(","))
            cursor = await tx.execute(
                f"SELECT {columns}, t.revoked_at, t.expires_at "
                "FROM auth_tokens AS t JOIN users AS u ON u.id = t.user_id "
                "WHERE t.token_hash = ?",
                (token_hash,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {"user": _row_to_user(row), "revoked_at": row[8], "expires_at": row[9]}

    async def list_tokens(self, user_id: str) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT token_hash FROM auth_tokens WHERE user_id = ? "
                " AND revoked_at IS NULL ORDER BY created_at",
                (user_id,),
            )
            hashes = [r[0] for r in await cursor.fetchall()]
            out = []
            for h in hashes:
                token = await self._get_token(tx, h)
                if token:
                    out.append(token)
            return out

    async def touch_token(self, token_hash: str, *, at: str) -> None:
        """Record that a bearer was just used.

        This short operation commits independently of transcript writes, so
        authentication cannot accidentally commit another operation's input.
        """
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE auth_tokens SET last_seen_at = ? WHERE token_hash = ?",
                (at, token_hash),
            )

    async def revoke_token(self, token_hash: str, *, at: str) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE auth_tokens SET revoked_at = ? WHERE token_hash = ? "
                " AND revoked_at IS NULL",
                (at, token_hash),
            )

    async def revoke_all_tokens(self, user_id: str, *, at: str, keep: str | None = None) -> int:
        """Sign a user out everywhere. `keep` spares the caller's own bearer,
        which is what "change my password" wants: other devices out, this one
        still in."""
        async with self.transaction(write=True) as tx:
            return await self._revoke_all_tokens(tx, user_id, at=at, keep=keep)

    async def _revoke_all_tokens(
        self, tx: Transaction, user_id: str, *, at: str, keep: str | None
    ) -> int:
        sql = (
            "UPDATE auth_tokens SET revoked_at = ? WHERE user_id = ? "
            " AND revoked_at IS NULL"
        )
        params: list[Any] = [at, user_id]
        if keep is not None:
            sql += " AND token_hash != ?"
            params.append(keep)
        cursor = await tx.execute(sql, params)
        return cursor.rowcount or 0

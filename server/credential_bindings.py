"""Validation of an account's explicit harness credential binding."""

from __future__ import annotations

import sqlite3

from .database import Database


class CredentialBindingError(ValueError):
    """A caller selected a credential that cannot be bound."""


async def validate_credential_binding(
    db: Database, credential_id: str | None, backend: str, user_id: str | None
) -> None:
    if credential_id is None:
        return
    row = await db.get_credential(credential_id, user_id=user_id)
    if row is None or row.get("user_id") != user_id:
        # Do not disclose whether an id exists in another account. None is
        # the legacy install's owner, not permission to bind an account's row.
        raise CredentialBindingError("Credential not found")
    if row["backend"] != backend:
        raise CredentialBindingError(
            "Credential backend does not match the selected backend. "
            "Choose a compatible credential or clear the binding."
        )


async def validate_failed_credential_write(
    db: Database,
    error: sqlite3.IntegrityError,
    credential_id: str | None,
    backend: str,
    user_id: str | None,
) -> None:
    """Identify a binding removed between validation and the atomic write.

    SQLite's foreign key rejects the whole statement, so no other Agent
    fields were changed. Unrelated constraints, and foreign-key failures with
    a still-valid binding, remain database errors for the caller to re-raise.
    """
    if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY:
        await validate_credential_binding(db, credential_id, backend, user_id)

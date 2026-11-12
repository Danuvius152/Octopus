"""Select durable account facts; execution history never enters the new database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..crypto import decrypt
from ..sqlite_schema import statements
from ..storage_verification import verify_main
from .schema import _SCHEMA, CURRENT_VERSION

OWNED = ("users", "agents", "backend_credentials", "connector_installations", "notifiers", "applications")
RELATED = ("credential_secrets", "connector_installation_secrets", "agent_connectors", "custom_connectors", "connector_oauth_clients")
# Message-private Git baseline observations leave with chats; they add no root
# and grant no file-revert authority to a restored account.
EXCLUDED = {"auth_tokens", "invites", "sessions", "messages", "schedules", "bg_tasks", "client_submissions",
            "research_jobs", "wiki_job_notifications"}


def _rows(db: sqlite3.Connection, table: str, where: str, values: tuple) -> list[dict]:
    return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" WHERE {where}', values)]


def read_account(source: sqlite3.Connection, user_id: str, *, key: str) -> dict[str, list[dict]]:
    """Validate and select current owned facts without creating any storage."""
    verify_main(source)
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if tables != set(OWNED) | set(RELATED) | EXCLUDED:
        raise ValueError("Account migration classification must be updated for the current storage tables")
    rows = {name: _rows(source, name, "id=?" if name == "users" else "user_id=?", (user_id,)) for name in OWNED}
    if len(rows["users"]) != 1 or not rows["users"][0]["dek_wrapped"]:
        raise ValueError("The selected account or its data key is missing")
    dek = decrypt(rows["users"][0]["dek_wrapped"], key)
    for row in rows["agents"]:
        # Target configuration starts a new authority; source chat is excluded.
        row["context_settings_revision"] = 0
    for row in rows["backend_credentials"]:
        if row["auth_type"] == "oauth":
            row.update(status="needs_reconnect", needs_reconnect=1, token_expires_at=None, last_refresh_error_code=None)
    rows["credential_secrets"] = _rows(source, "credential_secrets",
        "credential_id IN (SELECT id FROM backend_credentials WHERE user_id=? AND auth_type='api_key')", (user_id,))
    rows["connector_installation_secrets"] = _rows(source, "connector_installation_secrets",
        "installation_id IN (SELECT id FROM connector_installations WHERE user_id=?)", (user_id,))
    rows["agent_connectors"] = _rows(source, "agent_connectors",
        "agent_id IN (SELECT id FROM agents WHERE user_id=?)", (user_id,))
    rows["custom_connectors"] = _rows(source, "custom_connectors",
        "kind IN (SELECT kind FROM connector_installations WHERE user_id=?)", (user_id,))
    rows["connector_oauth_clients"] = _rows(source, "connector_oauth_clients",
        "kind IN (SELECT kind FROM connector_installations WHERE user_id=?)", (user_id,))
    for name in ("credential_secrets", "connector_installation_secrets"):
        for row in rows[name]:
            decrypt(row["secret_encrypted"], dek)
    for row in rows["connector_oauth_clients"]:
        decrypt(row["client_secret_encrypted"], key)
    for row in rows["applications"]:
        row["session_id"] = None
    return rows


def snapshot_account(source: sqlite3.Connection, target: Path, user_id: str, *, key: str) -> list[dict]:
    """Create the current schema, copying only explicitly owned durable facts."""
    rows = read_account(source, user_id, key=key)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as db, db:
        for statement in statements(_SCHEMA):
            db.execute(statement)
        for name in (*OWNED, *RELATED):
            for row in rows[name]:
                columns = ",".join('"' + column + '"' for column in row)
                db.execute(f'INSERT INTO "{name}" ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        db.execute(f"PRAGMA user_version={CURRENT_VERSION}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Selected account has references outside its migration scope")
    return rows["applications"]


def rebind_account(db_path: Path, map_path) -> None:
    """Rewrite defined system fields, never arbitrary user content or secrets."""
    with closing(sqlite3.connect(db_path)) as db, db:
        for identifier, roots in db.execute("SELECT id,extra_roots FROM users").fetchall():
            if roots:
                db.execute("UPDATE users SET extra_roots=? WHERE id=?", (json.dumps([map_path(p) for p in json.loads(roots)]), identifier))
        for identifier, path in db.execute("SELECT id,app_dir FROM applications").fetchall():
            db.execute("UPDATE applications SET app_dir=? WHERE id=?", (map_path(path), identifier))

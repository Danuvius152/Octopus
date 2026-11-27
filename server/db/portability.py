"""Select durable account facts; execution history never enters the new database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..crypto import decrypt
from ..sqlite_schema import statements
from ..storage_verification import (
    verify_agent_configurations,
    verify_main_layout_and_facts,
)
from .schema import _SCHEMA, CURRENT_VERSION

_INSTALLED_KIND = "kind IN (SELECT kind FROM connector_installations WHERE user_id=?)"
_ACCOUNT_FILTERS = {
    "users": "id=?",
    **dict.fromkeys(("agents", "backend_credentials", "connector_installations", "notifiers", "applications"), "user_id=?"),
    "credential_secrets": "credential_id IN (SELECT id FROM backend_credentials WHERE user_id=? AND auth_type='api_key')",
    "connector_installation_secrets": "installation_id IN (SELECT id FROM connector_installations WHERE user_id=?)",
    "agent_connectors": "agent_id IN (SELECT id FROM agents WHERE user_id=?)",
    "custom_connectors": _INSTALLED_KIND,
    "connector_oauth_clients": _INSTALLED_KIND,
}
# Chat exclusions include message-private Git baselines, pending revert writer
# identities, and generated BG summaries.
EXCLUDED = {"auth_tokens", "invites", "sessions", "messages", "schedules", "bg_tasks", "client_submissions",
            "research_jobs", "wiki_job_notifications"}
_INITIAL_CONTEXT_REVISION = 0
_OAUTH_ARCHIVE_STATE = {
    "status": "needs_reconnect",
    "needs_reconnect": 1,
    "token_expires_at": None,
    "last_refresh_error_code": None,
}


def verify_account_archive_rows(db: sqlite3.Connection, user_id: str) -> None:
    """Require the staged database to match the existing account archive rules."""
    for table, where in _ACCOUNT_FILTERS.items():
        if db.execute(f'SELECT 1 FROM "{table}" WHERE ({where}) IS NOT TRUE LIMIT 1', (user_id,)).fetchone():
            raise ValueError(f"Archive contains {table} rows outside the selected account")
    for table in sorted(EXCLUDED):
        if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
            raise ValueError(f"Archive contains excluded {table} rows")
    if db.execute("SELECT 1 FROM agents WHERE context_settings_revision IS NOT ? LIMIT 1",
                  (_INITIAL_CONTEXT_REVISION,)).fetchone():
        raise ValueError("Archive contains a noninitial Agent context settings revision")
    unexpected_state = " OR ".join(f"{field} IS NOT ?" for field in _OAUTH_ARCHIVE_STATE)
    if db.execute(f"SELECT 1 FROM backend_credentials WHERE auth_type='oauth' AND ({unexpected_state}) LIMIT 1",
                  tuple(_OAUTH_ARCHIVE_STATE.values())).fetchone():
        raise ValueError("Archive contains invalid OAuth credential state")


def read_account(source: sqlite3.Connection, user_id: str, *, key: str) -> dict[str, list[dict]]:
    """Validate and select current owned facts without creating any storage."""
    verify_main_layout_and_facts(source)
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if tables != _ACCOUNT_FILTERS.keys() | EXCLUDED:
        raise ValueError("Account migration classification must be updated for the current storage tables")
    verify_agent_configurations(source, user_id=user_id)
    rows = {table: [dict(row) for row in source.execute(f'SELECT * FROM "{table}" WHERE {where}', (user_id,))]
            for table, where in _ACCOUNT_FILTERS.items()}
    if len(rows["users"]) != 1 or not rows["users"][0]["dek_wrapped"]:
        raise ValueError("The selected account or its data key is missing")
    dek = decrypt(rows["users"][0]["dek_wrapped"], key)
    for row in rows["agents"]:
        # Target configuration starts a new authority; source chat is excluded.
        # An unfinished private deletion retains the original Agent and its closure.
        row["context_settings_revision"] = _INITIAL_CONTEXT_REVISION
    for row in rows["backend_credentials"]:
        if row["auth_type"] == "oauth":
            row.update(_OAUTH_ARCHIVE_STATE)
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
        for name, selected in rows.items():
            for row in selected:
                columns = ",".join('"' + column + '"' for column in row)
                db.execute(f'INSERT INTO "{name}" ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        db.execute(f"PRAGMA user_version={CURRENT_VERSION}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Selected account has references outside its migration scope")
    return rows["applications"]


def rebind_account(db_path: Path, user_id: str, map_path, application_paths: dict[str, str]) -> None:
    """Rewrite defined system fields, never arbitrary user content or secrets."""
    with closing(sqlite3.connect(db_path)) as db, db:
        user = db.execute("SELECT extra_roots FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("Archive is missing its declared account")
        if user[0]:
            db.execute("UPDATE users SET extra_roots=? WHERE id=?", (json.dumps([map_path(p) for p in json.loads(user[0])]), user_id))
        if {row[0] for row in db.execute("SELECT id FROM applications")} != application_paths.keys():
            raise ValueError("Archive contains applications outside the selected account")
        db.executemany("UPDATE applications SET app_dir=? WHERE id=?",
                       ((path, identifier) for identifier, path in application_paths.items()))

"""Durable protocol markers are never recreated as disposable projections."""

import json
import sqlite3
import uuid
from pathlib import Path

from ..service_health import ServiceHealth
from . import storage


def read_registration(state, *, require_root=True, binding_only=False):
    """Read current business identity or retained root exclusion evidence."""
    from .store import WikiError

    path = state / "config.json"
    if path.is_symlink():
        raise WikiError("Wiki registration is invalid; configuration retained", 503)
    if not path.is_file():
        raise WikiError("No local wiki is registered for this account", 404)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) - {"root", "vault_id", "curator_agent_id", "app_id", "registration_protocol", "registration_generation"}:
            raise ValueError("unsupported configuration structure")
        if not isinstance(config.get("root"), str) or not Path(config["root"]).is_absolute():
            raise ValueError("root must be an absolute path")
        if require_root and not Path(config["root"]).is_dir():
            raise ValueError("vault root is unavailable")
        if not isinstance(config.get("curator_agent_id"), str) or not config["curator_agent_id"]:
            raise ValueError("curator binding is missing")
        if config.get("app_id") is not None and not isinstance(config["app_id"], str):
            raise ValueError("invalid application binding")
        uuid.UUID(config["vault_id"])
        if "registration_protocol" in config or "registration_generation" in config:
            if type(config.get("registration_protocol")) is not int or config["registration_protocol"] != 1 or uuid.UUID(config["registration_generation"]).hex != config["registration_generation"]:
                raise ValueError("invalid registration generation")
        elif not binding_only:
            raise unavailable()
        return config
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise WikiError(f"Wiki registration is invalid; configuration retained: {exc}", 503) from exc


def unavailable():
    from .store import WikiError

    return WikiError("Wiki protocol ledger is missing or incompatible; durable state retained", 503)


def initialize(db):
    db.execute("CREATE TABLE wiki_protocols(name TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL)")
    db.execute("INSERT INTO wiki_protocols VALUES('registration',1)")


def require(db, name, version=1):
    columns = [(row[1], row[2].upper(), row[3], row[4], row[5], row[6])
               for row in db.execute("PRAGMA table_xinfo(wiki_protocols)")]
    if columns != [("name", "TEXT", 1, None, 1, 0), ("version", "INTEGER", 1, None, 0, 0)]:
        raise unavailable()
    row = db.execute("SELECT version FROM wiki_protocols WHERE name=?", (name,)).fetchone()
    if row is None or row[0] != version:
        raise unavailable()


def registration_preflight(state, *, health=None, complete=False):
    """Read supported storage and binding before a writer or publication."""
    path = state / "index.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise unavailable()
    try:
        with storage.connect(path, health if health is not None else ServiceHealth(), write=False) as db:
            db.execute("BEGIN")
            from .schema import require_current_format

            require_current_format(db)
            if complete or (state / "config.json").exists():
                require(db, "registration_config")
            binding = db.execute("SELECT root,vault_id FROM filesystem_state WHERE singleton=1").fetchone()
            if binding is None:
                raise unavailable()
            return dict(binding)
    except sqlite3.DatabaseError as error:
        raise unavailable() from error

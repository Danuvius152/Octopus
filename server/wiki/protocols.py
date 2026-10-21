"""Durable protocol markers are never recreated as disposable projections."""

import json
import sqlite3
import uuid
from pathlib import Path

from ..service_health import ServiceHealth
from . import storage


def read_registration(state, *, require_root=True, check_ledger=True, health=None):
    """Read the registration used by runtime and installation setup alike."""
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
        elif check_ledger:
            registration_preflight(state.resolve(), legacy=True, health=health)
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


def registration_preflight(state, *, legacy=False, health=None, complete=False):
    """Read-only check before a config upgrade can replace any file bytes."""
    path = state / "index.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise unavailable()
    try:
        with storage.connect(path, health if health is not None else ServiceHealth(), write=False) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "wiki_schema" not in tables:
                raise unavailable()
            version = db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] or 0
            from .schema import VERSION, validate_filesystem_ledger

            if version < 1 or version > VERSION or complete and version != VERSION:
                raise unavailable()
            if version >= 10:
                require(db, "registration")
            if legacy and (version >= 10 or "wiki_protocols" in tables):
                raise unavailable()
            if complete:
                from . import (
                    grants,
                    job_events,
                    maintenance_selections,
                    recovery_state,
                    source_bindings,
                )

                require(db, "registration_config")
                grants.require(db)
                job_events.require(db)
                maintenance_selections.require(db)
                recovery_state.require(db)
                source_bindings.validate(db)
            if version >= 9:
                validate_filesystem_ledger(db)
                binding = db.execute("SELECT root,vault_id FROM filesystem_state WHERE singleton=1").fetchone()
                if binding is None:
                    raise unavailable()
                return dict(binding)
            return None
    except sqlite3.DatabaseError as error:
        raise unavailable() from error

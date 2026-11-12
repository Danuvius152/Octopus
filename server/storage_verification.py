"""Read-only publication checks against the candidate's current declarations.

This boundary owns full retained-layout inspection. It never initializes a
database, repairs projections, interprets an earlier format or reads secrets.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .account_baseline import persistent_database_file
from .sqlite_schema import definition, statements

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .config import Settings


@contextmanager
def _reader(path: Path) -> Iterator[sqlite3.Connection]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Storage ledger is not a regular file: {path}")
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
            raise RuntimeError(f"Storage integrity check failed: {path}")
        yield db
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError(f"Storage relationships are inconsistent: {path}")


def verify_main(db: sqlite3.Connection) -> int:
    """Require the complete current layout without the startup upgrade path."""
    from .db.schema import _SCHEMA, CURRENT_VERSION

    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != CURRENT_VERSION:
        raise RuntimeError("Primary database does not match the current storage format")
    catalog = "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'"
    actual = {(kind, name): definition(kind, sql) for kind, name, sql in db.execute(catalog)}
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in statements(_SCHEMA):
            reference.execute(statement)
        expected = {(kind, name): definition(kind, sql) for kind, name, sql in reference.execute(catalog)}
    if actual != expected:
        raise RuntimeError("Primary database does not have the complete current structure")
    _verify_main_facts(db)
    for (identity,) in db.execute("SELECT external_account_id FROM connector_installations WHERE kind='github'"):
        if not isinstance(identity, str) or re.fullmatch(r"[1-9][0-9]*", identity) is None:
            raise RuntimeError("Primary database contains an invalid current GitHub identity")
    return version


def _verify_main_facts(db: sqlite3.Connection) -> None:
    """Check retained facts shared by current inspection and finite publication."""
    from pydantic import ValidationError

    from .context_models import ContextState
    from .delegations import _PersistedRound
    from .fork_helpers import decode_ignored_baseline
    from .session_operations import decode_payload
    from .submission_models import NativeInputEvidence

    invalid = db.execute("""SELECT 1 FROM schedules WHERE wiki_mode NOT IN ('auto','read','maintain')
        UNION ALL SELECT 1 FROM client_submissions
        WHERE execution_state NOT IN ('pending','running','done','withdrawn','unknown')
          OR reason_code='legacy_rejected'
          OR CASE WHEN json_valid(payload) THEN json_extract(payload,'$.wiki_mode')='off'
              OR json_extract(payload,'$.operation.wiki_mode')='off' ELSE 0 END
        LIMIT 1""").fetchone()
    if invalid:
        raise RuntimeError("Primary database contains inputs outside the current contract")
    if db.execute("SELECT 1 FROM agents WHERE typeof(context_settings_revision)!='integer' OR context_settings_revision<0 LIMIT 1").fetchone():
        raise RuntimeError("Primary database contains an invalid context settings revision")
    try:
        for (raw,) in db.execute("SELECT git_ignored_baseline FROM messages WHERE git_ignored_baseline IS NOT NULL"):
            decode_ignored_baseline(raw)
    except (ValueError, TypeError):
        raise RuntimeError("Primary database contains an invalid ignored-path baseline") from None
    try:
        for (raw,) in db.execute("SELECT context_state FROM sessions WHERE context_state IS NOT NULL"):
            ContextState.model_validate_json(raw)
    except (ValueError, TypeError):
        raise RuntimeError("Primary database contains an invalid current context snapshot") from None
    try:
        for row in db.execute("SELECT delegation_state FROM sessions WHERE delegation_state IS NOT NULL"):
            _PersistedRound.model_validate_json(row[0])
    except ValidationError:
        raise RuntimeError("Primary database contains an invalid current delegation snapshot") from None
    try:
        for request_id, raw, payload, execution in db.execute("SELECT request_id,native_inputs,payload,execution_state FROM client_submissions"):
            entries = [NativeInputEvidence.model_validate(item) for item in json.loads(raw)]
            if (any(item.input_id != request_id for item in entries)
                    or len({item.attempt_id for item in entries}) != len(entries)):
                raise ValueError("Native evidence belongs to a different input or repeats an attempt")
            operation = decode_payload(json.loads(payload))["operation"] if payload is not None else None
            if operation is not None and operation["kind"] == "compact":
                if any(item.kind != "compact" for item in entries):
                    raise ValueError("Compaction contains ordinary message evidence")
                if execution == "withdrawn" and any(item.state in {"accepted", "unknown"} for item in entries):
                    raise ValueError("A possibly executed compaction cannot be withdrawn")
    except (ValueError, TypeError):
        raise RuntimeError("Primary database contains invalid current native input evidence") from None


def verify_wiki(db: sqlite3.Connection, registration: dict) -> int:
    """Check durable authority; missing derived indexes remain rebuildable."""
    from .wiki import (
        grants,
        job_controls,
        job_events,
        job_items,
        maintenance_inputs,
        maintenance_selections,
        protocols,
        recovery_state,
        source_bindings,
    )
    from .wiki.schema import VERSION, validate_filesystem_ledger
    from .wiki.storage_verification import _catalog, _reference

    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != VERSION:
        raise RuntimeError("Wiki ledger does not match the current storage format")
    expected, markers, repairable, replaceable = _reference()
    actual = _catalog(db)
    if dict(db.execute("SELECT name,version FROM wiki_protocols")) != markers or actual.keys() - expected.keys():
        raise RuntimeError("Wiki ledger contains an incompatible structure or protocol")
    for key, value in expected.items():
        if key not in actual:
            if key in repairable:
                continue
        elif actual[key] == value:
            continue
        elif key in replaceable and actual[key][0] == value[0]:
            if replaceable[key] is None or actual[key] in replaceable[key]:
                continue
        raise RuntimeError("Wiki ledger does not have the current durable structure")
    validate_filesystem_ledger(db)
    source_bindings.validate(db)
    grants.require(db)
    maintenance_selections.require(db)
    job_controls.require(db)
    job_events.require(db)
    recovery_state.require(db)
    maintenance_inputs.require(db)
    protocols.require(db, "registration_config")
    binding = db.execute("SELECT root,vault_id FROM filesystem_state WHERE singleton=1").fetchone()
    if binding is None or dict(binding) != {key: registration[key] for key in ("root", "vault_id")}:
        raise RuntimeError("Wiki registration has a different durable ledger binding")
    for row in db.execute("SELECT items FROM jobs"):
        job_items.decode(row[0])
    return version


def verify_storage(config: Settings) -> dict:
    """Inspect all configured ledgers, including real committed WAL contents."""
    main_path = persistent_database_file(config.db_path)
    if main_path is None:
        raise RuntimeError("Publication requires the retained primary database")
    with _reader(main_path) as db:
        main_version = verify_main(db)
    return {"passed": True, "read_only": True, "main_integrity": "ok",
            "main_format_version": main_version, "registered_wiki_ledgers": _verify_registered_wikis(config)}


def _verify_registered_wikis(config: Settings) -> list[dict]:
    """One complete registration/ledger check across publication phases."""
    from .service_installation import _registered_vaults
    from .wiki.protocols import read_registration

    registered = list(_registered_vaults(config))
    states = [Path(config.wiki_dir)]
    users = Path(config.users_root)
    if users.is_dir():
        states.extend(path / "wiki" for path in users.iterdir() if path.is_dir())
    expected = {state.resolve() for state in states
                if (state / "config.json").exists() or (state / "config.json").is_symlink()}
    if expected != {state for _, state in registered}:
        raise RuntimeError("An existing Wiki registration could not be verified; retain its configuration")
    ledgers = []
    for _root, state in registered:
        registration = read_registration(state)
        with _reader(state / "index.sqlite3") as db:
            version = verify_wiki(db, registration)
        ledgers.append({"schema_version": version, "protocols_complete": True})
    return ledgers

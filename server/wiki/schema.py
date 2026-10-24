"""Inspect and prepare supported Wiki formats; derived data has no authority."""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from ..sqlite_schema import definition

VERSION = 13

_CATALOG = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'"


@dataclass(frozen=True)
class Inspection:
    version: int
    with_jobs: bool
    repairs: frozenset[str]


def _columns(db, name):
    return tuple(tuple(row)[1:] for row in db.execute("SELECT * FROM pragma_table_xinfo(?)", (name,)))


def _catalog(db):
    result = {}
    for kind, name, table, sql in db.execute(_CATALOG):
        columns = _columns(db, name) if kind == "table" else None
        result[kind, name] = (table, definition(kind, sql), columns)
    return result


def _reference(with_jobs):
    from . import maintenance_inputs, schema_ddl

    with closing(sqlite3.connect(":memory:")) as reference:
        reference.row_factory = sqlite3.Row
        reference.execute("PRAGMA recursive_triggers=ON")
        reference.execute("BEGIN IMMEDIATE")
        schema_ddl.create(reference, with_jobs=with_jobs)
        catalog = _catalog(reference)
        protocols = dict(reference.execute("SELECT name,version FROM wiki_protocols"))
        tables = {
            "maintenance": set(maintenance_inputs.TABLES),
            "search": {"search_documents", "search_sections", "search_fts"},
            "materials": {"raw_materials"},
            "catalog": {"wiki_edges", "wiki_edge_lookups", "wiki_lookup_names", "catalog_state",
                        "catalog_dirty", "wiki_uid_claims", "wiki_uid_dirty"},
        }
        tables["search"].update(row[1] for row in reference.execute("PRAGMA table_list") if row[2] == "shadow")
        triggers = {
            "maintenance": set(maintenance_inputs.triggers(with_jobs)),
            "search": {"search_section_delete", "search_document_delete"},
            "materials": {"raw_material_delete"},
            "catalog": {"wiki_edge_delete", "wiki_reference_delete", "wiki_lookup_delete"},
        }
        derived_indexes = {definition("index", sql)[2] for sql in maintenance_inputs.INDEXES}
        repairable = {}
        for key, value in catalog.items():
            if key[0] == "index" and key[1] in derived_indexes:
                repairable[key] = "maintenance"
            else:
                for owner in tables:
                    if value[0] in tables[owner] or key[0] == "trigger" and key[1] in triggers[owner]:
                        repairable[key] = owner
                        break
        replaceable: dict[tuple[str, str], frozenset[tuple] | None] = dict.fromkeys(
            {("table", name) for name in maintenance_inputs.TABLES}
            | {("trigger", name) for name in maintenance_inputs.triggers(with_jobs)}
            | {("index", name) for name in derived_indexes})
        # The search owner can append these two known tail columns, in order.
        # Admit only its exact repairable layouts, never arbitrary cache DDL.
        section_layouts = set()
        declaration = reference.execute("SELECT sql FROM sqlite_schema WHERE name='search_sections'").fetchone()[0]
        # ALTER validates the entire schema. These local columns need only the
        # trusted table declaration, not repeated validation of every trigger.
        with closing(sqlite3.connect(":memory:")) as sections:
            sections.execute(declaration)
            for column in ("readable_body", "section_role"):
                sections.execute("ALTER TABLE search_sections DROP COLUMN " + column)
                sql = sections.execute("SELECT sql FROM sqlite_schema WHERE name='search_sections'").fetchone()[0]
                section_layouts.add(("search_sections", definition("table", sql), _columns(sections, "search_sections")))
        replaceable["table", "search_sections"] = frozenset(section_layouts)
    return catalog, protocols, repairable, replaceable


def inspect(db):
    """Read one existing committed format; no preparation or file observation."""
    from . import (
        grants,
        job_controls,
        job_events,
        maintenance_inputs,
        maintenance_selections,
        protocols,
        recovery_state,
        source_bindings,
    )
    from .store import WikiError

    try:
        version = db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0]
        if version not in (12, VERSION) or version > VERSION:
            raise WikiError("Wiki format has no supported preparation path; durable state retained", 503)
        protocols.require(db, "registration")
        markers = dict(db.execute("SELECT name,version FROM wiki_protocols"))
        with_jobs = "maintenance_jobs" in markers
        if version == VERSION and not with_jobs:
            raise WikiError("Current Wiki format requires its complete job authority; durable state retained", 503)
        expected, expected_markers, repairable, replaceable = _reference(with_jobs)
        if version == 12:
            # A directly created, unpublished Store predates config publication.
            # Registered readers separately require this lifecycle receipt.
            expected_markers.pop("registration_config")
            registration = markers.pop("registration_config", None)
            if registration not in (None, 1):
                raise WikiError("Wiki registration protocol is incompatible; durable state retained", 503)
        actual = _catalog(db)
        if markers != expected_markers:
            raise protocols.unavailable()
        if actual.keys() - expected.keys():
            raise WikiError("Wiki format contains an unsupported structure or protocol; durable state retained", 503)
        for key, value in expected.items():
            if key not in actual:
                if key not in repairable:
                    raise WikiError("Wiki format is incomplete or incompatible; durable state retained", 503)
            elif actual[key] != value:
                if (key not in replaceable or actual[key][0] != value[0]
                        or replaceable[key] is not None and actual[key] not in replaceable[key]):
                    raise WikiError("Wiki format is incomplete or incompatible; durable state retained", 503)
        validate_filesystem_ledger(db)
        source_bindings.validate(db)
        grants.require(db)
        maintenance_selections.require(db)
        job_controls.require(db)
        job_events.require(db)
        recovery_state.require(db)
        maintenance_inputs.require(db)
        if not with_jobs:
            # References come from the trusted current declarations. The absent
            # authority may never be recreated over retained job-specific facts.
            references = [key[1] for key, value in expected.items() if key[0] == "table" and key not in repairable
                          and any(column[0] == "job_id" for column in value[2])]
            for table in references:
                quoted = table.replace('"', '""')
                if ("table", table) in actual and db.execute(f'SELECT 1 FROM "{quoted}" WHERE job_id IS NOT NULL LIMIT 1').fetchone():  # noqa: S608
                    raise WikiError("Wiki job references have no authority; durable state retained", 503)
            if db.execute("SELECT 1 FROM wiki_job_deliveries LIMIT 1").fetchone():
                raise WikiError("Wiki job deliveries have no authority; durable state retained", 503)
        repairs = {owner for key, owner in repairable.items() if key not in actual or actual[key] != expected[key]}
        if "maintenance" not in repairs:
            state = db.execute("SELECT version FROM maintenance_input_state WHERE singleton=1").fetchone()
            if state is None or state[0] != maintenance_inputs.VERSION:
                repairs.add("maintenance")
        if "search" not in repairs:
            from . import search_index

            if db.execute("SELECT 1 FROM search_documents WHERE version<>? LIMIT 1", (search_index.VERSION,)).fetchone():
                repairs.add("search")
        if "catalog" not in repairs:
            from . import catalog

            state = db.execute("SELECT version FROM catalog_state WHERE singleton=1").fetchone()
            if state is None or state[0] != catalog.VERSION:
                repairs.add("catalog")
        return Inspection(version, with_jobs, frozenset(repairs))
    except ValueError as error:
        raise WikiError("Wiki format is unavailable; durable state retained", 503) from error


def validate_filesystem_ledger(db):
    """Identity-event state is durable; an incompatible table is not a cache."""
    from .store import WikiError

    expected = {
        "filesystem_state": [
            ("singleton", "INTEGER", 0, None, 1),
            ("protocol", "INTEGER", 1, None, 0),
            ("root", "TEXT", 1, None, 0),
            ("vault_id", "TEXT", 1, None, 0),
            ("received_seq", "INTEGER", 1, "0", 0),
            ("applied_seq", "INTEGER", 1, "0", 0),
            ("armed_epoch", "TEXT", 0, None, 0),
            ("uncertain", "TEXT", 0, None, 0),
        ],
        "filesystem_inbox": [
            ("seq", "INTEGER", 0, None, 1),
            ("batch_id", "TEXT", 1, None, 0),
            ("observer_epoch", "TEXT", 1, None, 0),
            ("root_generation", "TEXT", 1, None, 0),
            ("payload", "TEXT", 1, None, 0),
            ("payload_hash", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ],
    }
    for table, columns in expected.items():
        actual = [(r[1], r[2].upper(), r[3], r[4], r[5]) for r in db.execute(f"PRAGMA table_xinfo({table})")]
        if actual != columns:
            raise WikiError("Filesystem-event ledger is missing or incompatible; durable state retained", 503)
    sql = {
        row[0]: re.sub(r"\s+", "", row[1])
        for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('filesystem_state','filesystem_inbox')")
    }
    unique_batch = any(
        row[2] and not row[4]
        and [column[2] for column in db.execute("SELECT * FROM pragma_index_info(?)", (row[1],))] == ["batch_id"]
        for row in db.execute("PRAGMA index_list(filesystem_inbox)")
    )
    if (
        not re.search(r"(?i:CHECK)\((?i:singleton)=1\)", sql["filesystem_state"])
        or not re.search(r"(?i:CHECK)\((?i:statusIN)\('pending','applied'\)\)", sql["filesystem_inbox"])
        or not unique_batch
    ):
        raise WikiError("Filesystem-event ledger constraints are incompatible; durable state retained", 503)


def _repair(db, owners):
    """Known projection owners share one transaction after durable admission."""
    from . import catalog, maintenance_inputs, materials, search_index

    if not owners:
        return
    maintenance_inputs.prepare_repair(db)
    if "search" in owners:
        search_index.initialize(db)
        db.execute("UPDATE index_state SET checked_at=NULL")
    if "materials" in owners:
        materials.initialize(db)
    if "catalog" in owners:
        catalog.initialize(db)
    if owners & {"materials", "catalog"}:
        db.execute("UPDATE documents SET signature=''")
        db.execute("UPDATE index_state SET checked_at=NULL")
    maintenance_inputs.initialize(db)


def initialize(store):
    """Commit complete creation or the finite 12→13 step, then publish nothing."""
    from . import schema_ddl
    from .jobs import WikiJobs
    from .store import WikiError

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT 1 FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' LIMIT 1").fetchone()
        if existing is None:
            schema_ddl.create(db)
            db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                       (str(store.root), store.vault_id))
            db.execute("INSERT INTO wiki_schema VALUES(?,?)", (VERSION, datetime.now(UTC).isoformat()))
        else:
            admitted = inspect(db)
            _repair(db, admitted.repairs)
            if admitted.version == 12:
                if not admitted.with_jobs:
                    WikiJobs.initialize_ledger(db)
                if db.execute("SELECT 1 FROM wiki_protocols WHERE name='registration_config'").fetchone() is None:
                    db.execute("INSERT INTO wiki_protocols VALUES('registration_config',1)")
                db.execute("INSERT INTO wiki_schema VALUES(?,?)", (VERSION, datetime.now(UTC).isoformat()))
        prepared = inspect(db)
        if prepared.version != VERSION or prepared.repairs:
            raise WikiError("Wiki preparation did not establish the complete current format; durable state retained", 503)

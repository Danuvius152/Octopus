"""The complete current Wiki declarations used by publication, never startup."""

import sqlite3
from contextlib import closing

from ..sqlite_schema import definition

_CATALOG = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'"


def _columns(db, name):
    return tuple(tuple(row)[1:] for row in db.execute("SELECT * FROM pragma_table_xinfo(?)", (name,)))


def _catalog(db):
    result = {}
    for kind, name, table, sql in db.execute(_CATALOG):
        columns = _columns(db, name) if kind == "table" else None
        result[kind, name] = (table, definition(kind, sql), columns)
    return result


def _reference():
    from . import maintenance_inputs, schema_ddl

    with closing(sqlite3.connect(":memory:")) as reference:
        reference.row_factory = sqlite3.Row
        reference.execute("PRAGMA recursive_triggers=ON")
        reference.execute("BEGIN IMMEDIATE")
        schema_ddl.create(reference)
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
            "maintenance": set(maintenance_inputs.triggers()),
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
            | {("trigger", name) for name in maintenance_inputs.triggers()}
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

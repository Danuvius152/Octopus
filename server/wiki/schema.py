"""Current Wiki creation and projection repair, without historical admission."""

import re

VERSION = 16


def require_current_format(db):
    from .store import WikiError

    if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
        raise WikiError("Wiki format does not match this program; durable state retained", 503)


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



def repair_projections(db):
    """Projection owners rebuild only their disposable state, in one transaction."""
    from . import catalog, maintenance_inputs, materials, search_index

    maintenance_inputs.prepare_repair(db)
    search_changed = search_index.initialize(db)
    material_changed = materials.initialize(db)
    catalog_changed = catalog.initialize(db)
    if material_changed or catalog_changed:
        db.execute("UPDATE documents SET signature=''")
    if search_changed or material_changed or catalog_changed:
        db.execute("UPDATE index_state SET checked_at=NULL")
    maintenance_inputs.initialize(db)


def initialize(store):
    """Create one current empty layout or reopen and repair current projections."""
    from . import protocols, schema_ddl
    from .store import WikiError

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == VERSION:
            protocols.require(db, "registration")
            protocols.require(db, "maintenance_jobs")
            repair_projections(db)
            return
        existing = db.execute("SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1").fetchone()
        if version != 0 or existing is not None:
            raise WikiError("Wiki format does not match this program; durable state retained", 503)
        schema_ddl.create(db)
        db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                   (str(store.root), store.vault_id))
        db.execute(f"PRAGMA user_version={VERSION}")

"""Transactional migrations; derived tables never replace durable ledgers."""

import re
from datetime import UTC, datetime

VERSION = 9


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


def initialize(store):
    from . import materials, search_index

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS wiki_schema(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
        from .store import WikiError

        if (db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] or 0) > VERSION:
            raise WikiError("Wiki schema is newer than this framework; durable state retained", 503)
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=1").fetchone():
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,revision TEXT NOT NULL,
                title TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,
                pages TEXT NOT NULL,quality TEXT NOT NULL,metadata TEXT NOT NULL,
                signature TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS managed_notes (
                path TEXT PRIMARY KEY,base_hash TEXT NOT NULL,rendered_revision TEXT NOT NULL,
                regions TEXT NOT NULL,metadata TEXT NOT NULL
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS document_identities(id TEXT PRIMARY KEY,path TEXT UNIQUE,file_key TEXT NOT NULL)")
            for row in db.execute("SELECT id,path FROM documents").fetchall():
                file = store.observed_path(row["path"])
                key = store.file_key(file.stat()) if file is not None and file.is_file() else ""
                db.execute("INSERT OR IGNORE INTO document_identities VALUES(?,?,?)", (row["id"], row["path"], key))
            db.execute("""CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY,path TEXT NOT NULL,before_hash TEXT,after_hash TEXT,
                actor TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS change_management(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS change_proposals(change_id TEXT PRIMARY KEY,operation TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS change_retention (
                change_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
                before_available INTEGER NOT NULL,after_available INTEGER NOT NULL,resolved_at TEXT
            )""")
            db.execute("""INSERT OR IGNORE INTO change_retention
                SELECT c.id,COALESCE(i.id,c.path),1,1,NULL FROM changes c
                LEFT JOIN document_identities i ON i.path=c.path""")
            db.execute("CREATE TABLE IF NOT EXISTS app_trash(trash_id TEXT PRIMARY KEY,path TEXT NOT NULL,document_id TEXT,management TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS job_publications (
                job_id TEXT NOT NULL,source_doc TEXT NOT NULL,input_revision TEXT NOT NULL,
                change_id TEXT PRIMARY KEY,output_path TEXT NOT NULL,output_revision TEXT NOT NULL,
                indexed INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS job_publication_source ON job_publications(job_id,source_doc)")
            db.execute("""CREATE TABLE IF NOT EXISTS index_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch INTEGER NOT NULL,
                checked_at TEXT,error TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO index_state VALUES(1,0,NULL,NULL)")
            search_index.initialize(db)
            materials.initialize(db)
            db.execute("INSERT INTO wiki_schema VALUES(1,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=2").fetchone():
            db.execute("CREATE TABLE uid_aliases(uid TEXT PRIMARY KEY,doc_id TEXT NOT NULL)")
            db.execute("CREATE INDEX uid_alias_document ON uid_aliases(doc_id)")
            db.execute("INSERT INTO wiki_schema VALUES(2,?)", (datetime.now(UTC).isoformat(),))
        from . import catalog

        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=3").fetchone():
            catalog.initialize(db)
            db.execute("UPDATE documents SET signature=''")
            db.execute("INSERT INTO wiki_schema VALUES(3,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=4").fetchone():
            db.execute("""CREATE TABLE operation_receipts(
                id TEXT PRIMARY KEY,job_id TEXT,item_doc TEXT,doc_id TEXT NOT NULL,
                path TEXT NOT NULL,input_revision TEXT,output_revision TEXT,
                operation TEXT NOT NULL,reason TEXT NOT NULL,change_id TEXT,
                created_at TEXT NOT NULL,details TEXT NOT NULL)""")
            db.execute("CREATE INDEX receipt_job_item ON operation_receipts(job_id,item_doc)")
            db.execute("INSERT INTO wiki_schema VALUES(4,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=5").fetchone():
            search_index.initialize(db)
            db.execute("CREATE TABLE watch_directories(path TEXT PRIMARY KEY)")
            db.execute("UPDATE index_state SET checked_at=NULL")
            db.execute("INSERT INTO wiki_schema VALUES(5,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=6").fetchone():
            db.execute("CREATE INDEX wiki_dirty_documents ON documents(path) WHERE signature=''")
            for action in ("INSERT", "UPDATE", "DELETE"):
                binding = "old" if action == "DELETE" else "new"
                db.execute(f"""CREATE TRIGGER wiki_management_{action.lower()} AFTER {action} ON managed_notes
                    BEGIN UPDATE documents SET signature='' WHERE path={binding}.path; END""")
            db.execute("INSERT INTO wiki_schema VALUES(6,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=7").fetchone():
            if "status" not in {column[1] for column in db.execute("PRAGMA table_info(wiki_edges)")}:
                db.execute("ALTER TABLE wiki_edges ADD COLUMN status TEXT NOT NULL DEFAULT 'unresolved'")
            db.execute("UPDATE documents SET signature=''")
            db.execute("UPDATE index_state SET checked_at=NULL")
            db.execute("INSERT INTO wiki_schema VALUES(7,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=8").fetchone():
            db.execute("CREATE TABLE material_origins(doc_id TEXT PRIMARY KEY,state TEXT NOT NULL)")
            db.execute("CREATE TABLE change_material_origins(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT)")
            db.execute("ALTER TABLE app_trash ADD COLUMN origin_state TEXT")
            for action in ("INSERT", "UPDATE", "DELETE"):
                binding = "old" if action == "DELETE" else "new"
                db.execute(f"""CREATE TRIGGER wiki_origin_{action.lower()} AFTER {action} ON material_origins
                    BEGIN UPDATE documents SET signature='' WHERE id={binding}.doc_id; END""")
            db.execute("INSERT INTO wiki_schema VALUES(8,?)", (datetime.now(UTC).isoformat(),))
        if not db.execute("SELECT 1 FROM wiki_schema WHERE version=9").fetchone():
            if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('filesystem_state','filesystem_inbox') LIMIT 1").fetchone():
                validate_filesystem_ledger(db)
            db.execute("""CREATE TABLE IF NOT EXISTS filesystem_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),protocol INTEGER NOT NULL,
                root TEXT NOT NULL,vault_id TEXT NOT NULL,
                received_seq INTEGER NOT NULL DEFAULT 0,applied_seq INTEGER NOT NULL DEFAULT 0,
                armed_epoch TEXT,uncertain TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS filesystem_inbox(
                seq INTEGER PRIMARY KEY,batch_id TEXT UNIQUE NOT NULL,
                observer_epoch TEXT NOT NULL,root_generation TEXT NOT NULL,
                payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','applied')),created_at TEXT NOT NULL
            )""")
            validate_filesystem_ledger(db)
            db.execute("INSERT INTO wiki_schema VALUES(9,?)", (datetime.now(UTC).isoformat(),))
        validate_filesystem_ledger(db)
        db.execute("CREATE INDEX IF NOT EXISTS filesystem_inbox_pending ON filesystem_inbox(seq) WHERE status='pending'")
        available = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"material_origins", "change_material_origins"}.issubset(available):
            raise WikiError("Material-origin ledger is missing; durable state retained", 503)
        if "origin_state" not in {row[1] for row in db.execute("PRAGMA table_info(app_trash)")}:
            raise WikiError("Material-origin trash state is missing; durable state retained", 503)
        search_missing = not {"search_documents", "search_sections", "search_fts"}.issubset(available)
        search_changed = search_missing or (
            "readable_body" not in {column[1] for column in db.execute("PRAGMA table_info(search_sections)")}
            or db.execute("SELECT 1 FROM search_documents WHERE version<>? LIMIT 1", (search_index.VERSION,)).fetchone()
        )
        search_index.initialize(db)
        if search_changed:
            db.execute("UPDATE index_state SET checked_at=NULL")
        if "raw_materials" not in available:
            materials.initialize(db)
        if "wiki_edges" not in available:
            catalog.initialize(db)
            db.execute("UPDATE documents SET signature=''")
            db.execute("UPDATE index_state SET checked_at=NULL")

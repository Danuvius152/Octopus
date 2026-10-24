"""Current Wiki storage declarations composed under the preparation owner."""

from ..sqlite_schema import statements

PREVIOUS_TABLES = {
    "jobs": """CREATE TABLE jobs (
          id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,
          payload TEXT NOT NULL,status TEXT NOT NULL,items TEXT NOT NULL,error TEXT,
          session_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          attempt_errors TEXT NOT NULL DEFAULT '{}'
        )""",
    "operation_receipts": """CREATE TABLE operation_receipts(
                id TEXT PRIMARY KEY,job_id TEXT,item_doc TEXT,doc_id TEXT NOT NULL,
                path TEXT NOT NULL,input_revision TEXT,output_revision TEXT,
                operation TEXT NOT NULL,reason TEXT NOT NULL,change_id TEXT,
                created_at TEXT NOT NULL,details TEXT NOT NULL)""",
}

# Exact parser-normalized declarations of the two 12/13 emitters. Admission
# verifies these before replacement; no executable old projection is retained.
PREVIOUS_EVENTS = {
    "wiki_job_state_emit_insert": "cf25175087233b46cdabefa69d01069e8fb5f663abe5aa4459c1e8527e157fd3",
    "wiki_job_state_emit_update": "812f0ed59754542bf750837076f7e6336610356d33273fbb2103703b65f992d6",
}

ITEM_MATCH_COLUMN = "item_match_sequence INTEGER CHECK(item_match_sequence IS NULL OR typeof(item_match_sequence)='integer')"

_CORE_SCHEMA = """
CREATE TABLE app_trash(trash_id TEXT PRIMARY KEY,path TEXT NOT NULL,document_id TEXT,management TEXT, origin_state TEXT, source_plan TEXT);

CREATE TABLE change_management(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT);

CREATE TABLE change_material_origins(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT);

CREATE TABLE change_proposals(change_id TEXT PRIMARY KEY,operation TEXT NOT NULL);

CREATE TABLE change_retention (
                change_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
                before_available INTEGER NOT NULL,after_available INTEGER NOT NULL,resolved_at TEXT
            );

CREATE TABLE changes (
                id TEXT PRIMARY KEY,path TEXT NOT NULL,before_hash TEXT,after_hash TEXT,
                actor TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL
            );

CREATE TABLE document_identities(id TEXT PRIMARY KEY,path TEXT UNIQUE,file_key TEXT NOT NULL);

CREATE TABLE documents (
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,revision TEXT NOT NULL,
                title TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,
                pages TEXT NOT NULL,quality TEXT NOT NULL,metadata TEXT NOT NULL,
                signature TEXT NOT NULL
            );

CREATE TABLE filesystem_inbox(
                seq INTEGER PRIMARY KEY,batch_id TEXT UNIQUE NOT NULL,
                observer_epoch TEXT NOT NULL,root_generation TEXT NOT NULL,
                payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','applied')),created_at TEXT NOT NULL
            );

CREATE TABLE filesystem_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),protocol INTEGER NOT NULL,
                root TEXT NOT NULL,vault_id TEXT NOT NULL,
                received_seq INTEGER NOT NULL DEFAULT 0,applied_seq INTEGER NOT NULL DEFAULT 0,
                armed_epoch TEXT,uncertain TEXT
            );

CREATE TABLE index_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch INTEGER NOT NULL,
                checked_at TEXT,error TEXT
            );

CREATE TABLE job_publications (
                job_id TEXT NOT NULL,source_doc TEXT NOT NULL,input_revision TEXT NOT NULL,
                change_id TEXT PRIMARY KEY,output_path TEXT NOT NULL,output_revision TEXT NOT NULL,
                indexed INTEGER NOT NULL DEFAULT 0
            );

CREATE TABLE managed_notes (
                path TEXT PRIMARY KEY,base_hash TEXT NOT NULL,rendered_revision TEXT NOT NULL,
                regions TEXT NOT NULL,metadata TEXT NOT NULL
            );

CREATE TABLE material_origins(doc_id TEXT PRIMARY KEY,state TEXT NOT NULL);

CREATE TABLE operation_receipts(
                id TEXT PRIMARY KEY,job_id TEXT,item_doc TEXT,doc_id TEXT NOT NULL,
                path TEXT NOT NULL,input_revision TEXT,output_revision TEXT,
                operation TEXT NOT NULL,reason TEXT NOT NULL,change_id TEXT,
                created_at TEXT NOT NULL,details TEXT NOT NULL);

CREATE TABLE uid_aliases(uid TEXT PRIMARY KEY,doc_id TEXT NOT NULL);

CREATE TABLE watch_directories(path TEXT PRIMARY KEY);

CREATE TABLE wiki_schema(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);

CREATE INDEX filesystem_inbox_pending ON filesystem_inbox(seq) WHERE status='pending';

CREATE INDEX job_publication_source ON job_publications(job_id,source_doc);

CREATE INDEX receipt_job_item ON operation_receipts(job_id,item_doc);

CREATE INDEX uid_alias_document ON uid_aliases(doc_id);

CREATE INDEX wiki_dirty_documents ON documents(path) WHERE signature='';

CREATE TRIGGER wiki_management_delete AFTER DELETE ON managed_notes
                    BEGIN UPDATE documents SET signature='' WHERE path=old.path; END;

CREATE TRIGGER wiki_management_insert AFTER INSERT ON managed_notes
                    BEGIN UPDATE documents SET signature='' WHERE path=new.path; END;

CREATE TRIGGER wiki_management_update AFTER UPDATE ON managed_notes
                    BEGIN UPDATE documents SET signature='' WHERE path=new.path; END;

CREATE TRIGGER wiki_origin_delete AFTER DELETE ON material_origins
                    BEGIN UPDATE documents SET signature='' WHERE id=old.doc_id; END;

CREATE TRIGGER wiki_origin_insert AFTER INSERT ON material_origins
                    BEGIN UPDATE documents SET signature='' WHERE id=new.doc_id; END;

CREATE TRIGGER wiki_origin_update AFTER UPDATE ON material_origins
                    BEGIN UPDATE documents SET signature='' WHERE id=new.doc_id; END;
"""


def create(db, *, with_jobs: bool = True) -> None:
    """Create a private empty format; no files or retained rows are consulted."""
    from . import (
        catalog,
        grants,
        job_controls,
        job_events,
        maintenance_inputs,
        maintenance_selections,
        materials,
        protocols,
        recovery_state,
        search_index,
        source_bindings,
    )
    from .jobs import WikiJobs

    if not db.in_transaction:
        raise ValueError("Wiki layout creation requires the preparation transaction")
    for statement in statements(_CORE_SCHEMA):
        db.execute(statement)
    db.execute("ALTER TABLE operation_receipts ADD COLUMN " + ITEM_MATCH_COLUMN)
    db.execute("INSERT INTO index_state VALUES(1,0,NULL,NULL)")
    protocols.initialize(db)
    db.execute("INSERT INTO wiki_protocols VALUES('registration_config',1)")
    db.execute("INSERT INTO wiki_protocols VALUES('maintenance_inputs',1)")
    source_bindings.initialize(db)
    search_index.initialize(db)
    materials.initialize(db)
    catalog.initialize(db)
    maintenance_selections.initialize(db)
    grants.initialize(db)
    maintenance_inputs.initialize(db)
    job_events.initialize(db)
    recovery_state.initialize(db)
    job_controls.initialize(db)
    if with_jobs:
        WikiJobs.initialize_ledger(db)

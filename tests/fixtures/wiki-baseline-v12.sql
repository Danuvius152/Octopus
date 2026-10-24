-- Frozen Wiki format 12 from 0a634c8; private empty producer, no user records.

CREATE TABLE app_trash(trash_id TEXT PRIMARY KEY,path TEXT NOT NULL,document_id TEXT,management TEXT, origin_state TEXT, source_plan TEXT);

CREATE TABLE catalog_dirty(source_doc TEXT PRIMARY KEY);

CREATE TABLE catalog_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL,pending INTEGER NOT NULL);

CREATE TABLE change_management(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT);

CREATE TABLE change_material_origins(change_id TEXT PRIMARY KEY,before_state TEXT,after_state TEXT);

CREATE TABLE change_proposals(change_id TEXT PRIMARY KEY,operation TEXT NOT NULL);

CREATE TABLE change_retention (
                change_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
                before_available INTEGER NOT NULL,after_available INTEGER NOT NULL,resolved_at TEXT
            );

CREATE TABLE change_source_bindings(
        change_id TEXT PRIMARY KEY,before_plan TEXT NOT NULL,after_plan TEXT NOT NULL);

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

CREATE TABLE filesystem_recoveries(
        id TEXT PRIMARY KEY NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),
        vault_id TEXT NOT NULL,root TEXT NOT NULL,registration TEXT NOT NULL,
        observer_epoch TEXT NOT NULL,root_generation TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('prepared','mapping_applied','rebuilding','handoff','complete')),
        uncertainty_generation INTEGER NOT NULL DEFAULT 0,
        cutoff INTEGER NOT NULL,plan_hash TEXT,plan TEXT CHECK(plan IS NULL OR json_valid(plan)),
        approved_hash TEXT,evidence TEXT NOT NULL CHECK(json_valid(evidence)),ledger_hash TEXT,
        rebuild_cursor INTEGER NOT NULL DEFAULT 0,mapping_receipt TEXT,completion_receipt TEXT,
        updated_at TEXT NOT NULL);

CREATE TABLE filesystem_recovery_audit(
        id TEXT PRIMARY KEY NOT NULL,recovery_id TEXT NOT NULL,generation INTEGER NOT NULL,
        kind TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL);

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

CREATE TABLE job_selection_evidence(
        job_id TEXT NOT NULL,selection_id TEXT NOT NULL,selection_ordinal INTEGER NOT NULL,
        doc_id TEXT NOT NULL,review_key TEXT NOT NULL,accepted_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_id,selection_ordinal));

CREATE TABLE maintenance_accepted_paths(
        doc_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(doc_id,path));

CREATE TABLE maintenance_dirty(
        doc_id TEXT PRIMARY KEY,cause_generation INTEGER NOT NULL);

CREATE TABLE maintenance_dirty_paths(
        doc_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(doc_id,path));

CREATE TABLE maintenance_input_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL,
        rebuild_nonce TEXT NOT NULL,generation INTEGER NOT NULL);

CREATE TABLE maintenance_job_dirty(
        job_id TEXT PRIMARY KEY,cause_generation INTEGER NOT NULL);

CREATE TABLE maintenance_job_items(
        job_id TEXT NOT NULL,ordinal INTEGER NOT NULL,item_doc TEXT,status TEXT,
        index_complete INTEGER NOT NULL,recording_failure INTEGER NOT NULL,
        index_recovery INTEGER NOT NULL,output_path TEXT,output_revision TEXT,
        output_revision_valid INTEGER NOT NULL,receipt_id TEXT,receipt_required INTEGER NOT NULL,
        output_change_id TEXT,PRIMARY KEY(job_id,ordinal));

CREATE TABLE maintenance_job_members(
        job_id TEXT NOT NULL,doc_id TEXT NOT NULL,
        PRIMARY KEY(job_id,doc_id));

CREATE TABLE maintenance_projection(
        doc_id TEXT PRIMARY KEY,version TEXT NOT NULL,input_generation INTEGER NOT NULL,
        path TEXT NOT NULL,candidate INTEGER NOT NULL,status TEXT NOT NULL,summary_json TEXT NOT NULL);

CREATE TABLE maintenance_selection_items(
        selection_id TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal>=0),doc_id TEXT NOT NULL,
        path TEXT NOT NULL,revision TEXT NOT NULL,review_json TEXT NOT NULL CHECK(json_valid(review_json)),
        PRIMARY KEY(selection_id,ordinal),UNIQUE(selection_id,doc_id),
        CHECK(json_type(review_json,'$.doc') IS 'text' AND json_extract(review_json,'$.doc')=doc_id),
        CHECK(json_type(review_json,'$.path') IS 'text' AND json_extract(review_json,'$.path')=path),
        CHECK(json_type(review_json,'$.revision') IS 'text' AND json_extract(review_json,'$.revision')=revision),
        CHECK(json_type(review_json,'$.review_key') IS 'text'));

CREATE TABLE maintenance_selections(
        id TEXT PRIMARY KEY NOT NULL,owner_ref TEXT NOT NULL,vault_id TEXT NOT NULL,
        registration_generation TEXT NOT NULL,actor TEXT NOT NULL,purpose TEXT NOT NULL,
        filters TEXT NOT NULL CHECK(json_valid(filters)),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),version INTEGER NOT NULL CHECK(version=1));

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

CREATE TABLE raw_materials (
      doc_id TEXT PRIMARY KEY,path TEXT NOT NULL,revision TEXT NOT NULL,
      original_hash TEXT NOT NULL,ownership TEXT NOT NULL
    );

CREATE TABLE search_documents (
      doc_id TEXT PRIMARY KEY,revision TEXT NOT NULL,path TEXT NOT NULL,
      title TEXT NOT NULL,aliases TEXT NOT NULL,version TEXT NOT NULL
    );

CREATE VIRTUAL TABLE search_fts USING fts5(
      title,path,aliases,tags,key_note,heading,body,tokenize='unicode61'
    );

CREATE TABLE search_sections (
      id INTEGER PRIMARY KEY,doc_id TEXT NOT NULL,title TEXT NOT NULL,
      start INTEGER NOT NULL,end INTEGER NOT NULL,line_start INTEGER NOT NULL,
      line_end INTEGER NOT NULL,section_role TEXT NOT NULL DEFAULT 'body',
      readable_body INTEGER NOT NULL DEFAULT 0
    );

CREATE TABLE source_binding_head(
        source_doc TEXT PRIMARY KEY,observation_id TEXT,pending_plan TEXT);

CREATE TABLE source_binding_observations(
        id TEXT PRIMARY KEY,source_doc TEXT NOT NULL,revision TEXT,context TEXT NOT NULL,
        path TEXT NOT NULL,occurrences TEXT NOT NULL,UNIQUE(source_doc,context));

CREATE TABLE source_bindings(
        id TEXT PRIMARY KEY,source_doc TEXT NOT NULL,selector TEXT NOT NULL,
        reference TEXT NOT NULL,base TEXT NOT NULL,policy TEXT NOT NULL
        CHECK(policy IN ('bound','unresolved','unknown','external')),
        target_doc TEXT,proof TEXT NOT NULL,
        CHECK((policy='bound')=(target_doc IS NOT NULL)));

CREATE TABLE uid_aliases(uid TEXT PRIMARY KEY,doc_id TEXT NOT NULL);

CREATE TABLE watch_directories(path TEXT PRIMARY KEY);

CREATE TABLE wiki_edge_lookups(
        source_doc TEXT NOT NULL,ordinal INTEGER NOT NULL,namespace TEXT NOT NULL,key TEXT NOT NULL,
        PRIMARY KEY(source_doc,ordinal,namespace,key));

CREATE TABLE wiki_edges(
        source_doc TEXT NOT NULL, ordinal INTEGER NOT NULL, target_doc TEXT,
        resource TEXT NOT NULL, relation TEXT NOT NULL, location TEXT NOT NULL,
        recorded_revision TEXT, label TEXT, lookup_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unresolved',
        PRIMARY KEY(source_doc,ordinal));

CREATE TABLE wiki_grant_documents(
        job_id TEXT NOT NULL,doc_id TEXT NOT NULL,path_at_admission TEXT NOT NULL,
        observed_revision TEXT NOT NULL,origin TEXT NOT NULL CHECK(origin IN ('selected','material','created')),
        reservation_id TEXT,PRIMARY KEY(job_id,doc_id));

CREATE TABLE wiki_grant_readmissions(
        job_id TEXT NOT NULL,registration_generation TEXT NOT NULL,request_hash TEXT NOT NULL,
        request TEXT NOT NULL,previous_error TEXT,created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,registration_generation));

CREATE TABLE wiki_job_controls(
    job_id TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
    PRIMARY KEY(job_id,operation_id));

CREATE TABLE wiki_job_deliveries(
        event_id TEXT NOT NULL,subscriber_id TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
        outcome TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(event_id,subscriber_id));

CREATE TABLE wiki_job_grants(
        job_id TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL CHECK(version=2),
        vault_id TEXT NOT NULL,registration_generation TEXT NOT NULL,
        creation_purpose TEXT CHECK(creation_purpose IS NULL OR creation_purpose='requested_knowledge_page'),
        policy TEXT NOT NULL,created_at TEXT NOT NULL);

CREATE TABLE wiki_job_outbox(
        event_id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,state_version INTEGER NOT NULL,
        body TEXT NOT NULL CHECK(json_valid(body)),created_at TEXT NOT NULL,
        UNIQUE(job_id,state_version));

CREATE TABLE wiki_job_state(
        job_id TEXT PRIMARY KEY NOT NULL,state_version INTEGER NOT NULL CHECK(state_version>0),
        attempt INTEGER NOT NULL CHECK(attempt>=0),settlement_phase TEXT NOT NULL
        CHECK(settlement_phase IN ('none','pending','confirmed','attention')));

CREATE TABLE wiki_job_subscribers(
        id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,owner TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('chat','app','schedule','human')),
        target TEXT NOT NULL,request_id TEXT NOT NULL,client_id TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(job_id,owner,kind,target,client_id));

CREATE TABLE wiki_lookup_names(
        namespace TEXT NOT NULL,key TEXT NOT NULL,doc_id TEXT NOT NULL,
        PRIMARY KEY(namespace,key,doc_id));

CREATE TABLE wiki_path_reservations(
        id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,path TEXT NOT NULL,path_key TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose IN ('framework_material','requested_knowledge_page')),
        state TEXT NOT NULL CHECK(state IN ('reserved','prepared','blocked','bound','released')),
        document_id TEXT,change_id TEXT,output_revision TEXT,
        UNIQUE(job_id,path),UNIQUE(change_id),
        CHECK((state IN ('reserved','released') AND document_id IS NULL AND change_id IS NULL AND output_revision IS NULL)
          OR (state IN ('prepared','blocked','bound') AND document_id IS NOT NULL AND change_id IS NOT NULL AND output_revision IS NOT NULL)));

CREATE TABLE wiki_protocols(name TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL);

CREATE TABLE wiki_schema(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);

CREATE TABLE wiki_uid_claims(
        doc_id TEXT NOT NULL,kind TEXT NOT NULL,uid TEXT NOT NULL,original TEXT,
        PRIMARY KEY(doc_id,kind));

CREATE TABLE wiki_uid_dirty(uid TEXT PRIMARY KEY);

INSERT INTO "catalog_state" VALUES(1,'direct-links-v4',1);

INSERT INTO "index_state" VALUES(1,0,NULL,NULL);

INSERT INTO "maintenance_input_state" VALUES(1,'maintenance-inputs-v8','c85c14e711514b848aaaae99673820f1',1);

INSERT INTO "wiki_protocols" VALUES('registration',1);

INSERT INTO "wiki_protocols" VALUES('maintenance_inputs',1);

INSERT INTO "wiki_protocols" VALUES('maintenance_selections',1);

INSERT INTO "wiki_protocols" VALUES('wiki_grants',2);

INSERT INTO "wiki_protocols" VALUES('wiki_job_events',2);

INSERT INTO "wiki_protocols" VALUES('filesystem_recovery',1);

INSERT INTO "wiki_protocols" VALUES('registration_config',1);

INSERT INTO "wiki_schema" VALUES(1,'2026-10-24T03:46:45.077135+00:00');

INSERT INTO "wiki_schema" VALUES(2,'2026-10-24T03:46:45.077190+00:00');

INSERT INTO "wiki_schema" VALUES(3,'2026-10-24T03:46:45.208231+00:00');

INSERT INTO "wiki_schema" VALUES(4,'2026-10-24T03:46:45.208304+00:00');

INSERT INTO "wiki_schema" VALUES(5,'2026-10-24T03:46:45.208402+00:00');

INSERT INTO "wiki_schema" VALUES(6,'2026-10-24T03:46:45.208485+00:00');

INSERT INTO "wiki_schema" VALUES(7,'2026-10-24T03:46:45.208526+00:00');

INSERT INTO "wiki_schema" VALUES(8,'2026-10-24T03:46:45.208853+00:00');

INSERT INTO "wiki_schema" VALUES(9,'2026-10-24T03:46:45.209115+00:00');

INSERT INTO "wiki_schema" VALUES(10,'2026-10-24T03:46:45.211367+00:00');

INSERT INTO "wiki_schema" VALUES(11,'2026-10-24T03:46:45.217584+00:00');

INSERT INTO "wiki_schema" VALUES(12,'2026-10-24T03:46:45.217683+00:00');

CREATE INDEX filesystem_inbox_pending ON filesystem_inbox(seq) WHERE status='pending';

CREATE UNIQUE INDEX filesystem_recovery_active ON filesystem_recoveries((1)) WHERE phase<>'complete';

CREATE INDEX filesystem_recovery_audits ON filesystem_recovery_audit(recovery_id,generation,kind);

CREATE INDEX job_publication_source ON job_publications(job_id,source_doc);

CREATE INDEX maintenance_binding_target ON source_bindings(target_doc,source_doc);

CREATE INDEX maintenance_candidate_order ON maintenance_projection(candidate,path,doc_id);

CREATE INDEX maintenance_dependency_edges ON wiki_edges(source_doc,relation,ordinal);

CREATE INDEX maintenance_dirty_scope ON maintenance_dirty_paths(path,doc_id);

CREATE INDEX maintenance_document_errors ON documents(id) WHERE quality='error' OR json_extract(metadata, '$.frontmatter_error') IS NOT NULL;

CREATE INDEX maintenance_evidence_selection ON job_selection_evidence(selection_id,selection_ordinal);

CREATE INDEX maintenance_items_document ON maintenance_job_items(item_doc,job_id,ordinal);

CREATE INDEX maintenance_members_document ON maintenance_job_members(doc_id,job_id);

CREATE INDEX maintenance_path_order ON maintenance_projection(path,doc_id);

CREATE INDEX maintenance_publication_change ON job_publications(change_id,job_id);

CREATE INDEX maintenance_receipt_change ON operation_receipts(change_id,job_id);

CREATE INDEX maintenance_receipt_document ON operation_receipts(doc_id);

CREATE INDEX maintenance_retention_document ON change_retention(document_id,change_id);

CREATE INDEX maintenance_selection_expiration ON maintenance_selections(expires_at,id);

CREATE INDEX raw_material_hash ON raw_materials(original_hash);

CREATE INDEX receipt_job_item ON operation_receipts(job_id,item_doc);

CREATE INDEX search_sections_doc ON search_sections(doc_id);

CREATE INDEX source_binding_selector ON source_bindings(source_doc,selector);

CREATE INDEX uid_alias_document ON uid_aliases(doc_id);

CREATE INDEX wiki_dirty_documents ON documents(path) WHERE signature='';

CREATE INDEX wiki_edge_target ON wiki_edges(target_doc);

CREATE INDEX wiki_grant_admission_path ON wiki_grant_documents(job_id,path_at_admission,doc_id);

CREATE INDEX wiki_grant_document_identity ON wiki_grant_documents(doc_id,job_id);

CREATE INDEX wiki_job_delivery_pending ON wiki_job_deliveries(event_id,subscriber_id) WHERE acknowledged=0;

CREATE INDEX wiki_job_subscriber_target ON wiki_job_subscribers(owner,target,job_id);

CREATE INDEX wiki_lookup_document ON wiki_lookup_names(doc_id);

CREATE INDEX wiki_reservation_job ON wiki_path_reservations(job_id,purpose,path);

CREATE UNIQUE INDEX wiki_reservation_live_path ON wiki_path_reservations(path_key) WHERE state IN ('reserved','prepared','blocked');

CREATE INDEX wiki_reverse_lookup ON wiki_edge_lookups(namespace,key,source_doc);

CREATE INDEX wiki_uid_claimants ON wiki_uid_claims(uid,doc_id);

CREATE TRIGGER filesystem_recoveries_delete_guard BEFORE DELETE ON filesystem_recoveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
        SELECT RAISE(ABORT,'Filesystem recovery facts are retained'); END;

CREATE TRIGGER filesystem_recoveries_insert_guard BEFORE INSERT ON filesystem_recoveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM filesystem_recoveries WHERE id=new.id)
        THEN RAISE(ABORT,'Filesystem recovery facts cannot be replaced') END; END;

CREATE TRIGGER filesystem_recovery_audit_delete_guard BEFORE DELETE ON filesystem_recovery_audit BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
        SELECT RAISE(ABORT,'Filesystem recovery facts are retained'); END;

CREATE TRIGGER filesystem_recovery_audit_immutable
    BEFORE UPDATE ON filesystem_recovery_audit BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
    SELECT RAISE(ABORT,'Filesystem recovery audit is immutable'); END;

CREATE TRIGGER filesystem_recovery_audit_insert_guard BEFORE INSERT ON filesystem_recovery_audit BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM filesystem_recovery_audit WHERE id=new.id)
        THEN RAISE(ABORT,'Filesystem recovery facts cannot be replaced') END; END;

CREATE TRIGGER filesystem_recovery_update_guard
    BEFORE UPDATE ON filesystem_recoveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Filesystem recovery requires recursive triggers') END;
    SELECT CASE WHEN new.id<>old.id OR new.root<>old.root OR new.vault_id<>old.vault_id
      OR old.phase='complete'
      OR new.generation<old.generation OR new.generation>old.generation+1
      OR (new.generation>old.generation AND new.phase<>'prepared')
      OR (new.generation=old.generation AND (new.evidence<>old.evidence OR new.registration<>old.registration
        OR new.observer_epoch<>old.observer_epoch OR new.root_generation<>old.root_generation
        OR new.rebuild_cursor<old.rebuild_cursor
        OR new.uncertainty_generation<old.uncertainty_generation
        OR (old.plan IS NOT NULL AND (new.plan IS NOT old.plan OR new.plan_hash IS NOT old.plan_hash OR new.cutoff<>old.cutoff))
        OR (old.mapping_receipt IS NOT NULL AND new.mapping_receipt IS NOT old.mapping_receipt)
        OR (old.phase='prepared' AND new.phase NOT IN ('prepared','mapping_applied'))
        OR (old.phase='mapping_applied' AND new.phase NOT IN ('mapping_applied','rebuilding','handoff'))
        OR (old.phase='rebuilding' AND new.phase NOT IN ('rebuilding','handoff'))
        OR (old.phase='handoff' AND new.phase NOT IN ('handoff','complete'))))
      OR (new.phase<>'prepared' AND (new.plan_hash IS NULL OR new.approved_hash IS NOT new.plan_hash OR new.mapping_receipt IS NULL))
      OR (new.phase='complete' AND new.completion_receipt IS NULL)
      THEN RAISE(ABORT,'Invalid filesystem recovery transition') END; END;

CREATE TRIGGER maintenance_evidence_immutable BEFORE UPDATE ON job_selection_evidence
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence is immutable'); END;

CREATE TRIGGER maintenance_evidence_insert
        BEFORE INSERT ON job_selection_evidence BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END; END;

CREATE TRIGGER maintenance_evidence_keep BEFORE DELETE ON job_selection_evidence
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence retained'); END;

CREATE TRIGGER maintenance_evidence_owner BEFORE INSERT ON job_selection_evidence
        WHEN NOT EXISTS(SELECT 1 FROM maintenance_selection_items i JOIN maintenance_selections s ON s.id=i.selection_id
            WHERE i.selection_id=new.selection_id AND i.ordinal=new.selection_ordinal AND i.doc_id=new.doc_id
            AND json_extract(i.review_json,'$.review_key')=new.review_key)
        OR NOT EXISTS(SELECT 1 FROM wiki_protocols WHERE name='maintenance_jobs' AND version=1)
        BEGIN SELECT RAISE(ABORT,'Accepted evidence has no matching job and observation'); END;

CREATE TRIGGER maintenance_input_change_proposals_delete AFTER DELETE ON change_proposals BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_change_proposals_insert AFTER INSERT ON change_proposals BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_change_proposals_update AFTER UPDATE ON change_proposals BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_change_retention_delete AFTER DELETE ON change_retention BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.document_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.document_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.document_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_change_retention_insert AFTER INSERT ON change_retention BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.document_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.document_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.document_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_change_retention_update AFTER UPDATE ON change_retention BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.document_id UNION SELECT new.document_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.document_id UNION SELECT new.document_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.document_id UNION SELECT new.document_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_changes_delete AFTER DELETE ON changes BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_changes_insert AFTER INSERT ON changes BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_changes_update AFTER UPDATE ON changes BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id UNION SELECT new.id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id UNION SELECT new.id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.id UNION SELECT new.id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.id UNION SELECT new.id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_documents_delete AFTER DELETE ON documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_documents_insert AFTER INSERT ON documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT new.id AS doc_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT new.id AS doc_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT new.id AS doc_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_documents_update AFTER UPDATE ON documents WHEN (old.id IS NOT new.id OR old.path IS NOT new.path OR old.revision IS NOT new.revision OR old.title IS NOT new.title OR old.kind IS NOT new.kind OR old.text IS NOT new.text OR old.pages IS NOT new.pages OR old.quality IS NOT new.quality OR old.metadata IS NOT new.metadata) BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id UNION SELECT source_doc FROM wiki_edges WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id) UNION SELECT source_doc FROM source_bindings WHERE target_doc IN (SELECT old.id AS doc_id UNION SELECT new.id AS doc_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_index_state_delete AFTER DELETE ON index_state BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_index_state_insert AFTER INSERT ON index_state BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_index_state_update AFTER UPDATE ON index_state WHEN (old.epoch IS NOT new.epoch OR old.error IS NOT new.error) BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_job_publications_delete AFTER DELETE ON job_publications BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.source_doc) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.source_doc) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id) UNION SELECT old.source_doc) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_job_publications_insert AFTER INSERT ON job_publications BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.source_doc) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.source_doc) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT new.change_id) UNION SELECT new.source_doc) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_job_publications_update AFTER UPDATE ON job_publications BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.source_doc UNION SELECT new.source_doc) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.source_doc UNION SELECT new.source_doc) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT document_id AS doc_id FROM change_retention WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT doc_id FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT item_doc FROM operation_receipts WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT source_doc FROM job_publications WHERE change_id IN (SELECT old.change_id UNION SELECT new.change_id) UNION SELECT old.source_doc UNION SELECT new.source_doc) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_operation_receipts_delete AFTER DELETE ON operation_receipts BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_operation_receipts_insert AFTER INSERT ON operation_receipts BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_operation_receipts_update AFTER UPDATE ON operation_receipts BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id UNION SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id UNION SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.doc_id AS doc_id UNION SELECT old.item_doc AS doc_id UNION SELECT new.doc_id AS doc_id UNION SELECT new.item_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_head_delete AFTER DELETE ON source_binding_head BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_head_insert AFTER INSERT ON source_binding_head BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_head_update AFTER UPDATE ON source_binding_head BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_observations_delete AFTER DELETE ON source_binding_observations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_observations_insert AFTER INSERT ON source_binding_observations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_binding_observations_update AFTER UPDATE ON source_binding_observations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_bindings_delete AFTER DELETE ON source_bindings BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_bindings_insert AFTER INSERT ON source_bindings BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_source_bindings_update AFTER UPDATE ON source_bindings BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_edges_delete AFTER DELETE ON wiki_edges BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_edges_insert AFTER INSERT ON wiki_edges BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_edges_update AFTER UPDATE ON wiki_edges BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.source_doc AS doc_id UNION SELECT new.source_doc AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_documents_delete AFTER DELETE ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.doc_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.doc_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.doc_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_documents_insert AFTER INSERT ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.doc_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.doc_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.doc_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_documents_update AFTER UPDATE ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.doc_id AS doc_id UNION SELECT new.doc_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.doc_id AS doc_id UNION SELECT new.doc_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.doc_id AS doc_id UNION SELECT new.doc_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_readmissions_delete AFTER DELETE ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_readmissions_insert AFTER INSERT ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_grant_readmissions_update AFTER UPDATE ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_job_grants_delete AFTER DELETE ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_job_grants_insert AFTER INSERT ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT new.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_job_grants_update AFTER UPDATE ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT doc_id FROM maintenance_job_members WHERE job_id IN (SELECT old.job_id UNION SELECT new.job_id)) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_lookup_names_delete AFTER DELETE ON wiki_lookup_names BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_wiki_lookup_names_insert AFTER INSERT ON wiki_lookup_names BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_wiki_lookup_names_update AFTER UPDATE ON wiki_lookup_names BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1; END;

CREATE TRIGGER maintenance_input_wiki_path_reservations_delete AFTER DELETE ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.document_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.document_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.document_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_path_reservations_insert AFTER INSERT ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT new.document_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT new.document_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT new.document_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_input_wiki_path_reservations_update AFTER UPDATE ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
        THEN RAISE(ABORT,'maintenance inputs require recursive triggers') END;UPDATE maintenance_input_state SET generation=generation+1 WHERE singleton=1;INSERT INTO maintenance_dirty(doc_id,cause_generation)
        SELECT affected.doc_id,s.generation FROM (SELECT old.document_id AS doc_id UNION SELECT new.document_id AS doc_id) affected
        CROSS JOIN maintenance_input_state s WHERE affected.doc_id IS NOT NULL
        ON CONFLICT(doc_id) DO UPDATE SET cause_generation=excluded.cause_generation;
        INSERT INTO maintenance_dirty_paths
            SELECT d.id,d.path FROM (SELECT old.document_id AS doc_id UNION SELECT new.document_id AS doc_id) affected JOIN documents d ON d.id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
        INSERT INTO maintenance_dirty_paths
            SELECT p.doc_id,p.path FROM (SELECT old.document_id AS doc_id UNION SELECT new.document_id AS doc_id) affected JOIN maintenance_projection p ON p.doc_id=affected.doc_id WHERE true
            ON CONFLICT(doc_id,path) DO NOTHING;
         END;

CREATE TRIGGER maintenance_selection_immutable
        BEFORE UPDATE ON maintenance_selections BEGIN SELECT RAISE(ABORT,'Selection observations are immutable'); END;

CREATE TRIGGER maintenance_selection_insert
        BEFORE INSERT ON maintenance_selections BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM maintenance_selections WHERE id=new.id)
            THEN RAISE(ABORT,'Selection observations are immutable') END; END;

CREATE TRIGGER maintenance_selection_item_immutable
        BEFORE UPDATE ON maintenance_selection_items BEGIN SELECT RAISE(ABORT,'Selection items are immutable'); END;

CREATE TRIGGER maintenance_selection_item_insert
        BEFORE INSERT ON maintenance_selection_items BEGIN
        SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
            THEN RAISE(ABORT,'Selection writers require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM maintenance_selection_items WHERE selection_id=new.selection_id
            AND (ordinal=new.ordinal OR doc_id=new.doc_id))
            THEN RAISE(ABORT,'Selection items are immutable') END; END;

CREATE TRIGGER maintenance_selection_item_keep BEFORE DELETE ON maintenance_selection_items
        WHEN EXISTS(SELECT 1 FROM job_selection_evidence WHERE selection_id=old.selection_id AND selection_ordinal=old.ordinal)
        BEGIN SELECT RAISE(ABORT,'Accepted selection item retained'); END;

CREATE TRIGGER maintenance_selection_item_owner BEFORE INSERT ON maintenance_selection_items
        WHEN NOT EXISTS(SELECT 1 FROM maintenance_selections WHERE id=new.selection_id AND new.ordinal<candidate_count)
        BEGIN SELECT RAISE(ABORT,'Selection item has no matching header'); END;

CREATE TRIGGER maintenance_selection_keep BEFORE DELETE ON maintenance_selections
        WHEN EXISTS(SELECT 1 FROM job_selection_evidence WHERE selection_id=old.id)
        BEGIN SELECT RAISE(ABORT,'Accepted selection evidence retained'); END;

CREATE TRIGGER raw_material_delete
      AFTER DELETE ON documents BEGIN
      DELETE FROM raw_materials WHERE doc_id=old.id; END;

CREATE TRIGGER search_document_delete
      AFTER DELETE ON documents BEGIN
      DELETE FROM search_sections WHERE doc_id=old.id;
      DELETE FROM search_documents WHERE doc_id=old.id; END;

CREATE TRIGGER search_section_delete
      AFTER DELETE ON search_sections BEGIN
      DELETE FROM search_fts WHERE rowid=old.id; END;

CREATE TRIGGER source_binding_immutable BEFORE UPDATE ON source_bindings
        WHEN old.policy IN ('bound','unknown','external') OR new.id<>old.id
          OR new.source_doc<>old.source_doc OR new.selector<>old.selector
          OR new.reference<>old.reference OR new.base<>old.base
        BEGIN SELECT RAISE(ABORT,'source identity proof is immutable'); END;

CREATE TRIGGER source_observation_no_delete
            BEFORE DELETE ON source_binding_observations
            BEGIN SELECT RAISE(ABORT,'source observation is immutable'); END;

CREATE TRIGGER source_observation_no_update
            BEFORE UPDATE ON source_binding_observations
            BEGIN SELECT RAISE(ABORT,'source observation is immutable'); END;

CREATE TRIGGER wiki_edge_delete AFTER DELETE ON documents
        BEGIN DELETE FROM wiki_edges WHERE source_doc=old.id; END;

CREATE TRIGGER wiki_grant_documents_delete BEFORE DELETE ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_grant_documents_insert BEFORE INSERT ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_grant_documents WHERE job_id=new.job_id AND doc_id=new.doc_id)
        THEN RAISE(ABORT,'Wiki grant facts cannot be replaced') END; SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant owner is missing') END;SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM documents WHERE id=new.doc_id)
          OR (new.origin='selected' AND new.reservation_id IS NOT NULL)
          OR (new.origin='created' AND new.reservation_id IS NULL)
          OR (new.reservation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM wiki_path_reservations r
            WHERE r.id=new.reservation_id AND r.job_id=new.job_id AND r.state='bound'
              AND r.document_id=new.doc_id AND r.path=new.path_at_admission AND r.output_revision=new.observed_revision
              AND r.purpose=CASE new.origin WHEN 'material' THEN 'framework_material' ELSE 'requested_knowledge_page' END))
          THEN RAISE(ABORT,'Wiki grant document requires its original identity proof') END; END;

CREATE TRIGGER wiki_grant_documents_update BEFORE UPDATE ON wiki_grant_documents BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_grant_readmissions_delete BEFORE DELETE ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_grant_readmissions_insert BEFORE INSERT ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_grant_readmissions WHERE job_id=new.job_id AND registration_generation=new.registration_generation)
        THEN RAISE(ABORT,'Wiki grant facts cannot be replaced') END; SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant owner is missing') END; END;

CREATE TRIGGER wiki_grant_readmissions_update BEFORE UPDATE ON wiki_grant_readmissions BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_job_deliveries_delete_guard BEFORE DELETE ON wiki_job_deliveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are retained'); END;

CREATE TRIGGER wiki_job_deliveries_insert_guard BEFORE INSERT ON wiki_job_deliveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_job_deliveries WHERE event_id=new.event_id AND subscriber_id=new.subscriber_id)
        THEN RAISE(ABORT,'Wiki job event facts cannot be replaced') END; END;

CREATE TRIGGER wiki_job_delivery_owner
    BEFORE INSERT ON wiki_job_deliveries WHEN NOT EXISTS(
      SELECT 1 FROM wiki_job_outbox o JOIN wiki_job_subscribers s ON s.job_id=o.job_id
      WHERE o.event_id=new.event_id AND s.id=new.subscriber_id)
    BEGIN SELECT RAISE(ABORT,'Wiki delivery requires its original subscriber'); END;

CREATE TRIGGER wiki_job_delivery_update_guard
    BEFORE UPDATE ON wiki_job_deliveries BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
    SELECT CASE WHEN new.event_id<>old.event_id OR new.subscriber_id<>old.subscriber_id
      OR old.acknowledged=1 OR new.acknowledged<>1
      THEN RAISE(ABORT,'Wiki delivery acknowledgement cannot be replaced') END; END;

CREATE TRIGGER wiki_job_grants_delete BEFORE DELETE ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_job_grants_insert BEFORE INSERT ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant facts cannot be replaced') END;  END;

CREATE TRIGGER wiki_job_grants_update BEFORE UPDATE ON wiki_job_grants BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_job_outbox_delete_guard BEFORE DELETE ON wiki_job_outbox BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are retained'); END;

CREATE TRIGGER wiki_job_outbox_deliver
    AFTER INSERT ON wiki_job_outbox BEGIN
    INSERT INTO wiki_job_deliveries(event_id,subscriber_id)
      SELECT new.event_id,id FROM wiki_job_subscribers WHERE job_id=new.job_id; END;

CREATE TRIGGER wiki_job_outbox_insert_guard BEFORE INSERT ON wiki_job_outbox BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_job_outbox WHERE event_id=new.event_id)
        THEN RAISE(ABORT,'Wiki job event facts cannot be replaced') END; END;

CREATE TRIGGER wiki_job_outbox_update_guard BEFORE UPDATE ON wiki_job_outbox BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are immutable'); END;

CREATE TRIGGER wiki_job_state_delete_guard BEFORE DELETE ON wiki_job_state BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are retained'); END;

CREATE TRIGGER wiki_job_state_insert_guard BEFORE INSERT ON wiki_job_state BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_job_state WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki job event facts cannot be replaced') END; END;

CREATE TRIGGER wiki_job_state_update_guard
    BEFORE UPDATE ON wiki_job_state BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
    SELECT CASE WHEN new.job_id<>old.job_id OR new.state_version<>old.state_version+1
      OR new.attempt<old.attempt OR new.attempt>old.attempt+1
      THEN RAISE(ABORT,'Invalid Wiki job state transition') END; END;

CREATE TRIGGER wiki_job_subscriber_current
    AFTER INSERT ON wiki_job_subscribers BEGIN
    INSERT INTO wiki_job_deliveries(event_id,subscriber_id)
      SELECT o.event_id,new.id FROM wiki_job_outbox o JOIN wiki_job_state s
        ON s.job_id=o.job_id AND s.state_version=o.state_version WHERE o.job_id=new.job_id; END;

CREATE TRIGGER wiki_job_subscribers_delete_guard BEFORE DELETE ON wiki_job_subscribers BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are retained'); END;

CREATE TRIGGER wiki_job_subscribers_insert_guard BEFORE INSERT ON wiki_job_subscribers BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_job_subscribers WHERE id=new.id)
        THEN RAISE(ABORT,'Wiki job event facts cannot be replaced') END; END;

CREATE TRIGGER wiki_job_subscribers_update_guard BEFORE UPDATE ON wiki_job_subscribers BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki job events require recursive triggers') END;
        SELECT RAISE(ABORT,'Wiki job event facts are immutable'); END;

CREATE TRIGGER wiki_lookup_delete AFTER DELETE ON documents
        BEGIN DELETE FROM wiki_lookup_names WHERE doc_id=old.id; END;

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

CREATE TRIGGER wiki_path_reservations_delete BEFORE DELETE ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
            SELECT RAISE(ABORT,'Wiki grant facts are immutable'); END;

CREATE TRIGGER wiki_path_reservations_insert BEFORE INSERT ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM wiki_path_reservations WHERE id=new.id)
        THEN RAISE(ABORT,'Wiki grant facts cannot be replaced') END; SELECT CASE WHEN NOT EXISTS(
        SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id)
        THEN RAISE(ABORT,'Wiki grant owner is missing') END;SELECT CASE WHEN new.state<>'reserved' OR (new.purpose='requested_knowledge_page'
          AND NOT EXISTS(SELECT 1 FROM wiki_job_grants WHERE job_id=new.job_id AND creation_purpose='requested_knowledge_page'))
          THEN RAISE(ABORT,'Wiki reservation requires its admitted creation purpose') END; END;

CREATE TRIGGER wiki_reference_delete BEFORE DELETE ON documents
        BEGIN
            INSERT OR IGNORE INTO catalog_dirty SELECT source_doc FROM wiki_edges WHERE target_doc=old.id;
            INSERT OR IGNORE INTO catalog_dirty
                SELECT l.source_doc FROM wiki_edge_lookups l JOIN wiki_lookup_names n
                ON l.namespace=n.namespace AND l.key=n.key WHERE n.doc_id=old.id;
            INSERT OR IGNORE INTO wiki_uid_dirty SELECT uid FROM wiki_uid_claims WHERE doc_id=old.id;
            DELETE FROM wiki_uid_claims WHERE doc_id=old.id;
            DELETE FROM wiki_edge_lookups WHERE source_doc=old.id;
            UPDATE catalog_state SET pending=1 WHERE singleton=1;
        END;

CREATE TRIGGER wiki_reservation_transition
    BEFORE UPDATE ON wiki_path_reservations BEGIN SELECT CASE WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
    THEN RAISE(ABORT,'Wiki grants require recursive triggers') END;
    SELECT CASE WHEN new.id<>old.id OR new.job_id<>old.job_id OR new.path<>old.path
      OR new.path_key<>old.path_key OR new.purpose<>old.purpose
      OR (old.document_id IS NOT NULL AND (new.document_id IS NOT old.document_id
        OR new.change_id IS NOT old.change_id OR new.output_revision IS NOT old.output_revision))
      OR NOT ((old.state='reserved' AND new.state IN ('prepared','released'))
        OR (old.state='released' AND new.state='reserved')
        OR (old.state='prepared' AND new.state IN ('bound','blocked'))
        OR (old.state='blocked' AND new.state='bound'))
      THEN RAISE(ABORT,'Invalid Wiki reservation transition') END;
    SELECT CASE WHEN new.state='prepared' AND NOT EXISTS(SELECT 1 FROM changes c
      JOIN change_retention t ON t.change_id=c.id WHERE c.id=new.change_id AND c.path=new.path
        AND c.before_hash IS NULL AND c.after_hash=new.output_revision AND c.status='prepared'
        AND t.document_id=new.document_id)
      THEN RAISE(ABORT,'Wiki reservation requires its actual prepared change') END; END;

"""Remove only verified empty future structures when constructing old schemas."""


def remove_future_job_events(db):
    from server.wiki import job_events, recovery_state

    job_events.require(db)
    recovery_state.require(db)
    assert db.execute("SELECT count(*) FROM wiki_job_controls").fetchone()[0] == 0
    db.execute("DROP TABLE wiki_job_controls")
    for table in recovery_state.TABLES:
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    for trigger in recovery_state.TRIGGERS:
        db.execute("DROP TRIGGER " + trigger)
    for table in reversed(recovery_state.TABLES):
        db.execute("DROP TABLE " + table)
    db.execute("DELETE FROM wiki_protocols WHERE name='filesystem_recovery'")
    for table in job_events.TABLES:
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    for trigger in job_events.TRIGGERS | job_events.job_triggers():
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone():
            db.execute("DROP TRIGGER " + trigger)
    for table in reversed(job_events.TABLES):
        db.execute("DROP TABLE " + table)
    db.execute("DELETE FROM wiki_protocols WHERE name='wiki_job_events'")
    db.execute("DELETE FROM wiki_schema WHERE version>=11")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
        assert all(row[0] == '{}' for row in db.execute("SELECT attempt_errors FROM jobs"))
        db.execute("ALTER TABLE jobs DROP COLUMN attempt_errors")


def remove_future_selections(db):
    from server.wiki import grants, maintenance_selections

    # Reject a damaged current fixture and any accepted/observed state rather
    # than silently discarding durable evidence to make a migration test pass.
    remove_future_job_events(db)
    maintenance_selections.require(db)
    grants.require(db)
    for table in grants.TABLES:
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    # Derived triggers may reference these future tables. They are fixture
    # state, not old-version facts, and cannot survive this deliberate downgrade.
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'maintenance_input_%'").fetchall():
        db.execute('DROP TRIGGER "' + row[0] + '"')
    for trigger in grants.TRIGGERS | grants.JOB_TRIGGER:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone():
            db.execute("DROP TRIGGER " + trigger)
    for table in reversed(grants.TABLES):
        db.execute("DROP TABLE " + table)
    for table in maintenance_selections.TABLES:
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    for trigger in maintenance_selections.TRIGGERS | maintenance_selections.JOB_TRIGGER:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone():
            db.execute("DROP TRIGGER " + trigger)
    for table in reversed(maintenance_selections.TABLES):
        db.execute("DROP TABLE " + table)

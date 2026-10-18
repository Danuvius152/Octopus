"""Remove only verified empty future structures when constructing old schemas."""


def remove_future_selections(db):
    from server.wiki import grants, maintenance_selections

    # Reject a damaged current fixture and any accepted/observed state rather
    # than silently discarding durable evidence to make a migration test pass.
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

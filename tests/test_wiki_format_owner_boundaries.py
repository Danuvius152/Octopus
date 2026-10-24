"""Wiki format admission includes current owner layouts and durable references."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from server.wiki import maintenance_inputs, protocols
from server.wiki.store import WikiError, WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account


@pytest.mark.parametrize("history", ["baseline12", "only13"])
def test_jobs_layout_validation_does_not_depend_on_historical_step_rows(tmp_path, history):
    with closing(sqlite3.connect(tmp_path / "jobs.sqlite3")) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA recursive_triggers=ON")
        database.executescript((Path(__file__).parent / "fixtures/wiki-baseline-v12-with-jobs.sql").read_text())
        if history == "only13":
            database.execute("DELETE FROM wiki_schema")
            database.execute("INSERT INTO wiki_schema VALUES(13,'new-format-creation')")
        maintenance_inputs.validate_jobs(database)
        assert "attempt_errors" in {row[1] for row in database.execute("PRAGMA table_info(jobs)")}
        database.commit()


async def test_reordered_receipt_columns_are_refused_before_positional_writers_can_enter(registered_account):
    _, state, _, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT count(*) FROM operation_receipts").fetchone() == (0,)
        original = database.execute("SELECT sql FROM sqlite_schema WHERE name='operation_receipts'").fetchone()[0]
        changed = original.replace("reason TEXT NOT NULL", "retained_swap TEXT NOT NULL").replace(
            "created_at TEXT NOT NULL", "reason TEXT NOT NULL").replace("retained_swap TEXT NOT NULL", "created_at TEXT NOT NULL")
        assert changed != original
        dependents = [row[0] for row in database.execute(
            "SELECT sql FROM sqlite_schema WHERE tbl_name='operation_receipts' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        database.execute("DROP TABLE operation_receipts")
        database.execute(changed)
        for sql in dependents:
            database.execute(sql)
        database.commit()
    original_bytes = path.read_bytes()
    with pytest.raises(WikiError) as refused:
        protocols.registration_preflight(state, complete=True)
    assert refused.value.status == 503
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("retained", ["control", "outbox", "state"])
async def test_no_jobs_layout_with_retained_durable_job_references_cannot_be_called_new(registered_account, retained):
    root, state, registration, _ = registered_account
    path = state / "index.sqlite3"
    # The Registry's recovery path creates jobs even before the first request.
    # Reconstruct the independently frozen, valid pre-first-use format 12.
    uninitialized = state / "uninitialized.sqlite3"
    with closing(sqlite3.connect(uninitialized)) as baseline:
        baseline.executescript((Path(__file__).parent / "fixtures/wiki-baseline-v12.sql").read_text())
        baseline.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,?)",
                         (str(root), registration["vault_id"]))
        baseline.commit()
    uninitialized.replace(path)
    with closing(sqlite3.connect(path)) as database:
        database.execute("PRAGMA recursive_triggers=ON")
        assert database.execute("SELECT 1 FROM wiki_protocols WHERE name='maintenance_jobs'").fetchone() is None
        assert database.execute("SELECT 1 FROM sqlite_schema WHERE name='jobs'").fetchone() is None
        if retained == "control":
            database.execute("INSERT INTO wiki_job_controls VALUES('original-job','original-control','original-request-digest')")
        elif retained == "outbox":
            database.execute("INSERT INTO wiki_job_outbox VALUES('original-event','original-job',1,'{}','original-time')")
        else:
            database.execute("INSERT INTO wiki_job_state VALUES('original-job',1,0,'none')")
        database.commit()
    original = path.read_bytes()
    with pytest.raises(WikiError) as refused:
        protocols.registration_preflight(state, complete=True)
    assert refused.value.status == 503
    assert path.read_bytes() == original


@pytest.mark.parametrize("damage", ["missing_protocol", "unknown_trigger", "misplaced_index"])
@pytest.mark.parametrize("projection", ["maintenance", "search"])
async def test_damaged_projection_does_not_authorize_detaching_unrecognized_or_unprotected_state(registered_account, monkeypatch, damage, projection):
    root, state, registration, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        if projection == "maintenance":
            database.execute("DROP TABLE maintenance_job_items")
        else:
            database.execute("ALTER TABLE search_sections DROP COLUMN readable_body")
        if damage == "missing_protocol":
            database.execute("DELETE FROM wiki_protocols WHERE name='wiki_grants'")
        elif damage == "unknown_trigger":
            database.execute("CREATE TRIGGER maintenance_input_future_evidence AFTER UPDATE ON documents BEGIN SELECT 1; END")
        else:
            database.execute("DROP INDEX maintenance_receipt_document")
            database.execute("CREATE INDEX maintenance_receipt_document ON documents(id)")
        database.commit()
    original = path.read_bytes()
    dropped = []
    connect = sqlite3.connect

    def observe(action, first, second, database_name, source):
        if action in (sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX):
            dropped.append((action, first))
        return sqlite3.SQLITE_OK

    def observed(*args, **kwargs):
        database = connect(*args, **kwargs)
        raw = str(args[0])
        location = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
        if location != ":memory:" and Path(location).resolve() == path:
            database.set_authorizer(observe)
        return database

    monkeypatch.setattr(sqlite3, "connect", observed)
    with pytest.raises(WikiError) as refused:
        WikiStore(root, state, registration["vault_id"], defer_recovery=True)
    assert refused.value.status == 503
    assert dropped == []
    assert path.read_bytes() == original


@pytest.mark.parametrize("damage", ["middle_column", "extra_column"])
async def test_search_owner_does_not_admit_unrecognized_section_declarations(registered_account, damage):
    root, state, _, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        if damage == "middle_column":
            database.execute("ALTER TABLE search_sections DROP COLUMN section_role")
        else:
            database.execute("ALTER TABLE search_sections ADD COLUMN future_fact TEXT")
        database.commit()
    original, config, body = path.read_bytes(), (state / "config.json").read_bytes(), (root / "source.md").read_bytes()
    with pytest.raises(WikiError, match="retained") as refused:
        protocols.registration_preflight(state, complete=True)
    assert refused.value.status == 503
    assert path.read_bytes() == original
    assert (state / "config.json").read_bytes() == config
    assert (root / "source.md").read_bytes() == body

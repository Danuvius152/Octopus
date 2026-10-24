"""Retire duplicate physical columns only after proving authoritative facts."""

import sqlite3
from contextlib import closing

import pytest

from server.database import Database
from server.db.schema import UnsupportedMainStorage
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline


async def test_fresh_main_storage_has_only_authoritative_credential_and_recovery_columns(tmp_path):
    database = Database(str(tmp_path / "fresh.db"))
    try:
        await database.initialize()
        async with database.transaction(write=False) as tx:
            credential_columns = {row[1] for row in await tx.fetch_all("PRAGMA table_info(backend_credentials)")}
            submission_columns = {row[1] for row in await tx.fetch_all("PRAGMA table_info(client_submissions)")}
            assert "secret_encrypted" not in credential_columns
            assert "recovered_by" not in submission_columns
            assert "recovery_of" in submission_columns
            assert await tx.fetch_one("SELECT 1 FROM sqlite_schema WHERE name='client_submission_recovery'")
    finally:
        await database.close()


@pytest.mark.parametrize("damage", ["legacy_only_ciphertext", "unmatched_forward_relation"])
async def test_version_one_refuses_to_discard_information_outside_the_authority(tmp_path, damage):
    path = tmp_path / "retained.db"
    database = Database(str(path))
    try:
        await database.initialize()
        owner, _ = await create_account(database)
        await database.save_session("original", "Original session", str(tmp_path), "time", user_id=owner["id"])
        await database.save_credential("saved", "codex", "Original credential", "api_key",
                                       "original-account-ciphertext", "time", user_id=owner["id"])
        await database.admit_client_submission("original", "source", "original-digest", "original-request",
                                               payload={"text": "Original input 原文"}, receipt={"accepted": True})
    finally:
        await database.close()
    # Independently frozen complete format-0 DDL plus the ticket-10 metadata
    # retirement is the actual supported format 1, including both old columns.
    write_unversioned_baseline(path)
    with closing(sqlite3.connect(path)) as original:
        original.execute("DROP TABLE schema_migrations")
        original.execute("PRAGMA user_version=1")
        if damage == "legacy_only_ciphertext":
            original.execute("DELETE FROM credential_secrets WHERE credential_id='saved'")
            original.execute("UPDATE backend_credentials SET secret_encrypted='retained-legacy-only-ciphertext'")
        else:
            original.execute("UPDATE client_submissions SET recovered_by='unproven-successor'")
        original.commit()
    before = path.read_bytes()
    unopened = Database(str(path))
    with pytest.raises(UnsupportedMainStorage):
        await unopened.inspect()
    assert path.read_bytes() == before

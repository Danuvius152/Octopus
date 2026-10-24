"""Current storage has one authority for secrets and recovery relationships."""


from server.database import Database


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

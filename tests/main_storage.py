"""Independent format-0 input for supported main-storage upgrade tests."""

import sqlite3
from contextlib import closing
from pathlib import Path

from server.database import Database


async def protect_historical_message_sequences(database: Database) -> None:
    """Complete the supported historical fixture before its next startup."""
    script = (Path(__file__).parent / "fixtures/message-sequence-guards.sql").read_text()
    statement = ""
    async with database.transaction() as tx:
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                await tx.execute(statement)
                statement = ""
    assert not statement.strip()


def write_unversioned_baseline(path: Path) -> None:
    """Put existing test facts under frozen pre-ticket-10 DDL.

    Both connections are closed before replacing this test fixture. Production
    preparation never copies a database or restores an earlier version.
    """
    target = path.with_name(path.name + ".format0")
    assert not target.exists()
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as baseline:
        baseline.executescript((Path(__file__).parent / "fixtures/main-baseline-v0.sql").read_text())
        tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'",
        )}
        expected = {row[0] for row in baseline.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' AND name!='schema_migrations'",
        )}
        assert tables == expected
        for table in sorted(tables):
            # Identifiers come only from the compared, private fixture catalogs.
            columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
            names = ",".join('"' + column + '"' for column in columns)
            values = source.execute(f'SELECT {names} FROM "{table}"').fetchall()  # noqa: S608
            # Format 2 has no legacy credential placeholder. Construct the
            # old fixture explicitly; production never adds this column back.
            if table == "backend_credentials" and "secret_encrypted" not in columns:
                columns.append("secret_encrypted")
                values = [(*row, "") for row in values]
                names += ',"secret_encrypted"'
            baseline.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})', values,  # noqa: S608
            )
        baseline.commit()
    target.replace(path)


def write_version_one_baseline(path: Path) -> None:
    """Construct the frozen complete baseline after ticket 10's one step."""
    write_unversioned_baseline(path)
    with closing(sqlite3.connect(path)) as baseline:
        baseline.execute("DROP TABLE schema_migrations")
        baseline.execute("PRAGMA user_version=1")
        baseline.commit()

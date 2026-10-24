"""Startup matches one format; publication owns complete retained-data checks."""

import sqlite3
from pathlib import Path

import pytest

from server.database import Database
from server.db.schema import UnsupportedMainStorage
from tests.accounts import create_account


@pytest.mark.parametrize('version', [0, 3, -1, 999])
async def test_nonempty_format_mismatch_is_refused_before_opening_a_writer(tmp_path, version):
    path = tmp_path / 'owned.db'
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database)
    await database.save_session('saved', 'Original identity', str(tmp_path), 'time', user_id=owner['id'])
    await database.append_message('saved', 2, 'user', 'text', 'Original input 中文')
    await database.close()
    with sqlite3.connect(path) as db:
        db.execute(f'PRAGMA user_version={version}')
    before = path.read_bytes()
    unopened = Database(str(path))
    with pytest.raises(UnsupportedMainStorage):
        await unopened.inspect()
    assert unopened._conn is None
    assert path.read_bytes() == before


async def test_nonempty_unmarked_storage_is_not_recreated_as_an_empty_database(tmp_path):
    path = tmp_path / 'unmarked.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE retained(original BLOB)')
        db.execute('INSERT INTO retained VALUES(?)', (b'original\x00\xff',))
    before = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect()
    assert Path(path).read_bytes() == before

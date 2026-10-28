"""Publication checks current declarations and facts without changing storage."""

import sqlite3
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.storage_verification import verify_storage
from tests.accounts import create_account
from tests.test_service_runtime import installation_settings


@pytest.fixture
async def installation(tmp_path, monkeypatch):
    config, _ = installation_settings(tmp_path / 'installation')
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database)
    await database.save_session('original', 'Saved session', str(tmp_path), 'time', user_id=owner['id'])
    await database.append_message('original', 2, 'user', 'text', 'Original input 原文')
    await database.admit_client_submission('original', 'input', 'digest', 'original-request',
                                            payload={'wiki_mode': 'read'}, receipt={'accepted': True})
    await database.close()
    return config


@pytest.mark.parametrize('damage', ['missing-index', 'extra-column', 'unknown-trigger',
                                   'quoted-primary', 'quoted-default', 'missing-unique', 'retired-input'])
async def test_publication_rejects_incompatible_current_structure_or_facts_without_repair(installation, damage):
    path = Path(installation.db_path)
    with sqlite3.connect(path) as db:
        if damage == 'missing-index':
            db.execute('DROP INDEX messages_unique_sequence')
        elif damage == 'extra-column':
            db.execute('ALTER TABLE agents ADD COLUMN unknown_configuration TEXT')
        elif damage == 'unknown-trigger':
            db.execute('CREATE TRIGGER unknown_effect AFTER INSERT ON agents BEGIN DELETE FROM messages; END')
        elif damage == 'retired-input':
            db.execute("UPDATE client_submissions SET payload='{\"wiki_mode\":\"off\"}'")
        else:
            table = {'quoted-primary': 'agents', 'quoted-default': 'backend_credentials', 'missing-unique': 'users'}[damage]
            original = db.execute('SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()[0]
            replacements = {
                'quoted-primary': ('id TEXT PRIMARY KEY', 'id TEXT "PRIMARY" "KEY"'),
                'quoted-default': ("DEFAULT 'active'", '''DEFAULT "'active'"'''),
                'missing-unique': ('username TEXT NOT NULL UNIQUE COLLATE NOCASE', 'username TEXT NOT NULL COLLATE NOCASE'),
            }
            changed = original.replace(*replacements[damage])
            assert changed != original
            rows = db.execute(f'SELECT * FROM {table}').fetchall()
            dependents = [row[0] for row in db.execute(
                "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
            db.execute(f'DROP TABLE {table}')
            db.execute(changed)
            for declaration in dependents:
                db.execute(declaration)
            for row in rows:
                db.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in row)})', row)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='current'):
        verify_storage(installation)
    assert path.read_bytes() == before


async def test_publication_verification_leaves_current_messages_and_input_facts_unchanged(installation):
    path = Path(installation.db_path)
    before = path.read_bytes()
    assert verify_storage(installation)['main_format_version'] == 5
    assert path.read_bytes() == before
    database = Database(str(path))
    await database.initialize()
    try:
        assert (await database.load_messages('original'))[0]['content'] == 'Original input 原文'
        submission = await database.get_client_submission('original', 'input')
        assert submission['payload'] == {'wiki_mode': 'read'}
        assert submission['request_id'] == 'original-request'
    finally:
        await database.close()


async def test_publication_uses_the_same_main_database_alias_as_current_startup(installation, tmp_path):
    original = Path(installation.db_path)
    alias = tmp_path / 'main-alias.db'
    alias.symlink_to(original)
    installation.db_path = str(alias)
    before = original.read_bytes()
    assert verify_storage(installation)['main_format_version'] == 5
    assert original.read_bytes() == before

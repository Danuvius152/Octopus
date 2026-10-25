"""An existing Wiki must match the current marker before any writer opens."""

import sqlite3

import pytest

from server.wiki import protocols
from server.wiki.store import WikiError, WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account


async def test_previous_wiki_marker_is_refused_without_reinterpreting_identical_tables(registered_account):
    root, state, registration, _ = registered_account
    path = state / 'index.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=15')
    before, config, body = path.read_bytes(), (state / 'config.json').read_bytes(), (root / 'source.md').read_bytes()
    for open_storage in (
        lambda: protocols.registration_preflight(state, complete=True),
        lambda: WikiStore(root, state, registration['vault_id'], defer_recovery=True),
    ):
        with pytest.raises(WikiError, match='retained'):
            open_storage()
        assert path.read_bytes() == before
        assert (state / 'config.json').read_bytes() == config
        assert (root / 'source.md').read_bytes() == body

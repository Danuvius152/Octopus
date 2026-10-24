"""Retained Wiki formats must be inspected before construction can write."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from server.config import settings
from server.database import Database
from server.wiki import protocols
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError, WikiStore
from tests.accounts import create_account
from tests.test_service_runtime import installation_settings


@pytest.fixture
async def registered_account(tmp_path, monkeypatch):
    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "wiki-format-test-master")
    database = Database(config.db_path)
    registry = WikiRegistry()
    try:
        await database.initialize()
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        curator = await database.get_system_agent(owner["id"])
        root = tmp_path / "vault"
        root.mkdir()
        (root / "source.md").write_text("# Original source\nPreserve the original words 原文.\n")
        registered = registry.configure(owner["id"], root, curator["id"])
        state = registry.state(owner["id"]).resolve()
        assert root.resolve().is_relative_to(tmp_path.resolve())
        assert state.is_relative_to(tmp_path.resolve())
        assert Path(config.db_path).resolve().is_relative_to(tmp_path.resolve())
        registry.store(owner["id"]).read("source.md")
        registry.close()
        yield root, state, registered, owner["id"]
    finally:
        registry.close()
        await database.close()


@pytest.mark.parametrize("entry", ["preflight", "store", "create", "registry"])
@pytest.mark.parametrize("damage", ["future_version", "previous_version", "unmarked"])
async def test_unsupported_wiki_format_in_real_wal_is_refused_before_a_writer(registered_account, monkeypatch, entry, damage):
    root, state, registered, owner = registered_account
    path = state / "index.sqlite3"
    wal = Path(str(path) + "-wal")
    config_bytes = (state / "config.json").read_bytes()
    body_bytes = (root / "source.md").read_bytes()
    modes = []
    connect = sqlite3.connect

    def observed(*args, **kwargs):
        raw = str(args[0])
        uri = urlsplit(raw)
        location = unquote(uri.path) if raw.startswith("file:") else raw
        if location != ":memory:" and Path(location).resolve() == path:
            modes.append(parse_qs(uri.query).get("mode", ["rw"])[0])
        return connect(*args, **kwargs)

    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        baseline_main = path.read_bytes()
        writer.execute(f"PRAGMA user_version={999 if damage == 'future_version' else 14 if damage == 'previous_version' else 0}")
        writer.commit()
        assert path.read_bytes() == baseline_main
        original_wal = wal.read_bytes()
        assert len(original_wal) > 32
        registry, candidate = WikiRegistry(), None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", observed)
                with pytest.raises(WikiError) as refused:
                    if entry == "preflight":
                        protocols.registration_preflight(state, complete=True)
                    elif entry in {"store", "create"}:
                        candidate = WikiStore(root, state, registered["vault_id"], create=entry == "create", defer_recovery=True)
                    else:
                        registry.store(owner)
                assert refused.value.status == 503
            assert modes and set(modes) == {"ro"}
            assert path.read_bytes() == baseline_main and wal.read_bytes() == original_wal
            assert (state / "config.json").read_bytes() == config_bytes
            assert (root / "source.md").read_bytes() == body_bytes
        finally:
            if candidate is not None:
                candidate.close()
            registry.close()


async def test_publication_refuses_unknown_current_wiki_structure_without_changing_it(registered_account):
    from server.storage_verification import verify_storage

    root, state, _, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE future_business(original BLOB NOT NULL)")
        db.execute("INSERT INTO future_business VALUES(?)", (b"Original durable fact\x00\xff",))
    before = {p: p.read_bytes() for p in (path, state / "config.json", root / "source.md")}
    with pytest.raises(RuntimeError, match="incompatible structure"):
        verify_storage(settings)
    assert {p: p.read_bytes() for p in before} == before

"""Correct-behavior assertions: failures demonstrate unfixed bugs. No live CLI/API."""

import asyncio
import json
import sqlite3
import uuid

import pytest

from server.routers.sessions import get_session
from server.users import UserManager
from server.wiki.history import WikiHistory
from server.wiki.registry import registry
from server.wiki.store import WikiError, WikiStore, revision
from tests import (
    test_session_retirement,
    test_tenant_isolation,
    test_wiki_api,
    test_wiki_legacy_metadata,
)
from tests.test_wiki_legacy_metadata import initially_organize, organize, read

runtime = test_session_retirement.runtime
two_accounts = test_tenant_isolation.two_accounts
client = test_wiki_api.client
notes = test_wiki_legacy_metadata.notes


@pytest.mark.parametrize("delete", [False, True])
def test_journal_window_preserves_external_editor(tmp_path, monkeypatch, delete):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    history = WikiHistory(store)
    path = "Library/note.md"
    original = b"# Original\n"
    human = b"# Human\nSaved during journal preparation.\n"
    try:
        history.write(path, original, None, "human", "seed")
        target = store.path(path)
        record = history.record

        def racing(*args, **kwargs):
            result = record(*args, **kwargs)
            if args[5] == "prepared":
                target.write_bytes(human)
            return result

        monkeypatch.setattr(history, "record", racing)
        error = None
        try:
            history.write(
                path,
                None if delete else b"# Agent\n",
                revision(original),
                "agent",
                "edit",
            )
        except WikiError as exc:
            error = exc
        assert target.is_file() and target.read_bytes() == human, (
            "External human bytes were overwritten/deleted after the prepared journal"
        )
        assert error is not None and error.status == 409
    finally:
        store.close()


@pytest.mark.parametrize(
    "metadata", [{"x_wiki": "legacy"}, {"title": ["invalid title"]}]
)
def test_bad_portable_note_does_not_abort_vault(tmp_path, metadata):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "bad.md").write_text(
        "# Bad\n<!-- octopus-wiki " + json.dumps(metadata) + " -->\n"
    )
    (root / "good.md").write_text("# Good\nOriginal.\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        assert store.read("bad.md")["metadata"]["metadata_error"]
        assert store.read("good.md")["title"] == "Good"
    finally:
        store.close()


async def test_legacy_to_frontmatter_clear_is_authoritative(client, notes):
    await initially_organize(client, modern=False)
    response = await organize(client, frontmatter_patch={}, tags=[], sources=[])
    assert response.status_code == 200, response.text
    data = await read(client)
    assert (
        data["metadata"]["tags"],
        data["metadata"]["sources"],
        data["metadata"]["source_refs"],
    ) == ([], [], [])


async def test_unsaved_terminal_is_not_in_snapshot_watermark(runtime, monkeypatch):
    manager, session = runtime
    test_session_retirement.scripted_codex(monkeypatch)
    record = manager.db.record_terminal
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_before_insert(**kwargs):
        entered.set()
        await release.wait()
        return await record(**kwargs)

    monkeypatch.setattr(manager.db, "record_terminal", wait_before_insert)
    receipt = await manager.start_message(session.id, "Original input")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        snapshot = await get_session(manager, session.id, user_id=None)
        assert [row.content for row in snapshot.messages] == ["Original input", "ready"]
        assert [row.seq for row in snapshot.messages] == [0, 1]
        assert snapshot.next_message_seq == 2, (
            "Snapshot claims unsaved result sequence was already received"
        )
    finally:
        release.set()
        assert (await asyncio.wait_for(receipt.wait(), 5)).recording_state == "committed"


async def test_config_preserves_approved_extra_root(two_accounts, tmp_path):
    owner = two_accounts["archer"]
    db = two_accounts["db"]
    root = tmp_path / "approved-vault"
    root.mkdir()
    await db.update_user_field(owner["user"]["id"], extra_roots=json.dumps([str(root)]))
    registry.configure(owner["user"]["id"], root, owner["agent"]["id"])
    response = await two_accounts["client"].put(
        "/api/wiki/config",
        headers={"Authorization": "Bearer " + owner["token"]},
        json={"root": str(root), "curator_agent_id": owner["agent"]["id"]},
    )
    assert response.status_code == 200, response.text


async def test_acknowledged_token_activity_releases_sqlite_writer(runtime):
    manager, _ = runtime
    db = manager.db
    users = UserManager(db)
    user = await users.create_user(username="idle-activity", password="password1")
    token = await users.issue_token(user["id"])
    await users.record_token_activity(token)

    def write_slot():
        connection = sqlite3.connect(db._db_path, timeout=0.05, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            return True
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
            return False
        finally:
            connection.close()

    assert await asyncio.to_thread(write_slot), "Acknowledged token activity retained the SQLite writer"

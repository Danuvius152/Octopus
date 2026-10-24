"""Unsupported external intents never become inferred file history."""

import fcntl
import json
import uuid

import pytest

from server.wiki import app_bridge
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore, revision


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


def test_unknown_external_format_preserves_the_unsettled_effect_and_original_evidence(history):
    original = b"# Original\nExact original bytes.\r\n"
    changed = b"# Changed\nThe application may already have written this.\n"
    history.write("note.md", original, None, "human", "create")
    identity = history.store.read("note.md")["id"]
    history.store.path("note.md").write_bytes(changed)
    journal = history.state / "external-change.json"
    raw = json.dumps({
        "version": 999, "before": {"note.md": revision(original)},
        "actor": "app:sapphire", "reason": "save",
    }, indent=2).encode()
    journal.write_bytes(raw)
    blobs = {path.name: path.read_bytes() for path in history.blobs.iterdir()}
    with history.store.connect() as db:
        changes = [tuple(row) for row in db.execute("SELECT * FROM changes ORDER BY id")]
        identities = [tuple(row) for row in db.execute("SELECT * FROM document_identities ORDER BY id")]

    with pytest.raises(WikiError, match="external.*format") as failure:
        history.recover()

    assert failure.value.status == 503
    assert journal.read_bytes() == raw
    assert history.store.path("note.md").read_bytes() == changed
    assert {path.name: path.read_bytes() for path in history.blobs.iterdir()} == blobs
    with history.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM changes ORDER BY id")] == changes
        assert [tuple(row) for row in db.execute("SELECT * FROM document_identities ORDER BY id")] == identities
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (identity,)).fetchone()[0] == "note.md"


@pytest.mark.parametrize("missing", ["source_binding_protocol", "paths", "origin_state", "source_plan", "hash"])
def test_incomplete_old_intent_is_refused_before_history_or_source_repair(history, missing):
    original = b"# Original\nExact preimage remains protected.\n"
    history.write("note.md", original, None, "human", "create")
    with history.locked():
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                 "before": history.capture_external(["note.md"]), "paths": ["note.md"],
                 "actor": "app:sapphire", "reason": "save"}
        del (entry if missing in {"source_binding_protocol", "paths"} else entry["before"]["note.md"])[missing]
        changed = original + b"The app already wrote this.\n"
        history.store.path("note.md").write_bytes(changed)
        journal = history.state / "external-change.json"
        raw = json.dumps(entry, indent=2).encode()
        journal.write_bytes(raw)
        with history.store.connect() as db:
            facts = {name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                     for name in ("changes", "change_retention", "material_origins", "source_binding_observations", "source_binding_head")}
        blobs = {path.name: path.read_bytes() for path in history.blobs.iterdir()}

    for _ in range(2):
        with pytest.raises(WikiError, match="external.*format") as failure:
            history.recover()
        assert failure.value.status == 503
        assert journal.read_bytes() == raw and history.store.path("note.md").read_bytes() == changed
        assert {path.name: path.read_bytes() for path in history.blobs.iterdir()} == blobs
        with history.store.connect() as db:
            for name, rows in facts.items():
                assert [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")] == rows


@pytest.mark.parametrize(("change", "message", "status"), [
    ("version", "external.*format", 503), ("reason", "original.*intent changed", 409),
])
async def test_warm_conflict_rechecks_the_original_journal_before_recording_history(history, change, message, status):
    original = b"# Original\nOriginal persisted note.\n"
    history.write("note.md", original, None, "human", "create")
    handle = (history.state / "vault.lock").open("a+b")
    fcntl.flock(handle, fcntl.LOCK_EX)
    mutation = None
    try:
        with history.under_existing_lock(handle):
            entry = {"id": uuid.uuid4().hex, "version": 2, "source_binding_protocol": 1,
                     "paths": ["note.md"], "before": history.capture_external(["note.md"]),
                     "actor": "app:sapphire", "reason": "save", "restored": None,
                     "trash_id": None, "trash_root": str(history.state / "trash")}
        journal = history.state / "external-change.json"
        journal.write_text(json.dumps(entry))
        mutation = app_bridge.AppMutation(history, handle, entry, journal, "app:sapphire", "save")
        raw = json.dumps({**entry, change: 999 if change == "version" else "delete"}, indent=2).encode()
        journal.write_bytes(raw)
        with history.store.connect() as db:
            changes = [tuple(row) for row in db.execute("SELECT * FROM changes ORDER BY id")]
        blobs = {path.name: path.read_bytes() for path in history.blobs.iterdir()}

        with pytest.raises(WikiError, match=message) as failure:
            await mutation.conflict({"path": "note.md", "content": "A conflicting proposed body"})

        assert failure.value.status == status and not handle.closed
        assert journal.read_bytes() == raw and history.store.path("note.md").read_bytes() == original
        assert {path.name: path.read_bytes() for path in history.blobs.iterdir()} == blobs
        with history.store.connect() as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM changes ORDER BY id")] == changes
    finally:
        # Do not consume an intentionally invalid intent during test teardown.
        if mutation is not None:
            app_bridge._active.discard(mutation)
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

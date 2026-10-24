"""Targeted app text journaling and bounded recovery, including crash windows."""

import asyncio
import fcntl
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.wiki import origins
from server.wiki.app_bridge import begin
from server.wiki.history import WikiHistory, atomic
from server.wiki.registry import registry
from server.wiki.store import WikiError, WikiStore, revision
from tests.accounts import create_account


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return WikiHistory(WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True))


def date_changes(history, changes, when):
    with history.store.connect() as db:
        for change in changes:
            db.execute(
                "UPDATE changes SET created_at=? WHERE id=?",
                (when.isoformat(), change["change_id"]),
            )


def legacy_snapshot(history, files):
    manifest = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "reason": "legacy fixture",
        "files": {path: history.blob(raw) for path, raw in files.items()},
    }
    atomic(
        history.state / "snapshots" / f"{manifest['id']}.json",
        json.dumps(manifest).encode(),
    )
    return manifest


def test_retention_distinct_versions_repeated_hashes_and_30_day_boundary(history):
    now = datetime.now(UTC)
    current = None
    receipts = []
    for n in range(14):
        current = history.write(
            "note.md", f"version {n}".encode(), current, "human", "edit"
        )["revision"]
    # Repeated saves consume no extra version slots.
    for _ in range(5):
        receipts.append(
            history.write("note.md", b"version 13", current, "human", "repeat")
        )
    result = history.cleanup(now)
    assert result["removed_objects"] == 3  # current + ten historical versions
    recoverable = history.history("note.md")["items"]
    hashes = {
        row[f"{side}_hash"]
        for row in recoverable
        for side in ("before", "after")
        if row["recoverable"][side] and row[f"{side}_hash"] is not None
    }
    assert len(hashes - {current}) == 10
    assert history.cleanup(now)["removed_objects"] == 0
    date_changes(history, receipts, now - timedelta(days=30))
    history.cleanup(now)
    assert history.history("note.md")["items"][0]["recoverable"]["after"]
    # Every remaining record is outside the time window, not just repeats.
    with history.store.connect() as db:
        db.execute(
            "UPDATE changes SET created_at=?",
            ((now - timedelta(days=30, seconds=1)).isoformat(),),
        )
    history.cleanup(now)
    assert not any(
        row["recoverable"]["after"] for row in history.history("note.md")["items"]
    )
    assert history.store.path("note.md").read_bytes() == b"version 13"
    with pytest.raises(WikiError) as error:
        history.restore(receipts[0]["change_id"], current, "after", "human")
    assert error.value.status == 410


def test_expired_absence_side_cannot_delete_current_document(history):
    receipt = history.write("note.md", b"original", None, "human", "create")
    now = datetime.now(UTC)
    date_changes(history, [receipt], now - timedelta(days=31))
    history.cleanup(now)
    with pytest.raises(WikiError) as error:
        history.restore(receipt["change_id"], receipt["revision"], "before", "human")
    assert error.value.status == 410
    assert history.store.path("note.md").read_bytes() == b"original"


def test_daily_gc_counts_current_external_edit_before_selecting_ten_history_versions(history):
    current = None
    for number in range(14):
        current = history.write("note.md", f"version {number}".encode(), current, "human", "edit")["revision"]
    external = b"version 14 written outside the app"
    history.store.path("note.md").write_bytes(external)
    history.cleanup(datetime.now(UTC))
    hashes = {
        row[f"{side}_hash"]
        for row in history.history("note.md")["items"] for side in ("before", "after")
        if row["recoverable"][side] and row[f"{side}_hash"]
    }
    assert len(hashes - {revision(external)}) == 10
    assert history.store.path("note.md").read_bytes() == external


def test_retirement_tombstone_immediately_wins_over_an_unremoved_old_manifest(history):
    manifest = legacy_snapshot(history, {"note.md": b"original"})
    history.retire_snapshot(manifest["id"], "retire redundant full copy")
    # The durable tombstone can precede unlinking the legacy manifest.
    atomic(history.state / "snapshots" / f"{manifest['id']}.json", json.dumps(manifest).encode())
    entries = history.snapshots()["items"]
    assert len(entries) == 1 and entries[0]["id"] == manifest["id"]
    assert entries[0]["recoverable"] is False
    with pytest.raises(WikiError) as error:
        history.restore_snapshot(manifest["id"], ["note.md"], {"note.md": None})
    assert error.value.status == 410


def test_unresolved_conflict_90_days_resolved_candidate_and_management_expiry(history):
    receipt = history.write("note.md", b"original", None, "human", "create")
    with pytest.raises(WikiError):
        history.write("note.md", b"candidate", "wrong", "human", "conflict")
    conflict = history.history("note.md")["items"][0]
    now = datetime.now(UTC)
    date_changes(
        history, [{"change_id": conflict["id"]}, receipt], now - timedelta(days=90)
    )
    history.cleanup(now)
    assert history.history("note.md")["items"][0]["proposed_content"] == "candidate"
    history.cleanup(now + timedelta(seconds=1))
    assert "proposed_content" not in history.history("note.md")["items"][0]
    with history.store.connect() as db:
        states = db.execute(
            "SELECT before_state,after_state FROM change_management"
        ).fetchall()
        assert all(tuple(row) == (None, None) for row in states)
    assert history.store.path("note.md").read_bytes() == b"original"


def test_resolved_candidate_loses_special_protection(history):
    history.write("note.md", b"original", None, "human", "create")
    with pytest.raises(WikiError):
        history.write("note.md", b"candidate", "wrong", "human", "conflict")
    conflict = history.history("note.md")["items"][0]
    history.resolve_proposals([conflict["id"]])
    history.cleanup()
    assert not (history.blobs / revision(b"candidate")).exists()
    assert "proposed_content" not in history.history("note.md")["items"][0]


def test_prepared_write_and_active_job_receipts_survive_cleanup(history):
    old = datetime.now(UTC) - timedelta(days=100)
    receipt = history.write("note.md", b"original", None, "human", "create")
    prepared = history.record(
        "note.md", b"original", b"pending", "human", "pending", "prepared"
    )
    atomic(history.store.path("note.md"), b"pending")
    # Explicit startup recovery confirms the landed write. An active job keeps
    # protecting its input and creation receipt beyond ordinary retention.
    history.recover()
    with history.store.connect() as db:
        db.execute("CREATE TABLE jobs (id TEXT,status TEXT,items TEXT,payload TEXT)")
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?)",
            (
                "active",
                "running",
                json.dumps(
                    [{"revision": revision(b"pending"), "change_id": prepared["id"]}]
                ),
                json.dumps(
                    {"request": {"created_changes": {"note.md": receipt["change_id"]}}}
                ),
            ),
        )
        db.execute("UPDATE changes SET created_at=?", (old.isoformat(),))
    history.cleanup()
    assert (history.blobs / revision(b"original")).is_file()
    assert (history.blobs / revision(b"pending")).is_file()
    restored = history.restore(
        receipt["change_id"], revision(b"pending"), "after", "human"
    )
    assert restored["revision"] == revision(b"original")


def test_old_snapshot_root_retirement_and_interrupted_gc(history, monkeypatch):
    manifest = legacy_snapshot(history, {"note.md": b"original", "video.mp4": b"video"})
    history.store.path("note.md").write_bytes(b"original")
    history.store.path("video.mp4").write_bytes(b"video")
    assert history.cleanup()["removed_objects"] == 0
    with pytest.raises(WikiError) as error:
        history.snapshot("new snapshots prohibited")
    assert error.value.status == 410
    history.retire_snapshot(manifest["id"], "accepted migration")
    real_unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path.parent == history.blobs:
            raise OSError("Interrupted GC")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(OSError, match="Interrupted GC"):
        history.cleanup()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert history.cleanup()["removed_objects"] == 2
    assert history.cleanup()["removed_objects"] == 0
    assert history.store.path("note.md").read_bytes() == b"original"
    assert history.store.path("video.mp4").read_bytes() == b"video"
    assert history.snapshots()["items"][0]["recoverable"] is False
    with pytest.raises(WikiError) as error:
        history.restore_snapshot(
            manifest["id"], ["note.md"], {"note.md": revision(b"original")}
        )
    assert error.value.status == 410


def test_gc_and_restore_blob_read_share_the_same_lock(history, monkeypatch):
    receipt = history.write("note.md", b"original", None, "human", "create")
    entered, release, cleanup_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_read = Path.read_bytes

    def paused(path):
        if (
            path == history.blobs / receipt["revision"]
            and threading.current_thread().name == "restore-test"
        ):
            entered.set()
            assert release.wait(5)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", paused)
    errors = []

    def restore():
        try:
            history.restore(receipt["change_id"], receipt["revision"], "after", "human")
        except BaseException as error:
            errors.append(error)

    def cleanup():
        try:
            history.cleanup(datetime.now(UTC) + timedelta(days=100))
        except BaseException as error:
            errors.append(error)
        finally:
            cleanup_done.set()

    reader = threading.Thread(target=restore, name="restore-test")
    collector = threading.Thread(target=cleanup)
    reader.start()
    try:
        assert entered.wait(2)
        collector.start()
        assert not cleanup_done.wait(0.1)
    finally:
        release.set()
        reader.join(5)
        if collector.ident is not None:
            collector.join(5)
    assert not errors
    assert cleanup_done.is_set()
    assert history.store.path("note.md").read_bytes() == b"original"


@pytest.fixture
async def sapphire(tmp_path, monkeypatch):
    database = Database(":memory:")
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent(owner["id"])
    app_dir = tmp_path / "sapphire"
    app_dir.mkdir()
    root = tmp_path / "sapphire.data" / "vaults" / "obv" / "repo"
    root.mkdir(parents=True)
    data = tmp_path / "sapphire.data"
    (data / "registry.json").write_text(json.dumps({"vaults": [{"id": "obv"}]}))
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    registry.configure(owner["id"], root, agent["id"], "sapphire")
    store = registry.store(owner["id"])
    (root / "note.md").write_bytes(b"original")
    (root / "unchanged.md").write_bytes(b"unrelated text")
    (root / "huge.pdf").write_bytes(b"unread PDF")
    (root / "video.mp4").write_bytes(b"unread video")
    store.refresh()
    try:
        yield {"id": "sapphire", "app_dir": str(app_dir), "user_id": owner["id"]}, WikiHistory(store)
    finally:
        registry.close()
        await database.close()


async def test_sapphire_save_only_captures_modified_text_and_never_reads_attachments(
    sapphire, monkeypatch
):
    row, history = sapphire
    import os

    original = Path.read_bytes
    original_walk = os.walk
    scans = []

    def forbid(path):
        if path.is_relative_to(history.store.root) and path.name != "note.md":
            raise AssertionError(f"Unrelated file read: {path.name}")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forbid)
    def observe_walk(path, *args, **kwargs):
        if path == history.store.root:
            scans.append(path)
        return original_walk(path, *args, **kwargs)

    monkeypatch.setattr(os, "walk", observe_walk)
    body = json.dumps({"path": "note.md", "content": "edited"}).encode()
    mutation = await begin(row, "POST", "save", {"vault": "obv"}, body)
    history.store.path("note.md").write_bytes(b"edited")
    await mutation.finish()
    assert {file.name for file in history.blobs.iterdir()} == {
        revision(b"original"),
        revision(b"edited"),
    }
    assert history.history("note.md")["items"][0]["after_hash"] == revision(b"edited")
    assert not (history.state / "external-change.json").exists()
    assert scans == []


async def test_attachment_upload_and_git_commit_hold_lock_without_text_capture(
    sapphire, monkeypatch
):
    row, history = sapphire

    def forbid(*args):
        raise AssertionError("Unexpected inventory")

    monkeypatch.setattr(WikiHistory, "capture_external", forbid)
    for endpoint, body in [
        ("upload", {"path": "image.png", "data": "AA=="}),
        ("git", {"action": "commit"}),
        ("git", {"action": "push"}),
    ]:
        mutation = await begin(
            row, "POST", endpoint, {"vault": "obv"}, json.dumps(body).encode()
        )
        assert mutation is not None and mutation.before is None
        with (history.state / "vault.lock").open("a+b") as contender:
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                await mutation.finish()
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert list(history.blobs.glob("*")) == []
    assert history.history("note.md")["items"] == []
    assert not (history.state / "external-change.json").exists()


async def test_sapphire_rename_delete_restore_preserve_identity_and_owned_additions(
    sapphire,
):
    row, history = sapphire
    original = history.store.read("note.md")
    origin_state = {
        "version": 1, "doc_id": original["id"], "revision": original["revision"],
        "status": "valid", "proof": {"version": 739, "opaque": "private source state"},
    }
    managed = {
        "path": "note.md",
        "base_hash": original["revision"],
        "rendered_revision": original["revision"],
        "regions": "[]",
        "metadata": json.dumps({"author": "Weber 整理"}),
    }
    with history.store.connect() as db:
        origins.put(db, original["id"], origin_state)
        db.execute(
            "INSERT INTO managed_notes VALUES(:path,:base_hash,:rendered_revision,:regions,:metadata)",
            managed,
        )
    rename = await begin(
        row,
        "POST",
        "rename",
        {"vault": "obv"},
        json.dumps({"path": "note.md", "to": "renamed.md"}).encode(),
    )
    history.store.path("note.md").rename(history.store.path("renamed.md"))
    await rename.finish()
    assert history.store.read(original["id"])["path"] == "renamed.md"
    with history.store.connect() as db:
        assert origins.get(db, original["id"]) == origin_state
    delete = await begin(
        row, "POST", "delete", {"vault": "obv"}, json.dumps({"path": "renamed.md"}).encode()
    )
    tid = uuid.uuid4().hex
    trash = Path(row["app_dir"] + ".data") / "trash" / "obv" / tid
    trash.mkdir(parents=True)
    (trash / "info.json").write_text(json.dumps({"id": tid, "path": "renamed.md"}))
    history.store.path("renamed.md").rename(trash / "file")
    await delete.receipt({"id": tid})
    await delete.finish()
    with history.store.connect() as db:
        assert origins.get(db, original["id"])["status"] == "invalid"
        assert json.loads(db.execute("SELECT origin_state FROM app_trash WHERE trash_id=?", (tid,)).fetchone()[0]) == origin_state
    # Same path reused for another document must not steal the old identity.
    other = history.write(
        "renamed.md", b"other document", None, "human", "create other"
    )
    replacement_id = history.store.read("renamed.md")["id"]
    assert replacement_id != original["id"]
    failed_restore = await begin(
        row, "POST", "restore", {"vault": "obv"}, json.dumps({"id": tid}).encode()
    )
    await failed_restore.finish()  # app rejects because destination exists
    assert history.store.read("renamed.md")["revision"] == other["revision"]
    history.store.path("renamed.md").unlink()
    history.store.refresh()
    restore = await begin(row, "POST", "restore", {"vault": "obv"}, json.dumps({"id": tid}).encode())
    (trash / "file").rename(history.store.path("renamed.md"))
    (trash / "info.json").unlink()
    trash.rmdir()
    await restore.finish()
    restored = history.store.read("renamed.md")
    assert restored["id"] == original["id"]
    assert restored["metadata"]["author"] == "Weber 整理"
    assert restored["content"] == original["content"]
    with history.store.connect() as db:
        assert origins.get(db, original["id"]) == origin_state
        assert origins.get(db, replacement_id) is None


async def test_sync_partial_failure_records_only_actual_text_changes_and_crash_replays_once(
    sapphire,
):
    row, history = sapphire
    mutation = await begin(
        row, "POST", "git", {"vault": "obv"}, json.dumps({"action": "sync"}).encode()
    )
    history.store.path("note.md").write_bytes(b"merge succeeded; push failed")
    # Simulate a process restart after journaling, before final journal unlink.
    await asyncio.to_thread(mutation._finish)
    # Recreate the journal to model an interrupted unlink; deterministic ids
    # prevent duplicate applied records during its replay.
    atomic(history.state / "external-change.json", json.dumps(mutation.before).encode())
    await mutation.finish()
    with history.locked():
        pass
    changes = history.history("note.md")["items"]
    assert len(changes) == 1
    assert changes[0]["reason"] == "git"
    assert {file.name for file in history.blobs.iterdir()} == {
        revision(b"original"),
        revision(b"merge succeeded; push failed"),
    }
    assert history.history("unchanged.md")["items"] == []


async def test_startup_daily_sweep_preserves_originals_and_does_not_call_models(
    sapphire,
):
    from server.wiki.cleanup import sweep

    _, history = sapphire
    receipt = history.write(
        "note.md", b"edited", revision(b"original"), "human", "edit"
    )
    date_changes(history, [receipt], datetime.now(UTC) - timedelta(days=31))
    results = await sweep()
    assert results[0]["removed_objects"] == 2
    assert history.store.path("note.md").read_bytes() == b"edited"
    assert (history.state / "last-cleanup.json").is_file()

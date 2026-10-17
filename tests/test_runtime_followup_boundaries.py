"""Runtime follow-up boundaries use files/SQLite/ASGI, never live models."""

import asyncio
import json
import socket
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from server.routers.sessions import get_session
from server.sessions.turns import _Settlement
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiError, WikiStore, revision
from tests import (
    test_session_retirement,
    test_tenant_isolation,
    test_wiki_api,
    test_wiki_legacy_metadata,
)
from tests.hermetic_guard import install
from tests.test_wiki_legacy_metadata import (
    NOTE,
    citation,
    initially_organize,
    managed,
    organize,
    read,
)

runtime = test_session_retirement.runtime
two_accounts = test_tenant_isolation.two_accounts
client = test_wiki_api.client
notes = test_wiki_legacy_metadata.notes


@pytest.mark.parametrize("mode", ["create", "identity", "restore", "publish"])
def test_prepared_mutations_recheck_live_target_and_never_replay_conflict(tmp_path, monkeypatch, mode):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    history = WikiHistory(store)
    path, original, human = "Library/note.md", b"# Original\n", b"# Human edit\n"
    target = store.path(path)
    try:
        if mode not in {"create", "publish"}:
            seed = history.write(path, original, None, "human", "seed")
        record = history.record

        def racing(*args, **kwargs):
            row = record(*args, **kwargs)
            if args[5] == "prepared":
                target.parent.mkdir(exist_ok=True)
                if mode == "identity":
                    replacement = target.with_suffix(".new")
                    replacement.write_bytes(original)
                    replacement.replace(target)
                else:
                    target.write_bytes(human)
            return row

        monkeypatch.setattr(history, "record", racing)
        with pytest.raises(WikiError, match="journal preparation") as error:
            if mode == "restore":
                history.restore(seed["change_id"], revision(original), "after", "human")
            elif mode == "publish":
                curation = WikiCuration(store)
                monkeypatch.setattr(curation.history, "record", racing)
                curation.publish(path, "# Published\n", "concept", [], None, "human", "publish")
            else:
                history.write(path, b"# Agent\n", revision(original) if mode == "identity" else None, "agent", "save")
        assert error.value.status == 409
        expected = original if mode == "identity" else human
        assert target.read_bytes() == expected
        for _ in range(2):
            with history.locked():
                history.recover()
        assert target.read_bytes() == expected
        with store.connect() as db:
            receipt = db.execute("SELECT c.status FROM operation_receipts r JOIN changes c ON c.id=r.change_id ORDER BY r.rowid DESC LIMIT 1").fetchone()
            assert receipt[0] == "conflict"
            assert db.execute("SELECT count(*) FROM changes WHERE status='prepared'").fetchone()[0] == 0
        assert (history.blobs / revision(expected)).read_bytes() == expected
    finally:
        store.close()


async def test_publication_race_keeps_attention_and_unindexed_receipt(client, notes, monkeypatch):
    root, store, config = notes
    document = await read(client)
    jobs = WikiJobs(None)
    job = jobs.maintain("race", "", "organize", [NOTE], None, [], "human")
    jobs.update(job["id"], status="running", session_id="worker")
    publication = {"job_id": job["id"], "session_id": "worker", "source_doc": document["id"], "source_path": NOTE, "input_revision": document["revision"]}
    history = WikiHistory(store)
    record = history.record
    human = b"# Preserved human save\n"

    def racing(*args, **kwargs):
        row = record(*args, **kwargs)
        if args[5] == "prepared":
            (root / NOTE).write_bytes(human)
        return row

    monkeypatch.setattr(history, "record", racing)
    with history.locked(), pytest.raises(WikiError):
        history.write_locked(NOTE, b"# Proposal\n", document["revision"], "worker", "publish", publication=publication)
    item = dict(jobs.get(job["id"])["items"][0])
    assert jobs.recover_publication(job["id"], item)
    assert item["status"] == "attention" and not item["index_complete"]
    assert jobs.has_conflict(job["id"], NOTE)
    receipt = jobs.publication(job["id"], document["id"])
    assert receipt["change_status"] == "conflict" and not receipt["indexed"]
    assert (root / NOTE).read_bytes() == human


def test_external_save_during_atomic_temporary_fsync_is_checked_again(tmp_path, monkeypatch):
    from server.wiki import history as module

    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    history = WikiHistory(store)
    original, human = b"# Original\n", b"# Saved during temporary-file fsync\n"
    history.write("note.md", original, None, "human", "seed")
    target = store.path("note.md")
    record, fsync = history.record, module.os.fsync
    prepared = False

    def prepare(*args, **kwargs):
        nonlocal prepared
        row = record(*args, **kwargs)
        if args[5] == "prepared":
            prepared = True
        return row

    def save_during_fsync(fd):
        nonlocal prepared
        if prepared:
            prepared = False
            target.write_bytes(human)
        return fsync(fd)

    monkeypatch.setattr(history, "record", prepare)
    monkeypatch.setattr(module.os, "fsync", save_during_fsync)
    try:
        with pytest.raises(WikiError, match="journal preparation"):
            history.write("note.md", b"# Agent\n", revision(original), "agent", "save")
        assert target.read_bytes() == human
        assert (history.blobs / revision(human)).read_bytes() == human
    finally:
        store.close()


BAD_METADATA = [
    {"x_wiki": "legacy"}, {"title": ["bad"]}, {"description": {}}, {"kind": []},
    {"tags": [1]}, {"aliases": {}}, {"uid": []}, {"type": 12}, {"resource": []},
    {"status": {}}, {"generated": []}, {"generated": {"by": []}},
    {"x_wiki": {"representation_of": []}}, {"x_wiki": {"profile": []}},
    {"x_wiki": {"body_dialect": {}}}, {"verified": ["bad"]},
    {"sources": "bad"}, {"sources": ["bad"]}, {"sources": [{"doc": []}]},
    *({"sources": [{"doc": "good.md", field: []}]} for field in ("resource", "path", "revision", "location", "id")),
]


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("metadata", BAD_METADATA)
def test_bad_portable_metadata_is_local_and_original_bytes_survive(tmp_path, metadata, incremental):
    root = tmp_path / "vault"
    root.mkdir()
    good = b"# Healthy\nSearchable evidence.\n"
    (root / "good.md").write_bytes(good)
    bad = b"# Bad\n<!-- octopus-wiki " + json.dumps(metadata).encode() + b" -->\n"
    if not incremental:
        (root / "bad.md").write_bytes(bad)
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    try:
        if incremental:
            (root / "bad.md").write_bytes(bad)
            store.synchronize()
        assert store.read("bad.md")["metadata"]["metadata_error"]
        assert store.read("good.md")["title"] == "Healthy"
        assert store.search("Searchable")["items"]
        assert (root / "bad.md").read_bytes() == bad
        assert (root / "good.md").read_bytes() == good
    finally:
        store.close()


def test_portable_metadata_cannot_forge_index_or_extraction_fields(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    supplied = {"body_start": [], "body_sections": "bad", "doc_role": [], "pipeline_version": [], "format_version": [], "html_body_version": [], "html_body_readable": [], "quality": "verified", "metadata_error": "forged", "source_refs": [{}]}
    (root / "note.md").write_text('# Note\nEvidence.\n<!-- octopus-wiki ' + json.dumps(supplied) + ' -->\n')
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    try:
        result = store.read("note.md")
        assert result["metadata"]["body_start"] == 0
        assert result["metadata"]["doc_role"] == "unknown"
        assert result["metadata"]["source_refs"] == []
        assert "metadata_error" not in result["metadata"]
        assert store.search("Evidence")["items"]
    finally:
        store.close()


def test_valid_portable_fields_recover_after_bad_metadata_without_losing_identity(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    raw = b"# Source\nEvidence.\n"
    (root / "good.md").write_bytes(raw)
    target = root / "note.md"
    target.write_text('# Note\n<!-- octopus-wiki {"title": []} -->\n')
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    try:
        old = store.read("note.md")
        metadata = {
            "title": "Portable title", "description": "A safe description", "type": "Concept", "kind": "concept",
            "status": "draft", "resource": "https://example.com/article", "uid": "urn:uuid:" + str(uuid.uuid4()),
            "tags": ["portabletag"], "aliases": ["portablealias"],
            "sources": [{"doc": "good.md", "path": "good.md", "resource": "good.md", "revision": revision(raw), "location": "L2", "id": "source"}],
            "generated": {"by": "imported", "at": "2026-01-01"}, "verified": [{"by": "human"}],
            "x_wiki": {"profile": "article-v1", "body_dialect": "markdown", "representation_of": "good.md"},
        }
        current = '# Note\nMeaningful explanation.\n<!-- octopus-wiki ' + json.dumps(metadata) + ' -->\n'
        target.write_text(current)
        store.synchronize()
        result = store.read("note.md")
        assert result["id"] == old["id"] and result["title"] == metadata["title"]
        assert "metadata_error" not in result["metadata"]
        assert result["metadata"]["source_refs"][0]["revision"] == revision(raw)
        assert store.search("portablealias", intent="lookup")["items"][0]["path"] == "note.md"
        assert target.read_text() == current
    finally:
        store.close()


@pytest.mark.parametrize("field", ["tags", "aliases", "sources"])
@pytest.mark.parametrize("mode", ["omitted", "null", "clear", "replace"])
async def test_legacy_to_yaml_replacement_is_independent_and_restorable(client, notes, field, mode):
    root, store, _ = notes
    source = await initially_organize(client)
    assert (await organize(client, aliases=["obsoletepseudonym"])).status_code == 200
    before = await read(client)
    old_raw = (root / NOTE).read_bytes()
    old_metadata, old_regions = managed(store)
    values = {"tags": ["new-tag"], "aliases": ["replacementpseudonym"], "sources": [citation(source) | {"location": "L4"}]}
    fields = {} if mode == "omitted" else {field: None if mode == "null" else [] if mode == "clear" else values[field]}
    response = await organize(client, frontmatter_patch={}, **fields)
    assert response.status_code == 200, response.text
    current = await read(client)
    if mode in {"omitted", "null"}:
        assert current["metadata"][field] == before["metadata"][field]
        assert managed(store)[0][field] == old_metadata[field]
    else:
        assert field not in managed(store)[0]
        if field == "sources":
            refs = current["metadata"]["source_refs"]
            assert len(refs) == (0 if mode == "clear" else 1)
            if refs:
                assert refs[0]["revision"] == source["revision"] and refs[0]["location"] == "L4"
        else:
            assert current["metadata"][field] == ([] if mode == "clear" else values[field])
            assert all(part["raw"] not in (root / NOTE).read_text(encoding="utf-8-sig") for part in old_regions if part["role"] == field)
            old_term = "managed-tag" if field == "tags" else "obsoletepseudonym"
            assert NOTE not in {item["path"] for item in store.search(old_term)["items"]}
            if mode == "replace":
                assert NOTE in {item["path"] for item in store.search(values[field][0])["items"]}
    for other in {"tags", "aliases", "sources"} - {field}:
        assert current["metadata"][other] == before["metadata"][other]
    assert "User sentence with #native-body." in (root / NOTE).read_text(encoding="utf-8-sig")
    # Restoring the real before-image restores both YAML/body and ownership.
    WikiHistory(store).restore(response.json()["change_id"], current["revision"], "before", "human")
    assert (root / NOTE).read_bytes() == old_raw
    restored = await read(client)
    assert restored["metadata"]["source_refs"] == before["metadata"]["source_refs"]
    assert managed(store)[0] == old_metadata


@pytest.mark.parametrize("mode", ["before_insert", "writing", "lost_ack"])
async def test_snapshot_watermark_uses_exact_loaded_rows_during_recording(runtime, monkeypatch, mode):
    manager, session = runtime
    record = manager.db.record_terminal
    settlement = _Settlement(request_id="recording-replay")
    session._settlement = settlement
    entered, release = asyncio.Event(), asyncio.Event()

    async def faulty(**kwargs):
        if mode == "before_insert":
            raise OSError("before INSERT")
        if mode == "writing":
            entered.set()
            await release.wait()
        await record(**kwargs)
        if mode == "lost_ack":
            raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(manager.db, "record_terminal", faulty)
    final = asyncio.create_task(manager._settle_turn(session, settlement, status="success"))
    if mode == "writing":
        await entered.wait()
        snapshot = await get_session(manager, session.id, user_id=None)
        assert snapshot.next_message_seq == 0 and snapshot.messages == []
        release.set()
    await final
    snapshot = await get_session(manager, session.id, user_id=None)
    assert snapshot.next_message_seq == (0 if mode == "before_insert" else 1)
    assert len(snapshot.messages) == snapshot.next_message_seq
    replay = json.loads((Path(__file__).parents[1] / "web/src/lib/recordingReplay.json").read_text())

    def projection(snapshot):
        return {"next_message_seq": snapshot.next_message_seq, "messages": [
            {key: row.model_dump()[key] for key in ("seq", "role", "type", "content", "is_error")}
            for row in snapshot.messages
        ]}

    assert projection(snapshot) == replay["before" if mode == "before_insert" else "after"]
    monkeypatch.setattr(manager.db, "record_terminal", record)
    delivered = []

    async def capture(event):
        if event.get("type") == "result":
            delivered.append(event)

    manager.on_broadcast("recording-replay", capture)
    if mode != "writing":
        assert await manager._reconcile_recording(session)
        assert delivered[0]["seq"] == 0
        event = dict(delivered[0], session_id="recording-replay-session")
        assert {key: event[key] for key in replay["event"]} == replay["event"]
    reconciled = await get_session(manager, session.id, user_id=None)
    assert len(reconciled.messages) == 1 and reconciled.next_message_seq == 1
    assert projection(reconciled) == replay["after"]
    assert await manager._reconcile_recording(session)
    assert len(delivered) <= 1


def writer_available(path):
    with sqlite3.connect(path, timeout=.02, isolation_level=None) as writer:
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
            return True
        except sqlite3.OperationalError as error:
            assert "locked" in str(error)
            return False


async def test_idle_append_and_continuous_activity_batches_do_not_starve(runtime, monkeypatch):
    from server.db import base

    monkeypatch.setattr(base, "_FLUSH_AFTER_SECONDS", .05)
    manager, session = runtime
    db = manager.db
    await db.append_message(session.id, 0, "user", "text", content="last message")
    await asyncio.sleep(.12)
    assert await asyncio.to_thread(writer_available, db._db_path)
    commits = []
    commit = db.conn.commit

    async def counted():
        commits.append(asyncio.get_running_loop().time())
        await commit()

    monkeypatch.setattr(db.conn, "commit", counted)
    # Missing hashes still perform real deferred UPDATEs. Frequent activity
    # must share batches without moving the first write's deadline forward.
    async def traffic():
        for _ in range(24):
            await db.touch_token("not-a-real-token", at="test")
            await asyncio.sleep(.01)

    await asyncio.gather(traffic(), traffic())
    await asyncio.sleep(.08)
    assert 3 <= len(commits) < 24
    assert await asyncio.to_thread(writer_available, db._db_path)


async def test_deferred_commit_failure_is_visible_and_retries_without_lost_batch(runtime, monkeypatch, caplog):
    from server.db import base

    monkeypatch.setattr(base, "_FLUSH_AFTER_SECONDS", .02)
    db = runtime[0].db
    commit = db.conn.commit
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("injected commit failure")
        await commit()

    monkeypatch.setattr(db.conn, "commit", fail_once)
    await db.touch_token("test", at="test")
    await asyncio.sleep(.12)
    assert attempts == 2 and not db._dirty
    assert "Deferred database commit failed" in caplog.text
    assert await asyncio.to_thread(writer_available, db._db_path)


async def test_cancelled_close_joins_timer_and_commits_before_connection_is_retired(runtime, monkeypatch):
    db = runtime[0].db
    commit = db.conn.commit
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        await commit()

    monkeypatch.setattr(db.conn, "commit", delayed)
    await db.touch_token("test", at="test")
    timer = db._flush_task
    waiter = asyncio.create_task(db.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await db.close()
    assert timer.done() and db._closed and db._conn is None
    assert await asyncio.to_thread(writer_available, db._db_path)


async def test_close_commit_failure_keeps_the_batch_and_timer_for_retry(runtime, monkeypatch, caplog):
    db = runtime[0].db
    commit = db.conn.commit
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("injected close commit failure")
        await commit()

    monkeypatch.setattr(db.conn, "commit", fail_once)
    await db.touch_token("test", at="test")
    timer = db._flush_task
    with pytest.raises(sqlite3.OperationalError, match="close commit"):
        await db.close()
    assert timer.done() and db._dirty and not db._closed
    assert db._flush_task is not None and not db._flush_task.done()
    assert "Database close failed" in caplog.text
    await db.close()
    assert db._closed and db._flush_task is None


async def test_snapshot_waits_for_inflight_sql_commit_and_does_not_claim_future_rows(runtime, monkeypatch):
    manager, session = runtime
    db = manager.db
    commit = db.conn.commit
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        await commit()

    monkeypatch.setattr(db.conn, "commit", delayed)
    settlement = _Settlement()
    final = asyncio.create_task(manager._settle_turn(session, settlement, status="success"))
    await entered.wait()
    snapshot_task = asyncio.create_task(get_session(manager, session.id, user_id=None))
    await asyncio.sleep(.01)
    assert not snapshot_task.done()
    release.set()
    await final
    snapshot = await snapshot_task
    assert snapshot.next_message_seq == 1 and len(snapshot.messages) == 1

    load = db.load_messages

    async def load_then_advance(*args, **kwargs):
        messages = await load(*args, **kwargs)
        session._message_count += 1  # A later reservation after SQL loaded.
        return messages

    monkeypatch.setattr(db, "load_messages", load_then_advance)
    snapshot = await get_session(manager, session.id, user_id=None)
    assert snapshot.next_message_seq == 1


async def test_config_uses_owner_approved_roots_for_rebinding_and_rejects_escapes(two_accounts, tmp_path):
    from server.agent_manager import AgentManager
    from server.wiki.registry import registry

    ctx = two_accounts
    owner, other = ctx["archer"], ctx["vera"]
    root = tmp_path / "adopted-vault"
    root.mkdir()
    outside = tmp_path / "unapproved"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    await ctx["db"].update_user_field(owner["user"]["id"], extra_roots=json.dumps([str(root)]))
    registry.configure(owner["user"]["id"], root, owner["agent"]["id"])
    curator = await AgentManager(ctx["db"]).create_agent(name="New curator", user_id=owner["user"]["id"])
    # Persist app metadata directly: this configuration test has no build.
    await ctx["db"].save_application(app_id="owned-app", name="Wiki", description="wiki", agent_id=curator["id"], session_id=None, app_dir=str(root), user_id=owner["user"]["id"], created_at="2026-01-01", updated_at="2026-01-01")
    headers = {"Authorization": "Bearer " + owner["token"]}
    for agent, app_id in ((owner["agent"]["id"], None), (curator["id"], None), (curator["id"], "owned-app"), (curator["id"], None)):
        response = await ctx["client"].put("/api/wiki/config", headers=headers, json={"root": str(root), "curator_agent_id": agent, "app_id": app_id})
        assert response.status_code == 200, response.text
    for path in (outside, root / "escape"):
        response = await ctx["client"].put("/api/wiki/config", headers=headers, json={"root": str(path), "curator_agent_id": curator["id"], "extra_roots": [str(outside)]})
        assert response.status_code in {400, 422}
    response = await ctx["client"].put("/api/wiki/config", headers={"Authorization": "Bearer " + other["token"]}, json={"root": str(root), "curator_agent_id": other["agent"]["id"]})
    assert response.status_code == 400
    response = await ctx["client"].put("/api/wiki/config", headers=headers, json={"root": str(root), "curator_agent_id": other["agent"]["id"]})
    assert response.status_code == 404


def test_hermetic_guard_blocks_real_cli_and_live_callback_before_any_io(monkeypatch):
    import httpx

    with monkeypatch.context() as patch:
        violations = install(patch)
        for name in ("claude", "codex"):
            with pytest.raises(AssertionError, match="model CLI"):
                subprocess.run([name, "--version"], check=True)
        for method in ("connect", "connect_ex"):
            with socket.socket() as connection, pytest.raises(AssertionError, match="production callback"):
                getattr(connection, method)(("127.0.0.1", 8000))
        with httpx.Client(proxy="http://127.0.0.1:7890") as client, pytest.raises(AssertionError, match="production callback"):
            client.get("http://127.0.0.1:8000/mcp/wiki/mcp")
        assert len(violations) == 5

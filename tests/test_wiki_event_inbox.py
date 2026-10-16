"""Durable filesystem intake must survive ambiguous commits and fail closed.

These faults use temporary vaults and their real SQLite/inotify boundary. No
model, user corpus, or running service participates in the recovery checks.
"""

import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.event_inbox import EventInbox
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    state = tmp_path / "state"
    vault_id = str(uuid.uuid4())
    stores = []

    def open_store():
        store = WikiStore(root, state, vault_id)
        assert store.events.mode == "inotify"
        stores.append(store)
        return store

    yield open_store(), open_store
    for store in stores:
        store.close()


@pytest.fixture
def inbox(vault):
    store, _open_store = vault
    return store.events.journal


def sql_rows(inbox, statement, parameters=()):
    with sqlite3.connect(inbox.db_path) as db:
        return db.execute(statement, parameters).fetchall()


def new_record(path="Library/note.md"):
    return {"type": "kernel", "path": path, "mask": 0x200, "cookie": 0}


def receive(inbox, records=None, *, batch_id=None):
    return inbox.receive(
        [new_record()] if records is None else records,
        "synthetic-observer", "synthetic-root-generation",
        armed=False, batch_id=batch_id,
    )


def original_note(store):
    path = store.root / "Library/note.md"
    path.write_bytes(b"# Original\nThe first identity's source judgment.\n")
    store.refresh()
    return path, store.read("Library/note.md")


def assert_retired(store, original):
    for identifier in (original["id"], original["metadata"]["uid"]):
        with pytest.raises(WikiError) as failure:
            store.read(identifier)
        assert failure.value.status == 404


@pytest.mark.parametrize("phase", ["after_arm", "before_receive"])
def test_interrupted_kernel_intake_cannot_rebind_the_old_identity(vault, monkeypatch, phase):
    store, open_store = vault
    path, original = original_note(store)
    replacement_raw = b"# Replacement\nAn unrelated source judgment.\n"
    journal = store.events.journal
    arm = journal.arm

    def interrupt_after_arm(epoch):
        arm(epoch)
        raise OSError("synthetic exit after armed receipt protection")

    def interrupt_before_receive(*_args, **_kwargs):
        raise OSError("synthetic exit after kernel read, before durable receive")

    with path.open("rb"):
        old_inode = path.stat().st_ino
        path.unlink()
        path.write_bytes(replacement_raw)
        assert path.stat().st_ino != old_inode
        with monkeypatch.context() as patch:
            patch.setattr(journal, "arm" if phase == "after_arm" else "receive",
                          interrupt_after_arm if phase == "after_arm" else interrupt_before_receive)
            with pytest.raises(WikiError) as failure:
                store.synchronize()
            assert failure.value.status == 503
        assert sql_rows(journal, "SELECT armed_epoch FROM filesystem_state")[0][0]
        assert not sql_rows(journal, "SELECT seq FROM filesystem_inbox WHERE status='pending'")
        assert sql_rows(journal, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [(original["path"],)]
        store.close()
        for _attempt in range(2):
            with pytest.raises(WikiError, match="uncertain|interrupted") as failure:
                open_store()
            assert failure.value.status == 503
    assert path.read_bytes() == replacement_raw


@pytest.mark.parametrize("entry", ["synchronize", "refresh"])
def test_delete_receipt_is_committed_before_the_index_application(vault, monkeypatch, entry):
    store, open_store = vault
    path, original = original_note(store)
    replacement_raw = b"# Replacement\nReceipt and binding commit separately.\n"
    observed = []

    def inspect_then_fail(*_args, **_kwargs):
        # This independent reader cannot see the uncommitted index changes.
        # It must already see the durable deletion receipt.
        with sqlite3.connect(store.db_path) as db:
            payloads = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM filesystem_inbox WHERE status='pending' ORDER BY seq"
            )]
            assert any(record.get("path") == original["path"] and record.get("mask", 0) & 0x200
                       for payload in payloads for record in payload["records"])
            assert db.execute("SELECT path FROM document_identities WHERE id=?", (original["id"],)).fetchone() == (original["path"],)
        observed.append(True)
        raise OSError("synthetic application failure after independently committed receipt")

    with path.open("rb"):
        path.unlink()
        path.write_bytes(replacement_raw)
        with monkeypatch.context() as patch:
            patch.setattr(store, "_index", inspect_then_fail)
            with pytest.raises(WikiError) as failure:
                getattr(store, entry)()
            assert failure.value.status == 503
        assert observed
        store.close()
        store = open_store()
        store.refresh()
    assert store.read(original["path"])["id"] != original["id"]
    assert_retired(store, original)
    assert path.read_bytes() == replacement_raw


@pytest.mark.parametrize("entry", ["synchronize", "refresh"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("recovery", ["same_store", "reopen"])
def test_application_commit_result_is_resolved_from_durable_state(vault, monkeypatch, entry, committed, recovery):
    store, open_store = vault
    path, original = original_note(store)
    replacement_raw = b"# Replacement\nOne identity across every recovery attempt.\n"
    with sqlite3.connect(store.db_path) as db:
        watermark = db.execute("SELECT applied_seq FROM filesystem_state").fetchone()[0]
    connect = store.connect
    faults = []

    @contextlib.contextmanager
    def ambiguous_commit():
        with connect() as db:
            yield db
            new_watermark = db.execute("SELECT applied_seq FROM filesystem_state").fetchone()[0]
            if db.in_transaction and new_watermark > watermark and not faults:
                faults.append(True)
                if committed:
                    db.commit()
                raise sqlite3.OperationalError("synthetic application COMMIT result unavailable")

    with path.open("rb"):
        path.unlink()
        path.write_bytes(replacement_raw)
        with monkeypatch.context() as patch:
            patch.setattr(store, "connect", ambiguous_commit)
            with pytest.raises(WikiError) as failure:
                getattr(store, entry)()
            assert failure.value.status == 503
        assert faults == [True]
        with sqlite3.connect(store.db_path) as db:
            first_id = db.execute("SELECT id FROM documents WHERE path=?", (original["path"],)).fetchone()[0]
            pending = db.execute("SELECT count(*) FROM filesystem_inbox WHERE status='pending'").fetchone()[0]
        assert (first_id != original["id"]) is committed
        assert bool(pending) is not committed
        if recovery == "reopen":
            store.close()
            store = open_store()
        store.refresh()
        replacement_id = store.read(original["path"])["id"]
        assert replacement_id != original["id"]
        if committed:
            assert replacement_id == first_id
        for _attempt in range(2):
            store.synchronize()
            store.refresh()
            assert store.read(original["path"])["id"] == replacement_id
        assert_retired(store, original)
    assert path.read_bytes() == replacement_raw


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("recovery", ["same_store", "reopen"])
def test_ack_commit_failure_never_replays_the_applied_delete(vault, monkeypatch, committed, recovery):
    store, open_store = vault
    path, original = original_note(store)
    replacement_raw = b"# Replacement\nAcknowledgement cannot end this new binding.\n"
    with sqlite3.connect(store.db_path) as db:
        watermark = db.execute("SELECT applied_seq FROM filesystem_state").fetchone()[0]
    commit = EventInbox.commit
    faults = []

    def ambiguous_ack_commit(db):
        acknowledgement = db.execute(
            "SELECT 1 FROM filesystem_inbox WHERE seq>? AND status='applied' AND payload='' LIMIT 1",
            (watermark,),
        ).fetchone()
        if acknowledgement and not faults:
            faults.append(True)
            if committed:
                commit(db)
            raise sqlite3.OperationalError("synthetic acknowledgement COMMIT result unavailable")
        commit(db)

    with path.open("rb"):
        path.unlink()
        path.write_bytes(replacement_raw)
        with monkeypatch.context() as patch:
            patch.setattr(EventInbox, "commit", staticmethod(ambiguous_ack_commit))
            with pytest.raises(WikiError) as failure:
                store.synchronize()
            assert failure.value.status == 503
        assert faults == [True]
        with sqlite3.connect(store.db_path) as db:
            committed_id = db.execute("SELECT id FROM documents WHERE path=?", (original["path"],)).fetchone()[0]
            assert committed_id != original["id"]
            assert not db.execute("SELECT 1 FROM filesystem_inbox WHERE status='pending'").fetchone()
            payload = db.execute("SELECT payload FROM filesystem_inbox WHERE seq>?", (watermark,)).fetchone()[0]
            assert bool(payload) is not committed
        if recovery == "reopen":
            store.close()
            store = open_store()
        for _attempt in range(2):
            store.refresh()
            assert store.read(original["path"])["id"] == committed_id
        assert_retired(store, original)
    assert path.read_bytes() == replacement_raw


def test_receive_resolves_commit_that_succeeded_before_raising(inbox, monkeypatch):
    epoch = "armed-synthetic-observer"
    inbox.arm(epoch)
    commit = EventInbox.commit
    failures = []

    def uncertain_commit(db):
        row = db.execute("SELECT 1 FROM filesystem_inbox WHERE batch_id='receipt-after-commit'").fetchone()
        commit(db)
        if row and not failures:
            failures.append(True)
            raise sqlite3.OperationalError("synthetic lost receive COMMIT response")

    with monkeypatch.context() as patch:
        patch.setattr(EventInbox, "commit", staticmethod(uncertain_commit))
        sequence = inbox.receive([new_record()], epoch, "generation", batch_id="receipt-after-commit")
    assert failures == [True]
    assert sql_rows(inbox, "SELECT armed_epoch FROM filesystem_state") == [(None,)]
    assert [row[0]["seq"] for row in inbox.pending()] == [sequence]
    assert inbox.receive([new_record()], epoch, "generation", armed=False, batch_id="receipt-after-commit") == sequence


def test_failed_receive_commit_retains_armed_protection(inbox, monkeypatch):
    epoch = "armed-synthetic-observer"
    inbox.arm(epoch)
    commit = EventInbox.commit

    def fail_receive_commit(db):
        if db.execute("SELECT 1 FROM filesystem_inbox WHERE batch_id='receipt-before-commit'").fetchone():
            raise sqlite3.OperationalError("synthetic failed receive COMMIT")
        commit(db)

    with monkeypatch.context() as patch:
        patch.setattr(EventInbox, "commit", staticmethod(fail_receive_commit))
        with pytest.raises(sqlite3.OperationalError, match="failed receive COMMIT"):
            inbox.receive([new_record()], epoch, "generation", batch_id="receipt-before-commit")
    assert sql_rows(inbox, "SELECT armed_epoch FROM filesystem_state") == [(epoch,)]
    assert not sql_rows(inbox, "SELECT seq FROM filesystem_inbox WHERE batch_id='receipt-before-commit'")
    with pytest.raises(WikiError) as failure:
        EventInbox(inbox.db_path, inbox.root, inbox.vault_id)
    assert failure.value.status == 503


def test_arm_resolves_commit_that_succeeded_before_raising(inbox, monkeypatch):
    epoch = "arm-after-commit"
    commit = EventInbox.commit
    failures = []

    def uncertain_commit(db):
        armed = db.execute("SELECT armed_epoch FROM filesystem_state").fetchone()[0]
        commit(db)
        if armed == epoch and not failures:
            failures.append(True)
            raise sqlite3.OperationalError("synthetic lost arm COMMIT response")

    with monkeypatch.context() as patch:
        patch.setattr(EventInbox, "commit", staticmethod(uncertain_commit))
        inbox.arm(epoch)
    assert failures == [True]
    sequence = inbox.receive([new_record()], epoch, "generation")
    assert [row[0]["seq"] for row in inbox.pending()] == [sequence]


@pytest.mark.parametrize("acknowledged", [False, True])
def test_repeated_batch_identity_is_idempotent_but_immutable(inbox, acknowledged):
    records = [new_record()]
    sequence = receive(inbox, records, batch_id="immutable-batch")
    assert receive(inbox, records, batch_id="immutable-batch") == sequence
    with inbox.connect() as db:
        inbox.mark_applied(db, [sequence])
    if acknowledged:
        inbox.acknowledge([sequence])
    before = sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE batch_id='immutable-batch'")
    assert receive(inbox, records, batch_id="immutable-batch") == sequence
    assert inbox.pending() == []
    assert inbox.applied([sequence])
    with pytest.raises(WikiError, match="different payload") as failure:
        receive(inbox, [new_record("Library/replacement.md")], batch_id="immutable-batch")
    assert failure.value.status == 503
    assert sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE batch_id='immutable-batch'") == before


def test_application_order_and_exact_ack_do_not_discard_later_receipts(inbox):
    first = receive(inbox, batch_id="first")
    second = receive(inbox, [new_record("Library/second.md")], batch_id="second")
    third = receive(inbox, [new_record("Library/third.md")], batch_id="third")
    with pytest.raises(WikiError, match="order") as failure:
        with inbox.connect() as db:
            inbox.mark_applied(db, [second])
    assert failure.value.status == 503
    with pytest.raises(WikiError, match="unapplied") as failure:
        inbox.acknowledge([first])
    assert failure.value.status == 503
    assert [row[0]["seq"] for row in inbox.pending()] == [first, second, third]
    with inbox.connect() as db:
        inbox.mark_applied(db, [first])
    inbox.acknowledge([first])
    assert [row[0]["seq"] for row in inbox.pending()] == [second, third]
    assert sql_rows(inbox, "SELECT payload FROM filesystem_inbox WHERE seq=?", (first,)) == [("",)]
    assert all(row[0] for row in sql_rows(inbox, "SELECT payload FROM filesystem_inbox WHERE seq IN (?,?)", (second, third)))
    with inbox.connect() as db:
        inbox.mark_applied(db, [second, third])
    inbox.acknowledge([second])
    assert sql_rows(inbox, "SELECT payload FROM filesystem_inbox WHERE seq=?", (second,)) == [("",)]
    assert sql_rows(inbox, "SELECT payload FROM filesystem_inbox WHERE seq=?", (third,))[0][0]
    assert sql_rows(inbox, "SELECT received_seq,applied_seq FROM filesystem_state") == [(third, third)]


@pytest.mark.parametrize("conflict", ["hash", "json", "protocol", "root", "vault", "generation", "records"])
def test_corrupt_receipts_are_not_treated_as_an_empty_inbox(inbox, conflict):
    sequence = receive(inbox, batch_id="corruption-control")
    payload_text, digest = sql_rows(inbox, "SELECT payload,payload_hash FROM filesystem_inbox WHERE seq=?", (sequence,))[0]
    payload = json.loads(payload_text)
    if conflict == "hash":
        digest = "0" * 64
    elif conflict == "json":
        payload_text = "{"
    else:
        key, value = {
            "protocol": ("protocol", 999),
            "root": ("root", "/another-vault"),
            "vault": ("vault_id", "another-vault-identity"),
            "generation": ("root_generation", "another-root-generation"),
            "records": ("records", {}),
        }[conflict]
        payload[key] = value
        payload_text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if conflict != "hash":
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
    with sqlite3.connect(inbox.db_path) as db:
        db.execute("UPDATE filesystem_inbox SET payload=?,payload_hash=? WHERE seq=?", (payload_text, digest, sequence))
    before = sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,))
    with pytest.raises(WikiError) as failure:
        inbox.pending()
    assert failure.value.status == 503
    assert sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,)) == before


@pytest.mark.parametrize("field,value", [("protocol", 999), ("root", "/another-vault"), ("vault_id", "another-vault-identity"), ("applied_seq", 10)])
def test_invalid_ledger_binding_or_watermark_fails_closed(inbox, field, value):
    sequence = receive(inbox, batch_id="binding-control")
    # Field names are a fixed parametrized allowlist, never external input.
    with sqlite3.connect(inbox.db_path) as db:
        db.execute(f"UPDATE filesystem_state SET {field}=?", (value,))
    before = sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,))
    with pytest.raises(WikiError) as failure:
        inbox.pending()
    assert failure.value.status == 503
    assert sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,)) == before


def test_applied_watermark_cannot_silently_hide_a_pending_receipt(inbox):
    sequence = receive(inbox, batch_id="watermark-control")
    with sqlite3.connect(inbox.db_path) as db:
        db.execute("UPDATE filesystem_state SET applied_seq=?", (sequence,))
    with pytest.raises(WikiError, match="watermark|sequence") as failure:
        inbox.pending()
    assert failure.value.status == 503
    assert sql_rows(inbox, "SELECT status FROM filesystem_inbox WHERE seq=?", (sequence,)) == [("pending",)]


def test_replacement_recovers_in_independent_process_without_transferring_sources(vault, monkeypatch):
    store, _open_store = vault
    path, original = original_note(store)
    consumer_path = store.root / "Library/consumer.md"
    consumer_path.write_bytes(b"# Consumer\n")
    store.refresh()
    consumer = store.read("Library/consumer.md")
    WikiCuration(store).organize(
        consumer["path"], consumer["revision"], "Depends on the original judgment.", [], [],
        [{"doc": original["id"], "revision": original["revision"], "location": "L1"}],
        "human", "test",
    )
    consumer_raw = consumer_path.read_bytes()
    replacement_raw = b"# Replacement\nA different judgment across process restart.\n"

    def fail_application(*_args, **_kwargs):
        raise OSError("synthetic process exit after receiving deletion")

    with path.open("rb"):
        path.unlink()
        path.write_bytes(replacement_raw)
        with monkeypatch.context() as patch:
            patch.setattr(store, "_index", fail_application)
            with pytest.raises(WikiError) as failure:
                store.synchronize()
            assert failure.value.status == 503
        store.close()
        script = """
import json, sys
from pathlib import Path
from server.wiki.store import WikiError, WikiStore
root, state, vault_id, old_id, old_uid, consumer_id = sys.argv[1:]
result = {"retired": [], "replacement_ids": []}
for attempt in range(2):
    store = WikiStore(Path(root), Path(state), vault_id)
    store.refresh()
    for identifier in (old_id, old_uid):
        try:
            store.read(identifier)
        except WikiError as error:
            result["retired"].append(error.status)
        else:
            result["retired"].append(200)
    result["replacement_ids"].append(store.read("Library/note.md")["id"])
    consumer = store.read(consumer_id)
    result["stale"] = consumer["stale"]
    result["source_refs"] = consumer["metadata"]["source_refs"]
    store.close()
print(json.dumps(result))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(store.root), str(store.state), store.vault_id,
             original["id"], original["metadata"]["uid"], consumer["id"]],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            timeout=30, check=True,
        )
    result = json.loads(completed.stdout)
    assert result["retired"] == [404] * 4
    assert len(set(result["replacement_ids"])) == 1
    assert result["replacement_ids"][0] != original["id"]
    assert result["stale"]
    assert any(ref["doc"] == original["id"] and ref["status"] == "missing" for ref in result["source_refs"])
    assert path.read_bytes() == replacement_raw
    assert consumer_path.read_bytes() == consumer_raw

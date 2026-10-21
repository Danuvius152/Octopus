"""Durable filesystem intake must survive ambiguous commits and fail closed.

These faults use temporary vaults and their real SQLite/inotify boundary. No
model, user corpus, or running service participates in the recovery checks.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.service_health import ServiceHealth
from server.wiki.curation import WikiCuration
from server.wiki.store import WikiError, WikiStore
from tests.test_service_runtime import installation_settings
from tests.test_wiki_storage import registration_process


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    state = tmp_path / "state"
    vault_id = str(uuid.uuid4())
    stores = []
    health = ServiceHealth()

    def open_store(*, create=False):
        store = WikiStore(root, state, vault_id, health=health, create=create)
        assert store.events.mode == "inotify"
        stores.append(store)
        return store

    yield open_store(create=True), open_store
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


@pytest.mark.parametrize("action", ["arm", "receive", "application", "ack"])
@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_exits_and_only_a_restart_reconciles_receipts(tmp_path, action, committed):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    operation = """
target=root/'Library/note.md'
target.parent.mkdir()
target.write_bytes(b'# Original\\nThe original judgment.\\n')
registry.configure(None,root,'curator')
store=registry.store(None)
original=store.read('Library/note.md')
with store.connect() as db:
    watermark=db.execute('SELECT applied_seq FROM filesystem_state').fetchone()[0]
proof={'id':original['id'],'uid':original['metadata']['uid'],'vault_id':store.vault_id,'watermark':watermark}
marker.with_suffix('.before').write_text(json.dumps(proof))
action=ACTION
committed=COMMITTED
connect=sqlite3.connect
class FaultyConnection(sqlite3.Connection):
    def commit(self):
        if self.in_transaction:
            state=self.execute('SELECT armed_epoch,applied_seq FROM filesystem_state').fetchone()
            selected=(action=='arm' and state[0]=='fault-epoch'
                or action=='receive' and self.execute("SELECT 1 FROM filesystem_inbox WHERE batch_id='retained-batch'").fetchone()
                or action=='application' and state[1]>watermark
                or action=='ack' and self.execute("SELECT 1 FROM filesystem_inbox WHERE seq>? AND payload=''",(watermark,)).fetchone())
            if selected:
                marker.with_suffix('.commit-started').touch()
                if committed: super().commit()
                raise OSError('synthetic lost authoritative commit response')
        return super().commit()
def faulty_connect(*args,**kwargs):
    kwargs['factory']=FaultyConnection
    return connect(*args,**kwargs)
sqlite3.connect=faulty_connect
try:
    if action=='arm':
        store.events.journal.arm('fault-epoch')
    else:
        with target.open('rb'):
            target.unlink()
            target.write_bytes(b'# Replacement\\nA different judgment.\\n')
            if action=='receive':
                journal=store.events.journal
                journal.arm('fault-epoch')
                journal.receive([{'type':'kernel','path':'Library/note.md','mask':0x200,'cookie':0}],
                    'fault-epoch',store.events.root_generation,batch_id='retained-batch')
            else:
                store.synchronize()
except Exception:
    pass
marker.touch()
""".replace("ACTION", repr(action)).replace("COMMITTED", repr(committed))
    failed = registration_process(config_file, marker, operation)
    assert marker.with_suffix(".commit-started").is_file(), failed.stderr
    assert failed.returncode == 70, failed.stderr
    assert not marker.exists()
    proof = json.loads(marker.with_suffix(".before").read_text())
    database = Path(config.wiki_dir) / "index.sqlite3"
    with sqlite3.connect(database) as db:
        current_id = db.execute("SELECT id FROM documents WHERE path='Library/note.md'").fetchone()[0]
        armed = db.execute("SELECT armed_epoch FROM filesystem_state").fetchone()[0]
        receipts = db.execute("SELECT seq,batch_id,payload_hash,status,payload FROM filesystem_inbox WHERE seq>?", (proof["watermark"],)).fetchall()
    if action == "arm":
        assert armed == ("fault-epoch" if committed else None)
        assert not receipts and current_id == proof["id"]
    elif action == "receive":
        assert armed == (None if committed else "fault-epoch")
        assert bool(receipts) is committed
        assert current_id == proof["id"]
    else:
        assert armed is None and receipts
        assert (current_id != proof["id"]) is (committed or action == "ack")
        assert all(row[3] == ("applied" if committed or action == "ack" else "pending") for row in receipts)
        if action == "ack":
            assert all(bool(row[4]) is not committed for row in receipts)
    blocked = action == "arm" and committed or action == "receive" and not committed
    restarted = registration_process(config_file, marker, """
from server.wiki.store import WikiError
from server.wiki.recovery import Metadata
result={'ids':[],'retired':[]}
proof=json.loads(marker.with_suffix('.before').read_text())
for attempt in range(2):
    try:
        store=registry.store(None)
        store.refresh()
        result['ids'].append(store.read('Library/note.md')['id'])
        for identifier in (proof['id'],proof['uid']):
            try: store.read(identifier)
            except WikiError as error: result['retired'].append(error.status)
            else: result['retired'].append(200)
    except WikiError as error:
        result['blocked']=error.status
        result['filesystem']=Metadata(registry,None).snapshot()['filesystem']
    registry.close()
result['vault_id']=registry.config(None)['vault_id']
marker.write_text(json.dumps(result))
""")
    assert restarted.returncode == 0, restarted.stderr
    result = json.loads(marker.read_text())
    assert result["vault_id"] == proof["vault_id"]
    if blocked:
        assert result["blocked"] == 503 and result["ids"] == []
        assert result["filesystem"]["armed_epoch"] == "fault-epoch"
    else:
        assert len(set(result["ids"])) == 1
        if action == "arm":
            assert result["ids"] == [proof["id"]] * 2
        else:
            assert result["ids"][0] != proof["id"]
            assert result["retired"] == [404] * 4
            if committed and action == "application" or action == "ack":
                assert result["ids"][0] == current_id
    with sqlite3.connect(database) as db:
        retained = db.execute("SELECT seq,batch_id,payload_hash,status,payload FROM filesystem_inbox WHERE seq>?", (proof["watermark"],)).fetchall()
    assert [row[:3] for row in retained] == [row[:3] for row in receipts]
    if not blocked:
        assert all(row[3] == "applied" for row in retained)
        # An applied receipt need not be acknowledged by the next observer.
        # Its retained payload must never be replayed against the new identity.
        if action == "ack" and not committed:
            assert [row[4] for row in retained] == [row[4] for row in receipts]
    expected = b"# Original\nThe original judgment.\n" if action == "arm" else b"# Replacement\nA different judgment.\n"
    assert (Path(config_file).parent / "vault/Library/note.md").read_bytes() == expected


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
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute(f"UPDATE filesystem_state SET {field}=?", (value,))
    before = sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,))
    with pytest.raises(WikiError) as failure:
        inbox.pending()
    assert failure.value.status == 503
    assert sql_rows(inbox, "SELECT * FROM filesystem_inbox WHERE seq=?", (sequence,)) == before


def test_applied_watermark_cannot_silently_hide_a_pending_receipt(inbox):
    sequence = receive(inbox, batch_id="watermark-control")
    with sqlite3.connect(inbox.db_path) as db:
        db.execute("PRAGMA recursive_triggers=ON")
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
from server.service_health import ServiceHealth
from server.wiki.store import WikiError, WikiStore
from tests.test_service_runtime import installation_settings
from tests.test_wiki_storage import registration_process
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

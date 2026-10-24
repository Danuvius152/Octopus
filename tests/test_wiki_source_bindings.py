"""Historical source facts survive graph loss, path reuse and exact-side recovery."""

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.wiki import history as history_module
from server.wiki import source_bindings
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


def source(path="a.md", *, label="one", location="L1", recorded="old"):
    return (f"---\nsources:\n  - resource: {path}\n    id: {label}\n"
            f"    location: {location}\n    revision: {recorded}\n---\n# Consumer\nA conditional claim.\n").encode()


def setup_replacement(history):
    history.write("a.md", b"# A\nOriginal evidence.\n", None, "human", "create")
    a = history.store.read("a.md")
    raw = source(recorded=a["revision"])
    receipt = history.write("consumer.md", raw, None, "human", "create")
    consumer = history.store.read("consumer.md")
    history.store.path("a.md").rename(history.store.path("moved.md"))
    history.store.refresh()
    history.write("a.md", b"# B\nDifferent evidence.\n", None, "human", "replacement")
    return a, consumer, raw, receipt


def head(store, doc):
    with store.connect() as db:
        return source_bindings.current(db, doc)


def dependency(store, doc="consumer.md"):
    return store.read(doc)["metadata"]["source_refs"][0]


def reopen(store):
    store.close()
    return WikiStore(store.root, store.state, store.vault_id, health=store.health)


def test_bound_source_survives_complete_derived_graph_loss_and_reopen(history):
    a, consumer, _, _ = setup_replacement(history)
    before = head(history.store, consumer["id"])
    with history.store.connect() as db:
        db.execute("DROP TABLE wiki_edges")
        db.execute("DROP TABLE wiki_edge_lookups")
    current = reopen(history.store)
    try:
        assert dependency(current)["doc"] == a["id"]
        assert head(current, consumer["id"]) == before
        assert current.graph(consumer["id"])["edges"][0]["to"] == a["id"]
    finally:
        current.close()


def test_equal_bytes_snapshot_unknown_and_exact_history_side_are_distinct_observations(history):
    a, consumer, raw, original = setup_replacement(history)
    proved = head(history.store, consumer["id"])
    snapshot = uuid.uuid4().hex
    folder = history.state / "snapshots"
    folder.mkdir(exist_ok=True)
    (folder / (snapshot + ".json")).write_text(json.dumps({
        "id": snapshot, "files": {"consumer.md": history.blob(raw)},
    }))
    history.restore_snapshot(snapshot, ["consumer.md"], {"consumer.md": consumer["revision"]})
    unknown = head(history.store, consumer["id"])
    assert proved["revision"] == unknown["revision"]
    assert proved["id"] != unknown["id"]
    assert dependency(history.store)["doc"] is None
    assert dependency(history.store)["status"] == "unknown"
    history.write("consumer.md", raw.replace(b"L1", b"L2"), consumer["revision"], "human", "locator edit")
    assert dependency(history.store)["status"] == "unknown"
    current = history.store.read("consumer.md")
    history.restore(original["change_id"], current["revision"], "after", "human")
    assert dependency(history.store)["doc"] == a["id"]
    with history.store.connect() as db:
        assert source_bindings.observation(db, unknown["id"]) == unknown


def test_history_restore_copy_reowns_proof_and_preserves_target(history):
    a, consumer, raw, original = setup_replacement(history)
    history.store.path("consumer.md").rename(history.store.path("other.md"))
    history.store.refresh()
    history.restore(original["change_id"], None, "after", "human")
    copy = history.store.read("consumer.md")
    assert copy["id"] != consumer["id"]
    assert history.store.path("consumer.md").read_bytes() == raw
    assert dependency(history.store)["doc"] == a["id"]
    assert dependency(history.store, "other.md")["doc"] == a["id"]
    with history.store.connect() as db:
        entry = head(history.store, copy["id"])["entries"][0]
        assert db.execute("SELECT source_doc FROM source_bindings WHERE id=?", (entry["binding_id"],)).fetchone()[0] == copy["id"]


def test_history_restore_copy_preserves_target_confirmed_after_selected_observation(history):
    raw = source("future.md")
    original = history.write("consumer.md", raw, None, "human", "create before target")
    consumer = history.store.read("consumer.md")
    selected = head(history.store, consumer["id"])
    assert selected["entries"][0]["policy"] == "unresolved"
    history.write("future.md", b"# A\nOriginal evidence.\n", None, "human", "create target")
    a = history.store.read("future.md")
    assert dependency(history.store)["doc"] == a["id"]
    history.store.path("future.md").rename(history.store.path("moved.md"))
    history.store.refresh()
    history.write("future.md", b"# B\nDifferent evidence.\n", None, "human", "reuse path")
    b = history.store.read("future.md")
    assert b["id"] != a["id"]
    history.store.path("consumer.md").rename(history.store.path("other.md"))
    history.store.refresh()
    history.restore(original["change_id"], None, "after", "human")
    copy = history.store.read("consumer.md")
    assert copy["id"] != consumer["id"]
    assert history.store.path("consumer.md").read_bytes() == raw
    assert dependency(history.store)["doc"] == a["id"]
    assert dependency(history.store, "other.md")["doc"] == a["id"]
    with history.store.connect() as db:
        # The old occurrence remains unresolved evidence; the durable binding
        # records its later confirmation, which a copy must preserve.
        assert source_bindings.observation(db, selected["id"]) == selected
        assert source_bindings.change_plan(db, original["change_id"], "after") == selected
        copied = source_bindings.current(db, copy["id"])["entries"][0]
        assert copied["binding_id"] != selected["entries"][0]["binding_id"]
        assert db.execute("SELECT source_doc,target_doc FROM source_bindings WHERE id=?",
                          (copied["binding_id"],)).fetchone()[:] == (copy["id"], a["id"])
        db.execute("DROP TABLE wiki_edges")
        db.execute("DROP TABLE wiki_edge_lookups")
    current = reopen(history.store)
    try:
        assert dependency(current)["doc"] == a["id"]
        assert dependency(current, "other.md")["doc"] == a["id"]
        with current.connect() as db:
            assert source_bindings.observation(db, selected["id"]) == selected
    finally:
        current.close()


@pytest.mark.parametrize("damage", ["missing", "owner", "selector"])
def test_history_restore_copy_rejects_missing_or_mismatched_binding_proof(history, damage):
    _, consumer, _, original = setup_replacement(history)
    history.write("unrelated.md", source("other-target.md"), None, "human", "unrelated source")
    unrelated = head(history.store, history.store.read("unrelated.md")["id"])
    history.store.path("consumer.md").rename(history.store.path("other.md"))
    history.store.refresh()
    with history.store.connect() as db:
        selected = source_bindings.change_plan(db, original["change_id"], "after")
        if damage == "missing":
            selected["entries"][0]["binding_id"] = uuid.uuid4().hex
        elif damage == "owner":
            selected["source_doc"] = unrelated["source_doc"]
        else:
            # Same owner, different selector: a valid proof for a different
            # reference must not authorize this occurrence.
            history_entry = unrelated["entries"][0]
            selected["entries"][0]["binding_id"] = history_entry["binding_id"]
            selected["source_doc"] = unrelated["source_doc"]
        db.execute("UPDATE change_source_bindings SET after_plan=? WHERE change_id=?",
                   (source_bindings.encoded(selected), original["change_id"]))
    with pytest.raises(WikiError, match="proof") as rejected:
        history.restore(original["change_id"], None, "after", "human")
    assert rejected.value.status == 503
    assert not history.store.path("consumer.md").exists()
    assert head(history.store, consumer["id"])["source_doc"] == consumer["id"]


@pytest.mark.parametrize("window", ["prepared", "replaced"])
def test_prepared_proposal_confirms_source_only_when_applied_and_recovers_exact_plan(history, monkeypatch, window):
    history.write("a.md", b"# A\nEvidence A.\n", None, "human", "create")
    history.write("b.md", b"# B\nEvidence B.\n", None, "human", "create")
    a, b = history.store.read("a.md"), history.store.read("b.md")
    history.write("consumer.md", source(), None, "human", "create")
    consumer = history.store.read("consumer.md")
    atomic = history_module.atomic

    def interrupted(path, raw, **kwargs):
        if path == history.store.path("consumer.md"):
            if window == "replaced":
                atomic(path, raw, **kwargs)
            raise RuntimeError("synthetic writer crash")
        return atomic(path, raw, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history_module, "atomic", interrupted)
        with pytest.raises(RuntimeError, match="synthetic writer crash"):
            history.write("consumer.md", source("b.md"), consumer["revision"], "human", "proposed change")
    with history.store.connect() as db:
        assert not db.execute("SELECT 1 FROM source_bindings WHERE source_doc=? AND target_doc=?", (consumer["id"], b["id"])).fetchone()
        pending = db.execute("SELECT id FROM changes WHERE status='prepared'").fetchone()[0]
        assert source_bindings.change_plan(db, pending, "after")["confirmed"] is False
    current = reopen(history.store)
    try:
        WikiHistory(current).recover()
        assert dependency(current)["doc"] == (a["id"] if window == "prepared" else b["id"])
        with current.connect() as db:
            status = db.execute("SELECT status FROM changes WHERE id=?", (pending,)).fetchone()[0]
            assert status == ("interrupted" if window == "prepared" else "applied")
            assert source_bindings.change_plan(db, pending, "after")["confirmed"] == (window == "replaced")
    finally:
        current.close()


def test_duplicate_reordered_occurrences_preserve_immutable_evidence(history):
    history.write("a.md", b"# A\nEvidence.\n", None, "human", "create")
    original = (b"---\nsources:\n  - resource: a.md\n    id: first\n    location: L1\n"
                b"  - resource: ./a.md\n    id: second\n    location: L2\n---\n# Consumer\n")
    history.write("consumer.md", original, None, "human", "create")
    consumer = history.store.read("consumer.md")
    before = head(history.store, consumer["id"])
    changed = original.replace(b"id: first\n    location: L1", b"id: second\n    location: L3").replace(
        b"id: second\n    location: L2", b"id: first\n    location: L4")
    history.write("consumer.md", changed, consumer["revision"], "human", "reorder references")
    after = head(history.store, consumer["id"])
    assert [item["label"] for item in before["entries"]] == ["first", "second"]
    assert [item["label"] for item in after["entries"]] == ["second", "first"]
    assert {item["occurrence_id"] for item in before["entries"]}.isdisjoint(item["occurrence_id"] for item in after["entries"])
    assert {item["binding_id"] for item in after["entries"]} <= {item["binding_id"] for item in before["entries"]}
    assert [item["target_doc"] for item in after["entries"]] == [item["target_doc"] for item in before["entries"]]
    with history.store.connect() as db:
        assert source_bindings.observation(db, before["id"]) == before
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE source_binding_observations SET occurrences='[]' WHERE id=?", (before["id"],))


def test_unresolved_source_retains_its_original_parent_when_source_moves(history):
    history.write("Library/consumer.md", source("evidence/future.md"), None, "human", "create")
    history.store.path("Other").mkdir()
    history.store.path("Library/consumer.md").rename(history.store.path("Other/consumer.md"))
    history.store.refresh()
    history.write("Other/evidence/future.md", b"# B\nWrong parent.\n", None, "human", "create")
    assert dependency(history.store, "Other/consumer.md")["doc"] is None
    history.write("Library/evidence/future.md", b"# A\nOriginal parent.\n", None, "human", "create")
    assert dependency(history.store, "Other/consumer.md")["doc"] == history.store.read("Library/evidence/future.md")["id"]


def test_unsupported_source_format_is_retained_without_promoting_derived_edges(history):
    a, consumer, raw, _ = setup_replacement(history)
    extra = raw.replace(b"---\n# Consumer", b"  - resource: a.md\n    id: missing\n---\n# Consumer")
    history.write("consumer.md", extra, consumer["revision"], "human", "add duplicate")
    store = history.store
    store.close()
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("DELETE FROM wiki_edges WHERE source_doc=? AND ordinal=1", (consumer["id"],))
        db.execute("PRAGMA user_version=14")
    retained = store.db_path.read_bytes()
    bodies = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*.md")}
    for _ in range(2):
        with pytest.raises(WikiError, match="retained") as refused:
            WikiStore(store.root, store.state, store.vault_id)
        assert refused.value.status == 503
        assert store.db_path.read_bytes() == retained
        assert {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*.md")} == bodies


@pytest.mark.parametrize("missing", list(source_bindings.TABLES))
def test_publication_rejects_missing_source_authority_without_recreating_it(history, missing):
    from server.storage_verification import verify_wiki

    setup_replacement(history)
    history.store.close()
    with sqlite3.connect(history.store.db_path) as db:
        db.execute("DROP TABLE " + missing)
        db.execute("DROP TABLE wiki_edges")
        before = list(db.iterdump())
    retained = history.store.db_path.read_bytes()
    with sqlite3.connect(history.store.db_path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        with pytest.raises(RuntimeError, match='current durable structure'):
            verify_wiki(db, {'root': str(history.store.root), 'vault_id': history.store.vault_id})
    assert history.store.db_path.read_bytes() == retained
    with sqlite3.connect(history.store.db_path) as db:
        assert list(db.iterdump()) == before
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='wiki_edges'").fetchone() is None


@pytest.mark.parametrize("missing_source_plan", [False, True])
def test_trash_restore_preserves_exact_captured_source_or_durable_unknown(history, tmp_path, missing_source_plan):
    a, consumer, raw, _ = setup_replacement(history)
    archive = tmp_path / "trash" / "saved"
    archive.mkdir(parents=True)
    (archive / "info.json").write_text(json.dumps({"path": "consumer.md"}))
    journal = history.state / "external-change.json"
    with history.locked():
        before = history.capture_external(["consumer.md"])
        deletion = {
            "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex, "before": before,
            "paths": ["consumer.md"], "actor": "human", "reason": "delete",
            "trash_root": str(archive.parent),
        }
        history_module.atomic(journal, json.dumps(deletion).encode())
        history.store.path("consumer.md").rename(archive / "file")
        history.finish_external(deletion)
        history.recover()
        assert not journal.exists()
        with history.store.connect() as db:
            if missing_source_plan:
                db.execute("UPDATE app_trash SET source_plan=NULL")
            restored = dict(db.execute("SELECT * FROM app_trash WHERE trash_id='saved'").fetchone())
        history_module.atomic(journal, json.dumps({
            "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex, "before": {},
            "paths": ["consumer.md"], "actor": "human", "reason": "trash/restore",
            "restored": restored, "trash_id": "saved",
        }).encode())
        (archive / "file").rename(history.store.path("consumer.md"))
    history.recover()
    assert not journal.exists()
    assert history.store.path("consumer.md").read_bytes() == raw
    assert history.store.read("consumer.md")["id"] == consumer["id"]
    restored_ref = dependency(history.store)
    assert restored_ref["doc"] == (None if missing_source_plan else a["id"])
    assert restored_ref["status"] == ("unknown" if missing_source_plan else "indexed")


def test_external_restore_without_source_plan_persists_selected_policy_before_first_index_and_replays(history, monkeypatch):
    _, consumer, raw, _ = setup_replacement(history)
    journal = history.state / "external-change.json"
    with history.locked():
        entry = {
            "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
            "before": history.capture_external(["consumer.md"]),
            "paths": ["consumer.md"], "actor": "human", "reason": "trash/restore",
            "restored": {"path": "consumer.md", "document_id": consumer["id"], "source_plan": None},
            "trash_id": "without-source-plan",
        }
        history_module.atomic(journal, json.dumps(entry).encode())
    policy = {"mode": "selected", "prior": None, "context": entry["id"] + ":external:consumer.md"}
    synchronize = history.store.synchronize
    indexed = []

    def index_with_durable_policy(*args, **kwargs):
        if kwargs.get("source_plans") is not None:
            assert json.loads(journal.read_text())["source_policies"] == {"consumer.md": policy}
            assert kwargs["source_plans"] == {"consumer.md": policy}
            indexed.append(True)
        return synchronize(*args, **kwargs)

    def interrupted(*args, **kwargs):
        assert indexed == [True]
        raise RuntimeError("crash after first external index")

    with monkeypatch.context() as patch:
        patch.setattr(history.store, "synchronize", index_with_durable_policy)
        patch.setattr(history, "record", interrupted)
        with pytest.raises(RuntimeError, match="crash after first external index"):
            history.recover()
    assert json.loads(journal.read_text())["source_policies"] == {"consumer.md": policy}
    first_indexed = head(history.store, consumer["id"])
    assert first_indexed["entries"][0]["policy"] == "unknown"
    current = reopen(history.store)
    try:
        WikiHistory(current).recover()
        assert current.path("consumer.md").read_bytes() == raw
        assert dependency(current)["doc"] is None
        assert dependency(current)["status"] == "unknown"
        change_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{entry['id']}:consumer.md").hex
        with current.connect() as db:
            assert source_bindings.observation(db, first_indexed["id"]) == first_indexed
            replayed = source_bindings.change_plan(db, change_id, "after")
            assert replayed == source_bindings.current(db, consumer["id"])
            assert replayed["entries"][0]["policy"] == "unknown"
        assert not journal.exists()
    finally:
        current.close()


def test_prepared_after_without_exact_source_plan_stays_unknown(history):
    _, consumer, raw, _ = setup_replacement(history)
    proposed = raw + b"An applied pre-upgrade edit.\n"
    with history.locked():
        receipt = history.record("consumer.md", raw, proposed, "human", "legacy preparation", "prepared")
        history.store.path("consumer.md").write_bytes(proposed)
    history.store.close()
    with sqlite3.connect(history.store.db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        # A missing exact source plan remains a current recovery obligation.
        db.execute("DELETE FROM change_source_bindings WHERE change_id=?", (receipt["id"],))
        assert db.execute("SELECT 1 FROM change_source_bindings WHERE change_id=?", (receipt["id"],)).fetchone() is None
    current = WikiStore(history.store.root, history.store.state, history.store.vault_id, health=history.store.health)
    try:
        WikiHistory(current).recover()
        assert dependency(current)["doc"] is None
        assert dependency(current)["status"] == "unknown"
        with current.connect() as db:
            assert db.execute("SELECT status FROM changes WHERE id=?", (receipt["id"],)).fetchone()[0] == "applied"
        assert current.path("consumer.md").read_bytes() == proposed
    finally:
        current.close()


@pytest.mark.parametrize("failed_table", ["source_binding_observations", "source_binding_head"])
def test_source_record_failure_rolls_back_new_facts_and_keeps_original_proof(history, failed_table, tmp_path):
    original, _consumer, _raw, _receipt = setup_replacement(history)
    store = history.store
    store.close()
    with sqlite3.connect(store.db_path) as db:
        before = list(db.iterdump())
    bodies = {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*.md")}
    marker = tmp_path / "admitted-after-migration-failure"
    program = """
import sqlite3,sys
from pathlib import Path
from urllib.parse import unquote,urlsplit
from server.wiki.history import WikiHistory
from server.wiki.store import WikiStore
root,state,vault_id,failed_table,marker=sys.argv[1:]
marker=Path(marker)
store=WikiStore(Path(root),Path(state),vault_id)
target=store.db_path.resolve()
connect=sqlite3.connect
def authorize(action,table,*args):
    if action==sqlite3.SQLITE_INSERT and table==failed_table:
        marker.with_suffix('.fault-started').touch()
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
def denied(*args,**kwargs):
    db=connect(*args,**kwargs)
    raw=str(args[0]);location=unquote(urlsplit(raw).path) if raw.startswith('file:') else raw
    if location!=':memory:' and Path(location).resolve()==target and 'mode=ro' not in raw:
        db.set_authorizer(authorize)
    return db
sqlite3.connect=denied
try:
    history=WikiHistory(store)
    raw=store.path('consumer.md').read_bytes()
    history.record('consumer.md',raw,raw,'human','uncommitted source record','applied')
except Exception: pass
marker.touch()
"""
    failed = subprocess.run(
        [sys.executable, "-c", program, str(store.root), str(store.state), store.vault_id, failed_table, str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert marker.with_suffix(".fault-started").is_file(), failed.stderr
    assert failed.returncode == 70, failed.stderr
    assert not marker.exists()
    with sqlite3.connect(store.db_path) as db:
        assert list(db.iterdump()) == before
    restarted = subprocess.run(
        [sys.executable, "-c", """
import json,sys
from pathlib import Path
from server.wiki.store import WikiStore
root,state,vault_id,original_id=sys.argv[1:]
observations=[]
for attempt in range(2):
    store=WikiStore(Path(root),Path(state),vault_id)
    store.refresh()
    assert store.read('consumer.md')['metadata']['source_refs'][0]['doc']==original_id
    assert store.read('a.md')['id']!=original_id
    with store.connect() as db:
        observations.append([tuple(row) for row in db.execute('SELECT source_doc,observation_id FROM source_binding_head ORDER BY source_doc')])
    store.close()
assert observations[0]==observations[1]
print(json.dumps({'observations':observations}))
""", str(store.root), str(store.state), store.vault_id, original["id"]],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert restarted.returncode == 0, restarted.stderr
    assert {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*.md")} == bodies


def test_vault_wide_external_move_preserves_proof_for_an_unchanged_related_note(history):
    a, consumer, raw, _ = setup_replacement(history)
    history.write("unrelated.md", b"# Unrelated\nOther evidence.\n", None, "human", "create")
    saved = head(history.store, consumer["id"])
    journal = history.state / "external-change.json"
    with history.locked():
        before = history.capture_external()
        history_module.atomic(journal, json.dumps({
            "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
            "before": before, "paths": None, "actor": "app:sapphire", "reason": "rename",
        }).encode())
        history.store.path("unrelated.md").rename(history.store.path("renamed.md"))
    history.recover()
    assert not journal.exists()
    assert history.store.path("consumer.md").read_bytes() == raw
    assert dependency(history.store)["doc"] == a["id"]
    assert head(history.store, consumer["id"]) == saved


def test_external_restore_uses_selected_observation_even_when_live_bytes_are_identical(history):
    _, consumer, raw, original = setup_replacement(history)
    snapshot = uuid.uuid4().hex
    folder = history.state / "snapshots"
    folder.mkdir(exist_ok=True)
    (folder / (snapshot + ".json")).write_text(json.dumps({
        "id": snapshot, "files": {"consumer.md": history.blob(raw)},
    }))
    history.restore_snapshot(snapshot, ["consumer.md"], {"consumer.md": consumer["revision"]})
    selected = head(history.store, consumer["id"])
    assert selected["entries"][0]["policy"] == "unknown"
    history.restore(original["change_id"], consumer["revision"], "after", "human")
    proved = head(history.store, consumer["id"])
    assert proved["entries"][0]["policy"] == "bound"
    before = history.capture_external(["consumer.md"])
    journal = history.state / "external-change.json"
    history_module.atomic(journal, json.dumps({
        "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex, "before": before,
        "paths": ["consumer.md"], "actor": "human", "reason": "trash/restore",
        "restored": {"path": "consumer.md", "document_id": consumer["id"],
                     "source_plan": json.dumps(selected)}, "trash_id": "same-bytes",
    }).encode())
    history.recover()
    assert not journal.exists()
    assert history.store.path("consumer.md").read_bytes() == raw
    assert dependency(history.store)["status"] == "unknown"
    restored = head(history.store, consumer["id"])
    assert restored["revision"] == proved["revision"] == selected["revision"]
    assert restored["id"] not in {proved["id"], selected["id"]}
    with history.store.connect() as db:
        receipt = db.execute("SELECT id FROM changes WHERE reason='trash/restore'").fetchone()
        assert receipt is not None
        before_plan = source_bindings.change_plan(db, receipt[0], "before")
        after_plan = source_bindings.change_plan(db, receipt[0], "after")
        assert before_plan["entries"][0]["target_doc"] is not None
        assert after_plan["entries"][0]["policy"] == "unknown"

"""A missing claim is not a new source; private maps cannot contain prose."""

import base64
from datetime import UTC, datetime, timedelta

import pytest

from server.wiki import material_proof, materials, origins, registry
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.retention import collect
from server.wiki.store import WikiError, revision
from server.wiki.tasks import WikiTask, tasks
from tests import test_wiki_jobs as job_tests
from tests import test_wiki_material_proof as map_tests

client = job_tests.client
jobs = job_tests.jobs
history = map_tests.history


def activate(jobs, job, document):
    session_id = "isolated-worker-" + job["id"]
    items = [{"doc": document["id"], "path": document["path"], "revision": document["revision"],
              "quality": document["quality"], "status": "pending"}]
    jobs.update(job["id"], status="running", session_id=session_id, items=items)
    task = WikiTask(session_id, None, registry.config(None)["curator_agent_id"], "maintain",
                    job_id=job["id"], writable=(document["path"],))
    tasks.record_read(task, jobs.store.read(document["id"], expected_revision=document["revision"]))
    return task


@pytest.mark.parametrize("source_kind", ["ingest", "paths"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("operation", ["normalize", "restructure"])
async def test_missing_current_claim_cannot_recertify_an_intermediate_on_another_job(
    jobs, source_kind, expired, restart, operation,
):
    raw, path = map_tests.source_text(0, "\r\n", "\ufeff").encode(), "Library/continuity.md"
    if source_kind == "ingest":
        first = jobs.maintain("first", raw.decode(), "Preserve original", [], path, [], "human")
        document = jobs.ingest(first["id"])
    else:
        WikiHistory(jobs.store).write(path, raw, None, "human", "Existing unproven source")
        first = jobs.maintain("first", "", "Preserve original", [path], None, [], "human")
        document = jobs.store.read(path)
    first_task = activate(jobs, first, document)
    WikiCuration(jobs.store).organize(path, document["revision"], "A generated summary.", [], [], None,
                                     "curator", "First source-preserving adaptation", task=first_task,
                                     frontmatter_patch=map_tests.PROFILE)
    document = jobs.store.read(path)
    middle = jobs.store.path(path).read_bytes()
    with jobs.store.connect() as db:
        original_state = origins.get(db, document["id"])
        assert material_proof.reconstruct(original_state, document["id"], middle) == raw
    completed_items = jobs.get(first["id"])["items"]
    completed_items[0].update(status="done", output=path, output_revision=document["revision"], index_complete=True)
    jobs.update(first["id"], status="completed", session_id=None, items=completed_items)
    with jobs.store.connect() as db:
        origins.put(db, document["id"], None)  # Isolated current-row loss; keep history.
    if expired:
        collect(WikiHistory(jobs.store), now=datetime.now(UTC) + timedelta(days=40))
        with jobs.store.connect() as db:
            rows = db.execute("SELECT o.* FROM change_material_origins o JOIN change_retention r ON r.change_id=o.change_id WHERE r.document_id=?", (document["id"],)).fetchall()
            assert rows and all(row["before_state"] is None and row["after_state"] is None for row in rows)
    if restart:
        registry.close()
        jobs = job_tests.WikiJobs(None)
    second = jobs.maintain("second", "", "Adjust metadata only", [path], None, [], "human")
    document = jobs.store.read(path)
    second_task = activate(jobs, second, document)
    curation = WikiCuration(jobs.store)
    if operation == "normalize":
        curation.organize(path, document["revision"], None, ["new-metadata"], [], None,
                          "curator", "Later metadata update", task=second_task,
                          frontmatter_patch=map_tests.PROFILE, reuse_existing_summary=True)
    else:
        blocks = curation.blocks(document["id"], expected_revision=document["revision"])["blocks"]
        curation.restructure(path, document["revision"], [block["id"] for block in blocks], [],
                             "curator", "Later metadata update", task=second_task, tags=["new-metadata"])
    current = jobs.store.read(document["id"])
    after = jobs.store.path(path).read_bytes()
    assert after != middle and current["id"] == document["id"]
    with jobs.store.connect() as db:
        state = origins.get(db, current["id"])
    reconstructed = material_proof.reconstruct(state, current["id"], after)
    assert reconstructed != middle, "Intermediate rendered bytes must not become a new source"
    if expired:
        assert reconstructed is None
    else:
        assert reconstructed == raw
        assert state["proof"]["source"] == original_state["proof"]["source"]
    with WikiHistory(jobs.store).locked():
        assert materials.find(jobs.store, middle) is None
    third = jobs.maintain("third", middle.decode(), "A separate submitted material", [], "Library/another.md", [], "human")
    assert third["status"] == "queued" and not third["items"]
    assert not jobs.store.path("Library/another.md").exists()


@pytest.mark.parametrize("source", [
    b"# Original\nBody must remain in the current file.\n",
    "\ufeff# Original\r\nBody must remain in the current file.\r\n".encode(),
    "\ufeff---\r\ncustom: original\r\n---\r\n# Original\r\nOriginal body.\r\n".encode(),
])
def test_forged_prefix_cannot_turn_literal_body_into_current_material(history, source):
    raw = b"# Different current document\nThis is not the submitted original body.\n"
    history.write("Library/current.md", raw, None, "human", "Synthetic current document")
    document = history.store.read("Library/current.md")
    state = material_proof.seeded(document["id"], source, {"kind": "synthetic"})
    state["revision"] = revision(raw)
    state["proof"]["source_prefix_size"] = len(source)
    state["proof"]["pieces"] = [{"literal": base64.b64encode(source).decode(),
                                   "size": len(source), "sha256": revision(source)}]
    state["proof"]["generated"] = [material_proof.region(raw, 0, len(raw))]
    assert material_proof.prefix_size(source) < len(source)
    assert material_proof.reconstruct(state, document["id"], raw) is None
    with history.store.connect() as db:
        origins.put(db, document["id"], state)
    with history.locked():
        assert materials.find(history.store, source) is None
    with history.store.connect() as db:
        assert origins.get(db, document["id"]) == state
    assert history.store.path(document["path"]).read_bytes() == raw
    assert "proof" not in history.store.read(document["id"])["metadata"]


def test_genuine_first_paths_source_remains_allowed_after_unproven_history(history):
    path = "Library/legacy.md"
    first = history.write(path, b"# Legacy\nFirst human wording.\n", None, "human", "Before any proof")
    raw = b"# Legacy\nCurrent human wording with its condition.\n"
    history.write(path, raw, first["revision"], "human", "Still before any proof")
    document = history.store.read(path)
    WikiCuration(history.store).organize(path, document["revision"], "Summary.", [], [], None,
                                        "curator", "First controlled source", frontmatter_patch=map_tests.PROFILE)
    current = history.store.read(document["id"])
    with history.store.connect() as db:
        state = origins.get(db, document["id"])
    assert state["proof"]["source"]["kind"] == "maintenance_revision"
    assert material_proof.reconstruct(state, current["id"], history.store.path(path).read_bytes()) == raw


@pytest.mark.parametrize("condition", ["invalid", "unknown", "different_revision"])
def test_history_recovery_cannot_revive_an_invalid_unknown_or_stale_claim(history, condition):
    path, raw = "Library/note.md", map_tests.source_text(0).encode()
    created = history.write(path, raw, None, "human", "Source")
    WikiCuration(history.store).organize(path, created["revision"], "Summary.", [], [], None,
                                        "curator", "Adapt", frontmatter_patch=map_tests.PROFILE)
    current = history.store.read(path)
    rendered = history.store.path(path).read_bytes()
    with history.store.connect() as db:
        state = origins.get(db, current["id"])
    if condition == "invalid":
        state = origins.invalidated(state, current["id"], current["revision"], "Explicit revocation")
    elif condition == "unknown":
        state = state | {"proof": state["proof"] | {"format": "future-unknown-format"}}
    if condition != "different_revision":
        with history.locked():
            history.write_locked(path, rendered, current["revision"], "human", "Historical claim", origin_state=state)
    with history.store.connect() as db:
        origins.put(db, current["id"], None)
    if condition == "different_revision":
        history.store.path(path).write_bytes(rendered + b"\nUnrecorded external edit.\n")
    current = history.store.read(path)
    WikiCuration(history.store).organize(path, current["revision"], None, ["metadata"], [], None,
                                        "curator", "Metadata cannot recertify", frontmatter_patch=map_tests.PROFILE,
                                        reuse_existing_summary=True)
    current = history.store.read(path)
    with history.store.connect() as db:
        recovered = origins.get(db, current["id"])
    assert recovered["status"] == "invalid" and recovered["proof"] == state["proof"]
    assert material_proof.reconstruct(recovered, current["id"], history.store.path(path).read_bytes()) is None
    with history.locked():
        assert materials.find(history.store, raw) is None


def test_a_real_metadata_only_source_can_be_retained_as_a_prefix_literal():
    source = b"---\ncustom: original\n---\n"
    raw = b"# A generated representation of metadata-only input\n"
    state = material_proof.seeded("a" * 32, source, {"kind": "synthetic"})
    state["revision"] = revision(raw)
    state["proof"]["pieces"] = [{"literal": base64.b64encode(source).decode(),
                                   "size": len(source), "sha256": revision(source)}]
    state["proof"]["generated"] = [material_proof.region(raw, 0, len(raw))]
    assert material_proof.prefix_size(source) == len(source)
    assert material_proof.reconstruct(state, "a" * 32, raw) == source


def moved_origin(history, condition="valid"):
    path, raw = "Library/source.md", map_tests.source_text(0, "\r\n", "\ufeff").encode()
    created = history.write(path, raw, None, "human", "Create source")
    WikiCuration(history.store).organize(path, created["revision"], "Summary with its condition.", [], [], None,
                                        "curator", "Adapt source", frontmatter_patch=map_tests.PROFILE)
    document = history.store.read(path)
    rendered = history.store.path(path).read_bytes()
    state = map_tests.state_for(history.store, document["id"])
    if condition == "invalid":
        state = origins.invalidated(state, document["id"], document["revision"], "Revoked source")
    elif condition == "unknown":
        state = state | {"proof": state["proof"] | {"format": "future-opaque-map"}}
    with history.locked():
        change = history.write_locked(path, rendered, document["revision"], "human", "Historical source state",
                                      origin_state=state)
    history.store.path(path).rename(history.store.path("Library/moved.md"))
    history.store.refresh()
    return change, document, state, rendered


@pytest.mark.parametrize("condition", ["valid", "invalid", "unknown"])
@pytest.mark.parametrize("occupied", [False, True])
def test_restore_copy_cannot_transfer_a_moved_documents_origin_or_identity(history, condition, occupied):
    change, original, state, rendered = moved_origin(history, condition)
    expected, target_id = None, None
    if occupied:
        created = history.write(original["path"], b"# Replacement\nSeparate document.\n", None, "human", "New occupant")
        target = history.store.read(original["path"])
        expected, target_id = created["revision"], target["id"]
    restored = history.restore(change["change_id"], expected, "after", "human")
    copy = history.store.read(original["path"])
    moved = history.store.read(original["id"])
    assert moved["path"] == "Library/moved.md" and moved["revision"] == original["revision"]
    assert moved["metadata"]["uid"] == original["metadata"]["uid"]
    assert map_tests.state_for(history.store, original["id"]) == state
    assert history.store.path(moved["path"]).read_bytes() == rendered
    assert copy["id"] != original["id"] and (not occupied or copy["id"] == target_id)
    assert copy["metadata"]["uid"] != original["metadata"]["uid"]
    assert history.store.path(copy["path"]).read_bytes() == rendered
    copied_state = map_tests.state_for(history.store, copy["id"])
    assert copied_state["doc_id"] == copy["id"] and copied_state["revision"] == copy["revision"]
    assert copied_state["status"] == "invalid" and copied_state["proof"] == state["proof"]
    assert material_proof.reconstruct(copied_state, copy["id"], rendered) is None
    with history.store.connect() as db:
        assert origins.change_state(db, restored["change_id"], "after") == copied_state
        assert db.execute("SELECT document_id FROM change_retention WHERE change_id=?",
                          (restored["change_id"],)).fetchone()[0] == copy["id"]
        assert db.execute("SELECT doc_id FROM operation_receipts WHERE change_id=?",
                          (restored["change_id"],)).fetchone()[0] == copy["id"]
    history.store.rebuild_search()
    assert map_tests.state_for(history.store, copy["id"]) == copied_state
    assert map_tests.state_for(history.store, original["id"]) == state


@pytest.mark.parametrize("field", ["doc_id", "revision"])
def test_restore_rejects_a_historical_origin_not_bound_to_its_recorded_source(history, field):
    import json

    change, original, state, rendered = moved_origin(history)
    with history.store.connect() as db:
        db.execute("UPDATE change_material_origins SET after_state=? WHERE change_id=?",
                   (json.dumps(state | {field: "wrong historical binding"}), change["change_id"]))
        changes = db.execute("SELECT count(*) FROM changes").fetchone()[0]
    with pytest.raises(WikiError, match="identity/revision"):
        history.restore(change["change_id"], None, "after", "human")
    assert not history.store.path(original["path"]).exists()
    assert history.store.path("Library/moved.md").read_bytes() == rendered
    assert map_tests.state_for(history.store, original["id"]) == state
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == changes
        assert db.execute("SELECT 1 FROM document_identities WHERE path=?", (original["path"],)).fetchone() is None


@pytest.mark.parametrize("window", ["prepared", "replaced", "applied"])
def test_restore_copy_recovers_its_own_identity_without_revoking_the_moved_source(history, monkeypatch, window):
    from server.wiki import history as history_module

    change, original, state, rendered = moved_origin(history)
    atomic, index = history_module.atomic, history.store._index

    def replace(path, raw, **kwargs):
        if path == history.store.path(original["path"]):
            raise RuntimeError("restore-copy interruption")
        return atomic(path, raw, **kwargs)

    def apply(change_id, value):
        raise RuntimeError("restore-copy interruption")

    def reindex(db, path):
        if path == original["path"]:
            raise RuntimeError("restore-copy interruption")
        return index(db, path)

    with monkeypatch.context() as patch:
        if window == "prepared":
            patch.setattr(history_module, "atomic", replace)
        elif window == "replaced":
            patch.setattr(history, "status", apply)
        else:
            patch.setattr(history.store, "_index", reindex)
        with pytest.raises(RuntimeError, match="restore-copy interruption"):
            history.restore(change["change_id"], None, "after", "human")
    with history.locked():
        pass
    history.store.refresh()
    if window == "prepared":
        assert not history.store.path(original["path"]).exists()
        history.restore(change["change_id"], None, "after", "human")
    copy = history.store.read(original["path"])
    assert copy["id"] != original["id"]
    assert history.store.path(copy["path"]).read_bytes() == rendered
    copy_state = map_tests.state_for(history.store, copy["id"])
    assert copy_state["status"] == "invalid" and copy_state["proof"] == state["proof"]
    assert copy_state["doc_id"] == copy["id"] and copy_state["revision"] == copy["revision"]
    assert history.store.read(original["id"])["path"] == "Library/moved.md"
    assert map_tests.state_for(history.store, original["id"]) == state
    with history.store.connect() as db:
        row = db.execute("SELECT c.id,c.status,r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id "
                         "WHERE c.path=? ORDER BY c.rowid DESC LIMIT 1", (copy["path"],)).fetchone()
        assert row["status"] == "applied" and row["document_id"] == copy["id"]
        assert origins.change_state(db, row["id"], "after") == copy_state

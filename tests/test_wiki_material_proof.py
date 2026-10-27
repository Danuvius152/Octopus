"""Current-byte reuse must survive legitimate layouts and reject lost evidence."""

import json
import uuid
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint, verify
from server.wiki import format, material_proof, materials, origins
from server.wiki import history as history_module
from server.wiki.contracts import PatchRevision
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.revise import revise
from server.wiki.store import WikiError, WikiStore, revision
from server.wiki.tasks import tasks
from tests import test_wiki_jobs as job_tests
from tests.wiki_jobs import jobs as jobs

PROFILE = {"type": "Synthesis", "x_wiki": {"profile": "article-v1", "body_dialect": "obsidian"}}
ARGUMENT = "The original argument retains its exception and limit."


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", "isolated-material-map", create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


def state_for(store, doc):
    with store.connect() as db:
        return origins.get(db, doc)


def arrange(curation, document, task, *, finish):
    parts = curation.blocks(document["id"], expected_revision=document["revision"], task=task)["blocks"]
    position = next(index for index, part in enumerate(parts) if ARGUMENT in part["content"])
    return curation.restructure(document["path"], document["revision"], [p["id"] for p in parts],
                                [{"before": position, "content": "## 正文", "sources": []}],
                                "curator", "Keep existing summary and body", task=task,
                                frontmatter_patch=PROFILE if finish else None)


def source_text(chain, newline="\n", bom=""):
    summary = "## Key note\n\nThe existing summary preserves its scope.\n\n" if chain else ""
    return bom + ("---\ncustom: 'keep exact spelling' # original comment\n---\n"
                  "# Source title\n\n" + summary + ARGUMENT + "\n\n重复文字。\n\n重复文字。\n").replace("\n", newline)


@pytest.mark.parametrize("chain", [0, 1, 2])
@pytest.mark.parametrize("newline,bom", [("\n", ""), ("\r\n", ""), ("\n", "\ufeff"), ("\r\n", "\ufeff")])
def test_paths_maintenance_reconstructs_exact_input_across_all_layout_chains(history, chain, newline, bom):
    store, raw = history.store, source_text(chain, newline, bom).encode()
    history.write("Library/note.md", raw, None, "human", "create")
    curation = WikiCuration(store)
    document = store.read("Library/note.md")
    if chain:
        arrange(curation, document, None, finish=chain == 2)
        document = store.read(document["id"])
    if chain != 2:
        curation.organize(document["path"], document["revision"], None if chain else "A generated summary.",
                          [], [], None, "curator", "Adapt current original", frontmatter_patch=PROFILE,
                          reuse_existing_summary=bool(chain))
    current = store.read(document["id"])
    state = state_for(store, document["id"])
    assert material_proof.reconstruct(state, current["id"], store.path(current["path"]).read_bytes()) == raw
    assert state["proof"]["source"]["kind"] == "maintenance_revision"
    assert ARGUMENT not in json.dumps(state)
    assert current["metadata"]["x_wiki"]["profile"] == "article-v1"
    assert "proof" not in current["metadata"]
    with history.locked():
        assert materials.find(store, raw)["id"] == current["id"]


class CurrentWorker(job_tests.Curator):
    def __init__(self, jobs, chain):
        super().__init__(jobs)
        self.chain = chain

    async def _events(self, session_id, prompt, **kwargs):
        self.turns += 1
        with self.jobs.store.connect() as db:
            job_id = db.execute("SELECT id FROM jobs WHERE session_id=?", (session_id,)).fetchone()[0]
        session = SimpleNamespace(id=session_id, user_id=self.jobs.user_id,
                                  agent_id=self.jobs.registration["curator_agent_id"])
        token = tasks.open(session, "maintain", request_id=kwargs["request_id"])
        task = tasks.call(tasks.active[token], verify(mint(session_id, user_id=self.jobs.user_id, wiki_task_id=token)))
        try:
            for item in self.jobs.get(job_id)["items"]:
                if item["status"] != "pending" or (
                    item.get("doc") not in task.document_ids
                    and item.get("reservation_id") not in task.reservation_ids
                ):
                    continue
                store = self.jobs.store
                current = store.read(item["doc"])
                tasks.record_read(task, current)
                curation = WikiCuration(store)
                if self.chain:
                    arrange(curation, current, task, finish=self.chain == 2)
                    current = store.read(current["id"])
                    tasks.record_read(task, current)
                if self.chain != 2:
                    curation.organize(current["path"], current["revision"], None if self.chain else "Generated summary.",
                                      [], [], None, "curator", "Use current byte evidence", task=task,
                                      frontmatter_patch=PROFILE, reuse_existing_summary=bool(self.chain))
            yield {"type": "result", "content": "Published with current operation receipts", "is_error": False}
        finally:
            tasks.close(token)


@pytest.mark.parametrize("chain", [0, 1, 2])
async def test_completed_submission_reuses_current_original_on_a_different_key_without_worker(jobs, chain):
    raw = source_text(chain, "\r\n", "\ufeff")
    first = jobs.maintain("map-first", raw, "organize", [], "Library/Input.md", [], "human")
    curator = CurrentWorker(jobs, chain)
    complete = await job_tests.process(jobs, first, curator)
    assert complete["status"] == "completed"
    assert curator.turns == 1
    current = jobs.store.read("Library/Input.md")
    state = state_for(jobs.store, current["id"])
    assert state["proof"]["source"] == {"kind": "ingest", "job_id": first["id"]}
    output = jobs.store.path(current["path"]).read_bytes()
    assert material_proof.reconstruct(state, current["id"], output) == raw.encode()
    with jobs.store.connect() as db:
        changes = db.execute("SELECT count(*) FROM changes").fetchone()[0]
    second = jobs.maintain("map-second", raw, "organize", [], "Library/Another.md", [], "human")
    assert second["id"] != first["id"] and second["status"] == "completed"
    assert second["items"][0]["reused"] is True
    assert second["items"][0]["doc"] == current["id"]
    assert jobs.store.path(current["path"]).read_bytes() == output
    assert not jobs.store.path("Library/Another.md").exists()
    assert len(list(jobs.store.root.rglob("*.md"))) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == changes
    assert curator.turns == 1


def test_reordered_repeated_utf8_prose_and_generated_edits_keep_source_order(history):
    raw = "# Title\n\n相同段落🙂\n\nAnother original.\n\n相同段落🙂\n".encode()
    history.write("Library/note.md", raw, None, "human", "create")
    doc = history.store.read("Library/note.md")
    first = len(b"# Title\n\n")
    trace = material_proof.Trace(raw.decode())
    trace.replace([(first, len(raw)), b"\nGenerated annotation.\n", (0, first)])
    change = history.write_locked(doc["path"], trace.raw, doc["revision"], "curator", "layout",
                                  origin_builder=trace.transition(allow_seed=True))
    trace = material_proof.Trace(trace.text)
    start = trace.text.index("Generated annotation.")
    trace.edit([(start, start + len("Generated annotation."), "Changed generated annotation.")])
    history.write_locked(doc["path"], trace.raw, change["revision"], "curator", "revise generated words",
                         origin_builder=trace.transition())
    state = state_for(history.store, doc["id"])
    assert material_proof.reconstruct(state, doc["id"], trace.raw) == raw
    assert ARGUMENT not in json.dumps(state)


@pytest.mark.parametrize("external", [False, True])
def test_one_original_word_change_invalidates_and_later_normalize_cannot_reseed(history, external):
    store, raw = history.store, source_text(0).encode()
    history.write("Library/note.md", raw, None, "human", "create")
    curation = WikiCuration(store)
    doc = store.read("Library/note.md")
    curation.organize(doc["path"], doc["revision"], "Summary.", [], [], None,
                      "curator", "adapt", frontmatter_patch=PROFILE)
    doc = store.read(doc["id"])
    before = store.path(doc["path"]).read_bytes()
    if external:
        store.path(doc["path"]).write_bytes(before.replace(b"retains", b"drops", 1))
        store.synchronize()
    else:
        trace = material_proof.Trace(before.decode())
        start = trace.text.index("retains")
        trace.edit([(start, start + len("retains"), "drops")])
        history.write_locked(doc["path"], trace.raw, doc["revision"], "curator", "correct original words",
                             origin_builder=trace.transition())
    current = store.read(doc["id"])
    assert state_for(store, doc["id"])["status"] == "invalid"
    curation.organize(current["path"], current["revision"], None, [], [], None, "curator", "metadata update",
                      frontmatter_patch=PROFILE, reuse_existing_summary=True)
    assert state_for(store, doc["id"])["status"] == "invalid"
    with history.locked():
        assert materials.find(store, raw) is None


def test_restore_of_first_controlled_edit_restores_the_verified_preimage(history):
    store, raw = history.store, source_text(0).encode()
    created = history.write("Library/note.md", raw, None, "human", "create")
    changed = WikiCuration(store).organize("Library/note.md", created["revision"], "Summary.", [], [], None,
                                          "curator", "adapt", frontmatter_patch=PROFILE)
    doc = store.read("Library/note.md")
    history.restore(changed["change_id"], changed["revision"], "before", "human")
    assert store.path("Library/note.md").read_bytes() == raw
    assert material_proof.reconstruct(state_for(store, doc["id"]), doc["id"], raw) == raw
    store.rebuild_search()
    with history.locked():
        assert materials.find(store, raw)["id"] == doc["id"]


def test_a_literal_copy_of_original_prose_is_not_an_admissible_proof():
    raw = b"# Original\nOriginal prose is not a metadata fragment.\n"
    state = material_proof.seeded("a" * 32, raw, {"kind": "test"})
    state["proof"]["pieces"] = [{"literal": __import__("base64").b64encode(raw).decode(),
                                  "size": len(raw), "sha256": revision(raw)}]
    state["proof"]["generated"] = [material_proof.region(raw, 0, len(raw))]
    assert material_proof.reconstruct(state, "a" * 32, raw) is None


def test_metadata_trace_refuses_a_writer_that_also_changes_the_body():
    trace = material_proof.Trace(source_text(0))
    patched = format.patch(trace.text, fields={"type": "Reference"})
    with pytest.raises(WikiError, match="Metadata writer changed the original body"):
        trace.metadata(patched.replace("retains", "drops"))


@pytest.mark.parametrize("window", ["replaced", "applied", "indexed"])
async def test_ingest_seed_recovers_in_the_same_actual_new_file_transaction(jobs, monkeypatch, window):
    raw = source_text(0, "\r\n", "\ufeff")
    job = jobs.maintain("crash-ingest", raw, "organize", [], "Library/Input.md", [], "human")
    target = jobs.store.path("Library/Input.md")
    original_status = WikiHistory.status
    original_index = jobs.store._index

    def crash():
        raise RuntimeError("injected byte-map ingest crash")

    def status(self, change_id, value):
        if value == "applied" and window == "replaced":
            crash()
        return original_status(self, change_id, value)

    def index(*args, **kwargs):
        if window == "applied":
            crash()
        result = original_index(*args, **kwargs)
        if window == "indexed":
            crash()
        return result

    with monkeypatch.context() as injected:
        injected.setattr(WikiHistory, "status", status)
        injected.setattr(jobs.store, "_index", index)
        with pytest.raises(RuntimeError, match="injected byte-map ingest crash"):
            jobs.ingest(job["id"])
    WikiHistory(jobs.store).recover()
    current = jobs.ingest(job["id"])
    assert target.read_bytes() == raw.encode()
    state = state_for(jobs.store, current["id"])
    assert state["proof"]["source"] == {"kind": "ingest", "job_id": job["id"]}
    assert material_proof.reconstruct(state, current["id"], target.read_bytes()) == raw.encode()
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes WHERE status='prepared'").fetchone()[0] == 0
    assert len(list(jobs.store.root.rglob("*.md"))) == 1


async def test_prepared_ingest_without_output_retains_original_proof_and_never_recreates_automatically(jobs, monkeypatch):
    raw = source_text(0, "\r\n", "\ufeff")
    job = jobs.maintain("uncertain-ingest", raw, "organize", [], "Library/Input.md", [], "human")
    target = jobs.store.path("Library/Input.md")
    atomic = history_module.atomic

    def before_replace(path, value, **kwargs):
        if path == target:
            raise RuntimeError("injected before material replace")
        return atomic(path, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history_module, "atomic", before_replace)
        with pytest.raises(RuntimeError, match="before material replace"):
            jobs.ingest(job["id"])
    with jobs.store.connect() as db:
        original = dict(db.execute("SELECT * FROM wiki_path_reservations WHERE job_id=?", (job["id"],)).fetchone())
        proof = db.execute("SELECT after_state FROM change_material_origins WHERE change_id=?", (original["change_id"],)).fetchone()[0]
        assert original["state"] == "prepared" and proof
    with pytest.raises(WikiError) as blocked:
        jobs.ingest(job["id"])
    assert blocked.value.status == 409 and not target.exists()
    assert jobs.get(job["id"])["payload"]["material"] == raw
    with jobs.store.connect() as db:
        current = dict(db.execute("SELECT * FROM wiki_path_reservations WHERE id=?", (original["id"],)).fetchone())
        assert current["change_id"] == original["change_id"] and current["document_id"] == original["document_id"]
        assert current["state"] in {"prepared", "blocked"}
        assert db.execute("SELECT status FROM changes WHERE id=?", (original["change_id"],)).fetchone()[0] != "applied"
        assert db.execute("SELECT after_state FROM change_material_origins WHERE change_id=?", (original["change_id"],)).fetchone()[0] == proof
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("window", ["prepared", "replaced", "applied", "indexed"])
def test_first_controlled_transform_recovers_its_exact_before_or_after_proof(history, monkeypatch, window):
    raw = source_text(0).encode()
    created = history.write("Library/note.md", raw, None, "human", "create")
    target = history.store.path("Library/note.md")
    original_atomic, original_status, original_index = history_module.atomic, WikiHistory.status, history.store._index

    def fail():
        raise RuntimeError("injected byte-map transform crash")

    def replace(path, value, **kwargs):
        if path == target and window == "prepared":
            fail()
        return original_atomic(path, value, **kwargs)

    def status(self, change_id, value):
        if value == "applied" and window == "replaced":
            fail()
        return original_status(self, change_id, value)

    def index(*args, **kwargs):
        changed = target.is_file() and target.read_bytes() != raw
        if changed and window == "applied":
            fail()
        result = original_index(*args, **kwargs)
        if changed and window == "indexed":
            fail()
        return result

    with monkeypatch.context() as injected:
        injected.setattr(history_module, "atomic", replace)
        injected.setattr(WikiHistory, "status", status)
        injected.setattr(history.store, "_index", index)
        with pytest.raises(RuntimeError, match="injected byte-map transform crash"):
            WikiCuration(history.store).organize("Library/note.md", created["revision"], "Summary.", [], [], None,
                                                  "curator", "adapt", frontmatter_patch=PROFILE)
    history.recover()
    with history.locked():
        history.store.refresh()
    current = history.store.read("Library/note.md")
    if window == "prepared":
        assert target.read_bytes() == raw and state_for(history.store, current["id"]) is None
        WikiCuration(history.store).organize(current["path"], current["revision"], "Summary.", [], [], None,
                                             "curator", "adapt", frontmatter_patch=PROFILE)
        current = history.store.read(current["id"])
    assert material_proof.reconstruct(state_for(history.store, current["id"]), current["id"], target.read_bytes()) == raw
    with history.locked():
        assert materials.find(history.store, raw)["id"] == current["id"]


def test_raw_cache_rebuild_and_store_restart_keep_the_durable_map(history):
    store, raw = history.store, source_text(0).encode()
    created = history.write("Library/note.md", raw, None, "human", "create")
    WikiCuration(store).organize("Library/note.md", created["revision"], "Summary.", [], [], None,
                                 "curator", "adapt", frontmatter_patch=PROFILE)
    doc = store.read("Library/note.md")
    state = state_for(store, doc["id"])
    with store.connect() as db:
        db.execute("DROP TABLE raw_materials")
    store.close()
    restarted = WikiStore(store.root, store.state, store.vault_id, health=store.health)
    try:
        with WikiHistory(restarted).locked():
            assert materials.find(restarted, raw)["id"] == doc["id"]
        assert state_for(restarted, doc["id"]) == state
    finally:
        restarted.close()


def test_category_move_retains_map_but_same_path_replacement_does_not_inherit_it(history):
    store, raw = history.store, source_text(0).encode()
    created = history.write("Library/note.md", raw, None, "human", "create")
    WikiCuration(store).organize("Library/note.md", created["revision"], "Summary.", [], [], None,
                                 "curator", "adapt", frontmatter_patch=PROFILE)
    current = store.read("Library/note.md")
    new = "Library/Other/note.md"
    store.path(new).parent.mkdir()
    journal = history.state / "external-change.json"
    with history.locked():
        before = history.capture_external([current["path"], new])
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex, "before": before,
                 "paths": [current["path"], new], "actor": "human", "reason": "move"}
        history_module.atomic(journal, json.dumps(entry).encode())
        store.path(current["path"]).rename(store.path(new))
        history.finish_external(entry)
        history.recover()
        assert not journal.exists()
        store.refresh()
        assert materials.find(store, raw)["id"] == current["id"]
    moved = store.read(current["id"])
    assert moved["path"] == new
    history.write(new, None, moved["revision"], "human", "delete")
    replacement = history.write(new, b"# Different file\nDifferent material.\n", None, "human", "create")
    assert replacement["revision"] != moved["revision"]
    replaced = store.read(new)
    assert replaced["id"] != current["id"]
    assert state_for(store, replaced["id"]) is None
    with history.locked():
        assert materials.find(store, raw) is None


def test_many_generated_metadata_updates_do_not_grow_a_parallel_source_copy(history):
    raw = source_text(0).encode()
    created = history.write("Library/note.md", raw, None, "human", "create")
    WikiCuration(history.store).organize("Library/note.md", created["revision"], "Summary.", [], [], None,
                                         "curator", "adapt", frontmatter_patch=PROFILE)
    doc = history.store.read("Library/note.md")
    initial = state_for(history.store, doc["id"])
    current_revision = doc["revision"]
    for count in range(20):
        trace = material_proof.Trace(history.store.path(doc["path"]).read_text())
        trace.metadata(format.patch(trace.text, tags=[f"iteration-{count}"]))
        result = history.write_locked(doc["path"], trace.raw, current_revision, "curator", "metadata",
                                      origin_builder=trace.transition())
        current_revision = result["revision"]
    state = state_for(history.store, doc["id"])
    assert len(state["proof"]["pieces"]) == len(initial["proof"]["pieces"])
    assert material_proof.reconstruct(state, doc["id"], history.store.path(doc["path"]).read_bytes()) == raw


@pytest.mark.parametrize("existing_summary", [False, True])
def test_revise_distinguishes_original_and_generated_summaries_without_heading_heuristics(history, existing_summary):
    raw = source_text(int(existing_summary)).encode()
    history.write("Library/note.md", raw, None, "human", "create")
    curation = WikiCuration(history.store)
    current = history.store.read("Library/note.md")
    if existing_summary:
        arrange(curation, current, None, finish=False)
        current = history.store.read(current["id"])
    curation.organize(current["path"], current["revision"], None if existing_summary else "Generated summary preserves its scope.",
                      [], [], None, "curator", "adapt", frontmatter_patch=PROFILE,
                      reuse_existing_summary=existing_summary)
    current = history.store.read(current["id"])
    parts = curation.blocks(current["id"])["blocks"]
    part = next(p for p in parts if "summary preserves its scope." in p["content"])
    start = part["content"].index("preserves")
    body = PatchRevision(doc=current["id"], expected_revision=current["revision"], operation="patch",
                         reason="Correct a specific summary condition", edits=[{
                             "locator": part["locator"], "expected_block_hash": part["sha256"],
                             "start": start, "end": start + len("preserves"),
                             "expected_text": "preserves", "replacement": "drops", "kind": "correction",
                         }])
    revise(curation, body, "curator", None)
    state = state_for(history.store, current["id"])
    after = history.store.path(current["path"]).read_bytes()
    if existing_summary:
        assert state["status"] == "invalid"
        assert material_proof.reconstruct(state, current["id"], after) is None
    else:
        assert material_proof.reconstruct(state, current["id"], after) == raw

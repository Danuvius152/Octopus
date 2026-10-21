"""Maintenance follows stable identities across observed moves and path reuse."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.wiki.history import WikiHistory
from server.wiki.store import WikiError
from tests import test_wiki_api as api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_maintenance_state import prepare

client = api.client


@pytest.mark.parametrize("move_when", ["none", "before_write", "after_write", "after_write_reused_path"])
async def test_publication_settlement_follows_the_same_moved_identity(
    client, tmp_path, move_when
):
    old, moved = "Library/note.md", "Library/moved.md"
    root, config, jobs = await prepare(
        client, tmp_path, {old: "# Note\n\nOriginal claim and conditions.\n"}
    )
    job = jobs.maintain("move-recovery", "", "Add a summary", [old], None, [], "human")
    item = dict(job["items"][0])
    with assigned(job, config) as (headers, _):
        if move_when == "before_write":
            (root / old).rename(root / moved)
            jobs.store.refresh()
        response = await client.get(
            "/api/wiki/read", headers=headers, params={"doc": item["doc"]}
        )
        assert response.status_code == 200, response.text
        note = response.json()
        response = await client.post(
            "/api/wiki/organize",
            headers=headers,
            json={
                "path": note["path"],
                "expected_revision": note["revision"],
                "key_note": "Original claim and conditions.",
                "reason": "Preserve the selected identity across its move",
            },
        )
        assert response.status_code == 200, response.text
        if move_when.startswith("after_write"):
            (root / old).rename(root / moved)
            jobs.store.refresh()
            if move_when == "after_write_reused_path":
                raw = (root / moved).read_bytes()
                WikiHistory(jobs.store).write(old, raw, None, "human", "Create another identity with the same bytes")
                assert jobs.store.read(old)["id"] != item["doc"]

    assert jobs.recover_publication(job["id"], item)
    assert item["status"] == "done" and item["index_complete"], item
    assert item["output"] == (old if move_when == "none" else moved)
    assert item["output_revision"] == response.json()["revision"]


@pytest.mark.parametrize("scenario", ["input_only", "output_only", "both", "input_changed", "output_changed"])
async def test_historical_separate_output_settles_both_original_identities_after_moves(client, tmp_path, scenario):
    source_path, output_path = "Library/source.md", "Library/explanation.md"
    source_moved, output_moved = "Library/moved-source.md", "Library/moved-explanation.md"
    root, _, jobs = await prepare(client, tmp_path, {source_path: "# Source\nOriginal conditions.\n"})
    job = jobs.maintain("separate-output-move", "", "Review the source", [source_path], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    jobs.reread(job["id"], item, "Reread the input before its historical publication")
    assert item["retry_count"] == 1
    source = jobs.store.read(source_path)
    history = WikiHistory(jobs.store)
    created = history.write(output_path, b"# Explanation\nIndependent interpretation.\n", None, "human", "historical publication")
    output = jobs.store.read(output_path)
    # Pre-V2 publications can associate a distinct output with this input.
    # This historical receipt grants no new-page authority to today's worker.
    receipt_id = uuid.uuid4().hex
    with jobs.store.connect() as db:
        db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)", (
            job["id"], source["id"], source["revision"], created["change_id"], output_path, output["revision"],
        ))
        db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            receipt_id, job["id"], source["id"], output["id"], output_path, None, output["revision"],
            "publish", "historical publication", created["change_id"], datetime.now(UTC).isoformat(),
            json.dumps({"input_source_doc": source["id"], "input_source_revision": source["revision"]}),
        ))
    moved = []
    if scenario != "output_only":
        moved.append((source_path, source_moved, source["id"]))
    if scenario != "input_only":
        moved.append((output_path, output_moved, output["id"]))
    replacements = {}
    for old, new, identity in moved:
        (root / old).rename(root / new)
        jobs.store.refresh()
        raw = (root / new).read_bytes()
        history.write(old, raw, None, "human", "Reuse the old path with an independent identity")
        assert jobs.store.read(old)["id"] != identity
        replacements[old] = raw
    if scenario.endswith("changed"):
        changed = source if scenario == "input_changed" else output
        current = jobs.store.read(changed["id"])
        history.write(current["path"], b"# Human correction\nChanged conditions.\n", current["revision"], "human", "Later correction")
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.md")}
    with jobs.store.connect() as db:
        change_count = db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    item = dict(jobs.get(job["id"])["items"][0])
    assert item["retry_count"] == 1
    assert jobs.recover_publication(job["id"], item)
    assert item["receipt_id"] == receipt_id and item["output_change_id"] == created["change_id"]
    assert item["status"] == ("attention" if scenario.endswith("changed") else "done")
    assert item["index_complete"] == (not scenario.endswith("changed"))
    assert item["output"] == jobs.store.read(output["id"])["path"]
    assert item["input_revision"] == source["revision"] and item["output_revision"] == output["revision"]
    assert item["retry_count"] == 1
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.md")}
    assert all((root / path).read_bytes() == raw for path, raw in replacements.items())
    with jobs.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == change_count


async def test_reread_candidate_and_restore_belong_to_the_moved_document(client, tmp_path):
    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(
        client, tmp_path, {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"}
    )
    job = jobs.maintain("move-reread", "", "Review A", [old], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    (root / moved).write_text("# A\nA new human condition for A.\n")
    jobs.store.refresh()
    before_b = (root / old).read_bytes()
    current_a = jobs.store.read(item["doc"])

    jobs.reread(job["id"], item, "A changed after submission")
    history = WikiHistory(jobs.store)
    candidates = history.history(moved)["items"]
    candidate = next(row for row in candidates if row["id"] == item["candidate_changes"][-1])
    assert candidate["proposed_operation"]["source_doc"] == item["doc"]
    assert item["path"] == moved and item["revision"] == current_a["revision"]
    restored = history.restore(candidate["id"], current_a["revision"], "before", "human")
    assert restored["path"] == moved and restored["revision"] == current_a["revision"]
    assert jobs.store.read(item["doc"])["path"] == moved
    assert (root / old).read_bytes() == before_b


@pytest.mark.parametrize("association", ["wrong_identity", "unrecorded"])
async def test_untrusted_legacy_reread_cannot_restore_over_another_document(
    client, tmp_path, association
):
    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(
        client, tmp_path, {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"}
    )
    job = jobs.maintain("legacy-reread", "", "Review A", [old], None, [], "human")
    item = job["items"][0]
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    jobs.store.refresh()
    b = jobs.store.read(old)
    before_b = (root / old).read_bytes()
    history = WikiHistory(jobs.store)
    # Historical versions inferred B's identity from A's now-reused path,
    # while retaining bytes read from A. Reproduce that persisted format.
    raw_a = (root / moved).read_bytes()
    candidate = history.record(
        old, raw_a, raw_a, "curator-worker", "legacy reread", "conflict",
        document_id=b["id"],
        proposal={
            "job_id": job["id"], "source_path": old,
            "expected_revision": item["revision"], "operation": "reread-and-organize",
        },
    )
    if association == "wrong_identity":
        item["candidate_changes"] = [candidate["id"]]
        jobs.update(job["id"], items=job["items"])

    retained = next(row for row in history.history(old)["items"] if row["id"] == candidate["id"])
    assert retained["recoverable"] == {"before": False, "after": False}
    assert retained["recovery_error"]
    with pytest.raises(WikiError) as error:
        history.restore(candidate["id"], b["revision"], "before", "human")
    assert error.value.status == 409
    assert (root / old).read_bytes() == before_b
    assert history.blobs.joinpath(candidate["before_hash"]).read_bytes() == raw_a


async def test_reread_retains_the_bytes_of_its_recorded_revision(client, tmp_path, monkeypatch):
    relative = "Library/a.md"
    root, _, jobs = await prepare(client, tmp_path, {relative: "# A\nOriginal A.\n"})
    job = jobs.maintain("racing-reread", "", "Review A", [relative], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    file = root / relative
    original_read = Path.read_bytes
    external_edits = 0

    def read_then_external_edit(path):
        nonlocal external_edits
        raw = original_read(path)
        if path == file:
            external_edits += 1
            path.write_text(f"# A\nHuman edit {external_edits}.\n")
        return raw

    with monkeypatch.context() as changes:
        changes.setattr(Path, "read_bytes", read_then_external_edit)
        jobs.reread(job["id"], item, "The human editor is active")

    candidate = next(
        row for row in WikiHistory(jobs.store).history(relative)["items"]
        if row["id"] == item["candidate_changes"][-1]
    )
    assert external_edits
    assert candidate["before_hash"] == item["revision"]
    assert candidate["proposed_operation"]["source_revision"] == item["revision"]


@pytest.mark.parametrize("narrow", [False, True])
async def test_legacy_candidates_keep_verified_ownership_after_scope_readmission(client, tmp_path, narrow):
    from server.agent_manager import AgentManager
    from server.wiki.jobs import WikiJobs

    paths = ["Library/a.md", "Library/b.md"]
    root, _, jobs = await prepare(client, tmp_path, {path: f"# {path}\nOriginal words.\n" for path in paths})
    job = jobs.maintain("legacy-readmission", "", "Review these notes", paths, None, [], "human")
    history = WikiHistory(jobs.store)
    candidates = []
    for item in job["items"]:
        raw = (root / item["path"]).read_bytes()
        candidate = history.record(
            item["path"], raw, raw, "curator-worker", "legacy reread", "conflict",
            document_id=item["doc"],
            proposal={"job_id": job["id"], "source_path": item["path"],
                      "expected_revision": item["revision"], "operation": "reread-and-organize"},
        )
        item["candidate_changes"] = [candidate["id"]]
        candidates.append((item["path"], candidate["id"], item["revision"]))
        assert next(row for row in history.history(item["path"])["items"] if row["id"] == candidate["id"])["recoverable"]["before"] is False
    jobs.update(job["id"], status="interrupted", items=job["items"])
    for path, candidate, _ in candidates:
        assert next(row for row in history.history(path)["items"] if row["id"] == candidate)["recoverable"]["before"]
    curator = await AgentManager(api.session_manager.db).create_agent(name="Replacement curator")
    changed = await client.put("/api/wiki/config", headers=api.HEADERS,
                               json={"root": str(root), "curator_agent_id": curator["id"]})
    assert changed.status_code == 200, changed.text
    current = WikiJobs(None)
    current.readmit(job["id"], paths[:1] if narrow else paths)
    for path, candidate, revision in candidates:
        restored = WikiHistory(current.store).restore(candidate, revision, "before", "human")
        assert restored["path"] == path
        assert (root / path).read_text() == f"# {path}\nOriginal words.\n"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("finish", ["restore_at_boundary", "expire_unrestored"])
async def test_candidate_retention_boundary_preserves_identity_after_readmission(client, tmp_path, legacy, resolved, finish):
    from server.agent_manager import AgentManager
    from server.wiki.jobs import WikiJobs

    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(client, tmp_path, {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"})
    job = jobs.maintain("retention-readmission", "", "Review these notes", [old, other], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    jobs.reread(job["id"], item, "Retain A's original revision for review")
    assert item["retry_count"] == 1
    candidate_id = item["candidate_changes"][-1]
    other_item = dict(job["items"][1])
    jobs.reread(job["id"], other_item, "Use one reread attempt for B before re-admission")
    assert other_item["retry_count"] == 1
    history = WikiHistory(jobs.store)
    if legacy:
        with jobs.store.connect() as db:
            proposal = json.loads(db.execute("SELECT operation FROM change_proposals WHERE change_id=?", (candidate_id,)).fetchone()[0])
            proposal.pop("source_doc")
            proposal.pop("source_revision")
            db.execute("UPDATE change_proposals SET operation=? WHERE change_id=?", (json.dumps(proposal), candidate_id))
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    jobs.store.refresh()
    before_b = (root / old).read_bytes()
    jobs.update(job["id"], status="interrupted")
    curator = await AgentManager(api.session_manager.db).create_agent(name="Retention review curator")
    changed = await client.put("/api/wiki/config", headers=api.HEADERS,
                               json={"root": str(root), "curator_agent_id": curator["id"]})
    assert changed.status_code == 200, changed.text
    current = WikiJobs(None)
    readmitted = current.readmit(job["id"], [old])  # Only B remains selected; A has no active-job protection.
    assert [entry["doc"] for entry in readmitted["items"]] == [other_item["doc"]]
    assert readmitted["items"][0]["retry_count"] == 1
    assert current.store.read(old)["id"] == other_item["doc"]
    history = WikiHistory(current.store)
    if resolved:
        history.resolve_proposals([candidate_id])
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30 if resolved else 90)
    with current.store.connect() as db:
        db.execute("UPDATE changes SET created_at=? WHERE id=?", (cutoff.isoformat(), candidate_id))
    history.cleanup(now)
    retained = next(row for row in history.history(old)["items"] if row["id"] == candidate_id)
    assert bool(retained["resolved_at"]) is resolved
    assert retained["recoverable"]["before"]
    if finish == "restore_at_boundary":
        restored = history.restore(candidate_id, item["revision"], "before", "human")
        assert restored["path"] == moved
    else:
        # Restore resolves a proposal, so this independent lifecycle must stay
        # untouched until cleanup crosses the unresolved 90-day boundary.
        history.cleanup(now + timedelta(seconds=1))
        expired = next(row for row in history.history(old)["items"] if row["id"] == candidate_id)
        assert bool(expired["resolved_at"]) is resolved
        assert not any(expired["recoverable"].values())
        with pytest.raises(WikiError) as rejected:
            history.restore(candidate_id, item["revision"], "before", "human")
        assert rejected.value.status == 410
    assert current.store.read(item["doc"])["path"] == moved
    assert (root / moved).read_text() == "# A\nOriginal A.\n"
    assert (root / old).read_bytes() == before_b


async def test_settlement_resolves_the_selected_documents_candidates_at_both_paths(client, tmp_path):
    old, moved = "Library/a.md", "Library/moved-a.md"
    root, config, jobs = await prepare(client, tmp_path, {old: "# A\nOriginal A.\n"})
    job = jobs.maintain("move-conflicts", "", "Review A", [old], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    (root / old).write_text("# A\nFirst human update.\n")
    jobs.reread(job["id"], item, "First update")
    (root / old).rename(root / moved)
    (root / moved).write_text("# A\nSecond human update.\n")
    jobs.store.refresh()
    jobs.reread(job["id"], item, "Second update after moving")
    with assigned(jobs.get(job["id"]), config) as (headers, _):
        read = await client.get("/api/wiki/read", headers=headers, params={"doc": item["doc"]})
        assert read.status_code == 200, read.text
        response = await client.post(
            "/api/wiki/organize", headers=headers,
            json={
                "path": moved, "expected_revision": read.json()["revision"],
                "key_note": "Second human update.", "reason": "Review the current selected document",
            },
        )
        assert response.status_code == 200, response.text
    assert jobs.recover_publication(job["id"], item) and item["status"] == "done"
    history = WikiHistory(jobs.store)
    candidates = [
        row for path in (old, moved) for row in history.history(path)["items"]
        if row["id"] in item["candidate_changes"]
    ]
    assert len(candidates) == 2
    assert all(row["resolved_at"] and "proposed_operation" not in row for row in candidates)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("collected", [False, True])
async def test_reread_restore_follows_its_identity_after_a_later_path_reuse(client, tmp_path, legacy, collected):
    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(
        client, tmp_path, {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"}
    )
    job = jobs.maintain("restore-moved", "", "Review A", [old], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    jobs.reread(job["id"], item, "Review the selected A")
    candidate_id = item["candidate_changes"][-1]
    history = WikiHistory(jobs.store)
    if legacy:
        raw = (root / old).read_bytes()
        candidate = history.record(
            old, raw, raw, "curator-worker", "legacy reread", "conflict", document_id=item["doc"],
            proposal={
                "job_id": job["id"], "source_path": old, "expected_revision": item["revision"],
                "operation": "reread-and-organize",
            },
        )
        candidate_id = candidate["id"]
        persisted = jobs.get(job["id"])
        persisted["items"][0]["candidate_changes"].append(candidate_id)
        jobs.update(job["id"], items=persisted["items"])
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    jobs.store.refresh()
    before_b = (root / old).read_bytes()

    if collected:
        from server.wiki.retention import collect

        history.resolve_proposals([candidate_id])
        collect(history)
        # A former path's current revision never authorizes changing B.
        with pytest.raises(WikiError) as rejected:
            history.restore(candidate_id, jobs.store.read(old)["revision"], "before", "human")
        assert rejected.value.status == 409
        assert (root / old).read_bytes() == before_b

    restored = history.restore(candidate_id, item["revision"], "before", "human")
    assert restored["path"] == moved
    assert jobs.store.read(item["doc"])["path"] == moved
    assert (root / old).read_bytes() == before_b


async def test_restoring_a_moved_managed_candidate_keeps_the_other_notes_metadata(client, tmp_path):
    from server.wiki.curation import WikiCuration

    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(
        client, tmp_path, {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"}
    )
    curation = WikiCuration(jobs.store)
    for path, tag in ((old, "alpha"), (other, "beta")):
        document = jobs.store.read(path)
        curation.organize(path, document["revision"], "Preserve this note.", [tag], [], None,
                          "human", "Prepare two independently managed notes")
    job = jobs.maintain("managed-move", "", "Review A", [old], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    jobs.reread(job["id"], item, "Review A's managed state")
    candidate = item["candidate_changes"][-1]
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    jobs.store.refresh()
    before_b = (root / old).read_bytes()

    restored = WikiHistory(jobs.store).restore(candidate, item["revision"], "before", "human")
    assert restored["path"] == moved
    assert (root / old).read_bytes() == before_b
    assert jobs.store.read(old)["metadata"]["tags"] == ["beta"]
    assert jobs.store.read(item["doc"])["metadata"]["tags"] == ["alpha"]

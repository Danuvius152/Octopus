"""Exact-material reuse, bounded retries and durable publication recovery."""

import asyncio
import json
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.store import WikiError
from tests import wiki_api as wiki_api
from tests.harness_profiles import process_profile
from tests.wiki_api import client as client
from tests.wiki_job_runtime import Curator, process, sapphire_delete
from tests.wiki_job_runtime import jobs as jobs


async def test_no_change_requires_actual_read_and_survives_worker_restart_without_regeneration(jobs, client, tmp_path):
    from server.mcp_identity import mint
    from server.wiki.registry import registry
    from server.wiki.tasks import tasks

    history = WikiHistory(jobs.store)
    history.write("Library/note.md", "# 判断\n\n保留我写的思想和条件。\n".encode(), None, "human", "create")
    job = jobs.maintain("review", "", "检查，已经满足时无需修改", ["Library/note.md"], None, [], "human")
    session = await wiki_api.session_manager.create_session(registry.config(jobs.user_id)["curator_agent_id"], "真实任务范围", str(tmp_path))
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
    original = jobs.store.path("Library/note.md").read_bytes()
    try:
        locator = (await client.get("/api/wiki/blocks", params={"doc": "Library/note.md", "view": "locators"}, headers=headers)).json()
        body = {"doc": locator["id"], "expected_revision": locator["revision"], "operation": "no_change", "reason": "条件、观点及措辞已经符合任务"}
        denied = await client.post("/api/wiki/revise", json=body, headers=headers)
        assert denied.status_code == 422
        await client.get("/api/wiki/read", params={"doc": locator["id"]}, headers=headers)
        accepted = await client.post("/api/wiki/revise", json=body, headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["change_id"] is None
        assert accepted.json()["receipt_id"]
        assert jobs.store.path("Library/note.md").read_bytes() == original
        assert len(history.history("Library/note.md")["items"]) == 1
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
    curator = Curator(jobs)
    restored = await process(WikiJobs(jobs.user_id), job, curator)
    assert restored["status"] == "completed"
    assert restored["items"][0]["operation"] == "no_change"
    assert curator.turns == 0


async def test_new_page_recovery_checks_assigned_input_revision_separately_from_target_preimage(jobs):
    history = WikiHistory(jobs.store)
    history.write("Library/source.md", b"# Source\noriginal input\n", None, "human", "create")
    job = jobs.maintain("new-page", "", "new explanation", ["Library/source.md"], None, [], "human")
    jobs.update(job["id"], status="running", session_id="new-page-worker")
    source = jobs.store.read("Library/source.md")
    # Historical pre-V2 evidence remains readable without giving a current
    # one-document worker permission to create this additional output.
    creation = history.write("Library/explanation.md", b"# Independent explanation\n", None, "human", "historical output fixture")
    output = jobs.store.read("Library/explanation.md")
    with jobs.store.connect() as db:
        db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)", (
            job["id"], source["id"], source["revision"], creation["change_id"], output["path"], output["revision"],
        ))
        db.execute("INSERT INTO operation_receipts(id,job_id,item_doc,doc_id,path,input_revision,output_revision,operation,reason,change_id,created_at,details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            uuid.uuid4().hex, job["id"], source["id"], output["id"], output["path"], None,
            output["revision"], "publish", "historical output fixture", creation["change_id"], datetime.now(UTC).isoformat(),
            json.dumps({"input_source_doc": source["id"], "input_source_revision": source["revision"]}),
        ))
    receipt = jobs.get(job["id"])["receipts"][0]
    assert receipt["input_revision"] is None
    assert receipt["details"]["input_source_revision"] == source["revision"]
    jobs.store.path("Library/source.md").write_text("# Source\ninput changed afterwards\n")
    item = dict(job["items"][0])
    assert jobs.recover_publication(job["id"], item)
    assert item["status"] == "attention"
    assert jobs.store.path("Library/explanation.md").read_bytes() == b"# Independent explanation\n"


async def test_concurrent_cross_key_same_material_creates_and_organizes_once(jobs):
    raw = "# 原始材料\n保留所有原文。\n"
    requests = [("first", "Library/first.md"), ("second", "Library/second.md")]
    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                jobs.maintain, key, raw, "organize", [], path, [], "human"
            )
            for key, path in requests
        ]
    )
    assert results[0]["id"] == results[1]["id"]
    curator = Curator(jobs)
    result = await process(jobs, results[0], curator)
    assert result["status"] == "completed"
    assert curator.turns == 1
    assert len(list(jobs.store.root.rglob("*.md"))) == 1
    for key, path in requests:
        assert (
            jobs.maintain(key, raw, "organize", [], path, [], "human")["id"]
            == result["id"]
        )
        with pytest.raises(WikiError, match="different maintenance request"):
            jobs.maintain(key, raw + "different", "organize", [], path, [], "human")


@pytest.mark.parametrize("status", ["queued", "running", "interrupted"])
@pytest.mark.parametrize("changed", ["scope", "sources", "location"])
async def test_pending_material_rejects_new_intent_without_accepting_alias(jobs, client, status, changed):
    WikiHistory(jobs.store).write("Library/source.md", b"# Evidence\nfirst\nsecond\n", None, "human", "create")
    source = jobs.store.read("Library/source.md")
    refs = [{"doc": source["id"], "revision": source["revision"], "location": "L1-L2"}]
    original = jobs.maintain("first", "# New material\n", "organize", [], "Library/new.md", refs, "human")
    jobs.update(original["id"], status=status)
    original = jobs.get(original["id"])
    response = await client.post("/api/wiki/maintain", json={
        "idempotency_key": "second", "material": "# New material\n",
        "scope": "summarize" if changed == "scope" else "organize",
        "sources": [] if changed == "sources" else [refs[0] | {"location": "L3" if changed == "location" else "L1-L2"}],
        "target": "Library/other.md",
    })
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "maintenance_intent_conflict"
    assert response.json()["existing_job"]["id"] == original["id"]
    assert response.json()["existing_job"]["status"] == status
    assert jobs.get(original["id"])["payload"] == original["payload"]
    assert len(jobs.list()["items"]) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM job_requests WHERE request_key='second'").fetchone()[0] == 0
    assert not jobs.store.path("Library/new.md").exists()
    assert not jobs.store.path("Library/other.md").exists()
    # A rejected key remains available for a request matching the original intent.
    alias = jobs.maintain("second", "# New material\n", "organize", [], "Library/other.md", refs, "human")
    assert alias["id"] == original["id"]


async def test_same_intent_reuses_pending_material_after_source_rename(jobs, client):
    WikiHistory(jobs.store).write("Library/source.md", b"# Evidence\nOriginal words\n", None, "human", "create")
    source = jobs.store.read("Library/source.md")
    refs = [{"doc": source["id"], "revision": source["revision"], "location": "L1-L2"}]
    original = jobs.maintain("first", "# New material\n", "organize", [], "Library/new.md", refs, "human")
    jobs.store.path("Library/source.md").rename(jobs.store.path("Library/renamed.md"))
    moved = jobs.store.read("Library/renamed.md")
    assert (moved["id"], moved["revision"]) == (source["id"], source["revision"])
    response = await client.post("/api/wiki/maintain", json={
        "idempotency_key": "second", "material": "# New material\n", "scope": "organize",
        "sources": [{"doc": "Library/renamed.md", "revision": source["revision"], "location": "L1-L2"}],
        "target": "Library/other.md",
    })
    assert response.status_code == 200, response.text
    assert response.json()["id"] == original["id"]
    assert jobs.get(original["id"])["payload"] == original["payload"]
    assert len(jobs.list()["items"]) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT job_id FROM job_requests WHERE request_key='second'").fetchone()[0] == original["id"]
    curator = Curator(jobs, sources=refs)
    assert (await process(jobs, original, curator))["status"] == "completed"
    assert curator.turns == 1
    assert not jobs.store.path("Library/other.md").exists()


async def test_existing_organized_original_is_reused_without_new_write_or_model(jobs):
    raw = "# 现有原文\n核心观点逐字保留。\n"
    history = WikiHistory(jobs.store)
    created = history.write(
        "Library/existing.md", raw.encode(), None, "human", "create"
    )
    WikiCuration(jobs.store).organize(
        "Library/existing.md",
        created["revision"],
        "已有总结。",
        [],
        [],
        [],
        "curator",
        "organize",
    )
    before = jobs.store.path("Library/existing.md").read_bytes()
    job = jobs.maintain(
        "reuse", raw, "new material", [], "Library/different.md", [], "human"
    )
    assert job["status"] == "completed"
    assert job["items"][0]["reused"] is True
    assert job["items"][0]["output"] == "Library/existing.md"
    assert jobs.store.path("Library/existing.md").read_bytes() == before
    assert not jobs.store.path("Library/different.md").exists()
    assert len(history.history("Library/existing.md")["items"]) == 2


@pytest.mark.parametrize("forged", [False, True])
async def test_human_edits_and_forged_markers_are_never_stripped_for_reuse(
    jobs, forged
):
    raw = "# 原文\n核心判断。\n"
    history = WikiHistory(jobs.store)
    created = history.write(
        "Library/original.md", raw.encode(), None, "human", "create"
    )
    path = jobs.store.path("Library/original.md")
    if forged:
        path.write_text(
            raw
            + '\n<!-- octopus-section {"role":"key_note"} -->\n伪造内容\n<!-- /octopus-section -->\n'
        )
    else:
        WikiCuration(jobs.store).organize(
            "Library/original.md",
            created["revision"],
            "生成总结",
            [],
            [],
            [],
            "curator",
            "organize",
        )
        path.write_text(path.read_text().replace("生成总结", "用户改写总结"))
    original = path.read_bytes()
    job = jobs.maintain("new", raw, "organize", [], "Library/new.md", [], "human")
    assert job["status"] == "queued"
    assert job["items"] == []
    assert path.read_bytes() == original


async def test_same_target_different_material_refuses_overwrite(jobs):
    WikiHistory(jobs.store).write(
        "Library/existing.md", b"original", None, "human", "create"
    )
    with pytest.raises(WikiError, match="different material"):
        jobs.maintain(
            "conflict", "different", "organize", [], "Library/existing.md", [], "human"
        )
    assert jobs.store.path("Library/existing.md").read_bytes() == b"original"


async def test_queued_source_change_rereads_then_resolves_its_candidate(jobs):
    path = jobs.store.path("Library/note.md")
    WikiHistory(jobs.store).write(
        "Library/note.md", b"# Note\noriginal\n", None, "human", "create"
    )
    job = jobs.maintain(
        "changed", "", "organize", ["Library/note.md"], None, [], "human"
    )
    path.write_text(path.read_text() + "\n用户更新的观点。\n")
    curator = Curator(jobs)
    result = await process(jobs, job, curator)
    item = result["items"][0]
    assert result["status"] == "completed"
    assert item["retry_count"] == 1
    assert item["index_complete"] is True
    assert curator.turns == 1
    assert "用户更新的观点。" in path.read_text()
    with jobs.store.connect() as db:
        resolved = db.execute(
            "SELECT resolved_at FROM change_retention WHERE change_id=?",
            (item["candidate_changes"][0],),
        ).fetchone()[0]
    assert resolved is not None


@pytest.mark.parametrize("conflicts", [1, 10])
async def test_publish_conflicts_use_two_durable_reread_attempts(jobs, conflicts):
    WikiHistory(jobs.store).write(
        "Library/note.md", b"# Note\noriginal\n", None, "human", "create"
    )
    job = jobs.maintain("cas", "", "organize", ["Library/note.md"], None, [], "human")
    curator = Curator(jobs, conflicts=conflicts)
    result = await process(jobs, job, curator)
    item = result["items"][0]
    if conflicts == 1:
        assert result["status"] == "completed"
        assert item["retry_count"] == 1
        assert curator.turns == 2
    else:
        assert result["status"] == "completed_with_attention"
        assert item["retry_count"] == 2
        assert curator.turns == 3
        # Reload from SQLite and manual retry do not grant another model turn.
        restarted = WikiJobs(jobs.user_id)
        restarted.retry(job["id"])
        await process(restarted, job, curator)
        assert curator.turns == 3
        assert restarted.get(job["id"])["items"][0]["retry_count"] == 2


@pytest.mark.parametrize("stage", ["after_atomic", "after_index"])
async def test_restart_claims_published_output_and_finishes_index_without_model(
    jobs, monkeypatch, stage
):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Note\noriginal\n", None, "human", "create")
    job = jobs.maintain("crash", "", "organize", ["Library/note.md"], None, [], "human")
    curator = Curator(jobs, crash=stage == "after_index")
    original_status = WikiHistory.status
    if stage == "after_atomic":

        def crash_before_status(self, change_id, value):
            raise RuntimeError("crash after atomic publish")

        monkeypatch.setattr(WikiHistory, "status", crash_before_status)
    with pytest.raises(
        asyncio.CancelledError if stage == "after_index" else RuntimeError
    ):
        await process(jobs, job, curator)
    monkeypatch.setattr(WikiHistory, "status", original_status)
    history.recover()
    jobs.store.refresh()
    jobs.recover_jobs()
    assert jobs.next_queued() == job["id"]
    curator.crash = False
    worker = WikiWorker()
    worker.manager = curator
    await worker.process(jobs, job["id"])
    result = jobs.get(job["id"])
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["worker_request"]["terminal"]["status"] == "failed"
    assert curator.turns == 1
    assert result["items"][0]["index_complete"] is True
    assert result["items"][0]["output_change_id"]
    with jobs.store.connect() as db:
        assert db.execute("SELECT indexed FROM job_publications").fetchone()[0] == 1


async def test_ingest_rechecks_current_original_after_waiting_in_queue(jobs):
    raw = "# Material\nexact original\n"
    job = jobs.maintain(
        "late", raw, "organize", [], "Library/requested.md", [], "human"
    )
    WikiHistory(jobs.store).write(
        "Library/external.md", raw.encode(), None, "human", "external save"
    )
    curator = Curator(jobs)
    result = await process(jobs, job, curator)
    assert result["status"] == "completed"
    assert result["items"][0]["path"] == "Library/external.md"
    assert result["items"][0]["reused"] is True
    assert curator.turns == 0
    assert not jobs.store.path("Library/requested.md").exists()


@pytest.mark.parametrize(
    "suffix,raw", [(".txt", "完整文本\n"), (".html", "<p>完整正文</p>\n")]
)
async def test_new_material_does_not_reuse_unsearchable_txt_html(
    jobs, suffix, raw
):
    path = "Library/original" + suffix
    WikiHistory(jobs.store).write(path, raw.encode(), None, "human", "create")
    result = jobs.maintain("same", raw, "organize", [], "Library/new.md", [], "human")
    assert result["status"] == "queued" and result["items"] == []
    created = jobs.ingest(result["id"])
    assert created["path"] == "Library/new.md"
    assert jobs.store.path(created["path"]).read_bytes() == raw.encode()
    assert jobs.store.path(path).read_bytes() == raw.encode()
    if suffix == ".html":
        different = jobs.maintain(
            "plain", "完整正文", "organize", [], "Library/plain.md", [], "human"
        )
        assert different["status"] == "queued"


async def test_restart_before_prepared_request_keeps_budget_and_marks_unknown_without_model(jobs):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Note\noriginal\n", None, "human", "create")
    job = jobs.maintain(
        "restart-budget", "", "organize", ["Library/note.md"], None, [], "human"
    )
    jobs.store.path("Library/note.md").write_text("# Note\nupdated original\n")
    curator = Curator(jobs)
    original_create = curator.create_session

    async def crash_before_turn(*args, **kwargs):
        raise asyncio.CancelledError()

    curator.create_session = crash_before_turn
    with pytest.raises(asyncio.CancelledError):
        await process(jobs, job, curator)
    assert WikiJobs(jobs.user_id).get(job["id"])["items"][0]["retry_count"] == 1
    jobs.recover_jobs()
    curator.create_session = original_create
    result = await process(jobs, job, curator)
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["retry_count"] == 1
    assert result["items"][0]["status"] == "unknown"
    assert not result["items"][0].get("worker_request")
    assert curator.turns == 0
    with pytest.raises(WikiError, match="new explicit task"):
        jobs.retry(job["id"])


async def test_active_publication_protects_unindexed_output_until_recovered(
    jobs, monkeypatch
):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Note\noriginal\n", None, "human", "create")
    job = jobs.maintain(
        "index-crash", "", "organize", ["Library/note.md"], None, [], "human"
    )
    original_index = jobs.store._index

    def fail_after_publish(db, path, *, raw=None):
        if b"octopus-section" in jobs.store.path(path).read_bytes():
            raise RuntimeError("index unavailable after publish")
        return original_index(db, path, raw=raw)

    monkeypatch.setattr(jobs.store, "_index", fail_after_publish)
    curator = Curator(jobs)
    with pytest.raises(RuntimeError, match="index unavailable"):
        await process(jobs, job, curator)
    monkeypatch.setattr(jobs.store, "_index", original_index)
    with jobs.store.connect() as db:
        publication = db.execute("SELECT * FROM job_publications").fetchone()
        assert publication["indexed"] == 0
    history.cleanup(datetime.now(UTC) + timedelta(days=100))
    assert (history.blobs / publication["input_revision"]).is_file()
    assert (history.blobs / publication["output_revision"]).is_file()
    jobs.recover_jobs()
    result = await process(jobs, job, curator)
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["index_complete"] is True
    assert curator.turns == 1


async def test_extraction_error_after_publish_recovers_index_without_another_model_turn(jobs, monkeypatch):
    import server.wiki.store as store_module

    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Note\noriginal\n", None, "human", "create")
    job = jobs.maintain("extract-error", "", "organize", ["Library/note.md"], None, [], "human")
    original_extract = store_module.extract

    def fail_published(raw, suffix):
        if b"octopus-section" in raw:
            raise ValueError("controlled extraction error")
        return original_extract(raw, suffix)

    monkeypatch.setattr(store_module, "extract", fail_published)
    curator = Curator(jobs)
    result = await process(jobs, job, curator)
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["index_complete"] is False
    assert result["items"][0]["status"] == "attention"
    assert curator.turns == 1
    published_bytes = jobs.store.path("Library/note.md").read_bytes()
    with jobs.store.connect() as db:
        assert db.execute("SELECT indexed FROM job_publications").fetchone()[0] == 0
    # A fresh queue instance represents restart; retry restores only the index.
    monkeypatch.setattr(store_module, "extract", original_extract)
    restarted = WikiJobs(jobs.user_id)
    retried = restarted.retry(job["id"])
    assert retried["items"][0].get("retry_count", 0) == 0
    result = await process(restarted, job, curator)
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["index_complete"] is True
    assert result["items"][0]["worker_request"]["terminal"]["status"] == "failed"
    assert curator.turns == 1
    assert jobs.store.path("Library/note.md").read_bytes() == published_bytes
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*),sum(indexed) FROM job_publications").fetchone()[:] == (1, 1)


async def test_replacement_at_ingested_path_is_reused_without_organizing_new_identity(jobs):
    raw = "# Original\nexact material\n"
    job = jobs.maintain("crashed-ingest", raw, "organize", [], "Library/note.md", [], "human")
    ingested = jobs.ingest(job["id"])
    history = WikiHistory(jobs.store)
    sapphire_delete(history, "Library/note.md")
    replaced = history.write("Library/note.md", raw.encode(), None, "human", "new document")
    current = jobs.store.read("Library/note.md")
    assert current["id"] != ingested["id"]
    assert current["revision"] == replaced["revision"]
    curator = Curator(jobs)
    result = await process(jobs, job, curator)
    assert result["status"] == "completed"
    assert result["items"][0]["doc"] == current["id"]
    assert result["items"][0]["reused"] is True
    assert curator.turns == 0
    assert jobs.store.path("Library/note.md").read_bytes() == raw.encode()


async def test_old_creation_receipt_cannot_select_same_hash_at_new_identity(jobs):
    history = WikiHistory(jobs.store)
    raw = b"# Note\nmaterial\n"
    created = history.write("Library/note.md", raw, None, "human", "create")
    sapphire_delete(history, "Library/note.md")
    history.write("Library/note.md", raw, None, "human", "replace")
    with pytest.raises(WikiError, match="different document identity"):
        jobs.maintain("forged-creation", "", "organize", ["Library/note.md"], None, [], "human", {"Library/note.md": created["change_id"]})


@pytest.mark.parametrize("scenario", ["index_error_source_changed", "cancelled_source_changed", "indexed_source_changed", "output_edited", "output_replaced", "missing_fts"])
async def test_published_item_recovery_never_generates_again_or_consumes_reread_budget(jobs, monkeypatch, scenario):
    import server.wiki.store as store_module

    history = WikiHistory(jobs.store)
    history.write("Library/citation.md", b"# Citation\nCondition A holds in this test.\n", None, "human", "create source")
    citation = jobs.store.read("Library/citation.md")
    history.write("Library/note.md", b"# Note\nOriginal claim.\n", None, "human", "create note")
    job = jobs.maintain("publication-recovery", "", "organize", ["Library/note.md"], None, [], "human")
    curator = Curator(jobs, crash=scenario != "index_error_source_changed", sources=[{"doc": citation["id"], "revision": citation["revision"], "location": "L1-L2"}])
    original_extract = store_module.extract
    if scenario == "index_error_source_changed":
        def broken(raw, suffix):
            if b"octopus-section" in raw:
                raise ValueError("controlled post-publication extraction failure")
            return original_extract(raw, suffix)

        monkeypatch.setattr(store_module, "extract", broken)
        outcome = await process(jobs, job, curator)
        assert outcome["status"] == "completed_with_attention"
        monkeypatch.setattr(store_module, "extract", original_extract)
    else:
        with pytest.raises(asyncio.CancelledError):
            await process(jobs, job, curator)
    curator.crash = False
    if "source_changed" in scenario:
        jobs.store.path(citation["path"]).write_text("# Citation\nCondition A no longer holds.\n")
    elif scenario == "output_edited":
        file = jobs.store.path("Library/note.md")
        file.write_text(file.read_text() + "\nHuman limitation added after publication.\n")
    elif scenario == "output_replaced":
        raw = jobs.store.path("Library/note.md").read_bytes()
        sapphire_delete(history, "Library/note.md")
        history.write("Library/note.md", raw, None, "human", "new identity")
    else:
        with jobs.store.connect() as db:
            db.execute("DELETE FROM search_fts")
    before = jobs.store.path("Library/note.md").read_bytes()
    restarted = WikiJobs(jobs.user_id)
    if scenario == "cancelled_source_changed":
        restarted.mark_cancelled(job["id"])
        with pytest.raises(WikiError, match="Confirm the original cancellation"):
            restarted.retry(job["id"])
        await restarted.settle_cancelled(job["id"], curator)
        outcome = restarted.get(job["id"])
        assert outcome["settlement_phase"] == "attention"
        assert outcome["items"][0]["status"] == "attention"
        with pytest.raises(WikiError, match="Confirm the original cancellation"):
            restarted.retry(job["id"])
    elif scenario == "index_error_source_changed":
        restarted.retry(job["id"])
    else:
        restarted.recover_jobs()
    if scenario != "cancelled_source_changed":
        outcome = await process(restarted, job, curator)
    expected = "cancelled" if scenario == "cancelled_source_changed" else "completed_with_attention"
    assert outcome["status"] == expected, outcome
    assert curator.turns == 1
    assert outcome["items"][0].get("retry_count", 0) == 0
    assert jobs.store.path("Library/note.md").read_bytes() == before
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM job_publications").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["after_atomic_edited", "after_atomic_reverted", "before_atomic"])
async def test_uncertain_publication_receipt_never_starts_another_model_turn(jobs, monkeypatch, stage):
    import server.wiki.history as history_module

    history = WikiHistory(jobs.store)
    original = b"# Note\nOriginal condition.\n"
    history.write("Library/note.md", original, None, "human", "create")
    job = jobs.maintain("uncertain-publication", "", "organize", ["Library/note.md"], None, [], "human")
    curator = Curator(jobs)
    original_status = WikiHistory.status
    original_atomic = history_module.atomic

    def crash_before_status(self, change_id, value):
        raise RuntimeError("controlled crash after atomic publication")

    def crash_before_atomic(path, raw, **kwargs):
        if path == jobs.store.path("Library/note.md"):
            raise RuntimeError("controlled crash before atomic publication")
        return original_atomic(path, raw, **kwargs)

    if stage == "before_atomic":
        monkeypatch.setattr(history_module, "atomic", crash_before_atomic)
    else:
        monkeypatch.setattr(WikiHistory, "status", crash_before_status)
    with pytest.raises(RuntimeError, match="controlled crash"):
        await process(jobs, job, curator)
    monkeypatch.setattr(WikiHistory, "status", original_status)
    monkeypatch.setattr(history_module, "atomic", original_atomic)
    with jobs.store.connect() as db:
        receipt = db.execute("SELECT p.*,c.status FROM job_publications p JOIN changes c ON c.id=p.change_id").fetchone()
        assert receipt["status"] == "prepared" and receipt["indexed"] == 0
    path = jobs.store.path("Library/note.md")
    if stage == "after_atomic_edited":
        path.write_bytes(path.read_bytes() + b"\nHuman condition added before recovery.\n")
    elif stage == "after_atomic_reverted":
        path.write_bytes(original)
    before_recovery = path.read_bytes()
    history.recover()
    restarted = WikiJobs(jobs.user_id)
    restarted.recover_jobs()
    outcome = await process(restarted, job, curator)
    assert outcome["status"] == "completed_with_attention", outcome
    item = outcome["items"][0]
    assert item["status"] == "attention"
    assert item["index_complete"] is False and item["output_change_id"] == receipt["change_id"]
    assert item.get("retry_count", 0) == 0 and curator.turns == 1
    assert path.read_bytes() == before_recovery
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*),sum(indexed) FROM job_publications").fetchone()[:] == (1, 0)
        assert db.execute("SELECT status FROM changes WHERE id=?", (receipt["change_id"],)).fetchone()[0] == ("conflict" if stage == "after_atomic_edited" else "interrupted")
    # A queued recovery protects its roots regardless of their age.
    restarted.retry(job["id"])
    future = datetime.now(UTC) + timedelta(days=100)
    history.cleanup(future)
    assert (history.blobs / receipt["output_revision"]).is_file()
    outcome = await process(restarted, job, curator)
    assert outcome["status"] == "completed_with_attention"
    assert outcome["items"][0].get("retry_count", 0) == 0 and curator.turns == 1
    assert path.read_bytes() == before_recovery
    # Once terminal, expired candidate bytes can leave the finite history.
    # The remaining receipt must still prevent automatic regeneration.
    history.cleanup(future)
    assert not (history.blobs / receipt["output_revision"]).is_file()
    restarted.retry(job["id"])
    outcome = await process(restarted, job, curator)
    assert outcome["status"] == "completed_with_attention"
    assert outcome["items"][0].get("retry_count", 0) == 0 and curator.turns == 1
    assert path.read_bytes() == before_recovery


async def test_recording_failure_retry_verifies_original_without_new_model(jobs):
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion, SubmissionReceipt

    class RecordingCurator(Curator):
        committed = False
        request_id = None

        def get_session(self, session_id):
            return None  # A later process has no runtime pending outcome.

        async def get_committed_terminal(self, session_id, request_id):
            assert request_id == self.request_id
            return {"request_id": request_id, "status": "success"} if self.committed else None

        async def start_message(self, session_id, prompt, **kwargs):
            assert kwargs["source"] == "wiki"
            self.request_id = kwargs["request_id"]
            async for _ in self._events(session_id, prompt, **kwargs):
                pass
            future = asyncio.get_running_loop().create_future()
            future.set_result(RequestCompletion(
                input_request_id=self.request_id, request_id=self.request_id,
                state="recording_failed", recording_state="unconfirmed",
                outcome=TurnOutcome(request_id=self.request_id, status="success"),
            ))
            return SubmissionReceipt("accepted", self.request_id, future)

    job = jobs.maintain("recording-failure", "Original source", "record original", [], "Library/source.md", [], "human")
    curator = RecordingCurator(jobs)
    first = await process(jobs, job, curator)
    item = first["items"][0]
    assert first["status"] == "completed_with_attention"
    assert item["worker_request"]["request_id"] == curator.request_id
    assert item["worker_request"]["phase"] == "unknown"
    assert curator.turns == 1
    assert item.get("retry_count", 0) == 0
    second = await process(jobs, jobs.retry(job["id"]), curator)
    assert second["items"][0]["status"] == "attention"
    assert curator.turns == 1
    curator.committed = True
    finished = await process(jobs, jobs.retry(job["id"]), curator)
    assert finished["status"] == "completed"
    assert finished["items"][0]["index_complete"] is True
    assert "recording_failure" not in finished["items"][0]
    assert curator.turns == 1


async def test_worker_keeps_completed_session_until_owned_cleanup_and_archives_once(jobs, monkeypatch):
    from server.harness import HarnessEvent
    from server.wiki.tasks import tasks as grants
    from tests.fake_run import FakeRunBase

    manager = wiki_api.session_manager
    started, finish_run = asyncio.Event(), asyncio.Event()
    cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
    automatic, archived, receipts = [], [], []
    retirement = manager.finish_work_cancellation
    auto_archive = manager.auto_archive_scheduled_session
    admit = manager.start_message
    processing = None
    job = jobs.maintain("owned-archive", "Original source", "organize", [], "Library/owned.md", [], "human")
    jobs.update(job["id"], status="running")

    class OwnedRun(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            started.set()
            await finish_run.wait()
            session_id = jobs.get(job["id"])["session_id"]
            async for event in Curator(jobs)._events(session_id, "organize"):
                yield HarnessEvent(type=event["type"], content=event.get("content", ""), is_error=event.get("is_error", False))

        async def interrupt(self):
            pass

        async def stop(self):
            pass

    async def owned_retirement(affected):
        await retirement(affected)
        if receipts and receipts[0]._completion.done():
            cleaning.set()
            await finish_cleanup.wait()

    async def observed_admission(*args, **kwargs):
        receipt = await admit(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    async def observed_auto_archive(session_id):
        automatic.append(session_id)
        return await auto_archive(session_id)

    async def observe(event):
        if event.get("type") == "session_archived":
            archived.append((event["old_session_id"], event["old_session_id"] in grants.assignments))

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: OwnedRun())
    monkeypatch.setattr(manager, "start_message", observed_admission)
    monkeypatch.setattr(manager, "finish_work_cancellation", owned_retirement)
    monkeypatch.setattr(manager, "auto_archive_scheduled_session", observed_auto_archive)
    manager.on_broadcast("observe-worker-archive", observe)
    worker = WikiWorker()
    worker.manager = manager
    processing = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        session_id = jobs.get(job["id"])["session_id"]
        session = manager.get_session(session_id)
        assert session._run.request.source == "wiki"
        finish_run.set()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert (await asyncio.wait_for(receipts[0].wait(), 2)).outcome.status == "success"
        assert automatic == []
        assert manager.get_session(session_id) is session
        assert session_id in grants.assignments
        assert archived == []
        rows = await manager.db.load_sessions(include_archived=True)
        assert not next(row for row in rows if row["id"] == session_id)["archived"]

        finish_cleanup.set()
        await asyncio.wait_for(processing, 2)
        assert jobs.get(job["id"])["status"] == "completed"
        assert manager.get_session(session_id) is None
        assert session_id not in grants.assignments
        assert archived == [(session_id, False)]
        rows = await manager.db.load_sessions(include_archived=True)
        assert next(row for row in rows if row["id"] == session_id)["archived"]
    finally:
        finish_run.set()
        finish_cleanup.set()
        if not processing.done():
            processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)


async def test_worker_cancel_waits_for_owned_execution_before_removing_grant(jobs, monkeypatch):
    from server.harness import HarnessEvent
    from server.wiki.tasks import tasks as grants
    from tests.fake_run import FakeRunBase

    started, stopping, reaped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldRun(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            started.set()
            await asyncio.Event().wait()
            if False:
                yield HarnessEvent(type="result")

        async def interrupt(self):
            await self.stop()

        async def stop(self):
            stopping.set()
            await reaped.wait()

    manager = wiki_api.session_manager
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: HeldRun())
    job = jobs.maintain("cancel-grants", "Original source", "organize", [], "Library/cancel.md", [], "human")
    jobs.update(job["id"], status="running")
    worker = WikiWorker()
    worker.manager = manager
    processing = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        session_id = jobs.get(job["id"])["session_id"]
        session = manager.get_session(session_id)
        assert session._run.request.source == "wiki"
        assert session_id in grants.assignments
        processing.cancel()
        await asyncio.wait_for(stopping.wait(), 2)
        assert session_id in grants.assignments
        assert not processing.done()
        reaped.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(processing, 2)
        assert session_id not in grants.assignments
        assert manager.get_session(session_id) is None
    finally:
        reaped.set()
        if not processing.done():
            processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)


async def test_job_cancel_targets_captured_wiki_request_and_preserves_human_correction(jobs, monkeypatch):
    from server.harness import HarnessEvent
    from tests.fake_run import FakeRunBase

    manager = wiki_api.session_manager
    entered, finish = asyncio.Event(), asyncio.Event()

    class HumanRun(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            entered.set()
            await finish.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: HumanRun())
    job = jobs.maintain("cancel-captured-request", "Original source", "organize", [], "Library/precise.md", [], "human")
    session = await manager.create_session((await manager.db.get_system_agent())["id"], "Wiki cancellation scope")
    await manager.interrupt(session.id)
    original = await manager.start_message(session.id, "Wiki queued work", source="wiki")
    jobs.update(job["id"], status="running", session_id=session.id, items=[{
        "status": "pending", "worker_request": {"session_id": session.id, "request_id": original.request_id, "phase": "unknown"},
    }])
    correction = await manager.start_message(session.id, "human correction")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(jobs.cancel(job["id"], manager), 2)
        assert result["status"] == "cancelled"
        assert (await original.wait()).state == "withdrawn"
        assert session._run.request.request_id == correction.request_id
        assert manager.session_is_executing(session)
        finish.set()
        assert (await asyncio.wait_for(correction.wait(), 2)).outcome.status == "success"
    finally:
        finish.set()
        await asyncio.wait_for(manager.interrupt(session.id), 2)


@pytest.fixture
async def shutdown_worker(jobs, monkeypatch):
    from server.harness import get_harness
    from server.harness.claude_code import ClaudeDriver
    from server.wiki.registry import registry

    manager = wiki_api.session_manager
    await manager.db.update_agent(registry.config(jobs.user_id)["curator_agent_id"], backend="codex")
    ready = asyncio.Event()
    script = (
        "import json,sys\n"
        "print(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'curator ready'}}),flush=True)\n"
        "sys.stdin.read()\n"
    )

    def argv(context):
        return [sys.executable, "-u", "-c", script], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, build_turn_argv=argv, new_driver=ClaudeDriver, can_steer=True, reusable=True,
    ))

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "curator ready":
            ready.set()

    manager.on_broadcast("shutdown-curator-ready", observe)
    worker = WikiWorker()
    try:
        yield worker, manager, ready
    finally:
        await worker.stop()
        manager.remove_broadcast("shutdown-curator-ready")


async def test_shutdown_interrupts_curator_and_preserves_queued_jobs(jobs, shutdown_worker):
    worker, manager, ready = shutdown_worker
    current = jobs.maintain("shutdown-current", "Original source", "organize", [], "Library/current.md", [], "human")
    await worker.start(manager)
    await asyncio.wait_for(ready.wait(), 3)

    worker.begin_shutdown()
    queued = jobs.maintain("shutdown-later", "Another source", "organize", [], "Library/later.md", [], "human")
    worker.wake(jobs.user_id)
    await worker.stop()

    stopped = jobs.get(current["id"])
    assert stopped["status"] == "interrupted"
    assert stopped["items"][0]["status"] == "pending"
    assert jobs.get(queued["id"])["status"] == "queued"


async def test_shutdown_cause_survives_shielded_wiki_write(jobs, shutdown_worker, monkeypatch):
    from server.wiki import job_execution

    worker, manager, ready = shutdown_worker
    current = jobs.maintain("shutdown-write", "Original source", "organize", [], "Library/write.md", [], "human")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    transition = job_execution.transition

    def delayed_transition(owner, job_id, reference, phase, **fields):
        result = transition(owner, job_id, reference, phase, **fields)
        if job_id == current["id"] and phase == "accepted":
            captured = owner.get(job_id)["items"][0]["worker_request"]
            assert (captured["attempt"], captured["session_id"], captured["request_id"]) == (
                reference["attempt"], reference["session_id"], reference["request_id"])
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("test Wiki writer was not released")
        return result

    monkeypatch.setattr(job_execution, "transition", delayed_transition)
    try:
        await worker.start(manager)
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(ready.wait(), 3)
        session_id = jobs.get(current["id"])["session_id"]
        worker.begin_shutdown()
        await asyncio.sleep(0)
        release.set()
        await worker.stop()

        outcomes = [row["content"] for row in await manager.db.load_messages(session_id) if row["type"] == "result"]
        assert len(outcomes) == 1
        assert outcomes[0]["status"] == "interrupted"
        assert outcomes[0]["code"] == "service_shutdown"
        assert jobs.get(current["id"])["status"] == "interrupted"
    finally:
        release.set()


async def test_shutdown_timeout_retains_writer_and_owner_until_cleanup_finishes(jobs, shutdown_worker, monkeypatch):
    worker, manager, ready = shutdown_worker
    current = jobs.maintain("shutdown-recording", "Original source", "organize", [], "Library/recording.md", [], "human")
    entered, release = asyncio.Event(), asyncio.Event()
    record = manager.db.record_terminal

    async def delayed_record(**fields):
        entered.set()
        await release.wait()
        return await record(**fields)

    monkeypatch.setattr(manager.db, "record_terminal", delayed_record)
    try:
        await worker.start(manager)
        await asyncio.wait_for(ready.wait(), 3)
        session_id = jobs.get(current["id"])["session_id"]
        worker.begin_shutdown()
        await asyncio.wait_for(entered.wait(), 3)

        with pytest.raises(RuntimeError, match="Wiki curator teardown remains unconfirmed"):
            await worker.stop(timeout=.02)
        assert jobs.get(current["id"])["status"] == "running"
        assert await manager.db.count_messages(session_id) > 0
        with pytest.raises(RuntimeError, match="Wiki curator teardown remains unconfirmed"):
            await worker.start(manager)

        release.set()
        await worker.stop(timeout=3)
        outcomes = [row["content"] for row in await manager.db.load_messages(session_id) if row["type"] == "result"]
        assert len(outcomes) == 1
        assert outcomes[0]["code"] == "service_shutdown"
        assert jobs.get(current["id"])["status"] == "interrupted"
    finally:
        release.set()


async def test_shutdown_preserves_earlier_user_job_cancellation(jobs, shutdown_worker, monkeypatch):
    worker, manager, ready = shutdown_worker
    current = jobs.maintain("shutdown-user-cancel", "Original source", "organize", [], "Library/user-cancel.md", [], "human")
    entered, release = asyncio.Event(), asyncio.Event()
    record = manager.db.record_terminal
    cancellation = None

    async def delayed_record(**fields):
        entered.set()
        await release.wait()
        return await record(**fields)

    monkeypatch.setattr(manager.db, "record_terminal", delayed_record)
    try:
        await worker.start(manager)
        await asyncio.wait_for(ready.wait(), 3)
        session_id = jobs.get(current["id"])["session_id"]
        cancellation = asyncio.create_task(jobs.cancel(current["id"], manager))
        await asyncio.wait_for(entered.wait(), 3)
        worker.begin_shutdown()
        release.set()
        assert (await cancellation)["status"] == "cancelled"
        await worker.stop()

        outcomes = [row["content"] for row in await manager.db.load_messages(session_id) if row["type"] == "result"]
        assert len(outcomes) == 1
        assert outcomes[0]["code"] == "interrupted"
        assert outcomes[0]["message"] == "(interrupted by user)"
        assert jobs.get(current["id"])["status"] == "cancelled"
    finally:
        release.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)


async def test_shutdown_cleanup_failure_is_reported_without_claiming_next_job(jobs, shutdown_worker, monkeypatch):
    worker, manager, ready = shutdown_worker
    current = jobs.maintain("shutdown-cleanup-failure", "Original source", "organize", [], "Library/cleanup.md", [], "human")
    queued = jobs.maintain("shutdown-unclaimed", "Another source", "organize", [], "Library/unclaimed.md", [], "human")
    cancel = manager.cancel_request

    async def failed_cleanup(*args, **kwargs):
        await cancel(*args, **kwargs)
        raise RuntimeError("captured curator cleanup failed")

    monkeypatch.setattr(manager, "cancel_request", failed_cleanup)
    try:
        await worker.start(manager)
        await asyncio.wait_for(ready.wait(), 3)
        session_id = jobs.get(current["id"])["session_id"]
        worker.begin_shutdown()
        with pytest.raises(RuntimeError, match="Wiki curator teardown remains unconfirmed") as failure:
            await worker.stop()

        assert jobs.get(current["id"])["status"] == "interrupted"
        assert jobs.get(current["id"])["items"][0]["status"] == "pending"
        assert jobs.get(queued["id"])["status"] == "queued"
        assert str(failure.value.__cause__) == "captured curator cleanup failed"
        with pytest.raises(RuntimeError, match="Wiki curator teardown remains unconfirmed"):
            await worker.stop(timeout=0)
        outcomes = [row["content"] for row in await manager.db.load_messages(session_id) if row["type"] == "result"]
        assert len(outcomes) == 1
        assert outcomes[0]["code"] == "service_shutdown"
    finally:
        monkeypatch.setattr(manager, "cancel_request", cancel)
        worker.begin_shutdown()
        await manager.drain_turns()
        await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        # Release the test's completed failed-owner metadata after observing
        # the public failure; no running curator or writer is discarded.
        worker.tasks.clear()

"""Exact-material reuse, bounded retries and durable publication recovery."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.store import WikiError
from tests import test_wiki_api as wiki_api

client = wiki_api.client


@pytest.fixture
async def jobs(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    return WikiJobs(None)


class Curator:
    def __init__(self, jobs, *, conflicts=0, crash=False, sources=None):
        self.jobs = jobs
        self.conflicts = conflicts
        self.crash = crash
        self.sources = sources or []
        self.turns = 0
        self.sessions = 0
        self.db = self

    async def get_agent(self, *args):
        return {"backend": "codex"}

    async def create_session(self, *args, **kwargs):
        self.sessions += 1
        return SimpleNamespace(id=f"curator-{self.sessions}")

    async def archive_session(self, *args, **kwargs):
        pass

    async def send_message(self, session_id, prompt, **kwargs):
        self.turns += 1
        with self.jobs.store.connect() as db:
            job_id = db.execute(
                "SELECT id FROM jobs WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        job = self.jobs.get(job_id)
        for item in job["items"]:
            if item["status"] != "pending":
                continue
            source = self.jobs.store.read(item["doc"])
            if self.turns <= self.conflicts:
                path = self.jobs.store.path(source["path"])
                path.write_text(path.read_text() + f"\n人工补充 {self.turns}。\n")
            try:
                await asyncio.to_thread(
                    WikiCuration(self.jobs.store).organize,
                    item["path"],
                    source["revision"],
                    "原文的观点与适用条件。",
                    [],
                    [],
                    self.sources,
                    "curator",
                    "test organization",
                    {"job_id": job_id, "session_id": session_id},
                )
            except WikiError as exc:
                yield {"type": "error", "message": str(exc)}
            else:
                if self.crash:
                    raise asyncio.CancelledError()
        yield {"type": "result", "content": "published"}


async def process(jobs, job, curator):
    worker = WikiWorker()
    worker.manager = curator
    jobs.update(job["id"], status="running")
    await worker.process(jobs, job["id"])
    return jobs.get(job["id"])


def sapphire_delete(history, path):
    with history.locked():
        before = history.capture_external([path])
        history.store.path(path).unlink()
        history.finish_external({"id": uuid.uuid4().hex, "before": before, "paths": [path],
                                 "actor": "human", "reason": "delete", "trash_root": str(history.state / "trash")})
        history.store.refresh()


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
        restarted = WikiJobs(None)
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
    jobs.recover_jobs()
    assert jobs.next_queued() == job["id"]
    curator.crash = False
    worker = WikiWorker()
    worker.manager = curator
    await worker.process(jobs, job["id"])
    result = jobs.get(job["id"])
    assert result["status"] == "completed"
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
async def test_reuse_compares_original_txt_html_bytes_not_extracted_text(
    jobs, suffix, raw
):
    path = "Library/original" + suffix
    WikiHistory(jobs.store).write(path, raw.encode(), None, "human", "create")
    result = jobs.maintain("same", raw, "organize", [], "Library/new.md", [], "human")
    assert result["items"][0]["output"] == path
    assert jobs.store.path(path).read_bytes() == raw.encode()
    if suffix == ".html":
        different = jobs.maintain(
            "plain", "完整正文", "organize", [], "Library/plain.md", [], "human"
        )
        assert different["status"] == "queued"


async def test_restart_after_reserved_retry_does_not_reset_budget(jobs):
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
    assert WikiJobs(None).get(job["id"])["items"][0]["retry_count"] == 1
    jobs.recover_jobs()
    curator.create_session = original_create
    result = await process(jobs, job, curator)
    assert result["status"] == "completed"
    assert result["items"][0]["retry_count"] == 1
    assert curator.turns == 1


async def test_active_publication_protects_unindexed_output_until_recovered(
    jobs, monkeypatch
):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Note\noriginal\n", None, "human", "create")
    job = jobs.maintain(
        "index-crash", "", "organize", ["Library/note.md"], None, [], "human"
    )
    original_index = jobs.store._index

    def fail_after_publish(db, path):
        if b"octopus-section" in jobs.store.path(path).read_bytes():
            raise RuntimeError("index unavailable after publish")
        return original_index(db, path)

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
    assert result["status"] == "completed"
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
    assert result["items"][0]["index_recovery"] is True
    assert curator.turns == 1
    published_bytes = jobs.store.path("Library/note.md").read_bytes()
    with jobs.store.connect() as db:
        assert db.execute("SELECT indexed FROM job_publications").fetchone()[0] == 0
    # A fresh queue instance represents restart; retry restores only the index.
    monkeypatch.setattr(store_module, "extract", original_extract)
    restarted = WikiJobs(None)
    retried = restarted.retry(job["id"])
    assert retried["items"][0].get("retry_count", 0) == 0
    result = await process(restarted, job, curator)
    assert result["status"] == "completed"
    assert result["items"][0]["index_complete"] is True
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
    restarted = WikiJobs(None)
    if scenario == "cancelled_source_changed":
        restarted.mark_cancelled(job["id"])
        restarted.retry(job["id"])
    elif scenario == "index_error_source_changed":
        restarted.retry(job["id"])
    else:
        restarted.recover_jobs()
    outcome = await process(restarted, job, curator)
    assert outcome["status"] == ("completed" if scenario == "missing_fts" else "completed_with_attention"), outcome
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

    def crash_before_atomic(path, raw):
        if path == jobs.store.path("Library/note.md"):
            raise RuntimeError("controlled crash before atomic publication")
        return original_atomic(path, raw)

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
    restarted = WikiJobs(None)
    restarted.recover_jobs()
    outcome = await process(restarted, job, curator)
    assert outcome["status"] == "completed_with_attention", outcome
    item = outcome["items"][0]
    assert item["status"] == "attention" and item["index_recovery"] is True
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

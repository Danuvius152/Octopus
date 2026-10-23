"""Explicit Library asset Markdown maintenance retains normal safety gates."""

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, MockTransport, Response

from server.mcp_identity import mint
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.store import WikiError, revision
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests import test_wiki_history as wiki_history
from tests.submission import receipt_from_events
from tests.test_wiki_jobs import sapphire_delete

client = wiki_api.client
sapphire = wiki_history.sapphire
HEADERS = wiki_api.HEADERS
PATHS = [
    "Library/pika_vault/思考随笔/assets/人类与AI共同写作动画/项目说明.md",
    "Library/pika_vault/思考随笔/assets/写作是一种认知编码/项目说明.md",
    "Library/pika_vault/思考随笔/ASSETS/大小写/项目说明.MD",
]
ORIGINAL = "# 项目说明\n\n我认为只在条件 A 下成立。原单位为 10 ms。😀\n\n```python\nprint('保留')\n```\n"


@pytest.fixture
async def asset_notes(client, tmp_path):
    root = tmp_path / "vault"
    for path in [*PATHS, "Library/ordinary.md"]:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ORIGINAL, encoding="utf-8")
    config = await wiki_api.configure(client, root)
    return root, config, WikiJobs(None)


async def read(client, doc, headers=HEADERS, **params):
    result = await client.get("/api/wiki/read", params={"doc": doc, **params}, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


def organize_body(document, modern=True):
    body = {
        "path": document["path"], "expected_revision": document["revision"],
        "key_note": "项目的结论有条件，代码保留。", "reason": "明确维护当前项目说明",
    }
    if modern:
        body["frontmatter_patch"] = {
            "type": "Reference", "title": "项目说明", "description": "带条件的项目设计说明",
            "x_wiki": {"profile": "article-v1", "body_dialect": "obsidian"},
        }
    return body


@contextmanager
def assignment(jobs, config, paths, *, agent_id=None, job_id=None):
    job = jobs.get(job_id) if job_id else jobs.maintain("asset-test", "", "原位维护", paths, None, [], "human")
    session = SimpleNamespace(id="asset-worker", user_id=None, agent_id=agent_id or config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], [item for item in job["items"] if item["path"] in paths])
    token = tasks.open(session, "maintain")
    try:
        yield {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}, job, tasks.active[token]
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


class AssetCurator:
    """Hermetic model stand-in; WikiWorker owns the actual job/assignment."""

    def __init__(self, client, jobs, modern):
        self.client, self.jobs, self.modern = client, jobs, modern
        self.db = wiki_api.session_manager.db
        self.sessions, self.results = [], []

    async def create_session(self, *args, **kwargs):
        session = await wiki_api.session_manager.create_session(*args, **kwargs)
        self.sessions.append(session)
        return session

    async def archive_session(self, *args, **kwargs):
        await wiki_api.session_manager.archive_session(*args, **kwargs)

    async def start_message(self, session_id, prompt, **kwargs):
        assert kwargs["source"] == "wiki"
        return await receipt_from_events(self._events(session_id, prompt, **kwargs), kwargs["request_id"])

    async def cancel_request(self, *args):
        return True

    async def _await_backend_retirement(self, session):
        pass

    async def close_work(self, work_id):
        return await wiki_api.session_manager.close_work(work_id)

    async def finish_work_cancellation(self, affected):
        await wiki_api.session_manager.finish_work_cancellation(affected)

    async def _events(self, session_id, prompt, **kwargs):
        session = next(session for session in self.sessions if session.id == session_id)
        assert kwargs["wiki_mode"] == "maintain"
        token = tasks.open(session, "maintain", prompt, request_id=kwargs["request_id"])
        headers = {"Authorization": f"Bearer {mint(session_id, wiki_task_id=token)}"}
        task = tasks.active[token]
        try:
            job = self.jobs.get(task.job_id)
            for item in job["items"]:
                if item["status"] != "pending":
                    continue
                document = await read(self.client, item["doc"], headers, length=1)
                body = organize_body(document, self.modern)
                unread = await self.client.post("/api/wiki/organize", json=body, headers=headers)
                assert unread.status_code == 422 and "full current note" in unread.text
                # An explicit asset batch returns real text; the missing middle
                # still prevents this worker from claiming it read the article.
                request = {"doc": document["id"], "expected_revision": document["revision"]}
                partial = await self.client.post("/api/wiki/read_many", json={"items": [request], "budget_chars": 5}, headers=headers)
                assert partial.status_code == 200, partial.text
                assert partial.json()["items"][0]["status"] == "partial"
                assert partial.json()["ranges"][0]["content"] == ORIGINAL[:5]
                await read(self.client, document["id"], headers, start=document["characters"] - 1)
                gap = await self.client.post("/api/wiki/organize", json=body, headers=headers)
                assert gap.status_code == 422 and "full current note" in gap.text
                complete = await self.client.post("/api/wiki/read_many", json={"items": [request], "budget_chars": 24000}, headers=headers)
                assert complete.status_code == 200, complete.text
                assert complete.json()["items"][0]["status"] == "ok"
                assert complete.json()["ranges"][0]["content"] == ORIGINAL
                key = (document["id"], document["path"], document["revision"])
                assert tasks.coverage(task.reads[key], 0) == document["characters"]
                organized = await self.client.post("/api/wiki/organize", json=body, headers=headers)
                assert organized.status_code == 200, organized.text
                after_organize = self.jobs.store.path(document["path"]).read_bytes()
                assert ORIGINAL.split("\n\n", 1)[1].encode() in after_organize
                assert organized.json()["path"] == document["path"]
                blocks = await self.client.get("/api/wiki/blocks", params={"doc": document["id"], "expected_revision": organized.json()["revision"]}, headers=headers)
                assert blocks.status_code == 200, blocks.text
                part = next(block for block in blocks.json()["blocks"] if "原单位为 10 ms" in block["content"])
                start = part["content"].index("10 ms")
                patched = await self.client.post("/api/wiki/revise", json={
                    "doc": document["id"], "expected_revision": organized.json()["revision"],
                    "operation": "patch", "reason": "只修正已经确认的单位",
                    "edits": [{"locator": part["locator"], "expected_block_hash": part["sha256"], "start": start, "end": start + 5, "expected_text": "10 ms", "replacement": "10 s", "kind": "correction"}],
                }, headers=headers)
                assert patched.status_code == 200, patched.text
                assert self.jobs.store.path(document["path"]).read_bytes() == after_organize.replace(b"10 ms", b"10 s", 1)
                self.results.extend([organized.json(), patched.json()])
            yield {"type": "result", "content": "维护完成"}
        finally:
            tasks.close(token)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("modern", [True, False], ids=["article-v1", "compatibility"])
async def test_explicit_asset_markdown_durable_worker_receipts_index_and_restore(client, asset_notes, path, modern):
    root, _, jobs = asset_notes
    before = await read(client, path)
    assert before["kind"] == "asset"
    created = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "first-assets", "paths": [path], "scope": "完整阅读并原位维护，不改变正文"})
    assert created.status_code == 200, created.text
    job = created.json()
    assert job["items"][0]["doc"] == before["id"]
    assert "paired_doc" not in job["items"][0]
    jobs.update(job["id"], status="running")
    curator = AssetCurator(client, jobs, modern)
    worker = WikiWorker()
    worker.manager = curator
    await worker.process(jobs, job["id"])
    # Reopening the durable store must retain the receipts and completed item.
    finished = WikiJobs(None).get(job["id"])
    assert finished["status"] == "completed", finished
    assert finished["items"][0]["index_complete"] is True
    assert len(curator.sessions) == 1
    assert len(finished["receipts"]) == 2
    assert {receipt["doc_id"] for receipt in finished["receipts"]} == {before["id"]}
    history = WikiHistory(jobs.store)
    changes = {item["id"]: item for item in history.history(path)["items"]}
    for result in curator.results:
        change = changes[result["change_id"]]
        assert change["status"] == "applied"
        assert change["recoverable"] == {"before": True, "after": True}
        assert (history.blobs / change["before_hash"]).is_file()
        assert (history.blobs / change["after_hash"]).is_file()
        receipt = next(receipt for receipt in finished["receipts"] if receipt["id"] == result["receipt_id"])
        assert receipt["change_id"] == change["id"]
        assert receipt["input_revision"] == change["before_hash"]
        assert receipt["output_revision"] == change["after_hash"]
    with jobs.store.connect() as db:
        indexed = db.execute("SELECT revision,kind FROM documents WHERE id=?", (before["id"],)).fetchone()
        assert indexed["revision"] == curator.results[-1]["revision"]
        assert indexed["kind"] == "asset"
        assert db.execute("SELECT count(*),sum(indexed) FROM job_publications WHERE job_id=?", (job["id"],)).fetchone()[:] == (2, 2)
    # Navigation policy remains unchanged even after successful maintenance.
    for operation, params in [("documents", {}), ("search", {"query": path, "intent": "lookup"})]:
        default = await client.get(f"/api/wiki/{operation}", params=params, headers=HEADERS)
        assert before["id"] not in {item["id"] for item in default.json()["items"]}
        explicit = await client.get(f"/api/wiki/{operation}", params={**params, "include_assets": True}, headers=HEADERS)
        assert before["id"] in {item["id"] for item in explicit.json()["items"]}
    restored = await client.post("/api/wiki/restore", headers=HEADERS, json={"change_id": curator.results[0]["change_id"], "expected_revision": curator.results[-1]["revision"], "side": "before"})
    assert restored.status_code == 200, restored.text
    assert (root / path).read_bytes() == ORIGINAL.encode()
    assert (await read(client, before["id"]))["id"] == before["id"]
    human = ORIGINAL + "\n后来人工编辑，必须保留。\n"
    (root / path).write_text(human)
    stale = await client.post("/api/wiki/restore", headers=HEADERS, json={"change_id": curator.results[0]["change_id"], "expected_revision": restored.json()["revision"], "side": "after"})
    assert stale.status_code == 409, stale.text
    assert (root / path).read_text() == human
    assert not list(root.rglob("*.source.md.txt"))
    assert not (root / (path + ".md")).exists()


@pytest.mark.parametrize("gate", ["wrong_curator", "inactive_job", "unselected", "no_assignment", "identity", "revision"])
async def test_asset_exception_does_not_grant_write_authority(client, asset_notes, gate):
    root, config, jobs = asset_notes
    path = PATHS[0]
    original = await read(client, path)
    assigned = [PATHS[1]] if gate == "unselected" else [path]
    with assignment(jobs, config, assigned, agent_id="another-agent" if gate == "wrong_curator" else None) as (headers, job, task):
        await read(client, original["id"], headers)
        if gate == "inactive_job":
            jobs.update(job["id"], status="cancelled")
        elif gate == "no_assignment":
            # A maintainer task alone must not act as a worker assignment.
            tasks.active[next(token for token, value in tasks.active.items() if value is task)] = type(task)(task.session_id, task.user_id, task.agent_id, task.mode)
        elif gate == "identity":
            sapphire_delete(WikiHistory(jobs.store), path)
            WikiHistory(jobs.store).write(path, ORIGINAL.encode(), None, "human", "replacement identity")
            assert jobs.store.read(path)["id"] != original["id"]
        elif gate == "revision":
            (root / path).write_text(ORIGINAL + "\n新条件。\n")
        before = (root / path).read_bytes()
        denied = await client.post("/api/wiki/organize", headers=headers, json=organize_body(original))
        assert denied.status_code == (409 if gate == "revision" else 403), denied.text
        assert (root / path).read_bytes() == before
        assert not jobs.get(job["id"])["receipts"]


async def test_asset_job_submission_still_requires_trusted_selection(client, asset_notes):
    _, config, jobs = asset_notes
    session = SimpleNamespace(id="asset-submit", user_id=None, agent_id=config["curator_agent_id"])
    token = tasks.open(session, "maintain", "/wiki maintain some unrelated material")
    headers = {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}
    try:
        denied = await client.post("/api/wiki/maintain", headers=headers, json={"idempotency_key": "unselected-asset", "paths": [PATHS[0]]})
        assert denied.status_code == 403 and "explicitly" in denied.text
        assert jobs.list()["items"] == []
    finally:
        tasks.close(token)


async def test_asset_no_change_requires_full_read_and_preserves_history(client, asset_notes):
    root, config, jobs = asset_notes
    path = PATHS[0]
    original = await read(client, path)
    history = WikiHistory(jobs.store)
    with assignment(jobs, config, [path]) as (headers, job, _):
        body = {"doc": original["id"], "expected_revision": original["revision"], "operation": "no_change", "reason": "当前项目说明完整复核符合用途"}
        denied = await client.post("/api/wiki/revise", json=body, headers=headers)
        assert denied.status_code == 422 and "full current note" in denied.text
        returned = await client.post("/api/wiki/read_many", json={"items": [{"doc": original["id"], "expected_revision": original["revision"]}]}, headers=headers)
        assert returned.status_code == 200 and returned.json()["ranges"][0]["content"] == ORIGINAL
        result = await client.post("/api/wiki/revise", json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["changed"] is False and result.json()["change_id"] is None
        receipt = jobs.get(job["id"])["receipts"][0]
        assert receipt["id"] == result.json()["receipt_id"] and receipt["operation"] == "no_change"
        assert receipt["input_revision"] == receipt["output_revision"] == original["revision"]
    assert (root / path).read_bytes() == ORIGINAL.encode()
    assert not history.history(path)["items"]


async def test_sapphire_history_capture_delete_and_absent_path_restore(client, asset_notes):
    root, _, jobs = asset_notes
    path = PATHS[0]
    history = WikiHistory(jobs.store)
    before = await read(client, path)
    captured = history.capture_external([path])
    assert captured[path]["document_id"] == before["id"]
    sapphire_delete(history, path)
    deletion = history.history(path)["items"][0]
    assert deletion["before_hash"] == before["revision"]
    assert deletion["after_hash"] is None
    assert deletion["recoverable"]["before"] is True
    assert not (root / path).exists()
    restored = history.restore(deletion["id"], None, "before", "human")
    assert restored["revision"] == revision(ORIGINAL.encode())
    assert (root / path).read_bytes() == ORIGINAL.encode()
    current = await read(client, before["id"])
    assert current["id"] == before["id"] and current["path"] == path
    assert current["revision"] == restored["revision"]


async def test_other_assets_remain_unmaintainable_and_batch_excluded(client, asset_notes):
    root, _, jobs = asset_notes
    # Each rejection has no write effect; exercise every path against one vault.
    for path in [
        "Other/assets/project.md", "Library/assets/project.txt", "Library/assets/project.html",
        "Library/tmp/assets/project.md", "Library/assets/TEMP/project.MD",
        "Library/static/assets/project.md", "Library/assets/node_modules/project.md",
        "Codex Skills/assets/SKILL.md", "Library/Codex Skills/assets/SKILL.md",
        "Library/assets/file.pdf",
    ]:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ORIGINAL)
        document = await read(client, path)
        assert document["kind"] == "asset"
        submitted = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "excluded", "paths": [path]})
        assert submitted.status_code == 422, (path, submitted.text)
        for modern in (True, False):
            organized = await client.post("/api/wiki/organize", headers=HEADERS, json=organize_body(document, modern))
            assert organized.status_code == 422, (path, organized.text)
        batch = await client.post("/api/wiki/read_many", headers=HEADERS, json={"items": [{"doc": document["id"]}]})
        assert batch.status_code == 200, batch.text
        assert batch.json()["items"][0]["status"] == "excluded"
        assert not batch.json()["ranges"]
        assert not WikiHistory(jobs.store).capture_external([path])
        assert target.read_text() == ORIGINAL


async def test_asset_new_page_targets_and_media_stay_excluded(client, asset_notes):
    root, config, jobs = asset_notes
    target = "Library/assets/new.md"
    rejected = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "new-assets", "material": ORIGINAL, "target": target})
    assert rejected.status_code == 422, rejected.text
    with assignment(jobs, config, [PATHS[0]]) as (headers, _, _):
        await read(client, PATHS[0], headers)
        published = await client.post("/api/wiki/publish", headers=headers, json={"path": target, "kind": "concept", "content": "# New\n", "expected_revision": None, "reason": "must remain excluded"})
        assert published.status_code == 403, published.text
    assert not (root / target).exists()
    media = "Library/assets/image.png"
    (root / media).parent.mkdir(parents=True, exist_ok=True)
    (root / media).write_bytes(b"not markdown")
    denied = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "media", "paths": [media]})
    assert denied.status_code == 404, denied.text
    assert (root / media).read_bytes() == b"not markdown"
    assert not WikiHistory(jobs.store).capture_external([media])


async def test_asset_index_failure_recovers_without_repeating_content(client, asset_notes, monkeypatch):
    _, config, jobs = asset_notes
    path = PATHS[0]
    original = await read(client, path)
    original_index = jobs.store._index

    def fail_published(db, relative, **kwargs):
        if relative == path and "## Key note" in jobs.store.path(path).read_text():
            raise RuntimeError("isolated asset index failure")
        return original_index(db, relative, **kwargs)

    with assignment(jobs, config, [path]) as (headers, job, _):
        await read(client, original["id"], headers)
        monkeypatch.setattr(jobs.store, "_index", fail_published)
        with pytest.raises(RuntimeError, match="isolated asset index failure"):
            await client.post("/api/wiki/organize", json=organize_body(original), headers=headers)
        published = jobs.store.path(path).read_bytes()
        with jobs.store.connect() as db:
            assert db.execute("SELECT indexed FROM job_publications").fetchone()[0] == 0
        monkeypatch.setattr(jobs.store, "_index", original_index)
        item = dict(job["items"][0])
        assert jobs.recover_publication(job["id"], item)
        assert item["status"] == "done" and item["index_complete"] is True
        assert jobs.store.path(path).read_bytes() == published
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*),sum(indexed) FROM job_publications").fetchone()[:] == (1, 1)
            assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 1
    assert len(WikiHistory(jobs.store).history(path)["items"]) == 1


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".txt", ".html", ".htm"])
@pytest.mark.parametrize("modern", [True, False], ids=["article-v1", "compatibility"])
async def test_ordinary_text_keeps_explicit_reads_and_markdown_only_maintenance(client, asset_notes, suffix, modern):
    root, config, jobs = asset_notes
    path = "Library/ordinary" + suffix
    text = "Ordinary evidence remains conditional on A.\n"
    raw = ("<p>" + text.strip() + "</p>\n") if suffix in {".html", ".htm"} else text
    (root / path).write_text(raw)
    original = await read(client, path)
    assert original["kind"] != "asset"
    if suffix not in {".md", ".markdown"}:
        returned = await client.post("/api/wiki/read_many", headers=HEADERS, json={"items": [{"doc": original["id"], "expected_revision": original["revision"]}]})
        assert returned.status_code == 200, returned.text
        assert returned.json()["items"][0]["status"] == "ok"
        assert returned.json()["ranges"][0]["content"] == original["content"]
        with pytest.raises(WikiError) as rejected:
            with assignment(jobs, config, [path]):
                pytest.fail("Non-Markdown maintenance was assigned")
        assert rejected.value.status == 422
        result = await client.post("/api/wiki/organize", headers=HEADERS, json=organize_body(original, modern))
        assert result.status_code == 422 and "Markdown" in result.text
        assert (root / path).read_text() == raw
        assert (await read(client, path))["id"] == original["id"]
        assert not (root / (path + ".md")).exists()
        assert jobs.list()["items"] == []
        return
    with assignment(jobs, config, [path]) as (headers, job, _):
        returned = await client.post("/api/wiki/read_many", headers=headers, json={"items": [{"doc": original["id"], "expected_revision": original["revision"]}]})
        assert returned.status_code == 200, returned.text
        assert returned.json()["items"][0]["status"] == "ok"
        assert returned.json()["ranges"][0]["content"] == original["content"]
        result = await client.post("/api/wiki/organize", headers=headers, json=organize_body(original, modern))
        assert result.status_code == 200, result.text
        output_path = path
        assert result.json()["path"] == output_path
        assert text in (root / path).read_text()
        receipt = jobs.get(job["id"])["receipts"][0]
        output = await read(client, output_path)
        assert receipt["doc_id"] == output["id"]
        assert receipt["item_doc"] == original["id"]
        assert receipt["details"]["input_source_revision"] == original["revision"]
        assert receipt["input_revision"] == original["revision"]
        change = WikiHistory(jobs.store).history(output_path)["items"][0]
        assert change["recoverable"]["after"] is True


@pytest.mark.parametrize("invalid", ["traversal", "absolute", "backslash", "symlink"])
async def test_asset_exception_does_not_bypass_path_confinement(client, asset_notes, invalid):
    root, config, jobs = asset_notes
    outside = root.parent / "outside.md"
    outside.write_text("Private outside evidence must remain untouched.\n")
    if invalid == "traversal":
        path = "Library/assets/../../../outside.md"
    elif invalid == "absolute":
        path = str(outside)
    elif invalid == "backslash":
        path = "Library\\assets\\outside.md"
    else:
        path = "Library/assets/outside.md"
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).symlink_to(outside)
    before = outside.read_bytes()
    denied = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "confined", "paths": [path]})
    assert denied.status_code == 404, denied.text
    assert jobs.list()["items"] == []
    with assignment(jobs, config, [PATHS[0]]) as (headers, job, _):
        denied = await client.get("/api/wiki/read", params={"doc": path}, headers=headers)
        assert denied.status_code == 404, denied.text
        for modern in (True, False):
            denied = await client.post("/api/wiki/organize", headers=headers, json=organize_body({"path": path, "revision": revision(before)}, modern))
            assert denied.status_code == 404, denied.text
        assert not jobs.get(job["id"])["receipts"]
    assert outside.read_bytes() == before


@pytest.fixture
def mutation_bridge(monkeypatch):
    from server.wiki import app_bridge

    # Owners are registered through follow_job; isolate their service lifetime.
    assert not app_bridge._pending
    monkeypatch.setattr(app_bridge, "_pending", {})
    yield app_bridge
    assert all(task.done() and not task.cancelled() and task.exception() is not None
               for task in app_bridge._pending)


async def test_mutation_shutdown_timeout_keeps_writer_lock_until_journal_finishes(sapphire, mutation_bridge, monkeypatch):
    row, history = sapphire
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    finish = WikiHistory.finish_external

    def delayed_finish(self, entry):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return finish(self, entry)

    monkeypatch.setattr(WikiHistory, "finish_external", delayed_finish)
    mutation = await mutation_bridge.begin(row, "POST", "git", {"vault": "obv"}, json.dumps({"action": "sync"}).encode())
    history.store.path("note.md").write_bytes(b"edited")
    remote = AsyncClient(transport=MockTransport(lambda request: Response(200, json={"status": "done"})))
    mutation.follow_job(remote, "http://sapphire", "journal-owner", row["id"])
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(RuntimeError, match="Wiki application writer teardown remains unconfirmed"):
            await mutation_bridge.shutdown(timeout=.02)

        from server.wiki.store import WikiError
        with pytest.raises(WikiError, match="stopped during service transition"):
            await mutation_bridge.begin(row, "POST", "save", {"vault": "obv"},
                                        json.dumps({"path": "note.md", "content": "edited"}).encode())
        import fcntl
        with (history.state / "vault.lock").open("a+b") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (history.state / "external-change.json").is_file()

        release.set()
        await mutation_bridge.shutdown(timeout=3)
        mutation_bridge.reopen()
        next_mutation = await mutation_bridge.begin(row, "POST", "save", {"vault": "obv"},
                                                    json.dumps({"path": "note.md", "content": "edited"}).encode())
        await next_mutation.finish()
        assert remote.is_closed
        assert history.store.read("note.md")["content"] == "edited"
        assert len(history.history("note.md")["items"]) == 1
        assert not (history.state / "external-change.json").exists()
    finally:
        release.set()
        await mutation_bridge.shutdown()
        await remote.aclose()


async def test_mutation_shutdown_reports_and_retains_failed_journal_owner(sapphire, mutation_bridge, monkeypatch, caplog):
    row, history = sapphire

    def failed_finish(self, entry):
        raise OSError("journal unavailable")

    monkeypatch.setattr(WikiHistory, "finish_external", failed_finish)
    mutation = await mutation_bridge.begin(row, "POST", "git", {"vault": "obv"}, json.dumps({"action": "sync"}).encode())
    history.store.path("note.md").write_bytes(b"edited")
    remote = AsyncClient(transport=MockTransport(lambda request: Response(200, json={"status": "done"})))
    mutation.follow_job(remote, "http://sapphire", "failed-journal-owner", row["id"])
    try:
        # Let the normal completion callback report the storage failure before
        # shutdown inspects owners; an already-finished failure must survive.
        async with asyncio.timeout(3):
            while not any(record.getMessage() == "Wiki application journal failed" for record in caplog.records):
                await asyncio.sleep(.01)

        with pytest.raises(RuntimeError, match="Wiki application writer teardown remains unconfirmed") as failure:
            await mutation_bridge.shutdown()
        assert isinstance(failure.value.__cause__, OSError)
        with pytest.raises(RuntimeError, match="Wiki application writer teardown remains unconfirmed"):
            await mutation_bridge.shutdown(timeout=0)
        assert remote.is_closed
        assert (history.state / "external-change.json").is_file()
    finally:
        await remote.aclose()


async def test_shutdown_joins_delete_receipt_before_releasing_journal(sapphire, mutation_bridge, monkeypatch):
    row, history = sapphire
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    atomic = mutation_bridge.atomic

    def delayed_atomic(path, data):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return atomic(path, data)

    mutation = await mutation_bridge.begin(row, "POST", "delete", {"vault": "obv"},
                                           json.dumps({"path": "note.md"}).encode())
    monkeypatch.setattr(mutation_bridge, "atomic", delayed_atomic)
    receipt = asyncio.create_task(mutation.receipt({"id": "a" * 32}))
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        shutdown = asyncio.create_task(mutation_bridge.shutdown())
        await asyncio.sleep(.05)
        assert not shutdown.done(), "shutdown released a journal whose receipt is still writing"
        assert not mutation.handle.closed
        release.set()
        await receipt
        await shutdown
        assert mutation.handle.closed
        assert not mutation.journal.exists()
        # Late response callbacks cannot resurrect a retired journal.
        await mutation.receipt({"id": "b" * 32})
        assert not mutation.journal.exists()
    finally:
        release.set()
        await receipt
        if shutdown is not None:
            await shutdown
        await mutation.finish()

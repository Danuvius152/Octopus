"""Read historical output by receipt while current text and paths keep changing."""

import asyncio
import fcntl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.wiki import job_execution
from server.wiki.history import WikiHistory
from tests.test_wiki_grant_v2 import assigned
from tests.wiki_job_runtime import Curator, process
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs


@pytest.mark.parametrize("stage", ["prepared", "before_prepare", "ingest"])
async def test_prior_attempt_detail_retains_complete_job_error_after_retry(jobs, client, stage):
    from tests.test_wiki_job_events import enqueue
    from tests.test_wiki_job_execution import prepare

    if stage == "prepared":
        job, reference = prepare(jobs, enqueue(jobs))
        job_execution.transition(jobs, job["id"], reference, "rejected", disposition="not_admitted", retired=True)
    else:
        job = enqueue(jobs) if stage == "before_prepare" else jobs.maintain(
            "failed-ingest", "# Input\nRetain original material.\n", "Review", [], None, [], "human")
        jobs.next_queued()
        job = jobs.get(job["id"])
    diagnostic = "Original full job failure\n" * 2000
    jobs.update(job["id"], status="failed", error=diagnostic, session_id=None)
    jobs.retry(job["id"])
    jobs.next_queued()
    response = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=client.headers,
                                params={"attempt": job["attempt"]})
    assert response.status_code == 200, response.text
    assert response.json()["error"] == diagnostic
    assert response.json()["current_attempt"] == job["attempt"] + 1
    if stage != "prepared":
        assert response.json()["items"] == []


async def test_restart_preserves_original_diagnostic_when_verification_continues_same_attempt(jobs, client):
    from tests.test_wiki_job_events import enqueue

    job = jobs.update(enqueue(jobs)["id"], status="running")
    diagnostic = "Original interrupted execution detail\n" * 2000
    jobs.update(job["id"], status="interrupted", error=diagnostic)
    jobs.recover_jobs(previous_writers_retired=True)
    assert jobs.get(job["id"])["error"] == diagnostic
    assert jobs.next_queued() == job["id"]
    response = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=client.headers,
                                params={"attempt": job["attempt"]})
    assert response.status_code == 200, response.text
    assert response.json()["error"] == diagnostic


async def test_historical_detail_pages_original_bytes_after_move_without_restoring(jobs, client):
    WikiHistory(jobs.store).write("Library/note.md", b"# Original\nRetain this condition.\n", None, "human", "create")
    job = jobs.maintain("historical-output", "", "Review", ["Library/note.md"], None, [], "human")
    manager = Curator(jobs)
    done = await process(jobs, job, manager)
    receipt = done["receipts"][0]
    original = jobs.store.path(receipt["path"]).read_text()
    jobs.store.path(receipt["path"]).rename(jobs.store.path("Library/moved.md"))
    jobs.store.refresh()
    current = original + "\nA later human condition.\n"
    jobs.store.path("Library/moved.md").write_text(current)
    jobs.store.refresh()
    full_error = "Complete diagnostic: " + "detail\n" * 2000
    jobs.update(job["id"], error=full_error)
    before = jobs.get(job["id"])
    detail = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=client.headers,
                              params={"attempt": done["attempt"]})
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["error"] == full_error
    output = data["outputs"][0]
    assert output["receipt_id"] == receipt["id"]
    assert output["current_path"] == "Library/moved.md"
    assert output["revision"] == receipt["output_revision"]
    endpoint = f"/api/wiki/jobs/{job['id']}/outputs/{receipt['id']}"
    page_length = (len(original) + 1) // 2
    parts, start = [], 0
    while start is not None:
        response = await client.get(endpoint, headers=client.headers, params={
            "attempt": done["attempt"], "view": "history", "start": start, "length": page_length,
        })
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["view"] == "history" and page["revision"] == receipt["output_revision"]
        parts.append(page["content"])
        start = page["next_start"]
    assert len(parts) == 2
    assert "".join(parts) == original
    response = await client.get(endpoint, headers=client.headers, params={"attempt": done["attempt"], "view": "current"})
    assert response.status_code == 200, response.text
    assert response.json()["content"] == current
    assert jobs.store.path("Library/moved.md").read_text() == current
    assert jobs.get(job["id"])["state_version"] == before["state_version"]
    assert manager.turns == 1


async def test_no_change_detail_keeps_reason_and_original_body_after_external_edit(jobs, client):
    raw = b"# Original\nAlready meets the requirement.\n"
    jobs.store.path("Library/note.md").parent.mkdir(exist_ok=True)
    jobs.store.path("Library/note.md").write_bytes(raw)
    jobs.store.refresh()
    job = jobs.maintain("no-change-history", "", "Review", ["Library/note.md"], None, [], "human")
    async with assigned(job, jobs.registration, client=client) as (headers, _):
        document = (await client.get("/api/wiki/read", headers=headers, params={"doc": job["items"][0]["doc"]})).json()
        saved = await client.post("/api/wiki/revise", headers=headers, json={
            "operation": "no_change", "doc": document["id"], "expected_revision": document["revision"],
            "reason": "Every original condition is already present.",
        })
        assert saved.status_code == 200, saved.text
    current = jobs.get(job["id"])
    jobs.store.path("Library/note.md").write_text("# Later\nIndependent human edit.\n")
    jobs.store.refresh()
    detail = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=client.headers)
    assert detail.json()["outputs"][0]["reason"] == "Every original condition is already present."
    output = await client.get(f"/api/wiki/jobs/{job['id']}/outputs/{saved.json()['receipt_id']}",
                              headers=client.headers, params={"attempt": current["attempt"]})
    assert output.status_code == 200, output.text
    assert output.json()["content"].encode() == raw


async def test_historical_output_and_real_gc_share_original_byte_lifetime(jobs, client, monkeypatch):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Original\nRetain this historical output.\n", None, "human", "create")
    job = jobs.maintain("gc-output", "", "Review", ["Library/note.md"], None, [], "human")
    done = await process(jobs, job, Curator(jobs))
    receipt = done["receipts"][0]
    blob = history.blobs / receipt["output_revision"]
    original = blob.read_bytes()
    current = receipt["output_revision"]
    for number in range(14):
        current = history.write("Library/note.md", f"# Later {number}\nIndependent words.\n".encode(), current, "human", "later edit")["revision"]
    endpoint = f"/api/wiki/jobs/{job['id']}/outputs/{receipt['id']}"
    opening, gc_probed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    opened, flock = Path.open, fcntl.flock
    gc_blocked = False

    def pause_open(path, mode="r", *args, **kwargs):
        if path == blob and mode == "rb":
            loop.call_soon_threadsafe(opening.set)
            assert release.wait(5), "Release the original output read"
        return opened(path, mode, *args, **kwargs)

    def observe_gc_lock(handle, flags):
        nonlocal gc_blocked
        if flags == fcntl.LOCK_EX and getattr(handle, "name", None) == str(history.state / "vault.lock"):
            try:
                flock(handle, flags | fcntl.LOCK_NB)
            except BlockingIOError:
                gc_blocked = True
            loop.call_soon_threadsafe(gc_probed.set)
        return flock(handle, flags)

    monkeypatch.setattr(Path, "open", pause_open)
    reading = asyncio.create_task(client.get(endpoint, headers=client.headers, params={"attempt": done["attempt"]}))
    cleanup = None
    try:
        await asyncio.wait_for(opening.wait(), 3)
        monkeypatch.setattr(fcntl, "flock", observe_gc_lock)
        cleanup = asyncio.create_task(asyncio.to_thread(history.cleanup, datetime.now(UTC) + timedelta(days=40)))
        await asyncio.wait_for(gc_probed.wait(), 3)
        if not gc_blocked:
            await asyncio.wait_for(asyncio.shield(cleanup), 3)
        release.set()
        response = await asyncio.wait_for(reading, 3)
        assert response.status_code == 200, response.text
        assert response.json()["content"].encode() == original
        await asyncio.wait_for(cleanup, 3)
        assert not blob.exists()
        expired = await client.get(endpoint, headers=client.headers, params={"attempt": done["attempt"]})
        assert expired.status_code == 410, expired.text
        assert jobs.store.path(receipt["path"]).read_text() == "# Later 13\nIndependent words.\n"
    finally:
        release.set()
        await asyncio.gather(reading, *([cleanup] if cleanup else []), return_exceptions=True)


@pytest.mark.parametrize("failure", ["missing", "corrupt", "permission"])
async def test_historical_output_distinguishes_unavailable_corrupt_and_storage_failure(jobs, client, monkeypatch, failure):
    history = WikiHistory(jobs.store)
    history.write("Library/note.md", b"# Original\nAn original condition.\n", None, "human", "create")
    job = jobs.maintain("damaged-output", "", "Review", ["Library/note.md"], None, [], "human")
    done = await process(jobs, job, Curator(jobs))
    receipt = done["receipts"][0]
    blob = history.blobs / receipt["output_revision"]
    current = jobs.store.path(receipt["path"]).read_bytes()
    before = jobs.get(job["id"])
    if failure == "missing":
        blob.unlink()
    elif failure == "corrupt":
        blob.write_bytes(b"Corrupted historical bytes")
    else:
        opened = Path.open

        def refuse_open(path, mode="r", *args, **kwargs):
            if path == blob and mode == "rb":
                raise PermissionError("Original history storage permission denied")
            return opened(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_open)
    endpoint = f"/api/wiki/jobs/{job['id']}/outputs/{receipt['id']}"
    if failure == "permission":
        with pytest.raises(PermissionError, match="permission denied"):
            await client.get(endpoint, headers=client.headers, params={"attempt": done["attempt"]})
    else:
        result = await client.get(endpoint, headers=client.headers, params={"attempt": done["attempt"]})
        assert result.status_code == (410 if failure == "missing" else 409), result.text
        assert "unavailable" in result.text if failure == "missing" else "hash mismatch" in result.text
    assert jobs.store.path(receipt["path"]).read_bytes() == current
    after = jobs.get(job["id"])
    assert after["receipts"] == before["receipts"]
    assert after["state_version"] == before["state_version"]

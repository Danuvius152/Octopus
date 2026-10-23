"""Read historical output by receipt while current text and paths keep changing."""

import pytest

from server.wiki import job_execution
from server.wiki.history import WikiHistory
from tests import test_wiki_api as wiki_api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


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
    response = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=wiki_api.HEADERS,
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
    response = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=wiki_api.HEADERS,
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
    detail = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=wiki_api.HEADERS,
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
        response = await client.get(endpoint, headers=wiki_api.HEADERS, params={
            "attempt": done["attempt"], "view": "history", "start": start, "length": page_length,
        })
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["view"] == "history" and page["revision"] == receipt["output_revision"]
        parts.append(page["content"])
        start = page["next_start"]
    assert len(parts) == 2
    assert "".join(parts) == original
    response = await client.get(endpoint, headers=wiki_api.HEADERS, params={"attempt": done["attempt"], "view": "current"})
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
    with assigned(job, jobs.registration) as (headers, _):
        document = (await client.get("/api/wiki/read", headers=headers, params={"doc": job["items"][0]["doc"]})).json()
        saved = await client.post("/api/wiki/revise", headers=headers, json={
            "operation": "no_change", "doc": document["id"], "expected_revision": document["revision"],
            "reason": "Every original condition is already present.",
        })
        assert saved.status_code == 200, saved.text
    current = jobs.get(job["id"])
    jobs.store.path("Library/note.md").write_text("# Later\nIndependent human edit.\n")
    jobs.store.refresh()
    detail = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=wiki_api.HEADERS)
    assert detail.json()["outputs"][0]["reason"] == "Every original condition is already present."
    output = await client.get(f"/api/wiki/jobs/{job['id']}/outputs/{saved.json()['receipt_id']}",
                              headers=wiki_api.HEADERS, params={"attempt": current["attempt"]})
    assert output.status_code == 200, output.text
    assert output.json()["content"].encode() == raw

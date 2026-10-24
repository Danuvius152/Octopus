"""Current job evidence survives batches and later attempts; old input refuses."""

import json

import pytest

from server.wiki import job_execution, job_items
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.mark.parametrize("old", [
    {"status": "pending"},
    {"version": True, "status": "pending"},
    {"version": 1, "status": "pending", "worker_request": {"request_id": "original"}},
    *[{"version": 1, "status": "pending", field: False} for field in (
        "legacy_evidence", "recording_failure", "recording_recovery", "execution_unknown", "index_recovery", "cancelled_before_admission",
    )],
])
def test_current_reader_rejects_retired_encoding_without_reinterpreting_it(old):
    with pytest.raises(WikiError):
        job_items.decode(json.dumps([{"doc": "original"} | old]))


async def test_batches_keep_original_references_and_opaque_evidence(jobs):
    paths = [f"Library/{number}.md" for number in range(4)]
    for path in paths:
        WikiHistory(jobs.store).write(path, b"# Original\nKeep this claim.\n", None, "human", "create")
    job = jobs.maintain("batches", "", "Review", paths, None, [], "human")
    job = jobs.update(job["id"], status="running")
    first = job_execution.prepare(jobs, job, job["items"][:3], "first-session", "first-request")
    job_execution.transition(jobs, job["id"], first, "settled", recording_state="committed")
    current = jobs.get(job["id"])
    for item in current["items"][:3]:
        item["status"] = "done"
    current = jobs.update(job["id"], items=current["items"])
    second = job_execution.prepare(jobs, current, current["items"][3:], "second-session", "second-request")
    job_execution.transition(jobs, job["id"], first, "settled", recording_state="committed")
    current = jobs.get(job["id"])
    assert [item["worker_request"]["request_id"] for item in current["items"]] == [
        "first-request", "first-request", "first-request", "second-request",
    ]
    assert all(item["version"] == 1 for item in current["items"])
    current_item = current["items"][3] | {
        "worker_request": second | {"phase": "unknown", "vendor_fact": {"offset": 17}},
        "status": "unknown",
    }
    with jobs.store.connect() as db:
        db.execute("UPDATE jobs SET items=? WHERE id=?", (json.dumps([current_item]), job["id"]))
    item = jobs.get(job["id"])["items"][0]
    assert item["status"] == "unknown"
    assert item["worker_request"]["vendor_fact"] == {"offset": 17}
    assert "recording_failure" not in item
    saved = jobs.update(job["id"], items=[item])["items"][0]
    assert saved == item
    job_execution.transition(jobs, job["id"], second, "settled", recording_state="committed", retired=True)
    jobs.update(job["id"], status="failed", items=[jobs.get(job["id"])["items"][0] | {"status": "pending"}], session_id=None)
    jobs.retry(job["id"])
    jobs.next_queued()
    before = jobs.get(job["id"])
    with pytest.raises(WikiError, match="superseded"):
        job_execution.transition(jobs, job["id"], first, "settled", recording_state="committed")
    assert jobs.get(job["id"])["state_version"] == before["state_version"]


@pytest.mark.parametrize("corrupt", [{"worker_request": []}, {"previous_worker_requests": {}}, {"status": "typo"}])
def test_malformed_current_items_are_rejected_at_the_json_boundary(corrupt):
    with pytest.raises(WikiError, match="malformed"):
        job_items.decode(json.dumps([{"doc": "original", "status": "pending", "version": 1} | corrupt]))

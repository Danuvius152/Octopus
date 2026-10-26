"""Native cancellation keeps the dispatch gate until its captured writer retires."""

import asyncio
import threading

import pytest

from server.wiki import job_returns, registry
from server.wiki.job_returns import dispatch_gate
from server.wiki.jobs import WikiJobs
from tests import test_wiki_api as wiki_api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.mark.parametrize("action", ["retry", "readmit"])
async def test_native_cancel_does_not_release_the_gate_before_control_commit(jobs, client, monkeypatch, action):
    job = enqueue(jobs)
    jobs.update(job["id"], status="running")
    previous = jobs.update(job["id"], status="completed_with_attention")
    if action == "readmit":
        registry.configure(jobs.user_id, jobs.store.root, "replacement-curator")
        jobs = WikiJobs(jobs.user_id)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    writer = getattr(WikiJobs, action)
    endpoint = []
    real_gate = dispatch_gate(wiki_api.session_manager, jobs.user_id)

    class CapturedGate:
        async def __aenter__(self):
            endpoint.append(asyncio.current_task())
            return await real_gate.__aenter__()

        async def __aexit__(self, *args):
            return await real_gate.__aexit__(*args)

    monkeypatch.setattr(job_returns, "dispatch_gate", lambda *_: CapturedGate())

    def paused_writer(self, *args, **kwargs):
        entered.set()
        assert release.wait(5), "Controlled writer was not released"
        try:
            return writer(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(WikiJobs, action, paused_writer)
    fields = {"expected_attempt": previous["attempt"], "expected_vault_id": jobs.registration["vault_id"]}
    if action == "readmit":
        fields["paths"] = [job["items"][0]["path"]]
    request = asyncio.create_task(client.post(f"/api/wiki/jobs/{job['id']}/{action}", json=fields))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        gate = real_gate
        assert gate.locked()
        endpoint[0].cancel()
        # Task.cancel schedules this request before this task's next ready turn.
        # Its synchronous unwinding must not retire a still-running writer.
        await asyncio.sleep(0)
        assert gate.locked(), "Native cancellation released admission while the writer was still active"
        assert not request.done()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        await asyncio.gather(request, return_exceptions=True)
    assert not gate.locked()
    with jobs.store.connect() as db:
        assert db.execute("SELECT dispatch_generation FROM wiki_job_state WHERE job_id=?", (job["id"],)).fetchone()[0] == 1

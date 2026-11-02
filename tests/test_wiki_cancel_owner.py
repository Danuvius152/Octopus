"""Waiting HTTP callers never own or duplicate retirement of a Wiki request."""

import asyncio
import threading

import pytest

from server.wiki import job_execution
from server.wiki.jobs import WikiJobs, WikiWorker
from tests.test_wiki_job_events import enqueue
from tests.wiki_job_runtime import Curator
from tests.wiki_job_runtime import client as client
from tests.wiki_jobs import jobs as jobs


async def test_confirmed_cancellations_release_finished_task_references(jobs):
    root = str(jobs.store.state.resolve())
    for index in range(4):
        job = jobs.maintain(f"retire-{index}", "", "Create requested knowledge", [], None, [], "human",
                            creation={"purpose": "requested_knowledge_page", "paths": [f"Library/new-{index}.md"]})
        await jobs.cancel(job["id"], None)
        await jobs.settle_cancelled(job["id"], None)
        assert jobs.get(job["id"])["settlement_phase"] == "confirmed"
    await asyncio.sleep(0)
    assert not [key for key in WikiWorker.cancellations if key[0] == root]


async def test_old_completion_does_not_remove_new_verification_owner(jobs, monkeypatch):
    job = jobs.maintain("replacement-owner", "", "Create requested knowledge", [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    key = (str(jobs.store.state.resolve()), job["id"], job["attempt"])
    create_task = asyncio.create_task
    finish = WikiJobs._finish_cancelled
    replacement, entered, release = [], asyncio.Event(), asyncio.Event()

    async def held_verification(owner, current, manager, **kwargs):
        if replacement:
            entered.set()
            await release.wait()
        await finish(owner, current, manager, **kwargs)

    def register_before_observer(coro, **kwargs):
        task = create_task(coro, **kwargs)
        if kwargs.get("name", "").startswith("wiki-cancel-") and not replacement:
            def replace_finished(done):
                if not replacement:
                    replacement.append(WikiWorker.cancel_attempt(jobs, jobs.get(job["id"]), None, verify=True))
            task.add_done_callback(replace_finished)
        return task

    monkeypatch.setattr(WikiJobs, "_finish_cancelled", held_verification)
    monkeypatch.setattr(asyncio, "create_task", register_before_observer)
    try:
        await jobs.cancel(job["id"], None)
        await asyncio.wait_for(entered.wait(), 3)
        assert WikiWorker.cancellations[key] is replacement[0]
    finally:
        release.set()
        await asyncio.gather(*replacement, return_exceptions=True)


async def test_confirmation_waits_for_original_unused_path_release(jobs, monkeypatch):
    job = jobs.maintain("cancel-before-creation", "", "Create requested knowledge", [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    releasing, release, released = threading.Event(), threading.Event(), threading.Event()
    release_unused = WikiJobs.release_unused
    calls = []

    def blocked(owner, job_id):
        calls.append(job_id)
        releasing.set()
        try:
            assert release.wait(5)
            return release_unused(owner, job_id)
        finally:
            released.set()

    monkeypatch.setattr(WikiJobs, "release_unused", blocked)
    verification = None
    try:
        returned = await jobs.cancel(job["id"], None)
        assert await asyncio.to_thread(releasing.wait, 3)
        assert returned["status"] == "cancelled" and returned["settlement_phase"] == "pending"
        assert jobs.get(job["id"])["grant"]["reservations"][0]["state"] == "reserved"
        verification = asyncio.create_task(jobs.settle_cancelled(job["id"], None))
        await asyncio.wait({verification}, timeout=0.05)
        assert not verification.done(), "Verification must join the original cancellation's cleanup"
        release.set()
        await asyncio.wait_for(verification, 3)
        current = jobs.get(job["id"])
        assert current["settlement_phase"] == "confirmed"
        assert current["grant"]["reservations"][0]["state"] == "released"
        assert calls == [job["id"]]
    finally:
        release.set()
        if verification is not None:
            await asyncio.gather(verification, return_exceptions=True)
        assert await asyncio.to_thread(released.wait, 3)
        await jobs.settle_cancelled(job["id"], None)


async def test_cancel_waiter_timeout_and_repeat_join_one_original_retirement(jobs):
    entered, stop_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Blocked(Curator):
        stops = 0

        async def _events(self, *args, **kwargs):
            entered.set()
            await release.wait()
            yield {"type": "result", "content": "Stopped", "is_error": True}

        async def cancel_request(self, *args):
            self.stops += 1
            stop_entered.set()
            await release.wait()
            return True

    job = jobs.update(enqueue(jobs)["id"], status="running")
    manager = Blocked(jobs)
    worker = WikiWorker()
    worker.manager = manager
    running = asyncio.create_task(worker.process(jobs, job["id"]))
    first = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        first = asyncio.create_task(jobs.cancel(job["id"], manager))
        await asyncio.wait_for(stop_entered.wait(), 5)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        repeated = await asyncio.wait_for(jobs.cancel(job["id"], manager), 1)
        assert repeated["settlement_phase"] == "pending"
        assert manager.stops == 1
        release.set()
        await running
        await jobs.settle_cancelled(job["id"], manager)
        assert jobs.get(job["id"])["settlement_phase"] == "confirmed"
        version = jobs.get(job["id"])["state_version"]
        await jobs.cancel(job["id"], manager)
        assert jobs.get(job["id"])["state_version"] == version
        assert manager.stops == 1
    finally:
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        await asyncio.gather(running, return_exceptions=True)


async def test_cancel_during_ingest_joins_original_file_thread_before_settlement(jobs, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    ingest = WikiJobs.ingest

    def blocked(owner, job_id):
        result = ingest(owner, job_id)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(WikiJobs, "ingest", blocked)
    job = jobs.maintain("ingest-thread", "# Material\nRetain the original.\n", "Review", [], None, [], "human")
    jobs.next_queued()
    manager = Curator(jobs)
    worker = WikiWorker()
    worker.manager = manager
    running = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(jobs.cancel(job["id"], manager), 0.01)
        current = await jobs.cancel(job["id"], manager)
        assert current["settlement_phase"] == "pending"
        assert not running.done()
        assert manager.turns == 0
        release.set()
        await running
        settled = jobs.get(job["id"])
        assert settled["settlement_phase"] == "confirmed"
        assert settled["receipts"][0]["operation"] == "ingest"
        assert settled["items"][0]["worker_request"]["disposition"] == "not_admitted"
        assert manager.turns == manager.sessions == 0
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


async def test_failed_bg_cleanup_still_cancels_and_joins_every_original_resource(jobs, monkeypatch):
    from server.bg_tasks import bg_task_manager
    from server.research import research_manager
    from server.wiki.history import WikiHistory

    for path in ("Library/first.md", "Library/second.md"):
        WikiHistory(jobs.store).write(path, b"# Input\nRetain the condition.\n", None, "human", "create")
    job = jobs.maintain("cleanup-failure", "", "Review both", ["Library/first.md", "Library/second.md"], None, [], "human")
    job = jobs.update(job["id"], status="running")
    first = job_execution.prepare(jobs, job, job["items"][:1], "first-session", "first-request")
    job = jobs.get(job["id"])
    second = job_execution.prepare(jobs, job, job["items"][1:], "second-session", "second-request")
    entered, release = asyncio.Event(), asyncio.Event()
    research, stopped, backend = set(), set(), []

    async def bg_cleanup(work_id):
        if work_id == first["work_id"]:
            raise RuntimeError("Original BG cleanup failed")

    async def research_cleanup(work_id):
        research.add(work_id)
        entered.set()
        await release.wait()

    class Captured(Curator):
        async def cancel_request(self, session_id, request_id):
            stopped.add((session_id, request_id))

        async def finish_work_cancellation(self, affected):
            await release.wait()
            backend.append(affected)

    monkeypatch.setattr(bg_task_manager, "cancel_work", bg_cleanup)
    monkeypatch.setattr(research_manager, "cancel_work", research_cleanup)
    manager = Captured(jobs)
    cancelling = asyncio.create_task(jobs.cancel(job["id"], manager))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (await cancelling)["settlement_phase"] == "pending"
        release.set()
        with pytest.raises(RuntimeError, match="Original BG cleanup failed"):
            await jobs.cancel(job["id"], manager)
        assert research == {first["work_id"], second["work_id"]}
        assert stopped == {("first-session", "first-request"), ("second-session", "second-request")}
        assert len(backend) == 2
        assert jobs.get(job["id"])["settlement_phase"] != "confirmed"
    finally:
        release.set()
        await asyncio.gather(cancelling, return_exceptions=True)

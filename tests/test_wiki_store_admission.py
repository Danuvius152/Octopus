"""A candidate is private until the same owner finishes its recovery pipeline."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from server.wiki import job_execution
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import WikiRegistry
from tests.test_wiki_registry_lifecycle import registered as registered


def test_simultaneous_borrow_cannot_escape_unfinished_recovery(registered, monkeypatch):
    original, _, _ = registered
    original.close()
    registry = WikiRegistry()
    entered, release = threading.Event(), threading.Event()
    recover = WikiHistory.recover
    candidates = []

    def blocked(history):
        if not entered.is_set():
            candidates.append(history.store)
            entered.set()
            assert release.wait(5)
        return recover(history)

    monkeypatch.setattr(WikiHistory, "recover", blocked)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(registry.store, None)
        try:
            assert entered.wait(2), "Registry published its Store without running recovery"
            second = pool.submit(registry.store, None)
            assert not first.done() and not second.done()
            assert not registry._stores
        finally:
            release.set()
        one, two = first.result(timeout=5), second.result(timeout=5)
    assert one is two is candidates[0]
    try:
        assert one.search("Preserved")["items"]
    finally:
        registry.close()


def test_failed_recovery_stays_closed_until_explicit_recovery_or_new_generation(registered, monkeypatch):
    import pytest

    from server.wiki.store import WikiError

    original, _, _ = registered
    original.close()
    registry = WikiRegistry()
    attempts = []

    def failed(history):
        attempts.append(history.store)
        raise WikiError("Retained original journal requires owner review", 503)

    monkeypatch.setattr(WikiHistory, "recover", failed)
    try:
        for _ in range(2):
            with pytest.raises(WikiError, match="owner review"):
                registry.store(None)
        assert len(attempts) == 1
        assert not registry._stores
    finally:
        registry.close()


async def test_registration_change_waits_for_old_vault_recovery_before_new_work(registered, monkeypatch):
    original, root, _ = registered
    original.close()
    registry = WikiRegistry()
    entered, second, release = threading.Event(), threading.Event(), threading.Event()
    recover = WikiHistory.recover
    recover_jobs = WikiJobs.recover_jobs
    recovered_bindings = []

    def blocked(history):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        else:
            second.set()
        return recover(history)

    def classified(jobs, **kwargs):
        recovered_bindings.append(jobs.registration)
        return recover_jobs(jobs, **kwargs)

    monkeypatch.setattr(WikiHistory, "recover", blocked)
    monkeypatch.setattr(WikiJobs, "recover_jobs", classified)
    starting = asyncio.create_task(registry.start(paused=True, previous_writers_retired=True))
    changing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        changing = asyncio.create_task(asyncio.to_thread(registry.configure, None, root, "replacement-curator", "app-a"))
        async with asyncio.timeout(3):
            while (await asyncio.to_thread(registry.config, None))["curator_agent_id"] != "replacement-curator":
                await asyncio.sleep(0.005)
        assert not await asyncio.to_thread(second.wait, 0.2), "A registration change started overlapping vault recovery"
        assert not changing.done()
        release.set()
        await starting
        config = await changing
        assert recovered_bindings == [config], "Only the valid captured binding may classify jobs"
        jobs = WikiJobs(None, store=registry.store(None), registration=config)
        job = jobs.maintain("fresh-after-recovery", "", "Review", ["note.md"], None, [], "human")
        assert jobs.next_queued() == job["id"]
        job = jobs.get(job["id"])
        job_execution.prepare(jobs, job, job["items"], "new-session", "new-request")
        assert not jobs.get(job["id"])["items"][0]["worker_request"].get("retired")
    finally:
        release.set()
        await asyncio.gather(starting, *([changing] if changing else []), return_exceptions=True)
        await registry.stop()

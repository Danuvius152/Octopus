"""Schedule fire and Wiki effects consume the original missing-decision fact."""

from __future__ import annotations

import asyncio

import pytest

from server.harness import HarnessEvent
from tests import wiki_api
from tests.fake_run import FakeRunBase
from tests.test_scheduler import save_schedule
from tests.test_scheduler import setup as setup
from tests.test_wiki_job_events import enqueue
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs
from tests.wiki_job_runtime import process


def asking_runs(manager, monkeypatch, failure=None):
    starts = []
    class Run(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id
            self.ready = asyncio.Event()
        async def start(self, prompt, *args, **kwargs):
            starts.append(self.session_id)
            self.ready.set()
        async def stream(self):
            await self.ready.wait()
            qid = await manager.create_pending_question(self.session_id, [{"question": "Which destination is authorized?"}])
            assert not manager.has_pending_question(self.session_id, qid)
            yield HarnessEvent(type="text", content="Independent analysis is complete. Destination decision is still needed; dependent publication stopped.")
            yield HarnessEvent(type="result", content=failure, is_error=failure is not None)
        async def stop(self):
            pass
        async def interrupt(self):
            pass
    monkeypatch.setattr(manager, "_make_run", lambda session, *args, **kwargs: Run(session.id))
    return starts


async def test_one_shot_missing_decision_consumes_original_fire_without_automatic_replay(setup, monkeypatch):
    manager, database, runner = setup
    agent = await database.get_system_agent()
    starts = asking_runs(manager, monkeypatch)
    await save_schedule(database, schedule_id="missing-decision-fire", agent_id=agent["id"], name="Missing destination",
        prompt="Analyze independently; publish only to an authorized destination", run_at="2026-11-01T01:00:00+00:00", created_at="2026-11-01")
    await runner._fire("missing-decision-fire")
    assert len(starts) == 1
    rows = await database.load_messages(starts[0])
    closed, = [row for row in rows if row["type"] == "question_closed"]
    assert closed["tool_input"]["reason"] == "needs_input"
    assert "destination" in closed["content"].lower()
    assert await database.get_schedule_definition("missing-decision-fire") is None
    await runner._fire("missing-decision-fire")
    assert len(starts) == 1


@pytest.mark.parametrize("failure", [None, "Original execution failed after requesting a decision"])
async def test_wiki_missing_decision_keeps_effect_unconfirmed_and_surfaces_attention(jobs, monkeypatch, failure):
    manager = wiki_api.session_manager
    starts = asking_runs(manager, monkeypatch, failure)
    job = enqueue(jobs)
    original = jobs.store.path("Library/note.md").read_bytes()
    finished = await process(jobs, job, manager)
    assert len(starts) == 1 and finished["status"] == "completed_with_attention"
    item, = finished["items"]
    assert item["status"] == "attention" and "destination" in item["error"].lower()
    if failure is not None:
        assert failure in item["error"]
    assert jobs.publication(job["id"], item["doc"]) is None
    assert jobs.store.path("Library/note.md").read_bytes() == original

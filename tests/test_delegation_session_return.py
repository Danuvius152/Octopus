"""Delegation replies keep their original parent across archive boundaries."""

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationManager
from server.harness import HarnessEvent
from tests.fake_run import FakeRunBase
from tests.test_research_session_return import returning_research as returning_research


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("question", [False, True])
async def test_delegation_reply_restores_parent_and_keeps_child_for_followup(returning_research, monkeypatch, paused, question):
    db, manager, _, _, _, _ = returning_research
    entered, release = asyncio.Event(), asyncio.Event()
    processed = asyncio.Queue()

    def make_run(configured, *args, **kwargs):
        class Run(FakeRunBase):
            def __init__(self):
                self.ready = asyncio.Event()

            async def start(self, prompt, *args, **kwargs):
                self.prompt = prompt
                self.ready.set()

            async def stream(self):
                await self.ready.wait()
                if configured.parent_session_id:
                    entered.set()
                    await release.wait()
                    yield HarnessEvent(type="text", content="Original child reply")
                else:
                    processed.put_nowait(self.prompt)
                yield HarnessEvent(type="result", is_error=False)

            async def stop(self):
                pass

            async def interrupt(self):
                pass

        return Run()

    monkeypatch.setattr(manager, "_make_run", make_run)
    agent = await db.get_system_agent()
    await AgentManager(db).create_agent(name="Reviewer", user_id=agent["user_id"])
    parent = await manager.create_session(agent["id"], "Original parent")
    delegations = DelegationManager()
    delegations.bind(manager, db)
    try:
        record = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Reviewer", request="Review")
        await asyncio.wait_for(entered.wait(), 3)
        if paused:
            await manager.interrupt(parent.id)
        await manager.archive_session(parent.id, replace=False)
        if question:
            question_id = await manager.create_pending_question(record.delegation_id, [{"question": "Choose", "options": [{"label": "A"}, {"label": "B"}]}])
            async with asyncio.timeout(3):
                while manager.get_session(parent.id) is None or not manager.dispatch_snapshot(parent.id)["items"] and paused:
                    await asyncio.sleep(0.01)
            if paused:
                await manager.resume_dispatch(parent.id)
            question_prompt = await asyncio.wait_for(processed.get(), 3)
            assert "[agent-question:Reviewer" in question_prompt and question_id in question_prompt
            if paused:
                await manager.interrupt(parent.id)
        release.set()
        async with asyncio.timeout(3):
            while record._terminal_disposition is None:
                await asyncio.sleep(0.01)
        assert record.state == "completed" and record._terminal_disposition == "accepted"
        assert manager.dispatch_snapshot(parent.id)["paused"] is paused
        if paused:
            assert processed.empty()
            await manager.resume_dispatch(parent.id)
        prompt = await asyncio.wait_for(processed.get(), 3)
        assert f"delegation={record.delegation_id}" in prompt and "Original child reply" in prompt
        assert manager.get_session(record.delegation_id) is not None
        followup = await delegations.follow_up_delegation(delegation_id=record.delegation_id, request="Review again", parent_session_id=parent.id)
        assert followup.delegation_id == record.delegation_id
    finally:
        release.set()
        delegations.shutdown()

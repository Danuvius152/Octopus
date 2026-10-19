"""Human steering crosses the same committed-input seam as queued requests."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.database import Database
from server.harness import HarnessEvent, get_harness
from server.knowledge import capture, derive
from server.mcp_identity import McpScope, register_run, revoke_run
from server.session_manager import SessionManager
from server.wiki.limits import MaintenanceLimit
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests.fake_run import FakeRunBase


@pytest.fixture
async def steering(tmp_path, monkeypatch):
    path = str(tmp_path / "steering.db")
    db = Database(path)
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(None, vault, agent["id"])
    session = await manager.create_session(agent["id"], "Steering", str(tmp_path), backend="codex")
    reader = Database(path)
    await reader.initialize()
    started = asyncio.Queue()
    frames = asyncio.Queue()
    first_done = asyncio.Event()
    control = SimpleNamespace(frame_error=False, derived=None, active_knowledge=None)

    def create(config):
        class Run(FakeRunBase):
            reusable = True
            run_lease_id = uuid.uuid4().hex

            async def start(self, prompt, *args, **kwargs):
                self.prompt = prompt
                register_run(self.run_lease_id, session.id, session.user_id, config.work_id)

            async def send_turn(self, prompt):
                self.prompt = prompt

            async def send_user_frame(self, prompt):
                # The independent connection cannot see the writer's pending
                # batch. Observe only the public Database transcript seam.
                committed = await reader.load_messages(session.id)
                scope = McpScope(
                    session.id, user_id=session.user_id, wiki_task_id=config.wiki_task_id,
                    run_lease_id=self.run_lease_id, work_id=config.work_id,
                )
                control.derived = derive(capture(session, scope, prompt="Continue verification"), "Verify a claim")
                control.active_knowledge = tasks.require(scope).knowledge
                await frames.put((prompt, committed))
                if control.frame_error:
                    raise BrokenPipeError("frame refused before delivery")

            def set_idle_handler(self, handler):
                pass

            async def stream(self):
                await started.put(self.prompt)
                if self.prompt == "Initial comparison":
                    await first_done.wait()
                yield HarnessEvent(type="result", is_error=False)

            async def stop(self):
                revoke_run(self.run_lease_id)

            async def interrupt(self):
                revoke_run(self.run_lease_id)

        return Run()

    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    try:
        yield SimpleNamespace(
            manager=manager, session=session, db=db, reader=reader,
            started=started, frames=frames, first_done=first_done, control=control,
        )
    finally:
        first_done.set()
        await manager.drain_turns()
        await manager.stop_all_held_processes()
        await reader.close()
        await db.close()


async def test_steered_input_and_knowledge_are_independently_committed_before_frame(steering):
    env = steering
    original = await env.manager.start_message(env.session.id, "Initial comparison")
    assert await asyncio.wait_for(env.started.get(), 2) == "Initial comparison"
    correction = "Also consult local notes and relevant web evidence. Preserve these source limits."
    receipt = await env.manager.start_message(env.session.id, correction, steerable=True)
    assert receipt.disposition == "steered"
    frame, committed = await asyncio.wait_for(env.frames.get(), 2)
    assert frame == correction
    matches = [row for row in committed if row["content"] == correction]
    assert len(matches) == 1
    assert matches[0]["tool_input"]["request_context"]["request_id"] == receipt.request_id
    assert "knowledge" in matches[0]["tool_input"]["request_context"]
    inherited = env.control.derived.return_binding.context
    assert "Initial comparison" in inherited.trusted_request
    assert correction in inherited.trusted_request
    assert env.control.active_knowledge == inherited
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"


@pytest.mark.parametrize("committed_before_error", [False, True])
async def test_unconfirmed_inline_input_never_reaches_the_backend_and_retries_once(
    steering, monkeypatch, committed_before_error,
):
    env = steering
    original = await env.manager.start_message(env.session.id, "Initial comparison")
    await asyncio.wait_for(env.started.get(), 2)
    failed = asyncio.Event()
    commit = env.db.conn.commit

    async def failed_commit():
        if committed_before_error:
            await commit()
        failed.set()
        raise OSError("steered input commit is unconfirmed")

    correction = "Use the added evidence before answering."
    with monkeypatch.context() as patch:
        patch.setattr(env.db.conn, "commit", failed_commit)
        receipt = await env.manager.start_message(env.session.id, correction, steerable=True)
        await asyncio.wait_for(failed.wait(), 2)
        await asyncio.sleep(0)
        assert env.frames.empty()
        committed = await env.reader.load_messages(env.session.id)
        assert sum(row["content"] == correction for row in committed) == int(committed_before_error)
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"
    assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
    rows = [row for row in await env.reader.load_messages(env.session.id) if row["content"] == correction]
    assert len(rows) == 1
    assert rows[0]["tool_input"]["request_context"]["request_id"] == receipt.request_id


async def test_failed_frame_reuses_committed_input_and_its_original_identity(steering):
    env = steering
    original = await env.manager.start_message(env.session.id, "Initial comparison")
    await asyncio.wait_for(env.started.get(), 2)
    correction = "Keep the source limits when this is retried."
    env.control.frame_error = True
    receipt = await env.manager.start_message(env.session.id, correction, steerable=True)
    _, committed = await asyncio.wait_for(env.frames.get(), 2)
    first = next(row for row in committed if row["content"] == correction)
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"
    completed = await asyncio.wait_for(receipt.wait(), 3)
    assert completed.request_id == receipt.request_id
    assert completed.outcome.status == "success"
    rows = [row for row in await env.reader.load_messages(env.session.id) if row["content"] == correction]
    assert rows == [first]


async def test_human_steering_preserves_the_existing_hard_maintenance_ceiling(steering):
    env = steering
    context = replace(
        capture(env.session, None, prompt="Initial comparison").context,
        maintenance=MaintenanceLimit(False, ("/wiki maintain Library/only.md",)),
    )
    original = await env.manager.start_message(
        env.session.id, "Initial comparison", request_id=context.origin.request_id,
        knowledge_context=context,
    )
    await asyncio.wait_for(env.started.get(), 2)
    receipt = await env.manager.start_message(
        env.session.id, "Use my updated evidence requirements.", steerable=True,
    )
    await asyncio.wait_for(env.frames.get(), 2)
    assert env.control.derived.execution.maintenance.may_submit is False
    assert env.control.derived.execution.maintenance.required_selections == ("/wiki maintain Library/only.md",)
    assert env.control.active_knowledge.maintenance.may_submit is False
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"
    assert (await receipt.wait()).outcome.status == "success"

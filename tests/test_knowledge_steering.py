"""Human steering crosses the same committed-input seam as queued requests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.database import Database
from server.harness import HarnessEvent, get_harness
from server.knowledge import capture, derive, encode
from server.mcp_identity import McpScope, register_run, revoke_run
from server.session_manager import SessionManager
from server.wiki.limits import MaintenanceLimit
from server.wiki.registry import registry
from server.wiki.tasks import RUN_WIKI_SCOPE, tasks
from tests.accounts import create_account
from tests.fake_run import FakeRunBase


@pytest.fixture
async def steering(tmp_path, monkeypatch):
    path = str(tmp_path / "steering.db")
    db = Database(path)
    await db.initialize()
    user, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(user["id"], vault, agent["id"])
    session = await manager.create_session(agent["id"], "Steering", str(tmp_path), backend="codex")
    reader = Database(path)
    await reader.initialize()
    started = asyncio.Queue()
    frames = asyncio.Queue()
    first_done = asyncio.Event()
    control = SimpleNamespace(frame_error=False, derived=None, active_knowledge=None, frame_marker=None, scope=None)

    def create(config):
        class Run(FakeRunBase):
            reusable = True

            def __init__(self):
                self.run_lease_id = uuid.uuid4().hex
                self.session_id, self.user_id = config.session_id, config.user_id
                self.work_id = config.work_id
                self.ready = asyncio.Event()

            def bind_wiki_task(self, task_id):
                tasks.bind_run(self.run_lease_id, task_id, self.session_id, self.user_id)

            async def start(self, prompt, *args, **kwargs):
                self.prompt = prompt
                register_run(self.run_lease_id, self.session_id, self.user_id, self.work_id)
                self.ready.set()

            def prepare_turn_stream(self):
                self.ready.clear()

            async def send_turn(self, prompt):
                self.prompt = prompt
                self.ready.set()

            async def send_user_frame(self, prompt):
                if control.frame_marker is not None:
                    control.frame_marker.touch()
                # The independent connection cannot see the writer's pending
                # batch. Observe only the public Database transcript seam.
                committed = await reader.load_messages(session.id)
                scope = McpScope(
                    self.session_id, user_id=self.user_id, wiki_task_id=RUN_WIKI_SCOPE,
                    run_lease_id=self.run_lease_id, work_id=self.work_id,
                )
                control.scope = scope
                control.derived = derive(capture(session, scope, prompt="Continue verification"), "Verify a claim")
                control.active_knowledge = tasks.require(scope).knowledge
                await frames.put((prompt, committed))
                if control.frame_error:
                    raise BrokenPipeError("frame refused before delivery")

            def set_idle_handler(self, handler):
                pass

            async def stream(self):
                await self.ready.wait()
                await started.put(self.prompt)
                if self.prompt == "Initial comparison":
                    await first_done.wait()
                yield HarnessEvent(type="result", is_error=False)

            async def stop(self):
                revoke_run(self.run_lease_id)
                tasks.unbind_run(self.run_lease_id)

            async def interrupt(self):
                revoke_run(self.run_lease_id)
                tasks.unbind_run(self.run_lease_id)

        return Run()

    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    try:
        yield SimpleNamespace(
            manager=manager, session=session, db=db, reader=reader, database_path=path,
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
    assert matches[0]["tool_input"]["request_context"]["knowledge"] == json.loads(encode(inherited))
    assert inherited.origin.request_id == original.request_id
    assert env.control.scope.session_id == env.session.id
    assert env.control.scope.user_id == env.session.user_id
    assert env.control.scope.wiki_task_id == RUN_WIKI_SCOPE
    assert tasks.require(env.control.scope).knowledge == inherited
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"


@pytest.mark.parametrize("committed_before_error", [False, True])
@pytest.mark.parametrize("stage", ["admission", "recording"])
async def test_unconfirmed_inline_input_stops_the_process_before_a_frame(tmp_path, committed_before_error, stage):
    proof = tmp_path / "commit-proof.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(Path(__file__).resolve()),
         "-o", "python_functions=_unconfirmed_inline_commit_child"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "OCTOPUS_TEST_INLINE_COMMIT_PROOF": str(proof),
             "OCTOPUS_TEST_INLINE_COMMIT_MODE": str(int(committed_before_error)),
             "OCTOPUS_TEST_INLINE_COMMIT_STAGE": stage, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 70, result.stdout + result.stderr
    evidence = json.loads(proof.read_text())
    assert evidence["stage"] == stage
    assert not proof.with_suffix(".frame").exists()
    reader = Database(evidence["database"])
    await reader.initialize()
    try:
        rows = await reader.load_messages(evidence["session_id"])
        assert any(row["content"] == "Initial comparison" for row in rows)
        corrections = [row for row in rows if row["content"] == "Use the added evidence before answering."]
        saved = await reader.get_submission_for_request(evidence["session_id"], "original-inline-input")
        assert (saved is not None) is (stage == "recording" or committed_before_error)
        if saved:
            assert saved["payload"]["content"] == "Use the added evidence before answering."
            assert saved["receipt"]["accepted"]
        assert len(corrections) == int(stage == "recording" and committed_before_error)
        if corrections:
            assert corrections[0]["tool_input"]["request_context"]["request_id"] == "original-inline-input"
    finally:
        await reader.close()


async def _unconfirmed_inline_commit_child(steering, monkeypatch):
    """Selected only by the parent subprocess to observe real fail-stop."""
    env = steering
    await env.manager.start_message(env.session.id, "Initial comparison")
    await asyncio.wait_for(env.started.get(), 2)
    proof = Path(os.environ["OCTOPUS_TEST_INLINE_COMMIT_PROOF"])
    committed_before_error = os.environ["OCTOPUS_TEST_INLINE_COMMIT_MODE"] == "1"
    stage = os.environ["OCTOPUS_TEST_INLINE_COMMIT_STAGE"]
    env.control.frame_marker = proof.with_suffix(".frame")
    commit = env.db.conn.commit
    before = env.db.conn.total_changes
    armed = stage == "admission"
    record = env.db.record_request

    async def recording(*, request_id, **message):
        nonlocal armed
        armed = request_id == "original-inline-input"
        return await record(request_id=request_id, **message)

    async def failed_commit():
        if not armed or env.db.conn.total_changes == before:
            return await commit()
        if committed_before_error:
            await commit()
        proof.write_text(json.dumps({"database": env.database_path, "session_id": env.session.id, "stage": stage}))
        raise OSError("steered input commit is unconfirmed")

    correction = "Use the added evidence before answering."
    monkeypatch.setattr(env.db.conn, "commit", failed_commit)
    if stage == "recording":
        monkeypatch.setattr(env.db, "record_request", recording)
    receipt = await env.manager.start_message(env.session.id, correction, steerable=True, request_id="original-inline-input")
    await asyncio.wait_for(receipt.wait(), 5)
    raise AssertionError("An unconfirmed input commit returned to the caller")


async def test_failed_frame_reuses_committed_input_and_its_original_identity(steering):
    env = steering
    original = await env.manager.start_message(env.session.id, "Initial comparison")
    await asyncio.wait_for(env.started.get(), 2)
    correction = "Keep the source limits when this is retried."
    env.control.frame_error = True
    receipt = await env.manager.start_message(env.session.id, correction, steerable=True)
    assert receipt.disposition == "steered"
    _, committed = await asyncio.wait_for(env.frames.get(), 2)
    first = next(row for row in committed if row["content"] == correction)
    assert first["tool_input"]["request_context"]["request_id"] == receipt.request_id
    completed = await asyncio.wait_for(receipt.wait(), 3)
    assert completed.input_request_id == receipt.request_id
    assert completed.request_id == original.request_id
    assert completed.reason == "steer_delivery_unknown" and completed.outcome is None
    assert completed.state == "unavailable" and completed.notification_state == "unavailable"
    assert completed.recording_state == "none"
    assert env.manager.dispatch_snapshot(env.session.id)["items"] == []
    assert env.started.empty() and env.frames.empty()
    env.first_done.set()
    assert (await original.wait()).outcome.status == "success"
    assert env.started.empty() and env.frames.empty()
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

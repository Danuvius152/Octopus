"""A one-shot is consumed by durable admission, not by its callback returning."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database
from server.scheduler import ScheduleRunner
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status
from tests.test_scheduler import capture_model_runs, save_schedule
from tests.test_scheduler import setup as setup
from tests.test_submission_recovery import _submit_paused
from tests.test_submission_recovery import offline_harness as offline_harness


async def test_one_shot_is_admitted_behind_paused_human_input(setup, tmp_path):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"], "One-shot caller", str(tmp_path), backend="codex")
    human = await _submit_paused(manager, origin.id, "Human backlog", "human")
    await save_schedule(
        db, schedule_id="once", agent_id=agent["id"], name="Reminder", prompt="Retain this fire",
        created_at="2026-10-23T00:00:00Z", run_at="2026-10-23T01:00:00Z", origin_session_id=origin.id,
    )
    await runner._fire("once")
    await runner._fire("once")
    pending = manager.dispatch_snapshot(origin.id)
    assert pending["paused"]
    assert [item["source"] for item in pending["items"]] == ["human", "schedule"]
    assert pending["items"][0]["request_id"] == human["request_id"]
    scheduled = await db.get_submission_for_request(origin.id, pending["items"][1]["request_id"])
    assert scheduled["receipt"]["accepted"]
    assert scheduled["payload"]["source"] == "schedule"
    assert await db.get_schedule_definition("once") is None
    assert (await submission_status(manager, origin.id, "human"))["execution"]["state"] == "pending"


async def test_overdue_unadmitted_one_shot_is_registered_for_catchup(setup):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="overdue", agent_id=agent["id"], name="Missed reminder", prompt="Do not lose me",
        created_at="2020-01-01T00:00:00Z", run_at="2020-01-01T01:00:00Z",
    )
    await runner.initialize(paused=True)
    assert await db.get_schedule_definition("overdue") is not None
    assert runner._scheduler.get_job("overdue") is not None


async def test_refused_one_shot_keeps_its_definition_and_retry_without_an_orphan_session(setup):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="refused", agent_id=agent["id"], name="Retained reminder", prompt="Try original fire later",
        created_at="2020-01-01T00:00:00Z", run_at="2020-01-01T01:00:00Z",
    )
    await runner.initialize(paused=True)
    manager.close_admission()
    await runner._fire("refused")
    assert await db.get_schedule_definition("refused") is not None
    assert runner._scheduler.get_job("refused") is not None
    assert not [row for row in await db.load_sessions(include_archived=True) if row["origin"] == "schedule"]


@pytest.mark.parametrize("committed", [False, True])
async def test_one_shot_process_loss_keeps_target_input_and_consumption_atomic(tmp_path, monkeypatch, committed):
    proof = tmp_path / "one-shot.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(Path(__file__).resolve()),
         "-o", "python_functions=_one_shot_process_loss_child"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "ONE_SHOT_PROOF": str(proof), "ONE_SHOT_COMMITTED": str(int(committed))},
    )
    assert result.returncode == 70, result.stdout + result.stderr
    saved = json.loads(proof.read_text())
    db = Database(str(proof.with_suffix(".db")))
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    runner = ScheduleRunner(manager, db)
    try:
        targets = [row for row in await db.load_sessions(include_archived=True) if row["origin"] == "schedule"]
        assert len(targets) == int(committed)
        assert (await db.get_schedule_definition("crash-once") is None) is committed
        input_row = await db.get_submission_for_request(saved["session_id"], saved["request_id"])
        if committed:
            assert input_row["receipt"]["accepted"]
            assert input_row["execution_state"] == "withdrawn"
        else:
            assert input_row is None
        runs = capture_model_runs(manager, monkeypatch)
        await runner._fire("crash-once")
        assert len(runs) == int(not committed)
        assert await db.get_schedule_definition("crash-once") is None
        assert len([row for row in await db.load_sessions(include_archived=True) if row["origin"] == "schedule"]) == 1
    finally:
        await runner.shutdown()
        await db.close()


async def _one_shot_process_loss_child(monkeypatch):
    proof = Path(os.environ["ONE_SHOT_PROOF"])
    db = Database(str(proof.with_suffix(".db")))
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="crash-once", agent_id=agent["id"], name="Crash once", prompt="Run exactly once",
        run_at="2020-01-01T00:00:00Z", created_at="2020-01-01T00:00:00Z",
    )
    execute, commit = db.conn.execute, db.conn.commit
    selected = None

    async def observe(sql, parameters=()):
        nonlocal selected
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO client_submissions"):
            selected = {"session_id": parameters[0], "request_id": parameters[3]}
        return cursor

    async def lose_commit():
        if selected is not None:
            if os.environ["ONE_SHOT_COMMITTED"] == "1":
                await commit()
            proof.write_text(json.dumps(selected))
            raise OSError("One-shot admission commit acknowledgment lost")
        await commit()

    monkeypatch.setattr(db.conn, "execute", observe)
    monkeypatch.setattr(db.conn, "commit", lose_commit)
    await ScheduleRunner(manager, db)._fire("crash-once")
    raise AssertionError("An uncertain admission commit returned")

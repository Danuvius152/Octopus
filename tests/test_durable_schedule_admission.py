"""A one-shot is consumed by durable admission, not by its callback returning."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.schedulers import base as apscheduler_base
from apscheduler.triggers.date import DateTrigger

from server import deps, scheduler
from server.credential_bindings import CredentialBindingError
from server.database import Database
from server.db import client_submissions
from server.db.transaction import Transaction
from server.knowledge import decode_context
from server.scheduler import ScheduleRunner
from server.service_health import ServiceHealth
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status
from server.sessions.lifecycle import SessionArchiveFailed
from server.users import UserManager
from tests.accounts import create_account
from tests.fake_run import capture_model_runs
from tests.schedule_support import dispatch_schedule, schedule_job
from tests.test_scheduler import save_schedule
from tests.test_scheduler import setup as setup
from tests.test_submission_recovery import _submit_paused
from tests.test_submission_recovery import offline_harness as offline_harness

_DUE = "2020-01-01T01:00:00Z"


async def save_once(db, agent_id, schedule_id, **fields):
    await save_schedule(db, schedule_id=schedule_id, agent_id=agent_id, name="Original reminder", prompt="Once only",
                        created_at="2019-01-01T00:00:00Z", **{"run_at": _DUE, **fields})


@pytest.mark.parametrize("effect", ["rebound", "restored", "restored_reverse", "rollback", "configuration", "plain"])
async def test_one_shot_waits_for_archive_rebinding_and_can_admit_before_notification(setup, monkeypatch, effect):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    old = await manager.create_session(agent["id"], name="Original target")
    await save_once(db, agent["id"], "archive-rebind", origin_session_id=old.id)
    await save_schedule(db, schedule_id="periodic-rebind", agent_id=agent["id"], name="Periodic reminder",
                        prompt="Keep the original cadence", created_at="2019-01-01T00:00:00Z",
                        interval_seconds=3600, origin_session_id=old.id)
    if effect == "rollback":
        async with db.transaction(write=True) as tx:
            await tx.execute("""CREATE TEMP TRIGGER refuse_schedule_rebind
                BEFORE UPDATE OF origin_session_id ON schedules
                BEGIN SELECT RAISE(ABORT, 'Rebinding refused'); END""")
    await runner.initialize(paused=True)
    job = schedule_job(runner, "archive-rebind")
    periodic_job = schedule_job(runner, "periodic-rebind")
    periodic_next = periodic_job.next_run_time
    runs = capture_model_runs(manager, monkeypatch)
    rebinding, release_rebind = asyncio.Event(), asyncio.Event()
    notifying, release_notice = asyncio.Event(), asyncio.Event()
    archiving_restored, release_restored = asyncio.Event(), asyncio.Event()
    fire_captured = asyncio.Event()
    repoint = db.repoint_schedules_origin
    update_session = db.update_session_field
    start = manager.start_one_shot
    delivery_id = f"schedule:archive-rebind:{_DUE}"
    restored_overlap = effect in {"restored", "restored_reverse"}
    rebound = effect == "rebound" or restored_overlap

    async def observe_fire(*args, **kwargs):
        fire_captured.set()
        return await start(*args, **kwargs)

    async def hold_rebinding(old_id, new_id):
        rebinding.set()
        await release_rebind.wait()
        return await repoint(old_id, new_id)

    async def hold_archive_write(session_id, **fields):
        await update_session(session_id, **fields)
        if session_id == old.id and fields.get("archived") is True:
            rebinding.set()
            await release_rebind.wait()

    async def hold_restored_archive(session_id, **fields):
        if rebinding.is_set() and session_id == old.id and fields.get("archived") is True:
            archiving_restored.set()
            await release_restored.wait()
        await update_session(session_id, **fields)

    async def refuse_successor_configuration(session_id, **fields):
        if session_id != old.id and "model" in fields:
            rebinding.set()
            await release_rebind.wait()
            raise ValueError("Inherited configuration refused")
        await update_session(session_id, **fields)

    async def hold_notification(event):
        if (event["type"] == "session_archived" and event["old_session_id"] == old.id
                and (effect == "plain" or event["new_session_id"] is not None)):
            notifying.set()
            await release_notice.wait()

    if effect == "plain":
        monkeypatch.setattr(db, "update_session_field", hold_archive_write)
    elif effect == "configuration":
        monkeypatch.setattr(db, "update_session_field", refuse_successor_configuration)
    else:
        monkeypatch.setattr(db, "repoint_schedules_origin", hold_rebinding)
        if effect == "restored_reverse":
            monkeypatch.setattr(db, "update_session_field", hold_restored_archive)
    monkeypatch.setattr(manager, "start_one_shot", observe_fire)
    manager.on_broadcast("hold-original-archive", hold_notification)
    archive = asyncio.create_task(manager.archive_session(old.id, replace=effect != "plain"))
    firing = None
    second_archive = None
    try:
        await asyncio.wait_for(rebinding.wait(), 3)
        assert (await db.get_session_binding(old.id))["archived"]
        successor_id = None
        if effect != "plain":
            assert manager.get_session(old.id) is None
            successor, = await db.load_sessions()
            successor_id = successor["id"]
        if restored_overlap:
            restored = await asyncio.wait_for(manager.unarchive_session(old.id), 3)
            assert restored is manager.get_session(old.id) and restored is not old
        firing = asyncio.create_task(dispatch_schedule(runner, "archive-rebind"))
        await asyncio.wait_for(fire_captured.wait(), 3)
        if effect == "restored":
            await asyncio.wait_for(manager.archive_session(old.id, replace=False), 3)
        elif effect == "restored_reverse":
            second_archive = asyncio.create_task(manager.archive_session(old.id, replace=False))
            await asyncio.wait_for(archiving_restored.wait(), 3)
        await asyncio.wait_for(runner.reconcile("unrelated-during-archive"), 3)
        assert not (await asyncio.wait({firing}, timeout=.1))[0], "the original fire completed before rebinding"
        assert await manager.source_receipt("schedule", delivery_id) is None
        current = await db.get_schedule_definition("archive-rebind")
        assert current["origin_session_id"] == old.id and current["run_at"] == _DUE
        assert schedule_job(runner, "archive-rebind") is None

        release_rebind.set()
        await asyncio.wait_for(notifying.wait(), 3)
        if second_archive is not None:
            assert not (await asyncio.wait({firing}, timeout=.1))[0], "the restored session was still archiving"
            assert (await db.get_schedule_definition("archive-rebind"))["origin_session_id"] == successor_id
            release_restored.set()
            await asyncio.wait_for(second_archive, 3)
        await asyncio.wait_for(firing, 3)
        assert not archive.done()
        target, receipt = await manager.source_receipt("schedule", delivery_id)
        assert receipt.accepted
        if rebound:
            assert target == successor_id and manager.get_session(target) is not None
        else:
            assert target not in {old.id, successor_id}
            assert manager.get_session(target) is None
            assert (await db.get_session_binding(target))["archived"]
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        assert [run.session_id for run in runs] == [target]
        assert schedule_job(runner, "periodic-rebind") is periodic_job
        assert periodic_job.next_run_time == periodic_next
        periodic = await db.get_schedule_definition("periodic-rebind")
        assert periodic["origin_session_id"] == (successor_id if rebound else old.id)
        assert await db.get_schedule_definition("archive-rebind") is None
        await job.func(*job.args)
        assert len(runs) == 1 and schedule_job(runner, "archive-rebind") is None
        release_notice.set()
        if effect in {"rollback", "configuration"}:
            with pytest.raises(SessionArchiveFailed) as failure:
                await asyncio.wait_for(archive, 3)
            assert failure.value.result.failure_stage == ("schedule_rebinding" if effect == "rollback" else "successor_configuration")
        else:
            await asyncio.wait_for(archive, 3)
    finally:
        release_rebind.set()
        release_notice.set()
        release_restored.set()
        await asyncio.gather(*(task for task in (archive, firing, second_archive) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("boundary", ["pre_commit", "mailbox", "unretired_writer"])
async def test_archive_refuses_an_in_flight_fire_without_waiting_on_its_owner(setup, monkeypatch, boundary):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    old = await manager.create_session(agent["id"])
    await save_once(db, agent["id"], "archive-during-submit", origin_session_id=old.id)
    await runner.initialize(paused=True)
    previous_request = "previous-request"
    if boundary == "unretired_writer":
        created, _ = await db.admit_client_submission(
            old.id, "previous-input", "previous-digest", previous_request,
            payload={"content": "Work with an uncertain result", "attachment_ids": [], "wiki_mode": None},
            receipt={"accepted": True, "disposition": "accepted"},
        )
        assert created
        generation = manager.dispatch_snapshot(old.id)["generation"]
        assert await db.claim_client_submission(old.id, previous_request, previous_request, generation)
        await db.complete_client_submission(old.id, previous_request, owner=previous_request, state="unknown")
    runs = capture_model_runs(manager, monkeypatch)
    archive_ready, begin_archive = asyncio.Event(), asyncio.Event()
    admitting, release_admission = asyncio.Event(), asyncio.Event()
    closing_owner, persisting_archive, allow_archive = asyncio.Event(), asyncio.Event(), asyncio.Event()
    archive_original = manager._archive_original
    close_execution = manager._close_original_execution
    execute = Transaction.execute
    update_session = db.update_session_field
    read_agent = db.get_live_agent_configuration
    read_writers = db.uncertain_submission_writers
    delivery_id = f"schedule:archive-during-submit:{_DUE}"

    async def hold_archive_entry(*args, **kwargs):
        archive_ready.set()
        await begin_archive.wait()
        return await archive_original(*args, **kwargs)

    async def observe_close(*args, **kwargs):
        closing_owner.set()
        return await close_execution(*args, **kwargs)

    async def hold_consumption(tx, sql, parameters=()):
        cursor = await execute(tx, sql, parameters)
        if sql == "DELETE FROM schedules WHERE id=?" and parameters == ("archive-during-submit",) and not admitting.is_set():
            admitting.set()
            await release_admission.wait()
        return cursor

    async def hold_configuration(*args, **kwargs):
        if not admitting.is_set():
            admitting.set()
            await release_admission.wait()
        return await read_agent(*args, **kwargs)

    async def hold_writer_check(session_id):
        writers = await read_writers(session_id)
        if session_id == old.id:
            writer, = writers
            assert writer["request_id"] == previous_request and writer["process_group"] is None
            admitting.set()
            await release_admission.wait()
        return writers

    async def hold_archive_persistence(session_id, **fields):
        if session_id == old.id and fields.get("archived"):
            persisting_archive.set()
            await allow_archive.wait()
        await update_session(session_id, **fields)

    monkeypatch.setattr(manager, "_archive_original", hold_archive_entry)
    monkeypatch.setattr(manager, "_close_original_execution", observe_close)
    if boundary == "pre_commit":
        monkeypatch.setattr(Transaction, "execute", hold_consumption)
    elif boundary == "mailbox":
        monkeypatch.setattr(db, "get_live_agent_configuration", hold_configuration)
        publisher = manager._session_owner(old).publisher
        can_offer = publisher.can_offer
        refused_notification = asyncio.Event()

        def refuse_schedule_notification(event):
            if any(item["source"] == "schedule" for item in event.get("dispatch_state", {}).get("items", ())):
                refused_notification.set()
                return False
            return can_offer(event)

        monkeypatch.setattr(publisher, "can_offer", refuse_schedule_notification)
    else:
        monkeypatch.setattr(db, "uncertain_submission_writers", hold_writer_check)
    monkeypatch.setattr(db, "update_session_field", hold_archive_persistence)
    archive = asyncio.create_task(manager.archive_session(old.id))
    firing = None
    try:
        await asyncio.wait_for(archive_ready.wait(), 3)
        firing = asyncio.create_task(dispatch_schedule(runner, "archive-during-submit"))
        await asyncio.wait_for(admitting.wait(), 3)
        begin_archive.set()
        await asyncio.wait_for(closing_owner.wait(), 3)
        release_admission.set()
        await asyncio.wait_for(persisting_archive.wait(), 3)
        if boundary == "mailbox":
            assert refused_notification.is_set()
        assert not firing.done() and not runs
        assert await manager.source_receipt("schedule", delivery_id) is None
        assert (await db.get_schedule_definition("archive-during-submit"))["origin_session_id"] == old.id
        assert schedule_job(runner, "archive-during-submit") is None

        allow_archive.set()
        successor = await asyncio.wait_for(archive, 3)
        await asyncio.wait_for(firing, 3)
        target, receipt = await manager.source_receipt("schedule", delivery_id)
        assert target == successor.id and receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        assert [run.session_id for run in runs] == [successor.id]
        assert await db.get_schedule_definition("archive-during-submit") is None
        assert schedule_job(runner, "archive-during-submit") is None
        if boundary == "unretired_writer":
            assert (await submission_status(manager, old.id, "previous-input"))["execution"]["state"] == "unknown"
    finally:
        begin_archive.set()
        release_admission.set()
        allow_archive.set()
        await asyncio.gather(archive, *([firing] if firing else []), return_exceptions=True)


@pytest.mark.parametrize("boundary", ["before_origin", "before_origin_restored", "before_owner"])
async def test_fire_preparing_the_old_target_recaptures_an_already_settled_rebind(setup, monkeypatch, boundary):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    old = await manager.create_session(agent["id"])
    await save_once(db, agent["id"], "late-target", origin_session_id=old.id)
    await runner.initialize(paused=True)
    runs = capture_model_runs(manager, monkeypatch)
    prepared, release = asyncio.Event(), asyncio.Event()
    start = manager.start_message
    start_one_shot = manager.start_one_shot
    create_session = manager._create_session
    stale_preparations = []
    selected = []

    async def hold_old_target(session_id, *args, **kwargs):
        if kwargs.get("schedule") is not None:
            selected.append(session_id)
            if boundary == "before_owner" and session_id == old.id:
                prepared.set()
                await release.wait()
        return await start(session_id, *args, **kwargs)

    async def hold_old_definition(row, *args, **kwargs):
        if row["origin_session_id"] == old.id:
            prepared.set()
            await release.wait()
        return await start_one_shot(row, *args, **kwargs)

    async def refuse_stale_temporary(*args, **kwargs):
        if kwargs.get("origin") == "schedule":
            stale_preparations.append(kwargs["origin"])
            raise CredentialBindingError("Temporary target credential became unavailable")
        return await create_session(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", hold_old_target)
    if boundary != "before_owner":
        monkeypatch.setattr(manager, "start_one_shot", hold_old_definition)
        monkeypatch.setattr(manager, "_create_session", refuse_stale_temporary)
    firing = asyncio.create_task(dispatch_schedule(runner, "late-target"))
    try:
        await asyncio.wait_for(prepared.wait(), 3)
        successor = await asyncio.wait_for(manager.archive_session(old.id), 3)
        if boundary == "before_origin_restored":
            await manager.unarchive_session(old.id)
        release.set()
        await asyncio.wait_for(firing, 3)
        assert not stale_preparations
        assert selected == ([old.id, successor.id] if boundary == "before_owner" else [successor.id])
        target, receipt = await manager.source_receipt("schedule", f"schedule:late-target:{_DUE}")
        assert target == successor.id and receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        assert [run.session_id for run in runs] == [successor.id]
        assert await db.get_schedule_definition("late-target") is None
        assert schedule_job(runner, "late-target") is None
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)


@pytest.mark.parametrize(("first", "updates"), [
    ({"run_at": "2020-01-01T01:00:00Z"}, {"run_at": "2099-01-01T01:00:00Z"}),
    ({"run_at": "2020-01-01T01:00:00Z"}, {"run_at": "2019-01-01T01:00:00Z"}),
    ({"run_at": "2020-01-01T01:00:00Z"}, {"run_at": None, "interval_seconds": 3600}),
    ({"interval_seconds": 3600}, {"run_at": "2020-01-01T01:00:00Z", "interval_seconds": None}),
    ({"interval_seconds": 3600}, {"interval_seconds": 7200}),
    ({"cron": "0 9 * * *", "timezone": "UTC"}, {"timezone": "Asia/Taipei"}),
    ({"run_at": "2020-01-01T01:00:00Z"}, {"enabled": False}),
    ({"run_at": "2020-01-01T01:00:00Z"}, None),
])
async def test_dispatched_fire_cannot_use_a_replaced_timing_before_registration(setup, monkeypatch, first, updates):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    runs = capture_model_runs(manager, monkeypatch)
    await save_schedule(db, schedule_id="move-date", agent_id=agent["id"], name="Reminder", prompt="Run once",
                        created_at="2020-01-01T00:00:00Z", **first)
    await runner.reconcile("move-date")
    old_job, = runner._scheduler.get_jobs()
    if isinstance(old_job.trigger, DateTrigger):
        old_job.remove()  # APScheduler removes the date job when it dispatches the callback.

    original = await db.get_schedule_definition("move-date")
    expected = {**original, **updates} if updates is not None else None
    await runner._registration_lock.acquire()
    edit = asyncio.create_task(runner.update(original, updates) if updates is not None else runner.delete("move-date"))
    try:
        async with asyncio.timeout(3):
            while await db.get_schedule_definition("move-date") != expected:
                await asyncio.sleep(0)
            await old_job.func(*old_job.args)
        assert await db.get_schedule_definition("move-date") == expected
    finally:
        runner._registration_lock.release()
        await edit
    replacement = schedule_job(runner, "move-date")
    await old_job.func(*old_job.args)
    assert schedule_job(runner, "move-date") is replacement
    assert await db.get_schedule_definition("move-date") == expected
    for row in (original, expected):
        if row and row["run_at"]:
            assert await manager.source_receipt("schedule", f"schedule:move-date:{row['run_at']}") is None
    assert not runs


@pytest.mark.parametrize("changed", ["name", "recurrence_label", "prompt", "origin_session_id", "registration"])
async def test_date_admission_rechecks_the_definition_after_target_preparation(setup, monkeypatch, changed):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    original = await manager.create_session(agent["id"], name="Original target")
    replacement = await manager.create_session(agent["id"], name="Replacement target")
    await save_once(db, agent["id"], "prepared", origin_session_id=original.id)
    await runner.initialize(paused=True)
    original_job = schedule_job(runner, "prepared")
    definition = await db.get_schedule_definition("prepared")
    runs = capture_model_runs(manager, monkeypatch)
    prepared, release = asyncio.Event(), asyncio.Event()
    admit = db.admit_client_submission

    async def hold_admission(*args, **kwargs):
        if kwargs.get("schedule") is not None and not prepared.is_set():
            prepared.set()
            await release.wait()
        return await admit(*args, **kwargs)

    monkeypatch.setattr(db, "admit_client_submission", hold_admission)
    firing = asyncio.create_task(dispatch_schedule(runner, "prepared"))
    try:
        await asyncio.wait_for(prepared.wait(), 3)
        value = "Edited definition"
        new_job = None
        if changed == "origin_session_id":
            value = replacement.id
            await db.repoint_schedules_origin(original.id, replacement.id)
        elif changed == "registration":
            await runner.update(definition, {"enabled": False})
            await runner.update({**definition, "enabled": False}, {"enabled": True})
            assert await db.get_schedule_definition("prepared") == definition
            new_job = schedule_job(runner, "prepared")
        else:
            await db.update_schedule("prepared", **{changed: value})
        release.set()
        await asyncio.wait_for(firing, 3)
        if changed == "origin_session_id":
            assert schedule_job(runner, "prepared") is None
        else:
            assert await manager.source_receipt("schedule", f"schedule:prepared:{_DUE}") is None
            current = await db.get_schedule_definition("prepared")
            assert current["enabled"]
            if new_job is not None:
                assert current == definition and schedule_job(runner, "prepared") is new_job
            else:
                assert current[changed] == value
                assert schedule_job(runner, "prepared").id != original_job.id
            await dispatch_schedule(runner, "prepared")
        target, accepted = await manager.source_receipt("schedule", f"schedule:prepared:{_DUE}")
        assert accepted.accepted and target == (replacement.id if changed == "origin_session_id" else original.id)
        assert (await asyncio.wait_for(accepted.wait(), 3)).outcome.status == "success"
        assert len(runs) == 1
        if changed in {"name", "recurrence_label", "prompt"}:
            assert value in runs[0].prompt
        assert await db.get_schedule_definition("prepared") is None
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)


async def test_committed_pause_revokes_dispatched_date_before_registration_reconciles(setup, monkeypatch):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"])
    await save_once(db, agent["id"], "pause-resume", origin_session_id=origin.id)
    await runner.initialize(paused=True)
    definition = await db.get_schedule_definition("pause-resume")
    old_job = schedule_job(runner, "pause-resume")
    runs = capture_model_runs(manager, monkeypatch)
    prepared, release = asyncio.Event(), asyncio.Event()
    admit = db.admit_client_submission

    async def hold_original(*args, **kwargs):
        if kwargs.get("schedule") is not None and not prepared.is_set():
            prepared.set()
            await release.wait()
        return await admit(*args, **kwargs)

    monkeypatch.setattr(db, "admit_client_submission", hold_original)
    firing = asyncio.create_task(dispatch_schedule(runner, "pause-resume"))
    controls = []
    delivery_id = f"schedule:pause-resume:{_DUE}"
    try:
        await asyncio.wait_for(prepared.wait(), 3)
        async with runner._registration_lock, asyncio.timeout(3):
            paused = {**definition, "enabled": False}
            controls.append(asyncio.create_task(runner.update(definition, {"enabled": False})))
            while await db.get_schedule_definition("pause-resume") != paused:
                await asyncio.sleep(0)
            controls.append(asyncio.create_task(runner.update(paused, {"enabled": True})))
            while await db.get_schedule_definition("pause-resume") != definition:
                await asyncio.sleep(0)
            assert all(not control.done() for control in controls)
            assert runner.next_run_at("pause-resume") is None
            release.set()
            await firing
            assert await manager.source_receipt("schedule", delivery_id) is None
            assert await db.get_schedule_definition("pause-resume") == definition
            assert not runs

        await asyncio.gather(*controls)
        assert schedule_job(runner, "pause-resume").id != old_job.id
        await dispatch_schedule(runner, "pause-resume")
        target, receipt = await manager.source_receipt("schedule", delivery_id)
        assert target == origin.id and (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        await old_job.func(*old_job.args)
        assert len(runs) == 1 and await db.get_schedule_definition("pause-resume") is None
    finally:
        release.set()
        await asyncio.gather(firing, *controls, return_exceptions=True)


async def test_date_rearms_while_app_deletion_blocks_its_live_target(setup, monkeypatch, tmp_path):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"])
    await db.save_application(app_id="builder", user_id=agent["user_id"], name="Builder", app_dir=str(tmp_path),
                              agent_id=agent["id"], session_id=origin.id, created_at=_DUE, updated_at=_DUE)
    await save_once(db, agent["id"], "app-reminder", origin_session_id=origin.id)
    await runner.initialize(paused=True)
    await db.set_application_deletion("builder", {"phase": "files", "keep_files": True, "error": None})
    runs = capture_model_runs(manager, monkeypatch)
    await dispatch_schedule(runner, "app-reminder")
    delivery_id = f"schedule:app-reminder:{_DUE}"
    assert not runs and await manager.source_receipt("schedule", delivery_id) is None
    assert (await db.get_schedule_definition("app-reminder"))["enabled"]
    assert runner.next_run_at("app-reminder") is not None

    await db.delete_application("builder")
    await dispatch_schedule(runner, "app-reminder")
    target, receipt = await manager.source_receipt("schedule", delivery_id)
    assert target == origin.id and (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
    assert len(runs) == 1 and await db.get_schedule_definition("app-reminder") is None


async def test_actual_date_dispatch_survives_a_clock_rollback_before_sqlite_admission(setup, monkeypatch):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    planned = "2035-10-01T09:00:00+08:00"
    due = datetime(2035, 10, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        current = due - timedelta(minutes=1)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    for module in (scheduler, client_submissions, apscheduler_base):
        monkeypatch.setattr(module, "datetime", Clock)
    await save_once(db, agent["id"], "rollback", run_at=planned)
    await runner.initialize(paused=True)
    job = schedule_job(runner, "rollback")
    runs = capture_model_runs(manager, monkeypatch)
    prepared, release = asyncio.Event(), asyncio.Event()
    admit = db.admit_client_submission

    async def hold_before_write(*args, **kwargs):
        if kwargs.get("schedule") is not None and not prepared.is_set():
            prepared.set()
            await release.wait()
        return await admit(*args, **kwargs)

    monkeypatch.setattr(db, "admit_client_submission", hold_before_write)
    try:
        Clock.current = due
        runner.start()
        await asyncio.wait_for(prepared.wait(), 3)
        assert runner._scheduler.get_job(job.id) is None
        Clock.current = due - timedelta(seconds=30)
        release.set()
        async with asyncio.timeout(3):
            while runner.next_run_at("rollback") is None:
                await asyncio.sleep(.005)
        assert datetime.fromisoformat(runner.next_run_at("rollback")) == due
        assert await manager.source_receipt("schedule", f"schedule:rollback:{planned}") is None
        assert not runs and (await db.get_schedule_definition("rollback"))["enabled"]
        Clock.current = due
        runner._scheduler.wakeup()
        async with asyncio.timeout(3):
            while await db.get_schedule_definition("rollback") is not None:
                await asyncio.sleep(.005)
        _, receipt = await manager.source_receipt("schedule", f"schedule:rollback:{planned}")
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        await job.func(*job.args)
        assert len(runs) == 1 and runner.next_run_at("rollback") is None
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["first_read", "live_owner"])
async def test_healthy_database_lock_timeout_rearms_and_same_live_owner_can_admit(setup, monkeypatch, stage):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"])
    owner = manager._session_owner(origin)
    runs = capture_model_runs(manager, monkeypatch)
    await save_once(db, agent["id"], "locked", origin_session_id=origin.id)
    await runner.initialize(paused=True)
    transaction = db.transaction
    loader = db.get_schedule_definition if stage == "first_read" else db.get_client_submission
    lookup = "get_schedule_definition" if stage == "first_read" else "get_client_submission"
    waiting, release, timed_out = (asyncio.Event() for _ in range(3))
    blocked = None

    @asynccontextmanager
    async def short_timeout(*, write=True, timeout=5.0):
        this_attempt = asyncio.current_task() is blocked
        try:
            async with transaction(write=write, timeout=.02 if this_attempt else timeout) as tx:
                yield tx
        except TimeoutError:
            if this_attempt:
                timed_out.set()
            raise

    async def read_while_locked(*args, **kwargs):
        nonlocal blocked
        if not waiting.is_set():
            waiting.set()
            await release.wait()
            blocked = asyncio.current_task()
            try:
                return await loader(*args, **kwargs)
            finally:
                blocked = None
        return await loader(*args, **kwargs)

    monkeypatch.setattr(db, "transaction", short_timeout)
    monkeypatch.setattr(db, lookup, read_while_locked)
    earliest = datetime.now(UTC) + timedelta(seconds=5)
    firing = asyncio.create_task(dispatch_schedule(runner, "locked"))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        async with transaction(write=True):
            release.set()
            await asyncio.wait_for(timed_out.wait(), 3)
        await asyncio.wait_for(firing, 3)
        assert db.health.cause is None and manager.get_session(origin.id) is origin and not owner.task.done()
        assert datetime.fromisoformat(runner.next_run_at("locked")) >= earliest
        assert await manager.source_receipt("schedule", f"schedule:locked:{_DUE}") is None
        await dispatch_schedule(runner, "locked")
        target, receipt = await manager.source_receipt("schedule", f"schedule:locked:{_DUE}")
        assert target == origin.id and manager._session_owner(origin) is owner and receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        assert len(runs) == 1 and await db.get_schedule_definition("locked") is None
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)


@pytest.mark.parametrize("fatal", [False, True])
async def test_date_finishes_without_rearming_on_missing_definition_or_untrusted_storage(setup, monkeypatch, fatal):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    runs = capture_model_runs(manager, monkeypatch)
    await save_once(db, agent["id"], "gone")
    await runner.initialize(paused=True)
    if fatal:
        class DriverUntrusted(BaseException):
            pass

        def exit_process(_status):
            raise DriverUntrusted

        health = ServiceHealth(exit_process=exit_process)
        with pytest.raises(DriverUntrusted):
            health.fail(OSError("SQLite acknowledgment was lost"))
        with monkeypatch.context() as scoped:
            scoped.setattr(db, "health", health)
            await dispatch_schedule(runner, "gone")
        assert (await db.get_schedule_definition("gone"))["enabled"]
    else:
        await db.delete_schedule("gone")
        await dispatch_schedule(runner, "gone")
    assert runner.next_run_at("gone") is None and not runs
    assert await manager.source_receipt("schedule", f"schedule:gone:{_DUE}") is None


@pytest.mark.parametrize("in_flight", [False, True])
async def test_healthy_unknown_answer_observes_original_writer_before_retry(setup, monkeypatch, in_flight):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"])
    runs = capture_model_runs(manager, monkeypatch)
    await save_once(db, agent["id"], "unknown", origin_session_id=origin.id)
    await runner.initialize(paused=True)
    delivery_id = f"schedule:unknown:{_DUE}"
    writing, release, observed, acknowledgment_lost = (asyncio.Event() for _ in range(4))
    admit, start_message, source_receipt = db.admit_client_submission, manager.start_message, manager.source_receipt
    original = None

    async def hold_write(*args, **kwargs):
        if kwargs.get("schedule") is not None and in_flight:
            writing.set()
            await release.wait()
        return await admit(*args, **kwargs)

    async def lose_answer(*args, **kwargs):
        nonlocal original
        if not acknowledgment_lost.is_set() and kwargs.get("delivery_id") == delivery_id:
            if in_flight:
                original = asyncio.create_task(start_message(*args, **kwargs))
                await writing.wait()
            acknowledgment_lost.set()
            raise TimeoutError("Caller lost the original answer")
        return await start_message(*args, **kwargs)

    async def observe(source, identity, **kwargs):
        result = await source_receipt(source, identity, **kwargs)
        if acknowledgment_lost.is_set() and identity == delivery_id and result is None:
            observed.set()
        return result

    monkeypatch.setattr(db, "admit_client_submission", hold_write)
    monkeypatch.setattr(manager, "start_message", lose_answer)
    monkeypatch.setattr(manager, "source_receipt", observe)
    fire = asyncio.create_task(dispatch_schedule(runner, "unknown"))
    try:
        await asyncio.wait_for(observed.wait(), 3)
        assert await source_receipt("schedule", delivery_id) is None
        if in_flight:
            assert not fire.done() and runner.next_run_at("unknown") is None
            release.set()
            await asyncio.wait_for(fire, 3)
        else:
            await asyncio.wait_for(fire, 3)
            assert runner.next_run_at("unknown") is not None
            await dispatch_schedule(runner, "unknown")
        target, receipt = await source_receipt("schedule", delivery_id)
        assert target == origin.id and receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
        assert len(runs) == 1 and runner.next_run_at("unknown") is None
    finally:
        release.set()
        await asyncio.gather(fire, *([original] if original is not None else []), return_exceptions=True)


async def test_lost_temporary_acknowledgment_confirms_original_and_leaves_later_registration_alone(setup, monkeypatch):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    await save_once(db, agent["id"], "lost")
    original = await db.get_schedule_definition("lost")
    context = decode_context(original["knowledge_context"])
    await runner.initialize(paused=True)
    runs = capture_model_runs(manager, monkeypatch, hold=True)
    committed, lose_answer = asyncio.Event(), asyncio.Event()
    start, archive, observe = manager.start_one_shot, manager.auto_archive_scheduled_session, manager.source_receipt
    archived, recovery_queries = [], []

    async def admit_without_answer(*args, **kwargs):
        await start(*args, **kwargs)
        committed.set()
        await lose_answer.wait()
        raise TimeoutError("Durable receipt was not returned to the timer")

    async def record_archive(session_id, **kwargs):
        archived.append(session_id)
        return await archive(session_id, **kwargs)

    async def record_recovery(source, identity, **kwargs):
        if lose_answer.is_set():
            recovery_queries.append((source, identity))
        return await observe(source, identity, **kwargs)

    monkeypatch.setattr(manager, "start_one_shot", admit_without_answer)
    monkeypatch.setattr(manager, "auto_archive_scheduled_session", record_archive)
    monkeypatch.setattr(manager, "source_receipt", record_recovery)
    fire = asyncio.create_task(dispatch_schedule(runner, "lost"))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        target, receipt = await observe("schedule", f"schedule:lost:{_DUE}")
        async with asyncio.timeout(3):
            while not runs:
                await asyncio.sleep(.005)
        await save_once(db, agent["id"], "lost", run_at="2099-01-01T01:00:00Z")
        await runner.reconcile("lost")
        replacement = schedule_job(runner, "lost")
        current = await db.get_schedule_definition("lost")
        async with db.transaction(write=True) as tx:
            await tx.execute("UPDATE agents SET mcp_servers='null' WHERE id=?", (agent["id"],))
        lose_answer.set()
        await asyncio.wait_for(fire, 3)
        assert recovery_queries == [("schedule", f"schedule:lost:{_DUE}")]
        confirmed_target, confirmed = await start(original, "Observe only", context)
        assert (confirmed_target, confirmed.request_id) == (target, receipt.request_id)
        assert await db.get_schedule_definition("lost") == current and schedule_job(runner, "lost") is replacement
        assert manager.get_session(target) is not None and not archived
        runs[0].release.set()
        async with asyncio.timeout(3):
            while manager.get_session(target) is not None:
                await asyncio.sleep(.005)
        assert (await db.get_session_binding(target))["archived"]
        assert archived == [target] and len(runs) == 1
    finally:
        lose_answer.set()
        for run in runs:
            run.release.set()
        await asyncio.gather(fire, return_exceptions=True)


async def test_one_shot_is_admitted_behind_paused_human_input(setup, tmp_path):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    origin = await manager.create_session(agent["id"], "One-shot caller", str(tmp_path), backend="codex")
    human = await _submit_paused(manager, origin.id, "Human backlog", "human")
    await save_schedule(
        db, schedule_id="once", agent_id=agent["id"], name="Reminder", prompt="Retain this fire",
        created_at="2026-10-23T00:00:00Z", run_at="2026-10-23T01:00:00Z", origin_session_id=origin.id,
    )
    await dispatch_schedule(runner, "once")
    await dispatch_schedule(runner, "once")
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
    assert runner.next_run_at("overdue") is not None


@pytest.mark.parametrize("destination", ["live", "temporary", "rebound"])
async def test_bad_agent_keeps_schedule_registered_and_original_receipt_readable(setup, monkeypatch, destination):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    runs = capture_model_runs(manager, monkeypatch)
    timestamp = "2020-01-01T01:00:00Z"
    origin = await manager.create_session(agent["id"]) if destination != "temporary" else None
    for schedule_id in ("original", "pending"):
        await save_schedule(db, schedule_id=schedule_id, agent_id=agent["id"], name=schedule_id,
                            prompt="Schedule execution", created_at="2020-01-01T00:00:00Z", run_at=timestamp,
                            origin_session_id=origin.id if origin and (schedule_id == "pending" or destination == "rebound") else None)
    original = await db.get_schedule_definition("original")
    await dispatch_schedule(runner, "original")
    original_session, original_receipt = await manager.source_receipt("schedule", f"schedule:original:{timestamp}")
    assert original_receipt.accepted
    assert (await asyncio.wait_for(original_receipt.wait(), 3)).outcome.status == "success"
    assert len(runs) == 1
    if destination == "rebound":
        origin = await manager.archive_session(origin.id)
        assert (await db.get_schedule_definition("pending"))["origin_session_id"] == origin.id
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE agents SET mcp_servers = 'null' WHERE id = ?", (agent["id"],))

    await runner.initialize(paused=True)
    assert runner.next_run_at("pending") is not None
    await dispatch_schedule(runner, "pending")
    assert await db.get_schedule_definition("pending") is not None and len(runs) == 1
    assert runner.next_run_at("pending") is not None
    assert await manager.source_receipt("schedule", f"schedule:pending:{timestamp}") is None
    context = decode_context(original["knowledge_context"])
    assert context is not None
    observed_session, observed = await manager.start_one_shot(original, "Never execute twice", context)
    assert (observed_session, observed.request_id) == (original_session, original_receipt.request_id)

    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE agents SET mcp_servers = '[]' WHERE id = ?", (agent["id"],))
    await dispatch_schedule(runner, "pending")
    target, receipt = await manager.source_receipt("schedule", f"schedule:pending:{timestamp}")
    assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
    assert origin is None or target == origin.id
    assert await db.get_schedule_definition("pending") is None and len(runs) == 2
    assert (await db.get_agent(agent["id"]))["mcp_servers"] == []


async def test_refused_one_shot_keeps_its_definition_and_retry_without_an_orphan_session(setup):
    manager, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(
        db, schedule_id="refused", agent_id=agent["id"], name="Retained reminder", prompt="Try original fire later",
        created_at="2020-01-01T00:00:00Z", run_at="2020-01-01T01:00:00Z",
    )
    await runner.initialize(paused=True)
    original = await db.get_schedule_definition("refused")
    manager.close_admission()
    earliest = datetime.now(UTC) + timedelta(seconds=5)
    await dispatch_schedule(runner, "refused")
    assert await db.get_schedule_definition("refused") == original
    assert datetime.fromisoformat(runner.next_run_at("refused")) >= earliest
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
    deps.set_user_manager(UserManager(db))
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
        await dispatch_schedule(runner, "crash-once")
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
    await create_account(db)
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
    await dispatch_schedule(ScheduleRunner(manager, db), "crash-once")
    raise AssertionError("An uncertain admission commit returned")

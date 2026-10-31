"""A real background process returns to its fixed caller after archive."""

import asyncio
import shlex
import sys

import pytest

from server.bg_tasks import BgTaskManager
from server.sessions.submission import (
    ReturnRefused,
    ReturnUnconfirmed,
    SubmissionReceipt,
)
from tests.test_research_session_return import returning_research as returning_research


async def test_background_reconciliation_keeps_full_masked_exception_chain(returning_research, monkeypatch, tmp_path, caplog):
    db, manager, _, _, _, _ = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Original transport diagnostic')
    await manager.interrupt(session.id)
    submit = manager.start_message
    tail = 'ORIGINAL_BG_TRANSPORT_DIAGNOSTIC_END'

    async def lose_ack(*args, **kwargs):
        await submit(*args, **kwargs)
        try:
            raise ValueError('Original transport cause token=DO_NOT_EXPOSE_ORIGINAL_TOKEN')
        except ValueError as cause:
            raise ConnectionError('Original ACK lost ' + 'detail ' * 400 + tail) from cause

    monkeypatch.setattr(manager, 'start_message', lose_ack)
    background = BgTaskManager()
    background.bind(db, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    try:
        record = await background.start_task(session_id=session.id, command="printf 'original'", working_dir=str(tmp_path))
        await asyncio.wait_for(asyncio.shield(background._running[record.id].task), 3)
        assert (await background.get_task(record.id)).delivery_disposition is None
        assert tail in caplog.text and 'Original transport cause token=[redacted]' in caplog.text
        assert 'DO_NOT_EXPOSE_ORIGINAL_TOKEN' not in caplog.text
    finally:
        await background.shutdown()


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_background_command_survives_archive_and_returns_once(returning_research, tmp_path, paused, exit_code):
    db, manager, _, _, _, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Original background caller")
    gate, executions = tmp_path / "release", tmp_path / "executions"
    program = (
        "import pathlib,sys,time\n"
        "gate=pathlib.Path(sys.argv[1])\n"
        "while not gate.exists(): time.sleep(0.01)\n"
        "with open(sys.argv[2],'a') as output: output.write('once\\n')\n"
        "print('retained output',flush=True)\n"
        "sys.exit(int(sys.argv[3]))\n"
    )
    command = shlex.join([sys.executable, "-c", program, str(gate), str(executions), str(exit_code)])
    finished = asyncio.Event()
    accepted = []

    async def deliver(record):
        result = await manager.deliver_bg_result(record)
        accepted.append(result)
        finished.set()
        return result

    background = BgTaskManager()
    background.bind(db, deliver, manager._broadcast)
    await background.start()
    try:
        record = await background.start_task(session_id=session.id, command=command, working_dir=str(tmp_path))
        if paused:
            await manager.interrupt(session.id)
        await manager.archive_session(session.id, replace=False)
        gate.touch()
        await asyncio.wait_for(finished.wait(), 5)
        assert len(accepted) == 1 and isinstance(accepted[0], SubmissionReceipt)
        assert manager.dispatch_snapshot(session.id)["paused"] is paused
        if paused:
            assert processed.empty()
            await manager.resume_dispatch(session.id)
        prompt = await asyncio.wait_for(processed.get(), 3)
        assert "[bg-task-result]" in prompt and "retained output" in prompt
        duplicate = await manager.deliver_bg_result(record)
        assert isinstance(duplicate, SubmissionReceipt) and duplicate.request_id == accepted[0].request_id
        assert processed.empty()
        assert executions.read_text() == "once\n"
        stored = await db.get_bg_task(record.id)
        assert stored["stdout"] == "retained output\n" and stored["exit_code"] == exit_code
    finally:
        gate.touch()
        await background.shutdown()


@pytest.mark.parametrize("receipt_readable", [False, True])
@pytest.mark.parametrize("diagnostic_failure", [False, True])
async def test_background_lost_ack_observes_only_original_receipt(
    returning_research, monkeypatch, tmp_path, receipt_readable, diagnostic_failure,
):
    from server.sessions.runtime_types import SessionOwnerUnavailable

    db, manager, _, _, _, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Unconfirmed background return")
    await manager.interrupt(session.id)
    submit, lookup, update = manager.start_message, manager.source_receipt, db.update_bg_task
    submissions = 0
    post_handoff_reads = 0
    diagnostic_recovered = False

    async def lose_ack(session_id, prompt, **kwargs):
        nonlocal submissions
        submissions += 1
        assert submissions == 1, "Unconfirmed admission must never submit again"
        await submit(session_id, prompt, **kwargs)
        raise ConnectionError("Original background acceptance acknowledgment lost")

    async def unavailable(*args, **kwargs):
        nonlocal post_handoff_reads
        if submissions:
            post_handoff_reads += 1
        if submissions and (not receipt_readable or diagnostic_failure and not diagnostic_recovered):
            raise SessionOwnerUnavailable("Original receipt is temporarily unreadable")
        return await lookup(*args, **kwargs)

    async def fail_diagnostic(task_id, **fields):
        if fields.get("delivery_disposition") == "unknown":
            raise RuntimeError("Delivery diagnostic write failed")
        await update(task_id, **fields)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", unavailable)
    if diagnostic_failure:
        monkeypatch.setattr(db, "update_bg_task", fail_diagnostic)
    background = BgTaskManager()
    background.bind(db, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    try:
        record = await background.start_task(
            session_id=session.id, command="printf 'saved output'", working_dir=str(tmp_path),
        )
        execution = background._running[record.id].task
        if diagnostic_failure:
            with pytest.raises(RuntimeError, match="diagnostic write failed"):
                await asyncio.wait_for(asyncio.shield(execution), 2)
            monkeypatch.setattr(db, "update_bg_task", update)
            diagnostic_recovered = True
            await background.shutdown()  # Original settlement reentry, never a fresh submission.
        else:
            await asyncio.wait_for(asyncio.shield(execution), 2)
        saved = await background.get_task(record.id)
        assert saved.status == "completed" and saved.exit_code == 0 and saved.stdout == "saved output"
        if receipt_readable:
            assert saved.delivery_disposition is None and saved.delivery_error is None
        else:
            assert saved.delivery_disposition == "unknown"
            assert "acknowledgment lost" in saved.delivery_error
        assert submissions == 1 and processed.empty()
        assert post_handoff_reads == (2 if diagnostic_failure else 1)
        assert len(manager.dispatch_snapshot(session.id)["items"]) == 1
    finally:
        monkeypatch.setattr(manager, "source_receipt", lookup)
        monkeypatch.setattr(db, "update_bg_task", update)
        await background.shutdown()


async def test_background_permanent_transcript_refusal_preserves_success(
    returning_research, monkeypatch, tmp_path,
):
    db, manager, _, _, _, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Damaged background destination")
    await manager.archive_session(session.id, replace=False)
    async with db.transaction(write=True) as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
        for _ in range(2):
            await tx.execute(
                "INSERT INTO messages(session_id,seq,role,type,content,created_at) "
                "VALUES(?,0,'user','text','\"duplicate\"','now')", (session.id,),
            )
    submit = manager.start_message
    attempts = []

    async def observe(*args, **kwargs):
        attempts.append(kwargs["delivery_id"])
        return await submit(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", observe)
    background = BgTaskManager()
    background.bind(db, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    try:
        record = await background.start_task(
            session_id=session.id, command="printf 'original result'", working_dir=str(tmp_path),
        )
        await asyncio.wait_for(asyncio.shield(background._running[record.id].task), 2)
        saved = await background.get_task(record.id)
        assert saved.status == "completed" and saved.exit_code == 0 and saved.stdout == "original result"
        assert saved.delivery_disposition == "stopped" and "duplicate sequences" in saved.delivery_error
        assert attempts == [f"bg:{record.id}"] and processed.empty()
        async with db.transaction(write=True) as tx:
            await tx.execute("DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages WHERE session_id=?)", (session.id,))
        await manager.archive_session(session.id, replace=False)
        await manager.unarchive_session(session.id)
        assert isinstance(await manager.deliver_bg_result(saved), ReturnRefused)
        assert attempts == [f"bg:{record.id}"] and processed.empty()
    finally:
        await background.shutdown()


async def test_background_identity_conflict_is_retained_and_propagated(returning_research, monkeypatch, tmp_path):
    from server.submission_models import SubmissionConflict

    db, manager, _, _, _, _ = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Conflicting return identity')
    conflict = SubmissionConflict('Original delivery identity has conflicting content')
    attempts = 0

    async def reject(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise conflict

    monkeypatch.setattr(manager, 'start_message', reject)
    background = BgTaskManager()
    background.bind(db, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    try:
        rec = await background.start_task(session_id=session.id, command="printf 'saved original result'", working_dir=str(tmp_path))
        with pytest.raises(SubmissionConflict) as observed:
            await asyncio.wait_for(asyncio.shield(background._running[rec.id].task), 2)
        assert observed.value is conflict
        saved = await background.get_task(rec.id)
        assert saved.status == 'completed' and saved.stdout == 'saved original result'
        assert saved.delivery_disposition == 'stopped' and 'SubmissionConflict' in saved.delivery_error
        await background.shutdown()
        assert attempts == 1
    finally:
        await background.shutdown()


async def test_background_returns_keep_acceptance_order_and_stop_through_human_correction(returning_research, tmp_path):
    db, manager, _, _, _, processed = returning_research
    session = await manager.create_session((await db.get_system_agent())["id"], "Paused returns")
    await manager.interrupt(session.id)
    background = BgTaskManager()
    background.bind(db, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    receipts = []

    async def finish_command(label):
        record = await background.start_task(
            session_id=session.id, command=f"printf {label}", working_dir=str(tmp_path),
        )
        await asyncio.wait_for(asyncio.shield(background._running[record.id].task), 2)
        receipt = await manager.deliver_bg_result(record)
        assert isinstance(receipt, SubmissionReceipt)
        receipts.append(receipt)
        return record.id

    try:
        first, second = await finish_command("first"), await finish_command("second")
        human = await manager.start_message(session.id, "Human correction")
        assert (await asyncio.wait_for(human.wait(), 2)).outcome.status == "success"
        assert await processed.get() == "Human correction"
        third = await finish_command("third")
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["paused"] is True and processed.empty()
        assert [item["request_id"] for item in snapshot["items"]] == [receipt.request_id for receipt in receipts]
        await manager.resume_dispatch(session.id)
        for task_id, receipt in zip((first, second, third), receipts, strict=True):
            assert task_id in await asyncio.wait_for(processed.get(), 2)
            assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
        assert processed.empty() and manager.dispatch_snapshot(session.id)["items"] == []
    finally:
        await background.shutdown()


async def test_background_settlement_reentry_refusal_cannot_clear_unknown(returning_research, tmp_path, monkeypatch):
    db, manager, _, _, _, _ = returning_research
    session = await manager.create_session((await db.get_system_agent())["id"], "Unknown remains unknown")
    attempts = []

    async def uncertain_then_closed(record):
        attempts.append(record.id)
        if len(attempts) == 1:
            return ReturnUnconfirmed(ConnectionError("Original handoff may have committed"))
        return ReturnRefused("admission_closed")

    background = BgTaskManager()
    background.bind(db, uncertain_then_closed, manager._broadcast)
    await background.start()
    update = db.update_bg_task

    async def fail_diagnostic(task_id, **fields):
        if fields.get("delivery_disposition") == "unknown":
            raise RuntimeError("Unconfirmed diagnostic write failed")
        await update(task_id, **fields)

    monkeypatch.setattr(db, "update_bg_task", fail_diagnostic)
    try:
        record = await background.start_task(
            session_id=session.id, command="printf retained", working_dir=str(tmp_path),
        )
        with pytest.raises(RuntimeError, match="diagnostic write failed"):
            await asyncio.wait_for(asyncio.shield(background._running[record.id].task), 2)
        assert attempts == [record.id]
        monkeypatch.setattr(db, "update_bg_task", update)
        await background.shutdown()  # Existing settlement reentry, no new observation trigger.
        saved = await background.get_task(record.id)
        assert saved.status == "completed" and saved.stdout == "retained"
        assert saved.delivery_disposition == "unknown" and "may have committed" in saved.delivery_error
        assert attempts == [record.id, record.id]
    finally:
        monkeypatch.setattr(db, "update_bg_task", update)
        await background.shutdown()

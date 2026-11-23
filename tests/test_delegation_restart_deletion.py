"""Deleted delegation relationships do not masquerade as storage outages."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.db.base import DatabaseBusy
from server.db.transaction import Transaction
from server.delegations import DelegationError
from server.mcp_identity import verify
from server.service_health import ServiceUnavailable
from server.sessions.runtime_types import SessionOwnerUnavailable
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_nested import gated_cli as gated_cli
from tests.test_delegation_restart_security import (
    completed_before_restart,
    wait_live_scope,
)


@pytest.mark.parametrize("committed", [False, True], ids=["before-cas", "after-commit"])
async def test_child_delete_joins_the_original_continuation_cas(runtime, monkeypatch, committed):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    db, child_id = current.database, original.delegation_id
    previous = current.delegations.get_delegation(child_id)
    compare = db.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()
    proposed_work = None

    async def gate_continuation(session_id, expected, candidate, **options):
        nonlocal proposed_work
        snapshot = json.loads(candidate)
        if session_id != child_id or snapshot["state"] != "running":
            return await compare(session_id, expected, candidate, **options)
        proposed_work = snapshot["work_id"]
        if committed:
            assert await compare(session_id, expected, candidate, **options)
        entered.set()
        await release.wait()
        return True if committed else await compare(session_id, expected, candidate, **options)

    monkeypatch.setattr(db, "compare_and_set_delegation_state", gate_continuation)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=child_id, request="continuation during deletion",
    ))
    deleting = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        deleting = asyncio.create_task(current.manager.delete_session(child_id))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(deleting), 0.15)
        assert await db.get_session_binding(child_id) is not None
        assert current.delegations.get_delegation(child_id) is previous
    finally:
        release.set()
        result, = await asyncio.gather(following, return_exceptions=True)
        if deleting is not None:
            assert await asyncio.wait_for(deleting, 3)
    assert isinstance(result, DelegationError) and result.status_code == 409
    assert await db.get_session_binding(child_id) is None
    assert current.delegations.get_delegation(child_id) is previous
    assert all(row.work_id != proposed_work for row in current.delegations.list_delegations(current.parent_id))
    assert starts.empty()
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


async def test_initial_admission_not_yet_holding_the_child_cannot_revive_a_deleted_conversation(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    create = current.manager.create_session
    created = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def hold_creation(*args, **kwargs):
        child = await create(*args, **kwargs)
        created.set_result(child.id)
        await release.wait()
        return child

    monkeypatch.setattr(current.manager, "create_session", hold_creation)
    asking = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="Initial work during deletion",
    ))
    try:
        child_id = await asyncio.wait_for(created, 3)
        assert await asyncio.wait_for(current.manager.delete_session(child_id), 3)
    finally:
        release.set()
        result, = await asyncio.gather(asking, return_exceptions=True)
    assert isinstance(result, DelegationError) and result.status_code == 409
    assert await current.database.get_session_binding(child_id) is None
    assert current.delegations.list_delegations(current.parent_id) == [] and starts.empty()
    await current.delegations.confirm_recording()


@pytest.mark.parametrize(("deleted", "status"), [("parent", 404), ("child", 409)])
@pytest.mark.parametrize("phase", ["before-cas", "after-cas", "before-input"])
async def test_relationship_deleted_during_followup_setup_is_reported_as_gone(
    runtime, monkeypatch, deleted, status, phase,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert current.manager.get_session(original.delegation_id) is not None
    save = current.database.compare_and_set_delegation_state
    saving = asyncio.Event()
    release = asyncio.Event()

    async def gated_save(*args, **kwargs):
        if phase == "after-cas":
            result = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            saving.set()
            await release.wait()
        return result if phase == "after-cas" else await save(*args, **kwargs)

    if phase == "before-input":
        get_identity = current.database.get_agent_identity

        async def gated_agent(*args, **kwargs):
            saving.set()
            await release.wait()
            return await get_identity(*args, **kwargs)

        monkeypatch.setattr(current.database, "get_agent_identity", gated_agent)
    else:
        monkeypatch.setattr(current.database, "compare_and_set_delegation_state", gated_save)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held review after setup",
    ))
    try:
        await asyncio.wait_for(saving.wait(), 5)
        deleted_id = current.parent_id if deleted == "parent" else original.delegation_id
        assert await current.manager.delete_session(deleted_id)
    finally:
        release.set()
        result = (await asyncio.gather(following, return_exceptions=True))[0]
    assert isinstance(result, DelegationError)
    assert result.status_code == status
    assert "gone" in str(result)
    assert starts.empty()
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()
    assert not await current.manager.delete_session(deleted_id)
    assert not current.delegations.drain_blockers()


@pytest.mark.parametrize("changed", ["target", "parent", "origin"])
async def test_changed_child_relation_is_a_confirmed_refusal_even_with_the_same_snapshot(
    runtime, monkeypatch, changed,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    other_parent = await current.manager.create_session(
        (await current.database.get_system_agent())["id"], "Other parent", backend="codex",
    )
    previous = await current.database.load_delegation_state(
        original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    )
    save = current.database.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()
    miss_reads: list[str] = []
    fetched_ids: list[list[str]] = []
    fetch_all = Transaction.fetch_all

    async def observe_relation_read(tx, sql, parameters=()):
        rows = await fetch_all(tx, sql, parameters)
        if sql.startswith("SELECT id, agent_id, user_id, origin, parent_session_id FROM sessions"):
            fetched_ids.append([row[0] for row in rows])
        return rows

    async def pause_new_round(*args, **kwargs):
        if json.loads(args[2])["state"] == "running":
            entered.set()
            await release.wait()
            saved = await save(*args, **kwargs)
            assert not saved
            await current.database.conn.set_trace_callback(miss_reads.append)
            return saved
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_new_round)
    monkeypatch.setattr(Transaction, "fetch_all", observe_relation_read)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held stale relationship review",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if changed == "target":
            replacement = await AgentManager(current.database).create_agent(
                name="Replacement", backend="codex", user_id=current.manager.get_session(current.parent_id).user_id,
            )
            await current.database.update_session_field(original.delegation_id, agent_id=replacement["id"])
            # The read scope intentionally does not include a target argument:
            # an equal old snapshot cannot confirm authority for the old target.
            assert await current.database.load_delegation_state(
                original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
            ) == previous
        elif changed == "origin":
            await current.database.update_session_field(original.delegation_id, origin="user")
        else:
            async with current.database.transaction(write=True) as tx:
                await tx.execute(
                    "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                    (other_parent.id, original.delegation_id),
                )
    finally:
        release.set()
    try:
        with pytest.raises(DelegationError) as refused:
            await following
    finally:
        await current.database.conn.set_trace_callback(None)
    assert refused.value.status_code == 409
    assert "relationship changed" in str(refused.value)
    current_round = current.delegations.get_delegation(original.delegation_id)
    assert current_round.work_id == original.work_id and current_round.state == "completed"
    assert starts.empty()
    await current.delegations.confirm_recording()
    session_reads = [
        " ".join(sql.split()) for sql in miss_reads if sql.startswith("SELECT") and " FROM sessions" in sql
    ]
    assert len(session_reads) == 1, session_reads
    projection = session_reads[0].split(" FROM sessions", 1)[0]
    assert set(projection.removeprefix("SELECT ").replace(" ", "").split(",")) == {
        "id", "agent_id", "user_id", "origin", "parent_session_id",
    }
    assert len(fetched_ids) == 1
    assert len(fetched_ids[0]) == 2
    assert set(fetched_ids[0]) == {current.parent_id, original.delegation_id}


async def test_cas_miss_relation_read_failure_does_not_settle_the_round(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    database = current.database
    save = database.compare_and_set_delegation_state
    execute = database.conn.execute
    read_error = ServiceUnavailable("Relationship read unavailable after CAS miss")

    async def change_relation_before_save(*args, **kwargs):
        await database.update_session_field(original.delegation_id, origin="user")
        saved = await save(*args, **kwargs)
        assert not saved
        return saved

    async def refuse_relationship_read(sql, parameters=()):
        if sql.startswith("SELECT id, agent_id, user_id, origin, parent_session_id FROM sessions"):
            raise read_error
        return await execute(sql, parameters)

    monkeypatch.setattr(database, "compare_and_set_delegation_state", change_relation_before_save)
    monkeypatch.setattr(database.conn, "execute", refuse_relationship_read)
    try:
        with pytest.raises(ServiceUnavailable) as failure:
            await current.delegations.follow_up_delegation(
                parent_session_id=current.parent_id, delegation_id=original.delegation_id,
                request="Review after relationship read failure",
            )
        assert failure.value is read_error
        still_original = current.delegations.get_delegation(original.delegation_id)
        assert still_original.work_id == original.work_id and still_original.state == "completed"
        assert starts.empty()
        with pytest.raises(RuntimeError, match="Delegation command cleanup failed") as recording:
            await current.delegations.confirm_recording()
        assert recording.value.__cause__ is read_error
    finally:
        # Release the isolated fixture after verifying the original failure
        # stays visible to its real recording/drain consumer.
        for command in tuple(current.delegations._commands):
            if command.done() and not command.cancelled() and command.exception() is read_error:
                current.delegations._forget_command(command)


@pytest.mark.parametrize("deleted", ["parent", "child"])
@pytest.mark.parametrize("lost_receipt", [False, True])
async def test_deletion_after_input_acceptance_keeps_original_round_responsibility(runtime, monkeypatch, deleted, lost_receipt):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    submit, read = current.manager.start_message, current.database.get_submission_for_request
    accepted, release = asyncio.Event(), asyncio.Event()
    receipts, observations = [], []

    async def held_receipt(sid, prompt, **kwargs):
        receipt = await submit(sid, prompt, **kwargs)
        if sid == original.delegation_id:
            receipts.append(receipt)
            accepted.set()
            await release.wait()
            if lost_receipt:
                raise SessionOwnerUnavailable("Original acceptance receipt unavailable")
        return receipt

    async def observe(sid, request_id):
        row = await read(sid, request_id)
        if sid == original.delegation_id and receipts and request_id == receipts[0].request_id:
            observations.append(row)
        return row

    monkeypatch.setattr(current.manager, "start_message", held_receipt)
    monkeypatch.setattr(current.database, "get_submission_for_request", observe)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held accepted review",
    ))
    deleting = None
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        await asyncio.wait_for(starts.get(), 5)
        rec = current.delegations.get_delegation(original.delegation_id)
        assert receipts[0].accepted and rec.work_id != original.work_id
        deleted_id = current.parent_id if deleted == "parent" else original.delegation_id
        deleting = asyncio.create_task(current.manager.delete_session(deleted_id))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(deleting), 0.15)
        assert await current.database.get_session_binding(deleted_id) is not None
    finally:
        release.set()
        settled = await asyncio.wait_for(following, 5)
        if deleting is not None:
            assert await asyncio.wait_for(deleting, 5)
    assert settled is rec and rec.state in {"failed", "cancelled", "interrupted"}
    assert len(receipts) == 1 and await current.database.get_session_binding(deleted_id) is None
    if lost_receipt:
        assert observations[0]["receipt"]["accepted"] is True
        assert observations[0]["payload"]["work_id"] == rec.work_id
    await current.delegations.confirm_recording()
    if deleted == "child":
        terminal = await current.manager.source_receipt("delegation", f"agent-terminal:{rec.work_id}", session_id=current.parent_id)
        assert terminal is not None and terminal[1].accepted
    assert not current.delegations.drain_blockers()


async def test_child_delete_retries_the_original_input_observation_after_a_failed_read(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    submit, read = current.manager.start_message, current.database.get_submission_for_request
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    submissions, observations = [], []
    fail_read = True

    async def lose_input_receipt(sid, prompt, **kwargs):
        receipt = await submit(sid, prompt, **kwargs)
        if sid != current.parent_id:
            assert receipt.accepted
            await asyncio.wait_for(starts.get(), 3)
            submissions.append((sid, receipt.request_id))
            raise SessionOwnerUnavailable("Original child receipt lost")
        return receipt

    async def observe_input(sid, request_id):
        if (sid, request_id) in submissions:
            observations.append((sid, request_id))
            if fail_read:
                raise OSError("Original child receipt read failed")
        return await read(sid, request_id)

    monkeypatch.setattr(current.manager, "start_message", lose_input_receipt)
    monkeypatch.setattr(current.database, "get_submission_for_request", observe_input)
    child_id = None
    try:
        with pytest.raises(OSError, match="Original child receipt read failed"):
            await current.delegations.start_delegation(
                parent_session_id=current.parent_id, agent_name="Worker", request="held initial input",
            )
        child_id, request_id = submissions[0]
        rec = current.delegations.get_delegation(child_id)
        with pytest.raises(OSError, match="Original child receipt read failed"):
            await current.manager.delete_session(child_id)
        row = await read(child_id, request_id)
        assert row["receipt"]["accepted"] and row["payload"]["work_id"] == rec.work_id
        assert await current.database.get_session_binding(child_id) is not None
    finally:
        fail_read = False
        if child_id is not None:
            assert await asyncio.wait_for(current.manager.delete_session(child_id), 3)
    assert len(submissions) == 1 and observations == submissions * 3
    assert await current.database.get_session_binding(child_id) is None
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


@pytest.mark.parametrize("busy_rollback", [False, True], ids=["unavailable-read", "busy-rollback"])
async def test_child_delete_rolls_back_the_original_continuation_when_no_input_was_written(runtime, monkeypatch, busy_rollback):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    child_id = original.delegation_id
    previous = current.delegations.get_delegation(child_id)
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    submit, read = current.manager.start_message, current.database.get_submission_for_request
    attempts, observations = [], []
    fail_read = not busy_rollback
    compare = current.database.compare_and_set_delegation_state
    rollbacks = 0

    async def busy_first_rollback(sid, expected, candidate, **options):
        nonlocal rollbacks
        if sid == child_id and json.loads(candidate)["work_id"] == previous.work_id:
            rollbacks += 1
            if rollbacks == 1:
                raise DatabaseBusy("Original rollback could not acquire SQLite")
        return await compare(sid, expected, candidate, **options)

    async def fail_before_commit(sid, prompt, **kwargs):
        if sid == child_id:
            attempts.append(kwargs["request_id"])
            raise SessionOwnerUnavailable("No child input reached the Owner")
        return await submit(sid, prompt, **kwargs)

    async def observe(sid, request_id):
        if sid == child_id and request_id in attempts:
            observations.append(request_id)
            if fail_read:
                raise OSError("Cannot observe original child input")
        return await read(sid, request_id)

    monkeypatch.setattr(current.manager, "start_message", fail_before_commit)
    monkeypatch.setattr(current.database, "get_submission_for_request", observe)
    if busy_rollback:
        monkeypatch.setattr(current.database, "compare_and_set_delegation_state", busy_first_rollback)
    try:
        error = DatabaseBusy if busy_rollback else OSError
        with pytest.raises(error):
            await current.delegations.follow_up_delegation(
                parent_session_id=current.parent_id, delegation_id=child_id, request="Continue the original work",
            )
        provisional = current.delegations.get_delegation(child_id)
        assert provisional.work_id != previous.work_id
    finally:
        fail_read = False
        assert await asyncio.wait_for(current.manager.delete_session(child_id), 3)
    assert len(attempts) == 1 and observations == attempts * (1 if busy_rollback else 2) and starts.empty()
    assert rollbacks == (2 if busy_rollback else 0)
    assert current.delegations.get_delegation(child_id) is previous
    assert await current.manager.source_receipt("delegation", f"agent-terminal:{provisional.work_id}") is None
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


async def test_child_delete_retries_empty_setup_cleanup_after_admission_cas_is_busy(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    archive = current.manager.auto_archive_scheduled_session
    targets, cleanups = [], []

    async def refuse_cas(sid, expected, candidate, **options):
        targets.append(sid)
        raise DatabaseBusy("Initial round could not acquire SQLite")

    async def busy_first_cleanup(sid, **options):
        cleanups.append(sid)
        if len(cleanups) == 1:
            raise DatabaseBusy("Initial cleanup could not acquire SQLite")
        return await archive(sid, **options)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", refuse_cas)
    monkeypatch.setattr(current.manager, "auto_archive_scheduled_session", busy_first_cleanup)
    with pytest.raises(DatabaseBusy, match="Initial cleanup"):
        await current.delegations.start_delegation(parent_session_id=current.parent_id, agent_name="Worker", request="Empty setup")
    child_id, = targets
    assert await current.database.get_session_binding(child_id) is not None
    assert current.delegations.get_delegation(child_id) is None
    assert await current.manager.delete_session(child_id)
    assert cleanups == [child_id, child_id] and targets == [child_id] and starts.empty()
    assert await current.database.get_session_binding(child_id) is None
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


@pytest.mark.parametrize("follow_up", [False, True], ids=["initial", "continuation"])
async def test_child_delete_withdraws_input_returned_closed_by_the_original_owner(runtime, monkeypatch, follow_up):
    boot, starts = runtime
    if follow_up:
        current, original = await completed_before_restart(boot, starts)
        previous = current.delegations.get_delegation(original.delegation_id)
    else:
        current = await boot()
        previous = None
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    submit = current.manager.start_message
    entered = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    refused = []

    async def wait_for_owner_close(sid, prompt, **options):
        if sid != current.parent_id:
            entered.set_result((sid, options["work_id"], options["request_id"]))
            await release.wait()
            receipt = await submit(sid, prompt, **options)
            refused.append(receipt)
            return receipt
        return await submit(sid, prompt, **options)

    monkeypatch.setattr(current.manager, "start_message", wait_for_owner_close)
    setup = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=previous.delegation_id, request="Never enter closed child",
    ) if previous is not None else current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="Never enter closed child",
    ))
    deleting = None
    try:
        child_id, work_id, request_id = await asyncio.wait_for(entered, 3)
        deleting = asyncio.create_task(current.manager.delete_session(child_id))
        async with asyncio.timeout(2):
            while not current.manager.session_deleting(child_id):
                await asyncio.sleep(.005)
    finally:
        release.set()
        result, = await asyncio.gather(setup, return_exceptions=True)
        if deleting is not None:
            assert await asyncio.wait_for(deleting, 3)
    assert isinstance(result, DelegationError) and result.status_code == 409
    assert len(refused) == 1 and not refused[0].accepted and refused[0].request_id == request_id
    rec = current.delegations.get_delegation(child_id)
    if previous is None:
        assert rec.work_id == work_id and rec.state == "cancelled"
    else:
        assert rec is previous
    assert await current.manager.source_receipt("delegation", f"agent-terminal:{work_id}") is None
    await current.delegations.confirm_recording()
    assert starts.empty() and not current.delegations.drain_blockers()


@pytest.mark.parametrize("busy_before_submit", [False, True], ids=["delete-after-cas", "busy-before-submit"])
async def test_deleted_unadmitted_followup_withdraws_only_its_original_parent_work_responsibility(
    runtime, gated_cli, monkeypatch, busy_before_submit,
):
    boot, starts = runtime
    current = await boot()
    await AgentManager(current.database).create_agent(
        name="Leaf", backend="codex", user_id=current.manager.get_session(current.parent_id).user_id,
    )
    outer = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="outer gate review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(context)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
    child = await current.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf", request="first nested review", scope=scope,
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, child)
    await current.delegations.confirm_recording()
    monkeypatch.setattr("server.delegations.delegation_manager", current.delegations)
    save = current.database.compare_and_set_delegation_state
    identity, submit = current.database.get_agent_identity, current.manager.start_message
    saved, release = asyncio.Event(), asyncio.Event()
    setup_work = None
    inputs = []

    async def after_cas(*args, **kwargs):
        nonlocal setup_work
        result = await save(*args, **kwargs)
        snapshot = json.loads(args[2])
        if args[0] == child.delegation_id and snapshot["state"] == "running":
            setup_work = snapshot["work_id"]
            saved.set()
            await release.wait()
        return result

    async def busy_parent_read(agent_id, *args, **kwargs):
        if setup_work is not None and agent_id == outer.target_agent_id:
            raise DatabaseBusy("Parent lookup before the original child input")
        return await identity(agent_id, *args, **kwargs)

    async def observe_submit(sid, prompt, **kwargs):
        if sid == child.delegation_id:
            inputs.append(kwargs["work_id"])
        return await submit(sid, prompt, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", after_cas)
    monkeypatch.setattr(current.manager, "start_message", observe_submit)
    if busy_before_submit:
        monkeypatch.setattr(current.database, "get_agent_identity", busy_parent_read)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=outer.delegation_id, delegation_id=child.delegation_id,
        request="unadmitted nested continuation", scope=scope,
    ))
    deleting = None
    try:
        await asyncio.wait_for(saved.wait(), 5)
        if not busy_before_submit:
            deleting = asyncio.create_task(current.manager.delete_session(child.delegation_id))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(deleting), 0.15)
        pending = await current.database.load_delegation_state(
            outer.delegation_id, parent_session_id=current.parent_id,
            user_id=current.manager.get_session(current.parent_id).user_id,
        )
        assert json.loads(pending)["child_work_ids"] == sorted([child.work_id, setup_work])
        assert current.delegations.get_delegation(child.delegation_id) is child
    finally:
        release.set()
        if deleting is not None:
            assert await asyncio.wait_for(deleting, 3)
    with pytest.raises(DelegationError) as refused:
        await following
    if busy_before_submit:
        assert refused.value.status_code == 503
        assert await current.manager.delete_session(child.delegation_id)
    parent = await current.database.load_delegation_state(
        outer.delegation_id, parent_session_id=current.parent_id,
        user_id=current.manager.get_session(current.parent_id).user_id,
    )
    assert setup_work not in json.loads(parent)["child_work_ids"]
    assert setup_work not in {row.work_id for row in current.delegations.list_delegations(outer.delegation_id)}
    assert current.delegations.get_delegation(child.delegation_id) is child
    assert not inputs and starts.empty()
    gated_cli.touch()
    await wait_finished(current.manager, outer)
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


async def test_changed_relationship_after_cas_does_not_use_deleted_setup_exit(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    save = current.database.compare_and_set_delegation_state
    saved, release = asyncio.Event(), asyncio.Event()

    async def after_cas(*args, **kwargs):
        result = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            saved.set()
            await release.wait()
        return result

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", after_cas)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held changed-relation review",
    ))
    try:
        await asyncio.wait_for(saved.wait(), 5)
        await current.database.update_session_field(original.delegation_id, origin="user")
        # Force a real pre-admission refusal after the relationship changes.
        await AgentManager(current.database).archive_agent(original.target_agent_id)
    finally:
        release.set()
    with pytest.raises(DelegationError, match="relationship changed"):
        await following
    rec = current.delegations.get_delegation(original.delegation_id)
    assert rec.work_id != original.work_id and rec.state == "running"
    assert current.delegations.drain_blockers()
    assert starts.empty()
    await current.delegations.confirm_recording()

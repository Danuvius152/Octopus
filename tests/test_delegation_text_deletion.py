"""Deleting a finished child is a normal loss of delivery material."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.db.base import DatabaseBusy
from server.delegations import DelegationError
from server.mcp_identity import work_is_open
from server.sessions.runtime_types import (
    SessionAdmissionClosed,
    SessionMailboxFull,
    SessionOwnerUnavailable,
)
from tests.delegation_runtime import notification_runtime as notification_runtime
from tests.delegation_runtime import (
    observe_terminal_deliveries,
    release_model,
    starts,
    wait_round_finished,
)


async def test_deleting_child_after_completion_does_not_poison_delegation_drain(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    database = runtime.manager.db
    read = database.read_session_result
    close = database.close
    reading, release = asyncio.Event(), asyncio.Event()
    observed_rows = []
    original = None

    async def close_original_test_database(*args, **kwargs):
        try:
            await close(*args, **kwargs)
        except BaseException:
            # A red assertion must retain the failed-command evidence. After
            # the public close hooks have attempted every original owner, only
            # release this fixture's SQLite worker so pytest can report it.
            # No command, hook, registry entry or result is repaired here.
            if database._conn is not None:
                await database._conn.close()
            raise

    async def hold_original_reply(session_id, request_id):
        if original is not None and request_id == original.request_id:
            reading.set()
            await release.wait()
        rows = await read(session_id, request_id)
        observed_rows.append((session_id, request_id, rows))
        return rows

    monkeypatch.setattr(database, "read_session_result", hold_original_reply)
    monkeypatch.setattr(database, "close", close_original_test_database)
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker",
        request="Complete this original request before deleting its conversation",
    )
    original = next(receipt for sid, work, receipt in runtime.receipts
                    if sid == rec.delegation_id and work == rec.work_id)
    try:
        await release_model(runtime, rec)
        await asyncio.wait_for(reading.wait(), 3)
        completion = await asyncio.wait_for(original.wait(), 2)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert completion.outcome.status == "success"
        assert rec.state == "completed"
        terminal = await database.get_committed_terminal(rec.delegation_id, original.request_id)
        assert terminal["status"] == "success"

        assert await asyncio.wait_for(runtime.manager.delete_session(rec.delegation_id), 2)
        assert runtime.manager.get_session(rec.delegation_id) is None
        release.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 3)

        assert len(observed_rows) == 1
        assert observed_rows[0][:2] == (rec.delegation_id, original.request_id)
        assert observed_rows[0][2].missing == "session_missing"
        assert rec.state == "completed"
        assert await original.wait() is completion
        assert completion.outcome.status == "success"
        assert len(runtime.starts) == 1
        replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
        assert len(replies) == 1
        assert replies[0]["summary"].startswith("[agent-error:")
        assert "reply_unavailable" in replies[0]["summary"]
        assert f"Original body from {rec.delegation_id}" not in replies[0]["summary"]
        assert len(deliveries) == 1
        parent_id, prompt, options, delivered = deliveries[0]
        assert parent_id == runtime.parent.id and delivered.accepted
        assert options["delivery_id"] == f"agent-terminal:{rec.work_id}"
        assert prompt == replies[0]["summary"]
        parent_rows = await database.load_messages(runtime.parent.id)
        assert not [row for row in parent_rows if row["role"] == "user"]
        print("deleted-child-completion", {
            "request_id": original.request_id, "business_result": completion.outcome.status,
            "round_state": rec.state, "delivery_result": "reply_unavailable", "model_spawns": len(runtime.starts),
        })
    finally:
        release.set()


async def test_deleted_leaf_reply_still_finishes_the_original_nested_parent(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    database = runtime.manager.db
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    await AgentManager(database).create_agent(name="Leaf worker", backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker",
        request="Delegate and wait for exactly one leaf delivery result",
    )
    async with asyncio.timeout(2):
        while not any(event.get("session_id") == outer.delegation_id
                      and event.get("type") == "assistant_text" for event in runtime.frames):
            await asyncio.sleep(.005)
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    leaf = await runtime.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf worker",
        request="Keep this leaf's acknowledged outcome after its text is deleted", scope=caller,
    )
    first = next(receipt for sid, work, receipt in runtime.receipts
                 if sid == outer.delegation_id and work == outer.work_id)
    original = next(receipt for sid, work, receipt in runtime.receipts
                    if sid == leaf.delegation_id and work == leaf.work_id)
    read = database.read_session_result
    reading, release = asyncio.Event(), asyncio.Event()
    observed_rows = []

    async def hold_leaf_reply(session_id, request_id):
        if session_id == leaf.delegation_id and request_id == original.request_id:
            reading.set()
            await release.wait()
        rows = await read(session_id, request_id)
        if session_id == leaf.delegation_id and request_id == original.request_id:
            observed_rows.append(rows)
        return rows

    monkeypatch.setattr(database, "read_session_result", hold_leaf_reply)
    try:
        await release_model(runtime, outer)
        first_completion = await asyncio.wait_for(first.wait(), 2)
        assert first_completion.outcome.status == "success"
        await runtime.manager.await_session_execution(outer.delegation_id, timeout=2)
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        saved = json.loads(await database.load_delegation_state(
            outer.delegation_id, parent_session_id=runtime.parent.id, user_id=runtime.parent.user_id,
        ))
        assert saved["candidate_request_id"] == first.request_id
        assert saved["child_work_ids"] == [leaf.work_id]
        assert outer.state == leaf.state == "running"
        assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []

        await release_model(runtime, leaf)
        await asyncio.wait_for(reading.wait(), 2)
        completion = await asyncio.wait_for(original.wait(), 2)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert completion.outcome.status == "success" and leaf.state == "completed"
        assert (await database.get_committed_terminal(leaf.delegation_id, original.request_id))["status"] == "success"
        assert await asyncio.wait_for(runtime.manager.delete_session(leaf.delegation_id), 2)
        release.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        assert len(observed_rows) == 1 and observed_rows[0].missing == "session_missing"
        leaf_deliveries = [item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{leaf.work_id}"]
        print("nested-deleted-leaf-handoff", {
            "leaf_outcome": completion.outcome.status, "leaf_state": leaf.state,
            "outer_state": outer.state, "leaf_delivery_count": len(leaf_deliveries),
            "original_outer_candidate": saved["candidate_request_id"],
        })
        assert len(leaf_deliveries) == 1
        parent_id, prompt, options, delivered = leaf_deliveries[0]
        assert parent_id == outer.delegation_id and options["work_id"] == outer.work_id
        assert delivered.accepted and prompt.startswith("[agent-error:")
        assert "reply_unavailable" in prompt
        assert f"Original body from {leaf.delegation_id}" not in prompt

        async with asyncio.timeout(2):
            while sum(scope.session_id == outer.delegation_id for scope, _ in runtime.starts) != 2:
                await asyncio.sleep(.005)
        await release_model(runtime, outer)
        await wait_round_finished(runtime, outer)
        assert outer.state == leaf.state == "completed"
        assert await original.wait() is completion
        assert completion.outcome.status == "success"
        assert await first.wait() is first_completion
        assert sum(scope.session_id == leaf.delegation_id for scope, _ in runtime.starts) == 1
        assert len(runtime.starts) == 3
        leaf_inputs = [row for row in await database.load_messages(outer.delegation_id)
                       if row["role"] == "user" and isinstance(row["content"], str)
                       and "reply_unavailable" in row["content"]]
        assert len(leaf_inputs) == 1 and leaf_inputs[0]["content"] == prompt
        root_returns = [item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]
        assert len(root_returns) == 1 and root_returns[0][0] == runtime.parent.id
        assert sum(item[2]["delivery_id"] == f"agent-terminal:{leaf.work_id}" for item in deliveries) == 1
        replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
        assert len(replies) == 1 and replies[0]["source"] == "delegation"
        assert f"Original body from {outer.delegation_id}" in replies[0]["summary"]
        assert f"Original body from {leaf.delegation_id}" not in replies[0]["summary"]
        print("nested-deleted-leaf-finished", {
            "leaf_outcome": completion.outcome.status, "outer_state": outer.state,
            "leaf_deliveries": len(leaf_deliveries), "root_deliveries": len(root_returns),
            "model_spawns": len(runtime.starts),
        })
    finally:
        release.set()


async def test_child_delete_waits_for_an_inflight_terminal_disposition_write(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    compare = db.compare_and_set_delegation_state
    writing, release = asyncio.Event(), asyncio.Event()
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Complete before deletion",
    )

    async def hold_disposition(session_id, expected, candidate, **options):
        if session_id == rec.delegation_id and json.loads(candidate)["terminal_disposition"] == "accepted":
            writing.set()
            await release.wait()
        return await compare(session_id, expected, candidate, **options)

    monkeypatch.setattr(db, "compare_and_set_delegation_state", hold_disposition)
    deleting = None
    try:
        await release_model(runtime, rec)
        await asyncio.wait_for(writing.wait(), 2)
        deleting = asyncio.create_task(runtime.manager.delete_session(rec.delegation_id))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(deleting), 0.15)
        assert await db.get_session_binding(rec.delegation_id) is not None
    finally:
        release.set()
        if deleting is not None:
            assert await asyncio.wait_for(deleting, 2)
    original = await runtime.manager.source_receipt("delegation", f"agent-terminal:{rec.work_id}", session_id=runtime.parent.id)
    assert original is not None and original[1].accepted
    assert rec.state == "completed" and rec.to_public_dict()["terminal_disposition"] == "accepted"
    await runtime.delegations.confirm_recording()
    assert not runtime.delegations.drain_blockers()


async def test_child_delete_uses_the_committed_result_when_its_notice_is_still_running(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    read = db.read_session_result
    deleted = asyncio.get_running_loop().create_future()
    allow_read = asyncio.Event()
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Delete from the committed result notice",
    )
    original = next(receipt for sid, work, receipt in runtime.receipts if work == rec.work_id)

    async def read_after_delete(sid, request_id):
        if sid == rec.delegation_id and request_id == original.request_id:
            await allow_read.wait()
        return await read(sid, request_id)

    async def delete_from_result(event):
        if event["type"] == "result" and event["session_id"] == rec.delegation_id:
            try:
                assert not original._completion.done()
                assert (await db.get_committed_terminal(rec.delegation_id, original.request_id))["status"] == "success"
                deleted.set_result(await asyncio.wait_for(runtime.manager.delete_session(rec.delegation_id), 2))
            except Exception as error:
                deleted.set_exception(error)

    monkeypatch.setattr(db, "read_session_result", read_after_delete)
    runtime.manager.on_broadcast("delete-from-result-notice", delete_from_result, required=True)
    try:
        await release_model(runtime, rec)
        assert await asyncio.wait_for(deleted, 3)
        assert rec.state == "completed" and await db.get_session_binding(rec.delegation_id) is None
    finally:
        allow_read.set()
        runtime.manager.remove_broadcast("delete-from-result-notice")
    completion = await asyncio.wait_for(original.wait(), 2)
    assert completion.outcome.status == "success"
    await runtime.delegations.confirm_recording()
    queued = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(queued) == 1 and "reply_unavailable" in queued[0]["summary"]
    assert not runtime.delegations.drain_blockers()


async def test_delete_between_delegated_parent_and_leaf_preserves_original_return(
    notification_runtime, monkeypatch, short_process_shutdown,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    agents = AgentManager(db)
    for name in ("Middle worker", "Leaf worker"):
        await agents.create_agent(name=name, backend="codex", user_id=runtime.parent.user_id)

    async def scope_for(rec):
        async with asyncio.timeout(2):
            while not any(event.get("session_id") == rec.delegation_id and event.get("type") == "assistant_text"
                          for event in runtime.frames):
                await asyncio.sleep(.005)
        return next(scope for scope, _ in runtime.starts if scope.session_id == rec.delegation_id)

    a = await runtime.delegations.start_delegation(parent_session_id=runtime.parent.id, agent_name="Notification worker",
                                                  request="Finish after the middle worker's original return")
    b = await runtime.delegations.start_delegation(parent_session_id=a.delegation_id, agent_name="Middle worker",
                                                  request="Wait for the leaf", scope=await scope_for(a))
    c = await runtime.delegations.start_delegation(parent_session_id=b.delegation_id, agent_name="Leaf worker",
                                                  request="Stay running until retired", scope=await scope_for(b))
    await scope_for(c)
    first_a, first_b, first_c = (next(receipt for sid, work, receipt in runtime.receipts if work == rec.work_id)
                                 for rec in (a, b, c))
    await release_model(runtime, b)
    finished_b = await asyncio.wait_for(first_b.wait(), 2)
    await release_model(runtime, a)
    assert (await asyncio.wait_for(first_a.wait(), 2)).outcome.status == "success"
    await runtime.delegations.confirm_recording()
    saved_b = json.loads(await db.load_delegation_state(b.delegation_id, parent_session_id=a.delegation_id,
                                                      user_id=runtime.parent.user_id))
    assert saved_b["candidate_request_id"] == first_b.request_id and saved_b["child_work_ids"] == [c.work_id]
    assert finished_b.outcome.status == "success" and all(rec.state == "running" for rec in (a, b, c))

    delivery_id = f"agent-terminal:{b.work_id}"
    submit = runtime.manager.start_message
    capacity, allow_return = asyncio.Event(), asyncio.Event()

    async def capacity_barrier(sid, prompt, **kwargs):
        if sid == a.delegation_id and kwargs.get("delivery_id") == delivery_id and not allow_return.is_set():
            capacity.set()
            raise SessionMailboxFull("Hold the original parent's return admission")
        return await submit(sid, prompt, **kwargs)

    monkeypatch.setattr(runtime.manager, "start_message", capacity_barrier)
    try:
        assert await asyncio.wait_for(runtime.manager.delete_session(b.delegation_id), 2)
        await asyncio.wait_for(capacity.wait(), 2)
        assert await db.get_session_binding(b.delegation_id) is None
        assert await db.get_session_binding(c.delegation_id) is not None
        assert not any(work_is_open(rec.work_id) or runtime.manager.work_has_resources(rec.work_id) for rec in (b, c))
        assert (await asyncio.wait_for(first_c.wait(), 2)).outcome.status != "success"
        assert b.state == "cancelled" and c.state != "running" and a.state == "running"
        assert await first_b.wait() is finished_b and finished_b.outcome.status == "success"
        saved_a = json.loads(await db.load_delegation_state(a.delegation_id, parent_session_id=runtime.parent.id,
                                                          user_id=runtime.parent.user_id))
        assert saved_a["child_work_ids"] == [b.work_id]
        assert await runtime.manager.source_receipt("delegation", delivery_id, session_id=a.delegation_id) is None
    finally:
        allow_return.set()

    async with asyncio.timeout(3):
        while sum(scope.session_id == a.delegation_id for scope, _ in runtime.starts) != 2:
            await asyncio.sleep(.005)
    row = await db.get_source_submission("delegation", delivery_id, session_id=a.delegation_id)
    assert row["receipt"]["accepted"] and row["payload"]["work_id"] == a.work_id
    await release_model(runtime, a)
    await wait_round_finished(runtime, a)
    assert a.state == "completed"
    root_return = await runtime.manager.source_receipt("delegation", f"agent-terminal:{a.work_id}", session_id=runtime.parent.id)
    assert root_return is not None and root_return[1].accepted
    assert not runtime.delegations.drain_blockers()


@pytest.mark.parametrize("delete_parent", [False, True], ids=["delete-refused-child", "delete-return-source"])
async def test_rollback_can_seal_the_parent_without_making_delete_wait_for_its_reply(
    notification_runtime, monkeypatch, delete_parent,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    await AgentManager(db).create_agent(name="Refused leaf", backend="codex", user_id=runtime.parent.user_id)
    parent = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Wait for child setup",
    )
    await starts(runtime, parent.delegation_id, 1)
    scope = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    submit, read = runtime.manager.start_message, db.read_session_result
    child = asyncio.get_running_loop().create_future()
    refuse, reading, release_read = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pause_child_input(sid, content, **kwargs):
        if sid not in {runtime.parent.id, parent.delegation_id} and not kwargs.get("delivery_id"):
            child.set_result(sid)
            await refuse.wait()
            raise SessionAdmissionClosed("Child was refused before admission")
        return await submit(sid, content, **kwargs)

    async def pause_parent_body(sid, request_id):
        if sid == parent.delegation_id:
            reading.set()
            await release_read.wait()
        return await read(sid, request_id)

    monkeypatch.setattr(runtime.manager, "start_message", pause_child_input)
    monkeypatch.setattr(db, "read_session_result", pause_parent_body)
    asking = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Refused leaf", request="Refuse this input", scope=scope,
    ))
    deleting = None
    try:
        child_id = await asyncio.wait_for(child, 2)
        await release_model(runtime, parent)
        async with asyncio.timeout(2):
            while parent._candidate_request_id is None:
                await asyncio.sleep(.005)
        assert parent.state == "running"
        session_id = parent.delegation_id if delete_parent else child_id
        if delete_parent:
            deleting = asyncio.create_task(runtime.manager.delete_session(session_id))
            async with asyncio.timeout(2):
                while not runtime.manager.session_deleting(session_id):
                    await asyncio.sleep(.005)
        refuse.set()
        await asyncio.wait_for(reading.wait(), 2)
        if deleting is None:
            deleting = asyncio.create_task(runtime.manager.delete_session(session_id))
        assert await asyncio.wait_for(asyncio.shield(deleting), 2)
        assert await db.get_session_binding(session_id) is None and parent.state == "completed"
    finally:
        refuse.set()
        release_read.set()
        await asyncio.gather(asking, return_exceptions=True)
        if deleting is not None:
            await deleting
    with pytest.raises(DelegationError, match="refused"):
        await asking
    await runtime.delegations.confirm_recording()
    queued = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(queued) == 1 and ("reply_unavailable" in queued[0]["summary"]) == delete_parent
    assert not runtime.delegations.drain_blockers()


async def test_delete_reobserves_an_already_retired_childs_unconfirmed_parent_return(
    notification_runtime, monkeypatch, short_process_shutdown,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Cancel while the original model runs",
    )
    await starts(runtime, rec.delegation_id, 1)
    original_id = f"agent-terminal:{rec.work_id}"
    submit, observe = runtime.manager.start_message, runtime.manager.source_receipt
    accepted = []
    unreadable = True

    async def lose_return_ack(sid, content, **kwargs):
        receipt = await submit(sid, content, **kwargs)
        if kwargs.get("delivery_id") == original_id:
            assert receipt.accepted
            accepted.append(receipt.request_id)
            raise TimeoutError("Original return reply was lost")
        return receipt

    async def observe_return(source, delivery_id, **kwargs):
        if delivery_id == original_id and unreadable and accepted:
            raise SessionOwnerUnavailable("Original return temporarily unreadable")
        return await observe(source, delivery_id, **kwargs)

    monkeypatch.setattr(runtime.manager, "start_message", lose_return_ack)
    monkeypatch.setattr(runtime.manager, "source_receipt", observe_return)
    await runtime.delegations.cancel_delegation(rec.delegation_id)
    assert rec.to_public_dict()["terminal_disposition"] == "unknown" and len(accepted) == 1
    unreadable = False
    assert await runtime.manager.delete_session(rec.delegation_id)
    await runtime.delegations.confirm_recording()
    outcome = await observe("delegation", original_id, session_id=runtime.parent.id)
    assert outcome is not None and accepted == [outcome[1].request_id]
    assert rec.to_public_dict()["terminal_disposition"] == "duplicate"
    assert not runtime.delegations.drain_blockers()


@pytest.mark.parametrize("delete_parent", [False, True], ids=["original-child", "parent-of-finished-child"])
async def test_child_delete_restarts_the_original_completion_after_busy_classification(
    notification_runtime, monkeypatch, short_process_shutdown, delete_parent,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    consumption = db.delegation_work_consumption
    classify = asyncio.Event()
    attempts = []
    parent = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Wait for work to complete",
    )
    rec = parent
    if delete_parent:
        await AgentManager(db).create_agent(name="Leaf worker", backend="codex", user_id=runtime.parent.user_id)
        await starts(runtime, parent.delegation_id, 1)
        scope = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
        rec = await runtime.delegations.start_delegation(
            parent_session_id=parent.delegation_id, agent_name="Leaf worker", request="Complete before the busy read", scope=scope,
        )
    original = next(receipt for sid, work, receipt in runtime.receipts if work == rec.work_id)

    async def busy_first_read(sid, work, children, candidate):
        if sid == rec.delegation_id:
            attempts.append((work, candidate))
            if len(attempts) == 1:
                classify.set()
                raise DatabaseBusy("Original completion classification could not acquire SQLite")
        return await consumption(sid, work, children, candidate)

    monkeypatch.setattr(db, "delegation_work_consumption", busy_first_read)
    await release_model(runtime, rec)
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
    await asyncio.wait_for(classify.wait(), 2)
    assert rec.state == "running" and db.health.cause is None
    assert await runtime.manager.delete_session(parent.delegation_id)
    await runtime.delegations.confirm_recording()
    assert rec.state == "completed" and attempts == [(rec.work_id, original.request_id)] * 2
    reply = await runtime.manager.source_receipt("delegation", f"agent-terminal:{parent.work_id}", session_id=runtime.parent.id)
    assert reply is not None and reply[1].accepted
    if delete_parent:
        assert await db.get_session_binding(rec.delegation_id) is not None
        assert (await db.get_committed_terminal(rec.delegation_id, original.request_id))["status"] == "success"
    assert len(runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]) == 1
    assert not runtime.delegations.drain_blockers()


async def test_parent_delete_joins_leaf_completion_registered_while_waiting_on_a_sibling(
    notification_runtime, monkeypatch, short_process_shutdown,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db, delegations = runtime.manager.db, runtime.delegations
    await AgentManager(db).create_agent(name="Leaf worker", backend="codex", user_id=runtime.parent.user_id)
    parent = await delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Wait for two leaf replies",
    )
    await starts(runtime, parent.delegation_id, 1)
    scope = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    first, second = [await delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Leaf worker", request=request, scope=scope,
    ) for request in ("First sibling", "Second sibling")]
    await starts(runtime, second.delegation_id, 1)
    originals = {work: receipt for _, work, receipt in runtime.receipts if work in {first.work_id, second.work_id}}
    consumption, join = db.delegation_work_consumption, delegations._join_round_completions
    classifying, release_first, joining, second_busy = (asyncio.Event() for _ in range(4))
    second_reads = []

    async def control_reads(sid, work, children, candidate):
        if work == first.work_id:
            classifying.set()
            await release_first.wait()
        elif work == second.work_id:
            second_reads.append(candidate)
            if len(second_reads) == 1:
                second_busy.set()
                raise DatabaseBusy("Sibling completion could not acquire SQLite")
        return await consumption(sid, work, children, candidate)

    async def observe_join(work_ids):
        if first.work_id in work_ids:
            joining.set()
        await join(work_ids)

    monkeypatch.setattr(db, "delegation_work_consumption", control_reads)
    monkeypatch.setattr(delegations, "_join_round_completions", observe_join)
    deleting = None
    try:
        await release_model(runtime, first)
        await asyncio.wait_for(classifying.wait(), 2)
        deleting = asyncio.create_task(runtime.manager.delete_session(parent.delegation_id))
        await asyncio.wait_for(joining.wait(), 2)
        await release_model(runtime, second)
        await asyncio.wait_for(second_busy.wait(), 2)
        assert not deleting.done() and first.state == second.state == "running"
    finally:
        release_first.set()
        if deleting is not None:
            result, = await asyncio.gather(deleting, return_exceptions=True)
            if isinstance(result, Exception) and isinstance(result.__cause__, DatabaseBusy):
                # Let the original fixture retire the sessions on a red run.
                assert await runtime.manager.delete_session(parent.delegation_id)
    assert result is True
    await delegations.confirm_recording()
    for rec in (first, second):
        assert rec.state == "completed"
        binding = await db.get_session_binding(rec.delegation_id)
        assert binding is not None
        assert (await db.get_committed_terminal(rec.delegation_id, originals[rec.work_id].request_id))["status"] == "success"
    assert second_reads == [originals[second.work_id].request_id] * 2
    assert not delegations.drain_blockers()


@pytest.mark.parametrize("late_ack_loss", [False, True], ids=["already-unknown", "unknown-during-delete"])
async def test_parent_delete_reobserves_a_childs_unconfirmed_return_before_removing_receipts(
    notification_runtime, monkeypatch, short_process_shutdown, late_ack_loss,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    monkeypatch.setattr("server.delegations.delegation_manager", runtime.delegations)
    db = runtime.manager.db
    await AgentManager(db).create_agent(name="Leaf worker", backend="codex", user_id=runtime.parent.user_id)
    parent = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Wait for leaf reply",
    )
    await starts(runtime, parent.delegation_id, 1)
    scope = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    leaf = await runtime.delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Leaf worker", request="Reply once", scope=scope,
    )
    original_id = f"agent-terminal:{leaf.work_id}"
    submit, observe = runtime.manager.start_message, runtime.manager.source_receipt
    accepted, deletion_observations = [], []
    block_reads = True
    sent, lose_ack = asyncio.Event(), asyncio.Event()

    async def lose_return_ack(sid, content, **kwargs):
        receipt = await submit(sid, content, **kwargs)
        if kwargs.get("delivery_id") == original_id:
            assert receipt.accepted
            accepted.append(receipt.request_id)
            sent.set()
            if late_ack_loss:
                await lose_ack.wait()
            raise TimeoutError("Original leaf return reply was lost")
        return receipt

    async def observe_return(source, delivery_id, **kwargs):
        if delivery_id == original_id and accepted:
            if runtime.manager.session_deleting(parent.delegation_id):
                deletion_observations.append((delivery_id, kwargs["session_id"]))
            if block_reads:
                raise SessionOwnerUnavailable("Original leaf return temporarily unreadable")
        return await observe(source, delivery_id, **kwargs)

    monkeypatch.setattr(runtime.manager, "start_message", lose_return_ack)
    monkeypatch.setattr(runtime.manager, "source_receipt", observe_return)
    cancellation = None
    deleting = None
    try:
        if late_ack_loss:
            cancellation = asyncio.create_task(runtime.delegations.cancel_delegation(leaf.delegation_id))
            await asyncio.wait_for(sent.wait(), 2)
            assert leaf.to_public_dict()["terminal_disposition"] is None
            deleting = asyncio.create_task(runtime.manager.delete_session(parent.delegation_id))
            async with asyncio.timeout(2):
                while not runtime.manager.session_deleting(parent.delegation_id):
                    await asyncio.sleep(.005)
            assert not deleting.done()
        else:
            await release_model(runtime, leaf)
            await wait_round_finished(runtime, leaf)
            assert leaf.state == "completed" and leaf.to_public_dict()["terminal_disposition"] == "unknown"
            deleting = asyncio.create_task(runtime.manager.delete_session(parent.delegation_id))
        row = await db.get_source_submission("delegation", original_id, session_id=parent.delegation_id)
        assert row["receipt"]["accepted"] and accepted == [row["request_id"]]
        lose_ack.set()
        with pytest.raises(SessionOwnerUnavailable, match="temporarily unreadable"):
            await asyncio.wait_for(deleting, 3)
        assert await db.get_session_binding(parent.delegation_id) is not None
        retained = await db.get_source_submission("delegation", original_id, session_id=parent.delegation_id)
        assert retained["request_id"] == row["request_id"] and retained["receipt"] == row["receipt"]
        assert retained["execution_state"] == "withdrawn"
    finally:
        lose_ack.set()
        if cancellation is not None:
            await cancellation
        if deleting is not None:
            await asyncio.gather(deleting, return_exceptions=True)
        block_reads = False
        assert await runtime.manager.delete_session(parent.delegation_id)
    await runtime.delegations.confirm_recording()
    assert deletion_observations == [(original_id, parent.delegation_id)] * (3 if late_ack_loss else 2)
    assert len(accepted) == 1
    assert leaf.to_public_dict()["terminal_disposition"] == "duplicate"
    assert await db.get_session_binding(leaf.delegation_id) is not None
    assert not runtime.delegations.drain_blockers()

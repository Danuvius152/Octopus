"""Deleting a finished child is a normal loss of delivery material."""

from __future__ import annotations

import asyncio
import json

from server.agent_manager import AgentManager
from tests import test_delegation_notification_failure as notification_tests

notification_runtime = notification_tests.notification_runtime
release_model = notification_tests.release_model
wait_round_finished = notification_tests.wait_round_finished


def observe_terminal_deliveries(runtime, monkeypatch):
    """Record actual public submissions without replacing any receipt."""
    submit = runtime.manager.start_message
    delivered = []

    async def observe(session_id, content, **kwargs):
        receipt = await submit(session_id, content, **kwargs)
        delivery_id = kwargs.get("delivery_id", "")
        if delivery_id.startswith("agent-terminal:"):
            delivered.append((session_id, content, kwargs, receipt))
        return receipt

    monkeypatch.setattr(runtime.manager, "start_message", observe)
    return delivered


async def test_deleting_child_after_completion_does_not_poison_delegation_drain(
    notification_runtime, monkeypatch,
):
    runtime = notification_runtime
    runtime.controls.enabled = False
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    database = runtime.manager.db
    read = database.load_committed_request_messages
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

    monkeypatch.setattr(database, "load_committed_request_messages", hold_original_reply)
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

        assert await runtime.manager.delete_session(rec.delegation_id)
        assert runtime.manager.get_session(rec.delegation_id) is None
        release.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 3)

        assert observed_rows == [(rec.delegation_id, original.request_id, [])]
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
    read = database.load_committed_request_messages
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

    monkeypatch.setattr(database, "load_committed_request_messages", hold_leaf_reply)
    try:
        await release_model(runtime, outer)
        first_completion = await asyncio.wait_for(first.wait(), 2)
        assert first_completion.outcome.status == "success"
        await runtime.manager.await_session_execution(outer.delegation_id, timeout=2)
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        saved = json.loads(await database.load_delegation_state(
            outer.delegation_id, parent_session_id=runtime.parent.id, user_id=runtime.parent.user_id,
        ))
        assert saved["deferred_request_id"] == first.request_id
        assert outer.state == leaf.state == "running"
        assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []

        await release_model(runtime, leaf)
        await asyncio.wait_for(reading.wait(), 2)
        completion = await asyncio.wait_for(original.wait(), 2)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert completion.outcome.status == "success" and leaf.state == "completed"
        assert (await database.get_committed_terminal(leaf.delegation_id, original.request_id))["status"] == "success"
        assert await runtime.manager.delete_session(leaf.delegation_id)
        release.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        assert observed_rows == [[]]
        leaf_deliveries = [item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{leaf.work_id}"]
        print("nested-deleted-leaf-handoff", {
            "leaf_outcome": completion.outcome.status, "leaf_state": leaf.state,
            "outer_state": outer.state, "leaf_delivery_count": len(leaf_deliveries),
            "original_outer_deferred": saved["deferred_request_id"],
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

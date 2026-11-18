"""Confirmed initial refusal retires its exact work after relationship deletion."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import close_work, work_is_open
from server.sessions import client_submissions
from server.sessions.runtime_types import SessionOwnerUnavailable
from tests.delegation_runtime import notification_runtime as notification_runtime
from tests.delegation_runtime import (
    observe_terminal_deliveries,
    release_model,
    starts,
    wait_round_finished,
)


@pytest.fixture
async def file_check(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    loop = asyncio.get_running_loop()
    checking = asyncio.Queue()
    release = threading.Event()
    gate = SimpleNamespace(checking=checking, release=release, enabled=True)
    files = client_submissions._files
    database = runtime.manager.db
    close = database.close

    async def close_after_reporting(*args, **kwargs):
        try:
            await close(*args, **kwargs)
        except BaseException as error:
            # The archive fault can leave teardown explicitly unconfirmed.
            # Release the test's isolated connection after observing that fact.
            if database._conn is not None:
                await database._conn.close()
            failure = runtime.controls.archive_error
            if failure is None:
                raise
            assert isinstance(error, RuntimeError) and str(error) == "Database teardown remains unconfirmed; connection kept open"
            cause = error.__cause__
            while cause is not None and cause is not failure:
                cause = cause.__cause__
            assert cause is failure

    def hold_files(session_id, identifiers, *, user_id):
        if gate.enabled and session_id != runtime.parent.id:
            loop.call_soon_threadsafe(checking.put_nowait, session_id)
            assert release.wait(5), "The test must release its original file check"
        return files(session_id, identifiers, user_id=user_id)

    monkeypatch.setattr(client_submissions, "_files", hold_files)
    monkeypatch.setattr(database, "close", close_after_reporting)
    try:
        yield gate
    finally:
        release.set()


async def test_deleted_initial_child_during_file_check_has_no_running_work(notification_runtime, file_check, monkeypatch):
    runtime = notification_runtime
    checking, release = file_check.checking, file_check.release
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Never start the deleted child",
    ))
    try:
        child_id = await asyncio.wait_for(checking.get(), 2)
        rec = runtime.delegations.get_delegation(child_id)
        assert rec.state == "running" and work_is_open(rec.work_id)
        child = runtime.manager.get_session(child_id)
        assert await runtime.manager.delete_session(child_id)
        owner = runtime.manager._session_owners.get(id(child))
        if owner is not None:
            await asyncio.wait_for(asyncio.shield(owner.task), 2)
        session_owner = runtime.manager._session_owner

        def no_retired_owner(session):
            assert session is not child, "The removed child has no new admission owner."
            return session_owner(session)

        monkeypatch.setattr(runtime.manager, "_session_owner", no_retired_owner)
        release.set()
        with pytest.raises(DelegationError):
            await starting
        assert rec.state == "cancelled" and rec.finished_at is not None
        assert not work_is_open(rec.work_id)
        assert not runtime.delegations.has_active_delegation_for_parent(runtime.parent.id)
        assert rec.work_id not in {item.work_id for item in runtime.delegations.list_delegations(runtime.parent.id)}
        assert runtime.starts == []
        assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
        await runtime.delegations.confirm_recording()
        assert not runtime.delegations.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("follow_up", [False, True], ids=["initial", "follow-up"])
async def test_unadmitted_cleanup_preserves_a_reopened_child(notification_runtime, file_check, follow_up):
    runtime = notification_runtime
    previous = None
    if follow_up:
        file_check.enabled = False
        previous = await runtime.delegations.start_delegation(
            parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Establish the original child",
        )
        await release_model(runtime, previous)
        await wait_round_finished(runtime, previous)
        await runtime.manager.archive_session(previous.delegation_id, replace=False)
        file_check.enabled = True
        submitting = runtime.delegations.follow_up_delegation(
            parent_session_id=runtime.parent.id, delegation_id=previous.delegation_id, request="Do not admit this follow-up",
        )
    else:
        submitting = runtime.delegations.start_delegation(
            parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Do not admit this initial request",
        )
    starting = asyncio.create_task(submitting)
    try:
        child_id = await asyncio.wait_for(file_check.checking.get(), 2)
        rec = runtime.delegations.get_delegation(child_id)
        original = runtime.manager.get_session(child_id)
        assert original is not None and rec.state == "running" and work_is_open(rec.work_id)
        await runtime.manager.archive_session(child_id, replace=False)
        reopened = await runtime.manager.unarchive_session(child_id)
        assert reopened is not original
        file_check.release.set()
        with pytest.raises(DelegationError):
            await asyncio.wait_for(starting, 2)
        assert not work_is_open(rec.work_id)
        assert not runtime.delegations.has_active_delegation_for_parent(runtime.parent.id)
        assert all(scope.work_id != rec.work_id for scope, _ in runtime.starts)
        if previous is None:
            assert rec.state == "cancelled"
        else:
            assert runtime.delegations.get_delegation(child_id) is previous
        assert runtime.manager.get_session(child_id) is reopened
        assert not (await runtime.manager.db.get_session_binding(child_id))["archived"]
    finally:
        file_check.release.set()
        await asyncio.gather(starting, return_exceptions=True)


async def test_closed_initial_receipt_still_retires_the_existing_relationship(notification_runtime, file_check):
    runtime = notification_runtime
    checking, release = file_check.checking, file_check.release
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Refuse through the original receipt",
    ))
    try:
        child_id = await asyncio.wait_for(checking.get(), 2)
        runtime.manager.close_admission()
        release.set()
        rec = await asyncio.wait_for(starting, 2)
        assert rec.delegation_id == child_id and rec.state == "cancelled"
        assert not work_is_open(rec.work_id)
        assert runtime.manager.get_session(child_id) is None
        assert (await runtime.manager.db.get_session_binding(child_id))["archived"]
        assert not runtime.delegations.has_active_delegation_for_parent(runtime.parent.id)
        assert runtime.starts == []
        await runtime.delegations.confirm_recording()
        assert not runtime.delegations.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize("exit_kind", ["deleted", "refused", "closed-receipt", "archive-write-failed", "archive-notice-failed"])
async def test_unadmitted_initial_descendant_releases_the_successful_outer_once(
    notification_runtime, file_check, monkeypatch, depth, exit_kind,
):
    runtime = notification_runtime
    file_check.enabled = False
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    for name in ("Middle coordinator", "Never admitted leaf"):
        await AgentManager(runtime.manager.db).create_agent(name=name, backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Finish only after owned setup exits",
    )
    await starts(runtime, outer.delegation_id, 1)
    chain = [outer]
    if depth == 2:
        caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
        middle = await runtime.delegations.start_delegation(
            parent_session_id=outer.delegation_id, agent_name="Middle coordinator", request="Coordinate the last leaf", scope=caller,
        )
        await starts(runtime, middle.delegation_id, 1)
        chain.append(middle)
    parent = chain[-1]
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    file_check.enabled = True
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Never admitted leaf", request="Deleted during original file check", scope=caller,
    ))
    try:
        child_id = await asyncio.wait_for(file_check.checking.get(), 2)
        child = runtime.delegations.get_delegation(child_id)
        file_check.enabled = False
        for rec in chain:
            original = next(receipt for sid, work, receipt in runtime.receipts
                            if sid == rec.delegation_id and work == rec.work_id)
            await release_model(runtime, rec)
            completion = await asyncio.wait_for(original.wait(), 2)
            assert completion.outcome.status == "success"
            assert rec.state == "running", "Model output alone cannot erase the owned setup"
        assert not deliveries
        if exit_kind == "deleted":
            assert await runtime.manager.delete_session(child_id)
        elif exit_kind == "refused" or exit_kind.startswith("archive-"):
            if exit_kind == "refused":
                await AgentManager(runtime.manager.db).archive_agent(child.target_agent_id)
            else:
                # Keep the session row live until the exact setup cleanup
                # archives it, while rejecting new work through its agent.
                await runtime.manager.db.update_agent(child.target_agent_id, archived=True)
            if exit_kind == "archive-write-failed":
                failure = RuntimeError("Original archive write failed")
                runtime.controls.archive_error = failure
                update = runtime.manager.db.update_session_field

                async def refuse_archive(session_id, **fields):
                    if session_id == child_id and fields.get("archived") is True:
                        raise failure
                    return await update(session_id, **fields)

                monkeypatch.setattr(runtime.manager.db, "update_session_field", refuse_archive)
            if exit_kind == "archive-notice-failed":
                failure = RuntimeError("Original archive notice failed")
                runtime.controls.archive_error = failure

                async def refuse_archive_notice(event):
                    if event.get("type") == "session_archived" and event.get("old_session_id") == child_id:
                        raise failure

                runtime.manager.on_broadcast("refuse-initial-leaf-archive", refuse_archive_notice, required=True)
        else:
            close_work(child.work_id)
        file_check.release.set()
        if exit_kind == "closed-receipt":
            assert await asyncio.wait_for(starting, 2) is child
        elif exit_kind.startswith("archive-"):
            with pytest.raises(RuntimeError, match="Original archive (write|notice) failed"):
                await asyncio.wait_for(starting, 2)
        else:
            with pytest.raises(DelegationError):
                await asyncio.wait_for(starting, 2)
        if exit_kind == "closed-receipt":
            await starts(runtime, parent.delegation_id, 2)
            assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
            await release_model(runtime, parent)
        if depth == 2:
            await starts(runtime, outer.delegation_id, 2)
            await release_model(runtime, outer)
        await wait_round_finished(runtime, outer)
        assert child.state == "cancelled" and not work_is_open(child.work_id)
        assert all(rec.state == "completed" for rec in chain)
        for rec in chain:
            assert len([item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{rec.work_id}"]) == 1
            assert not runtime.delegations.has_active_delegation_for_parent(rec.delegation_id)
        assert not any(scope.session_id == child_id for scope, _ in runtime.starts)
        assert len(runtime.starts) == depth * 2 - (exit_kind != "closed-receipt")
        if exit_kind != "deleted":
            assert bool((await runtime.manager.db.get_session_binding(child_id))["archived"]) == (exit_kind != "archive-write-failed")
            assert runtime.delegations.get_delegation(child_id) is child
        queued = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
        assert len(queued) == 1 and f"Original body from {outer.delegation_id}" in queued[0]["summary"]
        await runtime.confirm_recording()
        assert bool(runtime.delegations.drain_blockers()) is (runtime.controls.archive_error is not None)
    finally:
        file_check.release.set()
        runtime.manager.remove_broadcast("refuse-initial-leaf-archive")
        await asyncio.gather(starting, return_exceptions=True)


async def test_changed_initial_relationship_keeps_its_original_unsettled_record(notification_runtime, file_check):
    runtime = notification_runtime
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Do not confuse replacement with deletion",
    ))
    try:
        child_id = await asyncio.wait_for(file_check.checking.get(), 2)
        rec = runtime.delegations.get_delegation(child_id)
        await runtime.manager.db.update_session_field(child_id, origin="user")
        await AgentManager(runtime.manager.db).archive_agent(rec.target_agent_id)
        file_check.release.set()
        with pytest.raises(DelegationError, match="relationship changed"):
            await asyncio.wait_for(starting, 2)
        assert runtime.delegations.get_delegation(child_id) is rec
        assert rec.state == "running" and work_is_open(rec.work_id)
        assert runtime.manager.get_session(child_id) is not None
        assert runtime.starts == []
        await runtime.delegations.confirm_recording()
    finally:
        file_check.release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize("admitted", [False, True], ids=["no-terminal-input", "lost-terminal-ack"])
async def test_unknown_initial_terminal_preserves_parent_responsibility_until_original_consumption(
    notification_runtime, file_check, monkeypatch, depth, admitted,
):
    runtime = notification_runtime
    file_check.enabled = False
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    for name in ("Middle coordinator", "Unaccepted leaf"):
        await AgentManager(runtime.manager.db).create_agent(name=name, backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Wait for original return consumption",
    )
    await starts(runtime, outer.delegation_id, 1)
    chain = [outer]
    if depth == 2:
        caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
        middle = await runtime.delegations.start_delegation(
            parent_session_id=outer.delegation_id, agent_name="Middle coordinator", request="Own the original leaf return", scope=caller,
        )
        await starts(runtime, middle.delegation_id, 1)
        chain.append(middle)
    parent = chain[-1]
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    file_check.enabled = True
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Unaccepted leaf", request="Never admit this leaf", scope=caller,
    ))
    try:
        child_id = await asyncio.wait_for(file_check.checking.get(), 2)
        child = runtime.delegations.get_delegation(child_id)
        file_check.enabled = False
        for rec in chain:
            original = next(receipt for sid, work, receipt in runtime.receipts
                            if sid == rec.delegation_id and work == rec.work_id)
            await release_model(runtime, rec)
            assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
        async with asyncio.timeout(2):
            while any(rec._candidate_request_id is None for rec in chain):
                await asyncio.sleep(.005)
        assert all(rec.state == "running" for rec in chain)
        lookup, submit = runtime.manager.source_receipt, runtime.manager.start_message
        delivery_id = f"agent-terminal:{child.work_id}"
        submissions = []
        readable = False

        async def unavailable_original_receipt(source, original_id, **kwargs):
            if original_id == delivery_id and not readable and (not admitted or submissions):
                raise SessionOwnerUnavailable("Original leaf return receipt is temporarily unreadable")
            return await lookup(source, original_id, **kwargs)

        async def lose_original_ack(session_id, prompt, **kwargs):
            receipt = await submit(session_id, prompt, **kwargs)
            if kwargs.get("delivery_id") == delivery_id:
                submissions.append(receipt.request_id)
                raise TimeoutError("Original leaf return acknowledgment lost")
            return receipt

        monkeypatch.setattr(runtime.manager, "source_receipt", unavailable_original_receipt)
        monkeypatch.setattr(runtime.manager, "start_message", lose_original_ack)
        close_work(child.work_id)
        file_check.release.set()
        assert await asyncio.wait_for(starting, 2) is child
        await runtime.delegations.confirm_recording()
        row = await runtime.manager.db.get_source_submission("delegation", delivery_id, session_id=parent.delegation_id)
        assert (row is not None) == admitted
        assert child._terminal_disposition == "unknown" and child.state == "cancelled"
        assert not work_is_open(child.work_id)
        assert not any(scope.session_id == child_id for scope, _ in runtime.starts)
        assert all(rec.state == "running" for rec in chain)
        assert parent._child_work_ids == {child.work_id}
        assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
        assert not [item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]
        # The existing explicit cancellation can observe this same terminal.
        # It cannot submit again when the original receipt still does not exist.
        readable = True
        await runtime.delegations.cancel_delegation(child_id)
        await runtime.delegations.confirm_recording()
        assert parent._child_work_ids == {child.work_id}
        if not admitted:
            assert child._terminal_disposition == "unknown" and not submissions
            assert not deliveries
            assert all(rec.state == "running" for rec in chain)
        else:
            assert child._terminal_disposition == "duplicate" and len(submissions) == 1
            await starts(runtime, parent.delegation_id, 2)
            await release_model(runtime, parent)
            if depth == 2:
                await starts(runtime, outer.delegation_id, 2)
                await release_model(runtime, outer)
            await wait_round_finished(runtime, outer)
            assert all(rec.state == "completed" and not rec._child_work_ids for rec in chain)
            assert len([item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]) == 1
            assert len(runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]) == 1
            assert len(submissions) == 1
    finally:
        file_check.release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize("overlap", [False, True], ids=["direct", "concurrent-cancel"])
@pytest.mark.parametrize("returned", ["closed", "unknown", "accepted"])
async def test_initial_exit_settles_the_original_terminal_observation_before_withdrawing(
    notification_runtime, file_check, monkeypatch, depth, overlap, returned,
):
    runtime = notification_runtime
    file_check.enabled = False
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    for name in ("Middle coordinator", "Unaccepted leaf"):
        await AgentManager(runtime.manager.db).create_agent(name=name, backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Keep the original setup exit owner",
    )
    await starts(runtime, outer.delegation_id, 1)
    chain = [outer]
    if depth == 2:
        caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
        middle = await runtime.delegations.start_delegation(
            parent_session_id=outer.delegation_id, agent_name="Middle coordinator", request="Own the original leaf setup", scope=caller,
        )
        await starts(runtime, middle.delegation_id, 1)
        chain.append(middle)
    parent = chain[-1]
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == parent.delegation_id)
    file_check.enabled = True
    starting = asyncio.create_task(runtime.delegations.start_delegation(
        parent_session_id=parent.delegation_id, agent_name="Unaccepted leaf", request="Never admit this leaf", scope=caller,
    ))
    archived, continue_setup, setup_resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observing, continue_observation = asyncio.Event(), asyncio.Event()
    cancelling = None
    try:
        child_id = await asyncio.wait_for(file_check.checking.get(), 2)
        child = runtime.delegations.get_delegation(child_id)
        file_check.enabled = False
        for rec in chain:
            original = next(receipt for sid, work, receipt in runtime.receipts
                            if sid == rec.delegation_id and work == rec.work_id)
            await release_model(runtime, rec)
            assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "success"
        async with asyncio.timeout(2):
            while any(rec._candidate_request_id is None for rec in chain):
                await asyncio.sleep(.005)
        assert parent._child_work_ids == {child.work_id} and not deliveries
        archive, lookup = runtime.manager.auto_archive_scheduled_session, runtime.manager.source_receipt
        delivery_id = f"agent-terminal:{child.work_id}"

        async def hold_original_archive(session_id, **kwargs):
            result = await archive(session_id, **kwargs)
            if session_id == child_id:
                archived.set()
                await continue_setup.wait()
                setup_resumed.set()
            return result

        async def observe_original_return(source, original_id, **kwargs):
            if original_id == delivery_id:
                observing.set()
                await continue_observation.wait()
                if returned == "unknown":
                    raise SessionOwnerUnavailable("Original terminal receipt is temporarily unreadable")
            return await lookup(source, original_id, **kwargs)

        monkeypatch.setattr(runtime.manager, "auto_archive_scheduled_session", hold_original_archive)
        monkeypatch.setattr(runtime.manager, "source_receipt", observe_original_return)
        close_work(child.work_id)
        file_check.release.set()
        await asyncio.wait_for(archived.wait(), 2)
        assert child.state == "cancelled"
        if overlap:
            cancelling = asyncio.create_task(runtime.delegations.cancel_delegation(child_id))
            await asyncio.wait_for(observing.wait(), 2)
            assert child._terminal_injecting and child._terminal_disposition is None
        if returned == "closed":
            await runtime.manager.archive_agent(parent.target_agent_id)
        continue_setup.set()
        await asyncio.wait_for(setup_resumed.wait(), 2)
        if not overlap:
            await asyncio.wait_for(observing.wait(), 2)
        continue_observation.set()
        assert await asyncio.wait_for(starting, 2) is child
        if cancelling is not None:
            assert await asyncio.wait_for(cancelling, 2) is child
        await runtime.delegations.confirm_recording()
        row = await runtime.manager.db.get_source_submission("delegation", delivery_id, session_id=parent.delegation_id)
        assert (row is not None) == (returned == "accepted")
        assert child._terminal_disposition == returned
        assert not any(scope.session_id == child_id for scope, _ in runtime.starts)
        if returned == "unknown":
            assert parent._child_work_ids == {child.work_id}
            assert all(rec.state == "running" for rec in chain)
            assert not deliveries and runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
            return
        if returned == "accepted":
            assert all(rec.state == "running" for rec in chain)
            assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
            assert len([item for item in deliveries if item[2]["delivery_id"] == delivery_id]) == 1
            await starts(runtime, parent.delegation_id, 2)
            await release_model(runtime, parent)
        if depth == 2:
            await starts(runtime, outer.delegation_id, 2)
            await release_model(runtime, outer)
        await wait_round_finished(runtime, outer)
        assert all(rec.state == "completed" and not rec._child_work_ids for rec in chain)
        assert len([item for item in deliveries if item[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]) == 1
        assert len(runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]) == 1
        await runtime.delegations.confirm_recording()
        assert not runtime.delegations.drain_blockers()
    finally:
        file_check.release.set()
        continue_setup.set()
        continue_observation.set()
        await asyncio.gather(starting, *([cancelling] if cancelling is not None else []), return_exceptions=True)


async def test_deleting_the_parent_does_not_withdraw_an_adopted_initial_child(notification_runtime, file_check):
    runtime = notification_runtime
    file_check.enabled = False
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Keep the adopted original input",
    )
    await starts(runtime, rec.delegation_id, 1)
    original = next(receipt for sid, work, receipt in runtime.receipts
                    if sid == rec.delegation_id and work == rec.work_id)
    assert original.accepted and runtime.manager.work_has_resources(rec.work_id)
    assert await runtime.manager.delete_session(runtime.parent.id)
    assert runtime.delegations.get_delegation(rec.delegation_id) is rec
    assert rec.state == "running" and work_is_open(rec.work_id)
    assert runtime.manager.get_session(rec.delegation_id) is not None
    await release_model(runtime, rec)
    completion = await asyncio.wait_for(original.wait(), 2)
    assert completion.outcome.status == "success"
    await runtime.delegations.confirm_recording()
    assert rec.state != "cancelled" and len(runtime.starts) == 1

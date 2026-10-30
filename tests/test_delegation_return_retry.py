"""A completed round owns its original return until admission is confirmed."""

import asyncio
import json
import sqlite3

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.sessions.runtime_types import SessionMailboxFull
from tests.test_delegation_work import wait_state
from tests.test_delegation_work import work_runtime as work_runtime


@pytest.mark.parametrize('source', ['terminal', 'question'])
async def test_return_reconciliation_preserves_full_masked_exception_chain(work_runtime, monkeypatch, caplog, source):
    manager, delegations, parent, starts = work_runtime
    submit = manager.start_message
    reached = asyncio.Event()
    tail = 'ORIGINAL_TRANSPORT_DIAGNOSTIC_END'

    async def lose_ack(session_id, prompt, **kwargs):
        receipt = await submit(session_id, prompt, **kwargs)
        if session_id == parent.id:
            reached.set()
            try:
                raise ValueError('Original transport cause token=DO_NOT_EXPOSE_ORIGINAL_TOKEN')
            except ValueError as cause:
                raise ConnectionError('Original ACK lost ' + 'detail ' * 400 + tail) from cause
        return receipt

    monkeypatch.setattr(manager, 'start_message', lose_ack)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='held while asking' if source == 'question' else 'Review once',
    )
    await asyncio.wait_for(starts.get(), 3)
    if source == 'question':
        await manager.create_pending_question(rec.delegation_id, [{'question': 'Original question'}])
    await asyncio.wait_for(reached.wait(), 3)
    await delegations.confirm_recording()
    assert tail in caplog.text and 'Original transport cause token=[redacted]' in caplog.text
    assert 'DO_NOT_EXPOSE_ORIGINAL_TOKEN' not in caplog.text


@pytest.mark.parametrize('failure', ['mailbox', 'runtime'])
@pytest.mark.parametrize('question_closed', [False, True])
async def test_real_question_notice_reentry_retires_the_original_failed_command(work_runtime, monkeypatch, failure, question_closed):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='held while asking',
    )
    await asyncio.wait_for(starts.get(), 3)
    submit, lookup, retain = manager.start_message, manager.source_receipt, manager.retain_result_notice
    failed = asyncio.Event()
    attempts = 0
    settled = False

    async def lose_ack(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        assert attempts == 1
        await submit(*args, **kwargs)
        raise ConnectionError('Original question ACK lost')

    async def missing(*args, **kwargs):
        return None if attempts else await lookup(*args, **kwargs)

    async def blocked_notice(*args, **kwargs):
        failed.set()
        if failure == 'mailbox':
            raise SessionMailboxFull('Original child notice capacity unavailable')
        raise RuntimeError('Original child notice writer failed')

    monkeypatch.setattr(manager, 'start_message', lose_ack)
    monkeypatch.setattr(manager, 'source_receipt', missing)
    monkeypatch.setattr(manager, 'retain_result_notice', blocked_notice)
    try:
        question_id = await manager.create_pending_question(rec.delegation_id, [{'question': 'Original question'}])
        await asyncio.wait_for(failed.wait(), 3)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        if question_closed:
            assert await manager.answer_question(rec.delegation_id, question_id, [{'text': 'Original manual answer'}])
        monkeypatch.setattr(manager, 'retain_result_notice', retain)
        await manager._broadcast({
            'type': 'question_request', 'session_id': rec.delegation_id, 'question_id': question_id,
            'questions': [{'question': 'Original question'}], 'work_id': rec.work_id,
        })
        await delegations.confirm_recording()
        settled = True
        assert attempts == 1 and manager.has_pending_question(rec.delegation_id, question_id) is not question_closed
        notices = [row for row in await manager.db.load_messages(rec.delegation_id) if row['type'] == 'delivery_notice']
        assert len(notices) == 1 and question_id in notices[0]['content']
    finally:
        monkeypatch.setattr(manager, 'start_message', submit)
        monkeypatch.setattr(manager, 'source_receipt', lookup)
        monkeypatch.setattr(manager, 'retain_result_notice', retain)
        if not settled:
            # The failed behavioral assertion stays failed. Retire only this
            # probe's already-finished command references so fixture teardown
            # can close its isolated SQLite worker on the red implementation.
            assert all(task.done() for task in delegations._commands)
            delegations._commands.clear()


@pytest.mark.parametrize('takeover', ['accepted', 'refused', 'concurrent-refusals'])
async def test_followup_retires_old_question_writer_only_after_confirmed_takeover(
    work_runtime, monkeypatch, tmp_path, takeover,
):
    manager, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='controlled original round',
    )
    await asyncio.wait_for(starts.get(), 3)
    submit, lookup, retain = manager.start_message, manager.source_receipt, manager.retain_result_notice
    failed = asyncio.Event()
    attempts = 0
    settled = False

    async def lose_question_ack(session_id, prompt, **kwargs):
        nonlocal attempts
        receipt = await submit(session_id, prompt, **kwargs)
        if kwargs.get('delivery_id', '').startswith('agent-question:'):
            attempts += 1
            assert attempts == 1
            raise ConnectionError('Original question ACK lost')
        return receipt

    async def missing_question_receipt(source, delivery_id, **kwargs):
        if attempts and delivery_id.startswith('agent-question:'):
            return None
        return await lookup(source, delivery_id, **kwargs)

    async def blocked_question_notice(session_id, delivery_id, content):
        if delivery_id.startswith('agent-question:'):
            failed.set()
            raise RuntimeError('Original child notice writer failed')
        return await retain(session_id, delivery_id, content)

    monkeypatch.setattr(manager, 'start_message', lose_question_ack)
    monkeypatch.setattr(manager, 'source_receipt', missing_question_receipt)
    monkeypatch.setattr(manager, 'retain_result_notice', blocked_question_notice)
    try:
        question_id = await manager.create_pending_question(first.delegation_id, [{'question': 'Original question'}])
        await asyncio.wait_for(failed.wait(), 3)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        assert await manager.answer_question(first.delegation_id, question_id, [{'text': 'Manual answer'}])
        (tmp_path / 'release').touch()
        await wait_state(delegations, first.delegation_id, 'completed')
        await manager.await_session_execution(first.delegation_id, timeout=5)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        if takeover == 'concurrent-refusals':
            await manager.archive_session(first.delegation_id, replace=False)
            get_agent, auto_archive = manager.db.get_agent, manager.auto_archive_scheduled_session
            preparing, archive_entered, submitting = asyncio.Event(), asyncio.Event(), asyncio.Event()
            prepare_release, archive_release, submit_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def pause_parent_read(agent_id):
                if agent_id == parent.agent_id and not preparing.is_set():
                    preparing.set()
                    await prepare_release.wait()
                return await get_agent(agent_id)

            async def pause_archive(session_id):
                archive_entered.set()
                await archive_release.wait()
                return await auto_archive(session_id)

            async def pause_second_admission(session_id, prompt, **kwargs):
                if session_id == first.delegation_id:
                    submitting.set()
                    await submit_release.wait()
                return await lose_question_ack(session_id, prompt, **kwargs)

            monkeypatch.setattr(manager.db, 'get_agent', pause_parent_read)
            monkeypatch.setattr(manager, 'auto_archive_scheduled_session', pause_archive)
            a = asyncio.create_task(delegations.follow_up_delegation(
                parent_session_id=parent.id, delegation_id=first.delegation_id, request='first refused followup',
            ))
            b = None
            try:
                await asyncio.wait_for(preparing.wait(), 3)
                delegations.close_session(parent.id)
                prepare_release.set()
                await asyncio.wait_for(archive_entered.wait(), 3)
                assert delegations.get_delegation(first.delegation_id) is first
                delegations.reopen_session(parent.id)
                monkeypatch.setattr(manager, 'start_message', pause_second_admission)
                b = asyncio.create_task(delegations.follow_up_delegation(
                    parent_session_id=parent.id, delegation_id=first.delegation_id, request='second refused followup',
                ))
                await asyncio.wait_for(submitting.wait(), 3)
                archive_release.set()
                with pytest.raises(DelegationError, match='admission is closed'):
                    await a
                assert manager.get_session(first.delegation_id) is None
                submit_release.set()
                with pytest.raises(DelegationError):
                    await b
                assert delegations.get_delegation(first.delegation_id) is first
                # Both operations restored the old writer. Neither command's
                # temporary replacement proves its unfinished notice retired.
                with pytest.raises(RuntimeError, match='cleanup failed'):
                    await delegations.confirm_recording()
            finally:
                prepare_release.set()
                archive_release.set()
                submit_release.set()
                await asyncio.gather(a, *([b] if b else []), return_exceptions=True)
                delegations.reopen_session(parent.id)
                monkeypatch.setattr(manager.db, 'get_agent', get_agent)
                monkeypatch.setattr(manager, 'auto_archive_scheduled_session', auto_archive)
                monkeypatch.setattr(manager, 'start_message', lose_question_ack)
            await manager.unarchive_session(first.delegation_id)
        elif takeover == 'refused':
            agents = AgentManager(manager.db)
            await agents.archive_agent(first.target_agent_id)
            with pytest.raises(DelegationError):
                await delegations.follow_up_delegation(
                    parent_session_id=parent.id, delegation_id=first.delegation_id, request='refused followup',
                )
            assert delegations.get_delegation(first.delegation_id) is first
            with pytest.raises(RuntimeError, match='cleanup failed'):
                await delegations.confirm_recording()
            await agents.unarchive_agent(first.target_agent_id)
        second = await delegations.follow_up_delegation(
            parent_session_id=parent.id, delegation_id=first.delegation_id, request='held next round',
        )
        await asyncio.wait_for(starts.get(), 3)
        await delegations.confirm_recording()
        settled = True
        assert second.work_id != first.work_id and first.state == 'completed'
        assert second.state == 'running' and attempts == 1
    finally:
        monkeypatch.setattr(manager, 'start_message', submit)
        monkeypatch.setattr(manager, 'source_receipt', lookup)
        monkeypatch.setattr(manager, 'retain_result_notice', retain)
        if not settled:
            # Allow the red probe to retire its isolated database after the
            # failed drain assertion; this cannot turn the assertion green.
            assert all(task.done() for task in delegations._commands)
            delegations._commands.clear()


@pytest.mark.parametrize('deleted', ['child', 'parent'])
async def test_deletion_closes_only_recording_at_the_missing_target(
    work_runtime, monkeypatch, tmp_path, deleted,
):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='controlled original round',
    )
    await asyncio.wait_for(starts.get(), 3)
    submit, lookup, retain = manager.start_message, manager.source_receipt, manager.retain_result_notice
    failed = asyncio.Event()
    attempts = 0
    settled = False

    async def lose_question_ack(session_id, prompt, **kwargs):
        nonlocal attempts
        receipt = await submit(session_id, prompt, **kwargs)
        if kwargs.get('delivery_id', '').startswith('agent-question:'):
            attempts += 1
            assert attempts == 1
            raise ConnectionError('Original question ACK lost')
        return receipt

    async def missing_question_receipt(source, delivery_id, **kwargs):
        if attempts and delivery_id.startswith('agent-question:'):
            return None
        return await lookup(source, delivery_id, **kwargs)

    async def blocked_question_notice(session_id, delivery_id, content):
        if delivery_id.startswith('agent-question:'):
            failed.set()
            raise RuntimeError('Original child notice writer failed')
        return await retain(session_id, delivery_id, content)

    monkeypatch.setattr(manager, 'start_message', lose_question_ack)
    monkeypatch.setattr(manager, 'source_receipt', missing_question_receipt)
    monkeypatch.setattr(manager, 'retain_result_notice', blocked_question_notice)
    try:
        question_id = await manager.create_pending_question(rec.delegation_id, [{'question': 'Original question'}])
        await asyncio.wait_for(failed.wait(), 3)
        assert await manager.answer_question(rec.delegation_id, question_id, [{'text': 'Manual answer'}])
        (tmp_path / 'release').touch()
        await wait_state(delegations, rec.delegation_id, 'completed')
        await manager.await_session_execution(rec.delegation_id, timeout=5)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        unrelated = await manager.create_session(parent.agent_id, 'Unrelated history', working_dir=str(tmp_path))
        assert await manager.delete_session(unrelated.id)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        session_id = rec.delegation_id if deleted == 'child' else parent.id
        async with manager.db.transaction(write=True) as tx:
            await tx.execute(
                "CREATE TEMP TRIGGER refuse_original_deletion BEFORE DELETE ON sessions "
                f"WHEN OLD.id = '{session_id}' BEGIN SELECT RAISE(ABORT, 'Deletion refused'); END",
            )
        with pytest.raises(sqlite3.IntegrityError, match='Deletion refused'):
            await manager.delete_session(session_id)
        assert await manager.db.get_session_binding(session_id) is not None
        with pytest.raises(RuntimeError, match='cleanup failed'):
            await delegations.confirm_recording()
        async with manager.db.transaction(write=True) as tx:
            await tx.execute('DROP TRIGGER refuse_original_deletion')
        assert await manager.delete_session(session_id)
        assert await manager.db.get_session_binding(session_id) is None
        if deleted == 'parent':
            # The child notice target still exists. Losing its parent does
            # not prove that this original question's notice was recorded.
            with pytest.raises(RuntimeError, match='cleanup failed'):
                await delegations.confirm_recording()
            monkeypatch.setattr(manager, 'retain_result_notice', retain)
            await manager._broadcast({
                'type': 'question_request', 'session_id': rec.delegation_id,
                'question_id': question_id, 'questions': [{'question': 'Original question'}], 'work_id': rec.work_id,
            })
            notices = [row for row in await manager.db.load_messages(rec.delegation_id) if row['type'] == 'delivery_notice']
            assert len(notices) == 1 and question_id in notices[0]['content']
        await delegations.confirm_recording()
        settled = True
        assert rec.state == 'completed' and attempts == 1
    finally:
        monkeypatch.setattr(manager, 'start_message', submit)
        monkeypatch.setattr(manager, 'source_receipt', lookup)
        monkeypatch.setattr(manager, 'retain_result_notice', retain)
        if not settled:
            assert all(task.done() for task in delegations._commands)
            delegations._commands.clear()


async def test_running_child_after_parent_deletion_records_question_return_failure(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='held while asking',
    )
    await asyncio.wait_for(starts.get(), 3)
    assert await manager.delete_session(parent.id)
    submit = manager.start_message
    parent_attempts = []

    async def observe_submission(session_id, prompt, **kwargs):
        if session_id == parent.id:
            parent_attempts.append(kwargs)
        return await submit(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, 'start_message', observe_submission)
    question_id = await manager.create_pending_question(rec.delegation_id, [{'question': 'Question after parent deletion'}])
    await delegations.confirm_recording()
    notices = [row for row in await manager.db.load_messages(rec.delegation_id) if row['type'] == 'delivery_notice']
    assert len(notices) == 1 and question_id in notices[0]['content']
    assert 'Automatic return stopped' in notices[0]['content']
    assert parent_attempts == [] and rec.state == 'running'
    assert manager.has_pending_question(rec.delegation_id, question_id)
    assert await manager.answer_question(rec.delegation_id, question_id, [{'text': 'Manual answer after deletion'}])
    assert not manager.has_pending_question(rec.delegation_id, question_id)


@pytest.mark.parametrize('fault', ['write-error', 'cancel'])
async def test_original_question_remembers_unknown_before_notice_failure_or_reentry(
    work_runtime, monkeypatch, fault,
):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name='Worker', request='held while asking',
    )
    await asyncio.wait_for(starts.get(), 3)
    # Capture an actually published question before driving its original relay
    # coroutine. This test owns that coroutine's failed/cancelled wait, so no
    # fake admission/completion or failed framework-command cleanup is hidden.
    delegations.shutdown()
    published = asyncio.Event()

    async def observed(event):
        if event.get('type') == 'question_request' and event['session_id'] == rec.delegation_id:
            published.set()

    manager.on_broadcast('original-question-publication', observed)
    question_id = await manager.create_pending_question(rec.delegation_id, [{'question': 'Original question'}])
    await asyncio.wait_for(published.wait(), 3)
    manager.remove_broadcast('original-question-publication')
    delegations.bind(manager, manager.db)
    submit, lookup, retain = manager.start_message, manager.source_receipt, manager.retain_result_notice
    submissions = 0
    writing, release = asyncio.Event(), asyncio.Event()

    async def lose_ack(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        assert submissions == 1
        await submit(*args, **kwargs)
        raise ConnectionError('Original question ACK lost')

    async def missing_receipt(*args, **kwargs):
        return None if submissions else await lookup(*args, **kwargs)

    async def fail_notice(*args, **kwargs):
        writing.set()
        await release.wait()
        raise RuntimeError('Original child notice write failed')

    monkeypatch.setattr(manager, 'start_message', lose_ack)
    monkeypatch.setattr(manager, 'source_receipt', missing_receipt)
    monkeypatch.setattr(manager, 'retain_result_notice', fail_notice)
    event = {'question_id': question_id, 'questions': [{'question': 'Original question'}]}
    original = asyncio.create_task(delegations._inject_question(rec, event))
    try:
        await asyncio.wait_for(writing.wait(), 3)
        await delegations._inject_question(rec, event)
        if fault == 'cancel':
            original.cancel()
            with pytest.raises(asyncio.CancelledError):
                await original
        else:
            release.set()
            with pytest.raises(RuntimeError, match='notice write failed'):
                await original
        monkeypatch.setattr(manager, 'retain_result_notice', retain)
        await delegations._inject_question(rec, event)
        assert submissions == 1 and manager.has_pending_question(rec.delegation_id, question_id)
        notices = [row for row in await manager.db.load_messages(rec.delegation_id) if row['type'] == 'delivery_notice']
        assert len(notices) == 1 and 'ACK lost' in notices[0]['content']
        assert rec.state == 'running' and rec._terminal_disposition is None
    finally:
        release.set()
        await asyncio.gather(original, return_exceptions=True)
        monkeypatch.setattr(manager, 'source_receipt', lookup)
        monkeypatch.setattr(manager, 'start_message', submit)
        monkeypatch.setattr(manager, 'retain_result_notice', retain)


@pytest.mark.parametrize("question", [False, True])
async def test_permanent_parent_refusal_preserves_delegated_result_or_live_question(
    work_runtime, monkeypatch, tmp_path, question,
):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled original work",
    )
    await asyncio.wait_for(starts.get(), 3)
    await manager.archive_session(parent.id, replace=False)
    async with manager.db.transaction(write=True) as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
        for _ in range(2):
            await tx.execute(
                "INSERT INTO messages(session_id,seq,role,type,content,created_at) "
                "VALUES(?,0,'user','text','\"duplicate\"','now')", (parent.id,),
            )
    submit = manager.start_message
    attempts = []

    async def observe(session_id, prompt, **kwargs):
        if session_id == parent.id:
            attempts.append(kwargs["delivery_id"])
        return await submit(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, "start_message", observe)
    if question:
        question_id = await manager.create_pending_question(rec.delegation_id, [{"question": "Original question"}])
        async with asyncio.timeout(3):
            while not attempts:
                await asyncio.sleep(.01)
        await delegations.confirm_recording()
        messages = await manager.db.load_messages(rec.delegation_id)
        notices = [row for row in messages if row["type"] == "delivery_notice"]
        assert len(notices) == 1 and question_id in notices[0]["content"] and "duplicate sequences" in notices[0]["content"]
        assert rec.state == "running" and rec._terminal_disposition is None
        assert manager.has_pending_question(rec.delegation_id, question_id)
        assert await manager.answer_question(rec.delegation_id, question_id, [{"text": "Original manual answer"}])
        assert await manager.wait_for_question_answer(rec.delegation_id, question_id) == "Q: Original question\nA: Original manual answer"
    else:
        (tmp_path / "release").touch()
        await wait_state(delegations, rec.delegation_id, "completed")
        await delegations.confirm_recording()
        assert rec.error is None and rec._terminal_disposition == "stopped"
        assert "duplicate sequences" in rec.terminal_delivery_error
        public = (await manager.work_test_client.get(f"/api/sessions/{parent.id}/delegations")).json()[0]
        assert public["state"] == "completed" and public["terminal_disposition"] == "stopped"
        assert public["terminal_delivery_error"] == rec.terminal_delivery_error
    assert len(attempts) == 1
    async with manager.db.transaction(write=True) as tx:
        await tx.execute("DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages WHERE session_id=?)", (parent.id,))
    await manager.archive_session(parent.id, replace=False)
    await manager.unarchive_session(parent.id)
    if not question:
        await delegations._inject_terminal(rec)
    else:
        await manager._broadcast({
            "type": "question_request", "session_id": rec.delegation_id, "question_id": question_id,
            "questions": [{"question": "Old question event"}], "work_id": rec.work_id,
        })
    assert len(attempts) == 1


async def test_terminal_diagnostic_failure_reentry_cannot_resubmit(work_runtime, monkeypatch):
    from server.delegations import DelegationError
    from server.sessions.runtime_types import SessionOwnerUnavailable

    manager, delegations, parent, _ = work_runtime
    submit, lookup, save = manager.start_message, manager.source_receipt, manager.db.compare_and_set_delegation_state
    submissions = 0
    failed = asyncio.Event()

    async def lose_ack(session_id, prompt, **kwargs):
        nonlocal submissions
        if session_id != parent.id:
            return await submit(session_id, prompt, **kwargs)
        submissions += 1
        assert submissions == 1
        await submit(session_id, prompt, **kwargs)
        raise ConnectionError("Original terminal ACK lost")

    async def unavailable(*args, **kwargs):
        if submissions:
            raise SessionOwnerUnavailable("Original receipt temporarily unavailable")
        return await lookup(*args, **kwargs)

    async def refuse_diagnostic(*args, **kwargs):
        if json.loads(args[2])["terminal_disposition"] == "unknown":
            failed.set()
            raise DelegationError("Diagnostic relationship temporarily unavailable", status_code=409)
        return await save(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", unavailable)
    monkeypatch.setattr(manager.db, "compare_and_set_delegation_state", refuse_diagnostic)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Finish once")
    await asyncio.wait_for(failed.wait(), 3)
    with pytest.raises(RuntimeError, match='cleanup failed'):
        await delegations.confirm_recording()
    assert rec._terminal_disposition == "unknown"
    monkeypatch.setattr(manager.db, "compare_and_set_delegation_state", save)
    await delegations._command(delegations._inject_terminal(rec))
    await delegations.confirm_recording()
    stored = json.loads(await manager.db.load_delegation_state(
        rec.delegation_id, parent_session_id=parent.id, user_id=parent.user_id,
    ))
    assert stored["state"] == "completed" and stored["terminal_disposition"] == "unknown"
    assert "ACK lost" in stored["terminal_delivery_error"] and submissions == 1


@pytest.mark.parametrize("empty", [False, True])
async def test_live_question_return_preserves_full_answer_instructions(work_runtime, empty):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held while asking",
    )
    await asyncio.wait_for(starts.get(), 3)
    questions = [] if empty else [{
        "question": "Which file should I focus on?", "multiSelect": False,
        "options": [{"label": "Dashboard.tsx", "description": "main UI"}, {"label": "Sidebar.tsx"}],
    }]
    question_id = await manager.create_pending_question(rec.delegation_id, questions)
    async with asyncio.timeout(3):
        while not manager.dispatch_snapshot(parent.id)["items"]:
            await asyncio.sleep(.01)
    queued = manager.dispatch_snapshot(parent.id)["items"]
    row = await manager.db.get_submission_for_request(parent.id, queued[0]["request_id"])
    prompt = row["payload"]["content"]
    assert "[agent-question:Worker" in prompt
    assert f"delegation={rec.delegation_id}" in prompt and f"question_id={question_id}" in prompt
    assert "mcp__ask_agent__answer" in prompt and "answer_agent_question" not in prompt
    if empty:
        assert "empty payload" in prompt
    else:
        for expected in ("Which file should I focus on?", "Dashboard.tsx", "Sidebar.tsx", "main UI", "single-choice"):
            assert expected in prompt
    assert rec.state == "running" and manager.has_pending_question(rec.delegation_id, question_id)
    await manager.close_pending_question(rec.delegation_id, question_id)


async def test_completed_round_unconfirmed_admission_never_resubmits(work_runtime, monkeypatch):
    from server.sessions.runtime_types import SessionOwnerUnavailable

    manager, delegations, parent, starts = work_runtime
    submit, lookup = manager.start_message, manager.source_receipt
    submissions = 0
    reached = asyncio.Event()

    async def lose_ack(session_id, prompt, **kwargs):
        nonlocal submissions
        if session_id != parent.id:
            return await submit(session_id, prompt, **kwargs)
        submissions += 1
        assert submissions == 1, "Unknown terminal admission must never submit again"
        await submit(session_id, prompt, **kwargs)
        reached.set()
        raise ConnectionError("Original terminal acceptance acknowledgment lost")

    async def unavailable(*args, **kwargs):
        if submissions:
            raise SessionOwnerUnavailable("Original terminal receipt is temporarily unreadable")
        return await lookup(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", unavailable)
    try:
        rec = await delegations.start_delegation(
            parent_session_id=parent.id, agent_name="Worker", request="Finish this original round once",
        )
        await asyncio.wait_for(reached.wait(), 3)
        await asyncio.wait_for(delegations.confirm_recording(), 2)
        public = rec.to_public_dict()
        assert public["state"] == "completed" and public["error"] is None
        assert public["terminal_disposition"] == "unknown"
        assert "acknowledgment lost" in public["terminal_delivery_error"]
        assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1 and starts.qsize() == 1
        assert submissions == 1
    finally:
        monkeypatch.setattr(manager, "source_receipt", lookup)
        await delegations.confirm_recording()


@pytest.mark.parametrize("receipt_observation", ["missing", "unavailable"])
async def test_unconfirmed_question_return_keeps_original_question_answerable(
    work_runtime, monkeypatch, receipt_observation,
):
    from server.sessions.runtime_types import SessionOwnerUnavailable

    manager, delegations, parent, starts = work_runtime
    submit, lookup = manager.start_message, manager.source_receipt
    submissions = 0
    reached = asyncio.Event()

    async def lose_ack(session_id, prompt, **kwargs):
        nonlocal submissions
        if session_id != parent.id:
            return await submit(session_id, prompt, **kwargs)
        submissions += 1
        assert submissions == 1, "Unknown question admission must never submit again"
        await submit(session_id, prompt, **kwargs)
        reached.set()
        raise ConnectionError("Original question acceptance acknowledgment lost")

    async def original_receipt(*args, **kwargs):
        if submissions:
            if receipt_observation == "unavailable":
                raise SessionOwnerUnavailable("Original question receipt temporarily unreadable")
            return None
        return await lookup(*args, **kwargs)

    monkeypatch.setattr(manager, "start_message", lose_ack)
    monkeypatch.setattr(manager, "source_receipt", original_receipt)
    try:
        rec = await delegations.start_delegation(
            parent_session_id=parent.id, agent_name="Worker", request="held while asking",
        )
        await asyncio.wait_for(starts.get(), 3)
        question_id = await manager.create_pending_question(rec.delegation_id, [{"question": "Which option?"}])
        await asyncio.wait_for(reached.wait(), 3)
        await asyncio.wait_for(delegations.confirm_recording(), 2)
        assert rec.state == "running" and rec._terminal_disposition is None
        messages = await manager.db.load_messages(rec.delegation_id)
        notices = [row for row in messages if row["type"] == "delivery_notice"]
        assert len(notices) == 1 and question_id in notices[0]["content"]
        assert "unconfirmed" in notices[0]["content"] and "acknowledgment lost" in notices[0]["content"]
        await manager._broadcast({
            "type": "question_request", "session_id": rec.delegation_id,
            "question_id": question_id, "questions": [{"question": "Which option?"}], "work_id": rec.work_id,
        })
        await delegations.confirm_recording()
        assert submissions == 1 and manager.has_pending_question(rec.delegation_id, question_id)
        assert await manager.answer_question(rec.delegation_id, question_id, [{"text": "Original answer"}])
        assert await manager.wait_for_question_answer(rec.delegation_id, question_id) == "Q: Which option?\nA: Original answer"
    finally:
        monkeypatch.setattr(manager, "source_receipt", lookup)
        await delegations.confirm_recording()


@pytest.mark.parametrize("failure", ["refused", "lost_ack", "shutdown_after_ack"])
async def test_completed_round_retries_its_original_parent_return(work_runtime, monkeypatch, failure):
    manager, delegations, parent, starts = work_runtime
    start, attempted = manager.start_message, False
    reached = asyncio.Event()

    async def intercept(session_id, prompt, **kwargs):
        nonlocal attempted
        returning = session_id == parent.id and kwargs.get("delivery_id", "").startswith("agent-terminal:")
        if returning and not attempted:
            attempted = True
            reached.set()
            if failure == "refused":
                raise SessionMailboxFull("Temporary parent input capacity")
            await start(session_id, prompt, **kwargs)
            if failure == "shutdown_after_ack":
                manager.close_admission()
            raise ConnectionError("Parent acceptance acknowledgment lost")
        return await start(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, "start_message", intercept)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Finish this round once")
    await asyncio.wait_for(reached.wait(), 5)
    await wait_state(delegations, rec.delegation_id, "completed")
    await delegations.confirm_recording()
    pending = manager.dispatch_snapshot(parent.id)
    assert pending["paused"] and len(pending["items"]) == 1
    row = await manager.db.get_submission_for_request(parent.id, pending["items"][0]["request_id"])
    assert row["payload"]["delivery_id"] == f"agent-terminal:{rec.work_id}"
    assert row["receipt"]["accepted"]
    assert starts.qsize() == 1


@pytest.mark.parametrize("failure", ["refused", "lost_ack", "closed_before_retry"])
async def test_pending_delegation_question_retries_under_its_original_identity(work_runtime, monkeypatch, failure):
    manager, delegations, parent, starts = work_runtime
    start, attempted = manager.start_message, False
    reached = asyncio.Event()

    async def intercept(session_id, prompt, **kwargs):
        nonlocal attempted
        returning = session_id == parent.id and kwargs.get("delivery_id", "").startswith("agent-question:")
        if returning and not attempted:
            attempted = True
            reached.set()
            if failure in {"refused", "closed_before_retry"}:
                raise SessionMailboxFull("Temporary parent input capacity")
            await start(session_id, prompt, **kwargs)
            raise ConnectionError("Question acceptance acknowledgment lost")
        return await start(session_id, prompt, **kwargs)

    monkeypatch.setattr(manager, "start_message", intercept)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held while asking")
    await asyncio.wait_for(starts.get(), 5)
    question = await manager.create_pending_question(rec.delegation_id, [{"question": "Which option?"}])
    await asyncio.wait_for(reached.wait(), 5)
    if failure == "closed_before_retry":
        await manager.close_pending_question(rec.delegation_id, question)
        await manager.interrupt(rec.delegation_id)
        await manager.await_session_execution(rec.delegation_id)
        await delegations.confirm_recording()
        for item in manager.dispatch_snapshot(parent.id)["items"]:
            row = await manager.db.get_submission_for_request(parent.id, item["request_id"])
            assert row["payload"]["delivery_id"] != f"agent-question:{rec.work_id}:{question}"
        return
    async with asyncio.timeout(4):
        while not manager.dispatch_snapshot(parent.id)["items"]:
            await asyncio.sleep(.01)
    queued = manager.dispatch_snapshot(parent.id)["items"]
    assert len(queued) == 1 and manager.has_pending_question(rec.delegation_id, question)
    row = await manager.db.get_submission_for_request(parent.id, queued[0]["request_id"])
    assert row["payload"]["delivery_id"] == f"agent-question:{rec.work_id}:{question}"
    assert row["payload"]["question_id"] == question
    await manager.close_pending_question(rec.delegation_id, question)

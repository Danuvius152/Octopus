"""Evaluation cleanup joins real request writers before releasing its API/DB."""

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from server.config import settings
from server.credential_bindings import CredentialBindingError
from server.crypto import decrypt, encrypt
from server.harness import get_harness
from server.oauth_providers import OAuthTokenSet, get_provider
from server.users import UserManager
from tests import test_wiki_query_real as query_support
from tests.callback_api import CallbackApi
from tests.harness_profiles import session_turn_argv
from tests.test_wiki_query_real import query_probe_context
from tests.wiki_query_evidence import (
    collect_evaluation,
    load_registration,
    source_manifest,
)


@pytest.fixture
def controlled_query_cli(monkeypatch, tmp_path, short_process_shutdown):
    script = tmp_path / 'query-cli.py'
    script.write_text(
        "import json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'query-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Controlled source answer'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
    )
    harness = get_harness('codex')
    processes = []
    spawned = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    def argv(context):
        return [sys.executable, '-u', str(script)], {'cwd': context.working_dir}

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(script) in args:
            processes.append(process)
            spawned.set()
        return process

    monkeypatch.setattr(harness, 'profile', replace(
        harness.profile, binary=sys.executable,
        build_turn_argv=session_turn_argv(harness.profile, argv), prepare_resume=None, activity_observer=None,
    ))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', observe_spawn)
    return SimpleNamespace(processes=processes, script=script, spawned=spawned)


async def assert_dependencies_available(probe):
    session = probe.manager.get_session(probe.last_session_id)
    db = probe.manager.db
    assert await db.get_session_binding(session.id) is not None
    bearer = await UserManager(db).issue_token(session.user_id)
    async with httpx.AsyncClient() as http:
        response = await http.get(f'http://127.0.0.1:{settings.port}/api/wiki/info', headers={'Authorization': f'Bearer {bearer}'})
        assert response.status_code == 200


@pytest.mark.parametrize('ending', ['timeout', 'cancel'])
async def test_failed_query_waits_for_original_late_writer_before_returning(
    controlled_query_cli, tmp_path, monkeypatch, ending,
):
    async with query_probe_context(tmp_path, monkeypatch) as probe:
        manager = probe.manager
        original = manager._handle_result_event
        writing, release, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def late_result(session, attempt, event):
            writing.set()
            await release.wait()
            await original(session, attempt, event)
            recorded.set()

        monkeypatch.setattr(manager, '_handle_result_event', late_result)

        call = asyncio.create_task(probe.ask('Read the original source'))
        waiter = call
        try:
            await asyncio.wait_for(writing.wait(), 3)
            session = manager.get_session(probe.last_session_id)
            if ending == 'timeout':
                waiter = asyncio.create_task(asyncio.wait_for(call, .1))
            else:
                call.cancel()
            await asyncio.sleep(.3)
            assert not waiter.done(), 'The failed query released its waiter while its original writer was still active'
            await assert_dependencies_available(probe)
            release.set()
            with pytest.raises(TimeoutError if ending == 'timeout' else asyncio.CancelledError):
                await waiter
            assert recorded.is_set()
            assert not manager.session_is_executing(session)
            assert all(process.returncode is not None for process in controlled_query_cli.processes)
            # The existing environment can execute a fresh isolated question
            # after the original request has actually retired.
            assert await probe.ask('Read the source in another isolated request') == 'Controlled source answer'
        finally:
            release.set()
            if not call.done():
                call.cancel()
            await asyncio.gather(call, waiter, return_exceptions=True)
            await manager.drain_turns()


@pytest.mark.parametrize('cancel_cleanup', [False, True])
async def test_fixture_joins_late_write_before_closing_dependencies(
    controlled_query_cli, tmp_path, monkeypatch, cancel_cleanup,
):
    context = query_probe_context(tmp_path, monkeypatch)
    probe = await context.__aenter__()
    manager = probe.manager
    original = manager._persist_message
    writing, release, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api_stopping = asyncio.Event()
    original_stop = CallbackApi.stop

    async def late_input(session, message, **kwargs):
        writing.set()
        await release.wait()
        sequence = await original(session, message, **kwargs)
        recorded.set()
        return sequence

    async def observe_stop(api):
        api_stopping.set()
        await original_stop(api)

    monkeypatch.setattr(manager, '_persist_message', late_input)
    monkeypatch.setattr(CallbackApi, 'stop', observe_stop)
    session = await manager.create_session(probe.agent_id, 'In-flight cleanup', str(tmp_path), backend='codex')
    probe.last_session_id = session.id
    closing = None
    try:
        receipt = await manager.start_message(session.id, 'Read the source', wiki_mode='read')
        await asyncio.wait_for(writing.wait(), 3)
        assert not controlled_query_cli.processes
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(.1)
        if cancel_cleanup:
            closing.cancel()
            await asyncio.sleep(.05)
            closing.cancel()
        assert not closing.done()
        assert not api_stopping.is_set(), 'The callback API closed before the original input writer finished'
        await assert_dependencies_available(probe)
        release.set()
        if cancel_cleanup:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        assert recorded.is_set()
        assert (await receipt.wait()).recording_state == 'committed'
        assert all(process.returncode is not None for process in controlled_query_cli.processes)
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await context.__aexit__(None, None, None)


async def test_successful_query_receipt_already_proves_retirement(
    controlled_query_cli, tmp_path, monkeypatch,
):
    async with query_probe_context(tmp_path, monkeypatch) as probe:
        async def unnecessary_cancel(*args, **kwargs):
            pytest.fail('A committed successful request must keep its original outcome')

        monkeypatch.setattr(probe.manager, 'cancel_request', unnecessary_cancel)
        assert await probe.ask('Read the source') == 'Controlled source answer'
        session = probe.manager.get_session(probe.last_session_id)
        assert not probe.manager.session_is_executing(session)
        assert all(process.returncode is not None for process in controlled_query_cli.processes)
        assert not probe.cleanup_failures


async def test_cancelled_query_retires_its_running_cli_before_returning(
    controlled_query_cli, tmp_path, monkeypatch,
):
    controlled_query_cli.script.write_text(
        "import json,time\n"
        "print(json.dumps({'type':'thread.started','thread_id':'running-query'}),flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    async with query_probe_context(tmp_path, monkeypatch) as probe:
        call = asyncio.create_task(probe.ask('Read the source'))
        try:
            await asyncio.wait_for(controlled_query_cli.spawned.wait(), 3)
            process, = controlled_query_cli.processes
            assert process.returncode is None
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert process.returncode is not None
            assert not probe.cleanup_failures
        finally:
            if not call.done():
                call.cancel()
            await asyncio.gather(call, return_exceptions=True)


async def test_fixture_joins_shared_refresh_after_its_waiter_was_cancelled(
    controlled_query_cli, tmp_path, monkeypatch,
):
    context = query_probe_context(tmp_path, monkeypatch)
    probe = await context.__aenter__()
    manager, db = probe.manager, probe.manager.db
    session = await manager.create_session(probe.agent_id, 'Refresh cleanup', str(tmp_path), backend='codex')
    probe.last_session_id = session.id
    key = await UserManager(db).data_key_for(session.user_id)
    await db.save_credential('refreshing', 'claude-code', 'Synthetic refresh', 'oauth', encrypt(json.dumps({
        'access_token': 'expired', 'refresh_token': 'original', 'expires_at_epoch': 0,
    }), key), 'now', user_id=session.user_id)
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_stop = CallbackApi.stop

    async def refresh(token):
        entered.set()
        await release.wait()
        return OAuthTokenSet('fresh', 'rotated', time.time() + 3600)

    async def verify_before_stop(api):
        row = await db.get_credential('refreshing')
        assert json.loads(decrypt(row['secret_encrypted'], key))['access_token'] == 'fresh'
        committed.set()
        await original_stop(api)

    monkeypatch.setattr(get_provider('claude-code'), 'refresh_access_token', refresh)
    monkeypatch.setattr(CallbackApi, 'stop', verify_before_stop)
    operation = asyncio.create_task(manager.resolve_credential_by_id('refreshing', backend='claude-code', user_id=session.user_id))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(.1)
        assert not closing.done()
        await assert_dependencies_available(probe)
        release.set()
        await closing
        assert committed.is_set()
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await context.__aexit__(None, None, None)


async def test_request_cleanup_failure_preserves_cancellation_and_its_own_diagnostic(
    controlled_query_cli, tmp_path, monkeypatch,
):
    async with query_probe_context(tmp_path, monkeypatch) as probe:
        manager = probe.manager
        original_result, original_cancel = manager._handle_result_event, manager.cancel_request
        writing, release = asyncio.Event(), asyncio.Event()

        async def late_result(session, attempt, event):
            writing.set()
            await release.wait()
            await original_result(session, attempt, event)

        async def failed_cancel(*args, **kwargs):
            try:
                raise ValueError('original cleanup cause; token=private-test-value')
            except ValueError as cause:
                raise OSError('cancel acknowledgement unavailable') from cause

        monkeypatch.setattr(manager, '_handle_result_event', late_result)
        monkeypatch.setattr(manager, 'cancel_request', failed_cancel)
        output = tmp_path / 'evidence'
        call = asyncio.create_task(collect_evaluation(probe, 'Read the source', case_id='cleanup-failure', output=output))
        try:
            await asyncio.wait_for(writing.wait(), 3)
            call.cancel()
            with pytest.raises(asyncio.CancelledError) as raised:
                await call
            assert 'Original request cleanup failed' in raised.value.__notes__[0]
            report = json.loads(next(output.glob('*/report.json')).read_text())
            assert report['execution_status'] == 'failed'
            assert report['failure']['type'] == 'CancelledError'
            cleanup, = report['cleanup_failures']
            assert cleanup['phase'] == 'request'
            assert 'cancel acknowledgement unavailable' in cleanup['diagnostic']
            assert 'original cleanup cause' in cleanup['diagnostic']
            assert 'private-test-value' not in cleanup['diagnostic']
            await assert_dependencies_available(probe)
        finally:
            release.set()
            monkeypatch.setattr(manager, 'cancel_request', original_cancel)
            if not call.done():
                call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            await original_cancel(probe.last_session_id, probe.last_request_id)


async def test_fixture_cleanup_failure_keeps_dependencies_and_original_failure(
    controlled_query_cli, tmp_path, monkeypatch,
):
    apis = []
    original_start = query_support.start_callback_api

    async def observe_start(*args, **kwargs):
        api = await original_start(*args, **kwargs)
        apis.append(api)
        return api

    monkeypatch.setattr(query_support, 'start_callback_api', observe_start)
    original_failure = AssertionError('original evaluation failure')
    probe = None
    try:
        with pytest.raises(AssertionError) as raised:
            async with query_probe_context(tmp_path, monkeypatch) as probe:
                manager = probe.manager
                original_drain = manager.drain_turns
                session = await manager.create_session(probe.agent_id, 'Cleanup fault', str(tmp_path), backend='codex')
                probe.last_session_id = session.id

                async def failed_drain():
                    raise RuntimeError('original owners remain unconfirmed')

                monkeypatch.setattr(manager, 'drain_turns', failed_drain)
                raise original_failure
        assert raised.value is original_failure
        assert 'Fixture cleanup failed' in original_failure.__notes__[0]
        cleanup, = probe.cleanup_failures
        assert cleanup['phase'] == 'fixture'
        assert 'original owners remain unconfirmed' in cleanup['diagnostic']
        await assert_dependencies_available(probe)
    finally:
        if probe is not None:
            monkeypatch.setattr(manager, 'drain_turns', original_drain)
            await original_drain()
            await manager.drain_credential_refreshes()
            await manager.stop_all_held_processes()
            await apis[0].stop()
            await manager.db.close()


async def test_registered_setup_failure_closes_database_without_starting_cli(
    controlled_query_cli, tmp_path, monkeypatch,
):
    root = tmp_path / 'registered-vault'
    root.mkdir()
    (root / 'note.md').write_text('# Synthetic\n')
    monkeypatch.setattr(settings, 'codex_home_dir', str(tmp_path / 'no-login'))
    databases, closed = [], []
    original_database = query_support.Database

    def database(*args, **kwargs):
        db = original_database(*args, **kwargs)
        original_close = db.close

        async def close(*args, **kwargs):
            await original_close(*args, **kwargs)
            closed.append(db)

        monkeypatch.setattr(db, 'close', close)
        databases.append(db)
        return db

    monkeypatch.setattr(query_support, 'Database', database)
    registration = {'source_root': str(root), 'credential_id': 'absent',
                    'profile': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'low', 'service_tier': 'default'}}
    try:
        with pytest.raises(CredentialBindingError):
            async with query_probe_context(tmp_path, monkeypatch, registration=registration):
                pytest.fail('Missing selected login must stop setup')
        assert closed == databases
        assert not controlled_query_cli.processes
    finally:
        for db in databases:
            await db.close()


async def test_registered_query_executes_its_bound_login_and_explicit_profile(
    controlled_query_cli, tmp_path, monkeypatch,
):
    root = tmp_path / 'registered-vault'
    root.mkdir()
    (root / 'note.md').write_text('# Synthetic source\n')
    homes = tmp_path / 'homes'
    (homes / 'registered').mkdir(parents=True)
    (homes / 'registered/auth.json').write_text('{}')
    monkeypatch.setattr(settings, 'codex_home_dir', str(homes))
    harness = get_harness('codex')
    catalog = replace(harness.model_settings(None), models=[], reasoning_efforts=['max'], service_tiers=['default'])
    monkeypatch.setattr(harness, 'model_settings', lambda credential: catalog)
    manifest = json.dumps(source_manifest(root), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    registration = {'source_root': str(root), 'credential_id': 'registered',
                    'source_manifest_sha256': hashlib.sha256(manifest).hexdigest(),
                    'questions': [{'id': 'registered', 'question': 'Read the current source'}],
                    'profile': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'max', 'service_tier': 'default'}}
    path = tmp_path / 'registration.json'
    path.write_text(json.dumps(registration))
    async with query_probe_context(tmp_path, monkeypatch, registration=load_registration(path)) as probe:
        assert await probe.ask('Read the current source') == 'Controlled source answer'
        session = probe.manager.get_session(probe.last_session_id)
        assert session.credential_id == 'registered'
        rendered, = probe.input_context['rendered_attempt_profiles']
        assert rendered['configured_model'] == 'gpt-6.1-sol'
        assert rendered['configured_effort'] == 'max'
        assert rendered['configured_tier'] == 'default'
        assert probe.input_context['profile'] == registration['profile']
        assert controlled_query_cli.processes and all(process.returncode is not None for process in controlled_query_cli.processes)

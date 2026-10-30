"""The evaluation submits raw human questions through the real WS handler."""

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.routers.ws import router
from server.sessions import SessionManager
from tests import cli_gate, conftest
from tests.accounts import create_account
from tests.callback_api import start_callback_api
from tests.test_wiki_chat_real import OrdinaryChatProbe
from tests.test_wiki_chat_real import ordinary_chat_probe as ordinary_chat_probe
from tests.test_wiki_query_retirement import (
    controlled_query_cli as controlled_query_cli,
)
from tests.wiki_query_evidence import (
    collect_evaluation,
    guarded_credential,
    source_manifest,
)


async def test_losing_selected_credential_between_questions_forbids_default_fallback(tmp_path):
    selected = SimpleNamespace(home_dir=str(tmp_path / "selected-home"))
    available = iter([selected, None])

    async def actual_resolution(session, agent, harness):
        return next(available)

    manager = SimpleNamespace(_resolve_credential=actual_resolution)
    resolve = guarded_credential(manager, "registered", selected.home_dir)
    session = SimpleNamespace(credential_id="registered")
    assert await resolve(session, None, None) is selected
    with pytest.raises(AssertionError, match="default-login fallback forbidden"):
        await resolve(session, None, None)


class TestRegisteredChat:
    @pytest.fixture(autouse=True)
    def registration(self, controlled_query_cli, tmp_path, monkeypatch, request):
        root = tmp_path / 'frozen-source'
        root.mkdir()
        (root / 'note.md').write_text('# Synthetic evidence\n')
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
                        'questions': [{'id': 'chat-registered', 'question': 'Read the source'}],
                        'profile': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'max', 'service_tier': 'default'}}
        path = tmp_path / 'registration.json'
        path.write_text(json.dumps(registration))
        monkeypatch.setattr(request.config.option, 'wiki_chat_registration', str(path))

    async def test_profile_and_selected_login_reach_real_websocket_execution(self, ordinary_chat_probe):
        probe, registration = ordinary_chat_probe
        assert await probe.ask('Read the source') == 'Controlled source answer'
        session = probe.manager.get_session(probe.last_session_id)
        assert session.credential_id == 'registered'
        rendered, = probe.input_context['rendered_attempt_profiles']
        assert rendered['configured_effort'] == 'max'
        assert probe.input_context['configured_profile'] == 'gpt-6.1-sol/max/default'
        assert probe.root != Path(registration['source_root'])
        (probe.root / 'note.md').write_text('# Changed in this case only\n')
        assert (Path(registration['source_root']) / 'note.md').read_text() == '# Synthetic evidence\n'

    async def test_repeated_cancellation_cannot_release_original_chat_writer(self, ordinary_chat_probe, monkeypatch):
        probe, _ = ordinary_chat_probe
        writing, release, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = probe.manager._handle_result_event

        async def late_result(session, attempt, event):
            writing.set()
            await release.wait()
            await original(session, attempt, event)
            recorded.set()

        monkeypatch.setattr(probe.manager, '_handle_result_event', late_result)
        call = asyncio.create_task(probe.ask('Read the source'))
        try:
            await asyncio.wait_for(writing.wait(), 3)
            call.cancel()
            await asyncio.sleep(.05)
            call.cancel()
            await asyncio.sleep(.1)
            assert not call.done(), 'Chat released its failed waiter before the original writer retired'
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert recorded.is_set()
        finally:
            release.set()
            await asyncio.gather(call, return_exceptions=True)


@pytest.mark.parametrize("knowledge_eval", [False, True])
def test_knowledge_execution_does_not_pay_for_an_unrecorded_default_cli_probe(monkeypatch, knowledge_eval):
    calls = []
    monkeypatch.setattr(cli_gate, "codex_cli_works", lambda: calls.append("codex") or True)
    item = SimpleNamespace(get_closest_marker=lambda name: name == "real_codex" or name == "knowledge_eval" and knowledge_eval)
    conftest.pytest_runtest_setup(item)
    assert calls == ([] if knowledge_eval else ["codex"])


@pytest.mark.parametrize("outcome", ["success", "failed", "rejected"])
async def test_raw_question_real_websocket_identity_and_terminal_control(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(settings, "auth_token", "ephemeral-wiki-chat-host")
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    default = await db.get_system_agent()
    submitted, publishers = [], []
    builds = []
    answer_ready = tmp_path / "answer-ready"
    real_start = manager.start_message

    def argv(context):
        builds.append(context)
        code = "import json,time\nfrom pathlib import Path\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'offline-chat'}),flush=True)\n"
        code += f"while not Path({str(answer_ready)!r}).exists(): time.sleep(.005)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'A source-dependent answer.'}}),flush=True)\n"
        terminal = ({"type": "turn.failed", "error": {"message": "Controlled offline failure"}}
                    if outcome == "failed" else {"type": "turn.completed", "usage": {}})
        code += f"print({json.dumps(json.dumps(terminal))},flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv,
                                                 prepare_resume=None, activity_observer=None))

    async def start_message(session_id, question, **kwargs):
        if outcome == "rejected":
            manager.close_admission()
        receipt = await real_start(session_id, question, **kwargs)
        submitted.append({"question": question, "kwargs": kwargs, "receipt": receipt})

        async def publish():
            await asyncio.sleep(0.01)
            await manager._broadcast({"type": "result", "session_id": session_id, "request_id": "stale-request",
                                      "terminal": True, "content": {"status": "success", "request_id": "stale-request"}})
            answer_ready.write_text("Produce the original request's actual result")

        if receipt.accepted:
            publishers.append(asyncio.create_task(publish()))
        return receipt

    monkeypatch.setattr(manager, "start_message", start_message)
    api = await start_callback_api(router, mcp=False, websocket=True, overrides={get_session_manager: lambda: manager})
    probe = OrdinaryChatProbe(manager, default["id"], tmp_path, tmp_path, api.port, bearer)
    question = "为什么最大值和最小值都要保留？"
    try:
        if outcome == "success":
            assert await asyncio.wait_for(probe.ask(question), 5) == "A source-dependent answer."
            assert probe.last_request_id == submitted[0]["receipt"].request_id
            assert probe.last_events[-1]["content"]["request_id"] == probe.last_request_id
        else:
            with pytest.raises(AssertionError if outcome == "failed" else RuntimeError):
                await asyncio.wait_for(probe.ask(question), 5)
        assert len(submitted) == 1 and submitted[0]["question"] == question
        kwargs = submitted[0]["kwargs"]
        assert isinstance(kwargs["client_request_id"], str) and len(kwargs["client_request_id"]) == 32
        assert {key: value for key, value in kwargs.items() if key != "client_request_id"} == {
            "attachment_ids": [], "steerable": True, "wiki_mode": None, "recovery_of_client_id": None,
        }
        saved = await db.get_client_submission(probe.last_session_id, kwargs["client_request_id"])
        if outcome == "rejected":
            assert saved is None and builds == []
        else:
            assert saved["payload"]["content"] == question
            assert saved["request_id"] == probe.last_request_id == submitted[0]["receipt"].request_id
            assert saved["terminal"]["status"] == outcome
            assert len(builds) == 1 and question in builds[0].prompt
        session = manager.get_session(probe.last_session_id)
        assert session.origin == "user" and session.parent_session_id is None
        if outcome == "success":
            # After a real WS success, a later failure before a new session
            # exists must not inherit that answer's identity or observations.
            previous_session = probe.last_session_id
            previous_request = probe.last_request_id
            source_root = tmp_path / "sources"
            source_root.mkdir()
            (source_root / "note.md").write_text("An unchanged source.\n")
            probe.root = source_root
            for number, failure in enumerate([RuntimeError("create failed"), asyncio.CancelledError()]):
                async def fail_creation(*args, error=failure, **kwargs):
                    raise error

                probe.last_session_id = previous_session
                probe.last_request_id = previous_request
                probe.last_events = [{"type": "assistant_text", "content": "previous answer"}]
                probe.usage_observations = [{"raw": {"marker": "previous usage"}}]
                probe.call_trace = [{"operation": "previous read"}]
                monkeypatch.setattr(manager, "create_session", fail_creation)
                output = tmp_path / "reports" / str(number)
                with pytest.raises(type(failure)):
                    await collect_evaluation(probe, "A new natural question?", case_id=f"creation-{number}", output=output)
                report = json.loads(next(output.glob("*/report.json")).read_text())
                assert report["execution_status"] == "failed"
                assert report["session_id"] is report["request_id"] is None
                assert report["answer"] == "" and report["normalized_events"] == []
                assert report["usage_observations"] == [] and report["tool_trace"] == []
    finally:
        await asyncio.gather(*publishers)
        await api.stop()
        await manager.stop_all_held_processes()
        await db.close()

"""The evaluation submits raw human questions through the real WS handler."""

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server import deps
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.routers.ws import router
from server.sessions import SessionManager
from tests import cli_gate, conftest
from tests.callback_api import start_callback_api
from tests.test_wiki_chat_real import OrdinaryChatProbe, guarded_chat_credential
from tests.wiki_query_evidence import collect_evaluation


async def test_losing_selected_credential_between_questions_forbids_default_fallback(tmp_path):
    selected = SimpleNamespace(home_dir=str(tmp_path / "selected-home"))
    available = iter([selected, None])

    async def actual_resolution(session, agent, harness):
        return next(available)

    manager = SimpleNamespace(_resolve_credential=actual_resolution)
    resolve = guarded_chat_credential(manager, "registered", selected.home_dir)
    session = SimpleNamespace(credential_id="registered")
    assert await resolve(session, None, None) is selected
    with pytest.raises(AssertionError, match="default-login fallback forbidden"):
        await resolve(session, None, None)


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
    monkeypatch.setattr(deps, "_user_manager", None)
    monkeypatch.setattr(deps, "_accounts_exist", None)
    db = Database(":memory:")
    await db.initialize()
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
    probe = OrdinaryChatProbe(manager, default["id"], tmp_path, tmp_path, api.port, settings.auth_token)
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

"""Opt-in natural questions through the ordinary authenticated WebSocket entry.

No delegation or forced Wiki mode. A registration file is required before any
model request; answers remain subject to separate semantic review.
"""

import asyncio
import json
import logging
import secrets
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from server import deps
from server.agent_manager import AgentManager
from server.aio import complete_cleanup
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.diagnostics import exception_diagnostic
from server.main import _wiki_error
from server.mcp_identity import mint
from server.mcp_servers import wiki as wiki_tools
from server.routers.sessions import router as sessions_router
from server.routers.wiki import router as wiki_router
from server.routers.ws import router as ws_router
from server.sessions import SessionManager
from server.wiki import WikiError, registry
from tests.accounts import create_account
from tests.callback_api import start_callback_api
from tests.wiki_query_evidence import (
    collect_evaluation,
    copy_registered_sources,
    guarded_credential,
    load_registration,
    observe_invocations,
    observe_model_measurements,
)

pytestmark = [pytest.mark.real, pytest.mark.real_codex, pytest.mark.knowledge_eval]
logger = logging.getLogger(__name__)


@dataclass
class OrdinaryChatProbe:
    manager: SessionManager
    agent_id: str
    root: Path
    working_dir: Path
    port: int
    token: str = field(repr=False)
    credential_id: str = ""
    call_trace: list[dict] = field(default_factory=list)
    usage_observations: list[dict] = field(default_factory=list)
    last_events: list[dict] = field(default_factory=list)
    last_session_id: str | None = None
    last_request_id: str | None = None
    input_context: dict = field(default_factory=dict)
    cleanup_failures: list[dict] = field(default_factory=list)
    model_observations: list[dict] = field(default_factory=list)

    def record_cleanup_failure(self, phase, error):
        diagnostic = exception_diagnostic(error, secrets=(self.token,))
        self.cleanup_failures.append({'phase': phase, 'diagnostic': diagnostic})
        logger.error('Ordinary Wiki chat %s cleanup failed:\n%s', phase, diagnostic)

    async def ask(self, question):
        self.call_trace.clear()
        self.usage_observations.clear()
        self.last_events.clear()
        self.cleanup_failures.clear()
        self.model_observations.clear()
        self.input_context['rendered_attempt_profiles'] = []
        self.last_session_id = None
        self.last_request_id = None
        session = await self.manager.create_session(
            self.agent_id, "Chat", str(self.working_dir),
            credential_id=self.credential_id, origin="user", backend="codex",
        )
        self.last_session_id = session.id
        client_id = uuid.uuid4().hex
        receipt, outcome = None, None
        try:
            async with asyncio.timeout(240):
                async with connect(f"ws://127.0.0.1:{self.port}/ws?token={self.token}", max_size=16 * 1024 * 1024) as socket:
                    await socket.send(json.dumps({"type": "send_message", "session_id": session.id,
                                                  "content": question, "client_request_id": client_id}))
                    while receipt is None or outcome is None:
                        event = json.loads(await socket.recv())
                        if event.get("session_id") != session.id:
                            continue
                        self.last_events.append(event)
                        if event.get("type") == "submission" and event.get("client_request_id") == client_id:
                            assert event.get("accepted") is True, "ordinary chat submission rejected"
                            receipt = event
                            self.last_request_id = event["request_id"]
                        if receipt:
                            matching = [item for item in self.last_events if item.get("type") == "result"
                                        and item.get("terminal") is True and item.get("request_id") == self.last_request_id]
                            assert len(matching) <= 1, "duplicate terminal outcome"
                            outcome = matching[0] if matching else None
                await self.manager.await_session_execution(session.id)
        except BaseException as error:
            async def retire_request(original_failure=error):
                try:
                    if self.last_request_id is not None:
                        await self.manager.cancel_request(session.id, self.last_request_id)
                    else:
                        # This isolated evaluation session has no other work;
                        # its admission ACK may not have reached the WebSocket.
                        await self.manager.interrupt(session.id)
                        await self.manager.await_session_execution(session.id)
                except Exception as cleanup_error:
                    self.record_cleanup_failure('request', cleanup_error)
                    original_failure.add_note('Original chat request cleanup failed; see cleanup_failures.')

            try:
                await complete_cleanup(retire_request())
            except asyncio.CancelledError:
                pass
            self.input_context['execution_failure_diagnostic'] = exception_diagnostic(error, secrets=(self.token,))
            if isinstance(error, asyncio.CancelledError):
                raise
            # A WebSocket URL contains the short-lived test-host token.
            message = str(error).replace(self.token, "<redacted>")
            raise RuntimeError(f"{type(error).__name__}: {message}") from None
        assert outcome and not outcome.get("is_error"), "ordinary chat execution failed"
        content = outcome.get("content")
        assert isinstance(content, dict) and content.get("status") == "success", "unsuccessful request outcome"
        assert content.get("request_id") == self.last_request_id, "outcome request mismatch"
        assert not any(event.get("type") == "error" for event in self.last_events), "execution emitted an error"
        assert not any(call["operation"] in {"maintain", "organize", "publish", "revise", "restructure"} for call in self.call_trace), "a query attempted knowledge maintenance"
        answer = "\n".join(event.get("content", "") for event in self.last_events if event.get("type") == "assistant_text")
        assert answer.strip(), "empty ordinary chat answer"
        return answer


@pytest.fixture
async def ordinary_chat_probe(tmp_path, monkeypatch, request):
    configured = request.config.getoption("wiki_chat_registration")
    assert configured, "--wiki-chat-registration is required before running any ordinary-chat model request"
    registration = load_registration(configured)
    root = copy_registered_sources(registration, tmp_path / 'registered-sources')
    credential_id = registration["credential_id"]
    assert isinstance(credential_id, str) and credential_id
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "auth_token", secrets.token_urlsafe(32))
    monkeypatch.setattr(deps, "_user_manager", None)
    # Keep this fixture's observer handles separate from other test registries.
    monkeypatch.setattr(registry, "_stores", OrderedDict())
    monkeypatch.setattr(registry, "_retired_stores", {})
    work = tmp_path / "ordinary-work"
    work.mkdir()
    db = Database(":memory:")
    await db.initialize()
    owner, bearer = await create_account(db, extra_roots=[root, work])
    await db.save_credential(
        credential_id, "codex", "Registered evaluation home", "oauth", "",
        "2026-10-24T00:00:00+00:00", user_id=owner["id"],
    )
    manager = SessionManager()
    await manager.initialize(db)
    api, probe, failure = None, None, None
    try:
        # Directory-backed credential resolution reads no provider secrets here.
        # Refuse fallback to an unrelated default CLI login.
        credential = await manager.resolve_credential_by_id(credential_id, backend="codex", user_id=owner["id"])
        assert credential is not None, "selected credential home is unavailable; default-login fallback forbidden"
        monkeypatch.setattr(manager, "_resolve_credential", guarded_credential(manager, credential_id, credential.home_dir))
        agent = await AgentManager(db).create_agent(
            name="Assistant", backend="codex", system_prompt="",
            **registration['profile'], mcp_servers=[],
            user_id=owner["id"],
        )
        curator = await db.get_system_agent(owner["id"])
        registry.configure(owner["id"], root, curator["id"])
        api = await start_callback_api(
            ws_router, sessions_router, wiki_router, websocket=True,
            overrides={get_session_manager: lambda: manager}, exception_handlers={WikiError: _wiki_error},
            readiness_token=mint("callback-api-readiness", user_id=owner["id"]),
        )
        monkeypatch.setattr(settings, "port", api.port)
        probe = OrdinaryChatProbe(manager, agent["id"], root, work, api.port, bearer, credential_id)
        probe.input_context = {
            "transport": "authenticated WS send_message", "origin": "user", "parent_session_id": None,
            "forced_wiki_mode": False, "per_question_evaluator_prompt": False,
            "persona": "empty; normal framework/shared guides still supplied",
            "agent_name": "Assistant", "session_title": "Chat",
            "configured_profile": '/'.join(registration['profile'][key] for key in ('model', 'reasoning_effort', 'service_tier')),
            "credential_id": credential_id,
            "source_manifest_sha256": registration["source_manifest_sha256"],
            "environment": "isolated owned account and bearer, registered fixed corpus; production data not used",
            "blind": False, "filesystem_isolation": "fresh cwd and memory; unrestricted process filesystem access is not a blind-test guarantee",
            "extra_cli_availability_model_probe": False,
        }
        observe_invocations(monkeypatch, probe)
        observe_model_measurements(monkeypatch, probe)
        original_call = wiki_tools.call

        def record_call(method, path, *, params=None, body=None):
            item = {"method": method, "operation": path, "params": deepcopy(params), "body": deepcopy(body)}
            probe.call_trace.append(item)
            try:
                result = original_call(method, path, params=params, body=body)
            except Exception as error:
                item["exception"] = {"type": type(error).__name__}
                raise
            item["response"] = deepcopy(result)
            return result

        monkeypatch.setattr(wiki_tools, "call", record_call)
        original_result = manager._handle_result_event

        async def record_usage(session, attempt, event):
            current = session._run.request
            probe.usage_observations.append({
                "session_id": session.id, "request_id": getattr(current, "request_id", probe.last_request_id),
                "attempt_id": attempt.attempt_id, "native_thread_id": event.session_id,
                "reused": attempt.reused, "is_error": event.is_error, "raw": deepcopy(event.raw),
            })
            await original_result(session, attempt, event)

        monkeypatch.setattr(manager, "_handle_result_event", record_usage)
        yield probe, registration
    except BaseException as error:
        failure = error
        raise
    finally:
        async def close_resources():
            await manager.drain_turns()
            await manager.drain_credential_refreshes()
            await manager.stop_all_held_processes()
            if api:
                await api.stop()
            registry.close()
            await db.close()
        try:
            await complete_cleanup(close_resources())
        except asyncio.CancelledError:
            if failure is None:
                raise
        except Exception as cleanup_error:
            if probe is not None:
                probe.record_cleanup_failure('fixture', cleanup_error)
            else:
                logger.error('Ordinary chat setup cleanup failed:\n%s', exception_diagnostic(cleanup_error, secrets=(bearer,)))
            if failure is None:
                raise
            failure.add_note('Chat fixture cleanup failed; see cleanup_failures.')


async def test_registered_natural_questions_use_ordinary_chat(ordinary_chat_probe, request):
    probe, registration = ordinary_chat_probe
    configured = request.config.getoption("knowledge_eval_output")
    assert configured, "--knowledge-eval-output must retain ordinary-chat evidence"
    output = Path(configured)
    for item in registration["questions"]:
        path = await collect_evaluation(probe, item["question"], case_id=item["id"], output=output)
        request.node.user_properties.append(("knowledge_evidence", str(path)))

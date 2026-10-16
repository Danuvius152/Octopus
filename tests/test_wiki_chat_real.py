"""Opt-in natural questions through the ordinary authenticated WebSocket entry.

No delegation or forced Wiki mode. A registration file is required before any
model request; answers remain subject to separate semantic review.
"""

import asyncio
import hashlib
import json
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
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.main import _wiki_error
from server.mcp_servers import wiki as wiki_tools
from server.routers.sessions import router as sessions_router
from server.routers.wiki import router as wiki_router
from server.routers.ws import router as ws_router
from server.sessions import SessionManager
from server.wiki import WikiError, registry
from tests.callback_api import start_callback_api
from tests.wiki_query_evidence import collect_evaluation, source_manifest

pytestmark = [pytest.mark.real, pytest.mark.real_codex, pytest.mark.knowledge_eval]


def guarded_chat_credential(manager, credential_id, home_dir):
    """Reject credential loss at the real invocation boundary, on every turn."""
    original = manager._resolve_credential
    expected = Path(home_dir).resolve()

    async def resolve(session, agent, harness):
        assert session.credential_id == credential_id, "registered credential selection changed"
        resolved = await original(session, agent, harness)
        assert resolved and resolved.home_dir and Path(resolved.home_dir).resolve() == expected, "selected credential unavailable; default-login fallback forbidden"
        return resolved

    return resolve


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

    async def ask(self, question):
        self.call_trace.clear()
        self.usage_observations.clear()
        self.last_events.clear()
        self.cleanup_failures.clear()
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
        except BaseException as error:
            try:
                await self.manager.interrupt(session.id)
                active = getattr(session, "_active_task", None)
                if active and not active.done():
                    await asyncio.wait_for(asyncio.shield(active), timeout=15)
            except BaseException as cleanup_error:
                self.cleanup_failures.append({"type": type(cleanup_error).__name__,
                                              "message": str(cleanup_error).replace(self.token, "<redacted>")})
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
    registration = json.loads(Path(configured).read_text())
    questions = registration["questions"]
    assert 1 <= len(questions) <= 4
    assert len({item["id"] for item in questions}) == len(questions)
    assert all(isinstance(item["question"], str) and item["question"].strip() for item in questions)
    root = Path(registration["source_root"]).resolve(strict=True)
    manifest_bytes = json.dumps(source_manifest(root), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(manifest_bytes).hexdigest() == registration["source_manifest_sha256"], "registered source version changed"
    credential_id = registration["credential_id"]
    assert isinstance(credential_id, str) and credential_id
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "auth_token", secrets.token_urlsafe(32))
    monkeypatch.setattr(deps, "_user_manager", None)
    monkeypatch.setattr(deps, "_accounts_exist", None)
    # Keep this fixture's observer handles separate from other test registries.
    monkeypatch.setattr(registry, "_stores", OrderedDict())
    monkeypatch.setattr(registry, "_retired_stores", {})
    work = tmp_path / "ordinary-work"
    work.mkdir()
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    api = None
    try:
        # Directory-backed credential resolution reads no provider secrets here.
        # Refuse fallback to an unrelated default CLI login.
        credential = await manager.resolve_credential_by_id(credential_id, style="home_dir")
        assert credential is not None, "selected credential home is unavailable; default-login fallback forbidden"
        monkeypatch.setattr(manager, "_resolve_credential", guarded_chat_credential(manager, credential_id, credential.home_dir))
        agent = await AgentManager(db).create_agent(
            name="Assistant", backend="codex", system_prompt="",
            model="gpt-6.1-sol", reasoning_effort="high", service_tier="default", mcp_servers=[],
        )
        curator = await db.get_system_agent()
        registry.configure(None, root, curator["id"])
        api = await start_callback_api(
            ws_router, sessions_router, wiki_router, websocket=True,
            overrides={get_session_manager: lambda: manager}, exception_handlers={WikiError: _wiki_error},
        )
        monkeypatch.setattr(settings, "port", api.port)
        probe = OrdinaryChatProbe(manager, agent["id"], root, work, api.port, settings.auth_token, credential_id)
        probe.input_context = {
            "transport": "authenticated WS send_message", "origin": "user", "parent_session_id": None,
            "forced_wiki_mode": False, "per_question_evaluator_prompt": False,
            "persona": "empty; normal framework/shared guides still supplied",
            "agent_name": "Assistant", "session_title": "Chat",
            "configured_profile": "gpt-6.1-sol/high/default", "credential_id": credential_id,
            "source_manifest_sha256": registration["source_manifest_sha256"],
            "environment": "isolated database, legacy-auth test host, registered fixed corpus; production account binding not exercised",
            "blind": False, "filesystem_isolation": "fresh cwd and memory; unrestricted process filesystem access is not a blind-test guarantee",
            "extra_cli_availability_model_probe": False,
        }
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
            current = getattr(session, "_current_request", None)
            probe.usage_observations.append({
                "session_id": session.id, "request_id": getattr(current, "request_id", probe.last_request_id),
                "attempt_id": attempt.attempt_id, "native_thread_id": event.session_id,
                "reused": attempt.reused, "is_error": event.is_error, "raw": deepcopy(event.raw),
            })
            await original_result(session, attempt, event)

        monkeypatch.setattr(manager, "_handle_result_event", record_usage)
        yield probe, registration
    finally:
        try:
            for session in list(manager.sessions.values()):
                await manager.interrupt(session.id)
            await manager.stop_all_held_processes()
        finally:
            try:
                if api:
                    await api.stop()
            finally:
                registry.close()
                await db.close()


async def test_registered_natural_questions_use_ordinary_chat(ordinary_chat_probe, request):
    probe, registration = ordinary_chat_probe
    configured = request.config.getoption("knowledge_eval_output")
    assert configured, "--knowledge-eval-output must retain ordinary-chat evidence"
    output = Path(configured)
    for item in registration["questions"]:
        path = await collect_evaluation(probe, item["question"], case_id=item["id"], output=output)
        request.node.user_properties.append(("knowledge_evidence", str(path)))

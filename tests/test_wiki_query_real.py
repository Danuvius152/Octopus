"""Opt-in natural knowledge evaluations; pytest success only records execution.

All evidence is synthetic. Review the versioned sources, answers and traces
separately; these cases do not grade wording or block ordinary releases.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from server.agent_manager import AgentManager
from server.aio import complete_cleanup
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.diagnostics import exception_diagnostic
from server.main import _wiki_error
from server.mcp_identity import mint
from server.mcp_servers import wiki as wiki_tools
from server.routers.wiki import router
from server.session_manager import SessionManager
from server.wiki import WikiError, registry
from server.wiki.workflow import guide
from tests.accounts import create_account
from tests.callback_api import start_callback_api
from tests.wiki_query_evidence import (
    collect_evaluation,
    guarded_credential,
    observe_invocations,
    observe_model_measurements,
)

pytestmark = [pytest.mark.real, pytest.mark.real_codex, pytest.mark.knowledge_eval]
REPORT = "Library/Research/vidu_realtime_report/技术报告.md"
PERSONAL = "Library/Me/information.md"
logger = logging.getLogger(__name__)


@dataclass
class QueryProbe:
    manager: SessionManager
    agent_id: str
    root: Path
    requests: list[tuple[str, dict]] = field(default_factory=list)
    responses: list[tuple[str, dict]] = field(default_factory=list)
    last_session_id: str | None = None
    last_request_id: str | None = None
    last_events: list[dict] = field(default_factory=list)
    call_trace: list[dict] = field(default_factory=list)
    usage_observations: list[dict] = field(default_factory=list)
    cleanup_failures: list[dict] = field(default_factory=list)
    credential_id: str = ''
    input_context: dict = field(default_factory=dict)
    model_observations: list[dict] = field(default_factory=list)

    def record_cleanup_failure(self, phase: str, error: BaseException) -> None:
        diagnostic = exception_diagnostic(error)
        self.cleanup_failures.append({"phase": phase, "diagnostic": diagnostic})
        logger.error("Wiki query %s cleanup failed:\n%s", phase, diagnostic)

    async def ask(self, question):
        self.requests.clear()
        self.responses.clear()
        self.call_trace.clear()
        self.usage_observations.clear()
        self.model_observations.clear()
        self.last_events.clear()
        self.last_session_id = self.last_request_id = None
        self.input_context['rendered_attempt_profiles'] = []
        session = await self.manager.create_session(
            self.agent_id, "Wiki query acceptance", str(self.root.parent), backend="codex",
            credential_id=self.credential_id or None,
        )
        self.last_session_id = session.id
        self.last_request_id = uuid.uuid4().hex
        self.last_events = []

        async def observe(event):
            if event.get("session_id") == session.id:
                self.last_events.append(deepcopy(event))

        subscription = f"wiki-query-{self.last_request_id}"
        self.manager.on_broadcast(subscription, observe)
        try:
            async with asyncio.timeout(240):
                receipt = await self.manager.start_message(
                    session.id, question, wiki_mode="read", request_id=self.last_request_id,
                )
                assert receipt.accepted and receipt.request_id == self.last_request_id
                completion = await receipt.wait()
                assert completion.recording_state == "committed"
                assert completion.outcome is not None and completion.outcome.status == "success"
        except BaseException as failure:
            async def retire_request(original_failure=failure):
                try:
                    # Cancelling the waiter does not cancel its accepted work.
                    # This entry captures and joins the original request's retirement.
                    await self.manager.cancel_request(session.id, self.last_request_id)
                except Exception as cleanup_error:
                    self.record_cleanup_failure("request", cleanup_error)
                    original_failure.add_note("Original request cleanup failed; see cleanup_failures.")

            try:
                await complete_cleanup(retire_request())
            except asyncio.CancelledError:
                # A repeated cancellation cannot release the original writer;
                # propagate the original failure after its cleanup has finished.
                pass
            raise
        finally:
            self.manager.remove_broadcast(subscription)
        events = self.last_events
        assert not any(event.get("type") == "error" for event in events), events
        outcomes = [event for event in events if event.get("type") == "result"]
        assert len(outcomes) == 1, events
        outcome = outcomes[0]
        assert outcome.get("terminal") is True and outcome.get("is_error") is False, outcome
        assert outcome.get("session_id") == session.id and outcome.get("request_id") == self.last_request_id, outcome
        assert isinstance(outcome.get("content"), dict), outcome
        assert outcome["content"].get("status") == "success" and outcome["content"].get("request_id") == self.last_request_id, outcome
        answer = "\n".join(event.get("content", "") for event in events if event.get("type") == "assistant_text")
        assert answer.strip(), events
        return answer

    def reads(self):
        result = []
        for operation, response in self.responses:
            if operation == "read":
                result.append((response["path"], response["start"], response["end"]))
            elif operation == "read-many":
                paths = {document["id"]: document["path"] for document in response["documents"]}
                result.extend((paths[part["doc"]], part["start"], part["end"]) for part in response["ranges"])
        return result


def default_query_root(tmp_path):
    root = tmp_path / "vault"
    (root / "Library/Me").mkdir(parents=True)
    for number in range(30):
        (root / f"Library/Me/fragment-{number:02}.md").write_text(f"# A{number:02}\n测试占位页，未记录个人事实。\n")
    (root / PERSONAL).write_text("# Z个人背景\n\n我在青峦大学学习，方向是合成视觉推理。\n")
    (root / "Library/Research/vidu_realtime_report").mkdir(parents=True)
    (root / REPORT).write_text(
        "---\naliases: [数字人研究, Vidu实时报告]\ntags: [视频蒸馏]\n---\n"
        "# Vidu S1 / S2 与实时视频蒸馏技术报告\n\n"
        "## 研究结论\nAvatar 研究区分吞吐量与端到端延迟，条件 A 仍未实证。\n\n"
        "## 实验条件\n" + "Experimental context is recorded; repeated prose is not independent evidence.\n" * 450
        + "\n## 最后一条限制\n这些结论只适用于测试环境，缺少真实任务下的对照验证。\n"
    )
    (root / "Library/Research/投影矩阵.md").write_text("# 投影矩阵\n\n本笔记把投影矩阵解释为坐标映射，须保留所用坐标系的约定。\n")
    (root / "Library/Research/职业讨论.md").write_text("# 职业讨论\n\n文章中的人物是一位工程师；这不是本用户的资料。\n")
    return root


@asynccontextmanager
async def query_probe_context(tmp_path, monkeypatch, registration=None):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    root = Path(registration['source_root']) if registration else default_query_root(tmp_path)
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path, root])
    manager = SessionManager()
    await manager.initialize(db)
    api, probe, failure = None, None, None
    async def close_resources():
        await manager.drain_turns()
        await manager.drain_credential_refreshes()
        await manager.stop_all_held_processes()
        if api is not None:
            await api.stop()
        await db.close()

    try:
        credential_id = registration['credential_id'] if registration else None
        if credential_id:
            await db.save_credential(credential_id, 'codex', 'Registered evaluation home', 'oauth', '',
                                     '2026-10-30T00:00:00+00:00', user_id=owner['id'])
        profile = registration['profile'] if registration else {}
        agent = await AgentManager(db).create_agent(name="Weber", backend="codex", system_prompt=guide("curator"),
                                                    credential_id=credential_id, **profile,
                                                    mcp_servers=[], user_id=owner["id"])
        registry.configure(owner["id"], root, agent["id"])
        probe = QueryProbe(manager, agent["id"], root, credential_id=credential_id or '')
        probe.input_context = {'transport': 'SessionManager.start_message', 'forced_wiki_mode': 'read',
                               'persona': 'curator guide; additional dedicated-query instructions',
                               'profile': profile, 'source_manifest_sha256': registration.get('source_manifest_sha256') if registration else None,
                               'blind': False, 'extra_cli_availability_model_probe': False}
        if credential_id:
            credential = await manager.resolve_credential_by_id(credential_id, backend='codex', user_id=owner['id'])
            assert credential is not None, 'selected credential unavailable; default-login fallback forbidden'
            monkeypatch.setattr(manager, '_resolve_credential', guarded_credential(manager, credential_id, credential.home_dir))
            observe_invocations(monkeypatch, probe)
        observe_model_measurements(monkeypatch, probe)
        original_result = manager._handle_result_event

        async def record_usage(session, attempt, event):
            current = session._run.request
            probe.usage_observations.append({
                "session_id": session.id, "request_id": getattr(current, 'request_id', None),
                "attempt_id": attempt.attempt_id, "native_thread_id": event.session_id,
                "reused": attempt.reused, "is_error": event.is_error,
                "raw": deepcopy(event.raw),
            })
            await original_result(session, attempt, event)

        monkeypatch.setattr(manager, "_handle_result_event", record_usage)
        api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, exception_handlers={WikiError: _wiki_error},
                                       readiness_token=mint("callback-api-readiness", user_id=owner["id"]))
        monkeypatch.setattr(settings, "port", api.port)

        # Namespace reload precedes wrapping the real HTTP/auth/read shim.
        original_call = wiki_tools.call

        def record_call(method, path, *, params=None, body=None):
            observation = {"method": method, "operation": path,
                           "params": deepcopy(params), "body": deepcopy(body)}
            probe.call_trace.append(observation)
            try:
                result = original_call(method, path, params=params, body=body)
            except Exception as error:
                observation["exception"] = {"type": type(error).__name__, "message": str(error)}
                raise
            observation["response"] = deepcopy(result)
            if "error" not in result:
                probe.requests.append((path, dict(params or body or {})))
                probe.responses.append((path, result))
            return result

        monkeypatch.setattr(wiki_tools, "call", record_call)
        yield probe
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            await complete_cleanup(close_resources())
        except asyncio.CancelledError:
            if failure is None:
                raise
        except Exception as cleanup_error:
            if probe is not None:
                probe.record_cleanup_failure("fixture", cleanup_error)
            else:
                logger.error('Wiki query setup cleanup failed:\n%s', exception_diagnostic(cleanup_error))
            if failure is None:
                raise
            failure.add_note("Fixture cleanup failed; see cleanup_failures.")


@pytest.fixture
async def query_probe(tmp_path, monkeypatch):
    async with query_probe_context(tmp_path, monkeypatch) as probe:
        yield probe


async def evaluate(query_probe, question, request):
    configured = request.config.getoption("knowledge_eval_output")
    output = Path(configured) if configured else query_probe.root.parent / "quality-evidence"
    path = await collect_evaluation(query_probe, question, case_id=request.node.nodeid, output=output)
    request.node.user_properties.append(("knowledge_evidence", str(path)))


@pytest.mark.parametrize("question", ["介绍我的背景", "我的职业是什么"])
async def test_codex_personal_facts_use_current_me_directory_without_guessing(query_probe, question, request):
    await evaluate(query_probe, question, request)
    assert_personal_read_workflow(query_probe)


def assert_personal_read_workflow(probe):
    """Read every current personal note, batching without repeated passages."""
    expected = {path.relative_to(probe.root).as_posix(): len(path.read_text())
                for path in (probe.root / "Library/Me").glob("*.md")}
    reads = probe.reads()
    assert {path for path, _, _ in reads} == set(expected), "Personal facts require the current directory's evidence"
    assert all(start == 0 and end == expected[path] for path, start, end in reads), "Short notes must be completely read"
    assert len(reads) == len(set(reads)), "The same personal passage was delivered repeatedly"
    assert any(operation == "read-many" for operation, _ in probe.responses), "Short personal notes should use the existing batch reader"


async def test_codex_knowledge_question_with_my_still_searches_the_term(query_probe, request):
    await evaluate(query_probe, "我的笔记里怎样解释投影矩阵？", request)


@pytest.mark.parametrize("question", ["数字人研究有哪些结论？", "那篇 Vidu 论文主要有哪些结论？"])
async def test_codex_locates_curated_report_by_topic_and_named_object(query_probe, question, request):
    await evaluate(query_probe, question, request)


async def test_codex_whole_report_evaluation_reads_all_current_pages(query_probe, request):
    await evaluate(query_probe, "完整读完那篇 Vidu 技术报告再评价其结论。", request)

"""Opt-in natural knowledge evaluations; pytest success only records execution.

All evidence is synthetic. Review the versioned sources, answers and traces
separately; these cases do not grade wording or block ordinary releases.
"""

import asyncio
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.main import _wiki_error
from server.mcp_identity import mint
from server.mcp_servers import wiki as wiki_tools
from server.routers.wiki import router
from server.session_manager import SessionManager
from server.wiki import WikiError, registry
from server.wiki.workflow import guide
from tests.accounts import create_account
from tests.callback_api import start_callback_api
from tests.wiki_query_evidence import collect_evaluation

pytestmark = [pytest.mark.real, pytest.mark.real_codex, pytest.mark.knowledge_eval]
REPORT = "Library/Research/vidu_realtime_report/技术报告.md"
PERSONAL = "Library/Me/information.md"


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

    async def ask(self, question):
        self.requests.clear()
        self.responses.clear()
        session = await self.manager.create_session(
            self.agent_id, "Wiki query acceptance", str(self.root.parent), backend="codex"
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


@pytest.fixture
async def query_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
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
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await AgentManager(db).create_agent(name="Weber", backend="codex", system_prompt=guide("curator"), mcp_servers=[], user_id=owner["id"])
    registry.configure(owner["id"], root, agent["id"])
    probe = QueryProbe(manager, agent["id"], root)
    original_result = manager._handle_result_event

    async def record_usage(session, attempt, event):
        probe.usage_observations.append({
            "session_id": session.id, "request_id": probe.last_request_id,
            "attempt_id": attempt.attempt_id, "native_thread_id": event.session_id,
            "reused": attempt.reused, "is_error": event.is_error,
            "raw": deepcopy(event.raw),
        })
        await original_result(session, attempt, event)

    monkeypatch.setattr(manager, "_handle_result_event", record_usage)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, exception_handlers={WikiError: _wiki_error},
                                   readiness_token=mint("callback-api-readiness", user_id=owner["id"]))
    monkeypatch.setattr(settings, "port", api.port)

    # start_callback_api reloads the namespaces and verifies them over HTTP.
    # Wrap the freshly loaded shim, retaining the real HTTP/auth/read gates.
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

    try:
        yield probe
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()


async def evaluate(query_probe, question, request):
    configured = request.config.getoption("knowledge_eval_output")
    output = Path(configured) if configured else query_probe.root.parent / "quality-evidence"
    path = await collect_evaluation(query_probe, question, case_id=request.node.nodeid, output=output)
    request.node.user_properties.append(("knowledge_evidence", str(path)))


@pytest.mark.parametrize("question", ["介绍我的背景", "我的职业是什么"])
async def test_codex_personal_facts_use_current_me_directory_without_guessing(query_probe, question, request):
    await evaluate(query_probe, question, request)


async def test_codex_knowledge_question_with_my_still_searches_the_term(query_probe, request):
    await evaluate(query_probe, "我的笔记里怎样解释投影矩阵？", request)


@pytest.mark.parametrize("question", ["数字人研究有哪些结论？", "那篇 Vidu 论文主要有哪些结论？"])
async def test_codex_locates_curated_report_by_topic_and_named_object(query_probe, question, request):
    await evaluate(query_probe, question, request)


async def test_codex_whole_report_evaluation_reads_all_current_pages(query_probe, request):
    await evaluate(query_probe, "完整读完那篇 Vidu 技术报告再评价其结论。", request)

"""Natural Codex queries: personal scope, named reports and complete reading.

All evidence is synthetic; no real personal notes are used by this suite.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.main import _wiki_error
from server.mcp_servers import wiki as wiki_tools
from server.routers.wiki import router
from server.session_manager import SessionManager
from server.wiki import WikiError, registry
from server.wiki.workflow import guide
from tests.callback_api import start_callback_api

pytestmark = [pytest.mark.real, pytest.mark.real_codex]
REPORT = "Library/Research/vidu_realtime_report/技术报告.md"
PERSONAL = "Library/Me/information.md"


def states_career_is_unrecorded(answer):
    # Explicit phrases avoid matching across a turn such as “没有履历，但
    # 职业明确是…”. This is a mechanical cue, not a semantic certification.
    missing = (
        "未记载", "未记录", "没有记录", "没有提到", "未提供", "未说明",
        "无法确定", "未明确", "不足以确定", "不足以判断", "无法判断",
        "没有明确记录", "没有明确记载", "没有职业记录", "没有职业记载",
    )
    return "职业" in answer and any(term in answer for term in missing)


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

    async def ask(self, question):
        self.requests.clear()
        self.responses.clear()
        session = await self.manager.create_session(
            self.agent_id, "Wiki query acceptance", str(self.root.parent), backend="codex"
        )
        self.last_session_id = session.id
        self.last_request_id = uuid.uuid4().hex
        self.last_events = []
        async with asyncio.timeout(240):
            async for event in self.manager.send_message(session.id, question, wiki_mode="read", request_id=self.last_request_id):
                self.last_events.append(event)
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
    manager = SessionManager()
    await manager.initialize(db)
    agent = await AgentManager(db).create_agent(name="Weber", backend="codex", system_prompt=guide("curator"), mcp_servers=[])
    registry.configure(None, root, agent["id"])
    probe = QueryProbe(manager, agent["id"], root)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, exception_handlers={WikiError: _wiki_error})
    monkeypatch.setattr(settings, "port", api.port)

    # start_callback_api reloads the namespaces and verifies them over HTTP.
    # Wrap the freshly loaded shim, retaining the real HTTP/auth/read gates.
    original_call = wiki_tools.call

    def record_call(method, path, *, params=None, body=None):
        result = original_call(method, path, params=params, body=body)
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


@pytest.mark.parametrize("question", ["介绍我的背景", "我的职业是什么"])
async def test_codex_personal_facts_use_current_me_directory_without_guessing(query_probe, question):
    answer = await query_probe.ask(question)
    locating = [(operation, params) for operation, params in query_probe.requests if operation in {"documents", "search", "read"}]
    assert locating[0][0] == "documents" and locating[0][1].get("directory") == "Library/Me", locating
    first_limit = int(locating[0][1].get("limit", 30))
    if first_limit < 31:
        assert any(operation == "documents" and (int(params.get("offset", 0)) > 0 or params.get("cursor")) for operation, params in locating), locating
    assert any(path == PERSONAL for path, _, _ in query_probe.reads()), query_probe.requests
    assert all(path.startswith("Library/Me/") for path, _, _ in query_probe.reads())
    if "背景" in question:
        assert "青峦大学" in answer and "合成视觉推理" in answer, answer
    else:
        assert states_career_is_unrecorded(answer), answer


async def test_codex_knowledge_question_with_my_still_searches_the_term(query_probe):
    answer = await query_probe.ask("我的笔记里怎样解释投影矩阵？")
    assert any(operation == "search" for operation, _ in query_probe.requests)
    assert any(path == "Library/Research/投影矩阵.md" for path, _, _ in query_probe.reads())
    assert "坐标" in answer, answer


@pytest.mark.parametrize("question", ["数字人研究有哪些结论？", "那篇 Vidu 论文主要有哪些结论？"])
async def test_codex_locates_curated_report_by_topic_and_named_object(query_probe, question):
    answer = await query_probe.ask(question)
    assert any(path == REPORT for path, _, _ in query_probe.reads()), query_probe.requests
    assert "延迟" in answer and ("条件" in answer or "实证" in answer), answer


async def test_codex_whole_report_evaluation_reads_all_current_pages(query_probe):
    answer = await query_probe.ask("完整读完那篇 Vidu 技术报告再评价其结论。")
    spans = sorted((start, end) for path, start, end in query_probe.reads() if path == REPORT)
    covered = 0
    for start, end in spans:
        assert start <= covered, spans
        covered = max(covered, end)
    document = registry.store(None).read(REPORT, length=1)
    assert covered == document["characters"], spans
    assert "测试环境" in answer, answer

"""Signed-in Codex proves the task-scoped HTTP tools, including native helpers."""

import asyncio
import uuid
from copy import deepcopy

import pytest

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.main import _wiki_error
from server.routers.wiki import router
from server.session_manager import SessionManager
from server.wiki import WikiError, registry
from tests.callback_api import start_callback_api

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


async def _request_events(manager, session_id, prompt, **kwargs):
    request_id = uuid.uuid4().hex
    events = []

    async def observe(event):
        if event.get("session_id") == session_id and event.get("request_id") == request_id:
            events.append(deepcopy(event))

    manager.on_broadcast(request_id, observe)
    try:
        receipt = await manager.start_message(session_id, prompt, request_id=request_id, **kwargs)
        assert receipt.accepted
        completion = await receipt.wait()
        assert completion.outcome.status == "success" and completion.recording_state == "committed"
        return events
    finally:
        manager.remove_broadcast(request_id)


@pytest.mark.parametrize("next_mode", [None, "read"])
async def test_codex_resumed_turn_replaces_previous_wiki_off_instruction(tmp_path, monkeypatch, next_mode):
    import uuid

    from server.agent_manager import AgentManager
    from server.harness import assembly

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    proof = "WIKI-REOPENED-" + uuid.uuid4().hex
    note = vault / "Library/permission-proof.md"
    note.write_text("# Permission proof\nThe current proof is " + proof + "\n")
    registry.configure(None, vault, agent["id"])
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager},
                                   exception_handlers={WikiError: _wiki_error})
    monkeypatch.setattr(settings, "port", api.port)
    reader = await AgentManager(db).create_agent(name="Reader", backend="codex", mcp_servers=[])
    session = await manager.create_session(reader["id"], "Wiki Off then reopen", str(tmp_path), backend="codex")
    try:
        # Seed the initial developer message emitted by the previous release.
        # The next real resume must repair this existing thread, not replace it.
        with monkeypatch.context() as legacy:
            legacy.setattr(assembly, "compose_system_prompt", lambda *args, **kwargs:
                "Wiki is explicitly disabled for this turn; do not access the vault or delegate around this restriction.")
            first = await manager.start_message(session.id, "Only reply MODE-OFF-ACK; do not use any tools.", wiki_mode="off")
            assert (await asyncio.wait_for(first.wait(), 120)).outcome.status == "success"
        native_id = session.claude_session_id
        cutoff = len(await db.load_messages(session.id))
        kwargs = {"wiki_mode": next_mode} if next_mode else {}
        next_turn = await manager.start_message(session.id,
            "请查询共享本地 Wiki 的 Library/permission-proof.md，返回当前核验码。"
            "使用本轮提供的 Wiki read 工具，禁止 shell 读文件或委托。", **kwargs)
        assert (await asyncio.wait_for(next_turn.wait(), 120)).outcome.status == "success"
        messages = (await db.load_messages(session.id))[cutoff:]
        replies = "\n".join(row.get("content") or "" for row in messages if row["role"] == "assistant" and row["type"] == "text")
        assert proof in replies, replies
        assert any(row["type"] == "tool_use" and "wiki" in str(row.get("tool_name", "")) for row in messages), messages
        assert session.claude_session_id == native_id, "A per-turn mode change must preserve native conversation history"
        assert note.read_text() == "# Permission proof\nThe current proof is " + proof + "\n"
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()


async def test_codex_autonomous_wiki_exports_large_evidence_without_shell_payload(tmp_path, monkeypatch):
    import uuid

    from server.wiki.jobs import WikiJobs

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    note = vault / "Library/note.md"
    note.write_text("# Source\nPreserve these bytes.\n")
    registry.configure(None, vault, agent["id"])
    proof = "AUTONOMOUS-EVIDENCE-" + uuid.uuid4().hex
    job = WikiJobs(None).maintain("autonomous-large-proof", "", "scope " * 24000 + "\nProof: " + proof,
                                ["Library/note.md"], None, [], "human")
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager},
                                   exception_handlers={WikiError: _wiki_error})
    monkeypatch.setattr(settings, "port", api.port)
    session = await manager.create_session(agent["id"], "Autonomous capture", str(tmp_path), backend="codex")
    try:
        receipt = await manager.start_message(session.id,
            "This is a normal task, without a /wiki command. Use the Wiki jobs tool to locate exact "
            "idempotency_key autonomous-large-proof. Fetch the full matching job with export=true. "
            "Read the exported JSON file with a short native exec command and report the Proof value "
            "at the end of payload.request.scope. Do not put the JSON in a command, print the full "
            "scope, modify the vault, run the job, or ask the user to enable Wiki. Only report the proof.")
        completion = await asyncio.wait_for(receipt.wait(), 150)
        assert completion.outcome.status == "success", completion.outcome
        messages = await db.load_messages(session.id)
        replies = "\n".join(row.get("content", "") for row in messages if row["role"] == "assistant" and row["type"] == "text")
        assert proof in replies, replies
        tools = [row for row in messages if row["type"] == "tool_use"]
        assert any("wiki" in str(row.get("tool_name", "")) and "jobs" in str(row.get("tool_name", "")) for row in tools), tools
        assert not any("Argument list too long" in (row.get("content") or "") for row in messages)
        assert note.read_text() == "# Source\nPreserve these bytes.\n"
        assert WikiJobs(None).get(job["id"])["status"] == "queued"
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()


async def test_codex_explicit_wiki_and_native_helper_then_ordinary_task(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "证据.md").write_text(
        "# 工程决定\n\n唯一核验码：WIKI-COBALT-9472\n采用显式授权以避免上下文污染。\n"
    )
    registry.configure(None, vault, agent["id"])
    api = await start_callback_api(
        router,
        overrides={get_session_manager: lambda: manager},
        exception_handlers={WikiError: _wiki_error},
    )
    monkeypatch.setattr(settings, "port", api.port)
    session = await manager.create_session(
        agent["id"], "Wiki real", str(tmp_path), backend="codex"
    )
    try:
        async with asyncio.timeout(240):
            events = await _request_events(
                manager,
                session.id,
                "这是显式本地知识查询。请用 spawn_agent 创建一个原生助手，让它用 mcp__wiki__search 搜索工程决定，再用 mcp__wiki__read 查证，返回核验码、revision 与行号。主 agent 等待助手完成后回答。禁止 shell 读库。",
                wiki_mode="read",
            )
        assert any(event.get("type") == "subagent" for event in events), events
        text = "\n".join(str(event.get("content", "")) for event in events)
        assert "WIKI-COBALT-9472" in text, text
        assert not any(event.get("type") == "error" for event in events), events
        async with asyncio.timeout(120):
            ordinary = await _request_events(
                manager,
                session.id,
                "这是普通讨论，不查知识库，也不操作文件。只回复 ORDINARY-OK。",
            )
        assert not any(
            "wiki" in str(event.get("tool", ""))
            for event in ordinary
            if event.get("type") == "tool_use"
        )
        assert "ORDINARY-OK" in str(ordinary)
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()


@pytest.mark.parametrize("request_supplement", [False, True])
async def test_codex_weber_maintains_only_new_material(tmp_path, monkeypatch, request_supplement):
    from server.agent_manager import AgentManager
    from server.wiki.jobs import WikiJobs, worker
    from server.wiki.workflow import guide

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await AgentManager(db).create_agent(
        name="Weber", backend="codex", system_prompt=guide("curator")
    )
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    old = "# 已有核心判断\n只有条件 A 成立时，这个判断才可能成立。\n"
    (vault / "Library/旧观点.md").write_text(old)
    registry.configure(None, vault, agent["id"])
    api = await start_callback_api(
        router,
        overrides={get_session_manager: lambda: manager},
        exception_handlers={WikiError: _wiki_error},
    )
    monkeypatch.setattr(settings, "port", api.port)
    material = "# 条件判断\n\n条件 A 是前提，不是已证事实。结论只能说可能成立，不能说一定成立。\n"
    jobs = WikiJobs(None)
    try:
        await worker.start(manager)
        job = jobs.maintain(
            "real-new-note",
            material,
            "只整理这篇新增笔记。保留条件和判断强度；查找已有判断的关联，但不可改旧文。"
            + ("用户明确要求在开头补充 Weber 的 Key note，总结前提与未验证的限制；原文仍逐字保留。" if request_supplement else ""),
            [],
            "Library/新判断.md",
            [],
            "human",
        )
        async with asyncio.timeout(240):
            while (outcome := jobs.get(job["id"]))["status"] in {"queued", "running"}:
                await asyncio.sleep(0.2)
        assert outcome["status"] == "completed", outcome
        assert outcome["progress"]["completed"] == 1
        assert (vault / "Library/旧观点.md").read_text() == old
        current = jobs.store.read("Library/新判断.md")
        assert material.split("\n\n", 1)[1] in current["content"], current
        assert outcome["receipts"], outcome
        assert outcome["items"][0]["output_revision"] == current["revision"]
        assert outcome["items"][0]["index_complete"] is True
        if request_supplement:
            assert "## Key note" in current["content"], current
        # Preserve actual words and conditions under either compatible format;
        # private legacy ownership fields are not the v2 acceptance contract.
        assert "不是已证事实" in current["content"]
        assert "不能说一定成立" in current["content"]
    finally:
        await worker.stop()
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()

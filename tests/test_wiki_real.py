"""Signed-in Codex proves the task-scoped HTTP tools, including native helpers."""

import asyncio
import uuid
from copy import deepcopy

import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.main import _wiki_error
from server.mcp_identity import mint
from server.routers.wiki import router
from server.session_manager import SessionManager
from server.wiki import WikiError, registry
from server.wiki.jobs import WikiJobs
from tests.accounts import create_account
from tests.callback_api import start_callback_api

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


def observe_reads(store, monkeypatch):
    """Retain actual tool data, without grading the model's final wording."""
    observed = []
    original = store.read

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(store, "read", read)
    return observed


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


async def test_codex_resumed_turn_replaces_previous_wiki_off_instruction(tmp_path, monkeypatch):
    """A repaired ordinary turn reads Wiki and exports an oversized job."""
    from server.harness import assembly

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    proof = "WIKI-REOPENED-" + uuid.uuid4().hex
    note = vault / "Library/permission-proof.md"
    note.write_text("# Permission proof\nThe current proof is " + proof + "\n")
    registry.configure(owner["id"], vault, agent["id"])
    export_proof = "AUTONOMOUS-EVIDENCE-" + uuid.uuid4().hex
    job = WikiJobs(owner["id"]).maintain("autonomous-large-proof", "", "scope " * 24000 + "\nProof: " + export_proof,
                                ["Library/permission-proof.md"], None, [], "human")
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager},
                                   exception_handlers={WikiError: _wiki_error},
                                   readiness_token=mint("callback-api-readiness", user_id=owner["id"]))
    monkeypatch.setattr(settings, "port", api.port)
    reader = await AgentManager(db).create_agent(
        name="Reader", backend="codex", mcp_servers=[], reasoning_effort="low",
        user_id=owner["id"],
    )
    session = await manager.create_session(reader["id"], "Wiki Off then reopen", str(tmp_path), backend="codex")
    try:
        # Seed the initial developer message emitted by the previous release.
        # The next real resume must repair this existing thread, not replace it.
        with monkeypatch.context() as legacy:
            legacy.setattr(assembly, "compose_system_prompt", lambda *args, **kwargs:
                "Wiki is explicitly disabled for this turn; do not access the vault or delegate around this restriction.")
            first = await manager.start_message(session.id, "Only reply MODE-OFF-ACK; do not use any tools.", wiki_mode="read")
            assert (await asyncio.wait_for(first.wait(), 120)).outcome.status == "success"
        native_id = session.claude_session_id
        cutoff = len(await db.load_messages(session.id))
        observed = observe_reads(registry.store(owner["id"]), monkeypatch)
        next_turn = await manager.start_message(session.id,
            "请查询共享本地 Wiki 的 Library/permission-proof.md，返回当前核验码。"
            "使用本轮提供的 Wiki read 工具读笔记，禁止 shell 读取笔记或委托。"
            "This is a normal task, without a /wiki command. Also use the Wiki jobs tool to locate exact "
            "idempotency_key autonomous-large-proof and fetch the full matching job with export=true. "
            "Read only that exported JSON artifact with a short native exec command and report the Proof value "
            "at the end of payload.request.scope. Do not put JSON in a command, print the full scope, "
            "modify the vault, run the job, or ask the user to enable Wiki.")
        assert (await asyncio.wait_for(next_turn.wait(), 150)).outcome.status == "success"
        messages = (await db.load_messages(session.id))[cutoff:]
        assert any(row["path"] == "Library/permission-proof.md" and proof in row["content"] for row in observed)
        assert any(row["type"] == "tool_use" and "wiki" in str(row.get("tool_name", "")) for row in messages), messages
        tools = [row for row in messages if row["type"] == "tool_use"]
        assert any("wiki" in str(row.get("tool_name", "")) and "jobs" in str(row.get("tool_name", "")) for row in tools), tools
        shell_calls = {row["tool_use_id"] for row in tools if row["tool_name"] == "Bash"}
        assert any(row["type"] == "tool_result" and row["tool_use_id"] in shell_calls
                   and export_proof in (row.get("content") or "") for row in messages)
        assert not any("Argument list too long" in (row.get("content") or "") for row in messages)
        assert session.claude_session_id == native_id, "A per-turn mode change must preserve native conversation history"
        assert note.read_text() == "# Permission proof\nThe current proof is " + proof + "\n"
        assert WikiJobs(owner["id"]).get(job["id"])["status"] == "queued"
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()


async def test_codex_explicit_wiki_and_native_helper(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "wiki-state"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    # The task grants add the real Wiki namespace; native helpers do not need
    # the unrelated bg/ask/research/schedule namespaces.
    await db.update_agent(agent["id"], reasoning_effort="low", mcp_servers=[])
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "证据.md").write_text(
        "# 工程决定\n\n唯一核验码：WIKI-COBALT-9472\n采用显式授权以避免上下文污染。\n"
    )
    registry.configure(owner["id"], vault, agent["id"])
    api = await start_callback_api(
        router,
        overrides={get_session_manager: lambda: manager},
        exception_handlers={WikiError: _wiki_error},
        readiness_token=mint("callback-api-readiness", user_id=owner["id"]),
    )
    monkeypatch.setattr(settings, "port", api.port)
    session = await manager.create_session(
        agent["id"], "Wiki real", str(tmp_path), backend="codex"
    )
    observed = observe_reads(registry.store(owner["id"]), monkeypatch)
    # Capture the same real normalized stream consumed by SessionManager; the
    # wire identity checks share the Wiki helper's two rounds.
    runs = []
    harness = get_harness("codex")
    create_run = harness.create_run

    def observe_run(config=None):
        backend = create_run(config)
        raw, contexts = [], []
        stream, bind = backend.stream, backend.bind_tool_context

        def bind_context(request_id, attempt_id):
            contexts.append((request_id, attempt_id))
            return bind(request_id, attempt_id)

        async def collect():
            async for event in stream():
                raw.append(event)
                yield event

        monkeypatch.setattr(backend, "bind_tool_context", bind_context)
        monkeypatch.setattr(backend, "stream", collect)
        runs.append((backend, raw, contexts))
        return backend

    monkeypatch.setattr(harness, "create_run", observe_run)
    try:
        async with asyncio.timeout(240):
            events = await _request_events(
                manager,
                session.id,
                "这是显式本地知识查询。请用 spawn_agent 创建恰好一个原生助手 native_card_probe，"
                "让它用 mcp__wiki__search 搜索工程决定，再用 mcp__wiki__read 查证，返回核验码、revision 与行号。"
                "主 agent 收到第一轮结果后，对同一助手调用一次 followup_task，让它只回复 SECOND_NATIVE_ROUND，"
                "第二轮不使用工具。等待第二轮完成后，主 agent 回复 NATIVE_CARD_ACCEPTED。禁止 shell 读库或创建其他助手。",
                wiki_mode="read",
            )
        assert any(event.get("type") == "subagent" for event in events), events
        assert any(row["path"] == "证据.md" and "WIKI-COBALT-9472" in row["content"] for row in observed)
        assert not any(event.get("type") == "error" for event in events), events
        assert len(runs) == 1
        backend, raw, contexts = runs[0]
        assert len(contexts) == 1
        calls = [event for event in raw if event.type == "tool_use" and event.tool_name in {
            "subagent:spawn_agent", "subagent:followup_task",
        }]
        assert [event.tool_name for event in calls] == ["subagent:spawn_agent", "subagent:followup_task"]
        assert all(event.runtime_metadata["attempt_id"] == contexts[0][1] for event in calls)
        finished = [event.subagent for event in raw if event.type == "subagent" and event.subagent.status == "completed"]
        assert len(finished) == 2
        assert len({child.task_id for child in finished}) == 1
        assert [child.tool_call_id for child in finished] == [call.runtime_metadata["tool_call_id"] for call in calls]
        assert finished[0].tool_call_id != finished[1].tool_call_id
        assert all(not child.summary and not child.prompt for child in finished)
        assert "NATIVE_CARD_ACCEPTED" in "".join(event.content or "" for event in raw if event.type == "text")
        assert raw[-1].type == "result" and not raw[-1].is_error
        assert "Sent prompt with event ID:" not in backend.stderr_text
    finally:
        await manager.stop_all_held_processes()
        await api.stop()
        await db.close()

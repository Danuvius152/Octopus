"""Delegation contracts through the real dispatcher and durable request API."""

import asyncio
import json
import re
import sys
import uuid
from dataclasses import replace

import pytest

from server.agent_manager import AgentManager
from server.harness import get_harness
from server.mcp_identity import scope_is_live, verify
from server.wiki import registry
from tests.harness_profiles import session_turn_argv
from tests.test_delegation_work import work_runtime as work_runtime


@pytest.fixture
async def controlled_runtime(work_runtime, monkeypatch, tmp_path):
    _, _, parent, starts = work_runtime
    vault = tmp_path / "vault"
    vault.mkdir()
    registry.configure(parent.user_id, vault, parent.agent_id)

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\nfrom pathlib import Path\n"
        code += f"print(json.dumps({{'type':'thread.started','thread_id':{uuid.uuid4().hex!r}}}),flush=True)\n"
        gate = re.search(r"\bGATE:(\w+)\b", context.prompt)
        if gate:
            code += f"while not Path({str(tmp_path / gate[1])!r}).exists(): time.sleep(.01)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'reply'}}),flush=True)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=session_turn_argv(harness.profile, argv),
                                                  prepare_resume=None, activity_observer=None))
    yield work_runtime


async def new_parent(manager, parent):
    return await manager.create_session(parent.agent_id, "Codex caller", parent.working_dir, backend="codex")


async def live_scope(context):
    credential = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    scope = verify(credential)
    assert scope is not None
    async with asyncio.timeout(5):
        while not scope_is_live(scope):
            await asyncio.sleep(.01)
    return scope


async def request_context(database, session_id):
    rows = await database.load_messages(session_id)
    requests = [row for row in rows if row["role"] == "user" and row.get("tool_input")]
    assert requests
    return requests[-1]["tool_input"]["request_context"]


async def test_child_execution_retains_the_trusted_source_contract(controlled_runtime):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    original = "/wiki GATE:root question needing local and public evidence"
    await manager.start_message(parent.id, original)
    caller = await asyncio.wait_for(starts.get(), 5)
    scope = await live_scope(caller)
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="GATE:child examine one part", scope=scope,
    )
    await asyncio.wait_for(starts.get(), 5)
    recorded = await request_context(manager.db, child.delegation_id)
    context = recorded["knowledge"]["value"]
    assert context["sources"] == ["wiki", "web"]
    assert context["trusted_request"] == original
    assert context["mode"] == "auto" and context["maintenance"]["may_submit"] is True


async def test_terminal_return_restores_its_direct_parent_contract_after_retirement_and_a_new_root(
    controlled_runtime, tmp_path,
):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    original = "/wiki maintain Library/x.md Library/y.md\nGATE:root"
    first = await manager.start_message(parent.id, original)
    caller = await live_scope(await asyncio.wait_for(starts.get(), 5))
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker",
        request="/wiki maintain Library/x.md\nGATE:child", scope=caller,
    )
    await asyncio.wait_for(starts.get(), 5)
    child_context = (await request_context(manager.db, child.delegation_id))["knowledge"]["value"]
    assert child_context["maintenance"]["required_selections"] == [original, "/wiki maintain Library/x.md\nGATE:child"]
    saved = json.loads(await manager.db.load_delegation_state(
        child.delegation_id, parent_session_id=parent.id, user_id=parent.user_id,
    ))
    assert saved["knowledge_context"]
    assert "knowledge_context" not in child.to_public_dict() and "owned_context" not in child.to_public_dict()
    (tmp_path / "root").touch()
    assert (await first.wait()).outcome.status == "success"
    assert not scope_is_live(caller)
    newer = await manager.start_message(parent.id, "/wiki GATE:newer unrelated read question")
    await asyncio.wait_for(starts.get(), 5)
    (tmp_path / "child").touch()
    async with asyncio.timeout(5):
        while not manager.dispatch_snapshot(parent.id)["items"]:
            await asyncio.sleep(.01)
    (tmp_path / "newer").touch()
    assert (await newer.wait()).outcome.status == "success"
    await asyncio.wait_for(starts.get(), 5)
    returned = await request_context(manager.db, parent.id)
    context = returned["knowledge"]["value"]
    assert returned["source"] == "delegation" and returned["work_id"] is None
    assert context["trusted_request"] == original
    assert context["maintenance"]["required_selections"] == [original]
    assert context["mode"] == "maintain"
    assert returned["request_id"] != context["origin"]["request_id"]


async def test_question_return_keeps_the_parents_full_scope(controlled_runtime, tmp_path):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    original = "/wiki maintain Library/x.md Library/y.md\nGATE:root"
    first = await manager.start_message(parent.id, original)
    caller = await live_scope(await asyncio.wait_for(starts.get(), 5))
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="/wiki maintain Library/x.md\nGATE:child",
        scope=caller,
    )
    child_scope = await live_scope(await asyncio.wait_for(starts.get(), 5))
    question = await manager.create_pending_question(
        child.delegation_id, [{"question": "Which condition applies?", "options": [{"label": "X"}, {"label": "Y"}]}],
        scope=child_scope,
    )
    assert question is not None
    (tmp_path / "root").touch()
    assert (await first.wait()).outcome.status == "success"
    await asyncio.wait_for(starts.get(), 5)
    returned = await request_context(manager.db, parent.id)
    assert returned["source"] == "delegation"
    assert returned["knowledge"]["value"]["maintenance"]["required_selections"] == [original]
    assert manager.has_pending_question(child.delegation_id, question)


async def test_nested_return_restores_the_immediate_parent_without_losing_its_narrowing(controlled_runtime, tmp_path):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    await AgentManager(manager.db).create_agent(name="Grandchild", backend="codex", user_id=parent.user_id)
    original = "/wiki maintain Library/x.md Library/y.md\nGATE:root"
    await manager.start_message(parent.id, original)
    root_scope = await live_scope(await asyncio.wait_for(starts.get(), 5))
    narrow = "/wiki maintain Library/x.md\nGATE:middle"
    middle = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request=narrow, scope=root_scope,
    )
    middle_scope = await live_scope(await asyncio.wait_for(starts.get(), 5))
    leaf = await delegations.start_delegation(
        parent_session_id=middle.delegation_id, agent_name="Grandchild",
        request="/wiki maintain Library/y.md\nGATE:leaf", scope=middle_scope,
    )
    await asyncio.wait_for(starts.get(), 5)
    leaf_context = (await request_context(manager.db, leaf.delegation_id))["knowledge"]["value"]
    assert leaf_context["maintenance"]["required_selections"] == [original, narrow, "/wiki maintain Library/y.md\nGATE:leaf"]
    (tmp_path / "leaf").touch()
    async with asyncio.timeout(5):
        while not manager.dispatch_snapshot(middle.delegation_id)["items"]:
            await asyncio.sleep(.01)
    (tmp_path / "middle").touch()
    middle_return = await live_scope(await asyncio.wait_for(starts.get(), 5))
    assert middle_return.session_id == middle.delegation_id
    assert middle_return.work_id == middle.work_id
    returned = await request_context(manager.db, middle.delegation_id)
    assert returned["knowledge"]["value"]["maintenance"]["required_selections"] == [original, narrow]
    assert returned["request_id"] != middle.owned_context.return_binding.reference.request_id


async def test_follow_up_uses_a_new_caller_contract_and_keeps_the_previous_round_immutable(controlled_runtime):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    await manager.interrupt(parent.id)
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="/wiki first independent brief",
    )
    await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while first.state == "running" or manager.has_pending_or_active_work(first.delegation_id, first.work_id):
            await asyncio.sleep(.01)
    original_owned = first.owned_context
    await manager.start_message(parent.id, "/wiki maintain Library/y.md\nGATE:caller")
    caller = await live_scope(await asyncio.wait_for(starts.get(), 5))
    second = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=first.delegation_id,
        request="/wiki maintain Library/y.md\nGATE:followup", scope=caller,
    )
    await asyncio.wait_for(starts.get(), 5)
    recorded = (await request_context(manager.db, second.delegation_id))["knowledge"]["value"]
    assert first.owned_context is original_owned
    assert first.work_id != second.work_id and first is not second
    assert original_owned.execution.mode == "auto"
    assert recorded["mode"] == "maintain"
    assert recorded["trusted_request"] == "/wiki maintain Library/y.md\nGATE:caller"
    assert second.owned_context.return_binding.reference.request_id != original_owned.return_binding.reference.request_id


async def test_legacy_round_without_a_contract_retains_its_result_without_an_automatic_request(
    controlled_runtime, tmp_path, monkeypatch,
):
    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    await manager.interrupt(parent.id)
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="GATE:legacy",
    )
    await asyncio.wait_for(starts.get(), 5)
    # Represent an in-flight record made by the old format. There is no
    # model-facing operation that can clear this server-owned value.
    monkeypatch.setattr(child, "owned_context", None)
    (tmp_path / "legacy").touch()
    async with asyncio.timeout(5):
        while child.state == "running" or manager.has_pending_or_active_work(child.delegation_id, child.work_id):
            await asyncio.sleep(.01)
    assert child.state == "completed"
    await delegations.confirm_recording()
    assert manager.dispatch_snapshot(parent.id)["items"] == []
    assert starts.empty()
    history = await manager.db.load_messages(child.delegation_id)
    assert any(row["content"] == "reply" for row in history)
    saved = json.loads(await manager.db.load_delegation_state(
        child.delegation_id, parent_session_id=parent.id, user_id=parent.user_id,
    ))
    assert saved["terminal_disposition"] == "unbound"
    assert saved["knowledge_context"] is None
    notices = [row for row in await manager.db.load_messages(parent.id) if row["role"] == "system"]
    assert len(notices) == 1 and child.delegation_id in notices[0]["content"]
    assert not any(row["role"] == "user" for row in await manager.db.load_messages(parent.id))


async def test_cancelled_delegation_retires_its_owned_research_work(controlled_runtime, monkeypatch):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager

    manager, delegations, parent, starts = controlled_runtime
    parent = await new_parent(manager, parent)
    await manager.interrupt(parent.id)
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="GATE:child",
    )
    scope = await live_scope(await asyncio.wait_for(starts.get(), 5))
    began, retired = asyncio.Event(), asyncio.Event()

    async def pipeline(*args, **kwargs):
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()

    research = ResearchManager()
    research.bind(manager, manager.db)
    monkeypatch.setattr(research_module, "run_research", pipeline)
    monkeypatch.setattr("server.delegations.research_manager", research, raising=False)
    try:
        job = await research.start(child.delegation_id, "Research this subquestion", scope=scope)
        await asyncio.wait_for(began.wait(), 5)
        await delegations.cancel_delegation(child.delegation_id, work_id=child.work_id)
        await asyncio.wait_for(retired.wait(), .5)
        async with asyncio.timeout(5):
            while (await manager.db.get_research_job(job["id"]))["status"] == "running":
                await asyncio.sleep(.01)
        assert (await manager.db.get_research_job(job["id"]))["status"] == "cancelled"
    finally:
        await research.shutdown()

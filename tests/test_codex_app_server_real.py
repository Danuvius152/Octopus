"""Live product gates for the G0-pinned ordinary Codex transport."""

import asyncio
from pathlib import Path

import pytest

from server import monitor
from server.attachments import save_upload
from server.database import Database
from server.monitor import MetricsStore, query
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.forking import ForkingMixin
from tests.accounts import create_account
from tests.cli_gate import codex_cli_works
from tests.codex_legacy_history import seed_exec_history

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


@pytest.fixture
async def live_product(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "collaboration.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent()
    await database.update_agent(agent["id"], mcp_servers=[], reasoning_effort="low")
    manager = SessionManager()
    await manager.initialize(database)
    metrics = MetricsStore(str(tmp_path / "collaboration-metrics.db"))
    await metrics.initialize()
    monkeypatch.setattr(monitor, "monitor", metrics)
    session = await manager.create_session(agent["id"], "Live collaboration", str(tmp_path), backend="codex")
    try:
        yield manager, database, session, metrics
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=10)
        await manager.close_session_owners()
        await metrics.stop()
        await database.close()


async def live_run(manager, session, prompt):
    receipt = await manager.start_message(session.id, prompt)
    async with asyncio.timeout(30):
        while session._run is None or session._run.backend is None:
            await asyncio.sleep(.01)
    return receipt, session._run.backend


async def test_live_nested_wait_followup_keeps_one_root_and_collected_rounds(live_product):
    manager, database, session, metrics = live_product
    receipt, run = await live_run(manager, session,
        "This user explicitly authorizes exactly two native helpers for this verification. "
        "Use collaboration.spawn_agent to create middle with fork_turns=none. Tell middle to spawn leaf "
        "with fork_turns=none, ask leaf to compute 29*31 and reply NATIVE_LEAF=899, remain active with "
        "wait_agent until leaf returns, then reply NATIVE_MIDDLE=899. You remain active using wait_agent "
        "until middle returns. Next use followup_task on the SAME middle to request the reply "
        "NATIVE_FOLLOWUP=899 without any tools. Remain active using wait_agent until that second round "
        "returns. Finally reply only NATIVE_ROOT=899. Do not finish before both middle rounds complete. "
        "Use inherited model and effort; do not create any other helpers or use files.")
    completion = await asyncio.wait_for(receipt.wait(), 180)
    assert completion.outcome.status == "success", completion
    assert await receipt.execution.wait_resources() == ("proven",)
    result = await database.read_session_result(session.id, receipt.request_id)
    assert "NATIVE_ROOT=899" in result.reply_text(), result
    assert all(row["runtime_metadata"]["native"]["thread_id"] == session.claude_session_id
               for row in await database.load_messages(session.id) if row["seq"] in result.message_seqs)
    rows = await database.load_messages(session.id)
    cards = {row["tool_input"]["card_id"]: row["tool_input"] for row in rows if row["type"] == "native_subagent"}
    assert len(cards) == 3 and all(card["status"] == "completed" for card in cards.values()), cards
    assert len({card["native_thread_id"] for card in cards.values()}) == 2
    assert any(card["parent_thread_id"] != session.claude_session_id for card in cards.values())
    assert any(row["content"] == "NATIVE_FOLLOWUP=899" for row in rows if row["type"] == "text")
    assert not run.steering_open and run.active_turn is None and run.retirement_state == "proven"
    await metrics.drain()
    report, = await query.codex_runs(metrics.conn)
    assert report["root_turns"] == 1 and report["child_turns"] == 3, report
    usage = await query.codex_turns(metrics.conn)
    assert sum(row["child_responses"] for row in usage) >= 3, usage
    assert {row["request_id"] for row in usage} == {receipt.request_id}


async def test_live_root_final_freezes_input_and_retires_optional_helper(live_product, tmp_path):
    manager, database, session, metrics = live_product
    receipt, run = await live_run(manager, session,
        "This user explicitly authorizes exactly one native helper for a resource-retirement check. "
        "Its result is optional and is NOT required for your reply. Use collaboration.spawn_agent with "
        "fork_turns=none. Tell the helper to first send the commentary HELPER_PARTIAL_73, then run a shell "
        "command in the current working directory: echo $$ > helper-pid; touch helper-ready; sleep 60; "
        "touch helper-finished . It should reply only after that command returns. "
        "After spawn, use your own shell command to wait at most 30 seconds for helper-ready in the "
        "current working directory. Once it exists, immediately reply only NATIVE_ROOT_EARLY_73. "
        "Do not wait for the helper result, do not call wait_agent or interrupt_agent. "
        "The host will retire optional remaining work when your root turn ends. Do not create more helpers.")
    completion = await asyncio.wait_for(receipt.wait(), 150)
    assert completion.outcome.status == "success", completion
    assert not run.steering_open and run.active_turn is None
    assert await receipt.execution.wait_resources() == ("proven",)
    result = await database.read_session_result(session.id, receipt.request_id)
    assert "NATIVE_ROOT_EARLY_73" in result.reply_text()
    rows = await database.load_messages(session.id)
    cards = {row["tool_input"]["card_id"]: row["tool_input"] for row in rows if row["type"] == "native_subagent"}
    assert len(cards) == 1, cards
    card, = cards.values()
    assert card["status"] == "interrupted" and card["stop_reason"] == "parent_done", card
    partial = next(row for row in rows if row["type"] == "text" and row["role"] == "assistant"
                   and "HELPER_PARTIAL_73" in (row["content"] or ""))
    assert partial["runtime_metadata"]["native"]["thread_id"] == card["native_thread_id"]
    detail = await database.read_native_details(session.id, session.user_id, partial["seq"])
    assert any("HELPER_PARTIAL_73" in (message.content or "") for message in detail.messages)
    assert not Path(f"/proc/{(tmp_path / 'helper-pid').read_text().strip()}").exists()
    assert not (tmp_path / "helper-finished").exists()
    await metrics.drain()
    report, = await query.codex_runs(metrics.conn)
    assert report["root_turns"] == 1 and report["native_status"] == "completed"
    assert report["end_reason"] == "parent_done" and report["resource_state"] == "proven"


async def test_live_new_thread_reports_usage_without_existing_rollout(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "new-thread.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent()
    await database.update_agent(agent["id"], mcp_servers=[])
    manager = SessionManager()
    await manager.initialize(database)
    metrics = MetricsStore(str(tmp_path / "new-thread-metrics.db"))
    await metrics.initialize()
    monkeypatch.setattr(monitor, "monitor", metrics)
    session = await manager.create_session(agent["id"], "First usage", str(tmp_path), backend="codex")
    try:
        receipt = await manager.start_message(session.id, "Reply exactly: INITIAL_USAGE_17. Do not use tools or helpers.")
        completion = await asyncio.wait_for(receipt.wait(), 120)
        assert completion.outcome.status == "success", completion
        assert await receipt.execution.wait_resources() == ("proven",)
        await metrics.drain()
        responses = await query.codex_turns(metrics.conn)
        assert any(row["request_id"] == receipt.request_id and row["output_tokens"] > 0 for row in responses), responses
        run, = await query.codex_runs(metrics.conn)
        assert run["root_turns"] == 1 and run["metrics_unavailable"] == 0
        assert run["metrics_collection_complete"] == 1
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=10)
        await manager.close_session_owners()
        await metrics.stop()
        await database.close()


@pytest.mark.parametrize("first_transport", ["exec", "app-server"])
async def test_live_native_start_and_precise_resume(tmp_path, monkeypatch, first_transport):
    if not await asyncio.to_thread(codex_cli_works):
        pytest.skip("The selected existing Codex login is unavailable")
    database = Database(str(tmp_path / "product.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent()
    await database.update_agent(agent["id"], mcp_servers=[])
    manager = SessionManager()
    await manager.initialize(database)
    session = await manager.create_session(agent["id"], "Live native input", str(tmp_path), backend="codex")
    events = []
    metrics = MetricsStore(str(tmp_path / "native-metrics.db"))
    await metrics.initialize()
    monkeypatch.setattr(monitor, "monitor", metrics)

    async def observe(event):
        events.append(event)

    async def submit(text, identity, attachments):
        reply = await submit_client_message(manager, session.id, text, attachments, identity, wiki_mode=None)
        async with asyncio.timeout(120):
            while (snapshot := manager.dispatch_snapshot(session.id))["active_request_id"] is not None or snapshot["items"]:
                await asyncio.sleep(.05)
        status = await submission_status(manager, session.id, identity)
        assert status["outcome"]["status"] == "success", status
        return await database.read_session_result(session.id, reply["request_id"]), status

    manager.on_broadcast("live-native", observe)
    try:
        seeded = None
        if first_transport == "exec":
            seeded = await seed_exec_history(manager, database, session,
                "Remember amber-bridge-63 for my next message. Reply only OK. Do not use tools or helpers.")
        attachment = save_upload(session.id, "input.txt", b"NATIVE_ATTACHMENT_47", user_id=session.user_id)
        result, first = await submit(
            "Use the command tool to read the attached input.txt and copy its exact bytes to artifact.txt "
            "in the working directory. Reply with its text. Remember the phrase amber-bridge-63 for "
            "my next message. Do not spawn helpers.", "first", [attachment.id],
        )
        native_id = session.claude_session_id
        assert native_id
        if seeded is not None:
            assert native_id == seeded
        assert "NATIVE_ATTACHMENT_47" in "".join(result.fragments)
        assert (tmp_path / "artifact.txt").read_bytes() == b"NATIVE_ATTACHMENT_47"
        assert any(event.get("type") == "tool_result" for event in events)
        result, second = await submit("Reply only with the phrase I asked you to remember. Do not use helpers.", "second", [])
        assert "amber-bridge-63" in "".join(result.fragments)
        evidence = second["execution"]["native_inputs"][0]
        assert evidence["state"] == "accepted" and evidence["thread_id"] == native_id
        assert first["execution"]["native_inputs"][0]["turn_id"] != evidence["turn_id"]
        assert second["outcome"]["recovery"] == {"transient": 0, "idle": 0, "stale_session": 0, "premature_exit": 0}
        await metrics.drain()
        response_rows = await query.codex_turns(metrics.conn)
        measured_requests = {row["request_id"] for row in response_rows if row["responses"] > 0 and row["output_tokens"] > 0}
        assert second["request_id"] in measured_requests, response_rows
        assert first["request_id"] in measured_requests, response_rows
        runs = await query.codex_runs(metrics.conn)
        assert all(run["resource_state"] == "proven" and run["metrics_unavailable"] == 0 for run in runs), runs
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=10)
        await manager.close_session_owners()
        await metrics.stop()
        await database.close()


@pytest.mark.parametrize("first_transport", ["exec", "app-server"])
async def test_live_old_and_native_history_fork_continue_independently(tmp_path, monkeypatch, first_transport):
    database = Database(str(tmp_path / "fork-product.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent()
    await database.update_agent(agent["id"], mcp_servers=[])
    manager = SessionManager()
    await manager.initialize(database)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(ForkingMixin, "_fork_copy_base", staticmethod(lambda: str(tmp_path / "copies")))
    parent = await manager.create_session(agent["id"], "Live fork source", str(work), backend="codex")

    async def ask(session, prompt):
        receipt = await manager.start_message(session.id, prompt)
        completion = await asyncio.wait_for(receipt.wait(), 120)
        assert completion.outcome.status == "success", completion
        await manager.await_session_execution(session.id, timeout=10)
        return await database.read_session_result(session.id, receipt.request_id)

    try:
        if first_transport == "exec":
            await seed_exec_history(manager, database, parent,
                "Remember amber-thread-93 in this conversation. Reply only OK. Do not use tools or helpers.")
        first = await ask(parent, "Use the command tool to write FORK_ARTIFACT_81 into seed.txt in the working directory. "
                          "Remember amber-thread-93 in this conversation. Reply with that phrase. Do not spawn helpers.")
        assert (work / "seed.txt").read_text().strip() == "FORK_ARTIFACT_81"
        branch = await manager.duplicate_session(parent.id, user_id=parent.user_id)
        assert branch.claude_session_id and branch.claude_session_id != parent.claude_session_id
        assert branch.user_id == parent.user_id and branch.backend == "codex"
        from pathlib import Path

        assert (Path(branch.working_dir) / "seed.txt").read_bytes() == (work / "seed.txt").read_bytes()
        assert (await database.read_session_result(branch.id, first.request_id)).fragments == first.fragments
        fork_reply = await ask(branch, "Use the command tool to write BRANCH_ONLY into branch-only.txt in the current working "
                               "directory. Reply with the phrase I asked you to remember. Do not spawn helpers.")
        assert "amber-thread-93" in fork_reply.reply_text()
        assert (Path(branch.working_dir) / "branch-only.txt").read_text().strip() == "BRANCH_ONLY"
        assert not (work / "branch-only.txt").exists()
        source_reply = await ask(parent, "Reply only with the phrase I asked you to remember. Do not use helpers.")
        assert "amber-thread-93" in source_reply.reply_text()
        await manager.delete_session(parent.id)
        assert (await database.read_session_result(branch.id, first.request_id)).fragments == first.fragments
    finally:
        await manager.drain_turns(timeout=10)
        await manager.close_session_owners()
        await database.close()

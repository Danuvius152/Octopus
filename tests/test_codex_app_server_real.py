"""Explicit live gate for the opt-in, G0-pinned no-descendant transport."""

import asyncio

import pytest

from server.attachments import save_upload
from server.config import settings
from server.database import Database
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.forking import ForkingMixin
from tests.accounts import create_account
from tests.cli_gate import codex_cli_works

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


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
        monkeypatch.setattr(settings, "codex_transport", first_transport)
        attachment = save_upload(session.id, "input.txt", b"NATIVE_ATTACHMENT_47", user_id=session.user_id)
        result, first = await submit(
            "Use the command tool to read the attached input.txt and copy its exact bytes to artifact.txt "
            "in the working directory. Reply with its text. Remember the phrase amber-bridge-63 for "
            "my next message. Do not spawn helpers.", "first", [attachment.id],
        )
        native_id = session.claude_session_id
        assert native_id
        assert "NATIVE_ATTACHMENT_47" in "".join(result.fragments)
        assert (tmp_path / "artifact.txt").read_bytes() == b"NATIVE_ATTACHMENT_47"
        assert any(event.get("type") == "tool_result" for event in events)
        monkeypatch.setattr(settings, "codex_transport", "app-server")
        result, second = await submit("Reply only with the phrase I asked you to remember. Do not use helpers.", "second", [])
        assert "amber-bridge-63" in "".join(result.fragments)
        evidence = second["execution"]["native_inputs"][0]
        assert evidence["state"] == "accepted" and evidence["thread_id"] == native_id
        if first_transport == "app-server":
            assert first["execution"]["native_inputs"][0]["turn_id"] != evidence["turn_id"]
        assert second["outcome"]["recovery"] == {"transient": 0, "idle": 0, "stale_session": 0, "premature_exit": 0}
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=10)
        await manager.close_session_owners()
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
        monkeypatch.setattr(settings, "codex_transport", first_transport)
        first = await ask(parent, "Use the command tool to write FORK_ARTIFACT_81 into seed.txt in the working directory. "
                          "Remember amber-thread-93 in this conversation. Reply with that phrase. Do not spawn helpers.")
        assert (work / "seed.txt").read_text().strip() == "FORK_ARTIFACT_81"
        monkeypatch.setattr(settings, "codex_transport", "app-server")
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

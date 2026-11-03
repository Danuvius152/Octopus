"""Product references read the collected child work without opening a host."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server.deps import get_session_manager, get_user_manager
from server.main import app
from tests import native_app_server as native_tests
from tests.accounts import create_account
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    start_output,
    terminal,
    until,
)
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


@pytest.fixture
async def detail_client(native_runtime):
    manager, _, session, _, _, _ = native_runtime
    bearer = await get_user_manager().issue_token(session.user_id)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
                               headers={"Authorization": "Bearer " + bearer}) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session_manager, None)


async def test_spawn_reference_binds_its_original_child_round_and_survives_retirement(native_runtime, detail_client, tmp_path):
    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    run = session._run.backend
    spawn = {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "id": "spawn", "type": "collabAgentToolCall", "tool": "spawnAgent", "status": "completed",
        "senderThreadId": ROOT, "receiverThreadIds": ["child"], "agentsStates": {}, "prompt": "Inspect the file"}}}
    inject(tmp_path, spawn)
    await until(lambda: bool(session._subagents))
    rows = await database.load_messages(session.id)
    anchor = next(row["seq"] for row in rows if row["type"] == "native_subagent")
    url = f"/api/sessions/{session.id}/native-details/{anchor}"
    unbound = await detail_client.get(url)
    assert unbound.status_code == 200
    assert unbound.json()["availability"] == "not_collected" and unbound.json()["messages"] == []

    inject(tmp_path, child_start(), item("child", "child-turn", "answer", "Saved helper body"), child_end(),
           child_start(turn="second"), item("child", "second", "answer", "Another round"),
           item(ROOT, "turn-1", "answer", "Root only"))
    await until(lambda: len(session._subagents) == 2)
    await until(lambda: run.native_terminal_status is None)
    # Wait for original publication, rather than relying on the card snapshot
    # to prove that its complete item has entered the saved transcript.
    async with asyncio.timeout(3):
        while not any(row["content"] == "Root only" for row in await database.load_messages(session.id)):
            await asyncio.sleep(.01)
    live = await detail_client.get(url)
    assert live.status_code == 200
    assert live.json()["availability"] == "available"
    assert [row["content"] for row in live.json()["messages"]] == ["Saved helper body"]
    assert live.json()["child"]["status"] == "completed"
    assert run.question_admission_open and len(starts) == 1
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)
    retired = await detail_client.get(url)
    assert retired.json() == live.json()
    assert run.retirement_state == "proven" and len(starts) == 1
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"


async def test_detail_pages_keep_tool_call_and_result_and_reject_other_work_cursors(native_runtime, detail_client, tmp_path):
    manager, database, session, _, starts, _ = native_runtime
    await start_output(native_runtime)
    def command(phase):
        return {"method": f"item/{phase}", "params": {"threadId": "child", "turnId": "child-turn", "item": {
            "id": "command", "type": "commandExecution", "command": "true", "cwd": str(tmp_path), "commandActions": [],
            "status": "completed", "aggregatedOutput": "Saved output", "exitCode": 0}}}
    inject(tmp_path, child_start(), command("started"), command("completed"),
           item("child", "child-turn", "answer", "Saved partial"),
           child_start("other", "other-turn"), item("other", "other-turn", "answer", "Private other work"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    anchor = next(row["seq"] for row in rows if row["type"] == "native_subagent")
    expected = [row for row in rows if row["type"] != "native_subagent" and
                (row["runtime_metadata"] or {}).get("native", {}).get("thread_id") == "child"]
    url = f"/api/sessions/{session.id}/native-details/{anchor}"
    collected, after = [], None
    for _ in range(3):
        response = await detail_client.get(url, params={"limit": 1, **({"after_seq": after} if after is not None else {})})
        assert response.status_code == 200
        page = response.json()
        assert page["child"]["status"] == "interrupted" and page["child"]["stop_reason"] == "parent_done"
        collected.extend(page["messages"])
        after = page["next_after_seq"]
    assert not page["has_more"]
    assert [row["type"] for row in collected] == ["tool_use", "tool_result", "text"]
    from server.models import MessageContent
    assert collected == [MessageContent(**row).model_dump(mode="json") for row in expected]
    other = next(row["seq"] for row in rows if row["content"] == "Private other work")
    assert (await detail_client.get(url, params={"after_seq": other})).status_code == 409
    body_reference = await detail_client.get(f"/api/sessions/{session.id}/native-details/{expected[-1]['seq']}")
    assert body_reference.json()["messages"] == collected
    await manager.archive_session(session.id)
    assert (await detail_client.get(url)).json()["messages"] == collected
    assert len(starts) == 1


async def test_unreadable_saved_item_is_query_failure_instead_of_uncollected_history(native_runtime, detail_client, tmp_path):
    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, child_start(), item("child", "child-turn", "body", "Saved then damaged"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    anchor = next(row["seq"] for row in rows if row["type"] == "native_subagent")
    body = next(row["seq"] for row in rows if row["content"] == "Saved then damaged")
    async with database.transaction(write=True) as tx:
        await tx.execute("UPDATE messages SET content=? WHERE session_id=? AND seq=?", ("unreadable-json", session.id, body))
    response = await detail_client.get(f"/api/sessions/{session.id}/native-details/{anchor}")
    assert response.status_code == 500
    assert (await database.get_committed_terminal(session.id, receipt["request_id"]))["status"] == "success"
    assert len(starts) == 1


async def test_product_details_reject_cross_account_and_non_child_references(native_runtime, detail_client, tmp_path):
    manager, database, session, _, starts, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, child_start(), item("child", "child-turn", "body", "Owned body"),
           item(ROOT, "turn-1", "answer", "Root body"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    anchor = next(row["seq"] for row in rows if row["type"] == "native_subagent")
    root = next(row["seq"] for row in rows if row["content"] == "Root body")
    base = f"/api/sessions/{session.id}/native-details"
    for reference in (root, 999999):
        assert (await detail_client.get(f"{base}/{reference}")).status_code == 404
    assert (await detail_client.get(f"{base}/arbitrary-native-id")).status_code == 422
    _, other_bearer = await create_account(database, username="other-account", extra_roots=[tmp_path])
    assert (await detail_client.get(f"{base}/{anchor}", headers={"Authorization": "Bearer " + other_bearer})).status_code == 404
    # Sequence-keyed reads must not choose arbitrarily between duplicate rows.
    # Older/corrupted stores can contain duplicates. Normal writes already
    # have a uniqueness constraint, so remove it only in this bad-store fixture.
    async with database.transaction(write=True) as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
    await database.append_message(session_id=session.id, seq=anchor, role="system", type="text", content="Duplicate seq")
    conflict = await detail_client.get(f"{base}/{anchor}")
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "duplicate_message_sequences"
    assert len(starts) == 1


@pytest.mark.parametrize("body", [False, True])
@pytest.mark.parametrize("ending", ["stop", "root_terminal"])
async def test_pending_detail_response_cannot_hold_retirement_or_change_collected_partial(native_runtime, detail_client, tmp_path, monkeypatch, body, ending):
    manager, database, session, _, starts, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, child_start(), *([item("child", "child-turn", "body", "Before Stop")] if body else []))
    await until(lambda: bool(session._subagents))
    anchor = next(row["seq"] for row in await database.load_messages(session.id) if row["type"] == "native_subagent")
    entered, release = asyncio.Event(), asyncio.Event()
    read = database.read_native_details
    async def delay_response(*args, **kwargs):
        result = await read(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(database, "read_native_details", delay_response)
    loading = asyncio.create_task(detail_client.get(f"/api/sessions/{session.id}/native-details/{anchor}"))
    run = session._run.backend
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if ending == "stop":
            await manager.interrupt(session.id)
        else:
            inject(tmp_path, terminal())
        await wait_dispatch_idle(manager, session.id)
        assert run.retirement_state == "proven" and not release.is_set()
    finally:
        release.set()
        await loading
    page = (await detail_client.get(f"/api/sessions/{session.id}/native-details/{anchor}")).json()
    assert page["availability"] == ("available" if body else "not_collected")
    assert [row["content"] for row in page["messages"]] == (["Before Stop"] if body else [])
    assert page["child"]["status"] == "interrupted"
    assert page["child"]["stop_reason"] == ("user_stop" if ending == "stop" else "parent_done")
    assert len(starts) == 1

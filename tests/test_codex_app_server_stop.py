"""Stop keeps native retirement separate from independent returns and recovery."""

import asyncio
import json
import shlex
import sys

import pytest

from server.bg_tasks import BgTaskManager
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from tests import native_app_server as native_tests
from tests.native_app_server import (
    ROOT,
    active_root,
    child_end,
    child_start,
    inject,
    item,
    until,
    wait_until,
)
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


async def test_stop_retires_silent_native_work_but_keeps_independent_returns_in_acceptance_order(native_runtime, tmp_path):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_group"
    receipt = await manager.start_message(session.id, "Original root", source="human")
    await until(lambda: (tmp_path / "native-group-ready").exists())
    run = session._run.backend
    inject(tmp_path, child_start("finished", "finished-turn"), child_end("finished", "finished-turn"), child_start(),
           {"method": "item/completed", "params": {"threadId": "child", "turnId": "child-turn", "item": {
               "id": "nested", "type": "collabAgentToolCall", "tool": "spawnAgent", "status": "completed",
               "receiverThreadIds": ["grandchild"], "agentsStates": {}}}},
           child_start("grandchild", "grand-turn"), item("grandchild", "grand-turn", "partial", "Nested partial"),
           {"id": "native-question", "method": "item/tool/requestUserInput", "params": {
               "threadId": ROOT, "turnId": "turn-1", "itemId": "question", "isBlocking": True,
               "questions": [{"id": "choice", "question": "Continue?", "options": []}]}})
    await until(lambda: bool(manager.get_pending_questions(session.id)))
    background = BgTaskManager()
    background.bind(database, manager.deliver_bg_result, manager._broadcast)
    await background.start()
    records = []
    try:
        for label in ("first", "second"):
            gate = tmp_path / f"release-{label}"
            program = "import pathlib,sys,time\ngate=pathlib.Path(sys.argv[1])\nwhile not gate.exists(): time.sleep(.01)\nprint(sys.argv[2])"
            records.append(await background.start_task(session_id=session.id, working_dir=str(tmp_path),
                command=shlex.join([sys.executable, "-c", program, str(gate), label])))
        await manager.interrupt(session.id)
        await wait_dispatch_idle(manager, session.id)
        assert run.retirement_state == "proven" and not run.question_admission_open
        assert not manager.get_pending_questions(session.id)
        assert (await receipt.wait()).outcome.status == "interrupted"
        states = {row["tool_input"]["task_id"]: row["tool_input"] for row in await database.load_messages(session.id)
                  if row["type"] == "native_subagent"}
        assert states["finished"]["status"] == "completed"
        assert all(states[task]["status"] == "interrupted" and states[task]["stop_reason"] == "user_stop"
                   for task in ("child", "grandchild"))
        assert any(row["content"] == "Nested partial" for row in await database.load_messages(session.id))
        assert [(await background.get_task(record.id)).status for record in records] == ["running", "running"]
        for label, record in zip(("first", "second"), records, strict=True):
            (tmp_path / f"release-{label}").touch()
            await asyncio.wait_for(asyncio.shield(background._running[record.id].task), 4)
        queued = manager.dispatch_snapshot(session.id)
        assert queued["paused"] and len(queued["items"]) == 2 and len(starts) == 1
        assert all(row["source"] == "bg" for row in queued["items"])
        # Injection files represent bytes on the retired connection. A new
        # peer must not replay that fixture input before its own preparation.
        (tmp_path / "native-injections.jsonl").unlink()
        mode[0] = "normal"
        human = await manager.start_message(session.id, "Manual correction", source="human")
        assert (await asyncio.wait_for(human.wait(), 4)).outcome.status == "success"
        assert manager.dispatch_snapshot(session.id)["items"] == queued["items"]
        await manager.resume_dispatch(session.id)
        await wait_dispatch_idle(manager, session.id)
        inputs = [json.loads(line)["input"][0]["text"] for line in (tmp_path / "native-business.jsonl").read_text().splitlines()]
        assert inputs[:2] == ["Original root", "Manual correction"]
        assert records[0].id in inputs[2] and records[1].id in inputs[3]
        assert len(starts) == 4
    finally:
        for label in ("first", "second"):
            (tmp_path / f"release-{label}").touch()
        await background.shutdown()


@pytest.mark.parametrize("proof", ["initial_unknown", "steer_unknown", "steer_accepted"])
async def test_service_reentry_preserves_native_input_evidence_without_replay(native_runtime, tmp_path, proof):
    manager, database, session, mode, starts, _ = native_runtime
    if proof == "initial_unknown":
        mode[0] = "unknown"
        await submit_client_message(manager, session.id, "Keep original", [], "original", wiki_mode=None)
        client_id, content, expected = "original", "Keep original", "unknown"
    else:
        mode[0] = "steer_lost" if proof == "steer_unknown" else "steer_wait"
        await active_root(native_runtime, tmp_path)
        client_id, content = "correction", "Keep correction"
        await submit_client_message(manager, session.id, content, [], client_id, wiki_mode=None)
        async def has_evidence():
            saved = await submission_status(manager, session.id, client_id)
            # Native "unknown" is committed before the write. Wait until the
            # original request has actually settled its lost-ACK observation;
            # ending the root earlier can legitimately requeue an unsent steer.
            if proof == "steer_unknown" and saved["execution"]["state"] != "unknown":
                return False
            return bool(saved["execution"]["native_inputs"]) and saved["execution"]["native_inputs"][0]["state"] == (
                "unknown" if proof == "steer_unknown" else "accepted")
        await wait_until(has_evidence)
        (tmp_path / "native-finish").touch()
        expected = "unknown" if proof == "steer_unknown" else "accepted"
    await wait_dispatch_idle(manager, session.id)
    before = await submission_status(manager, session.id, client_id)
    await manager.close_session_owners()
    restored = SessionManager()
    await restored.initialize(database)
    try:
        after = await submission_status(restored, session.id, client_id)
        assert after["input"]["content"] == content
        assert after["execution"]["native_inputs"][0]["state"] == expected
        assert after["execution"]["native_inputs"] == before["execution"]["native_inputs"]
        assert after["execution"]["state"] == ("unknown" if expected == "unknown" else "done")
        duplicate = await submit_client_message(restored, session.id, content, [], client_id, wiki_mode=None)
        assert duplicate["request_id"] == after["request_id"]
        assert not restored.dispatch_snapshot(session.id)["items"] and len(starts) == 1
        assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1
    finally:
        await restored.close_session_owners()

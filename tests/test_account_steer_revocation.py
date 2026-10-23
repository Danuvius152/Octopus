"""Account revocation during an input anchor wait prevents inline delivery."""

import asyncio
import json
import sys
from dataclasses import replace

import pytest

from server.agent_manager import AgentManager
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.session_manager import session_manager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.workspace import paths_for
from tests import test_live_account_authorization as live_authorization

accounts = live_authorization.accounts
client = live_authorization.client


@pytest.mark.parametrize("input_kind", ["plain", "durable"])
async def test_disabling_owner_during_steer_git_anchor_prevents_frame_delivery(
    accounts, tmp_path, monkeypatch, input_kind,
):
    ready, anchor_entered, release_anchor = asyncio.Event(), asyncio.Event(), asyncio.Event()
    status_changed = asyncio.Event()
    frames = tmp_path / "received-frames.jsonl"
    finish_initial = tmp_path / "finish-initial"
    model = tmp_path / "synthetic_stream_model.py"
    model.write_text(
        "import json,select,sys\n"
        "from pathlib import Path\n"
        "initial=json.loads(sys.stdin.readline())\n"
        "with Path(sys.argv[1]).open('a') as record:\n"
        "    record.write(json.dumps({'index':0,'text':'initial'})+'\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'revocation-steer-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'Ready for a correction'}}),flush=True)\n"
        "index=0\n"
        "while not Path(sys.argv[2]).exists():\n"
        "    if not select.select([sys.stdin],[],[],0.02)[0]: continue\n"
        "    line=sys.stdin.readline()\n"
        "    if not line: break\n"
        "    index+=1\n"
        "    frame=json.loads(line)\n"
        "    with Path(sys.argv[1]).open('a') as record:\n"
        "        record.write(json.dumps({'index':index,'text':frame['message']['content']})+'\\n')\n"
        "    print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "    break\n"
        "else:\n"
        "    print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )

    def argv(context):
        return [sys.executable, "-u", str(model), str(frames), str(finish_initial)], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, prepare_resume=None,
        activity_observer=None, stdin_mode=StdinMode.STREAM_JSON,
    ))
    owner = accounts["owner"]["id"]
    workspace = paths_for(owner).workspace
    agent = await AgentManager(accounts["db"]).create_agent(
        name="Steer anchor", user_id=owner, backend="codex",
    )
    session = await session_manager.create_session(
        agent["id"], "Disable during a steer anchor", str(workspace), backend="codex",
    )
    (workspace / "tracked.txt").write_text("A stable Git anchor.\n")
    for arguments in (
        ("init", "-q"), ("add", "tracked.txt"),
        ("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "initial"),
    ):
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, (stdout + stderr).decode()

    gate_next_anchor = False
    spawn = asyncio.create_subprocess_exec

    async def controlled_spawn(*arguments, **kwargs):
        nonlocal gate_next_anchor
        process = await spawn(*arguments, **kwargs)
        if gate_next_anchor and arguments[:3] == ("git", "rev-parse", "HEAD"):
            gate_next_anchor = False
            anchor_entered.set()
            await release_anchor.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_spawn)

    async def observe(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "assistant_text":
            ready.set()
        if event.get("type") == "dispatch_state":
            status_changed.set()

    async def settled_submission():
        async with asyncio.timeout(5):
            while True:
                status_changed.clear()
                status = await submission_status(session_manager, session.id, "revoked-correction")
                assert status is not None
                if status["execution"]["state"] in {"withdrawn", "done", "unknown"}:
                    return status
                await status_changed.wait()

    session_manager.on_broadcast("steer-revocation-ready", observe)
    correction = "Preserve this original correction without delivering it after permission is revoked."
    try:
        initial = await session_manager.start_message(session.id, "Keep working until I send a correction.")
        assert initial.accepted
        await asyncio.wait_for(ready.wait(), 5)
        assert [json.loads(line) for line in frames.read_text().splitlines()] == [{"index": 0, "text": "initial"}]

        gate_next_anchor = True
        if input_kind == "durable":
            admitted = await submit_client_message(
                session_manager, session.id, correction, [], "revoked-correction", wiki_mode=None,
            )
            assert admitted["accepted"] is True
            assert admitted["client_request_id"] == "revoked-correction"
            assert admitted["disposition"] == "steered"
        else:
            receipt = await session_manager.start_message(session.id, correction, steerable=True)
            assert receipt.disposition == "steered"
        await asyncio.wait_for(anchor_entered.wait(), 5)
        disabled = await accounts["http"].post(
            f"/api/auth/users/{owner}/disabled", json={"disabled": True}, headers=accounts["admin_headers"],
        )
        assert disabled.status_code == 200, disabled.text
        release_anchor.set()
        if input_kind == "durable":
            status = await settled_submission()
        else:
            completion = await asyncio.wait_for(receipt.wait(), 5)

        assert [json.loads(line) for line in frames.read_text().splitlines()] == [{"index": 0, "text": "initial"}], (
            "The active model received another frame after account disable returned 200."
        )
        if input_kind == "durable":
            assert status["accepted"] is True
            assert status["request_id"] == admitted["request_id"]
            assert status["client_request_id"] == admitted["client_request_id"]
            assert status["execution"]["state"] == "withdrawn"
            assert status["execution"]["reason_code"] == "account_unavailable"
            assert status["execution"]["recoverable_input"] is True
            assert status["input"]["content"] == correction
            assert "outcome" not in status
        else:
            assert completion.state == "withdrawn"
            assert completion.reason == "account_unavailable"
            assert completion.outcome is None
            assert completion.recording_state == "none"

        finish_initial.touch()
        assert (await asyncio.wait_for(initial.wait(), 5)).outcome.status == "success"
        assert (await accounts["http"].get("/api/agents", headers=accounts["owner_headers"])).status_code == 401
        assert (await accounts["http"].get("/api/agents", headers=accounts["other_headers"])).status_code == 200
        enabled = await accounts["http"].post(
            f"/api/auth/users/{owner}/disabled", json={"disabled": False}, headers=accounts["admin_headers"],
        )
        assert enabled.status_code == 200, enabled.text
        login = await accounts["http"].post(
            "/api/auth/login", json={"username": "owner", "password": "test-password"},
        )
        assert login.status_code == 200, login.text
        detail = await accounts["http"].get(
            f"/api/sessions/{session.id}", headers={"Authorization": "Bearer " + login.json()["token"]},
        )
        assert detail.status_code == 200, detail.text
        assert any(message["role"] == "user" and message["content"] == correction
                   for message in detail.json()["messages"])
        if input_kind == "durable":
            endpoint = f"/api/sessions/{session.id}/submissions/revoked-correction"
            saved = await accounts["http"].get(
                endpoint, headers={"Authorization": "Bearer " + login.json()["token"]},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["execution"]["state"] == "withdrawn"
            assert saved.json()["execution"]["reason_code"] == "account_unavailable"
            assert saved.json()["execution"]["recoverable_input"] is True
            assert saved.json()["input"]["content"] == correction
            assert saved.json().get("outcome") is None
            assert saved.json()["request_id"] == admitted["request_id"]
            duplicate = await submit_client_message(
                session_manager, session.id, correction, [], "revoked-correction", wiki_mode=None,
            )
            assert duplicate == status
            assert [json.loads(line) for line in frames.read_text().splitlines()] == [{"index": 0, "text": "initial"}]
            assert sum(message["role"] == "user" and message["content"] == correction
                       for message in detail.json()["messages"]) == 1
    finally:
        release_anchor.set()
        session_manager.remove_broadcast("steer-revocation-ready")
        await session_manager.drain_turns()
        await session_manager.stop_all_held_processes()

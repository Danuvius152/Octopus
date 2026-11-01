"""A disabled owner cannot start a model after resumable preparation waits."""

import asyncio
import json
import sys
from dataclasses import replace

from server.agent_manager import AgentManager
from server.harness import get_harness
from server.harness.driver import ArgvDriver
from server.session_manager import session_manager
from server.workspace import paths_for
from tests import test_live_account_authorization as live_authorization

accounts = live_authorization.accounts
client = live_authorization.client


async def test_disabling_owner_during_resume_preparation_prevents_model_spawn(
    accounts, tmp_path, monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()
    spawns = tmp_path / "model-spawns.jsonl"
    model = tmp_path / "synthetic_model.py"
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as record:\n"
        "    record.write(json.dumps({'resume_id':sys.argv[2]})+'\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'revocation-resume-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'reply','type':'agent_message','text':'Synthetic reply'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )

    def argv(context):
        return [sys.executable, "-u", str(model), str(spawns), context.resume_id or "initial"], {
            "cwd": context.working_dir,
        }

    async def prepare_resume(context, _argv, _kwargs):
        assert context.resume_id == "revocation-resume-thread"
        entered.set()
        await release.wait()

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, prepare_resume=prepare_resume,
        activity_observer=None, new_driver=ArgvDriver,
    ))
    owner = accounts["owner"]["id"]
    agent = await AgentManager(accounts["db"]).create_agent(
        name="Resume preparation", user_id=owner, backend="codex",
    )
    session = await session_manager.create_session(
        agent["id"], "Disable while resuming", str(paths_for(owner).workspace), backend="codex",
    )
    original_input = "Keep this original input when permission is revoked during resume preparation."
    try:
        initial = await session_manager.start_message(session.id, "Establish a resumable conversation.")
        assert (await asyncio.wait_for(initial.wait(), 5)).outcome.status == "success"
        detail = await accounts["http"].get(
            f"/api/sessions/{session.id}", headers=accounts["owner_headers"],
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["claude_session_id"] == "revocation-resume-thread"
        assert [json.loads(line) for line in spawns.read_text().splitlines()] == [{"resume_id": "initial"}]

        receipt = await session_manager.start_message(session.id, original_input)
        assert receipt.accepted
        await asyncio.wait_for(entered.wait(), 5)
        disabled = await accounts["http"].post(
            f"/api/auth/users/{owner}/disabled", json={"disabled": True}, headers=accounts["admin_headers"],
        )
        assert disabled.status_code == 200, disabled.text
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 5)

        assert [json.loads(line) for line in spawns.read_text().splitlines()] == [{"resume_id": "initial"}], (
            "A model process started after the administrator confirmed account disable."
        )
        assert completion.outcome.status == "failed"
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
        assert any(message["role"] == "user" and message["content"] == original_input
                   for message in detail.json()["messages"])
    finally:
        release.set()
        await session_manager.drain_turns()

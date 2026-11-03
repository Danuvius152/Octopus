"""Late model rejection follows the material adopted by the original process."""

import asyncio
import sys
from dataclasses import replace

import pytest

from server.crypto import encrypt
from server.harness import get_harness
from server.harness.claude_code import build_turn_argv
from server.users import UserManager
from tests.native_app_server import until
from tests.test_session_manager import _bind_credential
from tests.test_session_manager import manager as manager


@pytest.mark.parametrize("change", ["replace", "oauth", "label", "delete", "disable", "none"])
@pytest.mark.parametrize("parallel", [False, True])
async def test_rejection_changes_only_the_authority_the_process_adopted(manager, tmp_path, monkeypatch, change, parallel):
    credential = await _bind_credential(manager, "claude-code", secret="material-A")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Auth", str(tmp_path), credential_id=credential)
    peer = tmp_path / "peer.py"
    peer.write_text('''import json, os, pathlib, time
with open("adopted", "a") as handle: handle.write(os.environ["ANTHROPIC_API_KEY"] + "\\n")
while not pathlib.Path("release").exists(): time.sleep(.005)
print(json.dumps({"type":"result", "is_error":True, "errors":["API Error: 401 Invalid authentication credentials"]}), flush=True)
''')

    def render(context):
        _, kwargs = build_turn_argv(context)
        kwargs.pop("private_settings", None)
        return [sys.executable, str(peer)], kwargs

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=render, activity_observer=None))
    receipt = await manager.start_message(session.id, "original")
    receipts = [receipt]
    if parallel:
        second = await manager.create_session(agent["id"], "Auth two", str(tmp_path), credential_id=credential)
        receipts.append(await manager.start_message(second.id, "also original"))
    try:
        await until(lambda: (tmp_path / "adopted").exists() and len((tmp_path / "adopted").read_text().splitlines()) == len(receipts))
        assert (tmp_path / "adopted").read_text().splitlines() == ["material-A"] * len(receipts)
        if change == "replace":
            key = await UserManager(manager.db).data_key_for(session.user_id)
            await manager.db.update_credential(credential, secret_encrypted=encrypt("material-B", key))
        elif change == "oauth":
            from server.harness.login import OAuthCompletion
            from server.oauth_credentials import OAuthCredentials

            await OAuthCredentials(manager.db).commit(OAuthCompletion(login_id="synthetic", code="synthetic",
                label="OAuth B", credential_id=credential, user_id=session.user_id), "material-B")
        elif change == "label":
            await manager.db.update_credential(credential, label="renamed")
        elif change == "delete":
            await manager.db.delete_credential(credential)
        elif change == "disable":
            async with manager.db.transaction() as tx:
                await tx.execute("UPDATE users SET disabled_at='now' WHERE id=?", (session.user_id,))
        before = await manager.db.get_credential(credential)
    finally:
        (tmp_path / "release").touch()
    completions = await asyncio.wait_for(asyncio.gather(*(item.wait() for item in receipts)), 5)
    completion = completions[0]
    assert all(item.outcome.status == "failed" and item.outcome.code == completion.outcome.code for item in completions)
    assert completion.outcome.status == "failed"
    row = await manager.db.get_credential(credential)
    if change in {"none", "label"}:
        assert row["needs_reconnect"] and completion.outcome.code == "auth_expired"
    else:
        assert row == before
        assert completion.outcome.code == "auth_failed"
        assert "re-authorize" not in completion.outcome.message.lower()
    if change in {"replace", "oauth"}:
        resolved = await manager.resolve_credential_by_id(credential, backend="claude-code", user_id=session.user_id)
        assert resolved.secret == "material-B"

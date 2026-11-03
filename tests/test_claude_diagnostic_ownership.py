"""A reused CLI's delayed stderr cannot identify its current request's failure."""

import asyncio
import sys
from dataclasses import replace

import pytest

from server.harness import get_harness
from tests.native_app_server import until
from tests.test_session_manager import _bind_credential
from tests.test_session_manager import manager as manager


@pytest.mark.parametrize("timing", ["idle", "second"])
@pytest.mark.parametrize("old_error", ["API Error: 401 Invalid authentication credentials", "session not found", "overloaded_error"])
async def test_delayed_stderr_does_not_classify_next_attempt(manager, tmp_path, monkeypatch, timing, old_error):
    credential = await _bind_credential(manager, "claude-code")
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "diagnostics", str(tmp_path), credential_id=credential)
    peer = tmp_path / "peer.py"
    peer.write_text('''import json, pathlib, sys, time
for index, line in enumerate(sys.stdin):
    if index == 0:
        print(json.dumps({"type":"result", "session_id":"held", "is_error":False}), flush=True)
        if sys.argv[1] == "idle": print(sys.argv[2], file=sys.stderr, flush=True)
    else:
        if sys.argv[1] == "second": print(sys.argv[2], file=sys.stderr, flush=True)
        pathlib.Path("second").touch()
        while not pathlib.Path("release").exists(): time.sleep(.005)
        print(json.dumps({"type":"result", "is_error":True,"errors":["unrelated failure"]}), flush=True)
''')
    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, activity_observer=None,
        build_turn_argv=lambda ctx: ([sys.executable, str(peer), timing, old_error], {"cwd": ctx.working_dir})))
    first = await manager.start_message(session.id, "first")
    assert (await asyncio.wait_for(first.wait(), 4)).outcome.status == "success"
    await manager.await_session_execution(session.id)
    original = session._run.backend
    second = await manager.start_message(session.id, "second")
    try:
        await until(lambda: (tmp_path / "second").exists())
        assert session._run.backend is original
    finally:
        (tmp_path / "release").touch()
    completion = await asyncio.wait_for(second.wait(), 4)
    assert completion.outcome.status == "failed"
    assert completion.outcome.code not in {"auth_expired", "auth_failed", "transient_exhausted", "stale_session", "recovery_unproven"}
    assert not any(completion.outcome.recovery.values())
    assert not (await manager.db.get_credential(credential))["needs_reconnect"]

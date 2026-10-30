"""Claude effort changes cross the real STREAM_JSON subprocess boundary."""

import asyncio
import json
import sys
from dataclasses import replace

from server.agent_manager import AgentManager
from server.harness import get_harness
from tests import test_question_lifecycle as question_tests

question_host = question_tests.question_host


async def test_effort_changes_replace_process_and_resume_history(question_host, tmp_path, monkeypatch):
    manager, original, client, _events = question_host
    harness = get_harness("claude-code")
    render = harness.profile.build_turn_argv
    effects = tmp_path / "effects.jsonl"
    model = tmp_path / "claude_model.py"
    model.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "args=sys.argv[2:]\n"
        "resume=args[args.index('--resume')+1] if '--resume' in args else None\n"
        "effort=args[args.index('--effort')+1] if '--effort' in args else None\n"
        "for line in sys.stdin:\n"
        " record={'pid':os.getpid(),'resume':resume,'effort':effort,"
        "'env':os.environ.get('CLAUDE_CODE_EFFORT_LEVEL')}\n"
        " with Path(sys.argv[1]).open('a') as out: out.write(json.dumps(record)+'\\n')\n"
        " print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'session_id':'original-thread','result':'OK'}),flush=True)\n"
    )

    def argv(context):
        args, kwargs = render(context)
        return [sys.executable, '-u', str(model), str(effects), *args[1:]], kwargs

    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, build_turn_argv=argv,
    ))
    agent = await AgentManager(manager.db).create_agent(
        name="Claude effort", backend="claude-code", reasoning_effort="low", user_id=original.user_id,
    )
    session = await manager.create_session(
        agent["id"], "Claude effort process", str(tmp_path), backend="claude-code",
    )
    try:
        for effort in ("low", "high", None):
            if effort != "low":
                changed = await client.patch(f"/api/sessions/{session.id}", headers=client.headers,
                                             json={"reasoning_effort": effort})
                assert changed.status_code == 200, changed.text
            receipt = await manager.start_message(session.id, "Keep this history")
            result = await asyncio.wait_for(receipt.wait(), 5)
            assert result.outcome.status == "success"
        await AgentManager(manager.db).update_agent(agent["id"], reasoning_effort=None)
        receipt = await manager.start_message(session.id, "Follow the CLI default")
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
        records = [json.loads(line) for line in effects.read_text().splitlines()]
        assert [r["effort"] for r in records] == ["low", "high", "low", None]
        assert [r["env"] for r in records] == ["low", "high", "low", "max"]
        assert len({r["pid"] for r in records}) == 4
        assert [r["resume"] for r in records] == [None, *["original-thread"] * 3]
        assert session.claude_session_id == "original-thread"
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert len([m for m in detail.json()["messages"] if m["role"] == "user"]) == 4
    finally:
        await manager.stop_all_held_processes()

"""Create external exec history for migration checks, outside product execution."""

import asyncio
import json
import os
import signal

from server.config import settings
from server.harness import get_harness


async def seed_exec_history(manager, database, session, prompt):
    agent = await database.get_agent(session.agent_id)
    credential = await manager._resolve_credential(session, agent, get_harness("codex"))
    env = os.environ.copy()
    if credential is not None and credential.home_dir:
        env["CODEX_HOME"] = credential.home_dir
    process = await asyncio.create_subprocess_exec(
        settings.codex_app_server_binary, "exec", "--json", "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox", "-c", "mcp_servers={}",
        "-c", 'model_reasoning_effort="low"', "--", prompt,
        cwd=session.working_dir, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    identity, = [row["thread_id"] for row in rows if row.get("type") == "thread.started"]
    assert any(row.get("type") == "turn.completed" for row in rows), rows
    session.claude_session_id = identity
    await database.update_session_field(session.id, claude_session_id=identity)
    return identity

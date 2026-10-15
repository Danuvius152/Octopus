"""Deliver trusted controller snapshots through Codex's native thread API.

exec resume restores the original developer instructions. Configuring a new
developer_instructions string does not replace that history. Acknowledged
thread/inject_items checkpoints a real developer message without invoking a
model or editing the native transcript. Close and reap this writer before exec.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from ..aio import complete_cleanup, drain_cancelled
from .profile import TurnContext

_UPDATE_TIMEOUT = 20


async def refresh_instructions(ctx: TurnContext, argv: list[str], options: dict[str, Any]) -> None:
    if not ctx.resume_id:
        return
    from .run import _terminate_process_group

    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        argv[0], "app-server", "--listen", "stdio://", "-c", "mcp_servers={}",
        "-c", "features.retain_client_developer_messages=true",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024, **options,
    ), name="codex-controller-startup")
    stderr: asyncio.Task[None] | None = None
    stdout: asyncio.Task[None] | None = None

    async def drain(reader: asyncio.StreamReader) -> None:
        while await reader.read(64 * 1024):
            pass

    async def retire() -> None:
        nonlocal stderr, stdout
        try:
            process = await asyncio.shield(spawn)
        except Exception:
            return  # launch failed without a process to retire
        if stderr is None and process.stderr is not None:
            stderr = asyncio.create_task(drain(process.stderr))
        if stdout is None and process.stdout is not None:
            stdout = asyncio.create_task(drain(process.stdout))
        if process.returncode is None:
            _terminate_process_group(process, signal.SIGKILL)
        await process.wait()
        await drain_cancelled(stderr, "Codex controller stderr")
        await drain_cancelled(stdout, "Codex controller stdout")

    try:
        async with asyncio.timeout(_UPDATE_TIMEOUT):
            process = await asyncio.shield(spawn)
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            stderr = asyncio.create_task(drain(process.stderr), name="codex-controller-stderr")

            async def send(message: dict) -> None:
                process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
                await process.stdin.drain()

            async def request(number: int, method: str, params: dict) -> dict:
                await send({"id": number, "method": method, "params": params})
                while raw := await process.stdout.readline():
                    message = json.loads(raw)
                    if message.get("id") != number:
                        continue
                    if "error" in message:
                        error = message["error"]
                        raise RuntimeError(f"Codex controller {method} failed ({error.get('code')}): {str(error.get('message', 'RPC error'))[:300]}")
                    return message["result"]
                raise RuntimeError(f"Codex controller disconnected before acknowledging {method}")

            await request(1, "initialize", {"clientInfo": {"name": "octopus", "title": "Octopus", "version": "1"}})
            await send({"method": "initialized"})
            # Resuming also applies thread settings. Match the upcoming exec
            # invocation so preparation cannot briefly reset model or speed.
            config: dict[str, Any] = {"service_tier": ctx.service_tier or "default", "features.fast_mode": True}
            if ctx.reasoning_effort:
                config["model_reasoning_effort"] = ctx.reasoning_effort
            restored = await request(2, "thread/resume", {
                "threadId": ctx.resume_id, "excludeTurns": True, "cwd": ctx.working_dir,
                "model": ctx.model, "config": config,
            })
            if restored["thread"]["id"] != ctx.resume_id:
                raise RuntimeError("Codex controller resumed a different thread")
            await request(3, "thread/inject_items", {"threadId": ctx.resume_id, "items": [{
                "type": "message", "role": "developer", "content": [{"type": "input_text", "text":
                    "Current Octopus controller snapshot for this turn. It supersedes earlier Octopus controller "
                    "instructions, including obsolete Wiki lookup prohibitions.\n\n" + ctx.system_prompt}],
            }]})
            stdout = asyncio.create_task(drain(process.stdout), name="codex-controller-stdout")
            process.stdin.close()
            await process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"Codex controller exited {process.returncode} after updating instructions")
    finally:
        await complete_cleanup(retire())

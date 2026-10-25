"""Deliver changed controller instructions through Codex's native thread API.

exec resume restores the original developer instructions. Configuring a new
developer_instructions string does not replace that history. Acknowledged
thread/inject_items checkpoints a real developer message without invoking a
model or editing the native transcript. Compare the latest local checkpoint in
the active rollout returned by Codex, not a filename search or historical set.
An inherited or compacted history without an explicit checkpoint is established
once on its active branch. Close and reap this writer before exec.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import signal
from pathlib import Path
from typing import Any

from ..aio import complete_cleanup, drain_cancelled
from .profile import TurnContext

_UPDATE_TIMEOUT = 20
_START = '<octopus_controller sha256="'
_END = "\n</octopus_controller>"
_HEADER = re.compile(r'<octopus_controller sha256="([0-9a-f]{64})" characters="([0-9]+)">')


def controller_message(instructions: str) -> str:
    """A recognizable checkpoint also present in the first exec's instructions."""
    payload = ("Current Octopus controller instructions. These supersede earlier Octopus controller "
            "instructions, including obsolete Wiki lookup prohibitions.\n\n"
            + instructions)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f'{_START}{digest}" characters="{len(payload)}">\n{payload}{_END}'


def turn_message(instructions: str) -> str:
    return ("Current Octopus turn context; applies to this turn only and replaces earlier turn context. "
            "The stable controller instructions remain in effect.\n\n" + instructions)


def _checkpoint(item: dict[str, Any]) -> str | None:
    if item.get("type") != "message" or item.get("role") != "developer":
        return None
    checkpoint = None
    for part in item.get("content", []):
        text = part.get("text", "")
        if part.get("type") == "input_text" and text.startswith(_START):
            header, _, body = text.partition("\n")
            match = _HEADER.fullmatch(header)
            checkpoint = "invalid"
            if match:
                length = int(match[2])
                payload = body[:length]
                if (body[length:].startswith(_END)
                        and hashlib.sha256(payload.encode()).hexdigest() == match[1]):
                    checkpoint = header + "\n" + payload + _END
        elif text.startswith("Current Octopus controller snapshot for this turn."):
            checkpoint = "legacy"
    return checkpoint


def _latest_checkpoint(path: str, thread_id: str) -> str | None:
    """Read only this active segment; never mistake discarded branches for it.

    The native API identifies the current path, including after a revert. A
    reference to inherited history is intentionally not replayed here: a branch
    without its own controller checkpoint receives one, then normal resumes
    reuse it. This also avoids an application-owned delivery sidecar.
    """
    checkpoint = None
    with Path(path).open("rb") as handle:
        first = json.loads(handle.readline())
        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
            raise RuntimeError("Codex controller history identity mismatch")
        for raw in handle:
            record = json.loads(raw)
            payload = record.get("payload", {})
            if record.get("type") == "compacted":
                checkpoint = None
                for item in payload.get("replacement_history") or []:
                    checkpoint = _checkpoint(item) or checkpoint
            elif record.get("type") == "event_msg" and payload.get("type") == "thread_rolled_back":
                checkpoint = None
            elif record.get("type") == "response_item":
                checkpoint = _checkpoint(payload) or checkpoint
    return checkpoint


async def refresh_instructions(ctx: TurnContext, argv: list[str], options: dict[str, Any]) -> None:
    if not ctx.resume_id:
        return
    from ..process_groups import wait_group_exit
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
        _terminate_process_group(process, signal.SIGKILL)
        await process.wait()
        await wait_group_exit(process.pid, timeout=2.0, after_kill=True)
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
            thread = restored["thread"]
            if thread["id"] != ctx.resume_id:
                raise RuntimeError("Codex controller resumed a different thread")
            if not thread.get("path"):
                raise RuntimeError("Codex controller did not return an active history path")
            current = await asyncio.to_thread(_latest_checkpoint, thread["path"], ctx.resume_id)
            desired = controller_message(ctx.system_prompt)
            messages = [desired] if current != desired else []
            if ctx.turn_instructions:
                messages.append(turn_message(ctx.turn_instructions))
            if messages:
                await request(3, "thread/inject_items", {"threadId": ctx.resume_id, "items": [
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
                    for text in messages
                ]})
            stdout = asyncio.create_task(drain(process.stdout), name="codex-controller-stdout")
            process.stdin.close()
            await process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"Codex controller exited {process.returncode} after updating instructions")
    finally:
        await complete_cleanup(retire())

"""Codex one-shot text requests; this module has no model tool dispatcher."""

import argparse
import asyncio
import base64
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


def response_events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("Invalid one-shot response event")
            yield event


async def native(binary: str, home: Path, method: str, params: dict) -> dict:
    """Read native settings or refresh native auth; never start a model thread.

    The common one-shot process group owns this child as well. No separate
    OAuth writer or model/tool execution is installed in this control client.
    """
    if method not in {"getAuthStatus", "model/list", "config/read"}:
        raise ValueError("Unsupported one-shot native control operation")
    with tempfile.TemporaryDirectory(prefix="octopus-text-control-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            binary, "app-server", cwd=cwd, env={**os.environ, "CODEX_HOME": str(home)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
        )
        assert proc.stdin is not None and proc.stdout is not None

        async def request(identifier, operation, values):
            proc.stdin.write((json.dumps({"id": identifier, "method": operation, "params": values}) + "\n").encode())
            await proc.stdin.drain()
            while line := await proc.stdout.readline():
                result = json.loads(line)
                if result.get("id") == identifier:
                    if "error" in result:
                        raise ValueError("Codex native control request failed")
                    return result["result"]
            raise ValueError("Codex native control ended without a reply")

        try:
            async with asyncio.timeout(30):
                await request(1, "initialize", {"clientInfo": {"name": "octopus_text", "version": "1"}})
                proc.stdin.write(b'{"method":"initialized","params":{}}\n')
                await proc.stdin.drain()
                return await request(2, method, params)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()


def credentials(binary: str, home: Path, *, refresh: bool = False) -> tuple[str, str | None]:
    # This installed native control operation resolves the selected keystore
    # itself. Never infer native storage selection from a leftover auth.json.
    data = asyncio.run(native(binary, home, "getAuthStatus", {"includeToken": True, "refreshToken": refresh}))
    if not isinstance(data, dict) or data.get("authMethod") != "chatgpt":
        raise ValueError("A managed ChatGPT subscription login is required")
    token = data.get("authToken")
    if not isinstance(token, str) or not token:
        raise ValueError("The selected ChatGPT login is incomplete")
    account = None
    if token.count(".") == 2:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if not isinstance(claims, dict) or not isinstance(claims.get("https://api.openai.com/auth", {}), dict):
            raise ValueError("The selected ChatGPT token has invalid account claims")
        account = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if account is not None and not isinstance(account, str):
            raise ValueError("The selected ChatGPT token has invalid account identity")
    return token, account


def resolve_and_complete(client: httpx.Client, *, home: Path, binary: str, prompt: str,
                         model: str, effort: str | None = None, tier: str = "default") -> str:
    captured = credentials(binary, home)
    try:
        return complete(client, token=captured[0], account=captured[1], prompt=prompt,
                        model=model, effort=effort, tier=tier)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 401:
            raise
    # A concurrent native refresh or reauthorization takes precedence. Only a
    # rejected, still-current login asks Codex to refresh its own auth store.
    current = credentials(binary, home)
    token, account = credentials(binary, home, refresh=True) if current == captured else current
    return complete(client, token=token, account=account, prompt=prompt,
                    model=model, effort=effort, tier=tier)


def complete(client: httpx.Client, *, token: str, account: str | None, prompt: str,
             model: str, effort: str | None = None, tier: str = "default") -> str:
    payload: dict[str, Any] = {
        "model": model, "stream": True, "store": False,
        "instructions": "Complete the supplied text task.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
        "service_tier": tier,
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    headers = {"Authorization": "Bearer " + token, "originator": "codex_cli_rs"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
        response.raise_for_status()
        for event in response_events(response):
            if event.get("type") in {"response.output_item.added", "response.output_item.done"}:
                if event.get("item", {}).get("type") not in {"message", "reasoning"}:
                    raise ValueError("One-shot response attempted a non-text operation")
            if event.get("type") in {"response.failed", "response.incomplete", "error"}:
                raise ValueError("One-shot response did not complete")
            if event.get("type") == "response.completed":
                result = event["response"]
                if result.get("status") != "completed":
                    raise ValueError("One-shot response has no successful terminal status")
                output = result.get("output", [])
                if any(item.get("type") not in {"message", "reasoning"} for item in output):
                    raise ValueError("One-shot response contained a non-text operation")
                return "\n".join(part["text"] for item in output if item.get("role") == "assistant"
                                 for part in item.get("content", []) if part.get("type") == "output_text")
    raise ValueError("One-shot stream ended before completion")


def effective_options(binary: str, home: Path, cwd: str, model: str | None,
                      effort: str | None) -> tuple[str, str | None]:
    if model is not None and effort is not None:
        return model, effort
    config = asyncio.run(native(binary, home, "config/read", {"includeLayers": False, "cwd": cwd}))["config"]
    model = model or config.get("model")
    effort = effort or config.get("model_reasoning_effort")
    if model and effort:
        return model, effort
    cursor = None
    while True:
        page = asyncio.run(native(binary, home, "model/list", {"cursor": cursor}))
        for option in page["data"]:
            if option.get("model") == model or model is None and option.get("isDefault"):
                return option["model"], effort or option.get("defaultReasoningEffort")
        cursor = page.get("nextCursor")
        if cursor is None:
            if model is not None:
                return model, effort
            raise ValueError("Codex did not advertise a default model")


def main() -> int:
    parser = argparse.ArgumentParser(description="One model text request without executable tools")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--tier", choices=("default", "fast", "priority"), default="default")
    parser.add_argument("prompt")
    args = parser.parse_args()
    try:
        model, effort = effective_options(args.codex, args.home, os.getcwd(), args.model, args.effort)
        with httpx.Client(timeout=60) as client:
            text = resolve_and_complete(client, home=args.home, binary=args.codex, prompt=args.prompt,
                                        model=model, effort=effort,
                                        tier="priority" if args.tier == "fast" else args.tier)
        print(json.dumps({"text": text}))
        return 0
    except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
        # Provider/CLI diagnostics may contain bearer tokens. Report only the
        # failure class and HTTP status; the parent owns final outcome mapping.
        status = f" status={error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else ""
        print(f"Codex text request failed: {type(error).__name__}{status}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

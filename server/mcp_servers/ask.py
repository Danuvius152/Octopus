"""Async ask transport; the original SessionOwner retains creation and cleanup."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP

from ..sessions.mcp_questions import McpQuestionCall
from . import _host

logger = logging.getLogger(__name__)
mcp = FastMCP("octopus-ask")
_PER_POLL_TIMEOUT = 60.0
_MAX_CONSECUTIVE_COMMUNICATION_FAILURES = 3


async def _run_call(call: McpQuestionCall, questions: list[dict[str, Any]]) -> str:
    call.started = True
    api, sid = _host.api_base(), call.scope.session_id
    headers = _host.headers()
    creation: asyncio.Task[httpx.Response] | None = None
    reason = "communication_failure"
    try:
        if call.cancelled:
            return "Question call cancelled before creation. No answer was received."
        async with httpx.AsyncClient(headers=headers) as client:
            try:
                creation = asyncio.create_task(client.post(f"{api}/api/sessions/{sid}/questions",
                    json={"questions": questions, "mcp_call_id": call.id}, timeout=10.0))
                response = await asyncio.shield(creation)
                if response.status_code != 201:
                    raise ValueError(f"Error: Octopus rejected the question ({response.status_code}): {response.text[:300]}")
                qid = response.json().get("question_id")
                if not isinstance(qid, str) or call.pending is None or call.pending.question_id != qid:
                    raise ValueError("Error: Octopus returned an invalid original question receipt.")
                failures = 0
                while True:
                    try:
                        response = await client.get(f"{api}/api/sessions/{sid}/questions/{qid}/answer",
                            params={"timeout": _PER_POLL_TIMEOUT, "structured": True}, timeout=_PER_POLL_TIMEOUT + 5)
                    except httpx.HTTPError:
                        failures += 1
                    else:
                        if response.status_code < 500:
                            failures = 0
                            if response.status_code == 408:
                                continue
                            if response.status_code == 200:
                                body = response.json()
                                answer = body.get("answer") if isinstance(body, dict) else None
                                resolution = body.get("resolution") if isinstance(body, dict) else None
                                if (not isinstance(answer, str) or not isinstance(resolution, dict)
                                        or resolution.get("question_id") != qid
                                        or resolution.get("disposition") not in {"answered", "expired", "closed", "cancelled"}):
                                    raise ValueError("Error: host returned an invalid question answer.")
                                return answer
                            # Retirement may revoke the bearer before this
                            # HTTP observer returns. Only the original saved
                            # resolution, never the HTTP error, proves closure.
                            if call.pending.ready.is_set() and call.pending.resolution is not None:
                                return call.pending.answer or "Question closed. No answer was received."
                            raise ValueError(f"Error long-polling answer ({response.status_code}): {response.text[:300]}")
                        failures += 1
                    if failures >= _MAX_CONSECUTIVE_COMMUNICATION_FAILURES:
                        raise ValueError("Error: communication with Octopus failed repeatedly while waiting. No answer was received.")
                    await asyncio.sleep(float(failures))
            finally:
                call.cancelled = True
                if creation is not None:
                    try:
                        await asyncio.shield(creation)
                    except httpx.HTTPError:
                        pass  # The original Owner envelope is observed below.
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise ValueError(f"Error: question communication failed: {error}") from error
    finally:
        await call.finish(reason)


@mcp.tool(name="user")
async def ask_user(questions: list[dict[str, Any]], ctx: Context) -> str:
    """Ask the user and wait for the original question's resolution.

    Each question has question, optional header/multiSelect, and options of
    label/description. Normal unanswered results contain no user choice and
    grant no new permission; continue only within existing authorization.
    """
    scope = _host.scope()
    request = ctx.request_context.request
    if scope is None or request is None:
        raise ValueError("Original MCP question scope is unavailable")
    manager = request.scope.get("octopus.session_manager")
    if manager is None:
        raise ValueError("Original MCP host is unavailable")
    metadata = ctx.request_context.meta
    call = manager.register_mcp_question_call(scope, {
        "rpc_id": ctx.request_context.request_id,
        "mcp_session_id": request.headers.get("mcp-session-id"),
        "server": "ask", "tool": "user",
        "metadata": metadata.model_dump(by_alias=True, exclude_none=True) if metadata is not None else None,
    })
    call.task = asyncio.create_task(_run_call(call, questions), name=f"mcp-question-{call.id}")
    manager._own_task(call.task)
    try:
        return await asyncio.shield(call.task)
    except anyio.get_cancelled_exc_class():
        call.cancel()
        # SDK cancellation is an AnyIO level-triggered scope. Protect the
        # handler's join as well as the original runner, then propagate cancel.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(call.task, return_exceptions=True)
        if call.error is not None:
            logger.error("Original MCP question cleanup failed call=%s: %s", call.id, call.error)
        raise


if __name__ == "__main__":
    mcp.run()

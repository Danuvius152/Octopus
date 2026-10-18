"""MCP 1.x result normalization and complete UTF-8 envelope measurement.

Keep this boundary in step with the installed SDK's low-level call_tool
normalization. Result content is never clipped as serialized JSON text.
"""

import json
from typing import Any

from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent
from pydantic_core import to_json

TEXT_RESULT_MAX_BYTES = 32768


def text_result(value: Any, *, structured: bool = False, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=to_json(value, fallback=str, indent=2).decode())],
        structuredContent=value if structured else None,
        isError=is_error,
    )


def normalize_result(result: Any) -> CallToolResult:
    """Accept the SDK's text blocks, combined output, or explicit result object."""
    if isinstance(result, CallToolResult):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return CallToolResult(content=list(result[0]), structuredContent=result[1])
    if isinstance(result, dict):
        # A bare dict at the low-level boundary is structured-only output.
        # FastMCP's own converter uses the pydantic serializer before here.
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2))],
            structuredContent=result,
        )
    return CallToolResult(content=list(result))


def result_bytes(result: CallToolResult) -> int:
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def model_result_bytes(value: Any, *, structured: bool = False) -> int:
    return result_bytes(text_result(value, structured=structured))


def too_large(*, hint: str | None = None) -> dict:
    return {
        "code": "response_too_large", "status": 413,
        "error": "The minimum model response exceeds its UTF-8 envelope limit.",
        "hint": hint or "Use compact view, a smaller page or passage, or jobs(export=true) for full job evidence.",
        "response_limit_bytes": TEXT_RESULT_MAX_BYTES,
    }


def bounded_result(result: Any, *, binary: bool = False) -> CallToolResult:
    normalized = normalize_result(result)
    if (binary and len(normalized.content) == 1 and normalized.content[0].type == "image"
            and normalized.structuredContent is None and normalized.meta is None):
        return normalized
    if result_bytes(normalized) <= TEXT_RESULT_MAX_BYTES:
        return normalized
    return text_result(too_large(), is_error=True)


def bound_tool_handler(server: Any, *, binary_tools: frozenset[str] = frozenset()) -> None:
    """Wiki-local MCP 1.x hook after SDK validation, normalization and errors.

    request_handlers is an SDK internal interface, pinned by the constrained
    dependency and real request-dispatch tests. Do not change input validation
    mode, authentication, tool discovery or the shared transport/runtime here.
    """
    handlers = server._mcp_server.request_handlers
    original = handlers[CallToolRequest]

    async def bounded(request: CallToolRequest) -> ServerResult:
        result = await original(request)
        if isinstance(result.root, CallToolResult):
            return ServerResult(bounded_result(result.root, binary=request.params.name in binary_tools))
        return result

    handlers[CallToolRequest] = bounded

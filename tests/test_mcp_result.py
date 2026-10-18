"""Measure the public SDK's complete result, including duplicate structured output."""

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    TextContent,
)

from server import mcp_result


async def test_text_and_structured_measurements_match_the_installed_sdk_converter():
    payload = {"content": '中文😀\r\n\\"\x00'}
    server = FastMCP("serialization-control")

    @server.tool()
    def text_only() -> dict:
        return payload

    @server.tool()
    def both() -> dict[str, str]:
        return payload

    for name, structured in [("text_only", False), ("both", True)]:
        actual = mcp_result.normalize_result(await server.call_tool(name, {}))
        assert actual == mcp_result.text_result(payload, structured=structured)
        assert mcp_result.result_bytes(actual) == mcp_result.model_result_bytes(payload, structured=structured)


async def test_combined_output_can_exceed_the_limit_when_its_text_alone_fits():
    payload = {"content": "甲😀" * 3000}
    server = FastMCP("structured-budget-control")

    @server.tool()
    def both() -> dict[str, str]:
        return payload

    assert mcp_result.model_result_bytes(payload) < mcp_result.TEXT_RESULT_MAX_BYTES
    actual = await server.call_tool("both", {})
    assert mcp_result.result_bytes(mcp_result.normalize_result(actual)) > mcp_result.TEXT_RESULT_MAX_BYTES
    bounded = mcp_result.bounded_result(actual)
    assert mcp_result.result_bytes(bounded) <= mcp_result.TEXT_RESULT_MAX_BYTES
    assert bounded.isError
    assert json.loads(bounded.content[0].text)["code"] == "response_too_large"
    assert bounded.structuredContent is None


def test_existing_result_fields_are_measured_and_unchanged_when_they_fit():
    actual = CallToolResult(
        content=[TextContent(type="text", text="payload")], isError=True,
        structuredContent={"code": "refused"}, _meta={"revision": "a" * 64},
    )
    assert mcp_result.bounded_result(actual) is actual
    assert mcp_result.result_bytes(actual) == len(actual.model_dump_json(by_alias=True, exclude_none=True).encode())
    assert mcp_result.result_bytes(actual) > mcp_result.model_result_bytes("payload")


def test_metadata_and_structured_content_cannot_bypass_the_complete_boundary():
    payload = CallToolResult(content=[TextContent(type="text", text="small")],
                             structuredContent={"metadata": "a" * 40000})
    metadata = CallToolResult(content=[TextContent(type="text", text="small")],
                              _meta={"metadata": "a" * 40000})
    for actual in (payload, metadata):
        bounded = mcp_result.bounded_result(actual)
        assert bounded.isError and mcp_result.result_bytes(bounded) <= 32768


def test_binary_exception_is_explicit_and_does_not_exempt_text_only_errors():
    image = CallToolResult(content=[ImageContent(type="image", data="a" * 40000, mimeType="image/png")])
    assert mcp_result.bounded_result(image, binary=True) is image
    assert mcp_result.bounded_result(image).isError
    error = mcp_result.text_result({"error": "a" * 40000})
    assert mcp_result.bounded_result(error, binary=True).isError
    mixed = CallToolResult(content=[*image.content, TextContent(type="text", text="unexpected")])
    assert mcp_result.bounded_result(mixed, binary=True).isError


def test_bare_low_level_dict_matches_the_sdk_combined_result_contract():
    payload = {"message": "中文😀"}
    actual = mcp_result.normalize_result(payload)
    assert actual.structuredContent == payload
    assert actual.content[0].text == json.dumps(payload, indent=2)


async def test_outer_hook_bounds_sdk_generated_input_and_output_validation_errors():
    server = FastMCP("low-level-error-controls")

    @server.tool()
    def sample(value: int) -> dict[str, int]:
        return {"value": value}

    # These controls also pin the SDK branches that its default FastMCP
    # configuration currently disables or validates earlier with Pydantic.
    for invalid_input in (False, True):
        async def dispatch(name, arguments):
            return [TextContent(type="text", text="small")], {"value": "x" * 33000}

        server._mcp_server.call_tool(validate_input=True)(dispatch)
        mcp_result.bound_tool_handler(server)
        request = CallToolRequest(method="tools/call", params=CallToolRequestParams(
            name="sample", arguments={"value": "x" * 33000 if invalid_input else 1},
        ))
        actual = (await server._mcp_server.request_handlers[CallToolRequest](request)).root
        assert isinstance(actual, CallToolResult) and actual.isError
        assert mcp_result.result_bytes(actual) <= 32768
        assert json.loads(actual.content[0].text)["code"] == "response_too_large"

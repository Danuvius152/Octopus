from __future__ import annotations

import pytest

from server.showme_ai import (
    _bare_path_fallback,
    _format_messages,
    extract_json,
    resolve_showme_reference,
)


class FakeHarness:
    def __init__(self, out: str):
        self.out = out
        self.calls = []

    async def run_oneshot(self, ctx):
        self.calls.append(ctx)
        return self.out


async def _run(harness: FakeHarness, text: str = "this file", messages=None, **kwargs):
    return await resolve_showme_reference(
        text,
        harness=harness,
        model="m",
        credential=None,
        working_dir=kwargs.pop("working_dir", "/tmp/wd"),
        messages=messages if messages is not None else [],
        session_name=kwargs.pop("session_name", "Demo"),
        **kwargs,
    )


# --- Robust JSON extraction ---


def test_extract_json_bare_object():
    assert extract_json('{"path":"x.md"}') == {"path": "x.md"}


def test_extract_json_strips_markdown_fence_with_lang_tag():
    assert extract_json('```json\n{"path":"x.md"}\n```') == {"path": "x.md"}


def test_extract_json_strips_bare_markdown_fence():
    assert extract_json('```\n{"path":"x.md"}\n```') == {"path": "x.md"}


def test_extract_json_tolerates_surrounding_prose():
    # The defining real-CLI failure mode: Claude adds context before/after.
    raw = 'Looking at the conversation, the file is:\n\n{"path":"README.md"}\n\nHope that helps!'
    assert extract_json(raw) == {"path": "README.md"}


def test_extract_json_returns_none_for_no_object():
    assert extract_json("just prose, no json here at all") is None
    assert extract_json("") is None
    assert extract_json("{ not valid json") is None


@pytest.mark.asyncio
async def test_json_object_in_prose_resolves():
    harness = FakeHarness(
        'Looking at the conversation:\n\n{"path":"docs/plan.md"}\n\nDone.'
    )
    result = await _run(
        harness,
        messages=[{"role": "user", "type": "text", "content": "open the plan"}],
    )
    assert result.path == "docs/plan.md"


@pytest.mark.asyncio
async def test_clarification_message_passes_through():
    harness = FakeHarness('{"message":"Which file do you mean?"}')
    result = await _run(harness, session_name=None)
    assert result.path is None
    assert result.message == "Which file do you mean?"


# --- prompt includes double-check instruction ---


@pytest.mark.asyncio
async def test_prompt_contains_double_check_instruction():
    harness = FakeHarness('{"path":"README.md"}')
    await _run(harness)
    assert harness.calls, "model should have been called"
    prompt = harness.calls[0].prompt
    assert "double-check" in prompt


# --- Bare-path fallback ---


def test_bare_path_fallback_accepts_filename():
    assert _bare_path_fallback("README.md") == "README.md"


def test_bare_path_fallback_accepts_relative_path():
    assert _bare_path_fallback("docs/plan.md") == "docs/plan.md"


def test_bare_path_fallback_strips_trailing_lines():
    # Model says the path then adds a confirmation sentence.
    out = "README.md\nThat's the readme file."
    assert _bare_path_fallback(out) == "README.md"


def test_bare_path_fallback_rejects_prose():
    assert _bare_path_fallback("The README is at README.md") is None
    assert _bare_path_fallback("I'm not sure which file") is None


def test_bare_path_fallback_rejects_without_extension():
    # "README" alone is too ambiguous — could be a misread token.
    assert _bare_path_fallback("README") is None


@pytest.mark.asyncio
async def test_bare_token_response_resolves_as_path():
    # `claude --print` very commonly replies with just the path token.
    harness = FakeHarness("README.md")
    result = await _run(harness)
    assert result.path == "README.md"


# --- Unrecoverable response ---


@pytest.mark.asyncio
async def test_unrecoverable_response_returns_friendly_message():
    harness = FakeHarness("I'm not sure which file you mean — try giving the path.")
    result = await _run(harness)
    assert result.path is None
    assert result.message  # generic fallback


# --- _format_messages helper ---


def test_format_messages_includes_tool_use_with_path():
    out = _format_messages(
        [
            {"role": "user", "type": "text", "content": "show me the plan"},
            {
                "role": "assistant",
                "type": "tool_use",
                "tool_name": "Read",
                "tool_input": {"path": "docs/plan.md"},
            },
        ]
    )
    assert "User: show me the plan" in out
    assert "Read" in out
    assert "docs/plan.md" in out


def test_format_messages_skips_tool_use_without_path():
    out = _format_messages(
        [{"role": "assistant", "type": "tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}}]
    )
    assert out == ""

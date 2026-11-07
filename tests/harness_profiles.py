"""Scoped session CLI stand-ins retain the pure native configuration renderer."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from server.harness.claude_code import ClaudeDriver
from server.harness.codex import CODEX_WEB_LEAF
from server.harness.driver import ArgvDriver
from server.harness.profile import RuntimeProfile, TurnContext
from server.submission_models import NativeInputEvidence


class JsonlProcessDriver(ClaudeDriver):
    """Borrow JSONL input/parsing without claiming native history observations."""

    retains_retirement_output = False


def process_profile(profile: RuntimeProfile, **changes: Any) -> RuntimeProfile:
    """A declared synthetic process boundary for shared Owner assertions.

    These Python peers do not implement native RPC or provider input proofs.
    Real App Server wire, uncertainty and retirement use its controlled peer.
    """
    selected: dict[str, Any] = {"select_transport": None, "native_input_evidence": False}
    if profile.backend == "codex":
        selected.update(new_driver=ArgvDriver, can_steer=False, can_continue=True,
            inline_large_inputs=False, activity_observer=None, prepare_resume=None, fork_copy=None,
            auth_error_patterns=CODEX_WEB_LEAF.auth_error_patterns,
            transient_error_patterns=CODEX_WEB_LEAF.transient_error_patterns,
            transient_error_codes=CODEX_WEB_LEAF.transient_error_codes,
            stale_session_patterns=CODEX_WEB_LEAF.stale_session_patterns)
    selected.update(changes)
    if selected.get("new_driver", profile.new_driver) is ClaudeDriver:
        selected["new_driver"] = JsonlProcessDriver
    return replace(profile, **selected)


def native_submission(run, request_id: str, attempt_id: str) -> list[NativeInputEvidence]:
    """Original in-memory submission recorder for direct Harness seam checks."""
    evidence: list[NativeInputEvidence] = []

    async def record(value: NativeInputEvidence) -> None:
        evidence.append(value)

    run.bind_tool_context(request_id, attempt_id)
    run.bind_input_recorder(record)
    return evidence


def session_turn_argv(
    profile: RuntimeProfile,
    render: Callable[[TurnContext], tuple[list[str], dict[str, Any]]],
) -> Callable[[TurnContext], tuple[list[str], dict[str, Any]]]:
    def build(context: TurnContext) -> tuple[list[str], dict[str, Any]]:
        # These session fixtures execute with HTTP MCP bearers. A static save
        # has none; its native render must not publish an execution to waiters.
        if not any(entry.credential for entry in context.mcp_servers):
            return profile.build_turn_argv(context)
        return render(context)

    return build

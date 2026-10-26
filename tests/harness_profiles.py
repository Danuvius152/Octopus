"""Scoped session CLI stand-ins retain the pure native configuration renderer."""

from collections.abc import Callable
from typing import Any

from server.harness.profile import RuntimeProfile, TurnContext


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

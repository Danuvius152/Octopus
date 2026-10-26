"""Encoded exec argument/environment budgets, shared by every model launch."""

from __future__ import annotations

import os
import struct
import sys
from collections.abc import Mapping

# Account for the executable/interpreter and auxiliary exec stack overhead.
# Linux still applies its own current RLIMIT_STACK through SC_ARG_MAX.
EXEC_HEADROOM_BYTES = 16 * 1024


class LaunchConfigurationError(ValueError):
    """A known pre-launch configuration refusal; no process was submitted."""


def launch_limits() -> tuple[int, int]:
    try:
        total = os.sysconf("SC_ARG_MAX")
        single = 32 * os.sysconf("SC_PAGE_SIZE") if sys.platform == "linux" else 32 * 1024
    except (AttributeError, OSError, ValueError):
        # Conservative supported budgets on hosts without POSIX sysconf.
        total, single = 256 * 1024, 32 * 1024
    return single, min(total, 2 * 1024 * 1024) - EXEC_HEADROOM_BYTES


def validate_launch(argv: list[str], env: Mapping[str, str] | None = None) -> None:
    """Count OS-encoded bytes, NULs and pointer slots; never quote values."""
    environment = os.environ if env is None else env
    single_limit, total_limit = launch_limits()
    total = (len(argv) + len(environment) + 2) * struct.calcsize("P")

    def add(label: str, value: str) -> None:
        nonlocal total
        size = len(os.fsencode(value)) + 1
        if size > single_limit:
            raise LaunchConfigurationError(
                f"{label} uses {size} bytes; limit is {single_limit} bytes including the terminating NUL. "
                "Reduce system instructions, subagents or MCP configuration before retrying."
            )
        total += size

    labels = {"--append-system-prompt": "system_prompt", "--agents": "subagents", "--mcp-config": "MCP configuration"}
    for index, argument in enumerate(argv):
        label = labels.get(argv[index - 1], f"argument #{index}") if index else "executable"
        if argument.startswith("developer_instructions="):
            label = "system_prompt (encoded backend configuration)"
        add(label, argument)
    for index, (key, value) in enumerate(environment.items()):
        add(f"environment item #{index}", f"{key}={value}")
    if total > total_limit:
        raise LaunchConfigurationError(
            f"Combined arguments/environment use {total} bytes; supported limit is {total_limit} bytes "
            f"after {EXEC_HEADROOM_BYTES} bytes of exec headroom. Reduce system instructions, subagents, MCP configuration or environment size."
        )

"""MCP namespace: cross-turn background tasks.

The `bg` server exposes three tools to the model:

  - `bg_run(command, description?, idle_timeout_seconds?, timeout_seconds?)`
    — start a fire-and-forget bg task,
    returns task_id immediately. Use this for anything that may take
    longer than ~30 seconds (long build, test suite, npm install) so
    the user's turn can end while the work proceeds. When the task
    finishes, Octopus auto-fires a follow-up turn into this session
    with the captured output.

  - `bg_cancel(task_id)` — SIGTERM a running bg task.

  - `bg_list()` — recent bg tasks for this session (most recent first).
    Useful on a resumed turn if the model needs to check the state of
    something it queued before.

Channel: this namespace runs in Octopus's in-process HTTP MCP service.
Its HTTP shim calls the session API with the verified MCP bearer scope;
the model supplies commands and limits, while the scope supplies session,
owner and logical work identity. The session id is never a tool parameter.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

# Keep the import-path mirror from viewer.py — claude spawns us with
# arbitrary cwd, so import-style "server.foo" needs the repo root on
# sys.path even when PYTHONPATH wasn't set.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError

from ..bg_tasks import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    IDLE_AFTER_OUTPUT_TIMEOUT_SECS,
    MAX_TIMEOUT_SECONDS,
    BgTaskLimits,
    describe_limits,
)
from . import _host  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s bg-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("octopus-bg")


def _required_env(name: str) -> str | None:
    """Resolved from the request scope and settings rather than the process
    environment: this namespace is served in-process now, so one process
    handles every session (polish-2026-09.md §4 B1)."""
    v = _host.resolve(name)
    if not v:
        logger.error("%s unavailable for this call", name)
    return v


def _api_base() -> str | None:
    return _required_env("OCTOPUS_API_BASE")


def _session_id() -> str | None:
    return _required_env("OCTOPUS_SESSION_ID")


def _headers() -> dict[str, str] | None:
    tok = _required_env("OCTOPUS_AUTH_TOKEN")
    if not tok:
        return None
    return {"Authorization": f"Bearer {tok}"}


@mcp.tool(name="run")
def bg_run(
    command: str,
    description: str | None = None,
    idle_timeout_seconds: Annotated[int, Field(strict=True, ge=0, le=MAX_TIMEOUT_SECONDS)] = IDLE_AFTER_OUTPUT_TIMEOUT_SECS,
    timeout_seconds: Annotated[int, Field(strict=True, gt=0, le=MAX_TIMEOUT_SECONDS)] = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Start a background shell command in this session's working
    directory. Returns immediately with a task id; the command runs
    asynchronously and Octopus will inject a follow-up turn into this
    session with the captured stdout/stderr when it finishes.

    Use this for commands that may take longer than ~30 seconds (long
    builds, test suites, npm install, large fetches). For short
    commands (< 30s) use the regular Bash tool — bg has overhead from
    the follow-up turn and is not free.

    Args:
        command: The shell command to run. Executed under `/bin/sh -c`
            with cwd = session working_dir. Use full shell syntax
            (pipes, redirects, &&) freely. Subprocess output is capped
            at 200 KB per stream (truncated from start when over).
        description: Optional one-line label shown in the UI chip and
            in the follow-up turn (e.g. "running pytest"). The model
            should include this for any task it expects to outlast
            the current turn.
        idle_timeout_seconds: After the first stdout/stderr byte, stop if both
            streams stay silent this long. Default 60. Set 0 explicitly for a
            test/build with long quiet phases; no command-name exemption exists.
        timeout_seconds: Maximum total runtime, including quiet phases. Default
            1800; must be a positive integer even when the idle check is disabled.

    Returns:
        A short string explaining what was started, including the
        task_id. Cite that id back to the user if they ask about it.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: bg server is misconfigured (env vars missing); cannot start."
    if not command.strip():
        return "Error: command must be a non-empty shell command string."
    try:
        limits = BgTaskLimits(
            idle_timeout_seconds=idle_timeout_seconds, timeout_seconds=timeout_seconds,
        )
    except ValidationError:
        return ("Error: idle_timeout_seconds must be a non-negative integer; "
                "timeout_seconds must be a positive integer.")
    url = f"{api}/api/sessions/{sid}/bg-tasks"
    body = {"command": command, "description": description, **limits.model_dump()}
    try:
        r = httpx.post(url, json=body, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to start bg task: {e}"
    if r.status_code != 201:
        return f"Error: Octopus rejected the bg task ({r.status_code}): {r.text[:300]}"
    data = r.json()
    task_id = data.get("id", "?")
    desc = data.get("description")
    desc_part = f" ({desc})" if desc else ""
    actual_limits = describe_limits(data.get("idle_timeout_seconds"), data.get("timeout_seconds"))
    return (
        f"Started bg task `{task_id}`{desc_part} ({actual_limits}). The command is running in "
        f"the background; when it finishes you'll receive a follow-up turn "
        f"with the output. Tell the user briefly what's running, then end "
        f"your turn — don't wait."
    )


@mcp.tool(name="cancel")
def bg_cancel(task_id: str) -> str:
    """Cancel a running background task. SIGTERMs the subprocess (5s
    grace, then SIGKILL). Idempotent — cancelling an already-finished
    task is a no-op that returns ok=False.

    Args:
        task_id: The id returned by an earlier `bg_run` call.

    Returns:
        A short string describing whether the task was actually signalled
        or had already finished.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: bg server is misconfigured (env vars missing)."
    url = f"{api}/api/sessions/{sid}/bg-tasks/{task_id}/cancel"
    try:
        r = httpx.post(url, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to cancel bg task {task_id}: {e}"
    if r.status_code == 404:
        return f"No bg task `{task_id}` in this session."
    if r.status_code != 200:
        return f"Error cancelling bg task `{task_id}` ({r.status_code}): {r.text[:300]}"
    data = r.json()
    if data.get("cancelled"):
        return (
            f"Sent SIGTERM to bg task `{task_id}`; it should exit within a few "
            f"seconds and a follow-up turn will deliver the partial output."
        )
    return f"Bg task `{task_id}` was not running (already finished or not tracked)."


@mcp.tool(name="list")
def bg_list() -> str:
    """List recent background tasks for this session, most recent first.

    Use this when a follow-up turn references a task id the chat
    history is too long to scroll for, or when the user asks "what's
    running?". Output is capped — the most recent 25 entries — so
    don't dump it verbatim in long sessions.

    Returns:
        A short text summary. For programmatic detail, the user can
        check the UI chip popover.
    """
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: bg server is misconfigured (env vars missing)."
    url = f"{api}/api/sessions/{sid}/bg-tasks"
    try:
        r = httpx.get(url, headers=hdrs, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Octopus to list bg tasks: {e}"
    if r.status_code != 200:
        return f"Error listing bg tasks ({r.status_code}): {r.text[:300]}"
    items = r.json()
    if not items:
        return "No background tasks in this session yet."
    lines = [f"{len(items)} bg task(s) — most recent first:"]
    for item in items[:25]:
        desc = f" — {item['description']}" if item.get("description") else ""
        line = f"  • {item['id']}  [{item['status']}]{desc}"
        if item.get("exit_code") is not None:
            line += f"  (exit={item['exit_code']})"
        line += f"  ({describe_limits(item.get('idle_timeout_seconds'), item.get('timeout_seconds'))})"
        if item.get("stop_reason") is not None:
            line += f"  reason={item['stop_reason']}"
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

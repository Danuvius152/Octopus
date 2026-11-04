"""The Codex runtime profile.

Everything Codex-specific as data + collaborators behind the `CODEX`
`RuntimeProfile`: rendering a turn into a `codex exec --json` command (MCP
servers as `-c mcp_servers.*` TOML overrides, developer_instructions, the
exec-flags-before-`resume` ordering), normalizing its event stream, the
one-shot call (a model-only text worker using native Codex authentication),
and — wired in Phase 4 — the device-code login
driver. Codex has no transcript codec (handoff/pull unsupported).

Ported faithfully from the former `backends/codex.py`. Shared per-turn
assembly happens upstream in `assembly.py`; `build_turn_argv` only renders
the neutral `TurnContext`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

from ..models import MessageContent
from .codex_activity import CodexActivityObserver, diagnostic_environment
from .codex_control import controller_message, turn_message
from .driver import ArgvDriver
from .events import HarnessCredential, HarnessEvent, SubagentUpdate
from .fork import ForkArtifact
from .harness import Harness
from .login import LoginMethod, OAuthCompletion, OAuthCredentialWriter
from .model_settings import ModelOption, ModelSettings
from .profile import (
    EventParser,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    TurnContext,
    WebCapability,
)
from .registry import register

logger = logging.getLogger(__name__)


# Codex variant of the in-app-tools system prompt (same builtins as Claude;
# phrased for Codex's execution model). Injected via
# `-c developer_instructions=...` every turn.
_OCTOPUS_SYSTEM_PROMPT_CODEX = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. They are \
first-class — call them whenever appropriate, not as a fallback.

Keep large tool results in files. Use a tool's export option when available, \
or a short program that reads data from an API or an existing file. Do not \
paste entire JSON results into exec_command, python -c, heredocs or environment \
variables: the command itself is passed as one OS argument. If an exec fails \
with E2BIG / "Argument list too long", reuse an existing artifact or fetch \
only the needed page/fields; do not retry the same oversized command or \
repeatedly print and reconstruct the complete response.

[1] `mcp__bg__run(command, description?, idle_timeout_seconds?, timeout_seconds?)` — fire-and-forget a shell command \
that runs in the BACKGROUND across turns. Returns a task_id immediately; when \
the bg task finishes, Octopus injects a follow-up turn with the captured \
output. Use it for anything long-running or unbounded — test suites, builds, \
package installs, sleeps, large fetches. Start it, tell the user briefly what \
you started, then end your turn; a new turn arrives with the result \
(prefixed `[bg-task-result]`). Related: `mcp__bg__cancel(task_id)`, \
`mcp__bg__list()`.

Background commands stop by default after 60 seconds of silence following \
output. For long quiet tests or model calls, explicitly pass \
`idle_timeout_seconds=0`; a positive value sets this task's silence limit. \
`timeout_seconds` is a positive total time limit, default 1800 seconds. \
These task-level choices do not disable cancellation or the total limit.

[2] `mcp__ask__user(questions)` — ask the user clarification questions and \
BLOCK until they answer. Use it when a real choice depends on the user and \
there isn't an obviously right answer; don't use it for things you can decide \
yourself or verify from the workspace.

[3] `mcp__ask_agent__ask(name, request, files?)` — delegate to another \
Octopus agent by display name. When the user says "ask <name> to …", \
"delegate this to <name>", or "have <name> review …", that is a direct \
call to invoke this tool — don't paraphrase, just call it with a \
self-contained `request` (the other agent never sees this transcript). \
Returns immediately; the other agent's reply arrives later as a follow-up \
turn prefixed `[agent-reply:<name> delegation=<id>]` (or `[agent-question:…]` \
/ `[agent-error:…]`). If a question arrives, answer it via \
`mcp__ask_agent__answer(delegation_id, choice)` when you can, or ask \
the user via `mcp__ask__user` if you can't. For follow-up rounds \
with the same agent on the same line of work (review iterations, \
"apply that same review to file Y"), call `mcp__ask_agent__ask` \
again but pass the PRIOR `delegation_id` (and omit `name`) — the \
same child session is reused so the other agent keeps their \
transcript and doesn't re-read from scratch. Exactly one of (`name`, \
`delegation_id`) must be set. Related: `mcp__ask_agent__cancel`, \
`mcp__ask_agent__list`.

[4] `mcp__schedule__create(prompt, cron=…|interval_seconds=…|run_at=…, \
name=…, timezone=…, in_session=True)` — give yourself a durable schedule. \
Each fire arrives as a new turn carrying `prompt`, prefixed \
`[scheduled:<name> — <recurrence>]`. When the user says "every morning…", \
"every Monday…", "remind me in two hours", that is this tool — you can't \
stay awake between turns, so a sleep or a promise to check back later is \
just not doing it. Exactly one recurrence: `cron` (5-field crontab, for \
clock times), `interval_seconds` (min 60), or `run_at` (ISO datetime, \
one-shot). Timezone defaults to the host's. Write `prompt` self-contained: \
when it fires the conversation is gone and nobody can answer questions. \
Related: `mcp__schedule__list()`, `mcp__schedule__update(schedule_id, \
enabled=False|…)` (pause rather than delete when the user may want it \
back), `mcp__schedule__delete(schedule_id)`.

Native helpers created by `spawn_agent` run inside this parent's current \
turn. Collect every required result before ending the turn, or explicitly \
cancel the helper and report what was not delivered. A mailbox wait timeout \
does not mean a helper failed, and helper completion does not mean you have \
read its answer. Use the existing background commands or named-agent \
delegations for work that must continue across turns.

Prefer `apply_patch` for substantial text and code edits. Keep bulk file \
contents out of shell command arguments; use a file tool or smaller writes \
and verify the reconstructed result. If a command fails with "argument list \
too long", reduce the command size before retrying."""


def _toml_basic_string(value: str) -> str:
    """Render `value` as a TOML basic string for `-c key=<value>` overrides
    (codex parses the value as TOML). Mirrors VM0's quote_toml_basic_string."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    out = "".join(c if (ord(c) >= 0x20 or c in "") else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _toml_string_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_basic_string(i) for i in items) + "]"


def _apply_home_dir(env: dict[str, str], credential: HarnessCredential | None) -> None:
    """Apply a home_dir credential. CODEX_HOME isolates the subscription
    login (auth.json) and is where codex persists its own token refresh; a
    per-credential dir overrides the host default ~/.codex."""
    if credential is not None and credential.home_dir:
        env["CODEX_HOME"] = credential.home_dir


def read_model_settings(credential: HarnessCredential | None = None) -> ModelSettings:
    """Use Codex's own cached catalog; never guess model names or contact a model."""
    home = Path(
        credential.home_dir if credential and credential.home_dir
        else os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    )
    config: dict[str, Any] = {}
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError):
        pass
    profiles = config.get("profiles", {})
    profile_name = config.get("profile")
    profile = profiles.get(profile_name, {}) if isinstance(profiles, dict) and isinstance(profile_name, str) else {}
    if isinstance(profile, dict):
        config = {**config, **profile}
    default_model = config.get("model")
    default_effort = config.get("model_reasoning_effort")
    result = ModelSettings(
        default_model=default_model if isinstance(default_model, str) else None,
        default_reasoning_effort=default_effort if isinstance(default_effort, str) else None,
        reasoning_efforts=["low", "medium", "high", "xhigh"],
        service_tiers=["default", "fast"],
        # Octopus defaults to normal speed independently of CLI preferences.
        default_service_tier="default",
    )
    try:
        cached = json.loads((home / "models_cache.json").read_text())
        entries = cached.get("models", []) if isinstance(cached, dict) else []
    except (OSError, ValueError):
        entries = []
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        visible = entry.get("visibility") == "list" or slug == result.default_model
        levels = entry.get("supported_reasoning_levels", [])
        efforts = [
            level["effort"] for level in levels
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        ] if isinstance(levels, list) else []
        default = entry.get("default_reasoning_level")
        name = entry.get("display_name")
        tiers = ["default"]
        advertised = entry.get("service_tiers", [])
        speeds = entry.get("additional_speed_tiers", [])
        if (
            isinstance(advertised, list) and any(
                isinstance(tier, dict) and tier.get("id") in ("fast", "priority")
                for tier in advertised
            )
            or isinstance(speeds, list) and "fast" in speeds
        ):
            tiers.append("fast")
        result.add_model(ModelOption(
            id=slug, name=name if isinstance(name, str) else slug,
            reasoning_efforts=efforts,
            default_reasoning_effort=default if isinstance(default, str) else None,
            service_tiers=tiers,
        ), visible=visible)
        for effort in efforts if visible else []:
            if effort not in result.reasoning_efforts:
                result.reasoning_efforts.append(effort)
    return result


def _service_tier_args(tier: str | None) -> list[str]:
    # Explicit normal speed also clears Fast saved in CLI config or a resumed
    # thread. These per-invocation overrides never rewrite the user's config.
    return [
        "-c", f"service_tier={_toml_basic_string(tier or 'default')}",
        "-c", "features.fast_mode=true",
    ]


def _apply_mcp_bearers(env: dict[str, str], ctx: TurnContext) -> None:
    """Put each HTTP MCP server's bearer where Codex will look for it.

    `_mcp_config_args` tells Codex the *name* of an env var per server
    (`bearer_token_env_var`); this defines those vars. Claude needs no
    equivalent because it takes the credential inline as a header — the
    difference is Codex's, and it stops here at the profile boundary."""
    for e in ctx.mcp_servers:
        if e.is_http:
            env[_bearer_env_var(e.key)] = e.credential


def _mcp_config_args(ctx: TurnContext) -> list[str]:
    """`-c mcp_servers.<key>.*` overrides for the assembled MCP servers. We
    use per-invocation `-c` overrides (not a config.toml) so per-session
    callback env stays per-session while CODEX_HOME remains the stable
    per-credential auth dir."""
    args: list[str] = []
    for e in ctx.mcp_servers:
        base = f"mcp_servers.{e.key}"
        if e.tool_timeout_seconds is not None:
            args += ["-c", f"{base}.tool_timeout_sec={e.tool_timeout_seconds}"]
        if e.is_http:
            # Codex takes a URL plus the NAME OF AN ENV VAR to read the bearer
            # from — `--env` is stdio-only here, so unlike Claude there is no
            # way to pass an arbitrary header. That asymmetry is why the
            # session identity rides in the credential itself; see
            # McpServerEntry. The var is per-key so two namespaces (or two
            # connector installations) never read each other's.
            args += ["-c", f"{base}.url={_toml_basic_string(e.url)}"]
            args += [
                "-c",
                f"{base}.bearer_token_env_var={_toml_basic_string(_bearer_env_var(e.key))}",
            ]
            continue
        args += ["-c", f"{base}.command={_toml_basic_string(e.command)}"]
        args += ["-c", f"{base}.args={_toml_string_array(e.args)}"]
        for env_key, env_val in e.env.items():
            args += ["-c", f"{base}.env.{env_key}={_toml_basic_string(env_val)}"]
    return args


def _bearer_env_var(key: str) -> str:
    """The env var Codex reads an HTTP MCP server's bearer from. Per-key so
    namespaces stay isolated; uppercased because env vars conventionally are,
    and `-` is not legal in a shell identifier."""
    return f"OCTOPUS_MCP_BEARER_{key.upper().replace('-', '_')}"


# ------------------------------------------------------------------ turn argv


def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
    """Render the independent Research web leaf; ordinary input uses RPC."""
    if not ctx.web_research or ctx.resume_id or ctx.mcp_servers:
        raise ValueError("Codex exec is reserved for an independent web research leaf without MCP or resume")
    argv: list[str] = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        ctx.working_dir,
        "-c",
        "developer_instructions=" + _toml_basic_string("\n\n".join(filter(None, (
            controller_message(ctx.system_prompt), turn_message(ctx.turn_instructions) if ctx.turn_instructions else "",
        )))),
        "-c",
        "features.retain_client_developer_messages=true",
    ]
    # Native functions/collaboration remain available; read-only is not a
    # strict web-only whitelist. This independent worker has no Octopus MCP.
    argv += ["--sandbox", "read-only", "-c", "tools.web_search=true", "-c", "mcp_servers={}"]
    if ctx.model:
        argv += ["-m", ctx.model]
    if ctx.reasoning_effort:
        argv += ["-c", f"model_reasoning_effort={_toml_basic_string(ctx.reasoning_effort)}"]
    argv += _service_tier_args(ctx.service_tier)
    argv += ["--", ctx.prompt]

    env = os.environ.copy()
    _apply_home_dir(env, ctx.credential)
    diagnostic_environment(env)
    return argv, {"cwd": ctx.working_dir, "env": env}


# ------------------------------------------------------------------ event parsing


def _mcp_result_text(result: Any) -> str:
    """Flatten an MCP tool result into display text. Confirmed shape:
    `{content:[{type:"text",text:...}], structured_content:{result:...}}`."""
    if result is None:
        return ""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
        sc = result.get("structured_content")
        if isinstance(sc, dict) and "result" in sc:
            return str(sc["result"])
    return str(result)


# What each collaboration tool is doing, in words a card can show.
_COLLAB_DESCRIPTIONS = {
    "spawn_agent": "starting",
    "wait": "working",
    "wait_agent": "working",
    "send_input": "given more input",
    "resume_agent": "resumed",
    "close_agent": "closing",
}

_COLLAB_STATUSES = {
    "pending_init": ("running", None),
    "running": ("running", None),
    "completed": ("completed", None),
    "failed": ("failed", None),
    "error": ("failed", None),
    "errored": ("failed", None),
    "interrupted": ("interrupted", "native_interrupted"),
    "shutdown": ("interrupted", "native_shutdown"),
    "not_found": ("unknown", "native_not_found"),
    "cancelled": ("cancelled", "native_cancelled"),
}


def _short_thread(thread_id: str) -> str:
    """Codex names a sub-agent by its thread uuid; the card shows a handle."""
    return f"agent {thread_id[:8]}" if thread_id else ""


def _provider_code(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    code = error.get("codex_error_info") or error.get("code")
    if isinstance(code, str):
        return code
    if isinstance(code, dict) and len(code) == 1:
        return next(iter(code))
    return None


class CodexEventParser(EventParser):
    """Normalize `codex exec --json` into HarnessEvents. Holds the captured
    thread id (the resume id), surfaced early on `session_started`."""

    def __init__(self) -> None:
        self._captured_thread_id: str | None = None
        # child thread id -> the `spawn_agent` item that created it, so every
        # later `wait` / `send_input` on that thread updates the same card.
        self._collab_owner: dict[str, str] = {}
        self._collab_status: dict[str, str] = {}
        self._collab_completed: set[tuple[str, str]] = set()
        self._activity_last_item = -1
        self._activity_open_items: set[int] = set()
        self._activity_turn_started = False
        self._activity_schema_warned = False
        self._activity_web_items: dict[str, str] = {}
        self._activity_terminal = False
        self._started_at: float | None = None

    def begin_turn(self) -> None:
        self._started_at = time.monotonic()

    def _duration_ms(self) -> int | None:
        return round((time.monotonic() - self._started_at) * 1000) if self._started_at is not None else None

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        kind = obj.get("type")

        if kind == "thread.started":
            tid = obj.get("thread_id")
            first = self._captured_thread_id is None
            self._captured_thread_id = tid
            if tid:
                return ParseOutput(
                    events=[HarnessEvent(type="session_started", session_id=tid)],
                    activity=first,
                )
            return ParseOutput()

        if kind == "turn.started":
            if self._started_at is None:
                self.begin_turn()
            first = not self._activity_turn_started
            self._activity_turn_started = True
            return ParseOutput(activity=first)

        if kind == "turn.completed":
            self._activity_open_items.clear()
            self._activity_terminal = True
            usage = obj.get("usage") or {}
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="result",
                        session_id=self._captured_thread_id,
                        cost=None,  # Codex reports tokens, not USD
                        num_turns=1,
                        duration_ms=self._duration_ms(),
                        raw={"usage": usage},
                    )
                ],
                end_of_stream=True,
            )

        if kind == "turn.failed":
            self._activity_open_items.clear()
            self._activity_terminal = True
            err = obj.get("error")
            msg = err if isinstance(err, str) else (
                (err or {}).get("message") if isinstance(err, dict) else None
            )
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="result",
                        session_id=self._captured_thread_id,
                        is_error=True,
                        duration_ms=self._duration_ms(),
                        error_code=_provider_code(err),
                        content=msg or "Turn failed",
                        raw=obj,
                    )
                ],
                end_of_stream=True,
            )

        if kind == "error":
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="error",
                        is_error=True,
                        content=obj.get("message") or obj.get("error") or "Unknown error",
                        error_code=_provider_code(obj),
                        raw=obj,
                    )
                ]
            )

        if kind in ("item.started", "item.updated", "item.completed"):
            item = obj.get("item")
            return ParseOutput(
                events=self._item_events(kind, obj), activity=self._item_activity(kind, item),
            )

        return ParseOutput(diagnostics=[(logging.DEBUG, f"Unhandled codex event type: {kind}")])

    def _item_activity(self, kind: str, item: Any) -> bool | None:
        if self._activity_terminal:
            return False
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in {
            "reasoning", "agent_message", "mcp_tool_call", "command_execution",
            "collab_tool_call", "file_edit", "file_write", "file_read", "file_change",
            "web_search", "todo_list",
        }:
            return False
        # Updates carry no revision or event time. Keep rendering them, but
        # only provably new lifecycle events may extend the idle clock.
        if kind == "item.updated":
            return False
        identifier = item.get("id")
        # Codex 0.162 emits native Web identities alongside numeric item ids.
        # UUIDs have no ordering; track each lifecycle within this attempt.
        if item["type"] == "web_search" and isinstance(identifier, str) and identifier.startswith("exec-"):
            try:
                valid = str(uuid.UUID(identifier[5:])) == identifier[5:]
            except ValueError:
                valid = False
            if valid:
                previous = self._activity_web_items.get(identifier)
                if previous == "item.completed" or previous == kind:
                    return False
                self._activity_web_items[identifier] = kind
                return True
        number = identifier[5:] if isinstance(identifier, str) and identifier.startswith("item_") else ""
        ordinal = int(number) if len(number) <= 20 and number.isascii() and number.isdecimal() else None
        if ordinal is None or identifier != f"item_{ordinal}" or ordinal >= 2**64:
            if not self._activity_schema_warned:
                self._activity_schema_warned = True
                logger.warning("Codex stdout activity identity unsupported; using display activity")
            return None
        if ordinal > self._activity_last_item:
            self._activity_last_item = ordinal
            if kind == "item.started":
                self._activity_open_items.add(ordinal)
            return True
        if kind == "item.completed" and ordinal in self._activity_open_items:
            self._activity_open_items.remove(ordinal)
            return True
        return False

    def _collab_events(
        self, item: dict[str, Any], *, started: bool, completed: bool
    ) -> list[HarnessEvent]:
        """`collab_tool_call` → a tool call plus normalized sub-agent updates.

        Codex narrates multi-agent work as a sequence of tool calls against
        *child threads*: `spawn_agent` creates one, `wait` blocks on it,
        `send_input` talks to it. Several items therefore describe the same
        sub-agent. Its exact child thread is the task identity, and maps back
        to the spawning tool call for the card (native-subagents.md §3).
        """
        item_id = str(item.get("id") or "")
        tool = str(item.get("tool") or "agent")
        prompt = str(item.get("prompt") or "")
        raw_receivers = item.get("receiver_thread_ids")
        receivers = (
            [tid for tid in raw_receivers if isinstance(tid, str) and tid]
            if isinstance(raw_receivers, list) else []
        )
        states = item.get("agents_states")
        states = states if isinstance(states, dict) else {}

        events: list[HarnessEvent] = []
        spawning = tool == "spawn_agent"

        # The tool call itself, so the transcript shows the work and the card
        # has something to attach to (Claude's `Task` is a real tool call;
        # this makes Codex's equivalent one too).
        if started:
            events.append(
                HarnessEvent(
                    type="tool_use",
                    tool_name=f"subagent:{tool}",
                    tool_use_id=item_id,
                    tool_input={"prompt": prompt, "agents": receivers} if prompt or receivers else {},
                    raw=item,
                )
            )
        if completed:
            done = [
                str(st.get("message") or "")
                for st in states.values()
                if isinstance(st, dict) and st.get("message")
            ]
            events.append(
                HarnessEvent(
                    type="tool_result",
                    tool_use_id=item_id,
                    content="\n\n".join(done) if done else tool,
                    raw=item,
                )
            )

        # Which card each child thread belongs to.
        targets: list[tuple[str | None, str]]
        if spawning:
            for tid in receivers:
                self._collab_owner[tid] = item_id
            targets = [(item_id, tid) for tid in receivers]
        else:
            targets = [
                (self._collab_owner.get(tid), tid) for tid in receivers
            ]

        for owner, tid in targets:
            call = (tid, item_id)
            if call in self._collab_completed:
                continue
            if completed and item_id:
                self._collab_completed.add(call)
            raw_state = states.get(tid)
            state = raw_state if isinstance(raw_state, dict) else {}
            raw_status = str(state.get("status") or "")
            status, stop_reason = _COLLAB_STATUSES.get(raw_status, ("unknown", None))
            previous = self._collab_status.get(tid)
            accepted_execution = (
                completed and bool(item_id) and item.get("status") == "completed"
                and tool in {"spawn_agent", "send_input", "resume_agent"}
            )
            accepted_continuation = accepted_execution and not spawning
            if accepted_execution and spawning and not raw_status:
                status = "running"
            elif accepted_continuation and raw_status not in {
                "not_found", "failed", "error", "errored", "interrupted", "shutdown", "cancelled",
            }:
                if previous == "running":
                    status, stop_reason = "unknown", "overlapping_execution"
                elif previous in {"completed", "failed", "cancelled", "interrupted"}:
                    status, stop_reason = "running", None
                else:
                    status, stop_reason = "unknown", None
            elif not spawning:
                if previous in {"completed", "failed", "cancelled", "interrupted"} and status in {"running", "unknown"}:
                    continue
                if not raw_status and previous is not None:
                    continue
                if status == "running" and previous != "running":
                    status = "unknown"
            self._collab_status[tid] = status
            events.append(
                HarnessEvent(
                    type="subagent",
                    subagent=SubagentUpdate(
                        task_id=tid,
                        tool_use_id=owner,
                        status=status,
                        stop_reason=stop_reason,
                        name=_short_thread(tid),
                        description=_COLLAB_DESCRIPTIONS.get(tool, tool),
                        prompt=prompt if spawning else "",
                        summary=str(state.get("message") or ""),
                    ),
                    raw=item,
                )
            )
        return events

    def _item_events(self, kind: str, obj: dict[str, Any]) -> list[HarnessEvent]:
        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        item_id = item.get("id")
        started = kind == "item.started"
        completed = kind == "item.completed"

        if item_type == "collab_tool_call":
            return self._collab_events(item, started=started, completed=completed)

        if item_type == "agent_message":
            text = item.get("text")
            if completed and text:
                return [HarnessEvent(type="text", content=text, raw=item)]
            return []

        if item_type == "reasoning":
            text = item.get("text")
            if completed and text:
                return [HarnessEvent(type="thinking", content=text, raw=item)]
            return []

        if item_type == "mcp_tool_call":
            server = item.get("server") or ""
            tool = item.get("tool") or ""
            tool_name = f"mcp__{server}__{tool}"
            if started:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name=tool_name,
                        tool_use_id=item_id,
                        tool_input=item.get("arguments") or {},
                        raw=item,
                    )
                ]
            if completed:
                err = item.get("error")
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=(str(err) if err is not None else _mcp_result_text(item.get("result"))),
                        is_error=err is not None,
                        raw=item,
                    )
                ]
            return []

        if item_type == "command_execution":
            if started and item.get("command") is not None:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name="Bash",
                        tool_use_id=item_id,
                        tool_input={"command": item.get("command")},
                        raw=item,
                    )
                ]
            if completed:
                output = item.get("aggregated_output")
                if output is None:
                    output = item.get("output") or ""
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=output,
                        is_error=item.get("exit_code") not in (0, None),
                        raw=item,
                    )
                ]
            return []

        if item_type in ("file_edit", "file_write", "file_read"):
            tool = {"file_edit": "Edit", "file_write": "Write", "file_read": "Read"}[item_type]
            if started and item.get("path") is not None:
                return [
                    HarnessEvent(
                        type="tool_use",
                        tool_name=tool,
                        tool_use_id=item_id,
                        tool_input={"file_path": item.get("path")},
                        raw=item,
                    )
                ]
            if completed:
                return [
                    HarnessEvent(
                        type="tool_result",
                        tool_use_id=item_id,
                        content=item.get("diff") or "File operation completed",
                        is_error=False,
                        raw=item,
                    )
                ]
            return []

        if item_type == "file_change":
            changes = item.get("changes")
            if kind == "item.completed" and isinstance(changes, list) and changes:
                summary = "\n".join(
                    f"{c.get('kind', 'change')}: {c.get('path', '')}" for c in changes
                )
                return [HarnessEvent(type="text", content=summary, raw=item)]
            return []

        return []


# ------------------------------------------------------------------ one-shot (D2)


def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
    """A model-only worker, owned and retired by the common one-shot engine."""
    from .run import _which_with_fallback, augmented_path

    env = os.environ.copy()
    _apply_home_dir(env, ctx.credential)
    binary = _which_with_fallback("codex")
    if binary is None:
        raise FileNotFoundError("codex")
    env["PATH"] = augmented_path(env.get("PATH"), os.path.dirname(binary))
    home = env.get("CODEX_HOME") or str(Path.home() / ".codex")
    argv = [sys.executable, "-I", str(Path(__file__).with_name("codex_text.py")),
            "--home", home, "--codex", binary, "--tier", ctx.service_tier or "default"]
    if ctx.model:
        argv += ["--model", ctx.model]
    if ctx.reasoning_effort:
        argv += ["--effort", ctx.reasoning_effort]
    argv += ["--", ctx.prompt]
    return argv, {"cwd": ctx.working_dir or os.getcwd(), "env": env}


def parse_oneshot_stdout(stdout: str) -> str:
    """Only the model worker's completed text is eligible for consumption."""
    try:
        result = json.loads(stdout)
    except ValueError:
        return ""
    return result.get("text", "") if isinstance(result, dict) and isinstance(result.get("text"), str) else ""


# ------------------------------------------------------------------ fork (HISTORY_REPLAY)


async def _fork_prepare_replay(
    messages: list[MessageContent],
    working_dir: str,
    resume_id_hint: str | None,
    fork_id: str,
) -> ForkArtifact:
    """Rewind uses product seq, which can split one native turn at a steer.
    The retained history therefore travels in the first explicit USER input,
    without approximating it as a native turn count. No creation or model
    execution occurs here; the hint is unused."""
    return ForkArtifact(resume_id=None, needs_replay=True)


def _codex_sessions_dir(credential: HarnessCredential | None) -> Path:
    """CODEX_HOME/sessions — codex's rollout store (date-partitioned files named
    ``rollout-<ts>-<id>.jsonl``). The id is cwd-independent, so a copy resumes
    from anywhere. Uses the credential's CODEX_HOME, else the host default
    ~/.codex."""
    home = (
        credential.home_dir
        if credential is not None and credential.home_dir
        else os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    )
    return Path(home) / "sessions"


async def _cleanup_legacy_fork(
    working_dir: str,  # unused: rollouts live under CODEX_HOME, not the cwd
    resume_id_hint: str | None,
    fork_id: str,
    *,
    credential: HarnessCredential | None = None,
) -> None:
    """Remove a copied rollout left by an incomplete full-copy saga. Rollouts
    live under CODEX_HOME (not the working dir), so this needs the credential to
    locate the store. No-op for /rewind (replay copies no rollout — nothing
    named by the hint exists)."""
    if not resume_id_hint:
        return
    sessions = _codex_sessions_dir(credential)
    # The caller minted this id for the incomplete fork, so exact id suffixes
    # identify its artifacts without reading a possibly unreadable transcript.
    # Include the old filename format for copies made by earlier versions.
    if not sessions.is_dir():
        return
    patterns = (
        f"rollout-????-??-??T??-??-??-{resume_id_hint}.jsonl",
        f"rollout-fork-{resume_id_hint}.jsonl",
    )
    for copied in (p for pattern in patterns for p in sessions.rglob(pattern)):
        try:
            copied.unlink()
        except FileNotFoundError:
            pass  # already gone — idempotent
        except OSError:
            # RE-RAISE: a returning cleanup is read as success and the caller
            # then deletes the DB row, which would strand this rollout. Keep the
            # row by surfacing the failure for a later retry.
            logger.exception(
                "fork %s: failed to remove copied rollout %s", fork_id, copied
            )
            raise


# ------------------------------------------------------------------ login driver


class _DeviceLoginDriver:
    """Codex's device-code login, wrapping the CodexLoginManager singleton.
    `codex login --device-auth` runs against a per-credential CODEX_HOME; the
    UI polls `get()` for the verification URL + code and for success. Owns the
    on-disk cleanup (CODEX_HOME) so the credentials router doesn't branch on
    kind."""

    method = LoginMethod.device_code

    async def start(
        self,
        label: str | None = None,
        *,
        reauth_credential_id: str | None = None,
        user_id: str | None = None,
    ):
        from ..codex_login import codex_login_manager

        return await codex_login_manager.start(
            (label or "").strip(),
            reauth_credential_id=reauth_credential_id,
            user_id=user_id,
        )

    async def complete_oauth(self, request: OAuthCompletion, credentials: OAuthCredentialWriter):
        raise NotImplementedError("device_code login polls; it has no code to submit")

    def get(self, login_id: str):
        from ..codex_login import codex_login_manager

        return codex_login_manager.get(login_id)

    async def cancel(self, login_id: str) -> None:
        from ..codex_login import codex_login_manager

        await codex_login_manager.cancel(login_id)

    async def delete_credential(self, credential_id, user_id, remove):
        from ..codex_login import codex_login_manager

        return await codex_login_manager.delete_credential(credential_id, user_id, remove)

    def owns_credential(self, credential_id: str, user_id: str | None) -> bool:
        from ..codex_login import codex_login_manager

        return codex_login_manager.owns_credential(credential_id, user_id)


# ------------------------------------------------------------------ profile


# Phrases Codex / the OpenAI backend emit when the stored ChatGPT auth has
# expired or been revoked (harness-credential-reauth.md §3). Codex surfaces
# these in `turn.failed` / `error` event text; we also scan its stderr.
_CODEX_AUTH_ERROR_PATTERNS = (
    "401 unauthorized",
    "error 401",
    "invalid authentication credentials",
    "invalid api key",
    "incorrect api key",
    "your authentication token has expired",
    "please run `codex login`",
    "please run codex login",
    "not logged in",
    # What a lapsed ChatGPT login actually says, verbatim, in `turn.failed`:
    # "Your access token could not be refreshed. Please log out and sign in
    # again." Without these the message only matched via the CLI's stderr
    # (which happens to carry a websocket "401 Unauthorized"), so the
    # structured error alone read as a generic failure and the user got no
    # Re-authorize prompt. Both phrases are auth-specific, per the note below.
    "access token could not be refreshed",
    "log out and sign in again",
    # NB: deliberately NOT a bare "unauthorized" / "token has expired" — those
    # appear in non-auth failures (an MCP/connector 401, a tool's "GitHub
    # Unauthorized") that escalate to a Codex `error`/`turn.failed`, and would
    # wrongly flag the harness credential dead. Patterns must be auth-specific.
)

# Transient provider-reliability failures worth an automatic retry
# (harness-transient-retry.md §3). Server-side 5xx / overload / dropped
# connection only — NO "rate limit" / "429" / "quota" (the user's limit) and
# no auth phrases (handled above).
_CODEX_TRANSIENT_ERROR_PATTERNS = (
    "selected model is at capacity",
    "overloaded",
    "error 500",
    "error 502",
    "error 503",
    "error 504",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "connection error",
    "connection reset",
    "connection refused",
    "stream error",
    "stream disconnected",
    "request timed out",
    "timed out",
    # Server-side throttle (NOT the user's usage limit) — retryable.
    "temporarily limiting requests",
    "not your usage limit",
)


def select_transport(base: RuntimeProfile, transport: str) -> RuntimeProfile:
    if transport == "exec":
        return CODEX_WEB_LEAF
    if transport == "app-server":
        from .codex_app_server import profile
        return profile(base)
    raise ValueError(f"Unknown Codex transport: {transport}")


CODEX_WEB_LEAF = RuntimeProfile(
    separate_turn_instructions=True,
    model_settings=read_model_settings,
    backend="codex",
    binary="codex",
    tools_prompt="",
    credential_style="home_dir",
    premature_exit_recovery=False,
    auth_error_patterns=_CODEX_AUTH_ERROR_PATTERNS,
    transient_error_patterns=_CODEX_TRANSIENT_ERROR_PATTERNS,
    transient_error_codes=("server_overloaded",),
    # Single combined search-and-read tool, enabled per-leaf via the
    # web_research render path (-c tools.web_search=true). native-deep-research.md §4.
    web=WebCapability(tool_names=("web_search",), combined=True),
    new_driver=ArgvDriver,
    select_transport=select_transport,
    can_steer=False,
    reusable=False,
    can_continue=False,
    build_turn_argv=build_turn_argv,
    activity_observer=CodexActivityObserver,
    new_event_parser=CodexEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    # Codex has no usable native memory in exec, so it reads/writes the shared
    # per-agent markdown dir with file tools, driven by the injected blurb
    # (docs/implemented/memory.md §3). Memory is decoupled from CODEX_HOME; the
    # canonical dir is ensured by session_manager.
    injects_memory_prompt=False,
    login=_DeviceLoginDriver(),
    transcript_codec=None,  # Codex has no handoff/pull transcript format
)

from .codex_app_server import profile as _app_server_profile  # noqa: E402

CODEX = _app_server_profile(CODEX_WEB_LEAF)
register(Harness(CODEX))

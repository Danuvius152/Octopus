"""Shared per-turn context assembly — the de-duplicated middle.

Both harnesses need the same three things computed before rendering argv:
the callback env our in-app MCP servers use to reach the host, the
selected set of MCP servers (built-ins per the agent's choice + connector
installations), and the composed system prompt (persona + the harness's
in-app-tools blurb + the connectors blurb). This module owns all three so
adding a new in-app tool (e.g. memory) is a single-point change rather
than an edit in every profile. The profile's `build_turn_argv` only
*renders* the neutral result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .events import HarnessCredential
from .profile import McpServerEntry, RuntimeProfile, StdinMode, TurnContext

if TYPE_CHECKING:
    from .run import RunConfig


def assemble_turn(
    profile: RuntimeProfile, config: RunConfig, prompt: str, working_dir: str,
    resume_id: str | None = None, credential: HarnessCredential | None = None, *,
    run_lease_id: str | None = None, work_id: str | None = None,
) -> TurnContext:
    """Pure inputs for static validation and actual execution, with no run,
    parser, observer or process creation. Runtime identities are supplied by
    the existing run owner when present.
    """
    profile.tool_policy.validate(profile.backend, config.tool_allow, config.tool_deny)
    wiki_scope_id = config.wiki_task_id
    if profile.stdin_mode is StdinMode.STREAM_JSON and config.session_id and wiki_scope_id:
        from ..wiki.tasks import RUN_WIKI_SCOPE
        wiki_scope_id = RUN_WIKI_SCOPE
    mcp_servers = select_mcp_servers(
        config.mcp_servers, config.connectors, build_callback_env(config.session_id),
        session_id=config.session_id, user_id=config.user_id, wiki_task_id=wiki_scope_id,
        run_lease_id=run_lease_id, work_id=work_id,
    )
    system_prompt = compose_system_prompt(
        config.system_prompt, profile.tools_prompt, config.connectors,
        memory_dir=config.memory_dir, inject_memory=profile.injects_memory_prompt,
        wiki_mode=config.wiki_mode, wiki_capability=True, wiki_task_id=config.wiki_task_id,
    )
    turn_instructions = "\n\n".join(filter(None, (config.turn_instructions, config.fork_note)))
    if not profile.separate_turn_instructions:
        system_prompt = "\n\n".join(filter(None, (system_prompt, turn_instructions)))
    return TurnContext(
        prompt=prompt, working_dir=str(Path(working_dir).resolve()), resume_id=resume_id,
        system_prompt=system_prompt, model=config.model, reasoning_effort=config.reasoning_effort,
        service_tier=config.service_tier, tool_allow=config.tool_allow, tool_deny=config.tool_deny,
        mcp_servers=mcp_servers, credential=credential, memory_dir=config.memory_dir,
        web_research=config.web_research, subagents=config.subagents,
        turn_instructions=turn_instructions if profile.separate_turn_instructions else "",
    )

# Repo root (contains the `server/` package): needed to launch each MCP
# server via `-m server.mcp_servers.<name>` and to set PYTHONPATH so the
# import resolves regardless of the child's cwd.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)

# In-app MCP servers the agent can enable (subset of these; None = all).
_BUILTIN_MODULES = {
    "bg": "server.mcp_servers.bg",
    "ask": "server.mcp_servers.ask",
    # Agent-to-agent delegation (agent-collaboration.md §5.1). Same
    # callback-env shape as ask/bg — talks to the FastAPI delegations
    # routes which sit in front of DelegationManager.
    "ask_agent": "server.mcp_servers.ask_agent",
    # Native deep research (native-deep-research.md §7). Thin shim to the
    # /api/sessions/{sid}/research routes in front of ResearchManager.
    "research": "server.mcp_servers.research",
    # The agent's own schedules (schedule-tool.md §4). Thin shim to the
    # session-scoped /api/sessions/{sid}/schedules routes.
    "schedule": "server.mcp_servers.schedule",
}


def repo_root() -> str:
    return _REPO_ROOT


def build_callback_env(session_id: str | None) -> dict[str, str]:
    """The env our bg/ask MCP servers use to call back into FastAPI."""
    from ..config import settings as _settings  # local import: avoid cycle at load

    env: dict[str, str] = {
        "OCTOPUS_API_BASE": f"http://127.0.0.1:{_settings.port}",
        "OCTOPUS_AUTH_TOKEN": _settings.auth_token,
        "PYTHONPATH": _REPO_ROOT,
    }
    if session_id:
        env["OCTOPUS_SESSION_ID"] = session_id
    return env


def select_mcp_servers(
    mcp_servers: list[str] | None,
    connectors: list[tuple[Any, Any]],
    callback_env: dict[str, str],
    session_id: str | None = None,
    user_id: str | None = None,
    wiki_task_id: str | None = None,
    run_lease_id: str | None = None,
    work_id: str | None = None,
) -> list[McpServerEntry]:
    """The built-in servers the agent enabled (None = all builtins) plus one
    entry per enabled connector installation. Order is stable: bg, ask, then
    connectors — so rendered argv is deterministic for tests. Unknown names in
    `mcp_servers` (e.g. a legacy `"viewer"` from before the viewer became a
    client-only flow) are silently filtered.

    With a `session_id` the entries are **streamable-HTTP**, pointing at the
    namespaces this app already serves — nothing is spawned
    (polish-2026-09.md §4 B1). Without one there is no session to scope a
    bearer to, so they fall back to the stdio form; `build_argv` uses that for
    argv inspection, and it is the only remaining caller of the old shape.
    """
    if session_id:
        return _http_entries(mcp_servers, connectors, session_id, user_id, wiki_task_id, run_lease_id, work_id)
    builtin_specs: dict[str, dict[str, Any]] = {
        "bg": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["bg"]],
            "env": dict(callback_env),
        },
        "ask": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["ask"]],
            "env": dict(callback_env),
        },
        "ask_agent": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["ask_agent"]],
            "env": dict(callback_env),
        },
        "research": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["research"]],
            "env": dict(callback_env),
        },
        "schedule": {
            "command": sys.executable,
            "args": ["-m", _BUILTIN_MODULES["schedule"]],
            "env": dict(callback_env),
        },
    }
    if mcp_servers is not None:
        selected = {k: v for k, v in builtin_specs.items() if k in mcp_servers}
    else:
        selected = builtin_specs

    entries: list[McpServerEntry] = [
        McpServerEntry(key=key, command=spec["command"], args=spec["args"], env=spec["env"])
        for key, spec in selected.items()
    ]

    # Agent-enabled connectors (connectors.md §5.6). Each contributes one
    # per-installation entry keyed `<kind>_<id6>` so two accounts of one
    # kind don't collide; the connector builds the {command,args,env} shape.
    for connector, installation in connectors:
        entry = connector.mcp_entry(installation, callback_env)
        entries.append(
            McpServerEntry(
                key=connector.mcp_key(installation),
                command=entry["command"],
                args=entry["args"],
                env=entry["env"],
            )
        )
    return entries


def _http_entries(
    mcp_servers: list[str] | None,
    connectors: list[tuple[Any, Any]],
    session_id: str,
    user_id: str | None = None,
    wiki_task_id: str | None = None,
    run_lease_id: str | None = None,
    work_id: str | None = None,
) -> list[McpServerEntry]:
    """The same namespaces, served over HTTP by this process.

    Two things stay exactly as they were, deliberately. The **key** is
    unchanged, because both CLIs build the tool name from it — a different key
    would rename every `mcp__<key>__<tool>` and invalidate every prompt that
    documents them. And connector keys stay per-installation even though all
    installations of a kind share one URL: the key and the URL are independent,
    so the name can stay stable while the mount is shared, and which
    installation a call belongs to comes from its bearer instead.
    """
    from ..config import settings as _settings
    from ..mcp_http import mount_path
    from ..mcp_identity import mint

    base = f"http://127.0.0.1:{_settings.port}"

    def entry(key: str, namespace: str, installation_id: str | None = None) -> McpServerEntry:
        return McpServerEntry(
            key=key,
            url=f"{base}{mount_path(namespace)}/mcp",
            # The bearer carries whose call this is as well as which session
            # (multi-tenancy.md §7), so a tool body has an owner without a
            # round trip and the signature covers it.
            credential=mint(session_id, installation_id, user_id, wiki_task_id, run_lease_id, work_id),
            installation_id=installation_id,
            # Ask waits for a human. Keep the client's tool deadline beyond
            # the host's answer deadline; Codex otherwise defaults to 60s.
            # Its numeric setting has no documented infinity value. The
            # positive sentinel allows a disabled host deadline in practice
            # without treating zero as a native timeout-disable instruction.
            tool_timeout_seconds=(
                _settings.ask_user_question_timeout_seconds + 60
                if _settings.ask_user_question_timeout_seconds > 0 else 2**31 - 1
            ) if namespace == "ask" else None,
        )

    names = list(_BUILTIN_MODULES) if mcp_servers is None else [
        n for n in _BUILTIN_MODULES if n in mcp_servers
    ]
    entries = [entry(n, n) for n in names]
    if wiki_task_id:
        entries.append(entry("wiki", "wiki"))
    for connector, installation in connectors:
        entries.append(
            entry(
                connector.mcp_key(installation),
                _connector_namespace(connector),
                _installation_id(installation),
            )
        )
    return entries


def _installation_id(installation: Any) -> str | None:
    """The installation's id, however it arrives.

    `ConnectorInstallation` is a model with an `.id`, which is what the
    connector framework passes and what `mcp_key` itself reads — but the same
    pair is a plain row in places that load it straight from the database.
    Reading only one shape silently produced `None`, which would have left a
    connector call unable to say *which* account it was for while still looking
    correct in every other respect.
    """
    got = getattr(installation, "id", None)
    if got:
        return str(got)
    if isinstance(installation, dict):
        raw = installation.get("id")
        return str(raw) if raw else None
    return None


def _connector_namespace(connector: Any) -> str:
    """Which mount serves this connector. Built-ins have their own module;
    every user-defined kind is served by the generic one."""
    kind = getattr(connector, "kind", "") or ""
    return kind if kind in ("github", "gmail") else "custom"


def render_memory_blurb(memory_dir: str) -> str:
    """Instructions for an agent's durable, cross-session markdown memory
    (docs/implemented/memory.md §3). Injected only for harnesses without native
    memory (Codex); Claude relies on its own auto-injected MEMORY.md. The
    format mirrors Claude's native memory (frontmatter facts + a MEMORY.md
    index) so the two are interchangeable on a backend switch."""
    return f"""\
== Long-term memory ==

You have a durable, cross-session memory for this agent, stored as Markdown \
files at:
  {memory_dir}

At the START of a task, read `{memory_dir}/MEMORY.md` (if it exists) to recall \
what you already know about this user and their projects. When you learn a \
durable fact worth keeping for future sessions — a stable preference, a \
project constraint, how the user wants things done — record it:
  - write/update a focused file `{memory_dir}/<slug>.md` with YAML frontmatter \
(`name`, `description`, `metadata.type` = user|feedback|project|reference) \
followed by the fact in prose;
  - add/update a one-line pointer in `{memory_dir}/MEMORY.md`: \
`- [Title](<slug>.md) — short hook`.
Keep entries durable and de-duplicated: update an existing file rather than \
adding a near-duplicate, and delete a file (and its MEMORY.md line) when it \
becomes wrong. Don't record secrets, throwaway details, or this session's \
transient state. Use your normal file-read/-write tools for these files."""


def compose_system_prompt(
    persona: str | None,
    tools_prompt: str,
    connectors: list[tuple[Any, Any]],
    memory_dir: str | None = None,
    inject_memory: bool = False,
    fork_note: str | None = None,
    wiki_mode: str | None = None,
    wiki_capability: bool = False,
    wiki_task_id: str | None = None,
) -> str:
    """persona (if any) ahead of the harness's in-app-tools blurb, then the
    connectors blurb (if any), then the memory blurb (when the harness has no
    native memory and an agent memory dir exists), then the fork first-turn
    note (session-rewind.md §5.6.4 — framing the model needs to read the
    forked world correctly). The runtime delivers request-specific instructions
    separately; unchanged Codex controller checkpoints are reused on resume."""
    from ..connectors.base import render_connectors_blurb

    out = tools_prompt
    if persona:
        out = f"{persona}\n\n{tools_prompt}"
    if connectors:
        out = f"{out}\n\n{render_connectors_blurb(connectors)}"
    if inject_memory and memory_dir:
        out = f"{out}\n\n{render_memory_blurb(memory_dir)}"
    if fork_note:
        out = f"{out}\n\n{fork_note}"
    if wiki_capability and wiki_mode is None:
        out += "\n\nLocal Wiki is not configured or is unavailable for this account; no Wiki tools are available in this turn."
    if wiki_mode in {"auto", "read", "maintain"}:
        from ..wiki.workflow import instructions
        out += "\n\n" + instructions(wiki_mode, wiki_task_id)
    return out

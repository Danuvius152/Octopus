"""The `RuntimeProfile` — one data record per harness kind.

This is the heart of the data-driven design (VM0's `Record<framework,…>`
shape in Python): there are no `ClaudeCodeHarness`/`CodexHarness`
subclasses. There is one `Harness` class, one `HarnessRun` engine, and
two `RuntimeProfile` *values* (`CLAUDE_CODE` in claude_code.py, `CODEX` in
codex.py) that supply the few genuinely per-framework pieces — argv
rendering, event parsing, one-shot, login, transcript codec — as data +
small collaborators.

The shared `assembly.py` pre-computes the neutral inputs (selected MCP
servers, composed system prompt) into a `TurnContext`; the profile's
`build_turn_argv` only renders that into a concrete CLI command.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .events import HarnessCredential, HarnessEvent
from .fork import ForkCleanup, ForkCopy, ForkPrepare
from .login import LoginDriver
from .model_settings import ModelSettings

if TYPE_CHECKING:
    from .driver import RunDriver


@dataclass
class McpServerEntry:
    """Backend-neutral MCP server spec.

    Built once by `assembly.select_mcp_servers`; each profile renders the
    list into its own config form (`--mcp-config` JSON for Claude,
    `-c mcp_servers.*` TOML for Codex).

    Two transports, distinguished by whether `url` is set:

    - **stdio** (`command` + `args` + `env`): the CLI spawns the server as a
      subprocess. This is the connector `mcp_entry` shape.
    - **streamable-HTTP** (`url` + `credential`): the CLI connects to a server
      the main app already hosts, so nothing is spawned. `credential` is the
      per-session bearer the server resolves an identity from — with stdio it
      travelled in `env`, which an HTTP server has no equivalent of.

    `key` is the namespace either way, and it is load-bearing: both CLIs derive
    the tool name from it (`mcp__<key>__<tool>`), so a key change renames every
    tool and invalidates every prompt that documents them.
    """

    key: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    credential: str = ""
    tool_timeout_seconds: int | None = None
    # Stable account binding carried by an HTTP bearer. The namespace only
    # contains a short ID; rotating lease/signature bytes do not change this ID.
    installation_id: str | None = None

    @property
    def is_http(self) -> bool:
        """Whether this entry is served over streamable-HTTP rather than
        spawned. Derived from the data, so no caller has to track a mode flag
        (the same reasoning as the profile's derived capabilities)."""
        return bool(self.url)


@dataclass(frozen=True)
class WebCapability:
    """How a backend does web search/fetch, for native deep research
    (native-deep-research.md §4). A profile with `web = None` has no web tools,
    so deep research is unavailable on it (gated like `can_fork`).

    `tool_names` are the backend's native web tool identifiers (Claude:
    WebSearch/WebFetch; Codex: web_search) — used to allow them on a research
    leaf and to recognize web activity in the event stream. `combined` marks a
    single search-and-read tool (Codex) vs separate search + fetch (Claude)."""

    tool_names: tuple[str, ...]
    combined: bool = False


@dataclass
class TurnContext:
    """Fully-assembled, neutral inputs for one turn — what `build_turn_argv`
    renders. The shared work (MCP selection, system-prompt composition,
    working-dir absolutization) already happened in `assembly.py`."""

    prompt: str
    working_dir: str                  # absolute
    resume_id: str | None
    system_prompt: str                # composed: persona + tools blurb + connectors
    model: str | None
    tool_allow: list[str] | None
    tool_deny: list[str] | None
    mcp_servers: list[McpServerEntry]  # selected built-ins + connectors
    credential: HarnessCredential | None
    # Per-agent native memory (docs/implemented/memory.md): the canonical markdown
    # dir both harnesses point at. None when there's no owning agent.
    memory_dir: str | None = None
    # Native-deep-research web leaf (native-deep-research.md §4): when True, the
    # profile renders a scoped turn with web tools and no Octopus MCP. Claude
    # restricts tools; Codex exec uses its native read-only sandbox, which is
    # not a strict web-only tool allowlist.
    web_research: bool = False
    # Sub-agent definitions to register for this turn (native-subagents.md §6).
    # A profile without a surface for them simply doesn't render them.
    subagents: list[dict[str, Any]] = field(default_factory=list)
    reasoning_effort: str | None = None
    service_tier: str | None = None
    # Request-specific trusted instructions; excluded from stable controller
    # comparison and delivered separately on a resumed Codex thread.
    turn_instructions: str = ""


@dataclass
class OneShotContext:
    """Inputs for a lean, tool-free single model call (`run_oneshot`)."""

    prompt: str
    model: str | None = None
    credential: HarnessCredential | None = None
    working_dir: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    authorize_spawn: Callable[[], Awaitable[None]] | None = None


@dataclass
class ParseOutput:
    """Result of feeding one stdout JSON object to an `EventParser`."""

    events: list[HarnessEvent] = field(default_factory=list)
    end_of_stream: bool = False
    # A recognized runtime event can carry no displayable text.
    activity: bool | None = None
    # The run logs parser notes with its captured credential context.
    diagnostics: list[tuple[int, str]] = field(default_factory=list)


class ActivityObserver(Protocol):
    def observe_stdout(self, obj: dict[str, Any]) -> None: ...
    def consume_stderr(self, line: str) -> bool: ...
    async def start(self) -> None: ...
    def invalidate(self) -> None: ...
    def set_event_handler(self, handler: Callable[[list[HarnessEvent]], Awaitable[None] | None]) -> None: ...
    async def drain_observations(self) -> None: ...
    async def close(self, *, deadline: float | None = None) -> bool: ...


class ActivityObserverFactory(Protocol):
    def __call__(self, env: Mapping[str, str], notify: Callable[[float], None], *,
                 resume_id: str | None = None) -> ActivityObserver: ...


class EventParser(ABC):
    """Per-turn stdout normalizer. A fresh instance is created for each run
    (`profile.new_event_parser()`), so it may hold the small per-turn state
    the protocols need — e.g. the captured session/thread id surfaced on
    `session_started` before `result` arrives."""

    def begin_turn(self) -> None:
        """Start local measurements when the harness accepts this attempt."""
        return None

    @abstractmethod
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        """Map one parsed stdout JSON object to zero+ events, flagging
        end-of-stream when the turn's terminal event (result) lands."""


class TranscriptCodec(Protocol):
    """Read/write a harness's on-disk transcript format (export/import).

    Present only for harnesses that support handoff/pull (Claude's JSONL);
    `None` on a profile means export/import is unsupported (Codex)."""

    def parse_file(self, path: str) -> Any: ...
    def write_file(
        self,
        path: str,
        messages: list[Any],
        session_id: str | None,
        working_dir: str | None,
    ) -> None: ...


@dataclass(frozen=True)
class ToolPolicySupport:
    """Restrictions this adapter can enforce, independent of auto-approval."""

    allow: bool = False
    deny: bool = False

    def validate(self, backend: str, allow: str | list[str] | None, deny: str | list[str] | None) -> None:
        for field_name, supported, value in (("tool_allow", self.allow, allow), ("tool_deny", self.deny, deny)):
            entries = value.splitlines() if isinstance(value, str) else value or []
            if not supported and any(entry.strip() for entry in entries):
                raise ValueError(
                    f"{backend} cannot enforce {field_name}. Clear this Agent policy in Agent settings "
                    "or select a backend that supports it."
                )


@dataclass(frozen=True)
class RuntimeProfile:
    """Everything that differs between harness kinds, as one record."""

    backend: str                 # "claude-code" | "codex" (matches the persisted field)
    binary: str                  # "claude" | "codex"
    tools_prompt: str            # in-app-tools blurb (per-framework wording)
    credential_style: str        # "env_secret" | "home_dir"
    # Internal Claude-CLI bug workaround flag (not a product capability):
    # the session_manager run loop respawns with "continue" after a
    # premature mid-turn exit only when this is set.
    premature_exit_recovery: bool
    # Per-run protocol state and operations; no process or product ownership.
    new_driver: Callable[[RuntimeProfile], RunDriver]
    # Renderers / parsers (module functions in the profile's file):
    build_turn_argv: Callable[[TurnContext], tuple[list[str], dict[str, Any]]]
    new_event_parser: Callable[[], EventParser]
    build_oneshot_argv: Callable[[OneShotContext], tuple[list[str], dict[str, Any]]]
    parse_oneshot_stdout: Callable[[str], str]
    select_transport: Callable[[RuntimeProfile, str], RuntimeProfile] | None = None
    # Ordinary invocation selection can differ from the independent leaf/text
    # CLI. Discovery calls the same selector without starting a process.
    turn_binary: Callable[[], str] | None = None
    # The Owner saves an original native-input preparation record before the
    # driver's fallible startup. Synthetic and non-native profiles omit it.
    native_input_evidence: bool = False
    # JSON-RPC accepts the original large input directly. Existing transports
    # retain their file-spill convention and its attachment lifecycle.
    inline_large_inputs: bool = False
    # Independent adapter guarantees. An open stdin pipe proves neither.
    can_steer: bool = False
    reusable: bool = False
    # Resume a captured conversation with a new continuation input. This
    # neither replays the original input nor guarantees exactly-once effects.
    can_continue: bool = False
    tool_policy: ToolPolicySupport = field(default_factory=ToolPolicySupport)
    # Request instructions have a native per-turn channel on some adapters;
    # otherwise they are fixed in the process's composed system prompt.
    separate_turn_instructions: bool = False
    # Lowercased substrings that identify an auth-credential rejection in
    # THIS backend's CLI error output (harness-credential-reauth.md §3). A
    # failed turn whose combined error text contains any of them is treated
    # as an expired/invalid credential; `Harness.is_auth_error` matches them.
    # Empty tuple = no reactive auth detection for this backend.
    auth_error_patterns: tuple[str, ...] = ()
    transient_error_codes: tuple[str, ...] = ()
    # Web search/fetch capability for native deep research (§4); None = the
    # backend has no web tools, so research is gated off on it.
    web: WebCapability | None = None
    # Lowercased substrings that identify a TRANSIENT provider-reliability
    # failure (5xx / overloaded / dropped connection / timeout) in this
    # backend's CLI error output (harness-transient-retry.md §3). A failed
    # turn matching these is retried with backoff. Must stay free of auth
    # phrases (handled separately) and quota/credit phrases (never retried).
    transient_error_patterns: tuple[str, ...] = ()

    # Phrases meaning "the resume id this session is pinned to no longer
    # exists on this engine" — its local transcript was rotated, cleaned or
    # written by another machine. Distinct from auth (the credential is fine)
    # and from transient (the same missing id cannot be resumed). Only
    # pre-submission evidence permits fresh history preparation; losing
    # native history after possible execution never permits prompt replay.
    stale_session_patterns: tuple[str, ...] = ()
    # Whether the composed system prompt should carry the agent-memory blurb
    # (docs/implemented/memory.md §3). Codex: True (no native memory — it reads/
    # writes the canonical dir with file tools by instruction). Claude: False
    # (native memory, pointed at the canonical dir via an env override).
    injects_memory_prompt: bool = False
    # Whether this backend can be forked (session-rewind.md §3). A
    # backend supplies `prepare_fork` + a working resume strategy
    # (NATIVE_TRANSCRIPT or HISTORY_REPLAY). Both v1 backends set True; a
    # future backend with no strategy leaves it False and the "Fork from
    # here" affordance renders disabled. Surfaced via `SessionInfo.can_fork`.
    can_fork: bool = False
    # Model names this harness's CLI is known to accept, offered by `/model`.
    # A *shortlist*, not a whitelist: the route stores whatever string it is
    # given, because both CLIs take names this list cannot know about and a
    # model released next week must not need an Octopus release. Empty is
    # fine — `/model <name>` still works, and the picker also offers whatever
    # models the user's own agents and sessions already use.
    models: tuple[str, ...] = ()
    # Read-only discovery from the selected runtime's local metadata. No model
    # call or auth refresh, and no credentials/config contents in the result.
    model_settings: Callable[[HarnessCredential | None], ModelSettings] | None = None
    # Collaborators (optional features):
    activity_observer: ActivityObserverFactory | None = None
    # Deliver current controller instructions before resuming a runtime whose
    # native history otherwise retains its initial developer message.
    prepare_resume: Callable[[TurnContext, list[str], dict[str, Any]], Coroutine[Any, Any, None]] | None = None
    login: LoginDriver | None = None
    transcript_codec: TranscriptCodec | None = None
    # Fork strategy collaborators (session-rewind.md §3). `fork_prepare`
    # synthesizes backend-specific resume state and returns a `ForkArtifact`;
    # `fork_cleanup` sweeps any partial artifacts left by an incomplete saga.
    # Both async. None on a backend with no fork strategy (can_fork stays
    # False).
    fork_prepare: ForkPrepare | None = None
    fork_cleanup: ForkCleanup | None = None
    # Full-copy fork (session-fork.md): prepare independent native history by
    # file copy or the backend's API, without running a model turn. The actual
    # returned id, not the caller's hint, is authoritative. The duplicate continues
    # with real context
    # instead of replaying the whole history into the first prompt. Returns a
    # `ForkArtifact`; falls back to `needs_replay=True` when the parent has no
    # native transcript yet. None on a backend with no native-copy strategy
    # (callers then fall back to replay).
    fork_copy: ForkCopy | None = None

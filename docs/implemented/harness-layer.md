# Harness: current runtime boundary

> **Status:** implemented — One public Harness with runtime profiles, shared execution ownership and explicit capabilities.

## Responsibility and rationale

Feature code expresses intent through `Harness`: streaming turns, one-shot text,
login, transcript operations and capability queries. `RuntimeProfile` supplies
backend-specific argv rendering, parsing, login and transcript collaborators.
The registry currently contains `claude-code` and `codex`; unknown kinds fail
explicitly. Availability means the native executable can be resolved, not that
credentials or every model capability have been verified.

One public boundary keeps schedules, research and sessions from each choosing
binaries, constructing credentials or interpreting native protocol events.
Profiles compose the real differences without a hierarchy of backend-specific
SessionManagers or public Harness subclasses. Capability predicates derive from
the supplied collaborators; unsupported operations fail rather than silently
changing backend or dropping a requested tool restriction.

The wire and database field remains `backend`, and the shared resume field is
still named `claude_session_id`. The latter stores either backend's native
identity; its old spelling does not make consumers Claude-specific. These names
avoid a persistence/API migration for a vocabulary-only change.

## Assembly, execution and retirement

`assembly.py` prepares a neutral `TurnContext`: absolute cwd, persona and trusted
turn instructions, selected MCP entries, credential, memory location and model
settings. A profile renders that context into its protocol. MCP entries retain
namespace, transport and identity; changing a namespace changes model-visible
tool names. Connector and built-in tools share this assembly path.

SessionOwner owns mutable execution state. Managed preparation, spawn, stdin,
reader and retirement operations report their original results to it. HarnessRun
retains the native resources and reader backpressure through confirmed retirement.
Input delivery, committed business outcome, notification and resource exit are
separate facts under the [execution contract](architecture-contract.md#输入完成与资源退休).

Claude uses stream-JSON input and can retain a process between turns. Codex exec
closes stdin after its initial prompt and queues subsequent user input. Settings
changes invalidate the held-process signature when needed while preserving the
native resume identity. See [steering](inline-steering.md),
[model settings](model-reasoning.md) and [turn safety](turn-safety.md).

## One-shot, login and transcripts

`run_oneshot` is a bounded, tool-free text operation. Schedule parsing, file
reference resolution and research reasoning use it without inheriting a full
interactive agent session. The current Codex implementation uses a dedicated
text worker; its authentication, failure and protocol boundaries are in
[the Codex contract](codex-backend.md#single-call-text-contract).

Login is exposed through profile login drivers rather than routed independently
by every feature. Claude OAuth and Codex native device login keep their actual
differences. Credential refresh and reauthorization have their own
[ownership contract](harness-credential-reauth.md).

Transcript import/export and fork are separate capabilities. Claude's transcript
codec supports handoff/pull; lack of a Codex codec produces an explicit unsupported
result. Both backends support rewind replay and full native-copy fork through
their respective profile functions; see [rewind](session-rewind.md) and
[full copy](session-fork.md). Fork support does not imply a public export codec.

## Source and verification

The owning code is [Harness](../../server/harness/harness.py),
[profiles](../../server/harness/profile.py), [assembly](../../server/harness/assembly.py),
[run engine](../../server/harness/run.py) and [registry](../../server/harness/registry.py).
`tests/test_harness_core.py`, `tests/test_harness_claude.py`,
`tests/test_harness_codex.py`, `tests/test_harness_flow_control.py` and
`tests/test_harness_authorization_stop.py` exercise these boundaries.

Codex app-server, complete native tails and DSH integration remain
[planned changes](../plans/engineering-runtime-roadmap.md), not current capabilities.
The [original implementation](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-layer.md)
contains old API sketches and rollout evidence; current verification follows
[testing.md](../testing.md).

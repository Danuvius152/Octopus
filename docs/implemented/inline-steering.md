# Input steering, streaming and held processes

> **Status:** implemented — Claude stream-JSON steering and process reuse; Codex exec queues later input.

## Current capability and delivery

Steering follows `RuntimeProfile.can_steer`, independently of `reusable` and
tool-policy support. Claude declares steering and reuse with `STREAM_JSON`;
Codex exec declares neither with `CLOSE_AFTER_SPAWN`. Session API views expose
the explicit steering capability; Owner dispatch also checks the active phase
and input eligibility. A native stdin channel grants no capability by itself.

| Evidence | Current guarantee |
| --- | --- |
| Committed input claim | The original input has an execution identity; delivery is not proven. |
| Steer `confirmed` | The complete local frame write/drain returned; provider acceptance or processing is not proven. |
| Codex exec `turn.completed` | The current parser's root ended; complete native descendants and parent continuation are not proven. |

A failed or cancelled frame write may have sent part or all of its input.
The original input keeps its unknown delivery fact; a successful root does not
upgrade that fact. These fields remain compatible without a new provider ACK layer.

All user input first follows the durable [input contract](../input-reliability.md).
SessionOwner owns dispatch eligibility and ordering. A supported steer is sent by
the managed stdin writer, retaining its input/request/execution identity across
awaits. Echoes are correlated by identity, not matching prompt text. A completed
primary request cannot silently prove successful delivery of another input.

Ordinary Stop pauses dispatch and preserves its backlog. New human correction can
run while that backlog remains paused; explicit resume restores queued dispatch.
BG, Research and delegation returns remain separate inputs in successful-admission
order. They are not automatically batched or converted to native steer. Current
boundaries are maintained in the
[execution contract](architecture-contract.md#输入完成与资源退休).

## Output and process lifetime

Incremental text improves perceived responsiveness without making a partial delta
a persisted final message. The event pipeline coalesces display updates and keeps
completed message identity separate from in-progress text.

Claude can retain a process after a turn, avoiding a spawn for the next compatible
turn. The process's configuration signature controls reuse; changed settings,
credential, cwd or controller requirements can require replacement. Native resume
identity survives an eligible replacement. Idle reaping and capacity limits bound
held resources; original process group and reader retirement still require proof.
Codex exec does not acquire held-process steering by sharing this public Harness.

Process reuse, within-turn steering, business completion and native descendant
completion are separate capabilities. In particular, the first parent result does
not prove its native tail is complete. The selected app-server migration and
complete-tail work remain [future scope](../plans/engineering-runtime-roadmap.md).

## Rationale and verification

The original Claude experiments showed that tool boundaries can consume streamed
input while uninterrupted generation may postpone it. That motivated an explicit
delivery protocol rather than a promise of instantaneous interruption. Their
latencies and cache measurements are version-specific, not current SLAs.

Code: [Owner runtime](../../server/sessions/owner_runtime.py),
[presentation](../../server/sessions/presentation.py),
[HarnessRun](../../server/harness/run.py),
[Claude profile](../../server/harness/claude_code.py).
Verification entry points include `tests/test_session_dispatch.py`,
`tests/test_harness_claude.py`, `tests/test_harness_flow_control.py` and
`tests/test_session_capability_projection.py`.

The [original experiments and rationale](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/inline-steering.md)
retain their tested CLI version and conditions. Current test policy is in
[testing.md](../testing.md).

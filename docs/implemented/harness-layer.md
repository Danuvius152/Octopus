# Harness: current runtime boundary

> **Status:** implemented — One public Harness with runtime profiles, shared execution ownership and explicit capabilities.

## Responsibility and rationale

Feature code expresses intent through `Harness`: streaming turns, one-shot text,
login, transcript operations and capability queries. `RuntimeProfile` supplies
backend-specific drivers, login and transcript collaborators. Each run gets its
own driver for argv rendering, preparation, input encoding and event parsing.
The registry currently contains `claude-code` and `codex`; unknown kinds fail
explicitly. Availability means the native executable can be resolved, not that
credentials or every model capability have been verified.

One public boundary keeps schedules, research and sessions from each choosing
binaries, constructing credentials or interpreting native protocol events.
Profiles compose the real differences without a hierarchy of backend-specific
SessionManagers or public Harness subclasses. Fork/transcript capabilities derive
from their collaborators; steering, process reuse and tool-policy support are
independent adapter declarations. Unsupported operations fail rather than
silently changing backend or dropping a requested tool restriction.

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
retains native resources and bounded event delivery through confirmed retirement.
Input delivery, committed business outcome, notification and resource exit are
separate facts under the [execution contract](architecture-contract.md#输入完成与资源退休).

`RunDriver` owns protocol state, without owning a process, lease or product
outcome. `ArgvDriver` supplies Codex exec's initial EOF; `ClaudeDriver` encodes
stream-JSON inputs and gives every opening or steering frame a unique UUID.
`AppServerDriver` owns ordinary Codex RPC preparation, exact history, single-root
correlation and protocol evidence, without product settlement authority.
The shared run checks authority immediately before spawn and business writes,
and owns readers, backpressure, process groups and retirement. Moving wire
operations into drivers does not add a second resource owner or settlement path.

`HarnessRun.close_admission()` synchronously closes new input/tool admission and
revokes its run lease and Wiki binding. It leaves the original proof readers and
resources owned. Starting retirement reuses that closure before starting the
original cleanup task. Stop can therefore close admission before storage waits
without losing late native input proof; closure alone is not an exit proof.
The [execution contract](architecture-contract.md#输入完成与资源退休) owns settlement
and cancellation timing.

Claude declares `can_steer=True`, `reusable=True` and `can_continue=True`;
this permits a new continuation input, not replay or exactly-once effects.
`new_driver` selects protocol operations instead of a shared stdin enum. Owner dispatch, API
capabilities and `HarnessRun.steer()` consume the explicit steering declaration;
held-process policy consumes reuse. Tool allow/deny support remains independent.
The ordinary Codex App Server profile declares steering true and reuse/continuation false:
it only steers its captured active root, does not retain
a host between requests or recover by automatically sending a continuation.
Explicit subsequent requests can still resume the same native history.
The independent Research exec leaf declares steering, reuse and continuation
false. It has no ordinary conversation or resume entry. Runtime profiles also
declare `native_input_evidence`: Owner uses that actual driver contract for
pre-recording input facts. Synthetic process peers declare their own capabilities;
backend spelling or a stdin pipe cannot grant native evidence.
For a reusable host, Wiki scopes bind through its original live run lease so
compatible later requests can install their own task. A non-reusable run uses
its original task scope directly, including when its initial input uses stdin.
Both paths preserve current Wiki authorization.

The possible-send boundary follows the actual transport: argv can contain the
business input when spawn is submitted; stream input becomes possibly sent at
its write. `send_input` delegates encoding through the driver. Local write/drain
confirmation does not prove native acceptance or model consumption.
For App Server, spawn and handshake carry no business input. Owner records
initial `not_sent` before starting preparation; the driver commits possible-send
evidence before the business frame. The original submission retains these facts
through Stop and late observations; see
[native input evidence](architecture-contract.md#codex-native-input-evidence).
Native accepted/rejected proof uses the existing cancellation-protected recorder
and database deadline, so cancelling its reader cannot discard a proof already
observed. The recorder reports a committed-change signal to the original Owner;
only Owner advances notification revisions. The serial writer validates an
App Server target after authorization and immediately before its write.

For multiplexed App Server I/O, stdout never waits for display capacity or an
input-proof database commit. `RunDriver.receive` synchronously separates responses,
native requests and notifications; both RPC directions have independent IDs. Existing run-owned
proof tasks preserve observed input acceptance/refusal in order and are joined
before collection closes. One write lock serializes complete control and
business frames. Native question replies use that lock's final eligibility
check, after all prior awaits; see [question ownership](architecture-contract.md#native-question-control).

The native event handoff has a 256-event / 1 MiB serialized payload budget,
allowing one complete oversized event into an empty queue under the native line
limit. Only adjacent compatible item deltas can coalesce. Capacity exhaustion
freezes admission and starts the original retirement task immediately. A failure
slot outside the saturated queue preserves the accepted prefix and prevents a
successful result from hiding output loss. After confirmed retirement, stream
delivery reconciles final helper observations from the same bounded projection;
no extra output or completion ledger is created. Legacy Claude delivery retains
its existing backpressure and held-process idle behavior.

Question/control handoffs have their own finite count and byte limits. A blocked
presentation publisher cannot hide original question admission or delay terminal
closure. Run-owned control writes have a finite deadline and fail the original
stream if they cannot complete; there is no cross-connection replay. Native
collection identity remains after execution admission closes, until the original
reader/proof cleanup ends. Raw pipe drain is not a saved complete-item prefix.

App Server root terminal leaves the product stream open for the same reader's
bounded shutdown prefix. Execution authority closes immediately; original
observation identity is retained through collection and released afterwards.
`observed_root` supplies recording identity without execution eligibility. The
first retirement reason and admission-closure time belong to the original Run;
late events cannot change that cause or renew active time. Collection failure
keeps a reliable error after the accepted prefix. Failed retirement closes
collection without claiming exit and wakes the original Owner for existing
failure takeover. Synthetic helper interruptions require proven retirement.

Passive measurements bypass the necessary output queue and never refresh
activity. The Driver interprets native response/turn facts; Owner attaches product
identity and exposes its existing overall/idle clock read-only; Monitor uses its
bounded disposable sink. HarnessRun records entered retirement phases with their
actual grace and elapsed time, without adding a timer or changing those limits.
Optional observation drain and close share one finite deadline, and their errors
cannot replace process-exit proof with a retirement failure. See the
[Monitor contract](codex-performance-monitor.md) for source and coverage limits.

Drivers normalize structured provider failures as `HarnessEvent.error_details`.
The existing recovery-message path retains that payload and original runtime
source, including after business status selection. A late failure therefore
remains readable without rewriting the selected outcome or adding a result ledger.

The run also owns local input-submission evidence and its captured cleanup task.
`retirement_state` and `wait_retirement()` observe that task without starting
retirement or changing its original process. A live held host remains pending
even after a request driver ends; cancellation of the waiter leaves resource
ownership intact. The [execution observation contract](architecture-contract.md#原执行的观察)
defines the receipt's request/attempt/work scope and consumer lifetime.

Claude uses stream-JSON input and can retain a process between turns. Ordinary
Codex steers only its bound active root and retires its host after that root. Settings
changes invalidate the held-process signature when needed while preserving the
native resume identity. See [steering](inline-steering.md),
[model settings](model-reasoning.md) and [turn safety](turn-safety.md).
Steer configuration compatibility reuses the same assembled spawn signature,
including current connector selection, while retaining the active request's
instructions. The ordinary Claude steering path still passes through the
public run input operation, retaining its original delivery and cancellation
boundary.

Configuration saves and execution share transport/profile selection and driver
rendering. Static validation checks the known payload without launching a CLI;
actual preparation validates dynamic instructions, MCP and environment again.
Each driver validates its own input boundary, so an RPC text frame is not
mistaken for an exec argv value. Model-control validation is also shared.
Explicit effort, speed and tool restrictions must be supported by the actual
backend and model; an empty capability list is not an exemption. Execution uses
the logical request's captured configuration and checks current credential
authorization before spawn. A legal Agent default save does not certify every
older session's inherited combination; repair and timing follow the model
settings contract above.

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
The narrow protocol seam is [driver.py](../../server/harness/driver.py), covered
by `tests/test_harness_driver.py` through a real subprocess with a distinct input
protocol. Existing backend, held-host, helper-retirement and auxiliary-entry tests
remain the behavior checks; the seam does not replace them.
`tests/test_harness_core.py`, `tests/test_harness_claude.py`,
`tests/test_harness_codex.py`, `tests/test_harness_flow_control.py` and
`tests/test_harness_authorization_stop.py` exercise these boundaries.
`tests/test_session_capability_projection.py` checks all four steering/reuse
combinations through real subprocess input, API projection and Wiki tool visibility.

Ordinary App Server start and precise resume are described in
[the Codex contract](codex-backend.md#opt-in-app-server-start-and-precise-resume).
The [runtime roadmap](../plans/engineering-runtime-roadmap.md) retains the separate
post-final recursive capability goal and unfinished DeepSeek scope.
The [original implementation](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-layer.md)
contains old API sketches and rollout evidence; current verification follows
[testing.md](../testing.md).

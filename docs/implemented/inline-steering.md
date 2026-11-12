# 轮内输入、流式交互与宿主复用

> **Status:** implemented — Claude stream-JSON 引导及条件复用；普通 Codex 引导绑定的活动根。

## Current capability and delivery

Steering follows the selected `RuntimeProfile.can_steer`, independently of
`reusable` and tool-policy support. Claude declares steering and reuse; Codex
App Server declares steering without process reuse; the independent Research
exec leaf declares neither.
Session API views use the same transport selection as run creation. Owner
dispatch also checks the active phase and input eligibility. A native stdin
channel grants no capability by itself.

| Evidence | Current guarantee |
| --- | --- |
| Committed input claim | The original input has an execution identity; delivery is not proven. |
| Steer `confirmed` | The complete local frame write/drain returned; provider acceptance or processing is not proven. |
| App Server `accepted` | The original steer response or complete user-message client ID proves native acceptance; model processing is unconfirmed. |
| App Server root terminal | The bound root ended and admission closed; child consumption and process exit are separate facts. |

A failed or cancelled frame write may have sent part or all of its input.
The original input keeps its unknown delivery fact; a successful root does not
upgrade that fact. Claude retains its local delivery evidence; App Server adds
native evidence to the same original submission.

All user input first follows the durable [input contract](input-reliability.md).
SessionOwner owns dispatch eligibility and ordering. A supported steer is sent by
the managed stdin writer, retaining its input/request/execution identity across
awaits. Echoes are correlated by identity, not matching prompt text. A completed
primary request cannot silently prove successful delivery of another input.

Ordinary Stop pauses dispatch and preserves its backlog. New human correction can
run while that backlog remains paused; explicit resume restores queued dispatch.
BG, Research and delegation returns remain separate inputs in successful-admission
order. They are not automatically batched or converted to native steer. Current
boundaries are maintained in the
[execution contract](input-reliability.md#原请求完成与失败交接).

## Bound Codex root and queue qualification

An App Server attempt owns one accepted root. The driver supplies an immutable
`ActiveTurnRef` with connection generation, original attempt, thread and turn.
Owner captures it when taking over a steer; the RPC sends that thread and
`expectedTurnId`, never a native child or a replacement root. Native wait and
its return leave the target eligible while that same root remains active.
Validated root terminal and Stop close eligibility immediately, before resource
retirement or receipt publication.

Only human text without attachments, with compatible automatic Wiki constraints
and configuration, can steer. Explicit read/maintenance inputs, attachments,
preparation, no target and retirement use ordinary queue dispatch. Compatibility
uses the actual run configuration, connector assembly and existing spawn
signature; request instructions retain their separate steering semantics. Free
text is not parsed for model or setting changes, and steering cannot replace
spawn-time configuration or extend authorization.

The pending budget is eight inputs including the current write. Overflow enters
the ordinary queue, and a later human input cannot overtake older queued human
work. The existing paused-backlog exception still permits a new human request
and corrections to that request; it does not resume the old backlog.

The writer runs from input admission, independently of stdout and subscriber
progress. Complete business and control frames share a write lock. After the
last authorization await, target/lease validation and the business write have no
intervening await; terminal/Stop closure uses the same event-loop ordering.
Before writing, the original submission commits its possible-send marker.
The native protocol's explicit rejection, or proof that no send marker exists,
permits Owner to return the original input and order to ordinary dispatch.
Generic internal errors and missing acknowledgments remain unknown. They cannot
trigger replay, a different target or automatic resumption after Stop.

The current steer owns its message and persisted sequence. Its completed
preparation task retains the original immutable Git anchor across the write;
delayed acceptance or uncertain delivery never recaptures the working tree.
There is no second input-record snapshot on the queued request. Requeued inputs
are reconstructed from their original durable identity.

Observed acceptance/rejection commits survive reader cancellation through the
existing cleanup protection and database deadline. Resource cancellation starts
independently; a late proof updates only its original input. Owner coalesces
committed evidence notifications into its existing dispatch revision so Saved
inputs refreshes during an active root, including an unknown-to-accepted change.
Accepted steer keeps its input and evidence without a success notice, during
the active root or after completion. Separate execution uncertainty, including
an earlier RPC timeout awaiting root completion, remains reviewable without a
restore-and-run action. Requeued input that later runs normally does not retain
a stale steer notice. Full identity, evidence and
recovery rules belong to the
[input contract](input-reliability.md#codex-native-input-evidence).

## Live transcript ordering

On Claude, the input row commits before its asynchronous stdin write, while
`steered` is published only after that write finishes. Output persisted meanwhile
can therefore be broadcast first. The browser accepts an unseen lower seq and
merges durable rows by seq; seeing N+1 no longer discards N. Snapshot coverage
and current-window duplicate detection are separate facts. Full merge, streaming
identity, settled-question and older-page rules belong to the
[frontend contract](frontend-list-reconciliation.md#实时历史身份与流缓冲).

The original writer and stdout reader remain independent. The fix neither
publishes `steered` before write confirmation nor pauses stdout to force display
order. A visible row proves neither provider receipt nor semantic consumption;
an unknown delivery is not permission to repeat input. Controlled real Claude
JSONL/driver tests hold stdin with a 1 MiB correction: N is durable and N+1
published while N is not yet `steered`; releasing the pipe confirms N, while a
lost drain acknowledgement retains the original unavailable result without a
`steered` claim. Actual snapshot/broadcast content and identity are compared with
the shared `steeringReplay.json` fixture, then consumed by the production browser
reducer. Claude and Codex frontend interleavings are covered offline; this is not
a claim of a reproduced native Codex stdin failure or a new live-provider result.

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
not prove its native tail is complete. Codex collects a finite original prefix
through retirement; unfinished helpers can remain partial after root success.
The [native output contract](native-subagents.md) owns that observation boundary.

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
`tests/test_session_capability_projection.py`. App Server regressions in
`tests/test_codex_app_server_steering.py` cover active wait, overflow and order,
configuration/connector changes, rejection, write/Stop/terminal races, lost and
late proof, and quiet stdout under subscriber pressure. Saved-input frontend
tests cover the normal WebSocket dispatch revision clearing pending or unknown
acceptance notices while the root stays active; separate unknown execution
still renders its recovery notice.
The delayed-ACK cases also verify that both timely and late acceptance preserve
the Git anchor captured before the write.

The [original experiments and rationale](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/inline-steering.md)
retain their tested CLI version and conditions. Current test policy is in
[testing.md](../testing.md).

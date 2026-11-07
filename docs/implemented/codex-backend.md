# Codex：普通交互与无工具文本调用

> **Status:** implemented — 普通 App Server 回合、有界恢复、独立 Research exec leaf 与订阅文本 worker。

| 调用路径 | 当前责任 |
| --- | --- |
| 普通 App Server | 会话交互、原生输入接纳、活动根引导、问题、原生输出与有界退休 |
| 独立 exec leaf | 有界 Research web 查询，不接受普通对话或历史 resume，不作隐式 fallback |
| 无工具 text worker | 调度解析等文本调用，单独拥有认证／配置与资源，不运行模型工具循环 |

## Interactive execution

The ordinary Codex profile uses the configured native App Server through the common
Harness. Each request attempt owns one stdio host and one accepted root turn;
HarnessRun owns its process group and retirement. A logical request can span
bounded attempts after a recoverable failure. Human steering targets that
active root. Later ordinary requests resume the captured native thread after
the preceding writer retires. Native questions, scoped root/child output,
completion consumers, native fork and passive accounting use the contracts below.

The profile renders selected MCP entries and a composed controller through
developer instructions. Resumed threads receive controller updates and separate
request-specific trusted instructions; no saved agent configuration is rewritten
to accomplish this. Model, effort and speed follow [their own contract](model-reasoning.md).
Nonempty arbitrary tool allow/deny policies unsupported by Codex are rejected;
headless bypass is not proof that such a policy was enforced.

Saved custom subagent definitions remain intact across edits and backend changes.
Claude renders those definitions into its native configuration. Codex currently
uses its built-in helpers; saving a definition does not map its custom role,
model or tool restrictions into Codex. The agent form states this boundary.
The remaining mapping is owned by the
[native-role plan](../plans/codex-native-roles.md).

Native stdout events supply product output; bounded verified rollout observations
supply passive measurements. Native helper cards describe observation, not proof that
the parent consumed every child result. Usage timing has the narrower
[Monitor contract](codex-performance-monitor.md).

The selected native login remains the authentication authority. A bound
credential selects its runtime home; an unbound invocation follows the existing
host-default path. In-app device login is published by the original manager
task through its acknowledged file/metadata commit; status only observes it.
Reauthorization and deletion retain original target/cleanup ownership as defined
in the [credential contract](harness-credential-reauth.md#codex-device-operation-ownership).
This differs from the
original plan's unresolved login alternatives. Public transcript export/import
is separate from [rewind](session-rewind.md) and [native-copy fork](session-fork.md).

<a id="opt-in-app-server-start-and-precise-resume"></a>
## App Server start and precise resume

Ordinary requests select App Server before preparation. The former
`OCTOPUS_CODEX_TRANSPORT` selector and ordinary exec path are removed.
`OCTOPUS_CODEX_APP_SERVER_BINARY` selects the binary
(default `codex`, resolved on PATH). There is no binary digest allowlist, exact
release requirement or version gate. Each connection checks initialization,
effective configuration and required thread/RPC behavior before business input.
Compatible upgrades and alternate builds use the same path; unsupported required
methods, configuration or response shapes fail through the existing preparation
or protocol boundary, preserving input evidence. The [official App Server
protocol](https://learn.chatgpt.com/docs/app-server) defines the integration surface.
The 0.166.0 G0 observations remain a historical validation baseline, not a runtime
requirement. This default does not rewrite saved agents, credentials, model
settings or backend identities.
There is no second transport execution or automatic fallback after a failure.

Each attempt has its own managed stdio host. Initialization, effective config,
thread preparation and controller checks precede `turn/start`. Process and thread
overrides disable message wake, goals and sleep execution while keeping the
required active collaboration features enabled. Effective mode, requested model
and permissions are checked before business input. Exact `thread/resume` must
return the captured native identity; missing or incompatible history fails
without creating a replacement. Controller checkpoint comparison and trusted
developer-item injection retain independent uncertainty evidence. Prompt text and
large inputs travel in the business RPC; attachment ownership and original
recovery payloads stay with the existing submission.

Only the start response or a matching native user-message client ID can bind the
accepted root. Notifications preceding proof are buffered within finite bounds.
Conflicting/missing identities, unsupported complete items and unexpected root
turns fail visibly, preserve the valid accepted prefix and retire the original
host. One bound root may contain multiple model samples and tools. Its terminal
starts retirement immediately; that attempt cannot start another root. An
eligible failure can let the original Owner prepare a later attempt after
retirement, under the [recovery contract](#error-source-and-recovery).
SessionOwner remains the product settlement authority. Thread
association commits with input evidence, so losing a `session_started` observation
cannot make the next writer open a new history. The next writer reads the
committed identity after its predecessor retires.

Preparation, possible-send and acceptance evidence live on the original
submission with separate input/attempt/RPC/thread/turn identities. Their timing,
late proof and manual recovery rules belong to
[the input contract](architecture-contract.md#codex-native-input-evidence);
[current storage](current-storage-format.md#publication-boundary) owns the format
and finite publication handoff. Acceptance does not prove model consumption.

This path supports human steering of its bound active root and evidence-qualified
continuation. Hosts are not reused. Ineligible later input queues for
an ordinary request. Native user questions use the existing product question
owner; unsupported approval/dynamic requests receive explicit method-specific
negative responses. Root and child output retain their original identities, and
known helper state is reconciled through finite retirement. Bound-root completion
and selected final replies, saved details, Stop/recovery, effect consumers and
history branching use the contracts below. [Monitor](codex-performance-monitor.md)
separates model responses, provider snapshots and resource cleanup.

Research web leaves explicitly select the independent `CODEX_WEB_LEAF` exec
profile: read-only, web search enabled, no Octopus MCP, history resume, steering
or fork. Read-only does not imply a web-only tool whitelist; arbitrary tool
allow/deny policies remain unsupported. Its argv builder rejects ordinary
conversation use. One-shots keep the tool-free text worker. Neither is a
fallback for a failed or unknown ordinary request.

### Native MCP configuration

Ordinary App Server inherits the selected credential home's and trusted project's
native MCP configuration. `_mcp_config_args` supplies per-invocation fields for
the selected Octopus namespaces without rewriting either file. Native extra
namespaces may remain discoverable. Same-namespace tables merge recursively;
an inline namespace table or `mcp_servers={}` is not a reliable replacement.

Before thread start/resume and business input, `config/read(includeLayers=true)`
checks active native layers against the actual rendered invocation paths.
Residual transport, identity or tool-set fields such as another transport's
command/URL, native headers/environment, disabling or filtering selected tools
produce an actionable preparation failure: rename or remove the conflicting
native entry, then retry. Native lifecycle policy, including startup/tool
timeouts, remains native. Default-equivalent enablement, local environment and
empty deny/environment-forwarding lists are allowed, using effective native
values where available. Disabled layers are ignored. A native config error
identifying a selected namespace receives the same conflict diagnosis.
The guard is a conflict check, not a native configuration parser or a general
tool allowlist; current tool endpoints still own authorization.

Research's empty MCP table means no Octopus injection; native extra MCP can still
be visible in the selected CLI. The leaf remains read-only with no ordinary history
resume/fork. Claude strict config and the tool-free text API worker have their
own [mode boundaries](harness-layer.md#instruction-assembly).
The [0.166.0 observations](../reference/runtime-resource-boundaries-20261103.md#mcp-configuration-and-discovery)
cover thread-scoped discovery, residual authentication fields and two identities
using the same native home without config writes.

### Controller history cost

`_latest_checkpoint` scans the active history segment line by line in
`asyncio.to_thread`; it does not synchronously read the whole file on the event
loop. Compacted replacement history can still contain a large JSON line.
Controller, rollback and thread checks remain authoritative on every resume;
there is no persistent sidecar or mtime/size cache.

The [bounded experiment](../reference/runtime-resource-boundaries-20261103.md#controller-scan-and-native-preparation)
found no scan bottleneck requiring a cache in its ordinary 1–256 MiB samples.
Native preparation reached the before-business boundary for 1–50 MiB; 256 MiB
native resume exceeded the experiment's 512 MiB RSS stop and is not validated at
that scale. The 20-second RPC timeout is per RPC, not an overall preparation SLA.
These results do not establish production recovery latency or arbitrary large
compacted-line safety.

### Native compaction

Idle manual compaction precisely resumes the captured thread in the ordinary
Run, then sends `thread/compact/start` once. Preparation checks model, permissions,
cwd and controls, without injecting controller/turn instructions, sending user
text or falling back to exec/new history. Request/attempt/connection/thread/RPC
facts precede possible-send and business write. Run proof that stdin.write was
never reached can refine the original marker to rejected; a failed drain cannot.

Empty ACK records acceptance without inventing a turn. After possible-send,
the selected thread's first `turn/started` binds the compact root; conflicting
roots fail. Committed root proof releases startup independently of ACK, preserving
slow operations under original turn deadlines. Late RPC errors cannot erase
execution. Ordinary inputs retain their response/user-message proof rules.

Completed root contextCompaction items prove effects. Existing observations
atomically install a reliable earlier recompute with its boundary; automatic
items retain their completion-obligation policy. Effect survives failed or
interrupted outcomes and finite Stop collection. Success without effect evidence
is unconfirmed, never inferred no-op. Product chat keeps its messages plus a system
maintenance outcome, with no final answer or copied summary. Version evidence is
in [the G0 record](../reference/context-meter-compact-g0.md); product regressions
use `tests/test_codex_compact.py`.

### Root completion and selected answer

Only the accepted root's real terminal supplies a provider candidate. A final
text item, model sample, native wait or child terminal cannot finish the request.
On validated root terminal the original Run freezes admission and starts bounded
retirement before storage or presentation waits. The same stdout parser retains
original observation identity for finite late items, questions and input proof;
it never reopens execution or routes collection through Claude's live idle scope.
EOF or the collection cutoff ends that responsibility. Raw drain only releases
descriptors. Missing complete root items, malformed necessary records or overflow
normally prevent success; a cancelled helper need not have produced an answer.
The only pure-input exception requires the original Owner's exact, consumed MCP
cancellation/cleanup receipt on the same attempt. The metadata's `codex_version`
is informational; authenticated scope and exact request/attempt/thread/turn/call
identity determine correlation. That exception cannot excuse reader cutoff,
another associated effect gap, a missing identity or an already selected failure.
The original required-items diagnostic retains its origin and full observed gap
count; raw integrity stays incomplete and no completed item is fabricated. See
[original-call retirement](architecture-contract.md#mcp-question-retirement).

Completion obligations follow native producer provenance and item type.
Uncommitted sampling Message/Reasoning and proven Core WebSearch observations
may be abandoned with or without a retry notice; deltas alone create no completion debt. Context
compaction is ordinary maintenance. WebSearch with absent/unknown action and
ambiguous ImageGeneration retain conservative actual-result obligations;
extension invocations are not excused by sharing a wire kind with sampling.
Command, file-change, MCP and dynamic-tool results remain necessary. A cancelled
collaboration wait may lack completion without proving every child finished.
The [0.166.0 observation and source evidence](../reference/codex-app-server-g0.md#上游流重试遗留的未完成消息)
explain why abandoned sampling does not make an otherwise complete root fail.

The driver validates identity and supported payload before accounting or proof
effects, including correlated userMessage acceptance when an ACK is missing.
Complete snapshots without a start are saved; normalized duplicates are
idempotent, while kind/body/result conflicts preserve the original and fail
collection. Observation timestamps and optional defaults do not create conflicts.
Late old starts/deltas cannot regress a completed item. After a trusted terminal,
the finite window accepts original complete facts and known closure/proof, while
new root starts and new questions/business RPC remain closed.

If the accepted root has an unfinished `commandExecution`, retirement sends
`thread/backgroundTerminals/clean` on its original connection before stdin EOF.
The same reader waits for the original thread/turn/item's complete event within
the existing 0.5-second protocol-interrupt grace. The cleanup ACK only confirms
submission; it cannot replace command output, exit code or complete-item evidence.
The 0.166.0 observation established why this order matters: EOF detaches the
notification subscriber before native shutdown terminates residual commands,
so their completion can reach the rollout without reaching the client. No residual root command means no
cleanup RPC. Stop uses the same retirement path; timeout or cleanup failure keeps
the existing incomplete-recording failure and process-group retirement.
A collected failed command remains an error tool result before the final result;
it does not override the root's independently selected business outcome.
The [locked-binary observation](../reference/codex-app-server-g0.md#residual-root-command-retirement)
and `test_codex_app_server_completion.py` cover this boundary.

The original Owner chooses one business outcome. Normal success requires its
necessary prefix and actual resource retirement. Stop and start-wait failure may
select their outcome first, then record it after finite collection. Final refs
and terminal sequence are allocated at that recording point, so late root finals
remain before the terminal without changing the selected outcome. Native provider
status is retained separately. Retirement failure wakes the original Owner and
enters its existing Manager takeover/fatal boundary, including behind slow
notification delivery; it cannot leave a normal completion pending forever.

Active time ends at first admission closure; cleanup uses the existing grace
periods. A late terminal cannot erase earlier overall/idle expiry while storage
waits. Real root/child progress can refresh idle, duplicate turn notifications
cannot, and late collection cannot renew it. The original slot and identity span
retirement. Remaining helpers become interrupted only after proven retirement,
with the original cause; completed helpers retain completion. Root success does
not claim that every descendant result was understood.

Provider terminal and reliable collection failure have separate bounded Run
slots. A damaged frame or overflowing handoff cannot replace typed provider
status/code/details or discard the accepted safe prefix. Native interrupted
remains interrupted with independent recording gaps unless an earlier local
outcome already won, or collection failure initiated cleanup before that terminal.
Completed with a true required-record gap is failed, keeps `native_status=completed`
and any saved answer, and grants no automatic replay. Chat displays the native
status and distinguishes saved-but-incomplete content from a known missing final.
The public Harness stream ends with its single provider result after internal
retirement/summary observations and collection diagnostics. Wire terminal parsing
still closes admission immediately; stream presentation order cannot extend it.

`TurnOutcome.final_answer` references ordered original root final-message rows.
Commentary, child text and tool text remain in the transcript and never become
reply fragments. Complete empty items are retained; a legal empty selection is
distinct from invalid/missing refs. SessionResult, LiveReply, App ask/SSE/status
and named delegation share that selection. Legacy records retain their prior
interpretation. See the [result contract](architecture-contract.md#原请求结果读取)
for formatting, input uncertainty and failure semantics.

A terminal summary only adds positive constraints for explicitly completed
`final_answer` items (positive completion timestamp). After the original ordered
message pipeline finishes saving, the Owner checks exact request/attempt,
thread/turn/item, phase, text digest, saved rows and final refs. Missing or
conflicting declared finals fail recording; summaries never backfill body text.
Absent/notLoaded summaries impose no empty-list constraint, and a legal empty
final or the existing ordered final selection stays valid. Duplicate terminals
compare status and declared error facts, ignore observation times and may add
compatible summary constraints.

Stop before the Owner's unique outcome selection chooses interrupted, even
after a native success candidate. Stop after selection preserves the chosen
outcome across unconfirmed recording, committed-but-unnotified and delivered
receipt windows; it still pauses dispatch and closes remaining admission.
It preserves partial output and external effects. Independent BG, Research and
named work keep their own owners and queue in acceptance order. A manual
correction can run while old backlog stays paused; explicit Resume releases it.
Service reentry retains original input and steer evidence without replay,
including unknown and accepted-without-consumption proof despite root success.

Native card [saved details](native-subagents.md#saved-app-server-details) read
only original persisted items. They do not keep this host alive or extend its
budgets, and cannot reopen native execution after Stop or root terminal.

### Active-root human input

Owner captures the original attempt and the driver's connection/thread/turn
target. A compatible human input uses `turn/steer` with `expectedTurnId` and its
original product input ID. A native wait can accept it; wait returning does not
close the root. Validated root terminal and Stop immediately close the target,
even while its original host is retiring.

The original submission retains possible-send, acceptance and rejection facts.
A response or correlated complete user message can prove acceptance; neither
proves the model consumed the instruction. Missing proof remains unknown after
a successful root and is never automatically replayed. A known unaccepted input
returns to the ordinary queue with its original identity and order. Configuration
intent, attachments, Wiki eligibility, the eight-input pending budget, paused
backlog exceptions and live status refresh are owned by
[the steering contract](inline-steering.md#bound-codex-root-and-queue-qualification).

### Native questions and output

The supported native question path accepts `request_user_input` only on the root;
child approvals can still arrive, including during shutdown. Native question forms
and structured answers pass through OwnerQuestions and the existing product
answer UI. The response uses the original native question IDs on the original
connection. Root terminal, Stop, serverRequest/resolved and connection loss
revoke reply eligibility immediately, independently of storage/publication
latency. The final write rechecks that binding; a saved answer is not proof
that its native response was sent. MCP ask keeps its existing entry and owner,
and also closes with its originating native run. Full timing and provenance
belong to [the question contract](architecture-contract.md#native-question-control).

`agentMessage.questions` supplies a separate asynchronous reply cue. Live and
loaded messages show a prominent question card while preserving the original
body and options. Users answer through the ordinary message composer; the card
explains that the agent may continue working. This presentation does not create
a pending native request or change execution waiting and question settlement.

Headless permission policy remains unchanged. Unexpected command/file approvals
are declined, permission requests receive no permissions, and legacy approval
methods use their own negative response shape. An unregistered dynamic tool
returns `success:false` with explanatory `contentItems`; an unknown request
returns MethodNotFound and fails the original stream. It cannot hang forever or
silently approve. `isBlocking` supplies native waiting evidence. Combined with
the original work's human policy, true disables the question deadline and pauses
idle; false gives a known human question a 120-second creation deadline. The UI
shows the final 60 seconds; active interaction can ask the original Owner to
snooze, clearing that effective deadline and pausing idle after adjudication.
Unknown legacy policy and MCP deadlines retain their own rules. The overall cap,
Stop and original-call closure remain effective. The G0 Default-mode native-question capability emits false in the
live smoke; it proves original-connection optional delivery, while explicit true
frames exercise blocking/idle suspension in the controlled native protocol tests.

Native item conversion is a pure boundary in `codex_items.py`; it does not rename
fields into the legacy exec parser or own execution. Complete items retain their
native fields and source. MCP `failed` remains failure even with a nonempty result
and null error; command status and exit evidence, declined file changes and
interrupted collaboration remain distinct. Complete empty text/reasoning is valid.
The original submission owns user input, so native confirmation does not add a
second user message. Final text and its phase remain separate from tool results.

`imageView` records the action and path without a snapshot. `hookPrompt` retains
fragments and hook-run provenance in a folded tool record. An unregistered dynamic
call is refused, while its native started/completed items retain status, success
and content. These observations do not independently fail a successful root.
Plan, host function-call output, sleep and native Review mode remain unsupported;
unexpected complete items fail explicitly. There is no external token hosting,
attestation or external-clock service.

Typed request/attempt/native provenance survives complete messages, tools,
deltas, WS, history and snapshot cards. Root resume identity remains unchanged
by child work. Main chat projects root output; child cards show collected text
and actual known work status. A mailbox interaction or completed start tool is
not a completed child turn. Pending child work binds in place so an already
captured wait still refers to its original work after the actual turn is known;
it cannot settle a later turn of that child. Root success can coexist with residual helper
interruption after proven retirement. The
[native output contract](native-subagents.md) owns work identities, display
windows, bounded caches and late-item semantics.

### Native generated images

Generated image records distinguish in-progress, completed and failed, retaining
failure, revised prompt, generation identity, input-image metadata and native
references. `savedPath` is the original absolute path, not a copied asset.
Completed without a path explicitly means the local file is unavailable. Chat
offers path copy and the existing viewer; its working-directory and size policy
still applies. Other files can be opened locally using the path. Later changes,
deletion and viewer refusal never turn the record into a historical snapshot.

The native image decoder's budget is 32 MiB. Its stdout reader allows the
corresponding base64 length plus 4 MiB of metadata. At the wire
boundary, a native file ID takes precedence over inline result data. Otherwise
inline bytes are validated and reduced to byte count and SHA-256 before correlation
buffers, raw events, persistence and WS. Projected non-media frames still have a
4 MiB budget; correlation/event queue limits do not grow. A real overrun fails
explicitly and preserves the valid prefix. There is no media root, automatic
copy, remote download or regeneration path. Persistent shape and portability
remain with [current storage](current-storage-format.md).

### Effective configuration and request isolation

Saved configuration validation and execution select the same transport, profile
and driver. App Server text is validated as JSONL rather than placed under exec's
argv limit; the shared argv/environment checks still apply to the actual launch.
The logical request captures product defaults at dispatch preparation. Its retry
retains that snapshot, while a later request captures newly saved defaults;
[model settings](model-reasoning.md#saving-defaults-and-repairing-an-existing-session)
owns the capture and inherited-session repair rules.

Clearing a session's model or effort override first restores Agent inheritance.
When neither the session nor its Agent supplies that field, it uses the current
native invocation's default rather than the previous history's setting.
The driver resolves native defaults through
`config/read` and, when no model is configured, the native catalog default.
It explicitly supplies the resolved model to prevent resume metadata from
restoring the old model and effort. A null prepared effort can represent the
model's native default; it is distinct from an explicitly requested effort.
After selection and capability checks, the driver captures resolved model,
effort (including null) and tier before the thread RPC. A preparation refusal
therefore cannot make recovery reread different native defaults. Each later
attempt inherits that selection; a new logical request resolves defaults anew.
Before sending input, the driver checks the prepared model, explicit effort, cwd,
approval policy, sandbox and service tier against the request.

Every `turn/start` explicitly supplies Default collaboration mode and the selected
model/effort. A resumed thread must already report Default; Plan or unknown mode
is refused before business input, without converting it or creating replacement
history. The mode setting does not replace the composed developer controller.
Enabled current-time reminders with `clock_source=external` also fail preparation,
because `currentTime/read` is not implemented. A disabled reminder does not enable
that requirement. Ordinary scheduling keeps its existing creation, queue and
execution path and is subject to the same effective-config checks.

Fast requires the native Fast feature and the selected model's advertised
`priority` tier, including paginated catalogs. It fails preparation when that
capability or the effective tier is missing. Normal explicitly resets the
thread to `default`; the 0.166.0 native provider observation omitted that tier field.
Process and thread overrides keep wake, goals and sleep disabled even when
the shared native home enables them. They also set
`features.unbounded_connection_retries=false`; `config/read` must confirm the
effective false value before input. The 0.166.0 observations recorded native
finite retry/backoff and WebSocket-to-HTTP fallback. Stream/request counts have
different scopes and are not a whole-turn call budget or wall-clock bound.
Every invocation checks the key and effective value; no CLI fork or
new recovery stage is maintained. Current controller checkpoints are checked
after exact resume, including compacted history, and changed trusted instructions
are acknowledged before business input. An uncertain injection blocks sending.

Each outbound frame is encoded and checked before a possible-send or controller
mutation is recorded. Its UTF-8 budget is 4 MiB minus 4096 bytes, reserving native
echo metadata below the ordinary 4 MiB non-media frame budget. Static saves check known
text; actual preparation and writes check the composed payload and complete
envelope. Oversized input fails explicitly and retains the original draft.
Unbound native observations have a separate 8 MiB UTF-8 / 256-frame correlation
budget; non-BMP text is not counted as expanded ASCII escapes. No path truncates
the input to fit either budget.

Sessions may share an owned credential home. Request-specific MCP configuration
travels through the invocation's argv/environment and RPC, without rewriting
that home's config. Namespace URLs may be shared; signed scope, original run
lease, Wiki task and connector installation determine the actual callback's
identity and authority. Current permissions are checked at the tool effect,
so a captured configuration or resumed history does not extend a grant.

Attachment and canonical memory ownership stay with their existing owners;
native skills remain native files. App Server supplies request text inline and
does not create a shared request-config file. An active late-created child can
read the original materials and use its original live scoped tools. Retained
materials do not keep authorization alive after Stop or root terminal. The
[execution contract](architecture-contract.md#输入完成与资源退休) separates closing
admission, late input proof, outcome recording and confirmed resource retirement.

### Native history branching

Dialogue and the management driver each compose one `RpcConnection`; management
does not inherit the conversation Driver or its private turn state. The connection
owns request IDs, frame encoding/budget, response waiters, exact RPC error data
and close association within its original generation. It owns no authorization,
business write policy, input proof, controller uncertainty or fork checkpoint.
Both callers reuse native initialization and configuration validation.

Releasing a timed-out waiter does not remove root/steer evidence. The dialogue
driver still receives and persists late ACKs on the original connection before
settling their proof; a later connection cannot supply that proof. During qualified
cleanup, only the exact missing-thread response together with an absent saved
original path confirms that the artifact is already absent. If that path still
exists, or the RPC reports permission failure or another error, recovery material
is retained. Raw RPC errors remain distinguishable for this check.

With App Server selected, full `/fork` uses `thread/fork` in the selected
`CODEX_HOME`, for both exec-origin and App Server histories. Paginated native
storage cannot be cloned by changing a rollout ID or relocating its home.
The short management driver reuses initialization, the selected executable and
HarnessRun retirement. It sends no `turn/start`, disables wake/goals/sleep and
sets `deferGoalContinuation`; unexpected execution is a protocol failure.
The original SessionOwner owns that host through actual retirement. The existing
fork saga checkpoints possible creation before the write and saves the returned
thread ID/path before further validation or installation. Its preallocated hint
does not identify an API-created artifact.

Unknown creation, including an RPC error after possible creation, retains the
initializing row and copied directory. Confirmed artifacts retain their strategy,
home and identity for recovery even when current transport or agent defaults
change. Cleanup checks the original ID/path/cwd/home, then uses `thread/delete`
only for this unexecuted fork. Its saved `cleanup_allowed` qualification starts
closed: ACK proves identity, while trusted, confirmed management-host retirement
proves the non-executing boundary. Each new management operation closes that
gate before starting. Unexpected execution or protocol corruption retains the
actual identity, initializing row and cwd; immediate and cold recovery both
refuse automatic deletion without the qualification. Upstream reference refusals
remain failures; cleanup
does not expand to the source or related histories. Ordinary product archive,
delete, reset and handoff keep their existing scope. Claude retains its file-copy
strategy. Saved pre-native Codex initializing forks without a `native_fork` key
retain their old file-cleanup strategy; new Codex forks use the native API.
A present but malformed checkpoint preserves the row and files for recovery,
instead of falling back to legacy deletion. See [the fork saga](session-fork.md).

`/rewind` retains product-message seq as its boundary: a steer may split one
native turn. History replay preserves native root/child, phase, partial/cause
and original seq, without executing during branch creation or replaying old
inputs as instructions. Known native patch paths, including rename endpoints
and complete-only items, feed the existing file-revert preflight through the
Harness decoder. Tool/model text cannot fabricate that file-effect provenance.
External effects and unknown input facts remain; see [rewind](session-rewind.md).

### Error source and recovery

Only a failed bound root's typed `codexErrorInfo` can establish provider
authentication rejection: `unauthorized`, or HTTP 401 on the known provider
connection/response variants. Arbitrary stderr, RPC rejection, connector/tool
text and child errors cannot mark the selected credential invalid. The original
reauth flow keeps its binding and login entry available.
Permanent invalidation additionally requires actual adopted-material evidence.
The current mutable Codex home cannot establish that fact, so typed unauthorized
produces `auth_failed` and preserves the current credential under the
[conditional rejection contract](harness-credential-reauth.md#4-run-loop-wiring-serversessionsturnspy).

Native error notifications retain original request/attempt/thread/turn and
connection identity, including before ACK after correlation. `willRetry` and
child diagnostics never become root failure evidence; the root terminal selects
its own status and provider code. A `willRetry:true` notification on the original
connection, bound to its active root or associated running child, refreshes idle
activity without new text or input. Repeated identical reconnect wording can be
a new notification. Replay of a saved diagnostic, unrelated/ended child errors,
nonretry errors, passive usage and post-closure collection cannot renew it.
One notice does not suspend the watchdog; Stop, root terminal and the logical
deadline retain their original authority.

Failed-root classification reads that root's message, additional details and
typed provider code. Preparation RPC refusal retains its own error facts; even
typed 401 there is scoped `permission_denied`, not proof that the main login is
invalid. Typed root authentication and explicit personal-quota facts veto retry.
Capacity, known 5xx/connection/timeout variants and the legacy reliability phrases
qualify; a bare 429/rate-limit message does not. Tool, child and unbound stderr
text cannot supply root recovery or credential-rejection evidence.

App Server supports bounded outer continuation only with this attempt's accepted
root, matching attempt/thread captured by the logical turn, proven original-run
retirement and no protocol issues. Success never reopens. A pre-submission
transient refusal can prepare its unchanged pending input again; possible-send
without acceptance proof remains unknown. Generic start RPC failure remains
unknown; an explicit InvalidRequest/InvalidParams rejection can prove
nonacceptance. Neither old session identity alone nor missing output authorizes
prompt replay, a new history or a fallback transport. The Owner retains budgets,
backoff, deadline, authorization and the unique final result under
[transient recovery](harness-transient-retry.md#4-recovery-evidence-in-the-original-owner).
The other shared policies remain in
[credential reauth](harness-credential-reauth.md) and
[turn safety](turn-safety.md).

<a id="single-call-text-contract"></a>
## Single-call text contract

`run_oneshot` performs one non-interactive text task without executable model
tools. The Codex profile now launches `codex_text.py` with isolated Python
(`-I`) under the common Harness, instead of starting `codex exec`. Schedule
parsing, `/showme` reference resolution and research decomposition/synthesis
keep their public interface; ordinary Codex turns still use the native CLI.

The worker sends a subscription Responses request with `tools=[]`,
`tool_choice="none"` and no dispatcher for tool execution. It does not load
project instructions, skills, MCP, apps or plugins into the model request.
Only a successful `response.completed` can publish text; incomplete, failed,
tool-operation and truncated streams fail without publishing partial text.
There is no fallback to the old executable-agent path or API-key billing.

Native Codex remains the authentication authority. The installed 0.164.0
app-server's `getAuthStatus(includeToken=true, refreshToken=false)` returns
the selected ChatGPT bearer in memory, using native file/auto/keyring storage
selection and login policy. The worker neither reads `auth.json` itself nor
writes a token cache. This preserves the unbound host-default login as well as
an explicitly bound application login. Account routing comes from the native
token's account claim. On HTTP401, it rereads native current auth and requests
native refresh only if the rejected login is still current, then retries the
model request once. Other failures do not start a retry loop.

Explicit model/effort values win; absent values use native `config/read` for
the working directory, then `model/list` defaults if needed. Octopus normal
speed remains `default`; `fast` maps to Responses `priority`. HTTP proxy
environment variables remain inherited. Native control starts no model thread.
Both it and the HTTP request belong to the common one-shot process group:
spawn authorization precedes execution; cancellation/timeout must confirm group
retirement, including native descendants, before reporting a clean stop.

The reason for this separate worker is executable capability control: disabling
public CLI features did not establish a complete tool-free native-agent request.
The text worker has no executable-tool dispatcher and publishes only a successful
text terminal. The original probe record retains the failed and corrected trials.

The native auth operation and subscription wire were checked against Codex
0.164.0; they are not a promise about arbitrary future CLI releases. Protocol
incompatibility fails visibly and cannot restore the executable-agent path.
No OS keyring was provisioned for the original live validation; synthetic
keystore cases cover bound/unbound behavior without claiming a live keyring test.

## Source and verification

Code: [profile](../../server/harness/codex.py),
[native control](../../server/harness/codex_control.py),
[App Server driver](../../server/harness/codex_app_server.py),
[connection coordinator](../../server/harness/codex_rpc.py),
[history management](../../server/harness/codex_history.py),
[text worker](../../server/harness/codex_text.py).
Regression entry points: `tests/test_harness_codex.py`,
`tests/test_codex_controller.py`, `tests/test_codex_text.py`,
`tests/test_codex_text_process.py` and `tests/test_backend_codex_real.py`.
The focused worker scenarios include admission denial, HTTP/stream failures,
missing native binary, hostile startup configuration and confirmed descendant
retirement. Current installation checks are selected through [testing.md](../testing.md).

App Server public submission/Owner/SQLite regressions are in
`tests/test_codex_app_server_submission.py`, with protocol correlation in
`tests/test_codex_app_server.py` and durable uncertainty in
`tests/test_native_input_evidence.py`. They cover preparation failure/early Stop,
write-before-record refusal, lost ACK, late proof, malformed buffered output,
history loss and original-receipt deduplication. Live Codex cases cover
exec-to-App Server and App Server-to-App Server precise resume with real
attachment/tool effects. `web/e2e/codex-app-server.spec.ts` verifies original
receipts and subsequent native resume after reopening the same session across
reloads. These checks do not establish production deployment.
`tests/test_codex_app_server_fork.py` also distinguishes exact missing history
from denied cleanup during cold recovery; neither case deletes the source or
guesses an API-created identity.

`tests/test_codex_app_server_configuration.py` covers transport-aware saves,
current defaults, Fast rejection, compatible upgrades across start/resume, required
RPC incompatibility and UTF-8 refusal without sending input. MCP cleanup tests
retain exact identity/scope refusals across changed or absent version metadata.
Unavailable executable tests retain unsent input and explicit recovery for
missing PATH entries, missing absolute paths and nonexecutable files.
`tests/test_codex_app_server_authorization.py` checks real HTTP tool denial while
outcome storage is blocked or the original process is still retiring. Protocol
correlation also covers a complete 3,000,000-byte non-BMP input before ACK.
Versioned native probes with a controlled provider verify effective configuration,
720,039-byte Chinese input, actual scoped MCP/connector callbacks, late child
material reads and active owner revocation. Those probes establish native
assembly and effects at the controlled boundary, not remote model capability.

On 2026-11-07, installed Codex 0.167.0 passed this installation's Codex live and
browser selections using the existing login. The versioned 0.166.0 fixtures
remain protocol evidence; they do not restrict the executable used at runtime.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/codex-backend.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.

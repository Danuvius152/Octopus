# Harness：当前运行时边界

> **Status:** implemented — 统一公共 Harness、运行时 profile、执行资源所有权与显式能力。

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

Login drivers also identify their original unpublished credential or unfinished
deletion cleanup, and own deletion through binding removal and local cleanup.
Configuration code resolves that declared owner even after the current row is
gone; it does not infer a backend from a directory or inspect a login manager's
private registry. Device publication, target exclusion and the original COMMIT
boundary are maintained in the [credential contract](harness-credential-reauth.md#codex-device-operation-ownership).

## Assembly, execution and retirement

<a id="instruction-assembly"></a>
### Instruction assembly

`assembly.py` prepares a neutral `TurnContext`: absolute cwd, persona and trusted
turn instructions, selected MCP entries, credential, memory location and model
settings. A profile renders that context into its protocol. MCP entries retain
namespace, transport and identity; changing a namespace changes model-visible
tool names. Connector and built-in tools share this assembly path.

MCP configuration is specific to each execution mode:

| Mode | Configuration boundary |
| --- | --- |
| Claude ordinary interaction | Selected invocation config with `--strict-mcp-config`; native extra MCP entries are not inherited. |
| Codex ordinary App Server | Per-invocation field overrides; native home/project entries can remain visible. Detected selected-namespace conflicts fail preparation before business input. |
| Codex Research exec leaf | No Octopus MCP injection or ordinary resume/fork; `mcp_servers={}` does not prove native entries were removed. |
| Text worker | API request uses `tools=[]` and `tool_choice="none"`; it does not launch an ordinary CLI host. |

The [Codex contract](codex-backend.md#native-mcp-configuration) owns merge and
conflict details; [fixed-version experiments](../reference/runtime-resource-boundaries-20261103.md)
separate effective config from actual thread tool discovery. Tool visibility
does not grant an Octopus effect: each endpoint still verifies its current scope.

`compose_system_prompt` combines framework tool guidance, saved Agent persona,
connector instructions and the profile-selected memory blurb. Framework guidance
describes Octopus tool/runtime behavior; persona describes the role; project
AGENTS/CLAUDE files own project rules; canonical memory owns durable preferences
and pointers. They do not become four copies of the same engineering contract.
Request-specific trusted knowledge restrictions stay associated with that request.

The same original request context carries its interaction policy through child
work and asynchronous returns; a machine return does not turn human work into
an unattended root. Policy controls waiting, without creating permission or a
new Harness execution state. Human native blocking pauses question/idle expiry;
Stop, execution termination and the whole-turn cap still close the original
attempt. See [question ownership](input-reliability.md#原工作交互策略与等待)
and [watchdogs](turn-safety.md#3-per-turn-watchdog-idle--overall).

Claude renders the composed instructions into its native invocation and can
conditionally retain a compatible host; ordinary Codex applies controller
instructions at thread start/resume and updates a resumed thread without changing
saved Agent settings. The constrained exec leaf and tool-free worker keep their
own narrower assembly. The historical fresh-process observation in
[prompt notes](../reference/cli-system-prompt-notes.md) does not override current
reuse, injection or tool authority.

### Execution and resources

MCP ask retains the original SDK Request, accepted creation command and waiter
through [confirmed original-call cleanup](#mcp-question-retirement).

Discovery checks the executable selected for ordinary interaction, using the
same profile selector as invocation construction. Codex reads
`OCTOPUS_CODEX_APP_SERVER_BINARY` at the operation boundary; an invalid explicit
path does not fall back to another PATH entry. Preparation validates the
native protocol and required effective configuration, not an exact version or
binary digest. Availability proves resolvability/executability, not
authentication, model access or readiness of the separate Research exec leaf,
tool-free worker and device-login commands.

SessionOwner owns mutable execution state. Managed preparation, spawn, stdin,
reader and retirement operations report their original results to it. HarnessRun
retains native resources and bounded event delivery through confirmed retirement.
Input delivery, committed business outcome, notification and resource exit are
separate facts under the [execution contract](input-reliability.md#原请求完成与失败交接).

Cancelling a start waiter leaves the original spawn owned by the original
retirement task. Only that spawn's completed result can establish a definite
launch failure without a child; a pending or cancelled spawn is not such proof.
A late handle can arrive after its leader has exited while descendants or
buffered output still retain the pipes. Retirement therefore confirms the
captured group's exit, both pipe EOFs and the original process wait. Original
protocol readers finish before the shared raw drain takes over each pipe;
that drain consumes stdout/stderr concurrently in 64 KiB chunks without
accumulating their discarded output. Business output and diagnostic collection
remain with the original readers. Missing input-history or private-settings
cleanup still fails retirement even when process resources are proven absent.

One-shot calls are owned by their original registry Harness. Before the first
possible suspension it registers the caller and resource operation, retaining
the original spawn, process-group retirement and private-settings cleanup.
A public timeout, repeated cancellation or a finished waiter does not prove
retirement. A late spawn is joined by that same operation; only confirmed group
exit, pipe/wait completion and private-settings cleanup release its installation
drain blocker.
Confirmed launch failure without a child still closes private settings. Failed
exit or cleanup proof retains the blocker and causes shutdown to report failure.
Installation quiesce closes admission synchronously; shutdown joins the original
retirement futures. Reopening cannot discard unresolved resources.

`RunDriver` owns protocol state, without owning a process, lease or product
outcome. `ArgvDriver` supplies Codex exec's initial EOF; `ClaudeDriver` encodes
stream-JSON inputs and gives every opening or steering frame a unique UUID.
`AppServerDriver` owns ordinary Codex RPC preparation, exact history, single-root
correlation and protocol evidence, without product settlement authority.
The shared run checks authority immediately before spawn and business writes,
and owns readers, backpressure, process groups and retirement. Moving wire
operations into drivers does not add a second resource owner or settlement path.

For pending [fork replay](session-rewind.md#532-history-replay-and-first-turn-state),
the original run records possible delivery before business I/O and asks its
driver for saved opening-input evidence at result, EOF and retirement boundaries.
The original recorder commits the native continuation and replay consumption
with input delivery facts; no business outcome is selected there. A definitely
unsent retirement preserves replay. Missing saved-history proof after possible
delivery stays uncertain. Claude supplies its own session/opening-UUID/storage
observation and keeps stream-JSON, held processes and spill handling; it does not
enable the Codex `native_input_evidence` state machine for this purpose.

`can_observe_context` and `can_compact` are independent profile declarations.
Ordinary Codex and Claude declare both; exec leaves and one-shot calls do not gain
compact capability from a backend name. `HarnessRun.start_compact` requires exact
resumable history, reusing preparation, authorization, readers and retirement.
Unsupported drivers refuse it. Compact skips message/attachment/Wiki/replay and
automatic continuation paths. Driver history effects remain distinct from the
Owner's outcome; see [input reliability](input-reliability.md#native-compaction-requests).

`HarnessRun.close_admission()` synchronously closes new input/tool admission and
revokes its run lease and Wiki binding. It leaves the original proof readers and
resources owned. Starting retirement reuses that closure before starting the
original cleanup task. Stop can therefore close admission before storage waits
without losing late native input proof; closure alone is not an exit proof.
Claude's `retains_retirement_output` is independent of multiplexed RPC support.
For a possibly sent input, collection remains owned until the original consumer
finishes the ordered prefix, including context observations and terminal/EOF
closure. Parser EOF alone cannot release this obligation. Admission closure
captures it once through retirement; an unstarted spawn or fully consumed held
stream has no such prefix. The original Owner commits finite late context facts
under their captured request/attempt, even after its reusable-host pointer is
detached. Generic JSONL process fixtures do not inherit native history evidence
merely by borrowing its parser.
The [execution contract](input-reliability.md#原请求完成与失败交接) owns settlement
and cancellation timing.

Claude declares `can_steer=True`, `reusable=True` and `can_continue=True`;
this permits a new continuation input, not replay or exactly-once effects.
`new_driver` selects protocol operations instead of a shared stdin enum. Owner dispatch, API
capabilities and `HarnessRun.steer()` consume the explicit steering declaration;
held-process policy consumes reuse. Tool allow/deny support remains independent.
The ordinary Codex App Server profile declares steering/continuation true and
reuse false: it steers its captured active root and creates a fresh host for
an evidence-qualified later attempt after the original writer retires. The Owner
retains recovery budgets and settlement; continuation does not replay unknown
input or reopen success. Driver-resolved controls carry across attempts within
the original request's configuration. Explicit subsequent requests can still
resume the same native history under the
[native recovery contract](codex-backend.md#error-source-and-recovery).
The independent Research exec leaf declares steering, reuse and continuation
false. It has no ordinary conversation or resume entry. Runtime profiles also
declare `native_input_evidence`: Owner uses that actual driver contract for
pre-recording input facts. Synthetic process peers declare their own capabilities;
backend spelling or a stdin pipe cannot grant native evidence.

<a id="claude-native-compaction"></a>

**Claude native compaction.**

Manual compact retires any previous held writer, then cold resumes the exact
captured session ID and sends one `/compact` frame with its own input UUID.
Ordinary messages may still reuse a compatible held host. The separate compact
operation avoids depending on a prior message's Wiki/instruction spawn signature.
Known `init.slash_commands` unavailability refuses the command before writing;
first init may follow the initial input, so it cannot be a universal pre-write
handshake. A contradictory resumed ID rejects the stream without rebinding the
product session. Command lifecycle, result and explicitly identified observations
must match the original UUID/session; a background notification or older input
cannot end its observation interval.

An original manual `compact_boundary` proves applied history. Automatic
boundaries update the meter without satisfying a manual request. Neither
`status=null`, command synthetic usage, boundary pre/post token metadata nor an
outer success envelope proves a new request sample or a successful no-op.
`local_command_run`, `local_command_outcome` and native failure text are retained;
a native local failure vetoes an outer success. An unassociated terminal cannot
write a new native session ID or replace a previously observed failure reason.
An associated terminal without result text retains its valid string-list
`errors`; an already observed local failure reason keeps precedence.

No-op requires the advertised command, matching start/input/session, a complete
protocol stream, successful native command terminal, no local failure/boundary
and the affirmative reason `Not enough messages to compact.` It preserves the
prior sample and clears activity. This is a conditional documented contract:
the saved 2.1.319 empty-history producer actually failed, so it is not evidence
that that version produced a successful no-op. EOF/Stop without confirmed
boundary/no-op keeps unknown effects and invalidates the old sample. The Owner's
business outcome stays independent of effect. See the
[version observations](../reference/context-meter-compact-g0.md) and
[sample contract](model-reasoning.md#上下文样本与配置绑定).

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
[native input evidence](input-reliability.md#codex-native-input-evidence).
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
check, after all prior awaits; see [question ownership](codex-backend.md#native-question-control).

The native event handoff uses one deque with a 256-event / 1 MiB serialized
payload budget. Streaming ASCII JSON accounting includes every final event field,
including content and any retained raw data. Identity normalization precedes
admission; queued fields do not change, and reclamation/dequeue subtract the
original charged size. This is a payload budget rather than an RSS bound.

Only multiplexed `text_delta` is disposable: it is admitted when both limits
allow it and otherwise omitted, including a single oversized delta in an empty
queue. A necessary event that already fits leaves queued previews alone. When it
does not fit, Run synchronously removes all queued deltas in one stable pass and
rechecks capacity, preserving the order and source of every necessary event.
There is no tail concatenation or incremental string-growth accounting. One legal
oversized necessary event may still occupy an empty queue under the native line
limit; further necessary events cannot expand the budget indefinitely. Complete
text/final, tools, questions, control and structured receipts are necessary.

Each multiplexed admission decision yields only after its queue/count changes
are complete, including omitted previews, failed admission and the independent
root-result slot. The stdout reader also yields after each frame's processing or
error handling, so RPC/control responses, ignored/duplicate notifications,
pre-bind buffering and diagnostics with no display event cannot starve ready
tasks. Neither point waits for display capacity; event-level yielding also covers
one bind releasing a whole buffered batch. A yield proves no database commit,
publication or process exit and does not revoke the captured finite prefix.

Necessary capacity exhaustion still freezes admission and starts the original
retirement task immediately. A failure slot outside the saturated queue preserves
the accepted prefix and prevents a successful result from hiding output loss;
ordinary command retention and omitted previews create no recording issue.
Provider terminal facts remain separate. After confirmed retirement, stream
delivery reconciles final helper observations from the same bounded projection;
no extra output/completion ledger, replacement attempt or automatic rerun is
created. Legacy Claude and Codex exec delivery retain their existing backpressure
and raw consumers; Claude's held-process idle behavior is unchanged.

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

The optional `ActivityObserverFactory` accepts captured environment, activity
notification and optional resume identity, and returns the existing complete
`ActivityObserver` protocol. HarnessRun owns creation, handler attachment, start,
generation invalidation and retirement; consumers call declared methods rather
than discovering extensions with `getattr`. `close(*, deadline=None) -> bool`
reports whether observation completed within the shared deadline. A false result
keeps metrics incomplete; it is not evidence of process retirement. App Server's
native metrics can confirm a thread before a turn exists and remain separate
from exec's turn-based initialization. Startup failure/cancellation, old
generations, observations after close and native lazy-turn regressions retain
these lifecycle boundaries.

Drivers normalize structured provider failures as `HarnessEvent.error_details`.
The existing recovery-message path retains that payload and original runtime
source, including after business status selection. A late failure therefore
remains readable without rewriting the selected outcome or adding a result ledger.

The run also owns local input-submission evidence and its captured cleanup task.
Its shared read-only `retirement_observation`, `retirement_state` and
`wait_retirement()` observe that task without starting
retirement or changing its original process. A live held host remains pending
even after a request driver ends; cancellation of the waiter leaves resource
ownership intact. The [execution observation contract](input-reliability.md#原执行的观察)
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

Rewind uses `Harness.validate_replay` to assemble its retained transcript,
known first-turn configuration and caller-supplied new-input reserve through
the same rendering boundary. It checks the selected credential's argv/env
representation and delegates input capacity to the actual driver, without
registering an execution, refreshing authentication, starting a CLI or writing
spill files. Native defaults not yet resolved for the cwd remain unknown;
real execution still resolves them and checks current authority and actual
bytes. The session saga owns the snapshot, refusal timing and conditional
[ready guarantee](session-rewind.md#532-history-replay-and-first-turn-state).

退休责任覆盖启动时捕获的整个进程组；直接 child 退出或管道 EOF 不证明重定向
输出的后代 writer 已停止。未确认退出仍保留原 backend owner 和配额；请求、
assignment 或 executing 登记结束不放行 Wiki 重试。正常退出与忽略 SIGTERM 的
后代分别核验，不能靠丢弃 done task 取得退出证明。

<a id="mcp-question-retirement"></a>
## MCP 原调用退休

每个服务实例的 builtin 与 connector 同步 MCP 工具共用独立的 40 容量；内层
FastAPI 同步 API 继续使用默认线程池。等待本机 HTTP 的 MCP worker 因而不会
占满回调自身所需的容量，也不会按 namespace 各自扩大并发。调用沿原 ContextVar
传递身份；排队取消不启动 worker，已开始的调用则先等待原 worker 完成，再传播
取消。成功和抛错路径都在退出原 worker 后检查待处理取消，避免 SDK 为已经取消的
调用重复响应。真实 MCP→HTTP→同步路由回归覆盖满容量回环、普通 API 推进、
跨 namespace 上限和两种取消窗口。

只有 ask 的长等待使用 async。实际 SDK Request 绑定原宿主，原 QuestionOwner
持有 `McpQuestionCall`，从建题开始保留已认证 scope、原 MCP RPC/session、
server/tool、实际提供的 metadata 及原 CreateQuestion envelope 回执。metadata
只作关联，不能授予权限；缺失 callId/native 身份不按名字或顺序补造。
HTTP POST 结束不证明 envelope 未创建题目；取消标记也由 `_create` 核验，
已接纳命令最终生成的准确 pending 必须观察并结算。

SDK 可先回复 Request cancelled；handler 在 AnyIO 取消屏蔽内等待原 runner，
runner 在 Owner 外观察创建结果、通过捕获的原 owner 关闭准确题目，再等待实际
REST waiter 的 finally 释放，随后传播取消。`ready` 只证明唤醒，不替代 waiter
退出。run bearer 已退休时也不恢复旧权限或把清理 401 当作完成证明。
封口 Owner 仍保留未退休 call；失败记录同时阻止自然排空和实际 shutdown，沿
既有 owner/writer failure 出口传播，不能因 task 回调移除而假成功。
runner 一旦进入取消或通信失败清理，既有 cancelled 标记阻止后到取消再次打断
创建回执、关闭持久化或 waiter 释放；真正清理失败仍保留原故障事实。

runner 完成不自行删除登记。原 Owner 在确认实际 task done 和清理成功后消费该
call，留下原 attempt 内有界的精确清理凭证并确认 consumed；shutdown join 同时
等待这两个事实。公共退休边界也检查仍登记的 call，包含 Stop 和已选故障路径，
不能提前确认 execution retired 或释放 App 槽位。

精确清理凭证匹配原 scope、request/attempt、thread/turn 和真实 MCP callId，
不能按工具名字推断。凭证不伪造 item.completed，本地清理和接纳不证明 provider
消费。纯 ask 输入缺项的有限成功裁定归[Codex 完成合同](codex-backend.md#root-completion-and-selected-answer)，
领域消费者继续核验自己的产物、发布或父子交接凭证。

结构化 GET 兼容提供原已保存 resolution 和可读文本；普通 GET 的关闭 404 语义
保留。单次 408、一个 poll 取消或浏览器断线不取消原工具调用。未知 qid、认证
失败、不可达或无效响应仍为工具错误；只有准确原记录的已保存结算可证明关闭。
本地接纳回答、模型实际收到回答和本地资源退出保持不同事实。

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
The OAuth-specific `complete_oauth` driver entry carries the fixed request and
narrow credential writer into the original login task, through its database
commit. Codex's device flow keeps its separate start/status/cancel semantics.
Its scrape budget ends only when both verification URL and user code are
available. The wakeup event also signals errors/exit and is not success proof.
Missing either field times out and joins the original process/drive; a selected
error or cancellation cannot be replaced by a late successful exit. A complete
device code retains the separate user-authorization window. Reauthorization
failure removes only its staging home and preserves the existing credential
and native history. Controlled CLI regressions in `test_codex_login.py` verify
partial output, the deadline boundary, late exit and actual retirement.

Transcript import/export and fork are separate capabilities. Claude's transcript
codec supports handoff/pull; lack of a Codex codec produces an explicit unsupported
result. Both backends support [rewind replay](session-rewind.md), whose policy
belongs to the session saga. Their profile functions implement
[full native-copy fork](session-fork.md) and artifact cleanup. Fork support does
not imply a public export codec. `ForkCopy` and `ForkCleanup` express the exact
profile signatures using `ForkArtifact`, `ForkHooks` and credential types.
Adapters reject unsupported native artifacts explicitly. The original session
saga still owns reservation, saved identity and compensation ordering; moving it
into another manager would not reduce a caller's responsibility.

### Complete typed transcript export and local pull

`GET /api/sessions/{id}/message-export` supplies complete `MessageContent`
records to a restoring codec. It is separate from browser message windows and
the raw `transcript-export` audit stream, which preserves storage rows, including
duplicate sequences. Each export page checks the current account/session binding
in the same short SQLite transaction as its read, for live and archived sessions.
Missing and foreign sessions both return 404.

Complete means the actual saved record. Both exports preserve command
`tool_input.output_capture`, content and provenance; a `head_tail` record does not
contain an archived middle to restore. Older records without capture keep their
saved bodies and metadata, with no inferred completeness or backfill. See
[command retention](codex-backend.md#saved-command-output).

The first transaction captures the backend, native resume ID and working directory,
the maximum committed `messages.id` (`through_id`), `expected_count`, `max_seq`
and the oldest page. Later requests echo this `snapshot` as JSON and advance
`after_seq`; the echo is reading context, never authorization. Rows always satisfy
`id <= through_id` and arrive in ascending seq order. `next_after_seq` refers to
the last returned record, including seq zero, rather than the extra row used to
detect another page. A page defaults to 200 records and accepts at most 500.
New appends and reserved low sequences committed later are outside the captured
membership. No transaction or persistent export task spans HTTP requests; this
is a fixed-member read, not a full byte snapshot across those requests.

`octopus pull` uses this endpoint and the captured backend's existing codec,
refusing a backend without one before creating output. The first resume ID and
working directory remain its codec inputs across pages; `--cwd` and
`--project-dir` retain their overrides, and a missing resume ID generates one
local UUID for the whole export. The native ID must be a safe single filename.
For Claude history, CLI destination selection, discovery and handoff reuse the
Harness project-directory rule: the configured `CLAUDE_CONFIG_DIR` (otherwise
`~/.claude`) owns `projects/`, and every character outside `[A-Za-z0-9]` in
the working directory becomes `-`. An explicit `--project-dir` takes priority;
otherwise `--cwd`, then the current directory supplies that rule. No extra slug,
hash, truncation or search fallback is added. Offline export, pull, discovery,
handoff and authenticated import tests cover special and long directory names;
they do not establish compatibility with every external Claude CLI version.
One iterator feeds one codec invocation, retaining its UUID/parent chain across
page boundaries. Strict sequence order, snapshot identity, cursor progress,
final source count and highest seq are checked before publication; legal gaps
and empty sessions are supported. Initial duplicates refuse export, and deleted
unread members or damaged pages cannot publish a partial file. The success count
is the confirmed source count: the codec may omit events its native format does
not represent.

The CLI owns a temporary file and descriptor in the destination directory.
Download, parsing, complete iterator consumption, codec output, flush, file fsync
and close all finish before atomic replacement, followed by directory fsync.
Earlier failures preserve the existing target and remove this attempt's temporary
file; a cleanup refusal identifies the remaining temporary path. A failure after
replacement reports that the new file may already be active and does not claim
rollback. Success is printed only after the required synchronization completes.

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
`tests/test_installation_drain.py` follows a timed-out or twice-cancelled one-shot
through a refused quiesce, its late real process exit and successful quiesce;
failed private-settings or exit proof remains a blocker.
`tests/test_session_capability_projection.py` checks all four steering/reuse
combinations through real subprocess input, API projection and Wiki tool visibility.

Ordinary App Server start and precise resume are described in
[the Codex contract](codex-backend.md#opt-in-app-server-start-and-precise-resume).
The [paused execution scope](../plans/runtime-execution-scope.md) retains the
stronger post-final capability goal; the
[DeepSeek plan](../plans/deepseek-harness-backend.md) owns its unimplemented backend.
The [original implementation](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/harness-layer.md)
contains old API sketches and rollout evidence; current verification follows
[testing.md](../testing.md).

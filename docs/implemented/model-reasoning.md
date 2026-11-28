# 模型、推理强度与速度设置

> **Status:** implemented — 独立模型、推理强度和 Normal/Fast 控制，保留继承、运行时发现与会话历史。

The conversation settings button and `/model` open the same dialog. The
conversation has independent model, reasoning effort, speed and sign-in
overrides. Clearing one restores the saved default source for that field. The
Agent form exposes the default engine and sign-in. Its Model defaults section
groups model, effort and speed.

Codex also offers a separate Speed selector: Normal and Fast. Agent speed
defaults to Normal; sessions inherit the agent's choice and can override it
independently. Fast increases usage. Octopus does not offer Ultrafast.

`POST /api/agents`, `PATCH /api/agents/{id}` and
`PATCH /api/sessions/{id}` accept `service_tier: "fast"` to opt in or
`service_tier: "default"` for Normal. Omitting the field from a PATCH keeps its
binding. Passing `null` restores the saved default source for the session.
Normal applies when neither the session nor its applicable Agent default sets
Fast. Speed and reasoning effort stay independent.

## Persistence and precedence

Each session stores `defaults_source` with its engine and overrides. An
explicit choice of a different engine from the owning Agent starts with
`backend` defaults. Choosing the Agent's engine starts with `agent` defaults.
For the `agent` source, an empty override uses the Agent value if present and
the runtime default otherwise. For the `backend` source, an empty override
uses the runtime default. Codex resolves an empty speed under either source
to Normal when no applicable Agent speed is set.

`configuration_version` travels with the saved engine, default source, model,
effort, speed and credential override. New sessions, imports and copies start at
version zero. Archive restore registers the original version and current settings
from the same protected read. Explicit settings saves advance the stored version
even when the values compare equal. Credential removal clears affected overrides
and advances their versions in one transaction before publishing to live sessions.
Pure renames and Agent default changes leave existing session versions intact.

Changing the Agent's defaults keeps each existing conversation's engine and
source. Restore, archive replacement, rewind and full-copy forks preserve the
source with the saved overrides. Archive replacement inserts the entire
successor configuration in one write before rebinding schedules.

Ordinary creation and [session import](input-reliability.md#session-history-import)
resolve the same configuration candidate under the configuration lock. The
candidate includes the selected engine, defaults source, per-session overrides,
effective credential and tool policy. An explicit credential must belong to
the account and support the selected engine. Validation and persistence use
the same candidate. Later changes follow the saved source and override rules.

Preparation captures the session overrides and Agent defaults together under
the configuration lock. The read runs in the original request's preparation
task. The Owner retains execution and context writes. Changes saved before
capture apply to that request. Later changes apply to a subsequent request;
retries keep the original capture. A held process's spawn signature includes
effort and speed, so incompatible settings replace the process while retaining
the backend resume id.

## Saving defaults and repairing an existing session

Saving an Agent validates the complete candidate against its own backend,
credential and model controls. A legal default change can make an existing
session's inherited settings temporarily incompatible; that does not veto the
save. The session keeps its original backend, history and native resume identity.
Overrides are not automatically added or converted to preserve compatibility.

The header shows the saved engine, model and available effort or speed summary.
For a conversation using the runtime model default, it uses the configured
default once settings have read it for the saved engine and effective sign-in.
The dialog uses a catalog dropdown for Model and shows the source in each
default option. Sign-in is folded during normal use. A credential and model
controls can be repaired in one PATCH. The dialog refreshes the catalog on
opening, engine changes and effective sign-in changes. A failed save keeps the
draft and displays the reason in the original dialog. A rejected Agent model
control opens Model defaults and points to the field that needs repair.

Settings PATCH requires `expected_backend` and `expected_configuration_version`
from the same snapshot. Missing preconditions return `428`, so an old browser
page must refresh. An outdated version returns `409 configuration_version_conflict`.
The original SessionOwner compares the persisted version in the same transaction
that saves the fields, any included name change and the next version. Confirmed
rollback leaves the original values and version intact. Unknown commit outcomes
follow the storage fail-stop rules.

For a same-engine PATCH, the dialog includes edited fields and previously
submitted fields with an unknown outcome. A timeout, network failure, HTTP 408 or
5xx lets the user explicitly resubmit those fields, including a value restored to
the original setting or a `null` that restores inheritance. A background refresh
keeps the dialog's edit baseline. On conflict, the dialog reads the full current
snapshot, preserves the user's edits and unconfirmed fields, and requires explicit
confirmation before saving against the new version. Cross-engine updates send the
complete target settings. Draft conflicts between engines must be resolved before
submission.

An empty conversation can switch engines in place. The Owner checks admitted
input, history and the native binding before committing a complete target
configuration and captures the success response before the next commit. The
session list and details report admitted input even before its first message.
The request includes all four overrides, using explicit nulls to select their
defaults. A busy lifecycle operation is a separate conflict. For a
conversation with history or admitted input, the dialog offers to start a
new conversation for the same Agent and working directory. The original
conversation keeps its history and engine.

| Boundary | Validation and timing |
| --- | --- |
| Agent save | Serialize candidate read, complete validation and write; validate the new defaults themselves. |
| Session save | Validate the complete proposed combination under the retained or selected engine before saving it atomically. |
| Logical request capture | Capture the effective configuration under the configuration lock; retries keep that capture. |
| Process launch | Recheck supported controls and current credential authorization before invoking the captured backend. |

`AgentManager` owns candidate validation and default saves. The original SessionOwner
owns override commits and configuration capture; the existing session module
coordinates committed credential-deletion propagation and
request capture in [configuration.py](../../server/sessions/configuration.py).
These operations share the database instance's short configuration lock;
routes translate requests and errors without composing locks or live-field
refreshes. Effective field precedence stays in `effective_settings`.
Credential refresh, connector loading and other slow preparation happen after
capture releases the lock. Revoking a credential can still prevent spawn or
any later frame write; capture grants no continuing authorization.

Research rechecks the original live Session identity and admission after capture
returns, before registering a job. A session deleted or archived while that
result was pending cannot admit research from its retained configuration copy.

Context-affecting changes follow [context binding](#上下文样本与配置绑定).

`resolve_credential_by_id` in [credentials.py](../../server/sessions/credentials.py)
serves ordinary sessions, Research, files and Agent operations. It owns
the account/credential refresh task, shields it from an individual waiter's
cancellation, conditionally commits refreshed secrets and rechecks authorization
after waiting. Manager/database shutdown drains those original tasks. An explicit
invalid binding never falls back to the host login.

Configuration capture owns request values; credential resolution owns
secret freshness and current authority.

## Runtime metadata and validation

`GET /api/backends` reports registered engines in `supported`, engines with a
resolvable conversation CLI in `available`, and the deployment preference for
new agents in `default`. Model shortlists and tool policies come from each
registered profile. The browser reuses one successful snapshot across views
in the current account. A failed read offers retry. An account change resets
the catalog and discards pending results from the previous account.

The Agent form offers registered engines and marks missing CLIs. Creating an
Agent requires a successful catalog read and uses the deployment preference
until the user chooses an engine. An existing Agent's saved engine remains
visible during a failed read, and the form can still save other changes.

The new session form shows the owning Agent's engine as the inherited choice and
offers detected engines as explicit overrides. Inheritance stays available while
discovery is pending or has failed. Switching between inheritance and an override
for the same engine keeps the selected credential. If an Agent update changes
the inherited engine, an incompatible selection stays visible and blocks
submission. The user can choose a compatible credential or switch to the engine
that matches the existing selection and keep that credential. A different
engine offers that engine's host sign-in and its compatible stored credentials.

`GET /api/backends/{backend}/models` requires authentication and account scope.
An optional `credential_id` must belong to the caller and match the backend.
The harness reads only model metadata from the effective credential's runtime
home, using the same fallback to the host login as an actual invocation.

Codex reads its own `models_cache.json` and the model/effort defaults from
`config.toml`; the response exposes no file paths, tokens or arbitrary config
values. The next catalog read reflects cache updates. Missing or damaged caches
return a catalog error with a retry action. Existing overrides can be cleared
while the catalog is unreadable. The save endpoint validates the resulting
configuration. Editors keep saved values visible while waiting for metadata and
distinguish an unsupported value from an unreadable catalog. The dropdown shows
the saved model outside the catalog so changes to other settings can preserve it.
The optional `saved_model` entries look up IDs within the same authorized
runtime home. The browser supplies the saved session and Agent model IDs. These
capabilities constrain the controls without adding models to the dropdown.
Existing model IDs remain supported by the API. Chat's
`/model` command opens the same settings dialog as the header. Claude Code
advertises low, medium, high, xhigh and max for its aliases and free-form models.
These are CLI request levels, not a guarantee of distinct model capabilities.
Claude defaults remain unspecified so clearing both overrides follows the CLI.

The model catalog includes `service_tiers` and `default_service_tier`.
Codex maps its advertised `priority` tier to Fast and also understands the
older `additional_speed_tiers` catalog field. Known models only offer Fast
when the cached catalog advertises it. Missing caches retain Normal/Fast for
free-form models, and the runtime remains the authority on actual access.
Claude Code does not expose speed controls. Invalid tiers and unavailable
model/tier combinations are rejected before changing any bindings.
Hidden cached models also constrain validation without appearing in the picker.
Agent saves, session saves and execution share the same pure model-control
validation. Empty supported-control lists do not authorize explicit values.
Free-form model names preserve backend capability checks; invocation preflight
catches incompatible inherited settings and changes in availability after a save.

Ordinary Codex applies model, reasoning effort and explicit service tier during
native thread start/resume and verifies the returned effective settings before
business input. Fast maps to native `priority`; Normal is explicit `default`,
so a resumed thread cannot retain its previous Fast setting. Model-only changes
keep the effort binding; effort is not converted into another model. The
independent exec adapter can render `-m` and TOML effort/tier overrides, but
Research forwards the captured model, `reasoning_effort` and `service_tier`
through [manager](../../server/research/manager.py),
[orchestrator](../../server/research/orchestrator.py) and both
[leaf entry points](../../server/research/leaf.py), into `RunConfig` or
`OneShotContext`. This establishes parameter transmission; provider adoption
and effects require the corresponding backend evidence.
One-shot schedule parsing and file-reference resolution pass the selected
settings to the text worker. No global Codex config or saved agent row is rewritten.

Claude Code renders explicit effort as `--effort` and sets
`CLAUDE_CODE_EFFORT_LEVEL` in the child environment to the same value: that
environment variable takes precedence over the CLI flag. When both overrides
are empty, neither is changed. Turn and one-shot calls share this rule; host
configuration is never rewritten. The existing process replacement mechanism
applies changes on the next turn while preserving the resume id.
DeepSeek maps low to low, medium/high/xhigh to high, and max to max; Octopus
passes the selected request level through without provider-specific conversion.
That provider mapping inside the Claude runtime is not a registered DeepSeek
Harness backend. The separate backend and capability investigation remain in
[the DeepSeek plan](../plans/deepseek-harness-backend.md).
See [Claude effort precedence](https://code.claude.com/docs/en/model-config#adjust-effort-level)
and [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/).

CLI configuration discovery describes the selected runtime's user config;
workspace-specific CLI overrides can still affect settings inherited from the
CLI. Explicit Octopus overrides take command-line precedence. A cached catalog
is the client's last advertised list, not a new availability check.

评测固定有效 model、effort、tier 及所选凭据来源；会话覆盖为空时继续核对 Agent
继承值和实际解析的同一目录，不把继承误判为登录回退。Normal 的配置值是
`service_tier="default"`；配置生效与原生响应的计费档位观察分别记录，后者缺失时
仍为未知。原生 response 用量关联原 request／attempt／thread／turn，cached input
和 reasoning output 是各自总量的子集，不重复相加，也不从帧字节推算 token 或费用。
输入测量只支持其固定条件下的结论，方法归[验证政策](../testing.md#optional-resource-experiments)。

## 上下文样本与配置绑定

`sessions.context_state`保存最近可信原生root样本及其适用范围，与Monitor累计
消耗分开。状态为unknown/valid/stale，样本带原request、attempt、连接代次、原生
thread/turn和观察序号；独立revision表达完整替换顺序，binding revision表达配置
或原生上下文边界。失效保留历史样本，不把旧值恢复为当前比例；NULL表示尚无
上下文事实。详情在同一次短配置锁读取中捕获已提交state及非持久的context_pending
展示资格，不创建或等待执行Owner；NULL无事实直接返回。绑定尚未对齐或原Owner
已失败时pending为true，保留样本供历史详情，但不显示为当前比例。需要对齐时
只唤醒已有Owner，不承诺即时通知；后续合法Owner建立后沿原路径对齐。原Owner
仍是原生运行时样本的唯一持久写者，纯读不制造revision或采样时间，历史诊断读取
不依赖其命令推进。

Codex App Server使用精确绑定根turn的`last.totalTokens`和同通知有效window，
排除child、旧turn/连接、resume replay和重复通知，不将累计total作为采样序号。
观测不累加、不估算聊天字符、不更新业务活动时钟。缺窗口时只保留实际用量，
不得生成比例；采样时间只表示该原生样本被产品记录，不承诺逐字实时预算。
同turn模型reroute使旧模型/窗口配对失效，不能用后续无模型身份的通知自行恢复。

Claude取最近合格根assistant请求的`input_tokens + cache_read_input_tokens +
cache_creation_input_tokens`，不含output，也不累加工具前后或同消息的重复内容块。
分母仅取result.modelUsage中与该assistant实际model完全同键的有效contextWindow；
不使用Agent模型别名、canonical计价名、首个map项或默认窗口。缺分母仍可保存
可信分子，但比例未知。实际模型改变先使旧配对失效，即使新usage缺失或非法；
同模型的坏usage不覆盖最近可信值，同消息后续可信细化可恢复。

Claude在发送前登记opening输入UUID及同attempt已送出的steer UUID；根assistant
须证明原输入来源，排除child、旧opening、无身份的后继根通知和旧消息重放。
消息身份与可补窗的数值候选分别有界保留；result不能为压缩前候选补窗复活旧值。
原生输入关联不等于Owner写权限：无Owner的Research leaf仍可接收其真实终态和
费用，但没有会话圆环写权限。原task-notification终态不能关闭新输入的观察。

自动压缩按item身份和有序观察区间收集候选。started保留有说明的压缩前样本并
标记正在压缩；completed与可证明的原生重算候选一次提交，否则stale。锁版
重算的五项明细均为零，但合成满窗delta也可能具有此形状；累计total等于window
且total/last明细全零的歧义通知不产生比例或post候选。完整通知去重跨item边界
保留，rate-limit重发不能解除stale或刷新采样时间。缺completed影响观测有效性，
不为普通消息新增普遍完成义务；此观察不创建手动压缩请求。

Claude的compacting状态展示最后压缩前样本，boundary切断旧候选并持久stale；
pre_tokens/post_tokens不是最近根请求的input/cache分子。下个合格请求及精确窗口
到齐才恢复比例。boundary后迟到终态不再抹掉新样本；重复boundary不推进状态。
只有完整肯定no-op可保留有效样本并清除活动。自动压缩开始后若无boundary就
terminal、EOF或Stop，由原driver有限收尾报告operation_unconfirmed，Owner清除
活动并持久待更新；status=null和累计result都不能解除失效。

实际准备捕获不可变binding并随准备结果交回Owner，重试沿原配置合同，不在慢
准备后重读新默认值。配置保存、样本接纳及已有快照对齐使用短configuration lock，
锁内不等待另一Owner、模型或通知。会话配置与失效同事务；Agent默认backend/
model/credential实际改变及凭据删除在原事务推进`context_settings_revision`。
依赖任一默认设置时保守纳入完整Agent版本，明确覆盖全部相关值时不依赖它；
名称、头像及不改变上下文绑定的effort/speed不推进该版本。继承A→B→A仍失效，
丢通知也不能跳过版本比较。首次原生ID补全不淘汰证明该ID的原attempt；已知ID
替换、清除或reset推进失效边界，旧attempt的晚到样本不能重新配对当前配置。

context、原生ID和会话配置的持久写入在提交前校验已有writer的Session身份、
失败状态及当前task；样本另须通过原request、attempt、binding和观察顺序校验。
COMMIT确认后才更新内存事实并通知捕获的同一Owner。合法的无Owner配置对齐
可以保存事实，不为通知安装执行者；context没有变化时保留原短路。真实存储错误
仍执行既定fail-stop，不由归档门禁或通知路径吞掉。归档关闭新接纳与原执行的
有限收尾资格分别成立，见[原请求完成](input-reliability.md#原请求完成与失败交接)。

启动在开放准入前，随原输入恢复事务对本次可能已发送的未完成执行保存失效，
覆盖普通消息中的自动压缩。明确未发送/被拒及纯pending保留其不同证据；旧
unknown行不在每次重启反复抹掉后来取得的新有效样本。归档详情只展示历史，
unarchive先对齐；fork沿新会话/原生身份建立自己的适用范围，不继承当前百分比。
完整备份、搬迁和有限转换范围见[存储合同](current-storage-format.md#complete-record-projections)。

界面展示与 HTTP/WS 合并归[会话读取合同](frontend-session-observation.md#会话打开与辅助数据)，
字段与时序由 [Codex 协议](codex-backend.md#native-compaction)和
[Claude compact](harness-layer.md#claude-native-compaction)分别定义；原始 JSON
样本保留于 `docs/reference/`，其版本不构成当前现场验证。

## Verification

Backend coverage checks independent inheritance, validation, metadata redaction,
credential ownership, persistence, archived restore and fork inheritance. A
configuration regression orders saves at the actual lock-acquisition boundary;
queued, captured and retried requests use the expected complete settings.
Spawn and initial-frame regressions revoke the captured credential before its
actual effect. Frontend tests cover effort-only requests, model-specific choices,
rejection messages, combined credential/effort repair and concurrent store
updates. Browser coverage changes effort through the visible header and reloads
an existing transcript. This installation's routine Codex live checks use low
effort; backend-specific parameter and retry variations retain hermetic coverage.
Speed coverage verifies Normal defaults, metadata compatibility, independent
overrides, validation, persistence, archive replacement, forks and retry
snapshots. Browser coverage toggles Normal → Fast → Normal and reloads the
original history. The live Codex test follows the same three speed settings
while recalling the original word from the same backend thread.
The CLI's applied-thread-settings records verify Fast (`priority`) followed by
Normal (`default`). Concurrent PATCH, fork/reload and stale-form regressions
cover the independent settings bindings.

Claude coverage uses its production renderer/parser with a hermetic STREAM_JSON
subprocess. It verifies low → high → cleared session → cleared agent settings,
child environment precedence, process replacement, a stable resume id and
preserved history. Agent/session UI tests cover saving Claude effort independently
of model selection. This coverage does not establish live Claude or DeepSeek
model behavior; this installation's live release checks remain Codex-only.

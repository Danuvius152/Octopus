# 模型、推理强度与速度设置

> **Status:** implemented — 独立模型、推理强度和 Normal/Fast 控制，保留继承、运行时发现与会话历史。

The chat header opens the same dialog as `/model`. A model override and a
reasoning-effort override are independent: editing only effort sends only
`reasoning_effort`, so the model binding stays intact. The agent form sets
durable defaults. Clearing either session field restores inheritance for that
field alone.

Codex also offers a separate Speed selector: Normal and Fast. Agent speed
defaults to Normal; sessions inherit the agent's choice and can override it
independently. Fast increases usage. Octopus does not offer Ultrafast.

`POST /api/agents`, `PATCH /api/agents/{id}` and
`PATCH /api/sessions/{id}` accept `service_tier: "fast"` to opt in or
`service_tier: "default"` for Normal. Omitting the field from a PATCH keeps its
binding; passing `null` clears it. A cleared session inherits its agent,
and a cleared agent uses Normal. Speed never changes the model or effort.

## Persistence and precedence

`agents.reasoning_effort` and `sessions.reasoning_effort` are nullable TEXT
columns. Existing rows remain NULL. Each field resolves independently as
session override → agent default → CLI configuration. Session model, effort
and credential bindings can be cleared to NULL and survive a restart.
`service_tier` is a separate nullable TEXT column on agents and sessions;
its precedence is session override → agent default → Normal, regardless of
Fast preferences saved in the CLI. Existing rows therefore use Normal.
Archived detail, restore, archive replacement, rewind and full-copy forks
preserve the settings along with their existing ownership and resume state.

One logical request captures its session overrides and Agent defaults together
through its original Owner when preparation acquires the configuration lock. Queued input and preparation
still waiting for that lock have not captured settings: changes saved before
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

The chat header and `/model` show the selected session's effective settings and
each field's source. The dialog can change credential and model controls in one
atomic PATCH, including when both an inherited credential and effort are
incompatible. Clearing a credential restores inheritance. Its draft catalog
describes the draft credential; it does not replace the current effective-value
display. Rejections preserve existing bindings and explain what to correct.
Responses remain associated with their original session, so switching sessions
cannot move an error or close the new session's dialog.

| Boundary | Validation and timing |
| --- | --- |
| Agent save | Serialize candidate read, complete validation and write; validate the new defaults themselves. |
| Session save | Validate the complete proposed effective combination using the session's retained backend; reject atomically. |
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
already serves ordinary sessions, Research, files and Agent operations. It owns
the account/credential refresh task, shields it from an individual waiter's
cancellation, conditionally commits refreshed secrets and rechecks authorization
after waiting. Manager/database shutdown drains those original tasks. An explicit
invalid binding never falls back to the host login.

Keep these two narrow boundaries: configuration capture owns request values;
credential resolution owns secret freshness and current authority. A combined
configuration/credential service would move initialization and drain obligations
without removing a rule from its existing consumers. Add new settings through
the existing precedence, validation and capture paths.

## Runtime metadata and validation

`GET /api/backends/{backend}/models` requires authentication and account scope.
An optional `credential_id` must belong to the caller and match the backend.
The harness reads only model metadata from the effective credential's runtime
home, using the same fallback to the host login as an actual invocation.

Codex reads its own `models_cache.json` and the model/effort defaults from
`config.toml`; the response exposes no file paths, tokens or arbitrary config
values. Missing or damaged caches leave free-form model selection and basic
reasoning levels available. The advertised levels for a known model constrain
the picker and explicit effort updates. Model names remain free-form so a new
model does not require an Octopus release. Claude Code advertises the request
levels low, medium, high, xhigh and max for its aliases and free-form models.
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
Research currently forwards only the model: its manager, orchestrator and both
leaf entry points omit selected effort/tier. Capturing the configured session
therefore does not prove those controls reach a Research execution; see
[the current Research limitation](native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler).
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

界面展示与 HTTP/WS 合并归[会话读取合同](frontend-list-reconciliation.md#会话打开与辅助数据)，
锁版字段与时序依据见[协议记录](../reference/context-meter-compact-g0.md)。

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

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/model-reasoning.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

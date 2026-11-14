# 输入接纳、原请求完成与恢复

> **Status:** implemented — 持久输入、原请求完成与问题结算、公共返回接管及显式恢复。

| 任务 | 首读范围 | 扩读条件 |
| --- | --- | --- |
| 接纳、重复提交或显式恢复 | [回执](#receipts-and-results)、[HTTP/WS](#http-and-websocket)、[保存的输入](#saved-inputs) | Codex 写入证据见[首输入](#codex-native-input-evidence) |
| 完成消费者或 Owner 失败接管 | [原请求完成](#原请求完成与失败交接) | 需要退出证明再读[原执行观察](#原执行的观察)及[Harness](harness-layer.md#execution-and-resources) |
| 读取 App/委派回复正文 | [原请求结果](#原请求结果读取) | 原生 final 完整性归[Codex](codex-backend.md#root-completion-and-selected-answer) |
| 问题回答、到期或无人值守等待 | [问题结算](#问题结算)、[交互策略](#原工作交互策略与等待) | MCP 清理归[Harness](harness-layer.md#mcp-question-retirement)，原生写帧归[Codex](codex-backend.md#native-question-control) |
| BG/Research/委派返回 | [公共返回接管](#公共返回接管) | 改来源材料、重试或恢复才读对应领域合同 |
| compact | [原生维护输入](#native-compaction-requests) | 协议及效果证据归[Codex](codex-backend.md#native-compaction)或[Claude](harness-layer.md#claude-native-compaction) |
| 聊天或应用修改框刷新恢复 | [浏览器文本草稿](#browser-text-drafts) | App 回执及弹层寿命归[列表与视图合同](frontend-list-reconciliation.md#应用修改草稿与视图寿命)，聊天归[共享聊天稿](frontend-list-reconciliation.md#共享聊天文字与操作寿命) |

The database owns the complete accepted input and its execution status.
SessionOwner decides when it can run. Acceptance commits the text,
attachment identities and byte hashes, request context, source, order and
stable identity together. The dispatch queue holds an identity and display
projection; execution reads the saved input. Model and tool effects begin
only after the claim commits.

Manager construction, listeners and in-memory inspection need no database.
Execution admission opens only after initialization, input recovery, session
loading and incomplete-fork recovery all succeed. An assigned database pointer
alone proves no readiness. Uninitialized, initializing or failed recovery returns
an explicit `service_not_ready` refusal without saving, queueing or spawning the
input; its identity may be submitted explicitly after the condition clears.
Status queries in that phase are unavailable, rather than proof that an input
does not exist. Shutdown remains a separate admission condition and existing
accepted identities retain their original receipts and content-conflict checks.

All input sources use this boundary, including internal messages, inline steer,
one-shot schedules and their new sessions, application turns/builds, independent
task returns and explicit input recovery. Both queues hold only pending identity
and display/scheduling projections; execution and steer preparation reload the
saved content and attachment evidence. A retained unstarted claim may continue
only in its original process generation before frame delivery. Restart unknown
and uncertain steer inputs never acquire that permission. Required attachments
are rechecked at claim; a changed file rejects execution as a whole. Once an
input is accepted, missing or untrustworthy storage fails its original owner;
there is no in-memory execution or successful-completion fallback. Resource
retirement and empty-manager shutdown still run under their own lifecycle.

## Receipts and results

Supply one `client_request_id` for an input, and retain it when confirming a
lost response. Repeating the same identity and content returns the original
fact; different content conflicts. An explicit refusal creates no input row,
so the user can retry that identity after fixing the cause. A response or
execution whose result is unknown must be checked under its original identity.
The browser uses cryptographic randomness and preserves the draft if none is
available.

| State | Meaning |
| --- | --- |
| `pending` | Accepted and not yet claimed. |
| `running` | Its execution claim committed; this alone does not prove model delivery. |
| `done` | Settled. Read `outcome.status` for the business result. |
| `withdrawn` | Withdrawn before execution; eligible inputs can be restored for review. |
| `unknown` | Execution or delivery cannot be confirmed; no automatic replay. |

Delivery evidence has its own `delivery_state`. An uncertain steer remains
unknown even when the carrying request has a successful result. For ordinary
single-input execution, the exact terminal and input settlement commit in one
transaction. Notification failure cannot reverse a committed outcome.
For ordinary Codex App Server, `native_inputs` separates accepted native steer
from its carrier's business result. Accepted does not prove model processing,
even when the carrier succeeds. Only a proven unaccepted steer may return to
the ordinary queue under its original identity and order; unknown inputs stay
out of automatic dispatch. Details and target qualification follow
[the native input contract](#codex-native-input-evidence).

First-input [history replay](session-rewind.md#532-history-replay-and-first-turn-state)
has a separate installation fact. The original input recorder commits matching
resumable identity, delivery and replay consumption in one transaction, preserving
its business outcome and durable fork evidence. Native acceptance alone does not
establish that fact. The Owner re-reads continuation/replay/metadata before prompt
construction and recovery classification, so a lost notification cannot wrap
history again. Possibly sent but unconfirmed replay prevents automatic next input
or internal continuation, including restart; definitely unsent replay is retained.

On restart, unclaimed inputs become withdrawn and claimed inputs become
unknown. Missing legacy payloads and completed legacy rows without an exact
terminal remain review-only. Historical refusal receipts stay read-only.
Managed startup retirement and current-generation retirement keep their
existing authority; hiding a saved input proves neither execution exit nor
permission to dispatch.

<a id="browser-application-text-drafts"></a>

## Browser text drafts

`textDrafts` saves application-change and ordinary chat text in the current tab's `sessionStorage`.
Its versioned namespace keys contain the confirmed stable `identity.user_id`,
draft kind and object identity: `application` with App ID, or `chat` with `shared`.
Chat keeps one shared text across session switches and view remounts, rather than
one draft per conversation. Values contain only text. Runtime draft
versions and record identities prevent an old acknowledgment from clearing a
new edit, but are not persisted or interpreted as server receipts.
`accountGeneration` retires runtime ownership and is never the cross-refresh
account key.

Restore waits for confirmed account identity. A null identity during startup
does not mean logout. If the user edits before identity arrives, the new version
wins over stored text and is saved once identity is known. Edits and clearing
write synchronously rather than waiting for an effect or unload callback.
Restoring saved text can change the text without incrementing its runtime
version. Async draft consumers must observe both changes; a version-only guard
cannot protect a just-restored draft. History's full-body read and saved pre-walk
draft follow the [composer contract](composer-and-time.md#composer-history).
Token/account changes clear the previous draft namespace, including entries for
Apps not opened in this page lifetime; identity confirmation removes entries
belonging to other accounts. Confirmed App deletion also clears an unopened
App's saved text, including when identity has not arrived yet.
Confirmed deletion of the current session clears the shared chat text and
advances its version even while ChatView is unmounted. Archiving or deleting
another session does not clear it.

At App submission, the captured version immediately leaves unsent-text recovery.
Its in-memory text remains available while the result is pending or unknown;
later edits are saved as a new unsent version. A matching success clears only
its own version. Refresh never restores busy/error or an old operation's rights,
and never sends or replays work. This browser text copy is separate from saved
accepted inputs, unknown submission observation and explicit input recovery;
it cannot establish whether a build was accepted or completed. App submission
and view ownership follow the [App draft contract](frontend-list-reconciliation.md#应用修改草稿与视图寿命).

Ordinary message sending clears the composer and its saved text synchronously.
Its rejected/unknown receipt and attachment recovery remain separate business
paths. `/compact` immediately consumes the submitted version for refresh recovery,
while retaining its in-memory command during observation or unknown admission.
Acceptance clears only the matching original version; later edits, even identical
characters, remain unsent text. When its composer retires, a still-consumed command
is cleared so remount cannot present it without the original confirmation context.
New edits survive that cleanup. Refresh restores neither confirmation IDs nor
operation rights, and never submits automatically. Message and compact remain
distinct in receipt observation and explicit recovery.

Fork prefill waits for identity restoration. It fills only an empty draft whose
version has not changed since selecting that conversation. Stored text, typing
and deliberate clearing during identity/metadata waits take precedence. Historical
editing in another conversation does not disable a newly selected fork's prefill.
The one current prefill target survives view remounts, without persisting a task
or building a per-session draft map. Details follow the
[chat view contract](frontend-list-reconciliation.md#共享聊天文字与操作寿命).

Denied reads, quota failures or failed writes/removals leave editing and sending
available and show a visible save warning. When storage refuses access, recovery
and cleanup cannot be guaranteed; the in-memory text remains usable. This is a
current-tab text convenience, without attachment, closed-tab or cross-device
recovery guarantees. No server draft table or storage root is added, and
browser drafts are outside [account archives](../backup-and-migrate.md#account-export-and-import).

Unresolved archived-session restoration identities are a separate tab-local
record, not text drafts or input receipts. Unlike draft namespaces, they survive
local sign-out/reload so a late restore POST cannot be forgotten. Their sealing,
current-account adoption and retirement rules belong to
[session restoration](frontend-list-reconciliation.md#归档恢复的原操作确认).

## 原请求完成与失败交接

Owner 串行修改活动状态；慢准备、spawn、stdin、reader 和退休由受管 I/O task
携带原 request/attempt/work/backend 身份回报，Owner 不等待另一 Owner。

正常结算等待必要前缀与原资源退出；Stop 或 start 等待失败可先唯一选定业务状态，
在有限采集后定稿原引用与 terminal。关闭准入、原生终态及迟到采集规则归
[Harness](harness-layer.md#execution-and-resources)与[Codex 完成合同](codex-backend.md#root-completion-and-selected-answer)。

归档 reservation 关闭新工作接纳，已接纳的消息和 compact 仍由捕获的同一原 Owner
完成有限收尾，保存适用的 context、原生效果证据和原请求终态。提交后的内部通知
沿原 writer 引用完成，不重新申请接纳或创建替代 Owner；归档本身不撤销这一资格。
写入校验、配置适用性和存储失败边界见[上下文绑定](model-reasoning.md#上下文样本与配置绑定)。

单轮结算由原 Owner 的 `OwnerRuntime.settle(execution)` 使用原 Execution、
settlement 和发布上下文完成。Manager 不再回取 `runtime.current/frames` 解释
旧结果，只保留入口、注册、跨会话配额和故障接管。重复结算返回原选定事件；
不把旧终态重新选择给后来的请求。Database 仍拥有输入和 terminal 的短事务，
原 publisher 仍拥有有序交付；慢通知和资源退出不阻塞命令处理。

订阅注册明确区分必需业务消费者与尽力观察者。App 正文及委派问题转发为必需；
WS、应用构建进展及 Research 交付投影为观察者。受众过滤保持原账户归属。
必需回调的普通异常或自行抛出的取消进入原 publication 的失败事实，并立即停止
本次有序前缀，不能越过失败继续转发业务问题。观察者故障记录后继续，不改变
业务结果或必需交付；日志沿原 publication marker 捕获的执行／后端上下文脱敏，
不能使用后来会话的凭据解释旧异常。调用广播的原任务实际收到的新取消仍立即
传播，不能当成观察者自行取消而吞掉关闭责任。

terminal 确认提交后先保留原 outcome、seq 与 `committed`，再刷新投影并交给
publisher。后两者失败不会清掉提交事实、补选另一结果或重跑模型；Owner 失败时
Manager 接管原对象的完成与资源责任。`done` 表示已结算，可以是 `done + failed`；
重启未结算 claim 及未确认 steer 仍为 unknown，主成功不能替它们确认送达。

Owner 故障交接在首次移除完成注册前，一次捕获全部原回执 ID 为不可变 tuple，
同次交接的每个 `RequestCompletion.ended_owner_inputs` 共用它。它只表达已结束
Owner 的交接成员，不是持久执行状态或资源退出证明。App 的 unavailable 诊断可
用其中精确 ID 排除遗留 pending/running；集合外输入仍受原事务门禁，已提交
outcome／recording_state 不因附带此信息改变，也不授予 hook 或完成时间更新。
无快照的旧完成路径保留自身／已确认 carrier 关联规则。其消费者边界见
[应用构建投影](applications.md#4-lifecycle)。

| 接口事实 | 保证与限制 |
| --- | --- |
| `SubmissionReceipt.disposition` | 表达是否接纳；取消等待者不撤销已接管工作，重复 delivery 返回原 receipt |
| `RequestCompletion.input_request_id` / `request_id` | 区分原输入与携带请求；未确认发送的 steer 不能套用主请求成功 |
| `recording_state` / 业务 outcome | terminal 事务确认后保留 committed；缺少确认时返回 unavailable，不伪造成功或失败 terminal |
| `notification_state` | delivered、failed、unavailable 独立于业务结果；通知失败不抹掉已提交结果 |
| 原 execution/preparation/reader/retirement 引用 | CloseWork、归档与撤权等待真实资源；外部关闭另行排空 publisher，不能反向等待正在调用自己的 subscriber |

会话删除、应用删除与 Agent 归档共用原 Owner 的执行关闭边界。关闭前或等待命令
期间原 Owner 失败时，Manager 只接收原回调注册的 resource_cleanup，并在原期限
内 shield 等待实际退出；获取 handle 本身不构成退休。缺失、失败、超时或存储失信
均保留会话，不能继续删行或移除归属。fork/archive reservation 的冲突及释放仍沿
原 Manager 接管。已确认资源退出后，应用历史退休与后代附件物化先于数据库删除；
发布订阅者可以发起删除，原 publisher 继续由外部排空，不形成自身等待。

live 与已归档会话的删除均由 Manager 的原生命周期捕获删除前账户 audience，
先退休原 writer，再确认 DB 删除与 registry 移除，最后广播最小 `session_deleted`。
失败不生产成功通知；通知不从已删行反查归属，也不提供持久重放或 exactly-once。
发送侧的逐帧资格与删除后页面清理分别归
[账户与 WebSocket](multi-tenancy.md#websocket-delivery)和
[会话列表](frontend-list-reconciliation.md#会话列表与跨重连写入确认)。

归档和 rewind 共用原 reservation、退休期限及失败释放。Owner 退出由 Manager
接管原对象与预留；关系冲突不免除已捕获 work 的清理。资源确认后仍需 seal/join
原 publisher；不能确认退休时保留原引用并进入真实故障出口。回复正文丢失的来源处置见[委派合同](agent-collaboration.md#53-reply-delivery-the-original-request-completion)。

mailbox、控制入口及顺序 publisher 按实际内容计预算，背压到达 producer。
合法大命令可独占空控制通道；未接纳的容量等待者取消后不执行，Owner 失败释放
等待者。超时只结束等待，不丢弃原命令、句柄或数据库责任。

每个原 receipt 只有一个 completion Future；构建、App ask/SSE、委派、Research、
Wiki 与 scheduler 都消费该原请求事实。App 的成功 done 另需原正文通知前缀已送达，
响应结束不释放原 driver 槽。App build 核验 entrypoint，scheduler 核验原完成与退休，
Wiki 核验原 grant 和 applied/indexed 效果；这些领域条件由各自合同维护，不查询
或猜测最终答案字符串来代替产物证明。

回归入口为 `test_terminal_recording.py`、`test_session_owner_failure.py` 和
`test_submission_recovery.py`：真实 SQLite 覆盖提交后投影/通知失败、重复迟到
terminal 及原 Owner 接管。它们不证明生产已经部署。

## HTTP and WebSocket

`POST /api/sessions/{session_id}/submissions` and WebSocket `send_message`
share the same authenticated admission path. The HTTP request contains
`client_request_id`, `content`, optional `attachment_ids`, `wiki_mode` and
`recovery_of_client_id`. A refusal has `accepted: false`; an uncertain
acknowledgment also has `state: "unknown"` and must not be treated as proof
that the input was never accepted.

`GET /api/sessions/{session_id}/submissions/{client_id}` returns the original
input, attachment evidence and execution result. The collection GET returns
saved-input notices with a stable `before` cursor. It associates exact
terminals, filters and sorts before applying the page limit in SQL.
Committed native input changes trigger the original Owner's dispatch revision,
so this collection refreshes while a root is still active. A delayed acceptance
can replace a pending/unknown notice without waiting for another submission or
root completion; it does not resume execution or change an existing outcome.

可提前退休的关联 Stop 先按原 Owner 的首个在途命令信封核对 command ID
及内容，再沿同一控制判定执行；最终被拒绝为 command_conflict 的命令不能
先对宿主产生副作用。Stop 后首输入的迟到拒绝、EOF 等只补原事实，保留
已选 interrupted outcome 与有限采集 reader。未进入 steer writer 的取消可
证明没有 I/O；已进入 writer 则继续依原持久发送证据判断，不能猜测回队。

## 原执行的观察

原接纳回执的 `execution` 持有 `RequestObservation`：原 input request/work、
驱动完成 Future，以及按尝试捕获的 request/attempt/work/run lease 和只读资源观察。
原 OwnerRuntime 在原 Execution 的完成或故障接管中绑定这些事实；Manager 不回读
当前执行解释旧回执。重建回执没有进程内资源证明时保持 `execution=None`，不因
会话 idle、删除或对象缺失推导成功。

`driver_state` / `wait_driver()` 只观察原输入的驱动收束。Stop 将未启动输入保留
队列时，该输入仍 pending；未知 steer 即使先返回 unavailable，其观察仍跟随原
承载 driver。`attempts` 返回有原身份的资源引用；`wait_resources()` 先等原 driver
不再添加 attempt，再等这些原资源的实际退出。资源仍存活、退出失败和缺少证明
分别保持 pending、failed、unavailable；等待失败也可传播原异常。

请求驱动结束允许 Claude 宿主供 B 复用，不能据此把 A 捕获的宿主报为已退出。
观察 A 或取消其等待不调用 stop，也不停止 B；资源引用由原消费者持有至观察责任
结束。普通 App 槽只等待原 driver，保留复用政策。安装交接另按
[领域事实与封口收据](installation-lifecycle.md#领域事实与封口收据)执行，不替代指定资源证明。

资源观察共享原 Harness 的退休 Task/Event，不持有完整 Run 或复制第二份终态。
提前捕获及多个回执共享的句柄继续观察同一原任务；取消等待不取消该任务，失败
仍传播原异常。输入、history recorder 与时钟闭包只捕获原身份和必要依赖，避免
已退休回执间接保留 Execution 和大输入。请求完成或 Owner 失败不能无条件清除
recorder：Claude held result 后迟到的历史仍须沿原提交点记录输入及 fork replay
消费事实。原资源和最后协议责任结束后，保留多个回执不再线性保留这些重对象；
最后当前或 held 对象可有界保留，轻量历史身份与原接纳、完成事实仍可增长。
双后端真实协议进程的 GC、retry、steer、held、Stop、归档及 Owner 失败回归
分别验证观察寿命与原责任收束，不以 TTL/LRU 或 driver 完成替代退出证明。

## 原请求结果读取

`Database.read_session_result` 在同一已提交快照解释 input/carrier、terminal、
attempt、有序正文、成本和可识别缺失。确认的 inline 输入沿真实承载关系读取；
unknown 输入不借用主请求的成功。旧 terminal 无 attempt 时只回退同 request
已关联文本，不按时间、文字含义或“最佳答案”猜关联，也不建答案引用表。

fork 复制原 terminal request key、seq 和原生来源，不重映射历史 ID；最终引用
只在分支内校验。用户行的只读 `copied_input` 保存原 delivery/native 事实，
不复制执行提交或恢复权；原输入 unknown、已接纳未消费的事实不会因分支而消失。
截断或损坏的引用不向父会话或其他请求补正文，删除源会话不影响已复制的完整记录。

新 App Server terminal 的 `final_answer` 仅保存绑定根 thread/turn 和有序原消息
seq。Owner 只在完整根 `final_answer` item 已提交后纳入引用；commentary、child、
工具文本和其他根均不属于答案，不追加总结轮。`SessionResult` 与 `LiveReply`
共同校验原 request/attempt、根身份、阶段、消息类型、顺序和 terminal 之前的
范围。引用为空表示合法空答；坏引用返回 `answer_reference_invalid`，不借用其他
行。完整空文本 item 仍保留原记录和引用。既有无引用记录沿原关联读取，不能
把旧空值升级为新格式的完整性证明。

| 消费者 | 保留的正文及完成语义 |
| --- | --- |
| 委派完成返回 | 新原生答案使用共同正文格式；旧 terminal attempt 正文仍连续拼接后 strip。成功空正文用既有占位；坏引用走 reply_unavailable，失败走 agent-error。 |
| App 历史状态与刷新 | 新原生答案使用共同正文格式，历史消息只投影被选中的原生正文行；旧正文仍双换行拼接、不整体 strip。失败可带已保存正文；无 terminal 区分 running/queued/unknown。 |
| App 实时 ask/SSE | `LiveReply` 依原 request/attempt 接收，新 attempt/retry 清旧正文；原生进展使用 progress，根 final item 使用 message。terminal 固定共同选择，成功另需原通知前缀送达。 |

委派初始与显式 follow-up 的实际送模协议沿上述条件说明正文选择，不承诺全部
commentary 都会回传，也不改变本节的关联与校验规则。原工作可能经必要后代
返回触发多次执行，不能把首次输出结束当成整轮完成。自足回复、结果收集及
会话与轮次的区别归[委派合同](agent-collaboration.md#1-invocation-and-identity)。

实时消费保留事件流，不强制改为事后 DB 查询；重建 receipt 不虚构 delivered。
新原生正文的共同格式为忽略空片段、按双换行连接后整体 strip；完整记录仍保留
原始空白。旧格式的合法空答和正文行后来丢失不能普遍区分；可识别的 terminal、
会话或输入关联缺失仍显式保留。Claude idle 迟到文本可以保存展示，不提升为
完整尾部交付保证。

审计新执行按非空 `terminal_request_id` 及唯一性关联，不按时间或
`request.content_id` 计数；复制的历史身份不表示新执行。

## 问题结算

HTTP、WS 和父代理代答将输入及可信来源送到原 Question Owner。Owner 找原
pending/attempt，先纯校验原表单，再不让出地检查 current/到期并 claim，保存后
发布和唤醒。读视图与 Owner 共用 `PendingQuestion.is_current`，即时核对原 attempt、
宿主准入与原生问题资格；状态和过期过滤仍归读方，到期回答与关闭仍归 Owner。
每题有且仅有合法、不重复的单/多选，或非空自定义文字；缺答、非法
选项、单选多选、有效选择与非空文字并存均整份拒绝，不消费问题且不使 Owner 失败。
未用字段的空列表/空文字合法；trim 只判断文字是否为空，保留原正文，委派入口
延续已有的文字 strip 转换。父代理入口只做题目 ID/位置转换，不再持另一份校验规则。

原消息 `tool_input.question_resolution` 保存 version 1、question/request/attempt/
work、处置、来源及答案；客户端提交还保存原 `client_command_id`。认证只读入口
`GET /api/sessions/{sid}/questions/{qid}/result` 可读取已退休问题的原持久结算，
不触发到期或恢复回答权。客户端只按完整问题身份和原命令关联自己的提交；
缺记录、旧记录缺关联、认证失败或不可达均不能证明答案未被接纳。
答案沿原 `question_index`、标签与可辨明的 `option_index`
关联表单，保留多选顺序与自定义文字，不生成 provider ID。处置区分 answered、
expired、closed；显式关闭保留 `question_closed` 和 reason，系统到期指导不伪装
成表单答案。旧结构和来源缺失保持未知，content 仍是旧 MCP/展示使用的文本投影。

来源沿认证 principal 表达 account 或 agent；父代理路径另带真实委派/父会话关系。
账户认证不证明自然人亲答，HTTP URL 与客户端自报 source 也不证明来源。内部缺少
可信调用来源时保持 unknown，系统到期和内部关闭记录 system。历史和实时消息携带
同一结构，正常 Question UI 不加步骤；这是对原先宽松 HTTP/WS 非法输入的明确收紧。

回答、到期和关闭只处置一次，晚答不能消费新 attempt。claim 后保存失败走原 Owner/
存储故障边界，不发布或返回未保存的成功答案，不用撤回 claim 处理普通校验错误。
这些事实沿现有消息 JSON 保存，不新增活回调、持久问题队列或存储根。

## 原工作交互策略与等待

原输入准入在既有 `KnowledgeContext.interaction_policy` 捕获 human 或 unattended。
人工输入及其 child、BG/Research/委派/Wiki 返回继承原工作策略；新的 schedule fire
与 Wiki worker 根工作使用 unattended。相同 session 可先执行 schedule 再接纳
human，不以 `session.origin`、机器消息来源或浏览器在线状态改写已有策略。
旧 JSON 缺失字段保持未知，继续原兼容等待规则，不回填人工或无人值守身份。

human 的原生 blocking 问题不设问题期限，并暂停对应 idle 等待；Stop、原调用
resolved、执行结束、真实故障及包含等待时间的整体上限仍生效。MCP 保留默认
1800 秒、配置 0 禁用问题期限。human 原生 optional 问题由原 Owner 在建题时设定
120 秒绝对期限；页面前 60 秒不显示倒计时，后 60 秒显示剩余时间。浏览器不执行
自动回答。旧未知策略保留兼容期限。

用户在表单内主动操作可发送原 `SnoozeQuestion`；原 Owner 先裁定是否已经到期，
成功时清除有效期限及 timer，并将原题加入 idle 等待暂停范围。该操作幂等，
不换题、不延长整体上限，也不屏蔽 Stop、resolved 或真实故障。原记录以有界
独立暂停通知槽保留交付责任，持久 `question_snoozed` 事件的 seq 保护较新投影
不被旧 snapshot 覆盖。Web/App、两种 session detail、实时事件及历史共用原问题
的策略、wait_mode 和暂停投影。

unattended 不创建长期人工表单：根问题立即以 `needs_input` 关闭，保存原问题及
缺失决定，不生成用户选择或授权。子工作可先将题目交给真实父关系；一次实际
父准入受忙碌、容量和生命周期冲突约束，不等待恢复锁或无限重试。实际接纳后
由原父 receipt 的完成唤醒 child Owner；父未答就关闭原问题，Owner 内不等待
父执行、MCP runner 或 REST waiter。独立已授权步骤可保留，依赖决定的步骤停止；
父会话已暂停时拒绝新的无人值守求解输入；接纳后 Stop 则由父 Owner 将尚未执行
的这类原输入结算为 expired，原 receipt 随即结束 child 的求解机会。普通输入
队列仍保留暂停，Resume 不重放已过期的求解输入。
用户补充通过新输入续接，不重开旧题或重放原工作。各领域仍以自身效果 receipt
裁定结果，完整父子交接归[委派合同](agent-collaboration.md#unattended-questions)。

<a id="mcp-question-retirement"></a>

## Independent returns and one-shot schedules

BG, Research and delegation return as independent later inputs under the
[complete return operation](#公共返回接管). Source completion does not prove parent
processing. Shutdown ends live delivery observations while preserving source
results and input facts; restart does not replay uncertain returns. Native idle
output only appends to its original conversation and cannot reopen a receipt.

A one-shot's target session, input and schedule consumption commit together.
Human backlog does not consume an unaccepted fire. Startup catches up an
overdue, unconsumed one-shot; a transient refusal retries the same source.
An accepted fire is not recreated after restart, including when its execution
is unknown. Confirming the original fire returns its original session and
receipt, including after automatic archive. Recurring schedules retain their
existing overlap policy.

## 公共返回接管

[`SessionManager.admit_return`](../../server/sessions/turns.py) 拥有 BG、Research、
委派终态和问题的一次完整返回接管。来源提供原身份和按需正文准备函数，不再自行
组合回执查询、会话唤醒和输入提交。采用回调是为了让材料读取仍归来源，同时把
调用顺序交给会话模块；拆成两个公开阶段会把顺序责任留给调用方，正文引用联合
类型则会让会话模块认识各来源的存储方式。

### 身份、材料与责任

[`ReturnTarget`、`ReturnBody` 和结果类型](../../server/sessions/submission.py)
只在本次调用内使用，不新增持久事实。`ReturnTarget` 由可信来源按原记录构造，
不能直接承接外部请求字段；目标 session、work 和捕获的 owned binding 必须相符。
缺失 binding 仍可确认已有 receipt，但不能创建新输入。delivery 身份保持
`bg:{task_id}`、`research:{job_id}`、`agent-terminal:{work_id}` 和
`agent-question:{work_id}:{question_id}`。

`prepare` 每次调用至多一次；已有回执或确定不能新接管时可以不调用。它只读取、
构造 `ReturnBody`，或用 `ReturnSkipped(reason)` 表达来源不再需要新投递。空正文
不表示跳过。问题关联的 child session/question 字段必须成对提供，Owner 在实际
执行前再检查问题有效性。准备不得结算来源、启动执行、发送通知或修改授权；
并发调用和容量重试可以重复纯准备，不为准备绝对一次增加锁表或账本。

| 责任 | 所有者与提交事实 |
| --- | --- |
| 原回执、资格、准备顺序、合法唤醒及交接 | 会话模块的公共返回操作 |
| 正文、是否仍需返回、重试节奏与放弃、业务结果 | BG / Research / 委派各来源 |
| 归档成员关系与暂停恢复 | 原 lifecycle；合法恢复保留 Stop，不重放已取消输入 |
| 输入接管、digest 冲突与去重 | 原 SessionOwner 和输入事务；完整输入与接纳事实提交后确认 |
| 父处理、结果、通知与资源退休 | 原 `SubmissionReceipt` 的 completion 及各自原 owner；不新增完成 Future |

### 顺序与失败边界

先查原 session/source/delivery 的 receipt。已确认接管优先于材料删除、Owner
失败、来源或当前准入关闭；快路径不准备、不唤醒、不重开输入。已经撤销的输入
也只观察原回执，不能重新入队。

没有回执才检查原归属与当前资格。准备前的检查不授予跨 `await` 的许可；准备
期间不持 mailbox、生命周期锁或事务，也不预留执行资源。准备完成后先再查原
receipt，覆盖另一调用已经接纳的情况，再处理来源跳过或重验资格、恢复原会话。
恢复可能等待原生命周期操作；等待结束后再次校验资格，最后由原 Owner 和输入
事务裁定。归档期间的合法返回等待归档结束，不能提前把瞬态 closed 当成永久拒绝。
session 的 reservation、fork 和 closed 由原生命周期锁内及实际 Owner 判断。

唤醒与输入提交仍是两个提交点；本次没有新输入不证明此前没有合法唤醒，接纳
拒绝不自动回滚归档成员关系。并发唯一性与实际提交的 digest 冲突仍由原事务及
Owner 保证，不新增每 delivery 的锁、队列或注册表。普通重复观察无需生成正文；
实际提交的 `SubmissionConflict` 原样传播，不能补查回执后把冲突当成 duplicate。

| 返回或异常 | 调用者合同 |
| --- | --- |
| `SubmissionReceipt` | 已确认接纳的原输入；不证明执行成功，继续观察原 completion |
| `ReturnRefused(reason)` | 本次没有新接纳；原因限于 `source_withdrawn`、`binding_unavailable`、`destination_unavailable`、`admission_closed`、`capacity`、`busy` |
| `ReturnUnconfirmed(cause)` | 原回执暂时无法可靠观察，或提交交接后未获确认；保留原身份与原因，不推断没有接纳 |
| 准备错误、实际正文冲突、未分类错误或取消 | 保留并传播原错误；取消等待不撤销已经交给 Owner 的 Submit |
| 存储失信 / `ServiceUnavailable` | 沿原 fail-stop，不在同代补查猜测，也不转为普通 unknown 重试 |

分类取决于阶段：回执读取的临时容量或期限失败是无法确认，提交处已知的
`SessionMailboxFull` / quota 拒绝才是容量拒绝。归档/fork busy 与生命周期关闭
使用其明确业务分类，不匹配错误文案、不把任意 `ValueError` 当拒绝。拒绝、来源
跳过及可确认的交接异常出口，在存储可信时核实原 receipt；当前没查到不证明另一个
在途提交不会成功，也不构成新接管许可。其他未分类异常不被转换为成功或普通拒绝。

丢确认后公共入口核实一次原回执。来源收到未确认时先记住原诊断，再完成自己的
必要写入并结束本次观察，不在外部重复核实。仅既有结算/恢复重入可观察原身份；
unknown 的准备函数只返回跳过，后来的拒绝不能覆盖原原因。只有无 unknown 前史
的明确容量或 busy 拒绝可按来源策略重试。用户可持续看到“返回未确认”；停止观察保留原材料与业务结果，不更换
delivery、不重新执行来源；接管未确认也不等于 `RequestCompletion` 的执行结果未知。

### 来源接入与验证

BG 的 `deliver_bg_result` 转换原身份、材料及辅助说明，manager 保有命令结果、
诊断与业务重试。Research 正常注入使用公共操作，启动恢复只读原持久接纳与完成，
不唤醒或重新接纳；恢复分类归[Research 合同](native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler)。报告、
queued/processing/processed、浏览器收讫及父处理仍分别由其原 owner 维护。
委派 `_inject_terminal` / `_inject_question` 保留原轮次和问题的重入保护、round
CAS / child notice 诊断写入及原 completion 观察，只消费公共接管的三类结果。
其精确正文、问题关闭及材料删除语义见[委派合同](agent-collaboration.md#53-reply-delivery-the-original-request-completion)。

来源不再组合 `source_receipt`、恢复、`start_message` 或读取准入私有字段来解释
一次接管；生命周期恢复只保留会话模块内部 `_restore_task_return`。Wiki/schedule
仍可使用 `source_receipt` 和通用 `start_message`；Wiki outbox、schedule 首次
触发、App、人工输入保持各自入口。本操作不引入通用任务框架、执行表、持久队列、
恢复机制或 provider 迁移，现有输入、导出及恢复表示不变。独立返回按成功接纳
顺序逐条派发；来源完成时间不决定队列位置。Stop 保留暂停 backlog，人工纠偏
不隐式 Resume，显式 Resume 后继续，来源 work 不并入当前请求的原生尾部。

回归通过真实隔离 SQLite/Owner 和可控本地后端观察原输入、receipt、启动与来源
记录，不替换最终接纳事务：`test_return_admission.py` 覆盖原回执、并发纯准备、
跳过、错误分类、冲突、取消与失信；`test_bg_session_return.py` 和
`test_research_session_return.py` 保留各自实际返回、资格与暂停覆盖。
`test_return_admission_lifecycle.py` 验证真实归档等待及单次委派接纳；
`test_delegation_return_admission.py` 验证准备前不唤醒、准备/恢复期间 work 或问题
关闭，以及 unknown 后材料丢失、Owner 失败仍得到原输入。
`test_research_return_admission.py` 覆盖同样的报告材料丢失场景；委派重试、诊断
失败重入和删除材料的边界由 `test_delegation_return_retry.py`、
`test_delegation_text_deletion.py` 保持。

## Saved inputs

New durable payloads retain an explicit `message | compact` under `operation`.
Messages contain submitted content, attachment IDs and Wiki mode; compact has
only its kind and separately captures the original native history. The original
MessageOperation is distinct from the effective execution KnowledgeContext.
Pending items, admission/status, dispatch, outcomes and completion readers carry
the operation kind. App answers, managed
work and notifications consume message completions under their existing scope.
Compact completes its own original receipt; ordinary answer publication and fork
replay installation apply only to message operations. It creates no final
answer, assistant summary or replay instruction.
An explicit malformed/unknown operation is rejected rather than decoded as text.

Legacy flat messages normalize only when read from retained-input storage;
their rows and original three-field input digest are unchanged. Existing IDs
therefore retain their deduplication and recovery relationships. Execution reads
the durable operation, not a second queued copy of its body. Public ordinary
message submission fields keep their existing HTTP shape.

Review shows the saved text, attachment metadata and the reason an outcome
is unknown. Restore only fills the composer. The user's send creates a new
input whose single `recovery_of` link identifies its source; source eligibility
and the unique direct successor are checked in the admission transaction.
Legacy forward links are read during migration and have no runtime writer.
An unknown input has no one-click restore-and-run action.
Accepted native steer keeps its original input and acceptance evidence without
a success notice. Separate execution or delivery uncertainty remains reviewable,
with no restore-and-run action. An old unsent/rejected steer that subsequently
executes as an ordinary request does not leave a stale notice.

Saved inputs **Dismiss notice** commits only a persistent visibility edit,
then best-effort notifies the original Owner. An unavailable or retired Owner
does not veto the edit or get recreated. **Stop** interrupts the active request
and pauses dispatch; **Resume processing** explicitly resumes the backlog.
An unstarted unattended question-solving return instead expires on Stop and
completes its original receipt, ending that child's bounded opportunity; it
cannot run on Resume. Ordinary inputs retain the paused-backlog contract.
See [unattended questions](agent-collaboration.md#unattended-questions).
Stop and question-answer confirmations clear their pending frontend notice on
success. Failed or unconfirmed commands remain visible; their **Dismiss** only
hides that frontend prompt. Question answers remain in the conversation history.

## Native compaction requests

Codex App Server and Claude stream-JSON admit compact for resumable native
history, both idle and busy. The original Owner applies ownership, readiness,
reservations, capacity, quota and retired-writer gates, then execution reloads
its durable payload. Unsupported runtimes/environments and absent history are
refused. No prompt, attachments, Wiki task, history replay or continuation/retry
is manufactured.

The existing pending queue retains its ordinary order. Its sole selection point
first applies paused/priority eligibility, then chooses an eligible compact ahead
of ordinary inputs. Current work, its original human question and its owned
resources finish before native compact starts; independent BG/research/delegation
jobs are not a barrier. New inputs after compact admission queue rather than
steer. Previously admitted steer keeps its delivery eligibility; one proven
unaccepted and returned to the ordinary queue follows compact in its original
order. Automatic compaction updates observations without completing a pending
manual request. Compact does not implicitly Resume paused backlog or take an
extra execution slot while pending.

HTTP and WS accept `operation: {kind: "compact"}` with `client_request_id`.
Same ID/payload returns the original fact; different content/kind conflicts.
A new ID during an active compact is refused with `compact_in_progress` and
canonical request/client IDs, without an alias row or false acceptance. Lost
receipts permit GET confirmation of the original ID, never automatic POST/resend.
Each HTTP observation, including its response body, has a 30-second limit. POST
expiry only permits the original-ID GET; GET expiry retains the identity and
draft for confirmation. Leaving the view cancels observation and releases its
local request slot, without stopping the admitted native operation. Account
changes discard transient notices rather than transferring them to a new owner.

Claim commits before business I/O. Codex additionally persists possible-send,
ACK and exact root proof as independent facts; none means completion. Committed
root evidence can end startup without ACK, so the RPC timeout does not interrupt
an already proven execution. Claude uses the original pipe write and command
identity, without manufacturing RPC evidence. Original turn deadlines, finite
collection and Run retirement apply.
The terminal's native admission-close time is checked against overall/idle
budgets even after Owner storage waits; expiry preserves its original timeout
reason without granting compact a message continuation or retry.

Maintenance outcomes have no final answer. `compact.effect` is applied,
not_applied or unknown independently of success, failed or interrupted. Stop can
retain interrupted + applied; unknown effects invalidate the old context sample.
A reliable post sample installed with its boundary stays valid. Success without
effect evidence is unconfirmed; no-op needs affirmative native evidence.

Stop withdraws pending/dequeued-unstarted compact; Resume cannot revive it.
Ordinary messages during compact queue instead of steering. Startup withdraws
pending/proven-unsent work, preserves unknown or terminal facts and never replays.
Admission and the recovery transaction reject compact as a `recovery_of` source.
Saved maintenance records remain read-only.

`cancel_compact` uses the existing correlated WS command/receipt path with its
client command ID, session, request and service generation. It removes only the
matching pending compact. After claim it returns `already_started` and directs
the user to Stop; neither mailbox admission nor the handler retires the current
run. A stale, foreign or wrong-operation target cannot cancel another input.
Queue UI reads the authoritative dispatch snapshot, retaining the item until a
server replacement arrives. Refresh and other tabs observe the same claim;
local cancellation progress is not another queue authority.

The composer recognizes exact argument-free `/compact`, allowing surrounding
whitespace. Arguments show usage; prose mentions stay normal messages. Confirmed
acceptance clears only matching original account/session/draft command text;
attachments, uploads and later drafts remain. Status uses existing dispatch,
result and saved-input surfaces, without a separate retry workflow.
Claude's native failure reason and command outcome remain visible in live and
historical results, including EOF/Stop and an applied effect. An unassociated
terminal supplies a separate diagnostic and cannot change native history.

<a id="codex-native-input-evidence"></a>
## Codex 首输入证据与准备边界

普通 Codex App Server 的首输入和人工 steer 继续使用原 `client_submissions`，不另建投递或结果账本。
Owner 在标记请求 started、启动异步 claim/附件/凭据/配置准备之前，以短事务记录
初始 `not_sent` 并保留首 attempt 身份；真正执行沿用该身份。pending 行只允许初始
未发送证据，不能写发送事实；已证明未接纳的 steer 回队后，原输入可记录后续
普通执行的新 attempt。准备失败与早期 Stop 由此保留恢复
入口；driver 的直接 Harness 调用仍自行初始化同类事实。

| 原生证据 | 含义与后续行为 |
| --- | --- |
| `not_sent` | 未记录业务帧可能发送；spawn、握手、只读查询不算首输入执行。controller 修改的不确定性独立保留。 |
| `unknown` | 原 submission 已先提交可能发送边界及 RPC 身份，随后才能写业务帧。写前存储失败不发送，写后 ACK 丢失不自动重放。 |
| `accepted` | 原 start/steer 响应或匹配的完整 user-message client ID 证明接纳并关联原根 turn；不证明模型消费。 |
| `rejected` | 原生协议明确未接纳。当前 Owner 的未接纳 steer 可按原身份和顺序回普通队列；首输入或迟到拒绝按原恢复合同处理。通用内部错误不证明拒绝。 |

产品 input、attempt、RPC、native thread 与 turn 身份分别解释；细化不能覆盖已知
身份或倒退证据。thread 与输入证据一起提交，后续 writer 在原资源退休后读取
持久事实，不依赖一次实时事件被消费。driver 报告协议事实，原 Owner 唯一结算。
steer 另保留 `kind`、连接 generation 与预期 target turn；接受证明的 turn 必须与
该 target 相同。连接代次与原生身份共同隔离编号复用，原输入不能迁往下一轮。
写前 marker 与最终 target/lease 校验分别拥有持久不确定性和实际写入资格；原事务
仅在没有 marker 或证据均为 `not_sent/rejected` 时允许当前 steer 回队。

terminal 事务与查询保留未澄清的 unknown；成功根 terminal 不能替输入确认。
Stop 后仅允许细化原来已可能发送的输入的接受/拒绝证明，不能登记新 attempt、
重新发送或改写已提交的 interrupted outcome。已观察的接受/拒绝记录复用现有
取消保护和数据库总时限，不能因 reader 退休取消而丢失，亦不阻断进程取消启动。
除当前 Owner 尚未交付的已证明未接纳 steer 按原序回队外，明确未发送/拒绝者
进入既有恢复列表，用户检查原文和附件后，以带 `recovery_of` 的新身份明确提交。重启按已有
证据区分未发送与未知；旧行的空证据不回填为原生接纳或模型消费。重复浏览器
身份仍返回原回执，界面分别说明原生确认未知与准备变更未知。
原生证据提交只唤醒原 Owner 的合并通知信号；Owner 推进现有 dispatch revision，
使活动中的 Saved inputs 随接受证明刷新。已接收的 steer 保留原输入与接受证据，
不单独显示成功提示；独立的执行或送达未知仍保留检查入口，没有自动恢复执行按钮。
接受证明不提升为模型消费保证。回队后已作为普通请求执行的
输入不因旧 steer 证据留下错误通知。资格、顺序和容量见[steer 合同](inline-steering.md)。

## 实际输入来源与成本边界

输入分析沿原请求身份、生产路径、阶段和通道归属：已保存的可信原约束、当前
子任务、附件／fork 历史与异步返回材料分别解释。同文的不同请求仍有各自身份；
子工作需要的祖先约束不能用消费者不可读的宿主引用替代，返回材料也不增加授权。

逐层限制与派发消息各保留全文：前者属于跨层执行及返回合同，后者是独立保存的
消息正文。限制列表的位置不表示本次任务身份；返回回合也不能把上一轮末项当作
当前材料中的重复。精简须证明具体限制与本次正文同源，并保持保存、回放及两种
后端消费者的可读性。只有字节差异而没有这些关系及质量证据时，保留原展开。

Codex 的稳定 controller 参数、历史 checkpoint、动态 turn policy 与实际
`turn/start` 是不同阶段。resume 参数重复不证明模型历史重复消费；当前来源清空
及 human／unattended 策略由本次上下文解释。成本分别记录各实际 UTF-8 RPC 帧，
不将多个 RPC 相加为一帧，也不把接受、成功或历史安装证据当作语义质量结论。
工具成本取最终 CallToolResult 与 JSON-RPC／SSE 交付边界，并分别标明包络；
中间 shim 返回全文不证明最终交付或阅读。方法与复测条件见
[可选输入测量](../testing.md#optional-resource-experiments)。

# 输入接纳与显式恢复

> **Status:** implemented — 持久输入接纳、原生送达证据、结果关联与显式恢复边界。

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

On restart, unclaimed inputs become withdrawn and claimed inputs become
unknown. Missing legacy payloads and completed legacy rows without an exact
terminal remain review-only. Historical refusal receipts stay read-only.
Managed startup retirement and current-generation retirement keep their
existing authority; hiding a saved input proves neither execution exit nor
permission to dispatch.

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

## Independent returns and one-shot schedules

Background tasks, Research and named delegation rounds keep their original
work and delivery identities. They submit independent results as later
inputs; an acceptance acknowledgment follows the input commit. Resending a
lost acknowledgment observes the same input. The original request may finish
first, and a paused backlog remains paused. Native idle output only appends
to its original conversation; it does not reopen a completed receipt.
The original source task retains its delivery observation under that identity.
The shared return operation checks the original receipt after a lost admission
acknowledgment; an unconfirmed result remains unknown rather than authorizing
another submission. Only a definite pre-effect busy/capacity refusal with no
unknown history permits source-policy retry under the same identity. Retry
does not repeat the command, research pipeline or delegation round. Shutdown
ends these live delivery owners; retained source results and durable input
facts remain available for review, without replaying uncertain work.
If the destination Owner permanently fails, sources first confirm whether the
input was already accepted. An accepted input keeps its original receipt.
An unaccepted permanent return refusal ends delivery and keeps the original result: background
output stays in its task record, Research reports failed delivery, and a
delegation exposes `terminal_disposition: "unavailable"` while its completed
reply remains in the child conversation. A capacity refusal continues under
the same delivery identity.

A one-shot's target session, input and schedule consumption commit together.
Human backlog does not consume an unaccepted fire. Startup catches up an
overdue, unconsumed one-shot; a transient refusal retries the same source.
An accepted fire is not recreated after restart, including when its execution
is unknown. Confirming the original fire returns its original session and
receipt, including after automatic archive. Recurring schedules retain their
existing overlap policy.

## Saved inputs

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
Stop and question-answer confirmations clear their pending frontend notice on
success. Failed or unconfirmed commands remain visible; their **Dismiss** only
hides that frontend prompt. Question answers remain in the conversation history.

<a id="codex-native-input-evidence"></a>
### Codex 首输入证据与准备边界

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
| `rejected` | 锁版协议明确未接纳。当前 Owner 的未接纳 steer 可按原身份和顺序回普通队列；首输入或迟到拒绝按原恢复合同处理。通用内部错误不证明拒绝。 |

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

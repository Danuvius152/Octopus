# SessionOwner 交付中的责任范围与验证遗漏

状态：历史复盘。整合至 2026-11-05 的已知修复与验证经验；不代表当前部署已验收。

## 摘要

10 月 21 日，一次原本只写“命令和 I/O 回报进入一个 owner”的交付，实际扩到了
完成回执、通知送达、资源退役和多个消费者。原复盘记录的候选验收覆盖 115 个文件，
1907 passed / 23 deselected，仍有测试没有触发目标故障。随后的 App、Schedule、
Connector、Research 和问题交互修复，又暴露出同一种遗漏：
**已有 owner，不代表它已经负责到消费者需要的提交、交接或清理终点。**

主要工程问题是交付切在实现入口，共同语义尚未用真实消费者验证就批量迁移，
测试又用邻近状态代替目标事实。单一写者解决并行裁定，完整责任还需要原操作身份、
明确的提交点及真实消费者。绿测试的价值取决于它是否制造了目标交错，并在正确的
边界观察结果。

## 范围与影响

本文以 `0a56386315548b6e9df6251166220749fc0ce4aa` 为整理基线，保留 10 月 21 日
交付及截至 11 月 5 日的相关修改证据。依据包括历史差异、所属合同和回归源码；
原候选的通过数只属于当时的记录，不是本轮重新运行结果。

没有取得生产数据损坏或停机的事故证据。可以确认的工程代价，是票面吸收多个相邻
责任、消费者迁移反复、评审和回归成本增加，以及局部绿色造成的错误信心。本文
没有重跑产品套件、真实模型或发布验收，也不把所有连续修复归为同一个 bug 被改坏。

## 责任范围怎样逐次扩大

原目标是让会话命令、I/O 回报和生命周期进入 `SessionOwner`，统一修改活动执行、
问题、取消、归档和派发状态。票面只描述了实现入口，没有列全下游需要的事实：

| 阶段 | 实际暴露的责任 | 原先不足的解释 |
| --- | --- | --- |
| 完成接口与消费者迁移 | 输入接纳、业务结果提交、通知送达和资源退休分别取证；App/SSE、delegation、WS/UI、archive 消费同一原身份。 | “命令被 owner 接住”不能同时代表业务结束、用户已见和旧资源已退出。 |
| App 生命周期与请求 | 原实例从读 body、响应消费到 client/response 清理及后续转交，始终可能被使用。 | backend 正在运行、handler task 已返回或 HTTPX 已标 closed，都不足以判断实例空闲。 |
| 控制操作与持久投影 | Schedule 保存后注册 trigger，Research 取消提交后通知原 pipeline，Connector 保存收到的轮转结果。 | 原执行已有 owner 或已有一把锁，不会自动保有新增控制操作的寿命。 |
| 问题调用与页面 | 原 MCP 建题、关闭及 REST waiter 清理必须收束；页面保管原答案草稿、提交身份和提交快照。 | SDK 回复取消、表单卸载或某次 HTTP 返回，不证明原调用退出，也不授权清除后来编辑。 |
| 原生完成及组合消费者 | provider 终态、必要效果、记录完整性和领域产物分别裁定。 | 模型 completed 不能消除未知效果；局部缺项也不能无条件覆盖一个已有合法结果。 |

这条修改链解释的是相邻保证怎样逐次补齐。它不支持把 SessionOwner 推倒重建，
也不说明所有异步工作都应进入一个通用工作流管理器。

## 具体反例与责任交接

### 1. 原实例的使用寿命超过 handler 的寿命

App 请求可能还在读取慢 body，也可能已经返回 StreamingResponse，但 ASGI 尚未
开始消费。只枚举 handler task 会把仍在使用的 backend 当成空闲；等待后重新按
port 查对象，还可能把原请求发给同端口的替代进程。

这条修复链从 `7449f12` 的 reader／进程组退休，逐步补到 `bbadc28` 将 STARTING
和未确认退休对象计入容量，`b8e0f32` 删除前持久记录 retiring，`fd7ab9c` 捕获原
port/process，再到 `07b83bf` 用 BackendUse 覆盖读 body、response/client 清理和
202 后续消费者。停止失败保留目录；响应关闭任务也由原对象保有，因为 is_closed
可能先于实际 stream cleanup 完成。

[固定基线的 BackendUse][src-use]与[response 清理][src-close]让 LRU、idle、delete
和 drain 消费同一份使用责任。[请求寿命回归](../../tests/test_app_request_lifetime.py)
观察慢 body、未迭代响应、header 失败及 close 失败，而不是只看 handler 是否结束。

### 2. 原执行有 owner，新增控制仍可能失去提交后的动作

Schedule 的 PATCH 已提交 SQL，HTTP／MCP waiter 被取消，内存 trigger 却还没注册。
Research 取消事务已提交，原 pipeline 尚未收到信号。Connector 已收到轮转 token，
调用者却在保存前退出。三个场景都已经有执行 owner 或锁，但它们没有覆盖这次
控制／保存交接的完整寿命。

`4f1418d` 为 Schedule 和 Research 的控制操作保责：前者在首次 DB 等待前登记并
shield，按当前持久定义重建 trigger，shutdown 先 join 控制；后者保有取消提交和
原 pipeline 信号，leaf 退休仍归原流水线。`324a913` 则由 Connector manager 保有
原 refresh 到结果保存和退休。这里共享的是责任判断，各领域仍拥有自己的效果。

Schedule 定义保存、trigger 注册和某次 fire 接纳有不同提交点。注册失败保留原
schedule ID，重建该身份的投影；删除未来 trigger 不取消已接纳的 fire，不能诱导
调用者再 create 一次。证据见[控制实现][src-schedule]、
[真实 DB 回归](../../tests/test_schedules.py)和[shutdown 交错](../../tests/test_scheduler.py)。
完整范围由[计划合同](../implemented/scheduled-runs.md)持有，per-fire history、
停机 catch-up 等独立产品选择不能仅因未实现就算成本次故障。

Connector 的失败分类、原授权和定义替换见[OAuth 复盘](oauth-repeated-boundary-bugs.md)；
Research 的取消交接见[Research 复盘](research-completion-boundaries.md#4-取消事务成功后停止原执行仍有独立责任)。

### 3. 原问题调用与真实消费者

MCP SDK 已回复取消，原建题回执或 REST waiter 仍可能没有退出。原 Owner 必须保有
该调用：准确建题结果被观察并结算，runner 在 Owner 外完成原问题关闭和 waiter
清理，Owner 再消费实际完成的调用。未知原生关联不能成为提前释放执行或 App
槽位的理由；取消标记和唤醒事件也不是清理完成证明。

页面问题卡有另一种寿命。卸载后原答案或续答回执仍可能到达，草稿因而保管原问题、
提交身份及提交快照。未知回执继续核对原提交；确认接纳也只清除当时提交的快照，
不能清空用户后来编辑的文字。丢弃续答预览后，普通发送仍须可用。

完成分类同时区分“模型完成”和“必要效果已确认”。已有有效根终态时，不再套用
缺终态解释；只有精确关联、已取消并完成本地清理的纯 ask 输入缺项，才可以适用
窄成功裁定。原始缺项仍保留，本地清理不证明 provider 消费；其他效果缺项、旧
attempt 故障和领域产物门槛继续有效。输出引用无效，也不能在界面显示“已保存”。

[原调用与组合消费者回归](../../tests/test_mcp_input_completion.py)把 mounted MCP
取消、受控 App Server、Owner/SQLite，以及 App、委派、Research、Wiki、Schedule
放在一条证据链中。委派完成后的查询通知曾仍早于返回接纳，验收改为等待原持久
回执，业务完成没有被提升为通知完成。页面证据见
[问题生命周期](../../web/src/components/QuestionLifecycle.test.tsx)与
[结果展示](../../web/src/components/MessageBubble.test.tsx)。

App 的有限响应等待同样只结束观察；调用者通过原 request 查询结果，不能重新提交
一次工作。[响应恢复回归](../../tests/test_app_response_recovery.py)分别覆盖等待
结束、原结果已提交和观察失败的窗口。

## 根因

### 交付按实现入口切分，没有列全责任与消费者

“命令进入 owner”没有回答：哪些事实由谁提交、消费者按哪个原身份读取、失败或
未知效果由谁继续负责，以及旧路径何时退出。schema、API、UI 和测试都改过，
也不意味着这些责任已经闭合。新事实被当作实现中自然发现的细节，越晚暴露，
就越容易让同一张票吸收完成接口、通知、退役和所有消费者。

### 共同接口尚未被真实需求验证，迁移已经展开

共同接口需要明确事实、提交点、迟到事件和异常出口。尚未用能暴露不同需求的
消费者验证，就批量迁移，消费者便会在迁移中不断重新定义接口。正确顺序是先以
真实调用者验证共同语义，再迁移其余路径；不能把“接口先冻结”理解成预建一个
没有调用者的抽象，或拒绝消费者提供新反例。

BG、Research、Delegation 可以共用[完整返回接管](../implemented/input-reliability.md#公共返回接管)，
正文准备、业务结果和重试节奏仍归来源。命名委派的工作轮、返回输入与实际 carrier
如何关联，见[三个完成点复盘](session-delegation-completion-boundaries.md)。

### 用状态、等待者和局部绿色代替了目标事实

进程还在不等于有人使用，task 返回不等于清理已结束，waiter 消失不等于效果撤销，
parent success 不等于原返回被处理。原测试中的几种构造又避开了真实故障：

| 旧构造或判定 | 实际没有证明的事实 | 应注入并等待什么 |
| --- | --- | --- |
| clean close 后读取恢复数据 | commit 后、通知前崩溃时的恢复责任。 | 在目标提交窗口制造中断，核对原持久事实和恢复结果。 |
| 构造 argv 的回调次数 | OS spawn、进程组建立及后代实际退休。 | 触发真实启动／失败／迟到窗口，观察原进程和资源退出。 |
| 业务完成后断言 child 仍在 live map | 业务结束与资源退休的相对阶段。 | 分别等待原 completion 和退休证明，不依赖临时登记时机。 |
| 队列变化、通知、卡片更新 | 输入接纳、业务提交或消费者处理。 | 等待精确原 receipt、持久结果和所需的送达证据。 |
| SDK 已取消、HTTP 已返回 | 原建题、runner 和 REST waiter 的完整清理。 | 阻塞原调用清理，确认 execution/App 容量在 Owner 消费前不释放。 |
| 表单卸载或旧提交接纳 | 新编辑仍可用、回执只清原快照。 | 卸载／重挂载期间完成原请求，再检查原身份、草稿和普通发送。 |

这些反例说明旧测试的构造或观察点不足，不能据此推断每个旧版本全套测试都曾跑绿。
完成审计和独立审查若只看通过数，也无法识别这些遗漏。

## 可迁移的开发经验

1. **先声明行为和完成条件。** 对所有权、并发、生命周期、授权、持久化或外部协议
   的改动，简要列出原身份、事实、写者、提交点、消费者和失败范围。普通局部修改
   按实际影响处理，不强制建计划、切票或写一份完整状态机。
2. **让原操作保有到真实交接或退出。** 分开判断接纳、业务结果、通知、处理与退休；
   取消等待者不撤销已发生的效果。新增控制与消费者必须有责任归属，已有锁或 owner
   的名字不能充当证明。
3. **发现反例先修正解释。** 新 writer 或新提交点可能第一次就推翻模型；两次命名
   调整却不需要重开设计。暂停的是继续堆状态、重试和 fallback，不是机械按变化
   次数重切票，也不自动要求再向用户确认或扩大需求。
4. **共同语义用真实消费者验证。** 保留来源差异，迁移时删除被替换的并行路径；
   切分以可独立验证的完整行为为单位，不按 schema/API/UI 各碰一层来宣称完整。
5. **验收写出可推翻旧设计的交错。** 明确注入点、目标观察和旧代码应失败的理由。
   按受影响行为选择必要检查，复用仍有效证据，不因某位审查者结束就重复全套测试。
6. **完成审计与独立审查核对责任。** 对照授权范围，确认实际交付、消费者迁移、
   旧路径退出和未决事实。状态标签须与正文一致，不能用 done 或绿色替代这些证据。

## 适用边界与关联

这些经验适用于本次已证明的交接问题，不承诺整个仓库无缺陷，也不把历史验收
扩成今天的部署结论。原问题、原生完成与领域效果的完整合同仍由所属文档维护：

- [开发范围与完成条件](../../AGENTS.md#scope-and-completion)、[测试政策](../testing.md)。
- [输入完成与资源退休](../implemented/input-reliability.md#原请求完成与失败交接)、
  [MCP 原调用退休](../implemented/harness-layer.md#mcp-question-retirement)。
- [App 后端合同](../implemented/application-backends.md)、
  [计划合同](../implemented/scheduled-runs.md)。
- [OAuth 责任边界](oauth-repeated-boundary-bugs.md)、
  [Research 完成与交接](research-completion-boundaries.md)。

[src-use]: https://github.com/Danuvius152/Octopus/blob/0a56386315548b6e9df6251166220749fc0ce4aa/server/app_backends.py#L130-L219
[src-close]: https://github.com/Danuvius152/Octopus/blob/0a56386315548b6e9df6251166220749fc0ce4aa/server/app_proxy.py#L14-L51
[src-schedule]: https://github.com/Danuvius152/Octopus/blob/0a56386315548b6e9df6251166220749fc0ce4aa/server/scheduler.py#L358-L415

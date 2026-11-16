# SessionOwner 接住命令后，责任仍在消费者处中断

状态：下列缺口已有修复。本文回看 2026-10-21 的交付及截至 11 月 5 日的相关修改，
历史整理基线为 `0a56386315548b6e9df6251166220749fc0ce4aa`。

## 摘要

原目标是让会话命令和 I/O 回报进入一个 owner，交付却逐步吸收完成回执、通知、
资源退役和多个消费者。共同接口尚未用真实消费者验证，迁移已展开；后续又把
handler 返回、状态成功或 SDK 取消，当成完整操作已经结束。

单一写者解决了并行裁定，没有自动定义下游需要什么事实。修复保留 SessionOwner
设计，补齐原操作身份、实际提交点和消费者交接，没有把全部异步工作迁入一个
通用工作流引擎。

## 影响与证据

原候选验收覆盖 115 个文件，记录为 1907 passed / 23 deselected，仍有测试没有
触发目标故障。可确认的工程代价是范围扩大、消费者迁移与评审反复，以及局部绿色
带来的错误信心；没有取得生产数据损坏或停机证据。

依据是历史差异、所属合同和回归源码。本文中的通过数只描述当次记录，本次文档
改写没有重跑产品套件、模型或部署验收。

## 关键时间线

| 节点 | 暴露的责任缺口 |
| --- | --- |
| 10-21 原交付 | 命令进入 owner 后，接纳、业务提交、通知和退休仍需分别定义。 |
| `bbadc28`、`b8e0f32`、`fd7ab9c` → `07b83bf` | App 容量、删除与原进程捕获逐步补齐，handler 之外的请求使用寿命仍需保有。 |
| `324a913`、`4f1418d` | Connector 轮转保存、Schedule trigger 注册和 Research 取消信号各需控制操作 owner。 |
| 问题与组合消费者整改 | 原 MCP 清理、页面草稿和精确原生输入缺项不能由邻近状态裁定。 |

各节点解决不同交接，不据提交连续推断同一个 bug 被反复改坏。

## 根因与修复

### 1. 原实例的使用寿命超过 handler

App 请求可能仍在读慢 body，也可能已返回 StreamingResponse、尚未被 ASGI 消费。
只枚举 handler task 会把仍被使用的 backend 算为空闲；等待后按 port 重找对象，
还可能把原请求发给同端口替代进程。

修复捕获原 port/process，以 BackendUse 覆盖读 body、响应消费、client/response
清理及 202 后续消费者。STARTING 和未确认退休对象计入容量，删除前持久记录
retiring，停止失败保留目录。HTTPX is_closed 可能先于 stream cleanup，原对象还
要保有关闭任务。LRU、idle、delete 和 drain 使用相同责任。

[请求寿命回归](../../tests/test_app_request_lifetime.py)扣住慢 body、未迭代响应、
header/close 失败，检查原实例仍被占用。实现见
[BackendUse](../../server/app_backends.py)与[代理清理](../../server/app_proxy.py)。

### 2. 执行已有 owner，新增控制仍由短命 waiter 承担

Schedule PATCH 已提交 SQL，waiter 却在注册 trigger 前退出；Research cancelled
已提交，原 pipeline 尚未收到停止信号；Connector 已收到轮转 token，仍未保存。
执行 owner 和对象锁都没有自然覆盖这些新增动作。

Schedule 在首次 DB 等待前登记并 shield 控制操作，按当前持久定义重建 trigger，
shutdown 先 join 控制。注册失败保留原 schedule ID；删除未来 trigger 不取消已接纳
fire，不能提示调用者再 create。Research 控制保有取消提交和原 pipeline 信号，
leaf 清理仍归原流水线；Connector manager 保有原 refresh 到保存和退休。

回归入口为[计划数据库](../../tests/test_schedules.py)、
[计划 shutdown](../../tests/test_scheduler.py)、
[Research 取消](research-completion-boundaries.md#4-取消事务成功后停止原执行仍有独立责任)
与[OAuth](oauth-repeated-boundary-bugs.md)。这些责任差异不要求共用一个取消策略。

### 3. SDK 取消没有证明原问题调用已退出

MCP SDK 回复取消后，原建题回执、runner 关闭和 REST waiter 清理仍可能未结束。
Owner 必须结算原建题结果，runner 在 Owner 外关闭原问题并清理 waiter，Owner 再
消费实际完成。未知原生关联不能提前释放 execution 或 App 容量。

页面也有独立责任：原答案、提交身份和不可变提交快照跨卸载保管；unknown 沿原提交
核对，迟到接纳只清当时提交的快照，不能清掉后来编辑。丢弃续答预览后普通发送仍
须可用。相关反例见[问题生命周期](../../web/src/components/QuestionLifecycle.test.tsx)
和[App 响应恢复](../../tests/test_app_response_recovery.py)。

### 4. 完成分类不能越过领域效果和原输入关系

有效根终态、原始记录完整性与业务成功分别保留。精确关联、已取消且本地清理完成
的纯 ask 输入缺项可适用窄成功判断；它不证明 provider 消费，不消除其他未知写入、
旧 attempt 故障或领域产物门槛。无效输出引用也不能在 UI 显示已保存。

[组合回归](../../tests/test_mcp_input_completion.py)串起 mounted MCP、受控原生
执行、Owner/SQLite 与 App、委派、Research、Wiki、Schedule。委派业务已完成时，
通知查询曾仍早于返回接纳，验收改为等原持久回执；不能把业务完成提升为通知完成。
结果展示另见[MessageBubble](../../web/src/components/MessageBubble.test.tsx)。

## 为什么测试没有先拦住

测试多次等待了目标事实附近的事件，或没有制造实际失败窗口：

| 原构造 | 漏掉的事实 | 修正后的观察 |
| --- | --- | --- |
| clean close 后读取恢复数据 | commit 后、通知前中断 | 在该窗口中断，核原持久事实与恢复。 |
| 统计 argv 回调次数 | OS spawn、进程组及后代退休 | 真实启动/失败/迟到交错，核原资源退出。 |
| 业务完成后查 live map | 业务与退休有不同阶段 | 分别等待 completion 和退休证明。 |
| 观察队列、卡片、SDK 取消或 HTTP 返回 | 原输入提交与清理可能尚未完成 | 扣住原清理，核 receipt、持久结果和实际容量。 |
| 旧提交接纳后清表单 | 用户后来编辑也被删除 | 卸载/重挂载后回报，只清原快照并检查发送仍可用。 |

共同接口在代表性消费者证明完整语义前就展开迁移，也让每个新增消费者继续重定义
接口。这是从代码与交错推得的工程原因，不是对每次开发动机的记录；原通过数不能
补足未触发的反例。

## 教训与当前合同

- 按消费者定义完成条件，列出原身份、事实、writer、提交点及失败接管，不用
  “进入 owner”代替完整行为。
- 共同语义先由不同的真实消费者验证，再迁移其余路径并退出旧编排；保留来源差异。
- 发现新提交点先修正责任模型，再决定锁、事务或原任务怎样承接，不继续叠状态。
- 验收等待原 receipt、效果和退休；审查核对反例与责任，不只看通过数。

现行规则见[范围与完成条件](../../AGENTS.md#scope-and-completion)、
[输入完成](../implemented/input-reliability.md#原请求完成与失败交接)、
[MCP 退休](../implemented/harness-layer.md#mcp-question-retirement)、
[App 后端](../implemented/application-backends.md)和[计划运行](../implemented/scheduled-runs.md)。
per-fire history、停机 catch-up 等独立产品选择不属于此次缺陷；验证选择与证据复用
归[测试政策](../testing.md)。命名委派的原 work、返回和执行关联另见
[三个完成点复盘](session-delegation-completion-boundaries.md)。

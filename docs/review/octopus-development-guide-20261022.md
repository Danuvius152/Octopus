# Octopus 后续开发指南：错误处理、执行所有权与最小机制架构

> **2026-10-22范围补充：** 用户决定先修现有架构bug，暂停交互app-server及完整跨turn
> 原生全树交付。Codex沿用exec，Claude沿用stream-json/idle；本轮receipt不等待所有未来
> 原生工作，迟到会话消息不回写已返回App/委托响应。普通委托完成后保留会话，下一请求
> 先退役旧held资源再安装新上下文。详见[执行计划§10](../plans/minimal-architecture-execution-20261020.md#10-what-this-defers)
> 和[规范§5.7](octopus_minimal_architecture_spec_20261020.md#57-当前调用迟到会话消息与资源退休分开)。
> 本次本地基线为7ee4ac9，52/53已有原票验收；下面2.0版的固定快照与历史核验边界保留。

> 文档版本：2.0 · 更新日期：2026-10-22（Asia/Shanghai）。
> 用途：供后续开发、代码审查和任务交接使用，统一已经确认的事实、错误处理原则、剩余实施范围与验收方式。
> 本次交付范围：更新开发文档。未修改 Octopus 业务代码，未执行发布，也未重新运行项目测试。

## 阅读入口

- 接手开发时，先读 §1 的版本与进展，再读 §2 的决策摘要。
- 处理 dismiss、取消、提交失败或恢复问题时，读 §3～§9。
- 准备继续实施时，使用 §12 的推进单元和 §13 的验收矩阵。
- 让编码模型工作时，使用 §14 的工作方法；不要直接把旧附件的建议逐项变成补丁。
- §11 对旧建议逐条作出处置，§16 保留可核查的固定版本来源。

## 1. 核验范围与实际进展

### 1.1 版本、时间与证据等级

| 对象 | 本文依据 | 限制 |
| --- | --- | --- |
| Octopus 源码 | 最后成功读取的 `main` 快照 `85b5b3c99192eb9a85b6bd923cd1be856492ded0`，提交时间 2026-10-21 20:32:48 +08:00 | 2026-10-22 刷新时，公开 Git 获取失败，GitHub 仓库 API 返回 404。无法确认此后是否新增提交，也不据此推断仓库已经私有化、改名或删除。 |
| DeepSeek Harness | `master` 的 `687f282e8625c6b8d11ff58211a90524be2e4317`，提交日期 2026-10-17 | 本次重新 fetch，远端仍为该提交。本文只引用已阅读的规范和相关实现。 |
| 新上传附件 | `architecture-optimization-20261020(2).md` | 与此前 `(1).md` 内容完全相同，仍是旧审阅，未包含后续实现与本轮修正。 |
| 仓库进展 | 执行计划、票20验收摘要及实际代码 | 仓库记录的验收结果作为历史证据引用，不表述为本次重新执行或独立复现。 |
| 实际部署 | 仓库中的受管 profile、unit 生成与启动核验代码 | 本次未读取用户机器实际生效的 unit、运行状态或部署日志。 |

后文使用四种状态：

- **源码已见**：在上述快照中读到了实现，不能单凭这一标签宣布测试或部署成功。
- **仓库验收记录**：已有文档记录某固定候选的验证结果，必须保留其范围和版本。
- **源码推导待验收**：存在具体代码路径和失败窗口，本次未运行复现。
- **后续目标**：建议或既有计划中的工作，不等于已经实现。

本文件不是另一套项目任务账本。具体票号和正式完成状态继续由仓库既有执行计划及验收记录维护；本文解释它们对后续开发的约束。[O01] [O02] [O03]

术语沿用仓库：**运行数据库**指框架 SQLite；**主库／Canonical Vault**指权威知识正文。本文不再沿用旧附件中容易混淆的“主库数据库”称呼。[O01]

### 1.2 仓库已经记录的阶段性完成

票20验收摘要记载：42 个实施票中完成 16 个，编号为 **10～20、29、37、40、45、46**；其余 26 个及完整迁移目标不能由票20验收推导为完成。执行计划保留后续实施及第51票完整发布门禁。[O01] [O02]

后续本地进展补充：拆分后共46个实施叶子；52/53已在`7ee4ac9`及其祖先提交完成，
当前为18项完成、28项待办。以下旧验收数字不变，剩余排程以现行执行索引为准。

| 仓库记录 | 记录中的结果 | 如何使用这份证据 |
| --- | --- | --- |
| 票20固定候选 `134705` 的受影响联测 | 115 个文件；1907 passed、23 deselected；选择 `not real and not knowledge_eval` | 证明该候选在该选择范围内通过，不代替其余工作包。 |
| 后续完整后端预检 R3 | 5084 passed、60 deselected；同样排除 real 与 knowledge_eval | R3 收集后另有两项纯测试补强，以当前版本8项通过核验；记录未据此改写 R3 来源清单。各范围不累加成一个测试总数。 |
| 前端与纯 UI 浏览器 | 前端 496 项／58 文件；纯 UI 浏览器 88 项 | 保留各自覆盖面；纯 UI 派发不等于真实模型派发。 |
| Codex 实机及真实浏览器 | 实机 18 项，真实浏览器 1 项；文档说明了后续诊断修改与证据复用范围 | 不能扩大为所有 provider 已实测；Claude 在该记录中未配置、未运行。 |
| 发布状态 | 记录发布预检与适配，并明确不声明已经上线 | 本文不补造部署成功事实；后续按当前候选执行适用发布门禁。 |

以上均为**仓库验收记录，本次未重跑**。不把“源代码存在”“某组测试绿色”“已获得某次发布授权”和“生产已经切换”混为一个完成状态。[O02]

### 1.3 当前实现与剩余范围

| 领域 | 已见实现或已记录进展 | 后续边界 |
| --- | --- | --- |
| 权威存储 fail-stop | `ServiceHealth.fail()` 设置 fatal 并直接 `os._exit(70)`；运行数据库与 Wiki 共享 health | 保持单一故障出口；真实进程退出及 supervisor 收束仍需对应运行证据。[O04] [O09] |
| 运行数据库短事务 | `Database.transaction()` 统一 BEGIN、SQL、游标、COMMIT/ROLLBACK 与总期限 | 不回到跨公共调用的未提交 SQL；业务接纳、claim、结算的原子化仍需按后续票完成。[O05] [O01] |
| 驱动取消收束 | `Transaction._wait()` 保留 driver task，等待取消不直接丢弃驱动结果 | shield、游标收尾和总期限处理的是真实异步边界，应保留。[O06] |
| 提交后读取 | `get_committed_terminal()` 已通过普通读事务查询 | 旧文档“每次另开连接验证 SQLite”不再是此函数的现状。[O07] |
| SessionOwner | 会话执行、问题与生命周期状态进入单 owner；原 request/attempt/work/backend 身份随 I/O 传递 | 不再从零引入 actor；继续完成 durable 派发、现有CLI本轮收尾/idle归属、RunBinding、实际通道送达和结算事务；完整跨turn原生交付已退出本期。[O01] [O02] [O08] |
| completion | 仓库记录消费者已迁入原 completion，业务结果、记录确认与通知状态独立 | 单 owner 已接线不等于整个 P4 完成；保留精确资源退休和交接 drain。[O01] [O02] |
| dismiss | 先提交用户处置，再通知原 owner；原 owner 退休不否决已提交成功 | 这是应保留的行为，不改为通知失败就退出全服务。[O10] |
| 历史 unknown 准入 | 旧85b5b3c快照仍有 `uncertain_submission_writers()` 与历史 PGID 阻塞 | 下述历史诊断由53接管，53已有原票验收；同代精确退休边界继续保留，本次不重跑或扩大其结论。[O11] [O12] [O13] [O14] |
| 受管进程管理 | unit 生成、运行归属校验、prestart 检查、启动限流已有代码 | 必须区分上一代已退休与同代仍在运行；不能把 managed 标志当全局绕过。[O15] [O16] |
| 输入账本 | 当前仍保留 reserved/queued/claimed 与 reserve→start→settle 等路径 | 五态持久账本与完整输入协议尚未全部收敛；不能仅因为有事务 wrapper 就宣布完成。[O11] [O12] [O01] |
| Wiki 存储 | 连接、提交、回滚、关闭及权威文件效果已接入健康边界 | Wiki 的业务恢复和取消路径仍需继续接管。[O17] |
| Wiki 遗留机制 | 仍有部分 unconfirmed、witness、取消轮询、宽泛异常退避和 claim 收尾 | 必须逐条说明剩余原因；不按名称机械删除。[O18] [O19] [O20] |
| Wiki 文件恢复 | prepared 变更按 before/after hash 分类为 applied、interrupted 或 conflict | 必须保留；文件与 SQLite 的非原子性不会因 fail-stop 消失。[O21] |

### 1.4 已落地接口应成为后续修改的约束

后续改动必须保留票20记录的这些区分：

| 接口事实 | 既有表达与责任 |
| --- | --- |
| 是否接纳 | `SubmissionReceipt.disposition`；HTTP 等待者取消不撤销已经由 owner 接管的命令。 |
| 原输入与携带请求 | `RequestCompletion.input_request_id` 与 `request_id`；不能把未确认的 steer 强行套入携带请求的结果。 |
| 业务结果是否已记录 | terminal 事务成功返回时确立 committed；之后投影读取失败不能反转它。 |
| 通知是否完成 | 同一 completion 的通知状态；通知失败不抹掉已提交的业务结果。 |
| 资源是否退出 | 原 execution、preparation、reader、retirement 等实际引用；不能从 terminal 或通知完成推断。 |

这些是仓库执行计划对票20接口的说明；下一步实施仍要核对目标代码版本，不能将文字说明当作所有路径都已证明正确。[O01] [O02]

## 2. 后续开发的决策摘要

1. **共享权威存储失信时，维持全服务 fail-stop。**接受单次严重存储故障中断其他会话的代价；由既有 supervisor 和启动恢复重新建立可执行状态。[O01] [O03]
2. **业务拒绝、正常取消、通知失败分别处理。**它们不自动意味着共享存储或整个进程失信。
3. **每个事实只有一个权威 owner。**事务拥有驱动与提交；SessionOwner 拥有活动会话；supervisor 管理整代进程；History 管理文件效果证据。查询、UI 和通知使用投影。
4. **unknown 表达缺少结果证据。**进程退出、dismiss 或 HTTP 断开，都不能把历史 unknown 改写为“从未执行”。
5. **成功必须绑定明确提交点。**已持久结果、资源退休、通知送达、用户处置可以独立成立，不用一个布尔值替代全部事实。
6. **取消必须收束原执行。**取消请求、终止信号、停止等待、实际退出是不同事件。
7. **重试必须有安全重复依据。**由一个 owner 负责；普通有限操作有预算，未知副作用不自动重放。
8. **沿用平台已有保证。**使用 SQLite 事务和受管进程生命周期，减少运行期反复回读、猜测与补偿。
9. **结构性改动按完整职责切换。**同时迁入调用者、状态更新和失败收尾，再删除旧 writer；不要让两个机制同时掌握权威。
10. **完成依据是行为和证据。**不以测试数量、补丁次数、文件长度或新抽象数量作为完成定义。

本文件不新增通用 Repository、Event Bus、Queue Manager、异常框架、执行数据库或第二套任务账本。需要增加的类型、字段或协调代码必须对应当前真实的处理差异。

## 3. 失败模型：先解释事实，再决定停止范围

### 3.1 只针对实际部署存在的边界

当前讨论包括这些真实边界：

- asyncio 调用方与 aiosqlite 后台线程之间的完成确认。
- 持久输入、进程启动、stdin 投递和终态保存之间的提交窗口。
- 活动 SessionOwner 与原执行 I/O、取消和迟到完成事件之间的生命周期。
- SQLite 与 Wiki 文件、外部编辑器、App、远端工具服务之间的非原子性。
- 已提交结果与 HTTP/WebSocket/通知接收方之间的传输失败。
- supervisor 管理的一代进程与下一代恢复之间的准入边界。

不存在当前需求或执行路径的假想故障，不应先实现 fallback、扫描器或状态机。真实的故障可以用更强的所有权或事务保证消除；消除后应同步删去旧的重复防御。

### 3.2 四类事实必须能独立解释

| 事实 | 回答的问题 | 由谁负责 |
| --- | --- | --- |
| 业务结果与持久记录 | 这项操作是否完成，哪些事实已提交？ | 业务 owner 与相应事务/效果协议 |
| 执行资源 | 原任务、线程操作或进程是否还可能写？ | 原执行 owner、退休句柄与 supervisor |
| 交付与展示 | 哪个客户端已经收到什么？ | 既有 publisher、outbox、接收方和查询投影 |
| 用户处置 | 用户是否收起提示、请求取消或发起新工作？ | 对应持久操作与命令入口 |

这是语义区分，不要求新建四张表或给所有对象添加四组字段。能从现有持久证据或实际执行引用得到的事实，不再存副本。

### 3.3 处理决策表

| 失败 | 最小正确处理 | 停止范围与重试 |
| --- | --- | --- |
| 参数、权限、配额或版本冲突 | 明确拒绝；必要时确认事务回滚 | 当前请求；按业务条件重新提交 |
| 模型限流、指定暂时性网络失败 | 保留原请求归属，按声明的策略处理 | 模型请求或当前回合；仅安全的、有负责人和预算的重试 |
| 工具返回失败 | 报告失败与可确定的副作用 | 工具/工作范围；不从 `isError` 推导已回滚 |
| 用户取消等待 | 取消等待者，原 owner 仍收束已接管工作 | 不直接销毁已接纳命令 |
| 用户取消执行 | 停止继续派发、请求原执行退出、等待退休 | 当前精确执行；保留已发生效果 |
| 已提交后的纯通知失败 | 保留结果，报告或重新读取投影 | 通知范围；不重新执行核心业务 |
| 单个 owner 的不变量无法继续保证 | 关闭该 owner 的新工作，收束原资源并上报 | 会话/组件；不能自动创建替代 owner 掩盖故障 |
| 驱动提交、回滚或权威存储无法可靠收束 | 不再使用本代失信状态，进入统一 fatal | 当前采用全服务停止；冷恢复读实际事实 |
| 历史外部效果未知 | 保存原执行与证据，必要时核对外部状态 | 允许与未决效果无冲突的后续工作；不盲目重放原操作 |

Fail-fast 表示在可判断处尽早失败；fail-closed 表示前提未满足就拒绝继续；fail-stop 表示停止不能再信任的执行实例。三者可以同时存在，停止层级由状态依赖决定。

## 4. 操作合同：dismiss、cancel、retry 与状态查询

本节规定操作必须保留的业务语义，不要求新增 API，也不意味着现有每个入口返回同一种对象。返回形状沿用对应接口合同；不能从一个布尔返回推断未被该接口承诺的事实。

| 操作 | 承诺 | 不应由这个操作推导的事实 | 后续处理 |
| --- | --- | --- | --- |
| dismiss | 用户不再显示/处理此条历史提示的处置已经持久化 | 原执行已退出、外部效果已撤销、unknown 已变为未执行 | 通知可失败；恢复列表不再返回该项，原执行结果保持原义 |
| cancel queued input | 明确阻止尚未开始的工作 | 所有其他工作也已取消 | 通过既有返回值或状态投影确认处置，释放属于该输入的资源 |
| cancel active execution | 请求原执行停止，并按接口合同确认退休与结算 | 过去的文件/API 效果已撤回 | 未确认退休时不放行冲突执行；保留部分结果 |
| retry known-unstarted work | 在明确规则下再次接纳该工作 | 可以重复所有历史 unknown | 沿既有接纳和幂等身份规则处理 |
| redo unknown work | 用户明确发起新的执行，原历史不被覆盖 | 新身份本身已经解决重复副作用 | 先核对相关效果；保留原记录与新旧关联 |
| query status | 读取已知持久状态和当前可见投影 | 查询失败使此前提交失效 | 保留最后已确认事实；可对安全读取作有限重试 |

传输重试与业务重做必须分开：同一身份的核对不应创建第二次业务执行；有意重新执行则使用符合现有协议的新执行身份。依据执行计划，已 accepted 或 unknown 的身份继续严格去重；明确拒绝后的新请求与旧版持久拒绝记录按现有兼容规则处理。[O01]

## 5. dismiss：保留正确实现，修复错误的准入责任

### 5.1 当前已经正确的提交/通知顺序

`server/sessions/turns.py:75–92` 的实现先等待 `db.dismiss_client_submission()` 提交，再按当前会话生命周期向可用 owner 投递 `RecoveryDismissed`。尚无 owner 且会话准入仍开放、没有归档预留时，可以按原规则建立 owner；原 owner 已失败或封闭时，不为发布投影复活它。通知遇到 `SessionOwnerUnavailable`，仍返回已提交的持久操作成功。[O10]

这个窄 catch 有明确理由：异常发生在已知可省略的通知环节，核心持久结果已经确定。不要把数据库写入、运行时修改、进程停止和通知放进一个大 `try`，再在 `except Exception` 里猜测哪些部分成功了。

已有测试表达了通知顺序、归档/失败 owner 后仍可 dismiss、失败 owner 不因此重新接活等要求。本次只读了这些测试，后续变更应按受影响范围执行它们。[O22]

### 5.2 残留问题的证据链

1. `claim_client_submission()` 保存认领身份与 boot 信息。[O11]
2. 后端启动返回后才记录 PGID；两者之间存在进程退出窗口。[O14]
3. 启动恢复将 claimed 改为 unknown；缺失 PGID 的事实不会自动补全。[O11]
4. `uncertain_submission_writers()` 找出 unknown 历史行；`require_retired_writers()` 继续检查旧 PGID。[O11] [O12]
5. 同 boot 且 PGID 为 NULL 时，退出检查不能确认已退出；dismiss 只改变可见性，不改变这条阻塞。[O13]

结论等级：**源码推导待验收**。不能把本次没有运行的故障窗口写成已经复现，也不能忽略代码链路中实际存在的准入责任冲突。

### 5.3 目标行为

经过已经验证的受管冷启动后，上一代本服务拥有的执行已经退休。原输入仍可以保留 unknown，但不再仅因历史 PGID 缺失就永久阻塞会话。当前一代正在运行、写入未知或正在退休的执行，仍由当前 owner 和精确引用约束。

**禁止两个快捷补丁：**

- 在 unknown writer 查询中加入 `dismissed=0`，把收起提示当作退出证明。
- 在准入入口加入 `if managed_profile: return`，连同代活执行的限制一起跳过。

受管启动的证明只覆盖其负责清理的上一代；不能替代当前一代的执行生命周期。只有能够确认全部写入资格已经结束或被有效撤销时，才释放冲突的新工作。

### 5.4 最小实现边界

- 复用当前服务代次与执行归属，不新增历史 PID 扫描数据库。
- 在启动/恢复边界接管“上一代是否可能继续写”的责任；历史输入只保留结果证据。
- 新准入与实际 dispatch 使用同一依据，避免只修入口而执行前仍走旧检查。
- 保留同代取消、steer 未决写入和迟到 I/O 的原 owner 收束。
- 如果仍有真实阻塞，提供独立、可操作的执行状态；用户隐藏历史卡片后，不得只剩无法解释的输入拒绝。
- 退休依据不足的非受管运行方式不能套用生产保证；明确其支持范围，不用 dismiss 绕过。

删除 `require_retired_writers` 或其历史分支的前提，是上述责任已经完整接管并通过针对性验收，不是某个字段已经改名。

## 6. 数据库：把必要复杂度集中在事务边界

### 6.1 区分 SQLite 结果、事务状态与调用方确认

SQLite 原生 COMMIT 的返回、事务是否仍活跃、async 调用方是否收到确认，是三个不同问题。COMMIT 遇到某些 BUSY 情况时事务仍活跃；其他错误可能只撤销当前语句，也可能取消整个事务。不能把所有异常都解释为“已经完整回滚”或“连接必然损坏”。[R01]

aiosqlite 每连接使用后台线程和请求队列。操作已进入队列后，等待它的协程取消不代表底层操作被撤销：提交可能继续完成，而原调用方已经停止等待。这是实际异步边界，不能用“数据库在本进程内”排除。[R03] [R04]

当前正确方向是由 `Database.transaction()` 和 `Transaction._wait()` 持有 SQL、游标、driver task 与结束责任。普通等待者取消由这个边界收束；无法在总期限内完成可信的结束时，进入统一 fatal。[O05] [O06]

### 6.2 一次事务只处理同库业务

事务内执行需要原子化的 SQL。网络、模型、子进程启动或等待、通知投递不放进持锁事务。业务 helper 显式接收已有 tx，不递归进入另一公共事务，不在各 helper 内部偷偷 commit。[O05] [O01]

条件更新可以合并检查与状态迁移，但 `RETURNING` 的行仍不等于持久化成功。返回语句完成、游标收束、COMMIT 成功后，才能发布依赖它的成功事实。[R02]

下面只示意顺序，不是可直接替换当前接口的补丁：

```python
async with db.transaction() as tx:
    claimed = await tx.fetch_one(
        "UPDATE inputs SET state='running' "
        "WHERE id=? AND state='pending' RETURNING id",
        (input_id,),
    )
# 事务上下文已经完成游标收尾和 COMMIT。
if claimed is None:
    return NotClaimed()
return Claimed(input_id)
```

使用条件更新的收益是让迁移前提和写入在同一语句中表达；是否需要改写现有已被事务串行保护的查询，应由实际简化收益决定，不能仅为了使用 RETURNING 改动所有 SQL。

### 6.3 错误分类只做到能够改变处理方式的程度

当前运行数据库对多数 `DatabaseError` 采用 fatal；Wiki 对部分 SQL/业务类错误保留回滚并上抛的路径。它们是当前保守程度不同的契约，不能统一描述为“这些异常都证明硬盘损坏”。[O05] [O17]

后续原则：

- 已知领域冲突、参数拒绝以及可以确认安全结束的事务外竞争，不通过杀服务表达。
- 提交、回滚、驱动结束无法确认，或权威存储无法继续被信任时，停止使用本代状态。
- 对意外编程错误是否允许局部继续，取决于 owner 能否证明受影响状态仍完整；不会因为加了 catch 就自动成立。
- 不为所有异常建立巨大的继承树。只有调用方确实需要不同动作时，才增加稳定类型或判别码。

### 6.4 可删除与必须保留

| 可在完整接管后删除 | 必须保留 |
| --- | --- |
| 正常 commit 成功后另开连接逐行验证提交 | 显式事务结束与驱动确认 |
| 多个业务模块重复保存“等待补写”状态 | 原始输入、执行身份和结果证据 |
| 同代反复修补已经失信的数据库连接 | 单一 fatal 出口与恢复准入 |
| 从字符串 reason 猜迁移、存储多份派生结果 | 必要的幂等身份、唯一约束、真实领域状态 |

运行数据库使用 WAL/FULL；Wiki 同步权威连接使用 `synchronous=EXTRA`。这些设置提供各自存储的持久性基础，不把文件/API 效果纳入同一个事务，也不证明同步小事务没有性能成本。批量策略只在测量表明需要时调整，不能重新引入跨业务调用的含糊提交。[O28] [O17] [R05]

## 7. SessionOwner：单写者、长 I/O 与取消

### 7.1 保留现有单 owner，继续完成责任接管

SessionOwner 已经存在。后续工作是核对所有活动会话写入是否仍经过它，以及输入、执行与完成消费者是否使用同一份身份和结果；不再把“引入 actor”当作一个从零开始的大项目。[O01] [O02] [O08]

长 preparation、spawn、stdin、reader、stop 等 I/O 使用受管任务，保留原结果并回报 owner。owner 在 I/O 期间仍能处理 Interrupt/Close 等控制命令。结果携带原 request、attempt、work 和 backend，不能在回到队列时重新绑定到“当前请求”。[O01]

单写者保证的是自己的内存修改顺序。外部文件、网络、子进程以及先前派发的 I/O 仍然会变化；迟到结果的归属检查不能机械删除。

### 7.2 失败 owner 不自动复活

普通业务拒绝可以返回调用者。若 owner 的状态已经无法继续解释，应关闭其准入，并让原对象与原资源完成收束；不能创建一个新 owner 接续失信状态，只为了让某条调用或测试看起来成功。

持久的管理操作仍可拥有独立成功条件。例如 dismiss 已提交后，通知原 owner 失败不否决提交；但这也不能证明原会话可以重新执行。

### 7.3 取消等待与取消执行

HTTP waiter 结束，不代表已接纳命令的 owner 消失。对活动执行的取消，则要沿原 execution/retirement 确认实际资源结束。当前 `cancel_request()` 的普通调用路径在取得原 retirement 时会等待它；在 publisher 回调内，为避免等待自己，不内联 join，退休责任仍由原 owner 持有。需要强退出确认的调用者应观察相应 retirement，不能仅凭方法的布尔返回推断旧执行已退出。[O23]

取消不能只把数据库行改为 cancelled 就放行替代写入者；同样也不必让每一次正常取消都升级为全服务退出。只有原 owner 不能可靠收束、且冲突写入资格无法被控制时，才按既定故障边界升级。

### 7.4 通知不可成为权威状态机的隐式驱动

关键提交、所有权交接和资源退休通过有明确成功/失败结果的调用或 completion 完成。纯广播只报告事实。若一个监听器还会修改权威业务状态，就必须明确其 owner 和失败合同，不能以“通知可以丢”处理。

资源 join 与通知 drain 需要保留各自依赖顺序，避免关闭资源的调用反过来等待正在调用它的 subscriber。后续精简应保留票20已有的交接保证。[O01] [O02]

## 8. 全服务 fail-stop 与冷启动恢复

### 8.1 为什么当前采用这个范围

Octopus 已选择：运行数据库、Wiki 权威存储和共享运行状态不能可信继续时，停止当前服务代次。单进程内有多个会话和后台入口，共享基础状态失信后，逐个业务分支猜测“是否还能继续”会维持更多恢复状态。统一停止使这些分支可以交由受控启动处理，代价是其他正常会话也会中断。[O01] [O03]

这是本项目的取舍，不是 SQLite 规范要求所有错误杀进程，也不是 Harness 对所有异常采用的策略。参数错误、权限变化、内容冲突、模型请求失败和纯通知失败，仍按 §3 的范围处理。

### 8.2 当前代码已经提供的基础

`ServiceHealth.fail()` 先设置 fatal、尝试写出有限诊断，再调用 `os._exit(70)`。它不依赖可能已经卡住的事件循环完成异步退出。正常关闭仍走正常 drain；不要为了共用一段 cleanup 把 fatal 路径重新变成可无限等待的关闭流程。[O04]

受管安装生成的 unit 使用 `KillMode=control-group`、`SendSIGKILL=yes`、`Restart=on-failure` 和启动限流；运行期检查实际 MainPID、cgroup 与 KillMode。prestart 检查 unit 根组中只有检查器自身，并核对 `pids.current`。它们共同承担“下一代不能与上一代的本地执行并存”的平台责任。[O15] [O16]

systemd 的 control-group 终止规则覆盖该 unit 控制组中的进程。保证的前提是实际运行配置和归属符合要求，不能从服务名称或配置文件存在推导已经生效；脱离管理域的远端操作也不在这项进程保证内。[R09] [R10] [R11]

### 8.3 保留最短的恢复顺序

后续沿用既有启动入口，按下面的依赖关系推进；这不是要求新增一层启动框架：[O01] [O09]

1. 核验运行 profile、数据目录独占和本代执行归属；不能确认时保持不可写。
2. 建立权威存储并执行必要迁移，读取真实已提交状态。
3. 在已确认上一代不能继续写的前提下，完成输入与 Wiki 效果核验；未知保留未知。
4. 完成统一恢复门，再开启 admission、scheduler、Wiki worker 等 producer。
5. 运行期普通取消继续收束当前持有的原任务、进程和工具句柄。

顺序中有两条不同的证明：supervisor 证明旧代未来不再写；恢复证据说明它过去写了什么。第一条不能替代第二条。

### 8.4 不再增加的进程机制

不新增 executions 表、历史 PID/PGID 扫描、`preexec_fn` 中的 PDEATHSIG 或“boot 读不到就尝试杀旧进程”等平行保障。既有规范已否决这些默认方案。[O03]

Python 文档明确警告多线程下使用 `preexec_fn` 的风险；PDEATHSIG 也存在父进程已退出的设置窗口、与创建线程相关的语义及对子孙进程的覆盖限制。它不能单独成为完整的进程树归属协议。[R07] [R08]

若未来必须支持新的部署平台，应先给出该平台对应的执行归属与重启合同，再判断需要什么实现；不要把未支持平台的每一种可能情况变成当前生产路径的 fallback。

## 9. Wiki：保留跨资源证据，替换失信后的在线补偿

### 9.1 fail-stop 不会把 SQLite 与文件变成同一事务

Wiki 同时涉及数据库、文件、观察器以及外部编辑器。即使数据库与文件都在本机，进程仍可能在其中一个资源写入后退出。当前 prepared 记录通过实际内容 hash 与 before/after 比较，区分 applied、interrupted、conflict；这是有真实非原子边界支撑的恢复证据。[O21]

因此保留 history、必要的 receipt、稳定文档身份、原始字节/来源证明、revision，以及已有跨库通知 outbox。不要将其当作“数据库 commit 不可信”的重复防御一并删除。[O01]

| 已知证据 | 可以得出的结论 | 仍不能得出的结论 |
| --- | --- | --- |
| prepared 对应文件为精确 after 版本 | 该条文件效果可分类为 applied | 整个 job、item 或工具序列全部成功 |
| prepared 对应文件仍为精确 before 版本 | 该条准备的文件变更尚未落到所核对版本 | 没有其他文件、网络或先前批次效果 |
| 当前内容既非 before 也非 after | 存在需要保留和解释的冲突 | 可以自动覆盖为任一版本 |
| 缺少某条 result/receipt | 缺少该步骤的完成证据 | 步骤没有执行，因而可以安全重试 |
| 执行资源已全部退休 | 该执行不能再继续写 | 过去效果已经回滚或全部成功 |

恢复依据是原执行的完整完成合同。某个 item 可能包含 ingest、多个文件修改或多个批次；不能用“至少一条 applied”直接标记整个 item done，也不能用“没有 applied”触发模型重跑。

### 9.2 对现存遗留机制先分责，再删除

当前 `wiki/jobs.py` 仍有 confirmed/unconfirmed 结算、recording recovery、execution unknown、异常退避与 claim 收尾；取消 witness 和 unsettled 轮询也仍存在。源码存在这些机制，不代表每一条都在实际存储 fatal 后仍可达，更不代表它们都可立即删除。[O18] [O19] [O20]

逐个机制只能归到以下实际责任之一：

| 原机制承担的责任 | 后续处理 |
| --- | --- |
| 同代对失信存储猜测提交、反复补写 | 接入统一事务/fatal 边界后删除此责任及其副本状态 |
| async 调用已取消，但线程或 claim 操作可能完成 | 由原 owner 持有并等待真实结果，再结算；替代完成前保留必要收尾 |
| 执行已停止，但部分文件或外部效果需要核对 | 交给原执行身份与 History 效果核验，保留必要证据 |
| 已提交结果的通知投递 | 由既有通知/outbox 负责，失败不重新执行 Wiki 工作 |
| 历史旧数据只能表达 unknown | 兼容读取与明确呈现，不以删字段方式生造确定结果 |

目标是减少 owner 数量和重复事实。用一个更大的 catch、更多标志或一个轮询任务覆盖所有责任，会使错误处理再次分散。

### 9.3 取消的最小完整流程

沿既有 P5：由一个受管完成对象拥有取消收尾，记录取消意图并撤销相应写入资格，释放不能跨 join 持有的协调锁，等待真正 writer 退休，核对效果，再完成结算。HTTP 等待者可以收到“处理中”，但不能因断线而丢掉这项责任；`confirmed` 也不等于被取消业务成功。[O01]

不把所有取消错误都压成 409，也不把存储错误包装成普通业务拒绝。正常冲突按类型返回，权威存储无法收束进入 fatal，通知失败保留已提交结果。`asyncio.shield()` 保护内部任务免受外层取消，外层 await 仍可能收到取消异常，因此还需要原 owner 保留任务引用并完成 join。[R06]

只有正常完成、取消和启动恢复已经共用正确的效果核验，并且新的执行引用确实接管全部 writer，才删除相应 witness、取消 poller、recording flags 和存储故障 backoff。不能直接删除整个 `recover_publication` 或 `recover_creation`：执行计划说明它们也承担正常完成的效果核验。[O01]

### 9.4 不可变绑定与真实操作边界

RunBinding 固定原请求的来源、权限上限和返回目标。捕获这个对象可以消除 live context 双写，但不能把活动请求换绑到后来的更大权限或另一来源。当前授权、实际目标身份和 revision 在真正执行时仍需核验；入口到操作之间可能经历等待、取消或外部修改。[O01]

可以统一同一合同的检查实现，避免在多个内部 helper 中重复解析静态输入。不能把“MCP 入口检查过一次”当作 HTTP、App、后台任务或动态文件写入永久有效的授权。

### 9.5 存储结构先收敛类型，不默认拆表

既有计划保留 JSON 聚合，集中有限 typed item、编解码和结算入口。一个 job attempt 可以有多个 batch，原 session/request 等执行身份仍有意义，不能仅用 `(job_id, attempt)` 唯一标识所有执行。[O01] [O03]

当前可优先消除已经确认的重复 membership 重建和重复 outbox 聚合；剩余 JSON 整体更新成本如实测量。不要为删除十几个 obsolete flags 新建三张表，也不要因锁都叫“lock”就合并文件、registration 和数据库保护边界。

## 10. DeepSeek Harness 的可借鉴经验及适用边界

### 10.1 值得迁入开发习惯的做法

| 已阅读的 Harness 做法 | 对 Octopus 的具体启发 |
| --- | --- |
| 错误规范要求写明处理/报告 owner、失败后仍成立的不变量、效果是否确定以及清理责任；捕获异常本身不恢复状态 | 每个重要 API 写几行失败合同；调用方只在合同允许时继续，不以日志代替恢复。[H01] |
| 预期失败有稳定类型和判别码，保留 cause；不从可变错误文案判断流程 | 复用已有领域错误；仅为不同处理动作增加类型，不构建庞大异常继承树。[H01] |
| 同进程的已类型化内部调用与真正的解析、持久化、进程和网络边界区别对待 | 在输入边界集中验证；避免内部反复检查同一静态值，同时保留动态权限和身份约束。[H02] |
| 重要时序用明确生命周期表达，disposal 需要等待原工作安静下来 | 用原 completion/retirement 表示结束，不靠微任务先后、短暂静默或 UI 计数。[H03] |
| checkpoint policy 对具备活动会话的模型请求、关联 agent 的顶层工具调用，在派发前 flush，并在 `agent/pre-step` 处 flush | 依赖 durable 输入的执行只在提交确认后派发；嵌套工具复用外层屏障，不能将其扩大为所有输入派生均已有持久保证。[H04] [H05] |
| 修复中断的工具记录时区分未开始与结果未知 | 对 unknown 保留效果核对入口，不向模型或用户暗示“肯定没发生”。[H06] |
| 工具调度与 retry 插件在取消/dispose 时保留原调用收尾 | 不因调用方停止等待就丢弃已开始的副作用或重试循环。[H07] [H09] |

其中“只有状态 owner 能决定能否安全继续”“catch 不会撤销副作用”“清理有明确负责人”最值得采用。它们能减少临时补丁，因为需要恢复的事实被集中到了真正掌握它的边界。

### 10.2 Harness 并非全局 fail-stop 样板

已阅读的 agent loop 会把部分错误限制在当前 turn，并在收尾后回到 idle；不能据此描述为“一遇错误就退出整个进程”。其错误规范要求按受影响状态决定停止范围，与 Octopus 当前选择共享存储故障时退出整个服务并不冲突。[H12] [H01]

checkpoint policy 是需要实际挂载的组合能力。只安装持久化 backend 不自动获得同样的派发前屏障。屏障保存执行意图，不提供外部副作用 exactly-once；工具已执行但结果尚未持久化时仍可能 unknown。[H05]

### 10.3 重试经验：一个负责人，显式策略

在已核对版本中，模型请求的 `normal` 策略默认最多 **5 次重试**，初始延迟 **500 ms**，上限 **10 s**，jitter 比率 **0.1**，并按配置的暂时性错误码筛选；`always` 模式则会持续重试直到成功、取消或 dispose。因此不能概括成“Harness 的所有重试都有有限次数”。[H08] [H09]

对 Octopus，有限模型请求优先使用已有预算和取消机制；持续监督只有在产品确实需要时才显式启用，并让生命周期与降级状态可见。不要在 HTTP、SessionOwner、工具和数据库四层各套一个 retry。外部效果未知也不能仅因为错误被标为 timeout/transport 就自动重放。

### 10.4 JSONL 后端不能照搬成 SQLite 的恢复策略

所读 JSONL storage 会在 drain 失败后保留待写缓冲并暂停自动 drain；显式 flush 可以再次尝试。追加写路径有失败后恢复原长度的逻辑，恢复长度也失败时会报告聚合错误。这展示的是具体后端自己的恢复尝试，不足以证明每次失败后原句柄都已经可信，或所有失败都会永久封闭该句柄。[H10] [H11]

Octopus 已有 SQLite 事务和共享 health 边界，无需复制 JSONL 的 truncate、补写和重试机制。应迁入的是明确结果、归属和生命周期的设计方法，再按当前存储的真实保证实现。

## 11. 旧报告建议的迁移裁决

本节取代旧附件中的直接实施建议。**保留**表示方向成立；**已实现**只在注明证据范围内成立；**纠正**表示应使用新合同；**撤销**表示不进入当前必做范围；**待验证**表示不能按旧行号直接修复。另将本轮讨论中需要避免的泛化明确标为“延伸误用”，不将其归作旧报告原句。旧附件继续作为问题来源，不作为高于执行计划和当前代码的指令。[O01] [O03] [O24]

### 11.1 事务与错误处理

| 旧建议 | 当前裁决与后续要求 |
| --- | --- |
| 使用明确短事务，取消普通提交后的重复回读 | **保留，核心已实现。**复用 `Database.transaction`；`get_committed_terminal` 已是普通事务读取，不因函数名保留 committed 就再次重构。 |
| commit 抛错必然没提交；本地 SQLite 没有未知结果 | **纠正。**区分 SQLite 事务状态、驱动完成和调用方确认；取消已入队操作仍需收束。 |
| RETURNING 取到行就可以派发 | **纠正。**语句结果、游标结束与 COMMIT 分开；只在确认提交后释放依赖该 claim 的执行。 |
| 将全服务 fail-stop 留作后续方案选择 | **更新进展。**当前已选择并已有实现；后续关注接线、真实退出与启动保证，避免再次设计一套出口。 |
| 把精简补偿泛化为删除所有 retry、unconfirmed、shield 或恢复函数（延伸误用） | **禁止这样泛化。**按实际责任删除同代失信补偿；保留线程收束、真实未知和跨资源证据。 |
| 为延迟提交再建连接 | **撤销为默认任务。**当前采用一个受控写连接和短事务，批量优化需要测量与明确合同。 |

### 11.2 进程、输入与 SessionOwner

| 旧建议 | 当前裁决与后续要求 |
| --- | --- |
| 从 uncertain writer 查询排除 dismissed | **撤销。**显示处置不能证明写入者退休；按 §5 接管代际与同代执行归属。 |
| managed 环境直接跳过所有 writer 检查（延伸误用） | **禁止作为替代补丁。**上一代启动保证不能放开当前仍活跃的执行。 |
| 新 executions 表、PDEATHSIG、跨启动扫描并 kill 历史 PGID | **撤销。**使用已选受管进程方案；未知 boot 不授予历史 PID 的处置权。 |
| 收敛为五态并删除 reserve/settle/receipt | **保留目标，不能提前删除。**`pending/running/done/withdrawn/unknown` 仍需完整接纳、claim、终态和所有消费者切换；旧拒绝与 unknown 保留历史语义。 |
| 恢复列表 SQL 后过滤导致短页 | **已有改善。**当前 SQL 已关联终态进行过滤；剩余逐项状态读取按实际重复成本处理，不重复套用旧补丁。[O11] |
| 分页 cursor 改为 lookahead 的第 limit+1 行 | **维持撤销。**使用严格小于游标条件时，游标应取本页最后一行，避免跳过尚未返回的数据。 |
| 从零引入 actor，删除所有锁及 await 后核验 | **部分已实现，其余纠正。**继续使用现有 SessionOwner；只删被替代的会话状态锁，保留跨资源协调和迟到身份核验。 |
| 在 actor 内顺序等待长 spawn/stdin/stop | **纠正。**受管 I/O 回报原身份；owner 在等待期间仍可处理控制命令。 |
| 问答平行 maps 改为单对象 | **已见实现。**现有 `PendingQuestion` 已承载该责任；继续核对生命周期，不另造一套问题管理器。[O25] |
| WikiTask 直接共享可变 live context | **纠正。**减少双份事实，但以固定 RunBinding 绑定请求，不用换引用改变活动请求合同。 |
| WikiTasks 改为显式注入 | **保留，未完成。**当前 fallback singleton 仍在；接通真实实例及所有入口后再删除。[O26] |

### 11.3 Wiki 与局部优化

| 旧建议 | 当前裁决与后续要求 |
| --- | --- |
| 保留 prepared/applied 与 before/after 恢复 | **保留。**它解决数据库与文件之间的非原子边界，不属于多余防御。 |
| 任一 applied 就 done；没有 applied 就 retry | **撤销。**逐原执行合同核验效果；不生造完整成功或未执行事实。 |
| 会话 idle/terminal 可以代替 writer join | **纠正。**状态与资源退休分别确认；保留真实执行/线程/工具引用。 |
| 取消改为一次调用就可删 witness/poller | **纠正。**取消 owner、效果结算和冷恢复完整接管后删除；HTTP waiter 可返回处理中。 |
| 将 Wiki items 立即拆成多表 | **本轮撤销。**先有限类型、唯一编解码与结算入口；按测量处理写放大。 |
| 统一 Wiki 写事务上下文（旧 W5） | **保留职责收口方向。**复用现有 storage 边界和协议 owner；不同保护对象的锁不合并，不另建通用 SQL/Cursor 代理。 |
| MCP 入口授权一次，内部永久信任 | **撤销。**集中同类检查，仍在真实操作边界核当前授权、身份和 revision。 |
| 用 size/mtime/inode 代替 SHA-256 | **本轮撤销。**stat 不是字节相等证明；若改变不可变存储合同，先证明并测量，再单独设计。 |
| 移除弱随机幂等键回退 | **保留条件，尚未实施。**当前 `Math.random` 回退仍存在；按支持环境提供密码学随机或明确失败，不弱化输入身份。[O27] |
| 删除不可达 WS、收束 typed parser、整理循环依赖 | **待当前调用链验证。**与协议迁移一并做有限清理；部分公共类型已迁移，不以旧行号直接改。 |
| 合并短常量文件，以文件长度或删行比例验收 | **撤销为独立目标。**仅在改善实际职责/依赖时顺手整理。 |
| 不引入通用 Repository、PostgreSQL、Event Bus、Queue Manager 或状态机库 | **保留当前范围。**现有机制足以承担所述责任，不为空泛的扩展可能增加层次。 |

旧报告中的 P0 数量、旧行号、旧环境版本和阶段性测试数不自动延续到当前版本。每次继续开发以实际候选重新确认可达路径；已经修复的项目不重复计入待办。

## 12. 后续实施顺序与退出条件

以下是对既有 P1～P8 工作包的阅读和推进顺序，不另设一套完成编号。票20已完成的内容作为基础；其余工作按仓库原票号接续。本次文档交付不改变已有票的状态。[O01] [O02]

### 12.1 开始前先建立同一个起点

读取实际目标提交、未提交 diff、最新执行/验收记录以及运行 profile。若已超出本文件快照，先修正 §1 和受影响结论，再动代码。不要把旧 spec 顶部保留的 proposed 文案当作“当前没有任何实现”，也不要从某一个新类存在推导整个工作包完成。

开发单元按“可独立审查的完整责任”划分：相关调用者、状态变更、异常收尾、兼容读取、删除清单和验证同批接管。可以分提交审阅，但不能部署两个 owner 共同掌握权威、或新接口只接一半消费者的中间态。

### 12.2 建议推进单元

| 顺序与既有工作包 | 本单元必须交付的行为 | 可以删除什么／何时删除 | 最小退出证据 |
| --- | --- | --- | --- |
| 保持已完成基础与53的准入边界 | 基础票及53已有验收，不重开；依据受管证明解除旧代阻塞，同代仍严格退休，dismiss只提交处置 | 不恢复已被53接管的旧代PGID阻塞，也不删除同代保护；后续消费者复用原合同 | 后续变更影响该边界时覆盖claim→spawn→登记、同代迟到写入与dismiss通知失败，不把旧验收当本次执行 |
| P3/P4：durable 接纳与派发 | 完整输入及身份一次接纳；COMMIT 后才可执行；同键重送不产生第二执行；明确拒绝与 unknown 按已定合同处理 | 新协议接通全部来源和消费者后，删旧内存队列的权威角色及 reserve/start/settle 的重复事实 | 接纳响应丢失、并发同键、旧数据迁移、恢复后继唯一性、commit 前不派发 |
| P4：当前调用结束与资源退休 | 固定RunBinding；按真实CLI通道收尾本轮receipt，Claude idle只追加原来源消息；下一请求先退役旧held再安装新上下文，普通委托保持可访问 | 完整接管本轮/资源事实后删对应callback writer、跨请求重绑和补写；不引入交互宿主或报告调度 | 本轮完整输出、idle不回写旧响应、held Stop、旧事件/定向Stop不影响新请求、会话保留与合法归档 |
| P5：Wiki 绑定、取消与一次恢复 | 显式实例注入；原 ExecutionRef；正常/取消/启动统一核验；每 vault 每代按既定顺序恢复；不重放未知效果 | 替代路径完整后删 fallback singleton、对应 witness/poller、在线 recording 补偿与存储 backoff | 部分 applied、无 receipt、外部编辑、取消后线程完成、重启再中断均可解释 |
| P5/P6/P7：明确边界与测量优化 | 接续既有 CR 覆盖；修真实重复工作、弱随机回退及已证实不可达分支；保留动态权限和内容合同 | 只删被证明重复或不可达的代码；不为本单元加持久缓存/通用框架 | 相关协议回归与所优化路径的前后成本；不得以减少断言换绿色 |
| P8／票51：整体迁移与发布 | 按实际最终候选核迁移、恢复、发布与用户可见行为 | 所有旧 writer 删除与兼容方案均有证据后完成最终切换 | 仓库适用的完整门禁及实际发布记录；票20记录不替代它 |

“最小退出证据”描述需要证明的行为，不要求为表格每一格新建一套测试。优先复用仓库已有等价覆盖，只有具体缺口才新增。

### 12.3 后续开发应明确留在范围之外

当前不追加新的执行数据库、第二套 durable ledger、通用补偿框架、全量 JSON 规范化迁移或独立 benchmark 平台。遇到当前范围外的平台、provider 或存储需求，先明确其产品合同，再另行评估。

也不以快照回滚作为已发生外部效果的通用恢复：数据库回退不会撤销文件、远端工具或已交付消息。延续执行计划的兼容读取、向前修复和已有证据恢复方案；遇到未知保留原事实。[O01]

## 13. 有针对性的验收矩阵

本节是后续代码改动的验收指导。本次只进行了源码/文档核对，没有执行以下故障注入或项目测试。既有测试应先按新候选确认适用；旧测试过度 mock 掉真实失败边界时，才补足缺口。

| 边界与具体场景 | 必须观察的结果 | 不能作为通过依据 |
| --- | --- | --- |
| SQL 已入 aiosqlite 队列后取消等待 | 驱动任务由事务边界收束；确认整笔事务提交/回滚及游标收尾后才复用连接；无法收束则进入真实 fatal | 仅断言捕获 CancelledError 或 mock commit 抛错 |
| RETURNING 已返回但 COMMIT 未确认 | 尚未发布 durable accepted/claim，也未开始依赖它的外部动作 | 只检查 UPDATE 返回行 |
| 权威存储失败触发 fatal | 在隔离子进程中可见非零退出；受管测试确认后代结束和重启门生效 | monkeypatch 后只看到 fatal 状态或日志 |
| claim 后、PGID 登记前退出，再经受管冷启动 | 原 unknown 保留；旧代已退休的会话不被缺失 PGID 永久堵塞 | dismissed 过滤，或全部 managed 检查直接 return |
| 同代仍有迟到 spawn/stdin/工具写入 | 原 owner 收束捕获资源，冲突新执行不能提前开始 | 会话显示 idle、terminal 已写或 waiter 已退出 |
| dismiss 提交后原 owner 已退休／通知失败 | 返回所承诺的持久处置结果；不恢复失败 owner；unknown 不变 | 通知成功才承认处置、重建 owner 让调用通过 |
| cancel 的 HTTP waiter 断开 | 原取消工作仍完成退休和结算；后续查询准确 | 请求协程结束即认为执行已结束 |
| Wiki 部分 applied，或已调用但缺 result/receipt | 保留原效果证据与未确定部分；不自动 done、不重跑模型 | 任一 history 行存在、进程退出或没有回执 |
| 操作等待期间授权/revision/目标变化 | 真正写入遵守当前合同，冲突可解释且不错误升级为存储 fatal | 仅测试入口校验曾通过 |
| 接纳响应丢失或通知重送 | 相同输入不重复执行；已提交业务结果保持；交付错误独立报告 | 前端收到某次成功消息 |
| 历史拒绝、unknown 或旧 JSON 迁移再次中断 | 重启可继续解释原事实，不伪造 accepted，不重复外部动作 | 只运行一次正常迁移 |

现有 `test_session_owner_review_fixes.py` 已覆盖 dismiss 与失败/退休 owner 的部分行为，可作为复用起点；它不自动证明 §5 的跨重启窗口已验证。[O22]

验收记录至少注明候选版本、实际执行的范围、结果及未覆盖限制。一次修复涉及结构性边界时，先完成对应责任切换再跑相应联测；不把不同时点的零散绿色数量累加成同一候选通过。日常定向验证、批次结束/代码提交前完整检查及同版证据复用统一按 [AGENTS.md](../../AGENTS.md#verification)；开发中只有具体剩余风险需要时才提前扩大验证。

## 14. 面向开发者和编码模型的工作方法

### 14.1 先写一个短失败合同

对会跨事务、执行或外部效果的 API，用下列问题界定合同即可，不要求为每个普通 helper 写模板：

1. 谁拥有状态和正在运行的工作？
2. 哪个时刻可以承诺成功？
3. 失败后哪些效果已知、哪些未知，什么仍可继续？
4. 调用方取消时，谁完成实际收尾？
5. 若可重试，安全重复的依据、负责人和预算是什么？
6. 用户如何看到失败，以及可执行的下一步是什么？

例如 dismiss 的合同可以只有一句：**处置以数据库提交为成功点；后续通知失败不撤销该成功；它不改变原执行结果或资源状态。**据此即可决定哪里 catch、哪里传播以及哪里需要测试，而不必增加新状态机。

### 14.2 每个新增机制回答三个问题

| 问题 | 可接受的回答示例 | 应继续简化的信号 |
| --- | --- | --- |
| 它对应哪条真实失败边界？ | 驱动在线程内继续执行，调用方已被取消 | 只说“以防万一”或“更健壮” |
| 谁是唯一 owner？ | 事务上下文拥有 driver；原 execution 拥有 retirement | 三层各保存 pending 状态，任何一层都可补写 |
| 有了它能删除什么？ | owner 接管后删除旧 callback writer 与派生字典 | 新路径全上了，旧路径仍可独立改变同一事实 |

必要的动态核验不是过分防御：外部编辑、取消后的线程工作、迟到结果以及跨资源提交都可能实际发生。过分防御主要表现为对同一个已经建立的保证重复回读、重复状态和多层补偿。

### 14.3 一个可直接用于后续任务的短模板

```text
目标：完成既有执行计划中的 <票号/工作包>，只解决 <具体责任边界>。
起点：核对当前提交、工作树、最新验收记录及本指南受影响章节。
合同：成功点为 <...>；失败后仍成立 <...>；未知效果保留 <...>。
归属：<唯一 owner> 接管 <调用者/状态/I/O/完成消费者>。
删除：接管后移除 <旧 writer/补写/派生状态>；保留 <跨资源证据>。
验证：复用 <已有覆盖>，补 <真实缺口>；记录候选与适用范围。
交付：说明行为变化、删除的机制、仍有限制，并同步原执行/验收文档。
```

任务中如果出现两个互相竞争的事实来源，优先完成职责迁移。不要用测试特判、无条件返回成功、扩大 catch 或弱化断言掩盖未完成的迁移；也不要为每个新反例增加一条永久运行的补偿路径。

### 14.4 错误报告应帮助继续工作

用户可见报告应说明受影响操作、已知影响、仍未知的部分、当前是否允许新工作及可行下一步。用现有 UI/日志通道呈现；不为统一文案新建框架。

例如“历史执行结果未知，但上一代执行已退出；可以查看文件结果并发起新工作”与“原执行仍在停止，暂不能启动冲突工作”分别表达两种实际状态。不要都简化为“操作失败，请重试”。具体可用动作必须与当前代码已经实现的合同一致。

内部诊断保留原 cause、稳定请求/执行身份和必要上下文，沿用项目日志规则处理真实凭据。不能因为用户文案需要简洁而丢弃开发所需证据。[O01] [H01]

## 15. 文档维护与本次更新记录

### 15.1 维护顺序

项目的实现事实以目标源码为准；完成声明以对应候选和适用验收为准；未来取舍以最新已确认规范/执行计划为准。旧报告出现冲突时，保留其问题来源并记录裁决，不把两个相反建议同时留作当前待办。

后续每完成一个相关责任切换，同步更新：

- §1：实际源码版本、完成状态、适用证据和仍未验证的边界。
- §5～§9：改变的成功点、执行归属和恢复行为。
- §11～§13：已删除机制、剩余工作与对应验证；原仓库任务账本同步维护。
- §16：新的固定提交来源；保留旧证据与其原版本，不覆盖成“当前全部通过”。

### 15.2 版本 2.0 的实质变化

| 变化 | 对后续开发的影响 |
| --- | --- |
| 将旧架构审阅改为持续开发指南 | 分开当前实现、历史验收、源码推导和未来目标 |
| 补入票20完成及后续26票边界 | 复用现有 owner/事务/fatal，不重复启动已完成的架构工作 |
| 修订 SQLite 与 aiosqlite 取消解释 | 用统一事务收束真实不确定性，去掉“本地就不会未知”的前提 |
| 明确 dismiss、cancel、unknown 与资源退休各自含义 | 修复真正的准入责任，不靠隐藏提示或清空状态绕过 |
| 纳入 Harness 经验并保留实现限制 | 采用 owner、失败合同、checkpoint 和重试治理，不宣称其全面 fail-stop |
| 撤销历史进程扫描、PDEATHSIG、无证据重放及本轮多表化 | 以既有平台保证和必要效果证据减少机制 |
| 增加推进顺序、删除前提和针对性验收 | 后续任务可按完整责任交付，避免一串互补临时补丁 |

原附件与此前版本字节相同，SHA-256 为 `20d28fdf03526bdd1d20436c4db0b982c56cd3187a260a6ea555c147f1119c95`。本次更新是对相同旧审阅的整合与修订，不将附件名称中的 `(2)` 当作已有内容进展。

## 16. 来源与代码定位

### 16.1 Octopus：固定快照

下面均锁定 `85b5b3c99192eb9a85b6bd923cd1be856492ded0`。本次公开远端访问不可用，链接保留为后续具备访问权限时的核对入口；实际核查基于此前已读取的本地快照。链接是否当前可公开打开，不改变本文对证据可见范围的说明。

- [O01：最小机制执行计划，含决策、票20接口、工作包与删除条件][O01]
- [O02：票20实施验收与发布预检记录][O02]
- [O03：最小机制架构规范，含对旧建议的修正][O03]
- [O04：ServiceHealth 与 fatal 出口][O04]
- [O05：运行数据库 transaction 及失败分类][O05]
- [O06：driver 等待、取消收束与游标结束][O06]
- [O07：terminal 的普通事务读取][O07]
- [O08：SessionOwner 的命令与异常边界][O08]
- [O09：main 的共享 health 接线与启动顺序][O09]
- [O10：dismiss 提交与 owner 通知][O10]
- [O11：client_submissions 的 claim、unknown、列表和 dismiss][O11]
- [O12：输入调用链与 require_retired_writers][O12]
- [O13：boot/PGID 退出判定][O13]
- [O14：owner runtime 的 spawn 与进程登记][O14]
- [O15：受管安装、unit 生成与实际配置核验][O15]
- [O16：unit 根 cgroup 的 prestart 检查][O16]
- [O17：Wiki 权威 storage 连接与错误处理][O17]
- [O18：Wiki job 结算、执行退休和 worker 路径][O18]
- [O19：Wiki 通知与 unsettled 取消轮询][O19]
- [O20：Wiki execution 与取消 witness][O20]
- [O21：History 的 prepared 效果核验][O21]
- [O22：已有 dismiss/owner 修复测试][O22]
- [O23：cancel 对原 retirement 的等待][O23]
- [O24：旧架构简化审阅，用作历史问题来源][O24]
- [O25：PendingQuestion 与会话共享类型][O25]
- [O26：WikiTasks 及仍在的 singleton][O26]
- [O27：浏览器输入 ID 与弱随机 fallback][O27]
- [O28：运行数据库连接、PRAGMA 与初始化][O28]

### 16.2 DeepSeek Harness：固定快照

下面均锁定本次重新 fetch 后的 `687f282e8625c6b8d11ff58211a90524be2e4317`。引用的是所列规范与实现，未对整个仓库错误处理作全面正确性证明。

- [H01：错误处理规范][H01]
- [H02：工程约束中的真实边界与类型信任][H02]
- [H03：防御模式、所有权与结束时序][H03]
- [H04：checkpoint policy 实现][H04]
- [H05：checkpoint policy 的组合前提与限制][H05]
- [H06：中断工具调用的 NOT_STARTED／OUTCOME_UNKNOWN 修复][H06]
- [H07：工具调度的开始与取消收束][H07]
- [H08：模型请求 retry policy][H08]
- [H09：retry 插件的取消、dispose 与活动工作 drain][H09]
- [H10：JSONL storage 的失败 drain 与缓冲处理][H10]
- [H11：JSONL append 失败后的长度恢复][H11]
- [H12：agent loop 的错误范围与 turn 收尾][H12]

### 16.3 平台原始资料

这些资料用于解释平台语义，不替代针对 Octopus 实际安装版本和部署的验证。

- [R01：SQLite 事务、COMMIT 与错误处理][R01]
- [R02：SQLite RETURNING 的处理与 ACID 说明][R02]
- [R03：aiosqlite 的每连接线程/队列模型][R03]
- [R04：aiosqlite 的请求队列与后台执行源码][R04]
- [R05：SQLite synchronous 说明][R05]
- [R06：Python asyncio shield 与取消][R06]
- [R07：Python subprocess 的 preexec_fn 限制][R07]
- [R08：Linux PR_SET_PDEATHSIG 语义][R08]
- [R09：systemd.kill 的控制组终止行为][R09]
- [R10：systemd.service 的重启策略][R10]
- [R11：Linux cgroup v2 资料][R11]

[O01]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/plans/minimal-architecture-execution-20261020.md
[O02]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/review/session-owner-20-acceptance-20261021.md
[O03]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/review/octopus_minimal_architecture_spec_20261020.md
[O04]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/service_health.py
[O05]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/db/base.py#L368-L419
[O06]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/db/transaction.py#L45-L89
[O07]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/db/sessions.py#L519-L527
[O08]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/owner.py
[O09]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/main.py
[O10]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/turns.py#L75-L92
[O11]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/db/client_submissions.py#L95-L187
[O12]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/client_submissions.py
[O13]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/execution_identity.py#L14-L26
[O14]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/owner_runtime.py#L1251-L1273
[O15]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/service_installation.py
[O16]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/service_scope.py#L59-L68
[O17]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/storage.py
[O18]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/jobs.py
[O19]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/job_notifications.py
[O20]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/job_execution.py
[O21]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/history.py#L324-L338
[O22]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/tests/test_session_owner_review_fixes.py#L62-L118
[O23]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/turns.py#L616-L626
[O24]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/review/architecture-optimization-20261020.md
[O25]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/base.py
[O26]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/wiki/tasks.py
[O27]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/web/src/lib/submission.ts#L21-L35
[O28]: https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/db/base.py
[H01]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/.agents/skills/dsh-error-handling/SKILL.md
[H02]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/AGENTS.md#L143-L150
[H03]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/defensive-patterns/general.md
[H04]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-checkpoint-policy/src/index.ts#L29-L82
[H05]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-checkpoint-policy/README.md
[H06]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/repair.ts
[H07]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/tools/src/index.ts#L1557-L1598
[H08]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/retry-policy.ts#L14-L57
[H09]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-retry/src/index.ts#L194-L258
[H10]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-persistence-jsonl/src/storage.ts
[H11]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-persistence-jsonl/src/index.ts#L1406-L1445
[H12]: https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts
[R01]: https://www.sqlite.org/lang_transaction.html
[R02]: https://www.sqlite.org/lang_returning.html
[R03]: https://aiosqlite.omnilib.dev/en/stable/
[R04]: https://github.com/omnilib/aiosqlite/blob/main/aiosqlite/core.py
[R05]: https://www.sqlite.org/pragma.html#pragma_synchronous
[R06]: https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation
[R07]: https://docs.python.org/3/library/subprocess.html
[R08]: https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html
[R09]: https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html
[R10]: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Restart=
[R11]: https://docs.kernel.org/admin-guide/cgroup-v2.html

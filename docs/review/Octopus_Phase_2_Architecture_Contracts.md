# Octopus 第二阶段实施方案：架构与接口收敛

> **历史设计输入，已被 2026-10-31 的确认设计修订。** 当前第二阶段执行范围及 G2
> 出口以[工程路线第二阶段](../plans/engineering-runtime-roadmap.md#phase-2)为准。
> 本文的 C4 首输入持久扩展、C5 最终原生 item 引用和完整 S6 网关反射替换已被
> D3/D4/D6/D11 替代，不再作为并行实施要求；其余正文保留当时论证。

实施顺序：[第一阶段](../plans/engineering-runtime-roadmap.md#phase-1) → **第二阶段（当前范围见上）** → [第三阶段](Octopus_Phase_3_Codex_App_Server.md) → [第四阶段](Octopus_Phase_4_DeepSeek_Integration.md)。每阶段先达到自己的出口，再开始下一阶段产品改造。

> 编制日期：2026-10-30。状态：**实施设计，尚未实施或验收**。\
> 输入：用户附件《Octopus 工程收敛与运行时路线》；本次只读核对仓库提交 `0ca897e97189c5f213a658d986cbd67f4d69c5e3`。该提交与附件 §7.3 的补充核查基线一致。本文没有运行产品测试、真实模型、发布或生产数据操作。\
> 串行位置：第一阶段通过 G1 → **本阶段通过 G2** → 第三阶段 Codex 完整迁移通过 G3 → 第四阶段 DeepSeek 接入通过 G4。\
> 本次建议将附件中的 S1、S2、S2b、S3、S4、S5、S6、S7、S8 选入第二阶段。原附件把部分条目标为候选；本文将它们展开为可执行的建议方案，不把“提出设计”写成“代码已经实施”。

## 0. 这一阶段到底交付什么

第二阶段的成果是：**既有 owner 对外提供完整业务操作，调用方不再负责拼锁序、原回执确认、完成判据或资源退出步骤。** 它不负责把 Codex 切换到 app-server，也不接入 DeepSeek；它负责让这两项迁移只改变真实的运行时适配与装配，而不会迫使 Wiki、App、Research、委派和前端再次学习 provider 私有协议。

不以新建多少接口、拆出多少文件、删掉多少代码判断完成。每个工作包必须同时交付实际入口、全部受影响消费者、失败路径、行为证据和旧路径删除。一个新接口下面仍要求调用方取得同样的锁、传入同样的内部快照，不能算架构收敛。

核心实施顺序是：先把第一阶段已纠正的行为纳入共同返回与配置接口；再把运行时证据、原执行退休和单轮结算收回正确 owner；然后完成 Wiki 业务边界、聊天读取投影和安装排空。阶段内存在可以并行开发的独立工作包，但**阶段之间仍严格串行交付**。本文末尾给出合并顺序与 G2 交接清单。

### 0.1 与其余三阶段的责任关系

| 内容 | 第一阶段交给本阶段 | 第二阶段交付 | 第三、四阶段使用方式 |
| --- | --- | --- | --- |
| 返回投递 | 永久拒绝、暂时拒绝、unknown、冲突的已验证行为 | S1 单一接管入口与原 receipt 观察 | 新 backend 不参与 BG/Research/命名委派的排队政策 |
| 配置 | 允许改 Agent 默认；候选配置与实际有效配置正确校验 | S3 统一组合、保存、捕获及效果前授权边界 | 各 adapter 安装捕获配置；新增能力只进入原能力入口 |
| Wiki | compact 必要数据、诊断快照、性能与语义基线 | S5 受保护资源操作；S6 transport 无关的业务操作 | runtime 更换不能改变阅读证明、grant、文件效果与 retry 门 |
| 完成 | 现有行为和 Codex 完整尾部缺口的反例 | S4 证据合同；S2b 单轮结算编排 | P3/P4 提供真实协议证据，不修改业务消费者的完成规则 |
| 资源退出 | 已有原资源责任与失败保持 | S2 按原执行查询；S8 排空事实 | 新进程、RPC、问题和迟到 spawn 都进入同一 owner |
| UI | 原有消息、问题、任务投影行为 | S7 一次加载的身份、基线与应用规则 | UI 接受归一化事件，不解析 Codex/DSH RPC |

### 0.2 本阶段范围的强弱依赖

S1、S3、S2、S2b、S4 是运行时迁移的直接接口基础。S8 使新资源能被发布交接看到。S5、S6 和 S7 有自己的真实复用收益，不应被伪称为 Codex 协议的技术硬前置；但按本次建议的整体交付范围，它们仍在 G2 前完成，避免把已选定的清理留给后续阶段。

协议调查可以提前：锁版 schema、假 provider、最小原生实验用于反证公共接口。**不能因为 G2 使用假运行时验证通过，就宣称 Codex 已能完成受管后代闭合。** G3 的真实协议证据仍是独立门槛。

## 1. 实施前先固定九条不变量

1. BG、Research、命名委派的终态及问题返回仍按**成功接纳顺序**逐条派发；不改为合批、自动 steer、notification-only，也不增加正常运行时的人工优先策略。
2. 普通 Stop 暂停派发并保留 backlog。暂停后新的人工纠偏可以先执行；它不自动恢复积压。显式恢复才继续原顺序。
3. 原生后代属于当前逻辑请求；独立 BG/Research/命名委派拥有独立工作身份。完整原生尾部不意味着等待所有独立工作。
4. 本地输入接纳、provider 发送、provider 接受、模型处理、业务提交、通知送达、资源退休是不同事实，各自使用真实证据。
5. 所有异步回报保留原 account/session/request/work/attempt/backend 与实际资源绑定；不能在回调执行时读取“会话当前请求”替换原归属。
6. 一个业务事实只由现有权威写入。主库持久输入与 terminal、Wiki History 文件效果、来源业务结果不合并成一个泛化任务状态。
7. Agent 默认允许改变。保存校验、请求使用校验、请求捕获与效果前授权分别承担不同责任；捕获配置不能延长已撤销授权。
8. 不确认发送，不自动换 attempt、换 backend 或重跑。存储失信沿统一 fail-stop；不增加同代回读猜提交的恢复循环。
9. 不增加第二个 SessionManager、全局任务总线、通用持久任务树、执行表、ConfigurationManager、万能 WikiService 或全仓 strict 迁移。

上述不变量从第一阶段继承；本阶段调整归属，不重新表决产品行为。依据：附件 §1、§3、§6，以及固定版本的[架构合同](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/plans/architecture-contract.md)。

## 2. 统一的事实、owner 与提交点

### 2.1 权威分工

| 事实 | 唯一权威或 owner | 可公开的操作/事实 | 不能转交给谁 |
| --- | --- | --- | --- |
| 返回正文与来源业务结论 | BG / Research / Delegation | 可重复准备原正文；来源是否仍要投递 | `admit_return` 不代来源结算或重试 |
| 接纳、去重、顺序、输入执行归属 | 原 SessionOwner + Database 输入事务 | 原 receipt；接管或明确拒绝 | adapter、UI、来源自建队列 |
| 单轮活动、问题与业务结算选择 | 原 SessionOwner / OwnerRuntime | 原执行完成观察；问题回答/关闭 | Manager 回读当前执行并重新选择结果 |
| terminal 与输入关联的提交 | Database 的既有事务 | 原提交确认与持久结果 | publisher、内存 Future |
| 通知前缀与送达 | 原 publisher | delivered / failed / unavailable | provider result 或 UI 卡片 |
| prepare、spawn、I/O、进程组、lease、临时秘密与退出 | HarnessRun 及原资源 owner | 原资源退休证据与等待 | Wiki 读私有集合；Monitor 判完成 |
| provider 亲缘、活动与原生协议语义 | 私有 adapter | 原生事实归一化；可证的 completion scope | 通用层猜协议或 UI 推断任务树 |
| Wiki 注册、Store 代次、借用和退休 | Registry | 同代受保护资源使用 | router 自行组合 registration/account 锁 |
| Wiki 文件效果、原像与恢复 | History / Store 的既有协议 | operation receipt、revision、事务结果 | 模型文字、job 的展示状态 |
| 一次聊天读取的身份和合并 | 现有加载/投影模块 | load、event、取消旧读取 | 页面自行拼基线；新增第二个 store |
| 安装是否可封口 | 安装 owner 组合各域事实 | 同步检查并关闭 producer | 一个推测性的全局活动计数 |

Owner 的命令裁定仍串行；模型、RPC、spawn、读取、用户回答等待、退出等待在受管 I/O task 中推进。不要把“只有一个 writer”实现成“Owner 持锁等待所有慢操作”。

### 2.2 C1–C8：后续阶段共同使用的语义索引

以下名称用于四份方案交接，**不是要求新增八个类、八张表或一个统一协议包**。优先扩充现有 `SubmissionReceipt`、`RequestCompletion`、`HarnessEvent`、`RuntimeProfile`、`HarnessRun` 与 owner 中已有类型。

| 编号 | 语义 | 最小内容与约束 |
| --- | --- | --- |
| C1 原始绑定 | OriginalBinding | 原 account/session/request/work/attempt/backend；原 resource/lease 引用由 Harness 持有。已有字段不复制一份持久权威 |
| C2 返回接管 | AdmissionResult | 已确认的原 receipt / 本次明确未接纳 / 无法确认。沿已有 `ReturnTarget` 等拟议类型；历史 unknown 不被后续拒绝覆盖 |
| C3 有效配置 | EffectiveConfig | 不可变的实际继承结果、配置来源、凭据身份和运行范围；实时权限不是快照的一部分 |
| C4 输入证据 | InputEvidence | 首输入和追加共用原输入证据权威；本地接纳、完整帧写出、provider 接受、认领/持久消息/实际处理分别表述；终态记录不能抹掉接受未知 |
| C5 完成证据 | CompletionEvidence | 声明所证明的范围、原生闭合依据、根/后代/父续跑状态、业务 outcome 与所选根最终回答的原消息引用；主输出和根 turn 结束不能代替请求完成 |
| C6 退休证据 | RetirementEvidence | 对原执行资源返回 retired / pending / unknown；退出失败保留责任；publisher/通知另行组合，不暗含于 retired |
| C7 问题绑定 | QuestionBinding | 产品 question、结构化原答案与原 connection/server-request/thread/turn/item 或原生回调的对应；文本仅为展示/旧 MCP 投影，晚答不能作用于新请求 |
| C8 排空事实 | DrainFacts | 各 owner 从真实责任推导是否可交接；安装层无 await 检查和封口，无持久 idle 缓存 |

“可观察证据逐步增加”不等于必须新建一张所有阶段共用的事件表。例如 C4 可直接利用既有输入行和受管发送对象；C6 可由原资源引用及已有持久退休证据组合。只有发现缺失的持久语义且给出真实恢复场景，才修改格式及对应搬迁合同。

### 2.3 关键提交边界

| 边界 | 提交前 | 提交后 | 提交之后失败怎么处理 |
| --- | --- | --- | --- |
| 合法归档唤醒 | 检查原归属与当前资格 | 原会话恢复成员关系 | 后续输入未接纳不自动把会话重新归档 |
| 本地输入接纳 | 原 Owner 校验、唯一性及正文冲突 | 完整输入及接纳事实有确认 | 丢 ACK 只观察原 receipt，不重跑来源 |
| terminal | 结果选择可在 owner 内暂存 | `record_terminal` 提交已确认 | 投影/通知失败不改写已提交 outcome |
| Wiki job 接纳 | 同代 Store、当前授权、revision/观察校验 | 原 job、grant、replay key 一致提交 | `worker.wake` 失败不撤销已提交 job；沿原唤醒恢复路径 |
| Wiki 文件效果 | 原像与 prepared 协议 | History/operation receipt 证明效果 | 取消或模型失败不撤销真实效果证据 |
| 安装封口 | 所有域同步事实为空闲 | 同一同步段关闭准入 | 封口后不能再次接纳；退出仍按原资源证明 |

## 3. S1：统一返回接管，保留各来源重试

### 3.1 现状与入口

固定基线的 `sessions/turns.py::deliver_bg_result`、`research/manager.py::_inject_report`、`delegations.py::_inject_terminal/_inject_question` 仍分别安排原 receipt、返回归属、归档唤醒和输入提交。已存在 `source_receipt`、`resolve_task_return` 和持久接纳机制；本阶段复用它们的实现责任，收回调用顺序。

现有 `architecture-contract.md` 已给出 `admit_return` 设计，本实施方案沿用其类型，不另创一个竞争性接管接口。第一阶段修好的 `AdmissionIntegrityError` 分类进入该接口的规范；原文中的 reason 枚举应随实际错误类型更新，不能丢失永久拒绝这一维度。

### 3.2 建议接口

以下为拟议接口，实施时优先采用仓库已有命名与类型：

```python
@dataclass(frozen=True)
class ReturnTarget:
    session_id: str
    source: Literal["bg", "research", "delegation"]
    delivery_id: str
    work_id: str | None
    owned: OwnedTaskContext | None

@dataclass(frozen=True)
class ReturnBody:
    prompt: str
    question_session_id: str | None = None
    question_id: str | None = None

async def admit_return(
    target: ReturnTarget,
    *,
    prepare: Callable[[], Awaitable[ReturnBody | ReturnSkipped]],
) -> SubmissionReceipt | ReturnRefused | ReturnUnconfirmed: ...
```

入口留在现有 `sessions/turns.py`；轻量值类型放 `sessions/submission.py`；`SessionManagerBase` 补充所属调用合同。完整性拒绝可以保留明确异常，也可以成为有类型的永久拒绝结果，二者选定一种并覆盖全部来源；不得退化成 `bool`，也不把程序错误全部包装成拒绝。

`ReturnTarget` 由可信来源从原记录构造。保留 delivery 命名：`bg:{task_id}`、`research:{job_id}`、`agent-terminal:{work_id}`、`agent-question:{work_id}:{question_id}`。这些是现有身份，不再新发一次投递 UUID。

### 3.3 完整顺序

1. 用原 session/source/delivery 查询 receipt。存在即返回原观察；不用当前正文，不唤醒、不重新入队，也不要求来源材料仍在。
2. 无 receipt 时判断原绑定及当前准入。来源关闭、work 不匹配、服务关闭等在其权威边界拒绝，不从异常文案猜测。
3. 调用 `prepare`，每次接管调用至多一次。其异步读取不持 Owner mailbox、生命周期锁或数据库事务；并发调用可以重复读取，因此 prepare 不结算、不发通知、不启动工作、不改授权。
4. prepare 后再次查询原 receipt。另一调用已经接管时，返回它，不继续使用刚准备的正文做第二次提交。
5. 检查 `ReturnSkipped` 或正文及问题配对。来源跳过是“本次不新增输入”；不能覆盖已经存在的原 receipt。
6. 由现有 lifecycle 合法唤醒；恢复等待后重验原身份及当前资格。唤醒和输入接纳保持两个提交点。
7. 原 Owner 用既有输入事务最终接纳、去重或拒绝。实际发生的同身份不同正文 `SubmissionConflict` 原样暴露，不能回查 receipt 后吞掉。
8. 确认链路中断且存储仍可信时核对原 receipt；没有可靠结论则 `ReturnUnconfirmed`。存储本身失信时直接进入原 fail-stop，不把该故障变成循环查询。

### 3.4 结果处理矩阵

| 观察 | 来源行动 | 不允许的变换 |
| --- | --- | --- |
| 原 receipt | 继续等待/观察同一 completion | 再造输入或重新读取已丢失正文 |
| 明确 busy / capacity | 沿原 delivery 和原来源预算重试 | 重跑 shell/agent/research |
| 本次永久完整性拒绝 | 停止自动投递，保存正文、错误和来源业务结果 | 把成功来源改成失败执行 |
| 来源撤回/问题已关闭 | 结束本次新投递；原 receipt 仍优先 | 用问题关闭取消已接纳的其他输入 |
| 提交交接可能发生、ACK 丢失 | 保持原身份与 unknown，只观察原权威 | 先改为 rejected，再用新 ID 重提 |
| 实际正文冲突/程序错误 | 保留异常及诊断，终止普通重试 | 转成 duplicate、success 或 busy |
| 等待者取消 | 取消当前等待；已接管 Owner 命令继续受管 | 清掉 original receipt 或原执行引用 |
| 存储失信 | 既有 fail-stop | 同代猜提交、补写或自动重放 |

若第一次未知、第二次明确拒绝，第二次结论只证明第二次没有新增接纳；第一次仍未知。这是来源保留观察历史的语义，不要求新增一张 delivery-history 表。

第一阶段交付的 BG 原记录 `delivery_disposition`（nullable，`stopped` / `unknown`）和 `delivery_error` 在本阶段保留为**来源异常投递说明**。接管模块不复制一份成功 receipt、request ID 或 accepted 状态到 BG。NULL 不表示成功或可自动重放；重启不自动重放已终态 BG。委派继续沿原 `_terminal_disposition` 和第一阶段的 `terminal_delivery_error` 表达来源处置与原因，Research 保留原报告/投递记录。S1 的统一范围是接纳编排，不能因此删除来源错误可见性。

来源进入一次新接管调用前，必须先遵守第一阶段已有的停止/unknown 状态。**unknown 后不重新调用一个可能执行提交的路径。** 只走既有 `source_receipt` 的纯观察能力（或同语义的公开只读封装），直到原 receipt 被确认；查不到继续 unknown。这不是让调用方重新拼接接管顺序，也不是把 `admit_return` 拆成两阶段写入接口。`stopped` 不因材料仍在、会话恢复或默认配置修复而自动重放。

### 3.5 消费者迁移与删除标准

| 消费者 | 保留 | 替换后必须删除 |
| --- | --- | --- |
| BG `deliver_bg_result` / `_settle_task` | 输出组织、业务结果、退休、来源交付循环 | bool 混合表达；查询→恢复→提交的重复步骤 |
| Research `_inject_report` | 报告、queued/processing/processed 投影、unknown 时纯 receipt 观察、completion 观察 | 读取 `_admission_closed` 与用于新接管的重复查询/恢复/提交分支 |
| Delegation 终态 | 原轮次、精确 terminal 正文、来源重入保护 | `_return_context` 中共同准入编排 |
| Delegation 问题 | 问题是否仍有效、原 question 关联 | 独立复制接管顺序；空正文表示跳过 |

Wiki outbox、schedule 首次触发、App、人工输入保留原入口。`source_receipt` 和通用 `start_message` 不能因三个来源迁走而机械删除。

### 3.6 验收

首先执行“提交已成功→确认丢失→原 Owner 失败→正文被删除→以原身份再次观察 receipt”。正确结果是一条持久输入、原 receipt、来源结果保全，观察路径没有新提交；公共 `admit_return` 的重复入口另验证已有 receipt 快路径未调用 prepare。再覆盖并发不同正文、prepare 时归档/撤权、已 Stop 会话被合法唤醒、问题过期、容量拒绝、等待者取消及存储失信。

测试使用真实临时 SQLite 和可控运行时，通过公共接管入口查看输入、receipt、来源记录与模型启动次数。优先扩充 `test_durable_independent_returns.py`、`test_source_return_owner_failure.py`、`test_delegation_return_retry.py` 等既有面；只有重复 helper 形状测试被公共行为覆盖后才删除。

## 4. S3：统一有效配置的组合、修改与请求捕获

### 4.1 现状与目标

`routers/agents.py::update_agent`、`routers/sessions.py::update_session`、`routers/credentials.py` 分别持 `configuration_lock`，安排更新及影响处理；`OwnerRuntime.capture_configuration` 又把配置复制为一个 `Session`。第一阶段先纠正保存规则，本阶段再收回组合和锁序知识。

不能把整个 Agent、Session、Credential 生存周期交给一个新大管理器。建议在既有会话/配置模块内提供三个薄业务操作：保存 Agent 默认、保存会话覆盖、捕获本次有效配置；底层仍调用 AgentManager/UserManager/Database 的既有权威。

### 4.2 一份有效结果，四个时间点

| 时点 | 负责的问题 | 允许失败的后果 |
| --- | --- | --- |
| 保存候选默认 | 该 Agent 自身 backend、credential、model 控制项、工具策略是否有效 | 拒绝无效候选；不能因所有旧会话未适配而拒绝合法默认 |
| 执行前使用 | 指定会话实际继承与覆盖的组合现在是否可执行 | 返回具体可修正原因，保留输入、历史及默认，不静默改 backend |
| 请求捕获 | 当前 request/attempt 实际采用哪组值 | 捕获同一受保护时点的结果；慢准备期间默认变化不混入 |
| 效果前授权 | 当前真实 operation 是否仍被允许 | 撤权及时生效；旧配置快照不是授权凭证 |

会话已有的 backend/history 绑定保持原事实；Agent 改默认 backend 不会让旧会话自动换 backend。model/effort/tier/credential 等字段沿现有逐字段继承；跨 backend 不兼容如实说明并让用户在既有可编辑范围修正。

建议的只读结果包含实际 backend/model/effort/tier、credential ID、cwd、工具策略、Wiki 范围，以及必要的继承来源供 UI 解释。秘密只在运行所需的原资源 owner 中解析，不把完整 secret 放进公共快照、日志或浏览器响应。

现有 schema 没有的 session override 不要顺手新增。例如工具策略来自 Agent，就如实展示来源；没有需求时不扩成每字段一套会话覆盖。

### 4.3 锁与慢准备

在既有 configuration lock 内读取和组合短路径事实，完成本次 capture 或短提交。prompt、memory、connector 网络准备、credential 刷新、进程启动放在锁外，并由原 attempt 负责取消和迟到结果。如果能力发现/凭据预检本身需要慢操作，先准备只读候选，再在锁内校验所依赖的身份或代次没有变；不要把每项配置变化都变成全局串行网络请求。

这里不要求给所有对象新增持久 revision。优先利用当前锁、已有记录身份和真实字段比较；只有出现必须跨锁阶段辨识的变化才引入局部捕获标记。

### 4.4 具体迁移

1. 整理第一阶段已验证的纯组合/校验函数，把 Agent 候选校验与 Session 有效校验区分为明确入口。
2. 让三个 HTTP 路由仅做认证、请求解析与结果映射，不再持有配置锁、遍历旧会话或安排 runtime 刷新。
3. Owner 创建 request/attempt 时使用同一组合结果；替换“复制 Session 再让多个层补字段”的隐式合同，可保留现有容器但不能把可变 run map、锁、live backend 引用带入配置快照。
4. 当前请求沿原 capture 运行；新默认只影响下一次有效捕获。当前撤权与 work 关闭仍立即作用于工具实际准入。
5. 人工 steer 比较原 attempt 的安装范围、有效配置和授权兼容性。模型、effort、credential、工具或 Wiki 范围不兼容则排队，不在活动宿主上偷偷更新。
6. 保存成功、广播失败分别处理；广播失败不回滚已保存默认，也不把错误报告成用户字段未保存。

### 4.5 验收

覆盖合法默认更新使旧会话暂不兼容、用户修正会话后继续；准备中改默认，当前请求不混配、下一请求使用新值；原地换 key、凭据删除/撤权；两个账户同时选择外观相同的模型但使用各自 credential；工具限制不支持时明确失败；自定义模型名未被列表当成白名单禁止。

G2 交给 P3/P4 的不是“默认值冻结保证”，而是**配置选择时点和授权时点固定**的保证。

## 5. S4：用证据定义运行时接口，不统一虚假的能力

### 5.1 已确认的收敛点

固定基线中，`HarnessRun.reusable` 从 `StdinMode.STREAM_JSON` 推导；`OwnerRuntime._consume_runtime` 又以 `backend.reusable` 推导 `_steer_open`。这把传输形态、轮内输入和跨请求复用混在一起。`RuntimeProfile` 已有真实的 `ToolPolicySupport`，可以沿同一思路分别表达其他能力，但不能堆成未经验证的布尔真相表。

先收敛操作语义，再决定最少实现形状。不要提前把 Codex、Claude、DSH 都强制塞进一个 JSON-RPC dispatcher：Claude 流式输入没有与 Codex 等价的逐输入 ACK；DSH 的 inbox 接纳也不等于模型处理。

### 5.2 操作合同

| 操作 | 输入和结果 | 谁执行、谁观察 |
| --- | --- | --- |
| prepare/start | C1 + C3；获得原资源句柄或明确准备失败 | HarnessRun 持有 prepare/spawn/临时资源；adapter 做协议握手和原生绑定 |
| events | C1 + 归一化文本/完整 item/工具/原生任务/问题/usage/错误/证据 | 一个 stdout reader 解码分派；Owner 处理业务事实；publisher 处理展示 |
| steer | 原输入 ID、预期活动目标、原配置；C4 接受证据 | Owner 判断产品资格；adapter 校验原生活动并发送 |
| answer | C7 + 结构化回答 | Question owner 校验仍有效；adapter 回原通道 |
| completion | C5 原生闭合与 outcome，不直接提交业务 terminal | adapter 提供证据；Owner 作唯一结算选择 |
| interrupt | 对原 scope 发取消，返回取消请求的证据 | adapter 操作原 runtime；不是退休证明 |
| retire | 关闭准入、取消/终止/join 原资源，得到 C6 | HarnessRun/原资源 owner 负责；Owner 不丢未退出引用 |
| reuse | 是否跨请求复用、需要重新安装什么 | 与 steer、双向通信分别声明；首版 Codex/DSH 不建池 |

拟议的能力表达应携“支持阶段、目标要求、证据强度”，而非一个 `can_steer=True` 就覆盖所有情况。静态能力来自 RuntimeProfile/adapter；动态资格来自原 execution。UI 显示支持与拒绝原因，不能自行计算运行许可。

### 5.3 C4 输入证据的最小模型

| 事实 | 它证明什么 | 它不证明什么 |
| --- | --- | --- |
| 原 `SubmissionReceipt` | Octopus 接纳并持久保存了输入 | 已写到 provider 或已被模型读到 |
| 完整帧 write/drain | 宿主尝试已写到本地传输边界 | provider 接受、持久记录或执行 |
| provider ACK | 该协议明确承诺的接受层级 | 不能自动提升为“模型已处理” |
| inbox claimed / 持久 user message | 该 provider 已观察到的中间事实 | claimed 后仍可能在 step 前被取消或拒绝 |
| 完成证据中的输入关联 | 该输入可被归入本次已确定结果 | 其他已接纳但未确认发送的输入也完成 |

内部实现优先在既有发送对象上增加判别联合与证据字段。`unknown` 是未能确认既有尝试的事实，不能变成“retry=True”的错误别名。发送后断连不自动换协议、不另起 turn；P3/P4 的具体恢复能力另以协议证据确认。

**G2 必须把 C4 从当前主要面向 steer 的持久观察扩到首输入，不能留给 P3 临时补。** 固定源码有一个直接影响新 RPC 的接缝：`db/client_submissions.py::set_steer_delivery` 记录追加输入的 confirmed/unknown，`complete_client_submission` 会保留该 unknown；但 `db/sessions.py::record_terminal` 对普通 root 输入直接设置 `execution_state='done'` 并清空 reason。`OwnerRuntime.completion` 又可因 settlement 中存在 outcome 返回 completed。若首输入写出后 ACK 未知，仅记录一个本地 failed terminal 就沿旧路径结算，会抹掉输入的未知处置，恢复列表也可能不再展示该输入。

本阶段按以下范围完成改造：

1. 在**原 SessionOwner + 既有 client_submissions 行**提供首输入与人工追加共用的 runtime input evidence 更新入口；可重命名/收敛现有 `set_steer_delivery`，不增加 app-server 输入表或第二 receipt。持久接纳早已成功，不重做本地 admission，也不把 provider unknown 倒写为本地未接纳。
2. 首输入、追加都使用原 input request ID。写出未确认、ACK 已接受、处理关联由 adapter 提交明确证据，Owner 选择持久观察；DB 不解析 provider 方法名。既有 confirmed 的含义只承诺约定的证据层级，不拿它自动证明模型消费。
3. `record_terminal` 不再仅凭“有一个终态消息”无条件把原输入 done/清 reason。Owner 提供该输入的确定处置；DB 在原 terminal 事务中保存一致结果。存在接受/处理未知时保留 input unknown 及原因，原本明确成功/失败的输入继续一次结算，不回归既有已成功接纳路径。
4. `RequestCompletion` **先判断该 input 的真实证据，再判断承载请求的 outcome**。存在本地已记录的 attempt failure，不等于输入执行结果已经可确认；该 input 可以 `state='unavailable'`，而本地诊断/attempt 记录确已 committed。通过既有 recording observation、reason 与原记录引用区分两者，不能为了表达 unknown 给 `TurnOutcome.status` 增加一个含混第四状态。不能向消费者递送一个看似已确认的 provider outcome。
5. `receipt_for_input`、公开 submission 查询、`list_recoverable_submissions`、启动恢复、withdraw/dismiss/recovery_of 入口及前端未知提示一并迁移。terminal 已存在不能使接受未知从恢复视图消失；dismiss 只隐藏提示，不证明未执行；withdraw/恢复新后继不能借 unknown 自动授权重放。只有原权威取得足够证据才升级观察。
6. 存储失信仍 fail-stop。对正常本地记录已提交但 provider 接受未知的情况，保全原来源、partial、原资源及 unknown；停止等待不删除记录、不新开 attempt。纯查询原权威不等于重新发送。

首要回归：真实 SQLite 中接纳首输入→pipe 写出→丢 ACK→原 attempt 停止并记录本地失败诊断→重启恢复。结果必须仍准确展示该输入接受未知，提交次数为一次，无自动 replay；对 steer 重复同一场景。再验证已确认正常输入/terminal 原子结算、已记录诊断与 unavailable receipt 不相互覆盖。

### 5.4 C5 完成范围

公共层必须区分以下事实，且不要求每个 provider 都具有同样的事件名：

- `main_output_available`：根正文可展示。
- `root_turn_ended`：某个根原生 turn 已结束，状态也已解析。
- `request_scope_closed`：所声明的受管范围无必需后代、父续跑、关键发送或阻塞问题；依据来自可证明的 provider 合同。
- `outcome`：完整根输出与错误支持的业务结论，子失败被父明确处理后仍可能成功。
- `recorded`、`notified`、`retired`：分别来自 Octopus DB、publisher 和 Harness，不能由原生 completed 一并设真。

这些是语义层次，未必都存成布尔字段。适配器可以用一个有类型的完成证据携带原始引用和范围，避免互相矛盾的 flag 组合。

**本 scope 的最终回答选择也归原完成投影。** 一个 attempt 在新协议下可能包含根第一次答复、child 结果、commentary、父续跑及最终汇总。当前 `delegations.py::_inject_terminal` 按 attempt 拼接全部 assistant text 的方式不能作为未来最终答复合同。结算投影须指明所选 scope 根最终回答的**已持久 item/消息引用或精确范围**；完整 transcript 全部保留，父子、阶段和历史身份不被压平。委派、App 等消费者读取原选择，不能各自拼全部 assistant text、截最后字符串或凭 UI 排序猜最终答案。

优先在既有 terminal/message JSON 元数据保存最小有序来源引用，不复制答案正文、不新建结果表。只有引用指向的必要正文已可靠记录，才能以它们组成完整成功结果；记录失败仍依既有 recording 合同处理。失败时可以保全 partial，但不能把 partial 冒充 final。新增持久语义与兼容读取、备份/搬迁范围在 S2b 同工作包交付。

G2 使用可控多 turn 假 runtime 验证此接口：根首次答复 A、子正文 B、commentary C、父最终答复 D 都在 transcript 中；消费者最终结果准确引用 D 或协议所证明的完整根最终 item 集合，不拼成 A+B+C+D。第三阶段负责将该选择绑定到真实 Codex scope 和原生 phase/item 证据；第四阶段不得自行新定义答案来源。

**G2 中旧 exec 只能声明它真实证明的 legacy/root-turn 范围。** 保留现有对外行为的回归，同时明确它尚不满足用户新增的完整原生尾部目标。严禁为了让共同接口“看起来兼容”，把旧 `turn.completed` 包装成 `request_scope_closed`。P3 取得真实证据后，普通 Codex 请求才采用完整范围，旧范围主执行路径随切换退出。Claude 也必须明确现有首 result 和 idle 迟到记录的边界；共同责任改变时同步调整，不把已完成 receipt 后的补写正文冒充完整等待。

### 5.5 C7 问题和答案：保留结构，格式化只用于展示

固定源码 `OwnerQuestions` 在处理 `AnswerQuestion` 时先 `format_answers(...)`，随后 `_answer` 只保存字符串；`questions.py::wait_for_question_answer` 也返回文本。它满足既有 MCP ask 的消费方式，但不能作为后续原生结构化问答的唯一内部答案。若不在本阶段收敛，第四阶段只能错误地反解析 `Q:/A:` 文本。

建议在原 PendingQuestion/Question owner 内保存一次类型化的问题结果，语义区分 `human_answer`、`system_expired`、`closed`（名称为示意，沿实际类型约定）；包含原题目/选项关联、仅在人类回答时存在的结构化 answers、来源、reason 与展示文本。已有用户答案校验在该 owner 内完成；原 native 请求的 question/choice 标识映射由 adapter 在创建问题时保留，不能用 UI 文案猜 ID。

- 原 MCP ask 的等待函数继续读取同一结果的文本投影，保留兼容，不新建第二个 Future 或问题账本。
- 原生 adapter 读取同一结果的结构化字段，按自身协议映射并回复原通道；保留多选、自定义文本、稳定题目/选项 ID。
- **本阶段选定持久保全方式**：在既有 `question_answer` 消息的 `tool_input` JSON 元数据中保存校验后的结构化原答案、schema 标识和回答来源，例如 human answer 的 `origin=human` 与系统过期的 `origin=system`，并分别保留 `kind=human_answer/system_expired` 和原 reason；展示 `content` 保留文本投影。没有单独 answers 表。关闭仍保存原 `question_closed` 事件及其 reason/origin，不为关闭伪造一条用户 answer。结构化题目/选项映射沿原 question request 的记录引用；不要存 bearer、活 socket、callback 或可重新使用的授权。
- 来源不能从文本猜测。系统 expiry 的自动文本明确为系统结果，`answers` 不伪装为人类选择；native 工具桥若能保留其来源，可把它作为原工具调用的 system-origin 结果返回。要求纯人类答案的 native schema 不得硬塞 custom 字段，应按已选合同取消/关闭；两种处理都不新建 turn/steer。重启后这些记录是已发生答案/关闭的证据，**不恢复 callback、不重开问题、不重发答案**。
- 这是持久语义变更，在同一 P2-04 更新所属存储合同、兼容读取、完整备份与必要格式验证。是否需要改格式号由现有完整格式规则裁定；不因 JSON 能装下就省略合同，也不新增启动自动迁移链。
- deadline 是原问题合同的一部分。有限 deadline 按已有行为；没有产品 deadline 的问题保持可取消的无截止等待，不为适配 provider 静默增加时限。
- 关闭、Stop、过期、原 attempt 结束和晚答分别映射。不得把关闭说明或自动回答提示词伪造成用户选择，也不把原生 answer 作为新的 prompt 或 steer。
- 后代问答是否可用由实际原生通道证明。公共接口可表达原绑定，不等于每个 adapter 首版都支持从任意 child 发问；不支持范围明确拒绝。

验收同时覆盖已有 MCP 文本消费与原生结构化假通道：多选/custom、取消后晚答、同数字不同连接请求、原问题结束后新请求活动、无截止等待中 Stop。这里验证结构无损与唯一问题 owner；真正的 Codex/DSH 问答桥仍在后续锁版实验验收。

### 5.6 单 reader、完整写入和背压

1. 每连接只保留一个 stdout reader。RPC 响应、通知、server request 分派后立即继续读取，不在 reader 中等待用户回答或慢 UI。
2. 待响应表按连接和方向关联请求 ID。对向 server request 使用相同数字时不覆盖本方向 pending call；新连接不能复用旧 pending 对象。
3. writer 只在完整帧写入期间持发送锁；等待 ACK 发生在锁外。取消、回答、shutdown 不能被一个等待 ACK 的业务调用锁死。
4. 控制事实与展示 delivery 使用不同的推进责任。不得让“等待 UI 队列空位”堵住控制事件识别。
5. 可合并的流式 delta 可以在原 item 内有界合并；完整最终 item、错误、question 与 completion 不能静默丢弃。预算耗尽且无法安全继续时，显式报告传输/交付失败并由原 owner 停止和退休，不能转为无界缓冲。
6. 有界不等于只计事件条数；同时考虑字节预算、单条合法大 item 的处理和最大帧限制。阈值沿项目既有预算策略，先测真实内容，不在规格里拍一个任意常数。

公共层最多共享完整帧 writer、关联和资源工具；方法名、握手、turn 状态、child 因果、ACK 解释都留在 adapter。测试可以共享场景，不要求共享一个万能 transport 类。

### 5.7 G2 验证边界

假 runtime 验证 Owner 是否等待声明范围、输入未知是否保全、Stop 是否可推进；真实管道/子进程替身验证完整帧、乱序、满队列和迟到 spawn。锁版 runtime 才能证明 provider 真的提供尾部事实。四种证据分别记录，不能用 fixture 的自设 `scope_closed` 证明外部协议支持。

提前调查已经提供一个具体风险：第三阶段所锁定 Codex 0.165.0 的子完成上报与父唤醒受真实 feature/模式影响；“看见 child completed”或通过亲缘过滤列出线程，均不自动证明父已处理及范围原子闭合。第二阶段不复制 provider 内部调度来弥补它；C5 必须允许如实报告证据不足，由第三阶段 C0 反证并决定可否切换。详细固定版本来源和证明范围以第三阶段文档为准。

## 6. S2：公开询问原执行是否退休

### 6.1 现状与要修的边界

`wiki/job_execution.py::require_retired` 会读取 `session._pending_queue`、`session._run.request`、整个 `session._run.retirements`。它已保留部分原 worker reference 的持久 retired 证据，但仍被迫理解会话内部布局，而且可能把原请求与后来请求的活动混在一个会话判断中。

本工作包目标是隐藏表示并精确询问原执行，不改变 Wiki 对 assignment、job、operation receipt、文件效果的领域判定。

### 6.2 建议接口与结果

建议在现有会话模块暴露按原身份的查询与等待，不直接返回可变内部集合：

```python
# 语义草图；优先复用现有原执行/资源类型。
query_retirement(original_execution) -> Retired | Pending | Unknown
await wait_retirement(original_execution, deadline=...) -> Retired | Pending | Unknown
```

- `Retired`：所要求范围的原 preparation/spawn、backend 进程组、I/O、原连接和授权资源已有明确退出证据；查询范围必须按原执行资源定义。**它不包含“通知已送达”或“所有 publisher subscriber 已退出”的隐式保证。**
- `Pending`：原 owner 仍持有具体准备、运行、退出或等待责任；附有可诊断原因，不暴露所有内部集合供消费者二次判断。
- `Unknown`：无法证明原责任已经收束，例如 owner 故障交接尚未完成、原证据不可读、历史只有身份没有可用退出证明。

“找不到会话/进程对象”“session idle”“Task.done”“直接 PID 已退出”都不是通用 Retired 证据。进程组内后代可以在根退出后继续写文件。

`query_retirement` 是观察，不隐式 Stop 当前会话。需要取消时使用原 request/work 的现有控制入口；不能因为旧 Wiki 请求未退休而取消后来的人类请求。

生命周期消费者若还需 publisher seal/join 或 App done 通知前缀，应分别组合原 publisher 的证据与 C6；不能把它们折叠进通用 retired。尤其不能让由 subscriber 发起的资源退出等待，反向等待该 subscriber 自身完成而死锁。安装排空可以要求两类事实都满足，因为它组合的是各 owner 事实，而非把业务结果、通知和资源写成一个状态。

### 6.3 资源引用与故障移交

原 HarnessRun/执行 owner 是唯一资源责任持有者。准备 task 被取消但 spawn 晚到，晚到句柄仍登记给原 owner并进入同一退休；不能在 done callback 中只丢弃引用。Owner 故障时 Manager 接管的是原对象和未完成责任，不重新通过 session.current 猜出资源。

已确认 retired 的证据按既有 consumer 生命周期保留；不把一个 in-memory 缺失对象转换为成功，不新增永久“所有 execution 历史表”。跨服务代的判断使用既有受管旧代退出/恢复合同。standalone 无法证明未知历史 writer 时仍 Unknown，不能通过扫描历史 PID 自授停止权。

### 6.4 Wiki 消费方式

`require_retired` 继续先核对 Wiki assignment、active task 和自身 worker；对每个原 reference 调用新查询。被明确拒绝、从未接纳的 worker 与已确证退休的 worker沿既有规则处理。其他状态必须 pending/unknown 阻止不安全 retry。检查文件效果仍调用 History/receipt，不因 runtime retired 就宣称修改没有发生。

迁移时把 App 删除、归档/rewind、CloseWork 中确实读取同一私有布局的消费者一并改完；不能为了“统一”把与原请求无关的其他业务资源硬塞到此接口。全仓检索只用于确认真实调用方，不展开成全面生命周期重写。

### 6.5 首要反证

旧请求 A 已结束并开始资源退休；同一会话的新人工请求 B 活动。查询 A 必须报告 A 的真实责任，不能因 B 活动永远 pending，也不能 Stop B。再覆盖 A 准备取消后 spawn 迟到、根退出但后代重定向输出继续写、退休失败重试、owner 失败交接以及历史证据不足。

## 7. S2b：单轮结算编排归原 Owner

### 7.1 为什么不是 S2 的同一项

S2 是给消费者的资源查询。S2b 是把**本轮如何分类和提交结果**收回 Owner。固定基线中 `turns.py::_record_and_publish` 从 `self._session_owners[id(session)]` 回取 `owner.runtime.current`，并追加 `execution.frames`；`OwnerRuntime._decide` 又调用 Manager 的 `_after_attempt`、`_settle_turn`。这不是已证明的生产 bug，但使完成语义改变跨越 Owner → Manager → Owner。

S2b 选择在 OwnerRuntime 内，或由其唯一持有的私有结算协作者中，完成原 execution 的结果选择、terminal 提交观察、frames 交接和 receipt 完成。Database 保留事务，publisher 保留交付，Manager 保留注册、跨会话配额与失败接管。

### 7.2 推荐执行顺序

1. adapter 提交带 C1/C5 的证据；Owner 按原 execution 接收，迟到证据进入原归属或明确拒绝，不能结算下一请求。
2. Owner 将原输入集合、原 native scope、完整根输出及错误合成结算候选，并依据 C5 指明根最终回答的已持久 item/消息来源引用。accepted 但发送/处理未知的首输入或 steer 不能套用根成功；完整 transcript 保留，中间答复/子正文/历史不得混进最终结果选择。
3. Owner 只选择一次 outcome；同一原 execution 的重复原生 terminal 是同一证据的重复，不另分配消息 seq 或第二次业务结算。
4. 调用经 S4/C4 修订的 `Database.record_terminal` 短事务；事务内保留唯一性、输入关联和序号合同，并使用 Owner 提供的逐输入处置。不得把仍 unknown 的普通 root 输入无条件写 done/清原因；已记录的本地 attempt failure 与该输入接受未知必须能同时成立。必要持久语义按现有格式合同交付，不能机械升版或绕过验证。
5. 提交返回后立即记录原 settlement 的 committed 观察。后续 `message_sequence_state`、投影或 publish 失败不能把 outcome 退成未知业务结果。
6. 将原 frames 交给 publisher，并按真实 delivery 结果更新 notification observation。`RequestCompletion` 先按逐输入证据选择 completed/unavailable 等状态，再分别表达 `recording_state` 和 `notification_state`；有失败 terminal 不得自动把原未知输入标 completed。C6 仅证明原执行资源，publisher 的前缀送达和 seal/join 另行组合。
7. 资源退休独立推进。请求结果可确定而资源仍未退出；需要资源门的消费者等待 C6，不能用 Future.done 释放 writer 责任。

### 7.3 不做的“方便改法”

不要新建 CompletionManager；不要把 terminal DB 事务移进 provider adapter；不要先完成 receipt 再在 idle handler 里无限补写“最终正文”；不要在通知失败时重跑模型；不要把任何 child failure 一律传播为整个请求失败。

Claude 的现有 idle 迟到展示可在过渡中保留，但要明确它属于旧完成范围。第三阶段共同责任调整时，要用真实完整结算替换被覆盖的成功补写旁路，保留迟到事件原归属的回归。

### 7.4 验收矩阵

| 场景 | 必须结果 |
| --- | --- |
| terminal commit 成功后投影抛错 | 原 terminal 保持 committed；不重新选择 outcome、不重跑 |
| 同一 result 重复到达 | 只有一个 terminal、原 input 关联和最终回答来源引用不变 |
| 单 attempt 包含根初答、子正文、commentary 与根最终汇总 | 所有材料保留；委派/App 仅消费原结算投影选定的根最终 item/消息，不拼所有 assistant text，不复制结果正文 |
| A 的 terminal 在 B 活动时到达 | 不结算 B；A 原观察可解释 |
| 根成功但一个 steer 发送 ACK 未知 | 该 steer 保留独立未知，不能全部输入 done |
| 首输入发送 ACK 未知，但本地 attempt failure 已持久记录 | 原输入仍 unknown；receipt 按证据可 unavailable；重启仍可见，不自动 replay，不用已记录诊断假定 provider 执行失败 |
| 子任务失败，父明确处理后完成 | 根据父完整结果结算，保留子失败可见证据 |
| 请求完成而 backend 退出失败 | 业务结论不改写，资源仍 pending/unknown |
| 存储提交失信 | 统一 fail-stop；不发伪造成功 terminal |

G2 删除 Manager 对 `runtime.current/frames` 的单轮编排读取；允许 Manager 在原 owner 故障后接管原对象，这是不同责任。

## 8. S5：Wiki 注册与受保护操作归原模块

### 8.1 现状与目标

Registry 已有 `Construction`、注册代次、Store 取得及退休；维护接纳已有 `prepare`、`commit`、History 锁与 grant 校验。这不是“缺一把锁”的系统。当前问题是 `job_execution.py`、maintenance、router 等调用方仍组合 account/registration/History 顺序并读取内部状态。

S5 的目标不是把所有 Wiki 操作放进同一个长锁，也不是让 Registry 变成完整 Wiki 业务层。Registry 收回**同代资源取得、借用、退休**；维护接纳收回**该业务的准备、最终校验和提交**；History/Store 保留真实文件及数据库协议。

### 8.2 受保护操作合同

对每种已有操作写清四件事：准备可否读文件并同步索引；需要哪种 History 保护；哪些身份/授权在效果前必须重验；最终提交后谁唤醒。然后在现有模块提供业务级入口，不向 route 返回“你自己按顺序拿锁”的上下文。

建议语义如下，不要求统一实现为同一个 context manager：

| 操作 | 冷准备 | 受保护部分 | 完成后 |
| --- | --- | --- | --- |
| 普通搜索/读取 | 取得当前注册、完成必要 Store construction | 同代借用及当前读取授权；遵循 live revision | 返回引用证据；不延长授权 |
| 单项/批量维护接纳 | 当前文档与观察同步，构造候选 | 既有 History 保护→account/registration→最终短事务；重验注册、grant、revision | commit 确认后 worker.wake |
| 文件修改/恢复 | 所需原像和材料准备 | History 的原有 prepared/applied 协议与当前写授权 | 保留 operation receipt，派生索引按原恢复责任推进 |
| 注册变更/撤权 | 原 owner 关闭旧代新借用 | 注册更新及原 Store 退休保护 | 未退出旧借用仍归旧 owner，新代不借旧 grant |

以上锁顺序来自固定源码的现有约束，只在拥有该业务的模块里保留；不能把表复制到每个新 caller 要求其手工遵循。同步线程与 async 入口的锁语义不得混用。

### 8.3 实施步骤

1. 从真实调用点列出 `registry._account_lock`、`registration_lock`、`store.borrow`、`WikiHistory.locked` 的组合，按业务分组，而非把所有使用者强行替换成一个通用装饰器。
2. 将同代 Store 取得和借用封装回 Registry；Construction 的恢复不能由普通借用隐式重新触发全库恢复。
3. 将维护的 `prepare → 重验 → commit → wake` 收在 maintenance admission 原模块，routes/jobs 调用业务操作。`commit` 不再把“调用者已持全部锁”作为外部公共合同。
4. 其他写操作按原 History 权威迁移；不把注册事务和文件效果伪装成一笔跨库/文件原子事务。
5. 删除路由及普通调用方的私有锁/代次读取。只有内部协作者保留真正需要的锁层次。
6. 同步记录哪些调用仍被允许持长 History 锁以及原因；慢网络或模型等待禁止纳入。

### 8.4 反例验收

- 取得 Store 后立即重绑：旧操作不能在新库提交，也不能将旧注册证明带到新库。
- prepare 后撤权：已完成准备仍不能越权提交。
- 旧借用尚未退出时新注册开始：旧资源保留可见责任，不提前 close、不把新 grant 交给旧执行。
- 同一 review 重放、批量中的一个观察失效：维持既有原子接纳/重复关联语义。
- commit 失败：不唤醒 worker；commit 已确认但 wake 失败：保留原 job，不再创建第二个。
- 取消后 reconcile 文件效果：仍读原 History/receipt，不能因资源退休就推断没有写入。

第一阶段 compact/search/read/诊断的语义与性能基线在重构后抽样复验受影响路径。若 S5 引入额外全量扫描或长锁阻塞，即使功能正确也不通过。

## 9. S6：Wiki 业务入口与 HTTP/App/MCP 分离

### 9.1 已有具体问题

固定版本 `routers/wiki.py::app_wiki` 持 handler 映射，查找 `APIRoute`，读取 `route.dependant` 做参数/正文验证，再按具体函数补 `session_mgr`，最后调用普通 HTTP handler。它已经复用了正文校验，不能重报 null 旧缺陷；但新增一个业务操作仍需要理解另一条 transport 的函数签名。

本阶段把 query/read/jobs/maintenance 等真实业务操作放在既有 `server/wiki/` 领域模块，输入复用 `wiki/contracts.py`。HTTP、App 和 MCP 各自完成认证与路径解析，再调用同一个业务入口；不新增一个反射 dispatcher 继续调用 router。

### 9.2 调用上下文

建议用一个小的、不可变的可信调用上下文承载已验证 actor 身份、账户、client 身份以及 task/MCP scope 引用。它不是 `authorized=True`，也不是客户端可以填写的一包字段。

业务层仍检查操作当下的 actor、task grant、client scope、所属 vault/registration、task/job 所有权和 write/publish 权限。入口负责认证，不能替代业务效果前授权。客户端可提交的路径、doc、job ID 和上下文中可信账户必须分开。

不同入口保留真实差异：普通账户入口的 owner-only 操作、App 现行绑定白名单、MCP task grant、HTTP 状态/二进制响应/SSE 映射。抽取入口本身不新增多 App 授权或取消任务的权限。

### 9.3 迁移单位

按完整业务族迁移，每个单位同时迁移所有入口与验证：

1. 查询与读取：search/read/read-many/documents/blocks；保持参数 defaults、过滤、compact/full、cursor、revision、引用与错误语义。
2. job 读取与控制：list/detail/output/cancel/retry/verify；保留 requester、task/client 所属与原执行退休门。
3. 维护接纳：maintain/curate/selections/batch 等；直接使用 S5 收敛后的业务操作。
4. 编辑、发布、恢复与 snapshots：保留 owner-only、History 效果和二进制/流式响应差异。

不是必须新建四个 service 类。已有模块有足够责任就直接暴露函数；只有一个业务族确需协调多个模块时才设小的 operations 模块。

### 9.4 对照验收与删除

使用相同输入矩阵跑普通 HTTP、App、MCP 的业务许可范围：合法正文、缺失字段、null、错误类型、额外字段、非法路径、越账户、越 client、错误 task、撤权、无效 grant、已退休/未退休 job retry。合法差异单独标注，不能要求所有 transport 都返回完全同一 HTTP 码。

完成时 `app_wiki` 不再查另一 route 的 `dependant`、不按目标函数名补依赖、不反射调用 HTTP handler。普通 HTTP 与 MCP 不各自复制业务校验。API 路径和用户行为保持原合同；若确需改变客户端协议，单列迁移而非埋进抽取。

## 10. S7：一次聊天读取由投影模块拥有

### 10.1 现状与边界

`web/src/lib/hydrateSession.ts` 已有 `loadSessionSnapshot`、`loadSessionSnapshots`、Research 读取集合、detail 接受后服务代次关联以及问题/subagent 合并。调用方仍可以传 start snapshot/current callback，选择、归档查看和重连需要理解读取顺序及基线。

本阶段把这一责任继续集中在现有模块；Zustand 仍是可见投影，transcript 模块仍拥有消息历史窗口与序号，`sessionList` 保留列表责任。S7 不重写输入发送/unknown 恢复，也不把 BG、Research、问题、消息合并成一个泛化 reducer。

### 10.2 建议公开操作

页面只表达“加载这个 session，用于选择/归档查看/重连”；模块自行捕获 account generation、session ID、service generation、读取基线与操作有效期。WS 交给模块已识别的领域事件，模块调用现有 store 合并函数。

```ts
// 语义草图，复用现有模块，不新增第二份会话权威。
loadSessionView(sessionId, reason): Promise<LoadResult>
acceptSessionEvent(event): void
cancelSessionViewRead(operation): void
```

取消读取只取消观察，不取消后端执行。服务代次未建立时先接受可信 detail，再用其代次推进有依赖的 Research 读取；独立 BG 请求可并行，但应用结果仍受原读取基线和账户代次保护。

### 10.3 合并规则要分别保留

| 数据 | 必须保留的规则 |
| --- | --- |
| transcript | 消息 seq、历史窗口、增量与快照关系；不能由较旧 detail 清掉新的 WS 消息 |
| dispatch/active attempt | 服务 generation/revision、原 request/attempt；旧服务响应不能激活新会话 |
| questions | 读取中关闭的问题不能被快照重新打开；新 WS question 不能被旧快照删除 |
| native subagents | 按原身份和字段规则合并；终态事件后的旧 running 快照不能倒退 |
| Research | 原 job 更新与 snapshot baseline、浏览器收讫仍独立于模型处理 |
| BG | 当前任务记录与读取前基线合并；不以快照空列表删除读取期间新任务 |

页面取消选择、切换账户或重新连接时，失效的是原读取操作；不是清空所有会话的业务事实。服务器实际删除、归档等事件仍使用现有领域规则。

### 10.4 实施与验收

先在现有 HTTP/WS 合同下集中读取生命周期，再接入 S4 的归一化任务/完成事件。页面和 reconnect caller 不再持有 snapshot start、依赖排序或 service-generation 细节。不要让 UI 对 `threadId`、`server-request` 或 DSH inbox 做判断。

首要场景是“detail 在途→question 已关闭→旧 detail 返回”。之后覆盖切账户、旧服务响应晚到、root 输出后 native child 更新、子完成后旧快照到达、Research 已收讫与旧快照、归档查看与重连竞争。测试应从实际加载公开函数和 store 可见结果断言，不只验证新函数被调用。

## 11. S8：各 owner 提供排空事实，安装层完成封口

### 11.1 现状

`main.py::_installation_is_idle` 直接检查会话 owner、owned tasks、配置锁、队列、retirements、BG、Research、委派、Wiki、App backend、Codex login、scheduler 私有 executor futures、DB 锁等。`quiesce_installation` 特意在检查与 `health.begin_shutdown/close_admission` 之间不 await。这一竞态保护必须保留。

改造目标是：新增资源只在其所属 owner 内纳入排空事实，不要求 main 再认识内部集合；安装 owner 仍明确列出参与者，并负责一次检查和封口。

### 11.2 最小接口

各 owner 提供同步的 `quiescence()`/`can_quiesce()` 语义，返回从当前真实责任推导的空闲与原因。名称沿现有代码选择，不能引入持久 idle 标记或镜像 active_count。

| 参与者 | 必须纳入的原责任 |
| --- | --- |
| sessions/Harness | 准备、spawn、RPC、reader、问题、输入、通知、停止失败、held/retiring 原资源、配置变更与故障接管 |
| BG | starting/running、输出读取、退休、结果记录与投递 |
| Research | pipeline、delivery、完成观察、关停中的迟到 handoff |
| Delegation | 原轮次运行、终态/问题交付、保存、completion handoff |
| Wiki Registry/worker | construction、借用、退休、维护提交、worker、取消、恢复责任 |
| App backend/App agent | 真实启动/停止操作、运行资源和对话 |
| scheduler | 所属域受管触发与执行，不让 main 读第三方 `_executor` |
| credential/login | 原刷新、认证操作与其受管资源 |
| Database | 原写 owner、事务驱动及需收束的内存写入责任 |

清单显式列在安装编排中。没有自动注册“谁都可以成为 task”机制；缺席的真实 producer 是验收失败。

### 11.3 原子范围

事件循环内的检查与封口在同一无 await 同步段完成。各参与者必须保证封口后其公开入口不再接纳新工作。对线程、文件观察器、外部 producer 的竞争，继续使用该 owner 已有准入同步；Python 协程里没有 await 不能被误称为全系统跨线程原子性。

如某参与者判断需要异步工作，应先在准备阶段执行；最终 `can_quiesce + close admission` 必须重验。不要在 main 中逐个 await 并把较早结果缓存成一份全局一致快照。

允许保留原 API 的布尔响应及内部诊断；为了让每个 owner 更容易测试，可以返回短原因集合。原因是派生诊断，不再写一张交接状态表。

### 11.4 验收

在最后一次检查前/后分别注入新请求、schedule 触发、迟到 spawn、RPC pending、长 question、交付 observer、DB writer 和退出失败。存在任何原责任就拒绝自然交接；全部收束后一次放行。封口后新请求明确拒绝。检查不到私有字段之后，旧所有场景仍必须能够阻止交接。

自然排空不能为凑时间主动取消用户 backlog。服务 shutdown/fatal 与正常发布 quiesce 保留不同合同。

## 12. 建议开发和合并顺序

### 12.1 工作包与完整出口

下表是逻辑 PR 边界，不是强制一次只改一个文件。所有权切换必须在同一可审查单元里交付原入口及消费者，避免“先加新函数，后面再删旧路”长期并存。

| 顺序 | 工作包 | 主要文件范围 | 依赖 | 合并条件 |
| --- | --- | --- | --- | --- |
| P2-01 | 契约与基线对齐 | `architecture-contract`、`harness-layer`、实际类型/测试目录 | G1 | 固定 C1–C8 的语义、legacy scope、真实反例；不标已实现 |
| P2-02 | S1 返回接管 | `sessions/turns.py`、`submission.py`、`base.py`、BG/Research/Delegation | P2-01 | 四类实际返回入口迁移完；原 receipt/unknown/冲突行为保留 |
| P2-03 | S3 配置组合与捕获 | Agent/Session/Credential 入口、`owner_runtime.py`、`model_settings.py`、`assembly.py` | G1、P2-01 | 路由删除锁序/遍历兼容知识；实际请求捕获一致 |
| P2-04 | S4 运行时语义接口、原输入与结构化答案 | `profile.py`、`events.py`、`harness.py`、`run.py`、`db/client_submissions.py`、`db/sessions.py`、sessions 输入/恢复/Question 模块、现有 adapter | P2-01、可提前的协议调查/最小反证；不依赖 G3 通过 | stdin/steer/reuse 分离；首输入/追加 unknown 不被 terminal/恢复覆盖；结构化答案持久保全；不切传输 |
| P2-05 | S2 + S2b 完成/退休/最终正文选择 | `owner_runtime.py`、`turns.py`、`submission.py`、原消息/terminal 元数据、Delegation/Wiki/App 真消费者 | P2-04 | 单轮结算无 Manager 回读；最终结果沿原消息引用；原执行查询替换私有窥探，通知另行组合 |
| P2-06 | S5 Wiki 受保护操作 | `registry.py`、`maintenance_admission.py`、History/Store/job 真调用点 | G1 | 锁/代次协议留在原领域 owner；所有选中 caller 迁移 |
| P2-07 | S6 Wiki 业务入口 | `wiki/contracts.py`、业务模块、`routers/wiki.py`、MCP 真实入口 | P2-06 | HTTP/App/MCP 全业务族对照通过；反射 handler 路径退出 |
| P2-08 | S7 会话读取投影 | `hydrateSession.ts`、store、WS、选择/归档/重连入口 | P2-01；事件部分接 P2-04 | 调用方不传基线、不排序快照；原数据合并规则保留 |
| P2-09 | S8 排空组合 | `main.py`、既有各 domain owner、安装准入 | P2-02/03/05/06/08 | main 无私有布局清单；检查至封口竞态不变 |
| P2-10 | G2 整体验收与交接 | 受影响测试、主题合同、删除核对 | 全部选定工作包 | 全消费者路径、失败范围与删除标准完成 |

S1、S3、Wiki 和前端的开发可在稳定共同合同后分支并行；合并时一次只迁移一个事实 owner。S4/S2b 不按“每次一个布尔字段”拆碎所有权切换；允许一个工作包跨多个文件才能保持完整责任。

### 12.2 每个 PR 的说明模板

```text
问题：哪个真实调用者被迫理解了什么内部规则？
事实与 writer：哪些原权威保持，哪段编排回到哪个 owner？
提交点：接纳 / 配置 / terminal / 文件效果中的哪一个会变化？
影响入口：逐一列出已迁移消费者。
失败范围：最可能推翻设计的反例及其处理。
删除：被替代分支、私有访问、helper 和文档状态。
验证：本次真正运行的检查；未运行项及原因。
```

普通局部编辑不要求新建 ticket 或状态机。这个模板用于本阶段真实的跨模块所有权改动，不扩成所有代码提交的仪式。

## 13. 验证矩阵：按行为分层，避免重复跑同一检查

### 13.1 行为矩阵

| 编号 | 场景 | 测试层 | 关键断言 |
| --- | --- | --- | --- |
| G2-A | receipt 已存在但正文消失 | SQLite + 公共 admit_return | 不 prepare、不唤醒；原输入唯一 |
| G2-B | prepare 时 work 关闭/重绑 | SQLite + controlled await | 后置资格生效；无错目标接纳 |
| G2-C | ACK unknown 后再提交即触发永久拒绝陷阱 | 来源实际循环 + receipt | 提交次数等于一次；第二次提交陷阱不触发；原 unknown 保留，只观察原回执 |
| G2-D | 保存默认与旧会话不兼容 | HTTP + 实际配置入口 | 保存合法；启动明确失败；用户修正后继续 |
| G2-E | 捕获后改配置/撤权 | Owner + 工具入口 | 配置稳定、权限实时，互不替代 |
| G2-F | 根完成、后代/父续跑仍在 | 假运行时 + Owner | 未收到真实 scope closed 前不结算该 scope |
| G2-F2 | 首输入/steer 写出未知，本地 attempt failure 已记录后重启 | 真实 SQLite + pipe + 原恢复入口 | 原输入仍 unknown/可见，receipt 不伪 completed；无自动 replay/withdraw 放行 |
| G2-G | 通知早于响应、双向同数字 ID | 真实 pipe 替身 | 关联正确、不丢完成事实 |
| G2-H | 满展示队列 + Stop/问题回答 | 真实 pipe/子进程 | 控制可推进、完整 item 不静默丢 |
| G2-H2 | 多选/custom 的原生答案与旧 MCP 文本消费 | 原 Question owner + 双消费投影 | 结构无损、同一回答事实、关闭不伪造答案 |
| G2-I | commit 后投影失败、重复 terminal | 真实 SQLite + Owner | 单次 terminal，committed 保持 |
| G2-I2 | 一个 attempt 多根 turn，夹有 child 与 commentary | 假 runtime + 真实消息/terminal + 消费者 | 原结果投影选择准确；全 transcript 保留；委派/App 不拼全部 assistant text |
| G2-J | 原 A 退休，新 B 活动 | 实际原执行查询 | 不误停 B，A 按真实资源判定 |
| G2-K | 取消 spawn 后句柄晚到 | 可控子进程 | 原 owner 收束；未退出责任仍可查 |
| G2-L | Wiki 取得 Store 后重绑/撤权 | 临时真实 Wiki + 公共业务入口 | 不跨代/越权提交；原文件效果保留 |
| G2-M | 普通/App/MCP 非法正文/越权 | 三入口对照 | 相同业务许可范围、真实 transport 差异 |
| G2-N | 关闭问题后旧 detail 到达 | 前端实际加载+store | 不复活问题、不丢新消息 |
| G2-O | 旧服务/账户快照晚到 | 前端读取与 WS | 无跨代污染 |
| G2-P | 检查至封口竞争 | 安装公共 quiesce | 有责任拒绝，无 await 空窗，新输入不穿透 |

### 13.2 证据层次

- **纯类型/fixture**：归一化映射、能力表达与错误分类。它不证明真实 provider 能力。
- **临时真实存储/真实 pipe/子进程替身**：事务、并发、原身份、控制推进和资源引用。它不证明实际模型会处理 child 回报。
- **锁版 runtime 最小实验**：P3/P4 的协议假设。可提前做调查，但必须注明版本、配置、是否真实模型、原始事件及未验证范围。
- **浏览器与产品入口**：实际用户路径及不同消费者。G2 只验证已交付接口与现有后端；后续切换另有完整范围门。

每项检查一个执行者。复用代码、配置、依赖和环境均未变的证据；出现新反例或修改使证据失效时重跑受影响项。命令与提交/发布要求以仓库 `docs/testing.md` 和 `scripts/check.sh` 为唯一 owner，本文不维护第二套测试数量或门禁表。Wiki 的 `knowledge_eval`、`wiki_perf` 仍 opt-in；不恢复已暂停的多账户压力测试。

## 14. G2 阶段出口及交给第三阶段的内容

### 14.1 G2 必须全部成立

- [ ] G1 的投递、配置与 Wiki 基线已接入本阶段共同接口，行为没有被重构改写。
- [ ] BG、Research、委派终态/问题不再自行拼 `source_receipt → resolve_task_return → start_message`，原来源结算和重试仍在来源。
- [ ] Wiki 等消费者不再读会话内部队列/retirements 来猜原执行退出；原 A 与后续 B 可独立判断。
- [ ] 单轮结算选择、原提交观察、frames 编排归 Owner；Manager 不回取当前执行做该轮选择。
- [ ] C5/原结算投影保存根最终回答的最小原消息引用；多 turn 假 runtime 与委派/App 消费者通过，不复制正文或猜最后字符串。
- [ ] 首输入和追加共用原持久输入证据入口；`record_terminal`、`RequestCompletion`、查询/恢复不会把接受未知覆盖成 done/completed，原未知不授权 replay。
- [ ] 有效配置只有一套组合规则；路由不持跨模块锁或逐一检查旧会话兼容；撤权仍效果前校验。
- [ ] steer、双向通道、完成范围、跨请求复用分别表达；旧 exec/Claude 的不足如实标注。
- [ ] Question owner 在既有消息 JSON 元数据中持久保全结构化答案与来源，原通道仍只在当前运行期；旧 MCP 文本消费兼容，重启不恢复 callback/重发答案；没有新增问题账本。
- [ ] S5 和 S6 选中业务族的实际入口全部迁移；普通/App/MCP 不反射调用其他 transport handler。
- [ ] S7 删除页面/重连调用方的基线和依赖排序责任，保留各数据类型合并语义。
- [ ] S8 覆盖所有真实 producer；main 只组合 owner 事实并在同一同步段封口。
- [ ] 假运行时、真实存储和真实子进程证据可复核；未执行的真实 runtime 与浏览器项没有标绿。
- [ ] 旧路径、被替代 helper、重复文档合同同步退出；保留的真实差异有说明。
- [ ] 未引入第二账本、全局任务注册表、镜像 idle、自动未知重试或隐式调度政策变化。

G2 表示架构接缝已成立，不表示 G3/G4 已完成。若 S4 发现所要求的真实尾部事实在选定 runtime 不可取得，记录具体外部依赖；不能将 fake event 当代用品，也不能通过扩大宿主循环来绕过接口证据缺口。

### 14.2 交接包

交给第三阶段的材料直接更新原主题合同，不再增加永久的平行规范：

| 交接项 | 所属文档/代码 | 第三阶段必须遵守 |
| --- | --- | --- |
| C1–C8 实际类型和版本差异 | `architecture-contract.md`、`harness-layer.md`、真实类型 | 不重新定义输入、terminal、退休 owner |
| 输入与 scope 行为测试 | 现有 sessions/harness 测试 | 新 adapter 在同一公共入口通过 |
| 原执行退休接口及消费者清单 | sessions、Wiki/App 合同 | 新 app-server 资源全部进入原引用；publisher/通知独立组合 |
| 原结果选择接口与持久来源引用 | C5、原 terminal/message 元数据、Delegation/App 完成投影 | 真实原生 scope 决定根最终 items；消费者不再按 attempt 拼所有正文 |
| 首输入/追加持久证据与恢复语义 | C4、既有 client_submissions、RequestCompletion | 已记录本地 attempt failure 不消灭输入 unknown，恢复不自动 replay |
| 结构化问题结果和来源 | C7、原 Question owner、question-answer JSON 元数据 | human/system-expired/closed 不混同；重启不恢复旧 callback/重发答案 |
| 当前能力声明 | RuntimeProfile/adapter + UI 契约 | 不再从 stdin/reusable 推导 steer |
| 旧路径删除记录 | 相关主题合同/PR | 不恢复已被替换的私有窥探或结算旁路 |
| 协议调查证据及缺口 | `codex-backend.md`、`native-subagents.md` | C0 对固定版本重验；scope 闭合不能假设 |
| 发布排空参与者 | 原 owner、安装合同 | 新 RPC/question/spawn 必须被本 owner 纳入 |

第四阶段复用同一交接包和第三阶段形成的真实消费者验证方法。DSH 可以有不同的 ACK、wait 和 cancel 语义，但不得再增加输入接纳者、结果 owner 或产品调度者。

## 15. 失败、回退与阶段完成后的维护

本阶段不为架构抽取机械增加持久格式；但 C4 的首输入/追加证据语义、C5 的根最终回答来源引用和 C7 的结构化答案保全已经是本方案选定的持久合同工作。实施必须在同一改动更新 `current-storage-format.md` 与 `backup-and-migrate.md` 的保留、排除、恢复和验证，并按现有格式策略决定有限转换；不能借“只是重构”或“仍用原 JSON 列”绕过。第一阶段已新增的来源诊断字段保持原权威。

回退以完整所有权切换单元为界。禁止只回退新入口、留下新消费者，或同时启用旧新两条写路径“保险”。S1 的公共入口及三个来源应一起回退；S2b 的 Owner 结算及 Manager 旧调用应一起回退；S5/S6 的业务入口与 transport 映射应一起回退。只读投影可以独立回退，但必须与后端实际事件契约匹配。

运行中的原执行仍归启动它的原 owner，软件回退不能转移或撤销外部文件效果。正常发布沿既有自然排空，不主动清理 backlog、不把 unresolved retirement 当作 idle。失败保留原引用和证据，再选择完整前一候选；不在未知执行中切换 backend 继续。

每个完成的工作包把实现事实写回原文档 owner：返回/完成/退休归 `architecture-contract`，共享能力归 `harness-layer`，Wiki 边界归 Wiki 主题合同，前端读取归现有投影相关正文，运行与门禁归 `testing/local-wsl`。本文件作为四阶段实施设计保留，不长期维护一份与代码平行的“第二当前架构”。

## 16. 源码与设计依据

以下链接均固定于本次只读核对提交；说明该版本实现位置，不代表测试或部署已经通过。

| 依据 | 直接支撑的现状/设计 |
| --- | --- |
| [根 AGENTS](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/AGENTS.md)、[文档规则](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/AGENTS.md) | 完整责任、唯一正文 owner、文档与代码授权分开、验证门禁 |
| [架构合同及 admit_return 设计](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/plans/architecture-contract.md) | 原输入、完成/退休、来源边界及已确认的接管接口草图 |
| [turns.py](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/turns.py)、[owner_runtime.py](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/owner_runtime.py)、[submission.py](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/submission.py) | 重复接管、Manager 回读当前 execution、配置捕获、原 receipt 和 completion |
| [ResearchManager](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/research/manager.py)、[Delegation](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/delegations.py) | 当前来源调用方及原来源职责 |
| [terminal 事务](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/db/sessions.py)、[输入持久观察/恢复](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/db/client_submissions.py) | 当前 root terminal 直接 done 与 steer unknown 保留的差异，支撑 C4 首输入及恢复收敛 |
| [Question owner](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/owner_questions.py)、[question 等待](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/questions.py) | 当前答案字符串化与文本等待合同，支撑 C7 无损结构化改造 |
| [RuntimeProfile](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/harness/profile.py)、[HarnessRun](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/harness/run.py) | stdin/reusable 与 steer 的当前耦合、工具能力及受管资源 |
| [Agent 路由](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/agents.py)、[Session 路由](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/sessions.py)、[Credential 路由](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/credentials.py) | 配置锁、字段继承与更新编排所在位置 |
| [Wiki 原执行](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/job_execution.py)、[Registry](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/registry.py)、[维护准入](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/maintenance_admission.py) | 私有退休查询、Store 代次与现有锁/事务分工 |
| [Wiki HTTP/App 入口](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/wiki.py) | APIRoute 反射、handler 转调、现有认证/正文校验 |
| [hydrateSession](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/web/src/lib/hydrateSession.ts) | detail/Research、基线、服务代次和问题/subagent 合并 |
| [main.py](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/main.py) | 私有排空检查及检查至封口无 await 保护 |
| 用户附件 §1、§3、§4–6 | 四阶段顺序、S1–S8 取舍、完整尾部目标、后续消费者与不做范围 |

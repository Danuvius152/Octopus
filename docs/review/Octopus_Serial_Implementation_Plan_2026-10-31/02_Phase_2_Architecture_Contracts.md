# 阶段二：让现有模块拥有完整操作，收敛架构与运行时合同

实施顺序：[总纲与取舍](00_Overview_and_Decisions.md) → [阶段一：核验与交接](../../plans/engineering-runtime-roadmap.md#phase-1) → **阶段二（本文）** → [阶段三：Codex app-server](03_Phase_3_Codex_App_Server.md) → [阶段四：DeepSeek Harness](04_Phase_4_DeepSeek_Harness.md)。

> 日期：2026-10-31。源码核查基线：`d3e90499713f91e2c2a5aecace9c6f153528c810`。状态：**建议采用的实施设计，尚未实施、运行产品测试或部署**。本文以附件《Octopus_Phase_2_Architecture_Contracts.md》和仓库架构评审为输入，以当前源码重新划分已完成与待完成工作。接口代码均为语义草图；标注“现有”的名称除外，不能当作已存在 API。

## 0. 阶段目标、前置条件与成功标准

本阶段要让调用方只表达业务意图，不再各自组合锁序、回执查询、会话唤醒、原执行退休、配置继承和快照接收规则。保留模块化单体、SQLite、SessionOwner、Harness 与现有业务模块；重构集中在责任之间的接口，不重建整个系统。

**进入条件是 G1，而不是“旧评审中的每个缺陷再修一遍”。** 最新路线已记录第一阶段行为完成；源码中也已有 BG/委派停止与 unknown 分类、配置校验、Wiki 投影优化和文档分工。实施者应先依据阶段一核对代码、验证和发布证据，然后复用这些行为。阶段二仍未实现的主要接口包括共同返回接管、按原执行的退休查询以及 owner 级排空操作。[E01] [E02]

本阶段的验收标准有三项：第一，原有用户行为与故障保证保持；第二，真实消费者迁移后删除原先必须知道的内部协议；第三，阶段三和四可以用共同合同接入自己的协议，而不再要求 Wiki、App、Research 或委派理解 provider 的私有字段。新增接口数量、文件数或代码行下降不是验收依据。

### 0.1 已核对的现状

| 主题 | 当前实现 | 本阶段的差额 |
| --- | --- | --- |
| 返回投递 | 有原 receipt、持久接纳和来源错误分类 | 三个来源仍各自拼装接管步骤，统一完整操作 |
| 配置 | 有共享纯校验、实际设置与准备时捕获 | 路由仍掌握配置锁与编排，将业务操作收回现有模块 |
| 单轮结算 | Owner 是 writer，terminal 提交观察已保留 | Owner → Manager → Owner.current/frames 的回绕仍在，收回原轮次编排 |
| 原执行退休 | 有原资源责任、取消和退休过程 | Wiki 仍读取会话队列与全部退休集合，提供精确查询 |
| 运行时能力 | 有 RuntimeProfile、HarnessRun、ToolPolicySupport | steer 与跨请求复用仍受 stdin 形态牵连，拆开真实语义 |
| 首输入证据 | 追加输入已有 confirmed/unknown | root 输入的 terminal 路径仍无条件 done，补齐未知保全 |
| Wiki | 正文/参数的旧缺陷已有修正 | Registry 锁序和 App 反射调用 HTTP handler 的耦合仍在 |
| 前端 | 已有 hydrateSession 与领域合并规则 | 调用方仍传读取基线和有效性回调，封装一次完整读取 |
| 发布排空 | 已有自然排空及同步检查到封口 | main 仍逐项查看域私有集合，让所属 owner 提供事实 |

上述属于结构及代码路径核查，不代表本轮独立重跑了仓库记录的历史测试。[E03]–[E15]

## 1. 设计取舍：选什么、为什么、做到哪里

### 1.1 两种可行的交付组合

| 方案 | 范围 | 优点 | 代价与适用场景 |
| --- | --- | --- | --- |
| **A：完整的阶段二，推荐** | S1、S2、S2b、S3、S4、S5、S6、S7、S8 | 同时处理当前已存在的重复协议；新后端、知识库入口和任务展示均有稳定边界 | 阶段较大，必须拆成完整的小 PR，并分别验证 |
| B：运行时核心优先 | S1–S4、S2b、必要排空，Wiki/前端深化另列后续阶段 | Codex 关键路径更短 | 需要重排本次四阶段范围，并保留现有 Wiki/前端耦合；适合明确要求尽快切换后端时 |

本次选 A。理由不是“所有工作都是 Codex 前置”，而是 S5–S7 已有多个真实调用者，并与用户的 Sapphire、SmartReader、任务展示方向相交。**S5、S6、S7 是本次选择的完整交付范围，不是 Codex 协议的技术硬前置。** 每个单元必须迁完真实消费者和失败路径，不在一个 PR 中一次混改九项。

### 1.2 全阶段不变的产品行为

1. BG、Research、命名委派的终态与问题返回，继续按**成功接纳顺序**逐条派发。来源完成时间不是接纳次序。
2. 普通 Stop 暂停派发并保留积压。暂停后人工纠偏可以先执行；它不自动恢复积压，显式恢复才继续。
3. 不加入 collect-ready、自动返回 steer、正常运行人工优先、notification-only 或默认等待所有独立工作。
4. 原生后代属于当前逻辑请求；BG、Research、命名委派拥有独立身份，允许父请求先结束后再回流。
5. Agent 合法默认允许改变；旧会话保留 backend/历史，需要时由用户修正有效配置。不能为了保持所有旧会话兼容而禁止保存。
6. 存储失信进入既有 fail-stop；普通拒绝与单会话 Owner 故障保持各自范围。不能把所有失败都改成全服务重启，也不能在失信后在线猜提交。

这些规则已在当前路线中明确，不随接口收敛重新定义。[E01]

### 1.3 本阶段不增加的机制

不增加第二个 SessionManager、统一 Job 总线、通用持久任务树、第二份 execution 表、ConfigurationManager、万能 WikiService 或通用前端同步框架。不因大文件或 mixin 多就整体拆类。新的持久字段必须对应真实恢复问题；派生状态不能因为“查看方便”成为另一个 writer。

## 2. 共同事实、owner 与 C1–C8 合同

### 2.1 所有权及提交点

| 事实/操作 | 唯一权威 | 提交或确认边界 | 后续失败不能改写什么 |
| --- | --- | --- | --- |
| 来源业务结果、正文、是否继续返回 | BG / Research / Delegation | 来源现有记录与结算 | 父会话拒绝不把成功来源改成失败执行 |
| 本地输入接纳、去重、顺序 | 原 SessionOwner + 输入事务 | 完整输入与接纳事实提交 | 等待者取消不撤销已接管命令 |
| 归档会话合法唤醒 | 现有 lifecycle | 成员关系恢复 | 后续输入拒绝不自动重新归档 |
| 本轮业务结果选择 | 原 OwnerRuntime | 只选择一次 outcome | 重复/迟到事件不结算其他请求 |
| terminal 与逐输入关联 | Database | 原事务成功确认 | 投影或通知失败不改为未提交 |
| 通知顺序、前缀、送达 | 原 publisher | 原 publication 的实际结果 | 已送达不等于模型完成或资源退休 |
| prepare、spawn、连接、I/O、lease、退出 | HarnessRun / 原资源 owner | 原资源真实退出证据 | 根 PID 退出不抹掉后代或迟到 spawn |
| provider 接受、原生亲缘和闭合 | 私有 adapter | 锁版协议实际提供的事实 | 不把卡片、线程列表或空闲时间当闭合证明 |
| Wiki 注册与借用 | Registry | 同代使用与退休事实 | 重绑不把旧 grant 转给新库 |
| Wiki 文件效果 | History / Store 原协议 | operation receipt / revision / 原像协议 | job 取消不代表文件从未改变 |
| 安装交接 | 安装 owner 组合各域事实 | 同步检查到关闭 producer | 封口后不得再接纳新工作 |

Owner 串行处理的是**状态裁定**。模型/RPC/用户回答/文件准备/退出等待在受管 I/O task 中推进，不能让 owner mailbox 持锁等整个调用。后台回报携原身份，不能在回调时读取 `session.current` 重新归属。[E04] [E05] [E15]

### 2.2 C1–C8 是语义索引，不是八套新实体

| 编号 | 合同 | 推荐落点与最小内容 | G2 必须证明 |
| --- | --- | --- | --- |
| C1 | 原绑定 | 复用 request/attempt/RunConfig；原 account/session/request/work/attempt/backend，资源引用留原 owner | 迟到结果不会作用于下一请求 |
| C2 | 返回接管 | `sessions/submission.py` 的轻量值类型与会话业务入口 | 原 receipt 优先、分类完整、三来源全部迁移 |
| C3 | 有效配置 | 现有配置/凭据 helper 与 Owner 捕获结果 | 保存、使用、捕获、效果前授权各自正确 |
| C4 | 输入证据 | 原输入行及原发送对象；首输入/追加分别关联 | 本地接纳不等于 provider 接受，unknown 不被 terminal 抹掉 |
| C5 | 完成证据 | HarnessEvent / 原 execution settlement | 完成范围、逐输入归属和根最终回答引用明确 |
| C6 | 退休证据 | HarnessRun / 原退休对象及已有持久证据 | 精确查询原执行，区分 retired/pending/unknown |
| C7 | 问题绑定 | 原 Question owner 与 adapter 私有映射 | 结构化答案经原通道返回，晚答不能影响新请求 |
| C8 | 排空事实 | 各域同步查询，main 显式组合 | 实际责任未收束时拒绝交接，封口没有竞态窗口 |

只在确有缺失时增加值类型或字段。C1 不要求复制一份全局执行记录；C4 不要求全量事件溯源表；C6 不要求永久保存所有内存执行对象；C8 不要求持久 idle 标志。

## 3. S1：共同返回接管完整拥有调用顺序

### 3.1 现状与接口

当前 BG 在 `deliver_bg_result` 内组合 `source_receipt → resolve_task_return → render → start_message`；Research、委派终态和问题分别做类似编排。阶段一已加入来源停止/unknown 状态，S1 必须保留这些语义，而不只是把旧 helper 搬到新文件。[E02] [E03]

建议沿用已有设计名称，入口放在现有会话模块：

```python
# 拟议值类型与入口；不是已存在 API。
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

现有 `SubmissionReceipt` 继续是唯一完成观察入口，不新增相同作用的 Future。delivery 使用原命名：`bg:{task_id}`、`research:{job_id}`、`agent-terminal:{work_id}`、`agent-question:{work_id}:{question_id}`。可信来源从原记录构造 target，不接受客户端任意声明的 account/work 绑定。

### 3.2 一次调用的顺序

1. 先用原身份查 receipt。存在就返回；不读正文、不唤醒、不重新入队。来源已关闭、正文已删除也不能覆盖原接纳事实。
2. 无 receipt 才检查原归属及本次准入。没有绑定可以观察旧事实，不能创建新输入。
3. 调用 `prepare`，每次调用至多一次。允许异步读取及重复准备；禁止结算来源、通知、启动模型或修改授权。等待时不持 mailbox、生命周期锁或 DB 事务。
4. 准备后再查 receipt，覆盖并发接管。已有 receipt 优先于刚准备的正文，也优先于 `ReturnSkipped`。
5. 校验正文和成对的问题关联；在现有 lifecycle 合法唤醒。唤醒后的归属/准入再次校验，由 Owner 最终决定是否接纳。
6. 使用原输入事务完成去重/接纳。实际提交的不同正文仍触发 `SubmissionConflict`；不能查到旧 receipt 就吞掉冲突。
7. 已进入提交交接但 ACK 丢失时，在存储可信的条件下观察原 receipt；仍不能确认就返回 unknown。存储失信直接走 fail-stop，不在同代循环猜测。

唤醒与输入提交是两个提交点。合法唤醒后输入未接纳，不授权回滚会话成员关系。

### 3.3 结果、重试与拒绝条件

| 情况 | 共同入口的结果 | 来源后续行为 |
| --- | --- | --- |
| 原 receipt 可确认 | 原 `SubmissionReceipt` | 继续观察原 completion，不再准备/执行来源 |
| 明确容量不足或暂时 busy | 有类型的本次拒绝 | 同 delivery 按原来源预算重试 |
| 绑定关闭/来源撤回/问题失效 | 明确拒绝或跳过 | 停止新增投递，保留已接纳输入的观察 |
| 永久完整性拒绝 | 明确永久拒绝或原完整性异常 | 停止自动投递并显示原因，业务结果保留 |
| 接管可能发生但未确认 | `ReturnUnconfirmed` | 只观察原 receipt，不再调用可能新增提交的路径 |
| 真正正文冲突/程序错误 | 原异常 | 终止普通重试，保留诊断 |
| 等待者取消 | `CancelledError` | 只取消等待；已交给 Owner 的命令仍有 owner |
| 权威存储失信 | 既有服务不可用路径 | 停止接纳，统一恢复，不自动重放 |

实现时把阶段一的永久拒绝类别补入当前提案的 reason/异常合同，并同步全部来源。不要用异常文案、任意 `ValueError`、`except Exception → False` 推断接纳结果。

历史 unknown 不因下一次明确拒绝变成“从未接纳”。来源沿用已有有限观察、材料保留及人工可见诊断，不增加常驻观察器。stopped 不因配置修复、重连或材料仍在而自动重放。

### 3.4 全消费者闭合与首个反例

BG 保留输出组织、来源记录和交付预算；Research 保留报告、queued/processing/processed 投影和完成观察；委派保留精确轮次正文、问题有效性与来源重入保护。删除它们的共同接管顺序及 `_admission_closed` 等私有解释。Wiki outbox、schedule 首触发、App 和人工输入仍走原入口，不扩大 S1。

**首个反例：** 输入事务已提交，ACK 丢失；原 Owner 随后失败，来源正文又被删除。只读观察必须返回原 receipt，数据库只有一条输入，来源没有重新执行。再验证并发准备、不同正文冲突、准备时归档/撤权、等待者取消和失效问题。扩充既有 `test_durable_independent_returns.py`、`test_source_return_owner_failure.py`、`test_delegation_return_retry.py`；使用真实临时 SQLite，不能 mock 掉最后的接纳事务。

## 4. S3：统一有效配置，四个时点分别负责

当前配置锁与捕获已经存在：路由主动持锁，Owner 准备阶段在锁内读取 session defaults 与 Agent，慢凭据/连接器准备在后续进行。目标是收回编排和继承规则，不另建配置平台。[E06]

| 时点 | 必须回答的问题 | 不允许替代的保证 |
| --- | --- | --- |
| 保存候选默认 | 这个 Agent 自身 backend/credential/controls/tool policy 是否有效？ | 不要求全部旧会话兼容 |
| 指定会话执行前 | 继承与覆盖后的实际组合能否执行？ | 不静默换 backend 或凭据 |
| 请求捕获 | 本 request/attempt 究竟采用哪组设置？ | 不在慢准备中混入后来的默认值 |
| 效果前授权 | 这个操作现在仍被允许吗？ | 配置快照不能延长撤销的授权 |

### 4.1 推荐实现形状

在现有会话/配置模块提供“保存 Agent 默认”“保存 Session 覆盖”“捕获有效配置”三个业务操作。AgentManager、UserManager、Database 保留原权威；路由负责认证、解析和响应映射，不再安排锁序及运行时刷新。

```python
# 拟议示意；类型复用现有模型、凭据和策略定义。
@dataclass(frozen=True)
class EffectiveConfig:
    backend: str
    model: str | None
    reasoning_effort: str | None
    service_tier: str | None
    credential_id: str | None
    working_dir: str
    tool_policy: ToolPolicy
    wiki_scope: WikiScopeBinding | None
    origins: tuple[SettingOrigin, ...]
```

该结果不包含秘密、live run map、锁或可变 backend。`origins` 只说明继承来源，不复制一份 Agent/Session 权威。若现有容器足够，可以先收敛为不可变数据而不坚持这个类名；新增 session override 必须有独立需求，不能把每个配置字段都扩一遍。

### 4.2 短锁与慢准备

锁内读取组合、短校验及提交；prompt、memory、连接器网络、凭据刷新和 spawn 在锁外。若能力发现本身较慢，先取得候选，再在短锁内检查依赖身份/实际字段仍匹配；失配则重新准备该候选，不引入所有对象的全局 revision 系统。

当前请求使用原 capture；下一请求使用新有效设置。凭据删除、work 关闭、Wiki 撤权仍在真实效果前检查。人工 steer 必须兼容原宿主安装的模型、凭据、工具和 Wiki 范围；不兼容就排队，不能向活动宿主偷偷安装一套新权限。

### 4.3 迁移与验收

完整迁移 Agent、Session、Credential 路由、Owner 普通请求和 steer、Research 已有配置捕获调用。检查 oneshot 等实际使用同一规则的入口，补的是实际依赖，不把所有辅助调用强行搬入 SessionOwner。

首个反例是“准备期间改变 Agent 默认，并撤销原凭据”：当前请求不混配，新请求能看到新默认，真实操作不能继续使用撤销许可。覆盖跨账户相同模型名、合法默认保存后旧会话报可修正原因、原地换 key、保存成功但广播失败以及自定义模型名。广播失败不能说成字段未保存。优先扩充 `test_request_configuration.py`、`test_launch_configuration.py` 与已有配置 API 测试。

## 5. S4：运行时提供证据，公共层使用真实能力

### 5.1 拆开四种能力

当前 `HarnessRun.reusable` 由 `StdinMode.STREAM_JSON` 推导，Owner 又用 reusable 打开 steer。这将传输形态、轮内追加和跨请求复用耦合。应在 RuntimeProfile/adapter 明确表达**轮内输入、双向问题、完成范围、跨请求复用**，动态资格仍由原 execution 判定。[E07]

| 操作 | 公共合同 | adapter 私有责任 |
| --- | --- | --- |
| prepare/start | 原绑定+捕获配置；原资源有 owner | 握手、native ID、安装工具和权限 |
| events | 带原身份的归一化文本/item/工具/问题/usage/错误 | 原协议解码、亲缘和事件含义 |
| steer | 原输入 ID、预期活动目标；返回 C4 | 是否处在可追加阶段及真实 ACK 层级 |
| answer | C7 结构化答复送原问题通道 | server request/回调的具体响应与关闭协议 |
| completion | C5 声明所证明的范围 | 根、后代、父续跑与原生闭合依据 |
| interrupt | 请求取消的事实 | 对原 native scope 发送取消 |
| retire | C6 原资源真实退出 | 关闭连接、终止/join 原进程及 I/O |
| reuse | 是否跨请求复用及重新安装条件 | 不从能双向通信推导可复用 |

此处可以复用已有 Profile 加小协作者。不要同时实施“万能RPC引擎”和两种新provider；公共层只共享有共同含义的完整帧写入、关联和资源工具。首版 Codex/DSH 不建立跨请求宿主池。

### 5.2 C4：首输入与追加输入都必须保全 unknown

当前追加输入会写 `delivery_state=confirmed/unknown`，完成时保留 unknown；普通 root 输入的 `record_terminal` 却直接改 done 并清空 reason。这是新RPC接入前必须修正的接缝，而非等阶段三临时加判断。[E08]

**推荐用“所证明的边界+结论”表达观察，不建所有组合的巨大状态机：**

```python
# 拟议观察值；不是要求新增持久事件表。
@dataclass(frozen=True)
class InputEvidence:
    input_request_id: str
    boundary: Literal["transport", "provider_accept", "provider_process"]
    conclusion: Literal["confirmed", "refused", "unknown"]
    evidence_ref: str | None
```

本地接纳已由 `SubmissionReceipt` 证明，无需复制进该对象。完整帧 write/drain 只能形成 transport 观察；某 provider 只有写出证据，就不能填 provider_accept confirmed。后续持久 user message、输入认领或模型完成关联是否提高证据强度，由对应 adapter 的真实合同决定。

**持久边界必须先于可能产生输入效果的发送。** 沿原 `client_submissions` 行，将“原输入开始发送、provider 接受尚未确认”的事实可靠提交后，才允许 adapter 跨越不可确定的发送边界；可以复用现有 unknown 表示及状态版本，不新建发送账本或第二个幂等键。提交必须命中原 input/execution 绑定；存储失信或原资格已关闭时不发送。当前追加路径已有先调用 `set_steer_delivery(confirmed=False)` 再发送的顺序，G2 将同一责任推广到首输入并区分实际证据层级。[E19]

发送边界按真实 transport 定义：现有 Codex prompt 在 argv 中，边界在携带该输入的 spawn 前；Claude 流式路径在首帧前；app-server/DSH 的纯控制宿主 spawn 尚不含业务输入，可在 `turn/start` / `session/prompt` 首次写出前提交。不能一律将所有控制宿主启动当作输入发送，也不能等写完或 ACK 超时才首次落 unknown。记录完成后、实际发送前崩溃，允许保守保留 unknown；只有原通道明确证明未发生发送效果，才能收窄为已知未发送，不能靠重启后“查不到原生消息”反推。

落实到既有输入行时遵守以下规则：

| 事件 | 原输入的处置 |
| --- | --- |
| 本地已接纳、尚未尝试发送 | 保持原 pending/running 责任，不能写“provider拒绝” |
| 发送进入不可确定边界，确认尚未得到 | 记录同一输入的 unknown，使崩溃后仍可见 |
| 原 provider 对同一输入明确接受 | 可按真实证据解除该输入接受未知，不等于模型已处理 |
| 同一输入被明确拒绝且没有历史未知 | 记录明确未接受，由原调度规则处理 |
| 后一次拒绝，但前一次发送仍未知 | 前一次未知保留；不自动换 ID、turn、attempt 或 backend |
| root 有成功/失败 terminal，但某输入发送仍未知 | terminal 可作为已记录业务/诊断事实；未知输入不能无条件 done |
| 重启或等待结束仍不能确认 | 展示原输入、原因与原身份；不自动重放 |

修改 `record_terminal`、`complete_client_submission`、`receipt_for_input`、Owner completion、恢复查询及前端 unknown 展示这些真实消费者。沿原 `client_submissions` 承载语义；如果确需持久字段变化，同时交付当前格式、离线转换/验证及账户搬迁合同。不能仅在内存对象加字段就宣称重启安全。

**首个反例：** 在“持久发送事实已提交、实际发送未发生”和“实际发送可能发生、ACK/后续记录未到”两个夹点模拟硬崩溃；另测完整帧写出后断连、本地记录 attempt failure，再服务重启。原输入仍为接受未知、不能自动 replay，已持久的诊断 terminal 也不能使 receipt 假报该输入已完成。追加输入做同一验证；不要只用能执行 finally 的任务取消代替硬崩溃场景。

### 5.3 C5：完成范围与根最终回答必须显式

```python
# 拟议；MessageRef复用原持久消息身份，不存第二份正文。
@dataclass(frozen=True)
class FinalAnswerRef:
    message_refs: tuple[MessageRef, ...]

@dataclass(frozen=True)
class CompletionEvidence:
    scope: Literal["root_turn", "native_request"]
    state: Literal["open", "closed", "unproven"]
    included_input_ids: tuple[str, ...]
    final_answer_ref: FinalAnswerRef | None
    proof_ref: str | None
```

`root_turn` 表示只证明根这一轮结束；`native_request` 才包含本请求内被纳入的原生后代、父处理返回、父续跑及再次派生。adapter 不能因为进程/事件流结束或看到一条 result，就填写更强的范围。请求要求的范围与已观察范围必须匹配，才能形成对应业务完成结论。

阶段二迁移现有 Claude/Codex 时保留其当前可证明行为，并诚实标识范围；不以统一类型强迫它们宣称完整尾部。阶段三的完整迁移必须取得真实 native_request 闭合，不能拿阶段二假 provider 的 `closed` 事件作为证明。

**根最终回答引用是 G2 交付。** 当前 App 与委派根据 attempt 拼接全部 assistant text；加入根初答、子结果和父最终总结后，这会把中间材料混成业务回答。[E09] 应让归一化事件保留原 item/message 身份与角色，Owner 在结算中引用所选根最终回答的已持久消息。全 transcript 继续保存，结果消费者通过该引用取正文，而不是“最后一段文本”或“同 attempt 全部文本”。

该引用随 terminal 的原提交关系存储，不在 BG/App/委派复制一份可漂移的最终正文权威。已有材料投影按来源需求可以保留，但必须有明确来源引用。无最终回答时给出原因，不伪造空文本成功。完整文档/附件型回答的引用可以是一组同一最终 item 对应的消息片段；不要硬编码只能一个字符串。

### 5.4 C7：问题答案保留结构，经原通道返回

当前 Owner 的 `AnswerQuestion` 收到列表后调用 `format_answers` 转成字符串。新 provider 原生问题不能依赖把展示文本再解析回结构。[E10]

建议沿现有唯一问题结果保留以下三种互斥结算，而非提前降成字符串。当前到期路径实际返回 `AUTO_ANSWER_TEXT` 系统指导，并不是无答案关闭；必须保留该产品行为。[E20]

| 结果 | 必须保存的结构/来源 | 旧文本消费者 |
| --- | --- | --- |
| `human` | 原题目/选项 ID、selected labels、多选、free text；可信来源为 human | 最后消费时调用现有 formatter |
| `system_expiry` | 原 `AUTO_ANSWER_TEXT`、`source=system`、`auto=true` 与到期原因 | 返回原系统指导，不伪装成人类 custom 选项 |
| `closed` | Stop、删除、原 attempt 结束或通道结束等准确 reason | 沿原关闭投影，不伪造一条用户 answer |

产品 question ID 关联原 account/request/attempt；adapter 的私有映射保留原 connection、server-request、thread/turn/item 或回调引用，不让浏览器提供这些可信字段。原 Owner 是人答、到期与关闭竞争的唯一裁决者。没有产品 deadline 时保持可取消的无截止等待，不为适配协议静默加时限。

**本方案选定的持久落点：** 在原 `question_answer` 消息的 `tool_input` JSON 中保存 schema 标识和校验后的 resolution（human 或 system_expiry），`content` 仍为兼容显示文本；closed 沿原 `question_closed` 记录保存 reason/source。原问题记录拥有题目/选项映射，答案引用它；不新增 answers 表、第二个 Future、活回调或可重用授权。历史文本没有结构时标明缺证据，不能反解 `Q:/A:` 伪造结构。重启只读取历史事实，不恢复旧 RPC 或重发答案。与该改动一并更新存储语义、兼容读取、账户搬迁及必要格式验证，不能因为使用已有 JSON 列而略过。

Owner 校验仍 pending、属于原 attempt、未过期且答复合法；答复给原 adapter。不把答案变成新 prompt，不关闭后靠“发一条用户消息”假装原问题已处理。Stop、删除、attempt 结束和连接退出都要关闭原问题及待响应责任；晚答只返回明确拒绝。等待回答不能阻塞 stdout reader。

全消费者包括 HTTP/WebSocket answer、父代理代答、原 MCP 问答、原生问题和自动关闭/到期路径。旧消费者需要文字时在最后一层格式化，不把同一答案保存成两个互相独立的权威。

问题答复可复用 C4 的发送证据原则，但仍由 C7 的原问题与消息记录承担，不新建一个普通 `client_submission` 或用户 prompt。协议只允许人类答案而无法表示 system_expiry 时，应暴露能力缺口或使用已验证的工具结果桥，不能伪造用户选择。G2 至少以原文本 MCP 与结构化假通道验证多选/自定义答案、人答和 expiry 竞争、无截止等待中的 Stop、晚答及重启后来源保全。

### 5.5 真实管道与背压要求

一个 stdout reader 负责解码与分派，不等待用户、UI或慢 subscriber。请求关联包含连接和方向；双向同数字 ID不能互相覆盖，新连接不得接管旧 pending。writer只在完整帧写入时持锁，等待 ACK在锁外，确保 Stop/answer/shutdown可发送。

可合并的 delta在原 item内有界合并；最终 item、问题、错误、完成证据不能静默丢弃。有界预算同时计条数和字节，并处理单条合法大item与最大帧；阈值沿现有预算及真实样本确定。展示变慢不阻塞控制事实；预算耗尽且无法安全继续时显式失败，由原 owner停止并退休，不改成无界缓冲。

G2以真实子进程替身验证通知先于响应、乱序、断连、双向重号、长item及满展示队列下Stop。假 runtime只证明消费者遵守合同，真实运行时才能证明协议存在该证据。

## 6. S2：按原执行查询退休，避免把整个会话当资源

当前 Wiki `require_retired` 检查原 request，也检查整个 `session._run.retirements`，因此必须了解会话布局。S2给它一项完整观察能力，而不是把私有集合改名公开。[E05]

```python
# 拟议语义：需要持久查询时可为async；查询本身不发Stop。
query_retirement(original_execution) -> Retired | Pending | Unknown
await wait_retirement(original_execution, deadline=...) -> Retired | Pending | Unknown
```

`original_execution`来自接纳/执行时保留的原身份及资源reference，不是查询时的current request。Retired必须证明所需原preparation/spawn、I/O、连接、进程组与授权资源退出；Pending表示原owner还有可指认责任；Unknown表示证据缺失或尚未可靠移交。未找到对象、Task.done、session idle、直接PID结束都不是通用退休证明。

**C6不包含通知送达，也不隐含publisher subscriber全部退出。** 生命周期消费者需要通知前缀或publisher seal/join时，单独组合其证据，避免subscriber触发退休后反向等待自己。安装排空可以要求两者都完成，因为它组合不同owner的事实。

资源退休失败仍保留原reference；prepare取消后迟到spawn也登记回原owner并继续退休。Owner失败时Manager接管原对象与未完成责任，不重新通过session当前状态猜资源。跨服务代依据既有受管旧代退出合同；standalone证据不足仍Unknown，不扫描历史PID后自行宣称有权停止。

迁移Wiki及实际共享该私有判断的App删除、归档/rewind、CloseWork调用方。Wiki继续先检查自己的assignment、active task，再查询各原reference，最后独立检查History/operation receipt。运行时retired不证明文件没有改过。

**首个反例：** A已结束并退休，B已在同一会话活动。查询A必须只报告A，不因B永远pending，也不Stop B。补充迟到spawn、根退出后后代继续写、退出失败后重试和Owner故障移交。新接口不要求永久保存全execution历史；按已有消费者生命周期保留必要证据即可。

## 7. S2b：单轮结果选择和提交观察收回 Owner

S2负责消费者如何询问原资源；S2b负责原轮次如何分类并结算。两者不可用同一个“退休API完成”验收。当前链路为 `_decide → Manager._after_attempt/_settle_turn → _record_and_publish → owner.runtime.current/frames`；单一writer已经存在，问题是编排知识回绕。[E04]

### 7.1 推荐内部组织

在OwnerRuntime或其独占的私有协作者中完成原execution结果选择、逐输入处置、terminal提交观察与frames交接。Database继续拥有事务，publisher继续拥有交付；Manager保留注册、跨会话配额、外部业务入口及失败Owner移交。无需新CompletionManager。

### 7.2 完整时序

1. 收到带C1/C5的原执行证据；迟到证据只进入原归属，不能结算下一请求。
2. 根据该请求要求的scope、根最终回答引用、逐输入证据和错误，形成候选。子失败由父是否处理及整体结果决定，不能机械传播为全请求失败。
3. 只选择一次outcome；重复原生terminal只补原证据，不增加第二次业务结算或消息序号。
4. 调用经C4修订的`record_terminal`短事务；保持唯一性及输入关联，未知root/steer不无条件done。
5. 事务返回后立即记录committed观察；随后读取消息序号、投影、发布失败都不得倒退成未提交。
6. 将原frames交publisher，分别得到recording_state与notification_state。存在本地failure terminal不自动使接受未知的输入completed。
7. 原资源退休独立推进；需要资源门的消费者用C6。通知失败不重跑模型，资源失败不改写已提交业务结论。

阶段二同时迁App和委派的最终回答消费者，使用C5引用；不能只给terminal加字段而继续拼全部assistant text。其他completion消费者保留各自对notification/retirement的要求，不要求所有业务等待同一泛化done。

### 7.3 首要验收与删除

| 场景 | 必须观察的结果 |
| --- | --- |
| terminal commit后投影抛错 | committed事实保留，不再选择outcome或重跑 |
| 同一terminal重复 | 一次提交、原seq和回答引用不变 |
| A的终态晚于B启动 | B不受影响，A事实仍可解释 |
| 根成功，某steer接受未知 | 该输入保持unknown，不全部标done |
| root接受未知，本地failure已持久 | 恢复仍展示未知，receipt不伪报该输入已完成 |
| 根初答、commentary、子正文、根最终总结并存 | transcript保留全部；App/委派只消费所选根最终内容 |
| 业务完成，backend退出失败 | 业务结论不变，C6仍pending/unknown |

删除Manager反向读取`runtime.current/frames`作单轮编排的路径；允许Manager故障接管原对象，这是不同职责。Claude旧idle迟到展示若仍存在，必须标明它的实际完成范围；阶段三替换覆盖该范围时删掉被替代旁路，不能同时保留两套成功结算。

## 8. S5：Registry及业务模块拥有受保护操作

已有Registry、Construction、Store借用与History锁应保留。当前多个调用方组合账户锁、注册锁、History保护和代次判断；这证明协议外泄，不等于已复现死锁。[E11]

### 8.1 责任与操作边界

| 操作 | 所属模块必须完成 | 不应外泄给调用方 |
| --- | --- | --- |
| 搜索/读取 | 当前注册、必要冷准备、同代借用、当前读取许可 | Store construction或重绑检查顺序 |
| 维护接纳 | prepare、当前revision/grant/注册重验、短commit、提交后wake | “调用前先拿好这些锁”的公共要求 |
| 文件编辑/恢复 | History原像、prepared/applied协议、效果前写许可 | 把文件与SQLite伪装成一笔原子事务 |
| 注册变更/撤权 | 关闭旧代新借用、保留旧责任、原Store退休 | 新代借旧grant或提前close旧借用 |

Registry只拥有同代Store生命周期；maintenance admission拥有接纳提交；History/Store保留真实文件与账本权威。不把所有操作装进一把全局大锁，也不强迫它们用同一种context manager。长History锁若是现有文件协议所需可以保留；模型或网络等待不能加入。

### 8.2 实施单位与验收

先用调用图列出真实锁组合，再按业务族迁移。冷准备完成后公开可用资源；普通借用不得隐式重新触发全库恢复。prepare后必须检查当前身份、授权和revision。commit成功后wake失败保留原job，沿现有恢复路径唤醒，不能再建一个job；commit失败不得wake。

首个反例为“取得Store后立即重绑/prepare后撤权”：旧操作不能在新库提交，也不能凭旧授权继续。补充旧借用未退出、批量中一个观察失效、重复review、commit确认后wake失败及取消后的效果核对。抽样复用阶段一字段/排序/引用/成本基线；引入额外全库扫描或长锁阻塞不算收敛完成。

完成时路由及普通caller不再组合`_account_lock/registration_lock/borrow/History`；必要顺序只留在拥有该业务操作的内部协作者。

## 9. S6：Wiki业务操作独立于HTTP/App/MCP调用约定

当前`app_wiki`复用普通route验证，旧null错误不能重报；但仍查`APIRoute.dependant`、反射调用HTTP handler并按函数补依赖。这使新增操作需要懂另一transport的内部签名。[E12]

推荐在既有`server/wiki/`模块暴露业务操作，复用`wiki/contracts.py`输入。HTTP/App/MCP负责各自认证、参数与路径解析、响应映射；业务操作负责当前actor、task grant、client scope、vault/job归属和具体效果权限。可信context来自入口认证和服务端绑定，不能是客户端填写的`authorized=True`。

按以下顺序迁移，每个业务族一次迁完全部真实入口：

1. 查询与读取：search/read/read-many/documents/blocks，保持defaults、filters、compact/full、cursor、revision及引用坐标。
2. job读取与控制：list/detail/output/cancel/retry/verify，保留requester/task/client归属和原执行退休门。
3. 维护接纳：maintain/curate/selections/batch，调用S5完整操作。
4. 编辑、发布、恢复、snapshots，保留owner-only、History效果以及二进制/流式响应区别。

App现有白名单、MCP task grant和普通账户权限都不扩权；不同transport不要求相同HTTP码，但对等业务许可必须一致。抽取操作不等于产品已支持多App授权。

首个反例为同一非法输入经三个入口：null、缺正文、错误类型、额外字段、非法路径、跨账户/client/task、撤权和无效grant。测试合法差异，并验证retry仍受C6和Wiki效果门限制。完成时App不再直接调用HTTP handler、不反射依赖、不给目标函数名打补丁；也没有新dispatcher复制同样规则。

## 10. S7：会话加载模块拥有一次读取及其应用规则

当前`hydrateSession.ts`已有detail、BG/Research读取与账户/服务代次判断，选择、归档、重连等调用方仍传start/current。继续深化已有模块即可，不新建store或泛化reducer。[E13]

```ts
// 拟议入口；保留既有Zustand与领域合并函数。
loadSessionView(sessionId, reason): Promise<LoadResult>
acceptSessionEvent(event): void
cancelSessionViewRead(operation): void
```

加载模块自行捕获account generation、session ID、service generation、原读取基线及有效期。detail接受后建立可信服务代次，再应用依赖它的Research快照；允许并发启动读取，但应用结果必须关联原基线。取消读取只取消观察，不取消后端请求。

| 数据 | 保留的合并权威 |
| --- | --- |
| transcript | 原消息seq与历史窗口，旧detail不能清掉新WS消息 |
| dispatch/active attempt | 服务generation/revision与原request/attempt |
| questions | 读取中已关闭不能重开，新问题不能被旧空快照删除 |
| native subagents | 原身份与领域状态，终态不被旧running回退 |
| Research | 原job更新与快照基线，浏览器收讫独立于父模型处理 |
| BG | 原任务记录与读取前基线，期间新增不能被旧列表删除 |

完整迁移选择、归档查看、重连，以及确实调用同一加载入口的新建/fork/委派跳转。UI仅表达要读哪个会话；WS提交已识别事件。不得顺带重写发送/unknown恢复流程、取消Research浏览器收讫账本或改变任务展示产品规则。

首个反例是读取开始后问题关闭、原生任务完成，再收到旧快照。补充切账户、旧服务响应晚到、快速切换会话和辅助请求失败；一个Research读取失败不应使已成功detail变成队列不可用。以`hydrateSession.test.ts`、`DispatchQueue.test.tsx`及受影响生产hook验证，不只测试私有合并函数。

## 11. S8：各owner提供排空事实，安装层同步封口

当前main集中读session、BG、Research、delegation、Wiki、App、login、scheduler、DB的私有任务与锁。原检查到封口之间无await保证要保留；优化的是各域事实的归属，不是删检查。[E15]

```python
# 拟议快照值；由各owner即时推导，不持久化。
@dataclass(frozen=True)
class DrainFacts:
    ready: bool
    blockers: tuple[str, ...]

def drain_facts(self) -> DrainFacts: ...
```

各owner负责自己的prepare、spawn、执行、问题、交付观察、迟到命令、退休和writer责任；空集合/计数只作内部实现。安装层显式列出参与者，不能动态发现后假定“所有owner都已登记”。facts是观察，不自动取消工作或启动新任务。

自然排空仍按现有产品政策：有实际工作就拒绝此次交接，等待完成；全部可交接才在同一同步段关闭所有producer。**无await只保护同一事件循环的交错，不能替代线程/外部producer的准入同步。** 这些producer必须沿原owner的锁/闸门证明封口后不能进入，不能用一轮快照外推安全。

逐项阻塞原输入、待回传材料、RPC/question、迟到spawn、App停止、DB writer、Wiki借用、调度回调和失败Owner移交，交接均应拒绝。全部事实满足才成功。测试检查与seal之间不能插入新工作；main不再了解第三方scheduler的`_pending_futures`等内部字段。后续新运行时只在所属owner更新facts，安装层仅在新增业务域时才变更参与清单。

## 12. 串行PR计划与每项交付

阶段内可并行调查或编写独立分支，但**按以下依赖合并并验证；阶段三产品实现须等G2通过**。协议只读调查与最小反证可提前给S4提供事实。

| PR | 完整改动 | 必须删除的旧责任 | 完成证据 |
| --- | --- | --- | --- |
| 2.1 S1返回接管 | 公共入口、结果类型、BG/Research/委派终态与问题全部迁移 | 来源共同receipt/唤醒/提交顺序及bool混合结果 | ACK丢失/材料删除/Owner失败仍只有原输入 |
| 2.2 S3有效配置 | 保存/使用/捕获操作，route与Owner/Research实际入口迁移 | route锁序、分散继承拼装、含糊配置容器 | 准备中变更不混配，撤权即时生效 |
| 2.3a C4输入证据 | root/steer持久unknown、terminal/receipt/恢复/UI全部消费者 | root无条件done清reason | 重启后首输入未知仍可见，无自动重放 |
| 2.3b S4+C5/C7 | 能力/事件合同、根回答引用、结构化问题、现有两adapter诚实迁移 | 由stdin猜能力、拼全部text、提早降文本 | 假runtime/真实管道证明合同，注明外部未证范围 |
| 2.4 S2退休查询 | 按原execution的query/wait，Wiki及真实生命周期消费者迁移 | 消费者读session整体退休集合 | A退休/B活动互不误判，迟到spawn仍有owner |
| 2.5 S2b结算 | Owner拥有分类到commit观察与frames交接 | Manager回取current/frames编排 | commit后投影失败、重复/迟到terminal正确 |
| 2.6 S5受保护Wiki | Registry与maintenance完整业务操作 | 路由/普通caller锁序和代次知识 | 重绑/撤权/借用退出/commit-wake反例 |
| 2.7 S6 Wiki入口 | 按业务族迁HTTP/App/MCP | 反射调用另一HTTP handler和重复校验 | 对等输入/授权矩阵及合法差异 |
| 2.8 S7会话加载 | 统一读取身份和基线，迁选择/归档/重连等实际入口 | 页面传baseline/current的义务 | 旧快照/问题关闭/切账户/旧服务响应 |
| 2.9 S8排空 | 各owner facts+安装显式同步组合 | main私有字段清单 | 每类原责任阻塞交接，封口无竞态 |

2.3a与2.3b可以多个实现提交，但G2必须两者完整；2.7可按业务族连续提交，每族不能只迁一个入口后留下旧反射旁路。S5–S7的独立性用于减少开发等待，不用于取消已选定出口。

每个PR描述只需交代：为什么现在要改、原事实与writer、这次收回的调用知识、提交/失败边界、全消费者与删除清单、首个反例和实际验证结果。不要给每次小修改再创建一套计划/ADR/事故报告。

## 13. 持久化、迁移及回退

### 13.1 持久格式

预计最可能涉及持久语义的是C4首输入观察、C5根最终回答引用和C7答案结构。先检查现有列/消息metadata能否准确表达；能复用则不新建表，但“没有新列”也不代表没有格式语义变更。

每项变化同步当前存储格式、账户导出/导入、离线转换与验证；历史数据没有证据的字段不得补造。旧terminal可保留旧范围，旧最终回答不能从不完整材料臆造为经验证的native最终结果。沿仓库当前离线存储合同执行，不添加启动时猜修复流程。[E16]

### 13.2 代码回退与运行中责任

只回退未启用的新代码可以撤对应PR；已产生新语义数据或已接纳工作时，先停止新准入并自然排空，确认旧版本能读取该格式和未知状态，再切换。格式不兼容则不能直接运行旧二进制，必须采用已验证的转换或向前修复。

回退不删除unknown输入、来源材料、原资源reference、Wiki receipt或已提交terminal；不以“回到旧版本”重放未确认工作。新API启用后不保留两个生产writer；临时兼容入口只能薄委托同一实现，并在本工作包完成时删除。

## 14. 验证分层、G2门槛与阶段三交接

### 14.1 证据强度

| 层次 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 真实SQLite+可控owner/runtime | 输入唯一性、commit、逐输入未知、消费者结果 | 外部provider支持原生闭合 |
| 真实管道/子进程替身 | 完整帧、关联、背压、退出、迟到spawn | 某发行版实际事件含义 |
| 锁版真实runtime最小实验 | 该版本/feature/配置下的真实协议行为 | 所有版本/全部模式都等价 |
| 产品集成与发布检查 | 真实入口、UI、迁移和安装交接 | 没运行的场景或另一发布版本 |

每项检查一个执行者；代码、依赖、配置、环境未变时复用已有成功证据。定向回归后按`scripts/check.sh`完成代码提交门禁；若实际发布，再遵循`docs/testing.md`的现行release matrix。共享runtime/Session改动覆盖Claude与Codex离线行为；本安装实机范围按仓库合同，不自行安装/认证Claude。Wiki专项基准仍按既有opt-in约定。本文没有执行这些检查。[E17]

### 14.2 G2全部成立才能进入阶段三

- [ ] G1交接已有对应基线与证据；阶段一已完成行为没有被重复实现或回退。
- [ ] S1/S2/S2b/S3/S4/S5/S6/S7/S8各有完整实际入口、全部消费者、失败路径与旧路径删除。
- [ ] C4覆盖首输入和追加；持久发送事实先于输入效果边界；两个硬崩溃夹点、terminal、服务重启及前端恢复均保全unknown，不自动重放。
- [ ] C5有原输入集合、完成范围及根最终回答引用；App/委派不再拼同attempt全部文本。
- [ ] C7的human/system_expiry/closed结构与来源持久保全；普通/原生/代答/到期/自动关闭/晚答路径全部闭合，没有新输入或第二问答账本。
- [ ] S2可精确观察A而不误停B；publisher事实不与资源退休混写。
- [ ] S2b保持commit后观察，重复/迟到terminal不新增结算。
- [ ] S5/S6重构保持Wiki领域授权、效果与阶段一相关成本/语义基线。
- [ ] S7不让旧快照回退问题、任务与消息；S8保留所有owner及同步封口保证。
- [ ] 格式及搬迁合同同步；历史缺证据如实表达，回退路径可解释。
- [ ] 现有两后端离线合同与适用提交门禁通过；实际运行的检查与未运行检查分开记录。
- [ ] 不用fake完成证据声称Codex完整原生尾部已解决；该门槛留给G3真实验证。

### 14.3 给阶段三和阶段四的交接包

| 交付 | 接收者可以直接复用 | 接收者还必须自行证明 |
| --- | --- | --- |
| C1绑定与原资源reference | 原owner/attempt/work归属、迟到回报隔离 | 原native ID映射与进程范围 |
| C2入口与来源分类 | 独立返回完整接管及队列政策 | 新后端无需重写此政策 |
| C3 capture与能力入口 | 实际配置选择、来源解释、效果前授权 | 原生工具/凭据/限制确实安装 |
| C4首输入/追加观察 | 未知保全、receipt和恢复/UI | ACK/claimed/processed的真实含义 |
| C5 scope与最终回答引用 | Owner如何结算、业务消费者如何取结果 | 根/后代/父续跑闭合及最终item识别 |
| C6退休操作 | Wiki/App按原执行等待 | 连接/host/后代/迟到spawn真实退出 |
| C7结构化问题 | 产品question与原通道生命周期 | 协议答复/关闭/取消具体方法 |
| C8排空接入 | 安装显式组合与封口 | 新runtime所有未收束责任已纳入 |

交接记录包含固定代码SHA、实际接口所在文件、持久语义变化、已运行证据、已删除路径及真实外部限制。阶段三若发现C5等合同不足，先用最小反例修订公共合同和受影响消费者，再接入；不能在adapter里悄悄另建一套业务结算。

## 15. 文档归属与固定源码依据

实施中当前合同归`docs/implemented/`的所属主题，未完成范围归`docs/plans/`，组件关系由`docs/architecture.md`导航，验证与本机操作分别归`testing.md/local-wsl.md`。`architecture-contract.md`只拥有执行/所有权等通用合同，不把Wiki全部细节、前端每个合并规则及每次实施过程堆进去。完成一个计划后合并必要合同与理由、删除被替代计划，并同步索引和入站链接。[E18]

下面引用固定在本次读取的HEAD；行号用于复查当前判断，实施时先核对目标分支变化。附件与HTML是设计输入，当前源码及当前合同拥有更高事实权重。

- [E01 当前四阶段路线、产品行为与阶段一状态](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/engineering-runtime-roadmap.md#L16-L159)
- [E02 统一返回接管的现有提案](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/task-return-admission.md#L1-L192)
- [E03 BG当前接管及来源停止/unknown](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/turns.py#L600-L667)
- [E04 Owner当前分类与结算回绕](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1443-L1480)；[terminal提交观察和frames](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/turns.py#L177-L212)
- [E05 Wiki当前原执行退休检查](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/wiki/job_execution.py#L181-L208)
- [E06 Agent配置路由](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/routers/agents.py#L132-L175)；[Session配置路由](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/routers/sessions.py#L90-L150)；[配置捕获及prepare](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1192-L1232)
- [E07 Harness复用推断](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/harness/run.py#L570-L576)；[Owner打开steer](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1370-L1395)
- [E08 追加输入证据与完成保全](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/db/client_submissions.py#L171-L209)；[普通输入terminal提交](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/db/sessions.py#L496-L529)；[receipt恢复](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/submission.py#L15-L75)
- [E09 App当前回答选择](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/app_agent.py#L280-L298)；[委派当前回答选择](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/delegations.py#L1590-L1620)
- [E10 结构化答案当前被格式化的位置](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_questions.py#L134-L149)
- [E11 Wiki当前锁序与提交](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/wiki/job_execution.py#L39-L76)；[取消效果核对](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/wiki/job_execution.py#L211-L227)
- [E12 App Wiki反射入口](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/routers/wiki.py#L50-L139)
- [E13 会话快照读取、基线与辅助读取](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/web/src/lib/hydrateSession.ts#L49-L168)
- [E14 当前架构候选及推荐范围](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/engineering-runtime-roadmap.md#L161-L312)
- [E15 安装当前排空与同步封口](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/main.py#L483-L559)
- [E16 当前存储与发布交接合同](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/implemented/current-storage-format.md)；[账户导出导入](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/backup-and-migrate.md)
- [E17 当前验证政策](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/testing.md)
- [E18 当前文档权威分工](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/AGENTS.md#L1-L58)
- [E19 当前追加先记录再发送、首输入启动边界](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1739-L1756)；[首输入启动](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1306-L1318)
- [E20 问题持久结果、关闭与系统到期](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_questions.py#L286-L324)

历史评审输入：[返回接管与深化候选](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/review/architecture/architecture-review-20261028.html)、[全库架构候选](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/review/architecture/architecture-review-codebase-20261028.html)、[前后架构对照](https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/review/architecture/architecture-before-after-20261028-131512.html)。其旧缺陷、测试和“当前”均限定在原版本，不直接移作今天的事实。

[E01]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/engineering-runtime-roadmap.md#L16-L159
[E02]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/task-return-admission.md#L1-L192
[E03]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/turns.py#L600-L667
[E04]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1443-L1480
[E05]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/wiki/job_execution.py#L181-L208
[E06]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/routers/agents.py#L132-L175
[E07]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/harness/run.py#L570-L576
[E08]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/db/client_submissions.py#L171-L209
[E09]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/app_agent.py#L280-L298
[E10]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_questions.py#L134-L149
[E11]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/wiki/job_execution.py#L39-L76
[E12]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/routers/wiki.py#L50-L139
[E13]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/web/src/lib/hydrateSession.ts#L49-L168
[E14]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/plans/engineering-runtime-roadmap.md#L161-L312
[E15]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/main.py#L483-L559
[E16]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/implemented/current-storage-format.md
[E17]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/testing.md
[E18]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/docs/AGENTS.md#L1-L58
[E19]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_runtime.py#L1739-L1756
[E20]: https://github.com/Danuvius152/Octopus/blob/d3e90499713f91e2c2a5aecace9c6f153528c810/server/sessions/owner_questions.py#L286-L324

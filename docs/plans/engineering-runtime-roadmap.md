# Octopus 工程收敛与运行时路线

> **Status:** in-progress — 第一阶段及返回接管、配置、原执行/结果/问题与恢复证据已实现；Wiki 操作、前端投影、排空和 Codex/DeepSeek 迁移仍待实施。

> 日期：2026-10-30（Asia/Shanghai）。源码核查基线：`1126d106d95af582ecf989b7c9206ec48c0a09c2`。
> 历史架构候选补充核查：`0ca897e97189c5f213a658d986cbd67f4d69c5e3` 及当前文档工作树，范围见§7.3。
>
> 第一阶段合同更新：2026-10-31，实施起点 `5867e6c5a92e47869859119b56d00995ec995dd4`。
> 第二阶段设计更新：2026-10-31，源码基线 `28c067a090414bdd0513f570320584b2d42d1ee3`。
> 当次设计整合架构访谈 D1–D12 与 Stage 2 补充评审 R1–R8，设计与分票不作为实施证明。
> 初始设计基线不代表当前运行版本。实现、验证与发布分别取证；S1、S2、S2b、S3、S4 及现有问题结算已实施，Wiki 受保护操作、前端投影和排空等范围继续按下文区分。
>
> 本文替代原 `future.md`、2026-10-29 架构路线、2026-10-29 回复调度指南及 2026-10-28 行动指南。按最新源码和本次访谈重新裁决，不按旧票号拼接待办。长期当前合同由 `implemented/` 各主题维护；本文只拥有未来变更的路线、取舍与范围。

阅读：[范围与顺序](#scope) · [三份架构意见的当前适用性](#prior-architecture-reviews) · [第一阶段](#phase-1) · [第二阶段](#phase-2) · [全部历史候选的扩展价值](#architecture-candidate-value) · [第三阶段：Codex](#codex-app-server-scope) · [第四阶段：DeepSeek](#deepseek-design) · [验收与证据](#verification)。

<a id="scope"></a>
## 1. 已确定的目标、保留行为与顺序

按四阶段推进：**当前 bug、Wiki 效率与工程约定 → 架构和接口收敛 → Codex app-server 迁移并解决原生子任务返回 → DeepSeek Harness 正式接入。** 每阶段交付完整责任，后阶段复用前阶段已经验证的接口。协议调查可提前进行，产品切换按此顺序。

```mermaid
flowchart LR
    A[1 当前缺陷 / Wiki 效率 / 文档约定] --> B[2 输入、配置、完成与退休接口收敛]
    B --> C[3 Codex app-server + 完整原生尾部]
    C --> D[4 DeepSeek 原生能力接入]
    P[锁定实际发行版并核查协议] -.提供事实.-> B
    P -.验证可行性.-> C
    P -.验证可行性.-> D
```

这是一项交付顺序选择：DSH 技术上不依赖 Codex 协议；先完成 Codex 可以验证共同接口，避免同时重写两个后端与所有消费者。继续采用模块化单体、SQLite、SessionOwner 和现有 Harness，不增加第二套模型工具循环、SessionManager 或全局任务总线。

### 1.1 自动返回继续逐条处理

BG、Research、命名 agent 的终态和问题返回，继续保留原 source、delivery、work 与 receipt，按**成功接纳顺序**进入原会话，每次派发一条。来源完成时间、提交时间和成功接纳次序不能混称同一个顺序。

保留既有例外：普通 Stop 暂停派发并保留积压；暂停后新人工纠偏可以先执行，但不自动恢复积压；显式恢复后沿原顺序处理。人工 steer 的既有资格与后端能力另行判断。依据：[Owner 调度](../../server/sessions/owner_runtime.py)、[队列回归](../../tests/test_session_dispatch.py)。

本次明确撤回旧稿中的 collect-ready、StartBatch/AppendBatch、自动返回 steer、默认 join-all/wait-any、notification-only，以及正常派发时新增人工优先政策。它们会改变产品行为，不能作为修 bug 或 app-server 迁移的附带优化。多条回复排队本身不再列作缺陷；永久拒绝后无效重试仍须修复。

### 1.2 原生子任务与独立返回分开

原生子任务由 provider runtime 在当前逻辑请求内创建、管理和回报；BG、Research、命名委派拥有独立工作身份，允许父请求先结束，再以新输入回流。**修完整原生尾部不改变独立返回的队列政策，也不让父请求等待所有独立工作。**

本次 Codex 目标包括根模型先输出后的受管后代、后代结果被父模型处理、父再次派生与最终汇总。旧稿的“A 请求内迁移可先交付，B 尾部另选”不再是本次切换标准；两部分可以分步开发，缺少尾部证据不能宣称迁移目标已完成。

允许统一重构 Claude、Codex、DSH 的公共责任；保留各后端真实能力与证据差异，不把 Claude 的现有实现当作不可改变的模板。轮内引导、双向通信、完整完成范围和跨请求宿主复用是四件事。

### 1.3 默认配置允许改变

用户确认的使用方式是：Agent 默认配置可以改变，旧会话需要时自行调整配置再继续。**因此，不能仅因新默认值使旧会话暂不兼容，就认定保存成功是 bug，或强制禁止保存。** 不自动固化所有旧会话 override，不静默换 backend 或转换历史。

同后端换模型通常可通过会话 model override 处理；跨后端凭据还需选择匹配 credential。工具策略当前来自 Agent，并非所有字段都有会话 override。配置应让用户看清实际继承结果与可修正原因；执行前仍须验证实际 backend 能兑现的约束。具体分类见第一阶段。

<a id="prior-architecture-reviews"></a>
### 1.4 三份 HTML 架构意见：保留依据，重新判断适用范围

以下是对三份报告与当前源码的交叉核对，不是新增实施授权。报告主要基于
`0df8b78`，前后对照另含 `before-takeover@88d2284`；本节按本文基线及当前工作树
核对。表中的“已有实现”只陈述所读代码，不替代当前版本的测试或部署证据。

| 报告 | 值得复用的内容 | 使用限制 |
| --- | --- | --- |
| [前后架构对照](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/review/architecture-before-after-20261028-131512.html) | 模块化单体、Harness、账户与独立工作原已存在；后续变化集中在原请求权威、资源责任与 Wiki 效果 | 用于说明现有设计为何形成；图中的“当前”是报告固定版本，不是今日实现 |
| [返回接管与三个深化候选](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/review/architecture-review-20261028.html) | `admit_return` 的完整顺序、原回执优先、来源职责与反例；配置捕获及会话视图时序的边界 | 接管设计以现有架构合同为正文 owner；聊天详情投影不等于已实施的应用/计划列表更新 |
| [全库七项候选](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/review/architecture-review-codebase-20261028.html) | 以调用方泄漏的锁序、生命周期、事务和合并规则定位改进点；逐项说明保留理由与删除标准 | 缺少原始复现附件；缺陷和建议须分别核实，旧优先级不能直接变为当前待办 |

去重后，各项意见在当前代码中的位置如下。未选定候选仍是候选，不因进入此表
就成为第二阶段或运行时迁移的必做项。

| 意见 | 当前事实与路线对应 | 尚需区分的范围 |
| --- | --- | --- |
| 应用删除先确认退休 | [应用删除](../../server/applications.py)已有 `retiring → files` 阶段，失败保留原责任与资料；§2.1 不重复开发 | 保留成功删除后开发聊天可继续的既有行为 |
| 应用与计划列表统一更新 | [应用列表](../../web/src/lib/applicationList.ts)、[计划列表](../../web/src/lib/scheduleList.ts)已有业务入口；合同归[列表协调](../implemented/frontend-list-reconciliation.md) | 这不证明聊天详情、Research 卡片、问题与 transcript 的合并责任已经集中 |
| Connector 首次绑定原子性 | [ConnectorManager](../../server/connector_manager.py)已有外部身份与 installation 串行化；[业务事务](../../server/db/connectors.py)以 upsert 一同保存身份和秘密 | 不再沿旧报告认定“先查后写导致首次绑定冲突”仍待修；锁与事务分别保护真实的不同责任 |
| 普通/App Wiki 请求校验 | [App 入口](../../server/routers/wiki.py)已复用路由正文校验，旧 null 缺陷已有修正路径 | 操作白名单、路径映射与特殊依赖仍由入口编排；完整 adapter 深化是另一项候选 |
| 返回接管 | [BG](../../server/sessions/turns.py)、[Research](../../server/research/manager.py)、[委派](../../server/delegations.py)已使用同一公共接管操作，见[当前合同](../implemented/architecture-contract.md#公共返回接管) | 保留来源重试、业务结算及独立返回消费政策，后续来源沿同一接口接入 |
| 原执行退休与单轮结算 | 原 OwnerRuntime 绑定 driver/资源观察并唯一结算；[Wiki](../../server/wiki/job_execution.py)通过公开查询及原 work 清理证明守住重试；结果关联已集中 | 当前合同归[原执行观察](../implemented/architecture-contract.md#原执行的观察)与[结果读取](../implemented/architecture-contract.md#原请求结果读取)，新协议尾部仍另行实施 |
| 配置变更与请求捕获 | 默认保存归 AgentManager，覆盖与捕获归[会话配置操作](../../server/sessions/configuration.py)；路由锁序已退出 | 保持§1.3允许改默认、准备锁内捕获及效果前撤权；不以保存或捕获延长授权 |
| Wiki 注册与受保护借用 | [Registry](../../server/wiki/registry.py)、[维护准入](../../server/wiki/maintenance_admission.py)及路由间仍存在外部锁序/代次编排；§3.7 固定 S5/S6 范围 | 不是已复现死锁；按业务操作收回协议，History 与 Store 的真实权威继续保留 |
| 聊天详情投影 | [hydrateSession](../../web/src/lib/hydrateSession.ts)仍显式协调 detail、Research、读取基线与服务代次 | 已选择 S7 集中读取生命周期，保留领域合并及视图观察责任，不整体重写前端 |
| 安装交接与自然排空 | [main](../../server/main.py)仍集中读取各域私有任务、锁与退休集合；§3.7 选定各域事实与交接收据 | 已选择 S8；未来运行时新增责任继续进入所属 owner，不由安装层猜私有布局 |

三份报告支持的共同方向是减少调用方必须了解的内部规则，继续利用已有责任模块。
它们没有证明统一 Job 总线、额外持久执行账本、通用 Repository 或全仓拆类能带来收益。
全部候选及次级机会的扩展价值、推荐边界与验收见§3.6–§3.9；不能把候选数量、
文件长度或接口名称当作架构质量结论。

### 1.5 已确认的架构评价标准与本次评估范围

用户已确认：总代码量不必下降，主要看规则是否有明确归属、一次变更是否集中、
调用方是否减少锁序/提交顺序等知识，以及完整行为能否通过公开入口验证。
可靠性、权限、结果真实性和既有产品行为继续作为约束；性能收益单独测量。

本次须评价三份 HTML 的**全部历史架构候选与次级建议**，结合本文的运行时路线及
[Wiki/App 后续计划](../plans/wiki-future-plan.md)判断扩展价值，不限于 Codex/DSH
迁移的直接前置。全面评估不等于全部实施；已有实现、值得继续深化、有明确触发条件
和不建议采用的方向分别给出结论。第二阶段已按 2026-10-31 访谈确认，范围以§3
为准；后续产品取舍仍须独立确认。

判断依据是已知变化：新运行时及完整原生尾部、持续变化的模型/凭据/工具配置、
Sapphire/SmartReader 与受限 App 客户端、更丰富的任务展示，以及这些资源的发布排空。
不以假想第四种后端、分布式部署或无限通用性为由增加机制。

<a id="phase-1"></a>
## 2. 第一阶段：当前可处理的问题、Wiki 效率与工程约定

### 2.1 已实现的行为与当前 owner

第一阶段的代码、定向审查、默认提交门禁及本机离线/实机验证已完成；G1 的
最终交接和部署仍以对应版本的审查及发布回执为准，源码状态不证明生产已升级。

| 行为 | 当前合同与已实现边界 |
| --- | --- |
| BG、委派终态和问题返回 | [执行合同](../implemented/architecture-contract.md#输入完成与资源退休)拥有原身份、receipt 优先、永久停止、unknown 有限观察及来源 writer 保责；[委派](../implemented/agent-collaboration.md)拥有 round/问题生命周期；Research 原分类保留。 |
| Agent 默认与旧会话 | [模型设置](../implemented/model-reasoning.md)允许合法默认保存，旧会话保留 backend/历史，按有效组合修正后继续；共享纯校验、实际参数、捕获和效果前授权均已覆盖。 |
| Wiki 读取与查询说明 | [Wiki 合同](../implemented/wiki-development-summary.md)拥有四入口长度边界、实际指南注入、同版正文复用及完整覆盖；评测失败完整收束先于 API/DB 退出。 |
| compact 投影与健康摘要 | lexical 页内章节批取、exact 先过滤/count/分页，准确 count 与有界样本已实现；排序、Unicode、分页、live revision 与维护准入保持。 |
| 可复现成本与环境 | [验证方法](../testing.md#wiki-evaluation-method)、[解析约束](../../constraints-wiki.txt)和冻结合成参考保留原判据与适用条件，专项仍独立 opt-in。 |

删除前退休、排序事务和列表协调的既有合同继续保留。应用成功删除后旧开发
聊天仍可重新创建目录，不恢复“成功后永久禁写”提案。公共返回接管及[配置保存与请求捕获](../implemented/model-reasoning.md#saving-defaults-and-repairing-an-existing-session)已完成；
完整原生尾部及统一退休查询/Owner 排空接口仍属于后续阶段。

### 2.2 投递错误分类：先修完整行为，再收回共同编排

返回真值表、各来源的业务材料/诊断/恢复差异及真实消费者统一归
[执行合同](../implemented/architecture-contract.md#输入完成与资源退休)。当前回归
经过真实 SQLite/Owner，覆盖永久拒绝、ACK 丢失、冲突、问题过期、人工回答、
删除/归档与写入失败；通过现有命令重入保责，没有新返回账本或常驻观察器。

第二阶段已完成[公共返回接管](../implemented/architecture-contract.md#公共返回接管)，
保留来源结果、接纳、父处理、通知和退休分别取证；未知不授权重放，正常返回仍按
成功接纳次序逐条派发。各来源不同的业务结算与恢复策略继续由其原 owner 维护。

### 2.3 Wiki：优先减少数据搬运，保持检索与阅读合同

当前投影、NUL 兼容例外、完整章节字段匹配、exact 身份语义及健康事实归
[Wiki 合同](../implemented/wiki-development-summary.md)。冻结参考逐字段保留页序、
total/cursor、revision、sections/role、matched fields 和原文坐标；准确性先于搬运。
最终完整测量遵循最初阈值；早先失败及参考身份修正的来由保留，不能用新容差
或删字段换取通过。固定100篇/长文/错误增长的结论及模型用量限制见该合同的
[版本化证据](../implemented/wiki-development-summary.md#historical-evidence)。

full 当前页依赖批读、宽查询/深分页计划、reader handle、多库公平性和语义检索
保留测量后再选择的条件。已有章节 FTS5、中文分词/bigram、去重、overview/compare
及增量同步不重写；不能通过减少候选牺牲召回。查询调用、SQLite 搬运和 live 文件
读取分别衡量，不把 JSON 小或调用少直接解释为 token/整体延迟收益。

### 2.4 文档与工程约定

一个解释有一个正文 owner：根 AGENTS 管高频工程规则，implemented 管当前合同，
plans 保留未完成范围，testing/local-wsl 管验证与本机操作。活动审阅和临时材料
退出前将必要理由及证据归回 owner；persona 管角色，memory 只存偏好与短入口。
代码、受影响验证和审查收敛后统一整理长期文档，不逐补丁复制过程记录。

当前解析链使用现有 venv/pip 和固定约束，前端沿用锁文件。新字段及校验边界已
同步类型，没有全仓 strict、ORM 或包管理器迁移。Harness 的能力拒绝、当前
Wiki 格式和完整原生尾部范围各有权威；原生卡片可见不证明完整尾部已完成。

<a id="phase-2"></a>
<a id="harness-protocol-contract"></a>
## 3. 第二阶段：让既有所有者提供完整的小接口

本节是第二阶段范围与 G2 出口的长期 owner。S1、S2、S2b、S3、S4 与当前问题
结算已实施；Wiki 的 S5/收窄 S6、前端 S7、排空 S8 及整体 G2 仍待完成。
2026-10-31 访谈选择九项 S1/S2/S2b/S3/S4/S5/S6/S7/S8，并按 D3/D4/D6/D11
修订旧串行计划：保留 Codex exec 与 Claude 有条件复用；不预建输入 ACK 状态层
或最终答案引用表；S6 只迁移下列现实操作。旧评审的 C4 首输入持久扩展、C5
最终原生 item 引用和完整 App 网关反射替换不再是 G2 条件。

原 Stage 2 补充评审的 R1–R8 已落实到本节。设计审计与补充评审各有固定版本，
不证明实现或产品测试通过。完整原生尾部、新宿主、RPC 接受语义留在第三/四阶段；
本阶段也不保证未来协议永远无需在所属边界扩展。独立返回与问答的现有体验保留。

### 3.1 责任与收益

| 所有者 | 保留的权威 | 收回的重复知识 | 对后续的帮助 |
| --- | --- | --- | --- |
| SessionOwner／会话模块 | 接纳、逐条派发、原输入、问题和唯一业务结算 | 各来源拼接 receipt/唤醒/准入顺序 | Codex/DSH 不另建输入账本 |
| HarnessRun／原资源 owner | 准备、spawn、连接、I/O、lease、停止和退出证据 | 消费者直接窥探内部任务集合 | 换协议不改变 Wiki/App 的退休判断 |
| RuntimeProfile＋私有协议 adapter | 能力、装配、原生事件和发送/完成证据 | 在公共层拼 Claude 帧、由 stdin 猜能力 | 三种后端各自实现真实能力 |
| 来源 BG／Research／委派 | 来源业务结果、材料、重试和放弃 | 接管流程内部细节 | 改善可靠性而不改变回复次序 |
| Wiki Registry／Store／jobs | 注册代次、阅读/写入授权、身份、文件效果与恢复 | 调用者借 Store、拼锁序、重验代次 | 后端变化不外泄到知识发布合同 |

SessionOwner 继续串行裁定状态、异步推进执行；长 RPC、模型、问题等待、prepare 和退出不占住命令处理。后台回报携原 request/attempt/work/backend/lease，不能读取会话最新绑定重新归属。

### 3.2 S1：公共返回接管已实现

当前合同、选择理由、提交与失败边界归[公共返回接管](../implemented/architecture-contract.md#公共返回接管)。BG、Research、委派终态和问题均已使用 `admit_return`，来源共同的 receipt/唤醒/准入编排及旧公开恢复入口已退出。

来源保有材料、业务结果、诊断及重试，逐条派发、Stop/Resume 和独立 work 语义保持。既有回归及真实接纳事务已覆盖丢确认、材料删除、Owner 失败与准备/归档竞争；验证政策仍归[testing.md](../testing.md)。本项完成不代表 G2 整体完成，也不改变后续运行时与安装交接的出口。

### 3.3 S2：原执行与资源观察已实现

原回执、驱动和指定资源分别取证，当前合同归[原执行的观察](../implemented/architecture-contract.md#原执行的观察)。Wiki 已退出会话私有队列/退休集合；它继续拥有 work 关闭、BG/Research 等原资源清理及持久确认。查询 A 不停止 B，App 普通槽不增宿主退出门槛。

模拟进程和原回执回归覆盖宿主复用、取消观察、unknown steer、缺少证明、退出失败与 Owner 故障后清理/归档。失败 Owner 的诊断引用不等于活动资源，也不能在清理尚未证明时放行。没有新增执行账本；新协议的完整原生尾部仍待后续阶段。

### 3.4 S3：配置保存与请求捕获已实现

当前操作、继承、锁内捕获时点和效果前授权归[模型设置](../implemented/model-reasoning.md#saving-defaults-and-repairing-an-existing-session)。AgentManager 与会话模块拥有各自完整保存操作，调用方已退出跨模块锁序及提交后字段刷新；没有新增持久配置副本。

保留这项责任分工，是为了将候选默认是否合法、当前会话能否使用、本次捕获什么、
效果前是否仍有授权分开判断。真实保存/启动/retry 回归覆盖排队与等锁改值、
捕获后不混配及 spawn/frame 撤权；合法默认不会因旧会话不兼容而被否决。

### 3.5 S4：当前能力与恢复证据已实现

显式能力已实现：`can_steer`、`reusable`、`can_continue` 和工具策略分别由 adapter 声明，
Owner、API 投影、Harness 发送、held 策略及 Wiki scope 使用相应能力；四种组合
均已通过真实子进程和工具可见性回归。当前证据含义归[运行时合同](../implemented/harness-layer.md)及[输入送达](../implemented/inline-steering.md#current-capability-and-delivery)。Codex 仍为 exec，Claude held 策略保持。

以下是操作语义，不是要求新增同名大类或存一张能力真相表：

| 接口/能力 | 必须明确的内容 |
| --- | --- |
| prepare/start | 固定原身份和配置；准备失败及迟到句柄有 owner；首次可用工具前完成授权 |
| events | 带原身份的文本、工具、原生任务、问题、usage、错误与完成证据；完整正文与流式投影分开 |
| steer | 后端是否支持、允许的阶段、实际接受层级；明确未接受/接受/未知，不能把所有 pipe drain 当原生 ACK |
| answer | 原问题与原通道、结构化答案、取消和晚答；不把答案当新 prompt |
| completion | 当前 root 结束、请求驱动收束、业务结论分别说明；不以卡片状态或字段改名声称完整原生尾部 |
| interrupt / retire | 请求取消与原资源确实退出分别回报；未退出责任继续可查 |
| reuse | 跨请求复用独立于双向通信和 steer；新请求不得借旧 lease 安装新授权 |

当前 claim 只证明取得执行身份；steer confirmed 只证明本地写入/drain；exec
turn.completed 只证明当前 parser 的 root 结束。公共合同不把这些值提升为
provider accepted/processed。steer、工具策略与跨请求复用独立声明，不从 stdin
形态或 reusable 推导其他能力。当前能力变化须贯通 Owner 与两个现有后端。

按效果证据限制 idle/transient/stale-session 恢复已实施；旧的无输出重播假设及
turn-start resume-ID 恢复路径已退出。当前动作表、捕获依据、预算与边界统一归
[恢复证据](../implemented/harness-transient-retry.md#4-recovery-evidence-in-the-original-owner)。
接纳去重、原身份确认与 attempt 恢复仍是三个合同；不新增持久 ACK，也不声称
continuation 可保证外部效果 exactly-once。两后端的外部文件效果/丢输出回归已
检查实际启动、prompt、结果与退出；重启 claim 与 unknown steer 保持原事实。

新 RPC reader/writer、握手、双向请求关联与完整尾部在§4/§5按实际协议交付；
本阶段不预建通用 RPC 引擎或改变 Claude 首 result/idle 迟到文本的交付保证。

Registry 的独立深化具体化为下文 S5，不再混在运行时协议责任中；S2 则是新运行时消费者隔离的直接前置。

<a id="architecture-candidate-value"></a>
### 3.6 全部历史候选：哪些值得为后续扩展做

以下保留候选的取舍理由；九项范围已由 D1/D12 选定，S6 由 D11 收窄。
选择设计不授权产品实现。优先级依据当前重复规则与现实消费者，不把展示顺序
当成技术依赖，也不因某项不是后端迁移前置就漏掉已选择的完整行为。

| 候选 | 对应的已知扩展与收益 | 判断及主要代价 |
| --- | --- | --- |
| S1 返回接管 | BG、Research、委派已是三个真实调用方；以后修改准入/唤醒/确认规则只改会话模块 | **优先做。** 已有充分复用，不必等新功能；代价是须保留各来源的正文、重试及结算差异 |
| S2 原执行退休接口 | Codex/DSH 改变进程和后代形态；Wiki、App 不必随之理解运行时私有集合 | **优先做。** 隐藏资源表示有直接收益；查询仍需保留原身份和未知证据，不能退化为一个 session idle 布尔值 |
| S2b Owner 内部结算编排 | 完整原生尾部改变何时可结算；将结果分类到原 terminal 的编排收在单轮 owner 内，避免 Owner → Manager → Owner 往返 | **值得纳入。** 收益是完成语义变更集中；风险高于普通 helper 抽取，须保持结果、记录、通知和退休分别取证 |
| S3 配置变更与捕获 | 后端、凭据、模型与工具能力会继续增加；保存与实际请求共用继承规则，路由不再各自拼锁和刷新 | **优先做。** 属于持续变化点；不能把慢准备或当前授权合并进配置快照 |
| S5 Wiki 注册与受保护操作 | 新读取/维护入口和受限客户端会反复遇到重绑、代次、借用与撤权；这些规则只在原 owner 维护 | **优先做。** 已有多个调用方，未来扩展进一步放大收益；主要成本是保留不同操作真实锁范围和冷准备顺序 |
| S6 Wiki 业务入口与 transport 分离 | 现有维护接纳、任务控制及受授权文件变更有跨 owner 规则；SmartReader 经 agent，Sapphire 经文件服务 | **按 D11 收窄纳入。** 迁完被选操作；保留简单操作和 App 网关兼容，不以未选择的直接 App 功能扩大范围 |
| S7 聊天详情投影 | 原生尾部、新任务卡片与诊断会增加事件；选择、归档查看、重连共用快照接收及合并规则 | **值得纳入。** 是已有前端时序责任的深化；必须保留消息、问题、任务卡片的不同合并语义，不能做一个无差别通用 reducer |
| S8 各 owner 提供排空事实 | 新宿主、RPC、问题等待、后台交付与应用资源持续增加；新增资源在所属模块纳入排空，不再同时改 main 的私有字段清单 | **值得纳入。** 运维扩展价值明确；仍须保留显式 owner 清单，以及检查至关闭准入之间的竞态保护 |

S4 共同运行时接口来自本 Markdown 路线，也值得做：已有 Claude/Codex 和明确的 DSH
接入目标，接口变化有真实驱动。应复用 Harness 的入口和资源责任，允许内部结构变化；
不以所有 provider 都使用相似传输为由提前统一完成、steer 或 ACK 语义。

<a id="37-新增推荐范围做到哪里才算有架构收益"></a>
### 3.7 已选边界：做到哪里才算有架构收益

**S2b：原 Owner 结算与结果读取已实现。** [当前结算合同](../implemented/architecture-contract.md#输入完成与资源退休)固定原 Execution 与发布上下文，Manager 回取 current/frames 的旧编排已退出。真实 SQLite 与原 receipt 观察覆盖提交后投影/通知失败、重复迟到 A 不结算 B、失败与 unknown 的既有区别。结果关联已归同一快照读取，三个消费者保持格式和通知差异；S2 的证明范围与新尾部协议分别判断。

**S5：Registry 与现有 Wiki 业务模块共同封装受保护操作。** Registry 拥有同代
Store 的取得、借用和退休；维护准入拥有其业务提交。冷准备不持业务锁，准备后与
效果前复核代次和当前权限。路由/普通调用方不再组合 account/registration/History
锁或读取借用内部状态；锁的正确顺序留在拥有该操作的实现内。History 的文件效果、
Store 的账本事务和恢复责任保留。验收覆盖取 Store 后重绑、准备后撤权、旧借用未退出、
提交失败不唤醒；不能通过一把全局大锁实现“统一”。

**S6：固定实际操作，收回业务规则。** 入口认证并传递可信账户/App/task/scope，
不把客户端目标或 expected revision 当许可，不新增可变 authorized 镜像。原业务
模块按当前权限重验；owner-only、submitter、原 worker/job 控制不能压成 read/write。
MCP 保持主机 HTTP 通路，App 保留现有白名单及反射适配；合法默认值/包装差异保持。
已收回规则不复制回网关，不强制所有路由套 wrapper，不新建统一 Context 类。

| 固定迁移单元 | 操作与实际调用者 | 必须拥有的完整行为 |
| --- | --- | --- |
| W1 维护观察 | maintenance-status、selection 创建/读取；HTTP、现有 App 白名单及已暴露 MCP 工具 | 同代 Store 准备/借用、History 观察、账户/注册保护及 actor 重验；调用方不拼锁 |
| W2 维护接纳 | maintain、maintain-batch、curate；原 HTTP/App/MCP，curate 保持 owner-only | 选择许可、原幂等键、批量共同验证、job/grant/别名/订阅短事务与提交后 wake |
| W3 任务控制 | cancel/retry/verify/readmit；HTTP，App 仅前三者，MCP 仅既有工具；readmit 仍 owner-only | 原 job/attempt/work、当前授权、dispatch gate、原 work 退休；retry 消费 S2 证明 |
| W4 受授权内容变更 | publish/revise/restructure/organize；原 HTTP/App/MCP | 同代捕获、curator/path/source-read/revision 检查、效果前重验、History 原像与 prepared/applied 恢复 |

W2/W4 复用 W1 的同代资源操作，不强迫借用、维护事务和文件写入共享锁范围。
冷构造/恢复在相应业务锁外，发布前重验；维护所需 History 保护保留。批量一项
失效不部分提交；commit 失败不 wake，成功后 wake 失败保留原 job 与原幂等身份。
文件与 SQLite 不构成跨资源原子事务，取消等待不抹去真实效果。

保留 search/read/read-many/documents/graph/blocks/image/info/guide、job 查询及
详情/output 的已有领域操作。保留 History 普通编辑/历史/restore、旧 snapshot
读取/恢复、显式 recovery 和 App bridge 的 owner；snapshot **创建已退役为 410**，
不因旧计划列举而恢复。W4 验证这些相邻操作仍按原许可运行；无新跨 owner 证据
不整体重写。保留搜索/Unicode/cursor、模型字节预算及实际已读证据，不加全库扫描。
SmartReader agent 通路与 Sapphire 文件保存保持，同一次保存不双写。

**S7：会话投影拥有一次读取及其应用规则。** 由现有会话加载/投影模块捕获账户、
会话、服务代次和读取基线，负责 detail 与任务快照的依赖、旧响应拒绝及事件合并；
页面表达目标与用途并拥有观察结束，WS 沿原领域更新。Zustand 继续保存可见投影，transcript 继续
拥有历史窗口与序号，问题关闭和任务更新保留各自规则。删除选择/归档/重连调用方
传 baseline、排序快照的义务，不新增一份权威会话。以快照读取中关闭问题、切账户、
旧服务响应晚到及原生子任务完成后旧快照到达为验收；不能只移动 switch 或拆 store。

读取模块管请求间有效性，领域模块区分真实事件/本地权威更新与其他快照写入。
两个请求共享 running baseline，A 的 running 快照先写 store 后，不能把它与
baseline 引用不同当作实时证据遮住 B 的 completed。两种返回顺序和真实 WS 对照
都须通过公开加载/hook 验证，不只测纯 merge。按原任务/原 work 合法状态处理终态，
不设置全局 terminal 最大。委派重连和卡片轮询用 delegation_id + work_id。
metadata、辅助重试与完整聊天按需读取；辅助失败不使成功 detail 不可用；取消
读取不取消业务，旧成功/失败、删除/归档及切账户均受原观察身份约束。

**S8：安装交接组合 owner 提供的真实排空事实。** 各域从自己持有的任务、准备、
投递和退休引用推导能否交接；main 明确列出参与者并执行安装级检查与封口，不猜
第三方 scheduler 或业务对象的私有布局。不引入通用任务注册中心、持久 idle 标记
或第二份活动计数。当前无 await 的检查至封口保证须保留；线程/外部 producer 的
竞争仍由对应 owner 的已有准入同步保护。原运行活动、迟到 spawn、交付观察者、停止失败
或 DB writer 任一未收束都应拒绝交接，全部满足后才放行。业务结果完成不代替资源退休。

Registry 必须覆盖已排出但未 borrow 的线程；health.stopping 不能代替线程准入
封口。短 guard 不在事件循环等活线程，无法取得同步关系就拒绝交接；取消 await
不证明 writer 退出，shutdown 最后同步仍由原 owner 等待。Wiki 通知 outbox/
receive/返回/ACK 在 sleep 或暂时失败间隙仍有责任；App bridge 准备和未确认清理
不能被 job 终态或 backend 空闲覆盖。HTTP 观察至原 ASGI 完成，WS 只计输入处理。

全部必要 gate 成功封口后才发布 offline_request 与明确成功响应；marker 证明
安全交接资格，不证明进程退出。封口前 busy 不关 producer；第 N 个封口失败不
发布成功 marker，也不伪装普通 busy 重开服务，已关闭部分由原 owner 保责收尾。
同 ID 丢响应后查询/重试读同一成功，异 ID 不得冒领；HTTP、ready 与导出消费者
共同验收，缺 status 不默认成功。不把逐域 try-seal 叫全安装原子事务，不建通用
两阶段提交。无活动责任的常驻循环/held 宿主可在成功交接后由正常 shutdown 退休。

### 3.8 已落实、条件性及不建议的历史意见

| 历史意见 | 当前结论与后续边界 |
| --- | --- |
| 应用删除前确认退休 | **保留已有实现，不重做。** 已有持久删除阶段和原资源退休责任，未来新增 App writer 接入同一生命周期 |
| 应用/计划列表协调 | **保留已有业务入口。** 参考其责任划分推进 S7，不把列表和聊天详情强制合成同一同步模型 |
| Connector 首次绑定 | **保留已有身份串行化与原子 upsert。** 新 provider 沿既有安装/刷新接口接入，不另建 OAuth 事务框架 |
| Wiki App 的 null/正文校验缺陷 | **局部修正已有实现；架构方向继续做 S6。** 参数一致与业务入口独立是两个完成条件 |
| 数据库完整行列映射 | **原建议已有实现。** [RecordProjection](../../server/db/projection.py)及[完整行声明](../../server/db/base.py)集中 SQL 列与字段名，领域解码仍各自负责；不扩成 ORM/Repository，也不强制所有局部查询返回完整对象 |
| Research 重复 Task 登记 | **原建议已有集中入口。** [ResearchManager](../../server/research/manager.py)的 `_register_task` 统一 pipeline/delivery 任务，保留失败清理和关停中迟到交接；多个集合不自动等于重复权威，不再抽成跨域 TaskManager |
| 前端输入接管/恢复编排 | **有价值，暂不作为独立必做重构。** [ChatView](../../web/src/components/ChatView.tsx)与[WS](../../web/src/hooks/useWebSocket.ts)确有跨层协调，但现阶段对用户承诺的发送/恢复行为不变；当新增发送入口、复杂附件恢复或修改接纳 UX 时，将原身份、unknown 确认和草稿恢复纳入同一业务模块。S7 不顺带承担写入流程 |
| 取消浏览器收讫账本 | **不建议作为架构优化。** [Research 回执](../../server/db/research.py)及[卡片](../../web/src/components/ResearchCard.tsx)仍区分浏览器收到状态与父模型处理；它有独立可观察含义。只有产品明确取消该保证时才讨论删除 |
| 删除无数据库执行路径 | **相关执行路径已收敛，不重复列项。** [输入就绪入口](../../server/sessions/base.py)与[回归](../../tests/test_input_readiness.py)要求初始化完成；保留不启动执行的构造/查询不等于保留第二条执行路径 |
| 因大文件或 mixin 多而拆类、统一所有 Job/前端 reducer、再造 Repository/任务总线 | **不建议。** 尚无独立业务责任或已知变化证明这些机制能减少调用知识，反而增加需要共同演进的接口与状态 |

HTML 提到的 BG 退休、应用启动容量、列表竞态、OAuth 绑定及中文路径缺陷仍按其
具体行为证据核对；不是额外五个架构重构项目。本表不以“值得保留”宣称已重新测试
这些历史修复，当前 bug 与性能工作仍由第一阶段和各主题合同承担。

<a id="39-推荐组合真实依赖与退出条件"></a>
### 3.9 已选组合、真实依赖与退出条件

S1、S2、S2b、S3、S4、S5、收窄 S6、S7、S8 已组成第二阶段设计范围。
按完整行为分票，推荐串行顺序不等于 blocking edge；同文件改动应串行安排，
不因此把所有单元连成技术依赖链。没有证据需要一个全仓机械迁移或集成分支。

| 组合 | 真实依赖与实施边界 |
| --- | --- |
| 输入与配置：S1、S3 | 各自已有多个真实入口，可先完整交付；第一阶段修正的错误分类/默认变更规则进入共同合同 |
| 运行时与结算：S2、S2b、S4 | 先收回原 Execution 裁定，再按当前证据修复恢复；能力、结果读取、问题与原资源观察按各自完整行为验收，不等待新 RPC |
| Wiki：S5、S6 | W2/W4 复用 W1 同代资源操作；W3 的 retry 需要 S2 原 work 证明，其余不等 Codex 切换或直接 App 产品 |
| 前端：S7 | 先以现有 detail/Research/问题合同验证投影边界；新运行时事件语义明确后接入，不让投影解析 provider RPC |
| 交接：S8 | 各域事实先通过真实安装判断消费，最终封口/收据贯通 HTTP/ready/导出；新运行时的额外资源在后续阶段加入所属域 |

每个被选择的单元都要交付实际入口、全部受影响消费者、失败路径和旧路径删除，
不把必需收尾留到后续阶段。完成证据至少包括：一项明确的未来变更场景、调用方删除
的协议知识、唯一权威仍在哪、最容易推翻设计的行为测试，以及保留下来的真实差异。
若重构后调用方仍须持同样的锁、传同样的基线或拼同样的完成步骤，就没有实现本次
确认的架构收益，不能仅凭新接口或新目录验收。

### 3.10 结果与问题消费已实现

当前关联、三个消费者的合法差异、空正文和旧 terminal 边界归
[原请求结果读取](../implemented/architecture-contract.md#原请求结果读取)。结构化答案、
可信来源、纯校验/claim/保存/发布顺序和旧记录兼容归
[问题结算](../implemented/architecture-contract.md#问题结算)。相关旧规则副本已退出。

实际回归覆盖多 attempt、carrier、空答/失败正文、丢失 terminal、父代理代答、
非法回答修正、到期/关闭竞争和保存失败；浏览器验证正常 UI、刷新历史与实时结构
一致。没有新增最终答案表、问题状态机或活回调恢复；这些完成不代表整体 G2 已通过。

### 3.11 G2 的反例与完成门槛

| 反例 | 必须验证的响应 |
| --- | --- |
| 原返回已接纳但丢确认、材料随后删除；Stop 后新返回与人工纠偏 | 原身份唯一，已有 receipt 不 prepare；来源结果不被交付失败覆盖；成功接纳顺序、暂停 backlog 与显式 Resume 保持 |
| 无本地输出但外部已有工具效果，随后 idle/transient/stale-session | 依据协议决定 continuation/停止，不能仅凭无输出重播；检查模型启动次数、实际 prompt、request/attempt、结果及退出 |
| 原 terminal 已提交但通知失败；A 结束后 B 复用宿主 | outcome/提交/通知分别取证，无重跑或错结算；A driver 完成不代表宿主退出，查询/取消等待不停止 B |
| 排队、等配置锁、捕获后分别改默认及撤权 | 捕获时点保持，retry 不混配；spawn/frame write 仍拒绝撤权效果 |
| 三种结果消费者与问题竞争 | 内容/格式/失败/通知保持；结构和真实来源可读，无效答案不消费，持久失败不伪成功 |
| Store 重绑、撤权、批量失效、commit/wake 失败及文件效果后取消 | W1–W4 同代、当前权限、整批事务和恢复责任完整；无双 job/双写，保留项不扩权 |
| 两次成功快照共享 baseline，夹真实 WS、旧错误或新委派 work | 快照不冒充实时更新；合法新状态不被遮住，不串轮次；从公开加载和真实 hook 验证 |
| 线程已排出未 borrow、通知 sleep 但待 ACK、bridge 清理失败 | 原域报告责任，安装拒绝；空闲循环/连接不永久阻塞 |
| 第 N 个 gate 失败、成功响应丢失、同/异 ID 或缺 status | HTTP/ready/导出一致，失败无成功收据，原 ID 可确认真实成功；自然排空和资源退出保留 |

G2 要求九项范围、W1–W4、真实消费者、上述失败路径与旧规则退出全部完成；每票
包含自己的回归与当前合同更新，出口只收敛跨票证据，不代替未做的局部验收。
持久结构变化同时覆盖存量兼容、资料导出/恢复及真实回退边界，不清用户资料。
共享会话/Harness 保留 Claude/Codex 离线覆盖，真实模型沿当前 Codex 验证范围；
提交/发布按 testing 的不同门禁，证据有效则复用。G2 不要求未来 RPC、完整尾部、
新宿主、输入 ACK 层、最终答案引用或完整 App 网关替换；这些已替代旧更宽条款。

<a id="codex-app-server-scope"></a>
## 4. 第三阶段：Codex app-server 迁移，完整解决原生尾部

### 4.1 当前问题是什么

普通 Codex 仍用 `exec --json`。[codex.py](../../server/harness/codex.py) 将 `turn.completed` 映射为 result 并结束逻辑流；[codex_activity.py](../../server/harness/codex_activity.py) 的 native drain 只观察首次捕获 EOF 之前已写出的记录。之后原执行退休，未完 helper 可以被标为 `interrupted/parent_done`。对应 [回归](../../tests/test_native_subagent_retirement.py) 明确允许父 success；[观察回归](../../tests/test_native_subagent_stream.py) 不证明父模型已经读到 helper 报告。

所以不能笼统声称“所有子返回都被丢包”，也不能把用户要求降格为可选 UI 增强。**本次要改变的是完成范围：属于本请求的原生后续工作尚未处理，不能只因父首次结束就成功结算并杀掉它。** 具体用户历史事件是否发生文本丢失，仍需其原始事件证据；本轮没有伪造该复现。

已有短时 [codex_control](../../server/harness/codex_control.py) 和 tool-free [codex_text](../../server/harness/codex_text.py) 各有独立用途，不等于普通对话已迁移。one-shot 不改回完整 agent 工具循环。

### 4.2 Claude 旧方案的取舍

Claude held process 的 idle handler 曾解决真实问题：首个 result 后，迟到子任务卡片和父正文不再直接丢弃，保留原 request/attempt/work，旧 receipt 不重开。现有 [离线回归](../../tests/test_subagents.py) 覆盖这项行为。

它适合作为**迟到事件归属和展示保全**的实现经验，不是本次完整任务结算的最佳共同模型：receipt 已先完成，委派消费者可以先取走回复；idle 追加正文不会自动补发已交付的委派结果。held 还受空闲回收、容量与配置切换影响，不能保证所有尾部都被等待。

统一重构的方向是让三家共同遵守“主输出≠请求完成、原任务归属不变、取消覆盖受管资源”，由 adapter 提供各自可证明的结束事实。Claude 已有迟到记录回归保留；一旦以完整请求收束替代旧 idle 成功补写，相应旧结算旁路、动态重绑与文档同时退出。不要求 Claude 提供其协议没有的逐输入 ACK，也不为本机验收另行安装或登录 Claude。

<a id="codex-app-server-interfaces"></a>
### 4.3 资源模型与协议接缝

一个 **Octopus request attempt 独占一个 app-server 宿主**；它可以承载根原生 turn、后代以及父后续 turn，不能把每个原生 `turn/completed` 当作宿主生命周期终点。下一逻辑请求在旧责任确认退休后，创建新宿主并精确 resume 原 thread。首版不建设跨请求池。

原 Harness 持有进程组、连接、I/O、lease、临时配置及未退出引用；adapter 管协议和原生因果；SessionOwner 管输入与唯一 terminal/receipt。捕获 account/session/request/attempt/work、credential、原 backend handle，再关联 connection/thread/turn/item；Stop、晚 ACK、问题答案都使用原绑定。

本机核查为 **Codex CLI 0.165.0**，已无模型调用生成 schema。`TurnSteerParams` 必须包含 `threadId/input/expectedTurnId`，响应含 `turnId`；可选 `clientUserMessageId` 不是已证明的幂等执行键。官方接口提供握手、thread start/resume、turn start/steer/interrupt 与完成通知；`turn/completed` 仍须检查状态。依据：[官方协议](https://learn.chatgpt.com/docs/app-server)。schema 形状不证明尾部或账号配置已联调。

将 controller 检查点比较/更新、thread 恢复与业务输入合并到同一受管宿主，避免普通对话先开控制 writer 再开 exec。稳定 controller 与当轮上下文分离，变化更新一次，compaction 后仍可核验；controller 注入未知不能继续业务输入。跨请求配置安装不延长旧 Wiki grant 或复用旧 bearer。

<a id="codex-app-server-input"></a>
### 4.4 输入、问题与停止

人工追加只在原活动 turn、配置和授权兼容时 steer；准备、结束收束和没有明确活动目标时继续排队。自动返回始终沿§1.1，不借 provider 支持 steer 改变产品调度。切换 model/effort、credential、工具或 Wiki 范围按下一请求安装；撤权仍作用于当前真实操作。

| 发送事实 | 处理 |
| --- | --- |
| 尚未发送或明确未接受 | 仅在原请求与预算允许时重试准备/回队；不能据普通异常推断未接受 |
| 写出后 ACK 丢失、断连或超时 | 保留原身份与 unknown；不自动新建 turn、continue、换协议或退回 exec 重跑 |
| 已接受后失败/取消 | 保留已接受与 partial，准确结束并退休原资源 |
| provider 内部仍重试 | 观察原执行；Octopus 不同时发起第二个副作用尝试 |

原生问题保留 connection/server-request/thread/turn/item 与产品 question 的对应关系，由既有 Question owner 管答案和关闭，adapter 回原通道。MCP ask 保留原回调；审批、一般问题和动态工具请求分别分类，未知类型不自动允许。问题期间可 Stop，晚答不影响下一请求。

Stop 先关闭新发送与工具准入、撤原 lease，随后对原活动和受管子树发协议取消，并有界停止/join 原资源。根 idle 时仍能 Stop；根 interrupt ACK、EOF、直接 PID 退出都不等于全范围退休。未知退出保留 owner，不能对同一 thread 启动第二个 writer；独立 BG/Research/命名委派不随原生宿主自动取消。

<a id="codex-native-tail"></a>
### 4.5 完整原生尾部的完成判据

| 需要证明的事实 | 不能拿什么替代 | 实现责任 |
| --- | --- | --- |
| 受管范围可枚举并归属原请求 | 子卡片列表暂空、只订阅根、日志暂时无新增 | adapter 使用目标版本可靠的创建/亲缘/活动事实；覆盖冷恢复、快 child、孙任务、同 child 再执行 |
| 相关子结果确实进入父处理 | child completed、结果通知已发、UI 已显示 | 记录输入与父后续活动的因果；保持子正文与根最终回答分离 |
| 父能继续汇总并再派生 | 固定一次自动报告或“强制 wait”提示词 | 原生 wake 与确有必要的宿主触发只能选一个驱动者，不双触发 |
| 请求范围真正闭合 | 首次根 success、静默窗口、轮询两次为空、父说“完成” | 无未完成的必需后代、父后续活动、关键发送或阻塞问题；依据可证明的原生合同 |
| 原资源可退出 | 根 turn 结束、直接进程退出 | 原 Harness 确认受管进程组、I/O 和授权资源收束 |

先执行最可能推翻设计的场景：**旧 exec 历史冷恢复 → 父先结束 → child 延迟回报 → 父处理后再派验证 child → 再汇总**；在父 idle、子活动及父续跑阶段分别 Stop，核对身份、文件效果、结果与资源退出。

完成结果至少分清：原生执行范围闭合及其 outcome、Octopus 结果记录、通知交付、资源退休。已确定业务结果不能被通知失败倒写；退休尚未确认也不能提前释放资源责任。本次成功对外宣告须具有所选范围的成功依据、完整必要输出、输入关联和持久结算，资源门按原消费者要求闭合。

helper 失败必须可见并进入父处理；父明确处理该失败后仍可能成功，不能把“任何 child failed”简单传播为整个请求失败。仍必需但未处理的结果、未知发送或被提前截断的尾部不能伪报完整成功。

**协议证据不足时，迁移保持未完成。** 先做最小锁版实验，明确缺失的原生事实；不以日志轮询、延长超时、提示词补保证或宿主自动 exec/resume 报告冒充修复。不在本方案中默许自维护 Codex fork、通用持久任务树或无限未来任务保证；如确需改变上游接口，再把该具体依赖及维护成本带回设计裁决。

<a id="codex-app-server-compatibility"></a>
### 4.6 消费者、历史与观测

迁移验收覆盖普通聊天、命名委派子会话、App build/App ask、计划、Wiki worker、Research leaf、问题及浏览器。委派不能在主输出后提前截走未完整的回复；Wiki 的原执行退休与文件效果继续分开判定；App 的 done 保留正文通知前缀合同。

delta 负责实时展示，完整 item 正文按原身份只记录一次；保留工具名、参数、错误、commentary 与最终回答。父子文本、恢复历史与本次 usage 分开；缺失用量不填零，不重复叠加 total/last 或父子统计。Monitor 识别新进程形态，观测不得成为第二个完成裁决者。

旧 exec thread 要能同机精确 resume；fork/rewind 保留目录、归属、消息位置与失败清理合同，不把原生 turn ID 当 Octopus 消息位置。不支持或丢失的历史明确失败，不偷偷创建空历史继续。跨 backend 历史不混用；账户搬迁与完整备份按各自合同，不因新增 runtime 擅自扩大历史导入范围。

tool-free one-shot 保留独立文本 worker。Research leaf 的 read-only、未装配 Octopus MCP 与严格 web-only 是不同保证；实际可见工具和强行调用效果分别验证，不借迁移文案宣称现有配置已完全隔离。

<a id="codex-app-server-delivery"></a>
### 4.7 实施顺序与删除标准

| 单元 | 出口 |
| --- | --- |
| C0 锁版与反证 | 本机版本/schema/配置可重建；基础交互与§4.5关键尾部协议假设有证据，不靠旧0.164/0.166评审环境推断 |
| C1 协议与事件 | 单 reader、控制可推进、完整 item、乱序/背压/未知/问题/退出可测；业务消费者不解析 RPC |
| C2 请求完整生命周期 | 同宿主准备和执行、原生尾部、父再汇总、输入集合、Stop 与退休闭合；Claude 的共同责任同步核对 |
| C3 消费者与切换 | 旧历史、各调用方、Monitor、浏览器、关停/发布排空及本机门禁通过；完整尾部列为必须项 |

完成后删除普通对话旧 exec 主执行及被替代的重复 controller 准备、从 stdin/reusable 推导 steer、已由 RPC 覆盖的重复生命周期判断。保留仍有独立用途的文本 worker、认证/配置控制和被动诊断。开发期传输选择在发送前确定；未知发送后不自动 fallback。发布使用已验收候选与自然排空，软件回退不能撤销已发生的外部效果。

分别测准备、spawn、握手、resume/controller、首输出、整体收束、退休、内存与进程数。减少一个辅助控制宿主不代表消除每请求启动或 MCP 初始化成本；只有实际成本证明必要时才另议跨请求复用。

<a id="deepseek-design"></a>
## 5. 第四阶段：复用既有架构的 DeepSeek Harness 接入设计

### 5.1 锁定实际接口，选择一条主路线

2026-10-30 实查 npm 的 `@deepseek-ai/dsh` latest/next 为 **0.2.1-rc.5**，发布于10-29；本机旧缓存为rc.2，PATH 无 dsh。已下载并核对 rc.5 发行包的 README、公开类型及编译代码，未安装或运行 DSH。[固定发行元数据](https://registry.npmjs.org/@deepseek-ai/dsh/0.2.1-rc.5)

官方仓库当时 HEAD 为 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，但其文档与发行包并非全部相同，元数据也没有 gitHead，不能将二者写成同一构建。下表以 rc.5 发行包为主，升级需重验。

| 接口 | 实际语义 | 本次裁决 |
| --- | --- | --- |
| 公开 Agent API | `followup` 是 next-turn，`steer` 是 next-step，`inject` 不唤醒；idle 时 steer 会开新 turn | 使用真实 steer；不能谎称它与 Codex expectedTurnId 完全相同 |
| SDK wire | `session/prompt` 调用 followup，返回 messageId；有 resume/wait/shutdown，未暴露 steer 或逐 session cancel/close | 复用官方 server，以薄插件补公开原生控制 |
| ACP | 一个 session 一个 inflight prompt；cancel 与 close 责任不同，标准事件投影较少 | 保留选型依据，不作为正式并行主路径 |
| headless | 有限任务、无运行中输入口，普通字符串/帧裁剪，final 也可能出现在失败后 | 不为适应旧 exec 而放弃原生交互 |
| 高层 SDK run | 只收集到下一次根 idle | 不作 Octopus 完整请求的 completion；使用低层 wait 与事件证据 |

来源：[Agent rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-agent/0.2.1-rc.5)、[SDK server rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-jsonrpc-server/0.2.1-rc.5)、[SDK client rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-client/0.2.1-rc.5)、[ACP rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-acp/0.2.1-rc.5)、[headless rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-headless/0.2.1-rc.5)。

### 5.2 一个 DSH 宿主，一个薄插件

推荐 `dsh --profile sdk --patch …`：在同一 DSH 进程中，用 Octopus 插件替换 SDK stdio wrapper，保留官方 SDK 启动与资源层。原 wrapper 没有公开动态注册 RPC method 的服务，不能假设旁挂插件就能扩展已有 dispatcher。

插件从包根导入公开 `HarnessSdkJsonRpcServer` 和 `JsonRpcLineTransport`；普通 initialize/prompt/resume/wait/shutdown 转给 `server.handleRequest()`。只增加 Octopus 命名空间的 steer/cancel 与必要观测，调用公开 Agent API。**不读取 private sessions，不导入 src 私有模块，不重写 agent loop、工具执行、历史或子任务调度。** 这是由公开接口支持的设计推断，尚待离线 loader/假 provider 实验验证组合可行性。

| 所有者 | 责任 |
| --- | --- |
| Octopus SessionOwner | 原输入接纳、逐条派发、人类 steer 资格、问题与业务结算 |
| Octopus HarnessRun | 一个 attempt 的 DSH 进程组、管道、lease、临时秘密文件、停止升级和退出确认 |
| DSH SDK server／原生 runtime | 根创建/resume、Session 事件、失败记录、wait、后代 drain、AgentHandle.dispose |
| 同宿主薄插件 | 原活动身份校验、公开原生控制桥、必要增量/任务观测；不另拥有 completion 或执行账本 |

只有本 attempt 已接管根的精确 Agent 引用可控制；不能接受任意 id 后直接在全局 registry 找对象。新历史通过原生创建，恢复必须精确匹配原 session，不存在或不可读时明确失败。

### 5.3 原生 steer 的关联与证据

运行中的 DSH 在下一 step 边界消费 steer；被 pre-step 拒绝可停留 inbox，cancel/dispose 可能丢弃未处理输入。idle steer 会自动开 turn，因此插件必须在**无 await 的同一准入步骤**核验原 Agent、活动身份与阶段再提交；状态已变就明确拒绝，让 Owner 沿原输入规则排队，不偷偷开新轮。

命名空间接口携原 attempt、native root、预期活动标识和输入身份；活动标识从可信 Session/Agent 事件关联，不从当前 UI 卡片推测。ACK 至多表示输入被原生 inbox 接纳，不能伪造“模型已经处理”。`agent/inbox/claimed` 也不等于消息已经提交到模型步骤：配置路由或取消仍可能发生在提交前。接受、认领、持久 user message 与实际结算按所取得证据分别映射；不能因为根后来成功就把所有 steer 标 done。

BG/Research/命名委派仍在 Octopus 排队，不能同时预装到 DSH next-turn inbox 形成第二个产品派发者。只把已获准的首输入与人工追加送入本 attempt；DSH 原生后代向父回报是本请求内部活动。

### 5.4 完成、取消与后代

低层 `session/wait` 等根 idle、受管后代，再检查根仍 idle 且 session seq 未变，能覆盖子回报触发的父后续 turn。它不把 idle 且仅有 parked inbox 的后代视为推进中；因此要关闭未经选定的自主未来触发，并说明本次受管范围，不能宣称任意未来工作都完成。

wait 成功不是业务 success：从本 attempt 的持久 turn/end、原生错误、完整根输出及输入关联裁定 completed、blocked、aborted、error、max-tokens 等结果；子正文不能覆盖父最终答案，先正文后错误不伪成功。只取 `run().final_response` 不满足合同。

Stop 可先原生 `agent.cancel()`，最终仍按官方 server.shutdown → 受管 descendants drain → AgentHandle.dispose → launcher 清理，再由 HarnessRun 确认进程组与 I/O 退出。取消 Python await/to_thread 或 ACK 不代表线程、回调、后代停止；迟到 spawn/callback 保留原身份并收束。Octopus 尚未派发的 backlog 不受 DSH 原生清 inbox 影响。

现有子任务卡片按真实身份/事件映射；SDK 的 `subagent.finished` 只转发 local 子完成。若所选受管范围需要额外观测，插件订阅公开 `subagent/start/end`，保留 runId/provider/id/local，不猜测外部后代已经完成。用户直接控制每个原生 child、通用 transcript 浏览不纳入首轮接入。

### 5.5 问答、MCP、权限与配置

| 面向 | 接入要求 |
| --- | --- |
| MCP | 逐 entry 保留原 URL、独立 Authorization bearer、installation/task/work/attempt/lease 和实际 timeout；不合并成一个全局 token |
| 问答 | 复用既有 Question owner 与 MCP ask；若启用 DSH 原生问答，通过公开 user-questions/request 桥接同一 owner，先核对其根/后代发起范围；不建第二套等待状态 |
| 审批 | approval/request 与普通问题分开；沿明确 sandbox/approval 配置处理，未支持的动态批准请求明确拒绝，不把 ACP permission 当通用 ask |
| 指令 | 分开稳定 controller、当轮上下文与项目原生 AGENTS/skills；使用 literal prompt section，避免 `{{…}}` 被插值；resume 后权限与规则实际生效 |
| profile | 显式选择工具、原生子任务和必要事件组件；禁用重复的 autonomous scheduler、cron、后台队列等产品调度来源，不直接照搬 full profile |
| 工具限制 | tool-free one-shot、Research 受限 leaf、普通 agent 分别装配并强制验证；sdk-minimal 仍有 shell/image，不能当作 tool-free |
| 认证与目录 | API key、账户/credential 私有 home、项目 cwd 与 canonical memory 分清；不按 cwd/key 猜账户，不把 home 隔离宣传成同 OS 用户的恶意租户沙箱 |

**timeout 换算是已查明的接入陷阱。** Octopus 禁用 ask deadline 时使用 `2**31-1` 秒；DSH MCP 接收毫秒，经其 MCP client 交给 Node setTimeout。直接乘1000将超过 Node 上限，本机最小实验结果为被压成1ms。不能照搬 sentinel，也不能猜 `0` 表示无限。

接入门要求在 DSH 端兑现既有人工等待语义：有限时长使用合法毫秒；无产品 deadline 的 ask 必须有真正可取消的无截止等待接口，或通过既有 Question owner 的原生桥实现。如果选定 MCP client 没有这项能力，先完善该接口或改走同 owner 的问答桥，不能暗加60秒/任意有限期限，也不自动重发已经发出的 ask。ACP 标准 MCP 转换没有 timeout 映射，同样不能绕过此要求。依据：[当前装配](../../server/harness/assembly.py)、[MCP 插件 rc.5](https://registry.npmjs.org/@deepseek-ai/dsh-mcp-client/0.2.1-rc.5)。

### 5.6 正式产品闭合与退出条件

后端身份统一为 `deepseek`，保留 `codex`、`claude-code`，不另注册同义 dsh。扩展 BackendKind、凭据/认证、home 解析、运行时注册、模型/能力目录、生成前端契约、AgentForm/HarnessPage、Monitor 及所有真实调用方。模型、effort、tier 来自实际 provider 能力，不静默承诺同功能。

配置临时文件归 launch owner，存活到实际资源退出，不在纯 argv renderer 内创建后立刻删除。原地换 key、撤权、两账户/两 bearer、配置继承与当前/下一请求都要验证。不同 provider native ID 不互用；DSH 失败不自动切 Codex 继续未知执行。

持久 home、被选择保存的历史/附件分别进入现有备份和账户搬迁的对应范围；临时秘密、socket 和运行资源不导入为新授权。当前账户迁移的保留/排除策略由 [搬迁合同](../backup-and-migrate.md) 决定，不把“备份全历史”偷换成“换机必须导入所有旧会话”。服务关闭和发布排空必须看到准备、问题、RPC 与退休责任。

| 单元 | 必须取得的证据与删除项 |
| --- | --- |
| D0 同宿主适配验证 | 锁定rc.5发行包与依赖；薄插件加载、公开API、首输入/steer竞争、完整wait、人工等待、停止退出的假provider实验；任何能力缺口显式保留 |
| D1 adapter与账户 | 原共享接口驱动新建/resume/输出/错误/问答/Stop；账户、key、home、MCP、规则与工具策略真实生效 |
| D2 全消费者 | 普通会话、委派、BG/计划回流、App、Wiki、one-shot、Research、UI、Monitor与资料交接分别通过 |
| D3 正式切换 | 实机及浏览器证据达成选定合同；只保留一条DSH主路线，旧条件方案和二后端漏项退出，现有Codex与Claude共享/离线行为不回归 |

DSH 的任务循环仍由 DSH 拥有，Octopus 只拥有产品请求和资源责任。是否成功复用架构，以新后端接入时业务消费者不识别其 RPC、没有第二份输入/结果权威，以及旧旁路实际删除来判断。

<a id="verification"></a>
## 6. 验收、暂停项与文档交接

### 6.1 各阶段首先验证什么

| 阶段 | 最先执行的反例 | 完成标准 |
| --- | --- | --- |
| 1 正确性 | 已完成来源遭永久拒绝；已接纳但ACK丢失；默认变更后旧会话调整有效配置再继续 | 投递不无效重试、不重跑来源；配置行为符合§1.3；已有成功结果保全 |
| 1 Wiki效率 | 长命中章节的匹配位于snippet窗口外；同快照分页、外部修改和撤权 | 必要字段与坐标等价，有实际成本改善；不放宽阅读证据或授权 |
| 2 接口 | receipt命中但正文已消失；prepare期间重绑；旧请求退休时新人工请求活动 | 原owner裁定原事实，调用者不依赖私有布局，逐条派发政策未变 |
| 3 Codex | 父先结束、child晚回、父再派验证；根idle时Stop | 必需结果被处理后才完成，消费者拿到完整结论，资源可退役 |
| 4 DSH | steer与idle竞争；根idle后子回报；长ask与Stop；cancel后晚事件 | 公开原生接口语义正确，无双重调度或完成owner，能力差异如实体现 |

各运行时按其真实协议覆盖发送后失联、正文后错误、异常 EOF、满展示队列时 Stop、
取消后 spawn 迟到、旧答案遇新请求、账户/credential/MCP bearer、撤权、恢复与
排空。新 RPC 特有的通知早于 response、双向重号等由第三/四阶段验证；不把它们
倒置为第二阶段 exec 的前置实现。正常短对话不能替代适用的失败边界。

每个检查一个执行者。离线fixture证明映射；真实pipe/子进程替身证明本地竞争和资源；锁版runtime实验证明协议；浏览器证明产品入口。已有证据在源码、配置、依赖、环境有效时复用，修复或新反例出现后只重跑受影响检查。提交/发布门禁统一归 [testing.md](../testing.md)，不维护第二份测试数量表。

本机真实验证使用既有 Codex 登录，不申请新设备授权或访问 `auth.openai.com`；仅链接auth.json不是网络行为证明。Claude 保留共享与离线回归，不将未执行的实机测试记作通过。DSH 未有目标版本和真实凭据验证前，不标记产品可用。

### 6.2 原暂停票如何处理

旧22/23/24/28/31/42/43的编号不继续决定新阶段依赖。与本次完整请求、配置安装、真实发送、原输入结算、组合验收、能力说明和输入预算有关的责任，已进入第二/三阶段的目标合同；**不能用“旧票暂停”免除本次必须的尾部修复，也不能据此声称整个旧票包已恢复实施或验收。**

本轮是文档交付，当前代码及部署范围仍按 [架构合同](../implemented/architecture-contract.md) 判断。实施时更新对应owner的当前正文和暂停边界；已完成的独立返回/会话保留不重新实现。跨请求池、远程宿主、公开恶意多租户隔离、通用任务树、自维护provider fork及全项目双语镜像不自动纳入。

### 6.3 保留但不作为运行时前置的知识产品范围

原future中的以下需求没有因合并而消失，仍由 [Wiki未来计划](../plans/wiki-future-plan.md) 维护；本次四阶段不自动实施相邻应用：

| 方向 | 真正前提与保留约束 |
| --- | --- |
| Sapphire标签/目录浏览 | tags all/any及目录交集在排名/分页前处理，cursor绑定过滤，facet按文档计数；不是从零添加标签可检索 |
| 稳定引用与SmartReader | 统一vault/doc/revision/selector，移动、同路径替换、旧版本、重复句、Unicode与换行可解释；未安装reader仍可预览 |
| 受限App客户端 | owner与client授权明确，读/草稿/提交/取消/重试按任务归属；直接网关、REST/MCP、AppAgent和撤权一同覆盖 |
| PDF/网页材料 | 保留原件、抽取版本、页码/文本坐标和恢复失败；OCR另定范围，预览不等于Wiki已入库 |
| 来源变化与定向维护 | 有可靠来源关系和真实需求，保留diff/版本冲突/人工修改，不做全库自主重写 |
| 语义召回及巨型文档治理 | 真实问题集证明词法缺口或规模瓶颈；新增索引可重建，不替代身份、正文和授权 |
| 任务诊断与成本展示 | 复用原request/work/attempt/receipt区分排队、执行、记录、投递、退休，不另建任务真相 |

Octop/Okou等外部项目的有界缓冲、身份保留、源会话串行和完成/退休分离可以作为结构参考；其内存inbox、结果合批、取消后重新调用或backend接受后另行记账，不能直接移植为本项目保证。相关旧稿的产品政策已由§1.1裁决，不保留第二套待实施调度方案。

### 6.4 各文档的后续职责

| owner | 实施时更新什么 |
| --- | --- |
| [architecture-contract](../implemented/architecture-contract.md) | 输入/完成/退休、返回错误分类、实际恢复的范围 |
| [harness-layer](../implemented/harness-layer.md)、[codex-backend](../implemented/codex-backend.md)、[native-subagents](../implemented/native-subagents.md) | 公共接口、Codex迁移、原生范围与Claude替代路径 |
| [deepseek-harness-backend](../plans/deepseek-harness-backend.md) | 本文推荐路线的实现状态、固定发行版差异与实机证据；退出旧headless条件规格 |
| [wiki-development-summary](../implemented/wiki-development-summary.md)、[wiki-future-plan](../plans/wiki-future-plan.md) | Registry、查询/读取合同、实际效率数据及条件性产品范围 |
| [current-storage-format](../implemented/current-storage-format.md)、[backup-and-migrate](../backup-and-migrate.md) | 只有新增持久语义时更新格式/交接、保留与排除项，不因重构机械升版 |
| [testing](../testing.md)、[local-wsl](../local-wsl.md) | 验证选择、证据复用、真实环境与发布操作 |

新结论修改其原owner，不再每轮增加一份重叠路线图。本文保留本次取舍与版本证据，不能长期变成另一份“当前实现”。

## 7. 来源、历史裁决与本轮证明范围

### 7.1 四稿合并的取舍

| 原稿 | 本文保留 | 本文替换或撤回 |
| --- | --- | --- |
| `future.md` | 完成/输入/退休反例、轻量调用边界、Wiki/App条件计划、文档owner和验收原则 | 旧优先级、A可独立宣告本次完成、旧DSH选型前提 |
| `Octopus_Architecture_Review_and_Runtime_Roadmap_2026-10-29.md` | 当前代码入口、R2分类、Wiki数据搬运证据、接口收敛与外部版本边界 | N3一概禁止保存；BG不steer作为必须修复项；自动返回合批主线 |
| `Octopus_Reply_Queue_Scheduling_Guide_2026-10-29.md` | 接纳/消费/退休分离、原身份、有界资源与外部方案的失败窗口提醒 | collect-ready、批量execution、自动steer、正常队列新优先级等产品变化 |
| `Octopus_Review_Action_Guide_2026-10-28.md` | 已修项不重开、S1/S2、真实消费者、删除标准和验证矩阵 | 旧排期与重复说明；尾部仍只是可选的本次范围；旧安装包与当前发行混用 |

输入以本轮实际工作树为准，包含用户已有未提交修改。行动指南现存正文从第4节开始，回复调度指南从第7节开始；没有假装读到缺失前半部，也没有凭后文引用补造内容。四份旧文件从docs树退出；已提交历史由Git保留，未提交输入在本轮忽略的工作证据目录保全。

原稿SHA-256：

| 原稿简称 | 摘要 |
| --- | --- |
| future | `5eefcea79ceb268cce03821c1d07a4088b7f25a42d85c5f00da85536ea42a047` |
| Architecture | `d33451c9905089991de2d4162740da27a82c02a5f4bffe78ef7983591c1c1c43` |
| Reply Queue | `43b87b5fadb2fc03e76a0acf1a941f40a00be47ccc5e3f9b9fd3a4b40896cbec` |
| Action | `6ef1d7d8fd9c5a9d84978dbaa93743a6c83eaeeec9f81f68e3ca5b00c51678e9` |

### 7.2 外部版本证据与可重复核查

Codex：本机 `codex --version` 为0.165.0，`codex app-server generate-json-schema --out <临时目录>` 成功。已核对steer必填字段、响应、完成与thread通知；没有运行模型、认证、实际RPC交互或完整尾部测试。旧稿0.164.0/0.166.0-alpha.1仅是历史环境证据。

DSH：本轮下载rc.5发行包及所需类型/编译源码，未安装。以下npm SRI固定主要候选产物；实施须锁定完整解析依赖、profile、插件和runtime，不以三个摘要冒充完整环境锁：

| 包（均0.2.1-rc.5） | npm integrity |
| --- | --- |
| `@deepseek-ai/dsh` | `sha512-WkyJZv4zl9sCAxSzRctGm58m0rilGqwR6pniBiLOz5CPDfHfDuAUicg0AAazXI7oj/Cph6/cjNTncA/rpLTXpw==` |
| `@deepseek-ai/dsh-agent` | `sha512-mHNbxag0rZVNHaXZvr4OhiveaTTGMNPEpzO4nS8ohOQiQL0MWqA7brwtjBbaAlB3aMZ13tbHGHINpFlXdu8J3Q==` |
| `@deepseek-ai/dsh-sdk-jsonrpc-server` | `sha512-sNE9NqKXHpjVpZWr+ytlxrINqsURdKt4rY88Itgj6CBol1dEChr7j1B1LksK+yF6ffDcuYxkgw8KcwTVOSfjBQ==` |

rc.5核查的关键位置为Agent公开类型与README、SDK server prompt/wait/shutdown/dispatcher、SDK client run、ACP映射、MCP配置和timeout实现。Node v24.21.0最小实验观察到 `2147483647000ms` 被压成1ms；这只证明该定时器陷阱，不证明任何DSH问答桥已通过联调。工作证据暂存 `.scratch/deepseek-facts-20261030/` 和 `.scratch/review-consolidation-20261030/`；长期依据为上述固定发行产物、源码位置与本节结论。

本轮完成源码/现存测试阅读、外部主源核查、schema生成、文档入链/锚点/索引/差异检查。没有执行产品测试套件、真实模型、Wiki性能基准、生产资料读取或服务切换；历史绿色不充当新实现验收。最终交付为未暂存、未提交的文档改动。

### 7.3 全部历史架构候选的补充核查

本次按用户确认的评价标准，覆盖三份 HTML 的七项、三项及重复候选，并补核次级
输入恢复、行映射、Research 登记、浏览器收讫及无数据库执行路径。建议结合本文
Codex/DSH 路线与 `wiki-future-plan.md` 的受限客户端、引用阅读及资料维护扩展，
不把“不是运行时迁移前置”当作排除理由。

补充核查基线为 `0ca897e97189c5f213a658d986cbd67f4d69c5e3`。与原基线 `1126d106`
相比，仅新增侧栏计划和会话列表相关实现/测试变更；后端源码未变。复用仍有效的
后端阅读证据，另读 Wiki/App 入口与准入、配置路由、结算调用、Research 登记、
数据库投影及前端输入/收讫消费者，确认上述范围和现存实现。

这轮历史核查时，§3.6–§3.9 尚是技术推荐；其范围后来由 2026-10-31 访谈确认并
按 D3/D4/D6/D11 修订，当前以§3为准。历史阅读和文档检查不证明产品实现。

### 7.4 第二阶段设计补充的证据边界

2026-10-31 在 `28c067a090414bdd0513f570320584b2d42d1ee3` 上核对已有架构访谈、
《Octopus Stage 2 架构设计评审》与真实调用：BG 快照写入可被当作实时变化；
quiesce 提前设置的 offline_request 被 ready/managed_source 当成功收据；
idle/transient 的无输出原 prompt 重放缺少无效果证明；配置在准备锁内捕获。
Wiki 操作清单按路由和已有领域保护冻结，另核实 snapshot 创建已退役为 410。
R1 的纯函数反例由补充评审执行；源码阅读不等于真实 WS、重复效果或安装故障复现。
本次仅整合目标合同与分票，不改当前 implemented 状态、不运行产品/模型/发布；
原审计只覆盖所记录的冻结版本，新验收需在相应实现时取得。

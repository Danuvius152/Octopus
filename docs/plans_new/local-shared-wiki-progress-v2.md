# 共享 Wiki v2：当前进度与执行计划

> **Status:** in-progress — 所有 agent 常驻查询、旧会话恢复、大返回及短查询入口／移除输入框 Wiki 开关已验收并上线 `3b60b70`。other001五篇及最后四篇课程均经Vera终验和Octo签后当前对账正式接受，不重复提交。当前46篇MD／365文件，44篇已验收（41变化、3真实no_change），原迁移已收口，2篇Inbox原稿保留；通用日常流程、R08/R14及最终全量验收仍未完成。

**当前交接入口：** [2026-10-14 暂停与交接](local-shared-wiki-handoff-20261014.md)列出剩余路径和合同；其中“other001未提交”“缺显式/wiki授权”及旧运行版本属于历史窗口，以本页最新记录和私有当前证据为准。原暂停已被用户撤销，查询入口修订先上线后继续内容终验；生产流程草稿与两份测试原稿继续后置。

**后续开发登记（2026-10-15）：** 用户将 R08 的 v2 原始材料身份／跨 key 复用修复、R14 的同步成本与账户间等待测量加入后续交接，尚未实施或验收。详细合同与代码入口见[暂停交接 §5.1](local-shared-wiki-handoff-20261014.md)和[框架审查计划](../plans/octopus-framework-wiki-review-20261014.md)。原迁移九篇已按先前顺序验收；接下来汇总全部迁移经验，完善并上线通用流程，之后才使用两份保留 Inbox 原稿。开发验收与内容验收分别记账。

原核对窗口：2026-10-13 15:13～15:25，Asia/Shanghai；下面 §1～4 保留该窗口及其下一步基线，结构修复过程见 §5，实际提交、上线和真实试点续接见 §6。执行行为以[当前 spec](local-shared-wiki-spec-v2.md)为准，详细任务仍沿用 V2 编号；[核心交接](local-shared-wiki-implementation-v2.md)保留最初交付时的验证与停止位置，恢复任务以本页顶部与最新暂停交接为准。

**执行范围澄清：** A～C 直接使用 Octopus 原生 `/wiki`、核心 `/api/wiki` 与既有 Weber worker。Octo 组织，Weber 整理，Vera 验收；Sapphire/SmartReader 只保留后续 feature plan。主库在 `sapphire.data` 的物理位置和旧可选 app 绑定保留，不需要启动、开发或经由 App 提交任务。

## 1. 阅读入口和事实来源

| 要回答的问题 | 文档 |
| --- | --- |
| 现在做到哪，下一步做什么 | 本页 |
| 暂停点、未完成路径与可复制续接 prompt | [2026-10-14 交接](local-shared-wiki-handoff-20261014.md) |
| 执行必须满足哪些行为 | [当前执行规格](local-shared-wiki-spec-v2.md) |
| 任务编号、依赖、原始设计与代码位置 | [任务票](local-shared-wiki-tickets-v2.md) |
| 格式、受控写入、检索参数 | [内容](local-shared-wiki-content-v2.md)、[定点维护](local-shared-wiki-maintenance-v2.md)、[检索](local-shared-wiki-retrieval-v2.md) |
| 试点/批次/质量检查方法 | [迁移](local-shared-wiki-migration-v2.md)、[评估](local-shared-wiki-evaluation-v2.md) |
| 后续 R08 修复与 R14 测量合同、框架最终发布证据 | [框架与 Wiki 审查计划](../plans/octopus-framework-wiki-review-20261014.md)、[暂停交接 §5.1](local-shared-wiki-handoff-20261014.md) |
| 当时为什么这样决定 | [访谈](local-shared-wiki-grill-v2.md)、[原位文章 ADR](../adr/0001-in-place-wiki-articles.md)、[表达保全 ADR](../adr/0002-targeted-maintenance-preserves-expression.md)、[术语](../../GLOSSARY.md) |

原核对窗口以 Git、当时源码、只读生产 SQLite/文件摘要、服务状态与已落盘的 Octo/Vera 产物交叉核对；该窗口没有提交生产任务、调用实际 agent、修改 persona 或生产文章。生产账本只读打开，没有实例化会迁移/索引的生产 Store。后续实际部署和维护见 §6；私有正文、个人事实、凭据和完整会话不复制进仓库。

实际执行证据保存在当前账户 workspace 的 `wiki-v2-rollout-20261013/`，根路径为 `/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-v2-rollout-20261013`：

- `progress.md`、`run.json`、`manifest.jsonl`：执行记录和批次范围；run/manifest 滞后于第二个已完成 job，不能单独当最新事实。
- `persona-applied.json`、`versions.json`：实际配置合并和初始版本。
- `batches/58a62e175303484dbeb4f6f9cd2b661c/`：首批当前读取、检索、回答、diff/回执审计；current-asset-read-branch.json 仅证明资产显式短读。
- `evidence/vera-final-independent/result.json`：首两篇 A 通过，完整试点与全量没有放行。
- `evidence/vera-fallback-followup/`：修复复核、166 项 targeted、旧状态升级及冻结 fingerprint。
- `evidence/integrated-core-fixes-v2/`：新版完整本机检查结果和两个 Codex 失败。

## 2. 原核对窗口的进度

### 2.1 代码、部署与配置

| 项 | 已核对事实 | 限制 |
| --- | --- | --- |
| Git | HEAD `e7259b4`；Wiki 核心 `ca45ffc`，工具/交接 `73ea9cd`，Library 全量范围 `4534c4b` 已提交 | HEAD 后两次提交是模型速度/应用退出修复，没有新增已提交 Wiki 核心 |
| 新 Wiki 修复 | 9 个 Wiki 模块和 2 个新增测试未提交；assets Markdown 资格、直接表示选择回退及 sections-v5 已在工作树 | 尚未提交，整版运行验收尚未完成，不由源码存在推断整版已采用 |
| 当前服务 | user service active，13:28:41 启动；health 200，OpenAPI 有 read-many/revise | 生产仍 sections-v4，无 readable_body；新 fallback/v5 未迁移，assets 单项运行资格尚需核对 |
| 核心配置 | account/vault/root/既有 Weber 与交接一致；旧可选 app 绑定仍保留 | 执行核对核心配置；App 绑定和运行不是前提，不能照旧记录重置配置 |
| 真实 agents | Octo `98905a679ddf`、Weber `f37612d667ec`、Vera `f97a9e992aee`，均未归档 | 隔离测试的 stand-in 不算实际执行者 |
| Weber persona | 知识段已实际合并，SHA 与应用记录一致 | 当前 reasoning/tier 与首 job 已不同，保留当前用户配置，不恢复旧 ultra/fast |
| 索引状态 | durable schema 7；336 indexed documents，当前 sections-v4；epoch 340，15:25:23 对账无 error | 336 是整个索引集合，不是本轮 Library Markdown 的分母；sections-v5 不是 durable schema8 |

当前 11 份修复源码/测试 fingerprint 与 Vera 复核和新版检查冻结记录一致；指南 fingerprint 也一致。这个对应关系只证明检查了同一版，不消除已经失败的检查。

资产资格相关模块修改时间早于当前服务启动，而 search_index/schema 修改晚于启动；仅凭 sections-v4 不能断言 assets 修复未加载。13:39 的 current-asset-read-branch.json 证明两份资产 MD 各显式 live 读取 1 字符成功，不证明完整阅读、maintain 入选或受控写入。源码已改、模块可能已加载、整版验收通过和最终提交/部署是不同证据层次，实际资产维护行为留待合法入口核对。

### 2.2 全量范围与真实整理

| Library 目录 | Markdown 数 |
| --- | ---: |
| pika_vault | 31 |
| course | 4 |
| Research | 5 |
| Me | 1 |
| Guides | 1 |
| 合计 | 42 |

15:15 枚举时 42/42 在 documents 中，当前文件摘要与索引 revision 一致，无缺失或漂移。kind 为 original 37/source 3/asset 2，doc_role 为 unknown 38/reference 3/template 1。**这是当前索引覆盖，不是 42 篇已经整理或逐篇检索通过。**

两个已有资产 Markdown 同属本次全量范围：

- `Library/pika_vault/思考随笔/assets/人类与AI共同写作动画/项目说明.md`
- `Library/pika_vault/思考随笔/assets/写作是一种认知编码/项目说明.md`

当前没有 Library/tmp/temp/static/node_modules/Codex Skills 内 Markdown。资产角色默认候选排除与显式维护/读取资格分开；用正确参数及有效 assignment 核对两个资产文档的实际运行资格，最终冻结版上线后验证其行为，不将旧派生索引版本当资产加载结论。

| 实际批次 | Weber 结果 | 验收状态 |
| --- | --- | --- |
| pilot-001，job `58a62e175303484dbeb4f6f9cd2b661c`，worker `1681d01894ad` | 《对笔记写作的理解》《AI agent skill的一些开发经验》两项 normalize | 真实 diff/回执、当前读取、10 条检索与 4 个回答有记录；现有 Vera 首批通过，manifest accepted 2 |
| pilot-002，job `8d471ee565394659af6c98548831362c`，worker `e062639d4950` | Me no_change；《AI越擅长，我们就必须要撤退？》normalize | 两项 done/receipt 存在；manifest 仍 pending/job_id 空，未找到保存后检索与 Vera 验收产物 |

账本为 2 个 completed jobs、4 个完成项和 4 个回执，包含 3 normalize、1 no_change。清单仍为 accepted 2/pending 40，应先对账，不重跑已成功正文。

**15:25 的新增版本事实：**Me 当前 revision 已不同于 pilot-002 no_change 回执版本，不能把该旧版本回执计为当前验收。未确认编辑来源，不猜作者；Octo 需重读并保留新内容，再决定复核/维护。

六份代表材料中两个 Research 报告尚无维护 job；Q58 的真实知识建立→不同问法复用→明确纠正没有完成证据。首两篇通过及允许继续其余样本，不等于 V2-06 或 V2-08 放行。

### 2.3 V2 任务状态

| 任务 | 当前状态 | 尚需完成 |
| --- | --- | --- |
| V2-01～03 | 核心已提交、上线；真实首批链路可用 | 当前修复版本的两项失败、资产资格/表示选择/v5 升级收口 |
| V2-04 | 既有 persona 合并、query/retrieval/entry/worker 指南及按需 article/writing 已接通 | 通用写作自动装配和拟议 subtype 模块不能冒称已交付，不阻塞核心 |
| V2-06 | 进行中，首两篇通过 | 第二批当前验收、两个 Research、完整阅读/定点维护、Q58、Vera 完整放行 |
| V2-08 | 全量清单已建，批量未放行 | V2-06 通过后 pika 优先，最终当前 Library 全量验收 |
| V2-05/09 | 后续 feature plans | 用户启动后才实施 App UI/tags/client_apps/reader 等 |
| V2-07 | 条件项 | 真实稳定词法缺口才启动，不由资料数量自动触发 |

## 3. 当前版本验证与明确失败

原核心交付的 1591 backend、334 frontend、71 UI、18 Codex 结果保留为历史，不能替代当前修复版本。

| 当前版证据 | 结果 | 范围 |
| --- | --- | --- |
| 新增资产/表示选择两个文件 | 166 passed，独立复验通过 | 隔离测试，包含 stand-in worker、身份/回执/恢复与直接表示选择 |
| Vera 旧状态升级 | 3 个实际旧 schema7/sections-v4 状态场景通过 | 隔离升级，不是生产已迁移 |
| integrated fast-gates | exit0，1780 hermetic passed/43 real deselected，344 frontend，其余八关通过 | 当前冻结源码 |
| pure UI | 72 passed，exit0 | 当前冻结源码 |
| Codex browser | 1 passed，exit0 | Octopus 原生聊天与隔离服务；不是 Sapphire/SmartReader 验收 |
| Codex real | **16 passed / 2 failed / 0 skipped，exit1** | 当前冻结源码，完整本机门槛尚未通过 |
| Claude live | 本轮未执行 | 旧 provider 402 记录单列，不计通过、不掩盖 Codex 失败 |

失败及下一步：

1. `test_codex_personal_facts_use_current_me_directory_without_guessing[我的职业是什么]`：QueryProbe 捕获 error event；现有 JUnit 截断事件，缺具体 provider 错误。先保留真实错误事件及 session 证据再定位，不能猜是登录、402、网络或测试适配。
2. `test_codex_whole_report_evaluation_reads_all_current_pages`：记录的 read spans=[]，覆盖 0/35263，完整读取断言失败。检查真实事件、Wiki task/tool 装配、执行与 probe 统计；保留原完整覆盖要求，不改断言把失败变通过。

另有本次隔离探针发现的结构资格边界：内联 `<span><script>…</script></span>` 与块级 script、HTML 单图与 Markdown 单图的 readable_body 判断不同，会改变原稿优先/摘要回退。当前 166 测试不覆盖这两类差异。收口时明确支持结构并补所需回归/修正，不将结构标志冒充完整 HTML 可见性或内容核验。

## 4. 之后的执行计划

### A. 收口当前修复版本

**负责人：**Octo 组织定位/修正，Vera 独立复核；这个阶段尚未完成。

1. 固定当前代码/指南 fingerprint 和检查输出，先诊断两个 Codex 失败，保存足够错误/调用事件并沿原断言复验。
2. 核对资产资格、直接表示选择和可读结构边界：修正合同范围内问题、补对应回归；有新改动时更新冻结版和受影响审查。
3. 在最终代码上完成 AGENTS 的本机必需检查：scripts/check.sh、pure UI、可用 Codex CLI/browser。Claude unavailable 单列；任何必需项失败仍需解决，旧版结果不算当前通过。
4. Vera 结论与最终冻结版对应后，聚焦提交/推送当前 Wiki 修复，保留其他工作区内容；记录 commit 和部署版本。
5. 用一致恢复点和既有服务流程部署，核对资产显式读写/检索、生产最终派生版本/readable_body、身份/历史/回执保全及实际 capabilities。durable schema7 与派生版本分开记录。

**退出条件：**当前必需项零失败，修复已提交且实际部署，生产升级和关键回归有证据。源码审查或 isolated 升级成功不能提前退出。

### B. 对账已执行材料，补齐 V2-06

**负责人：**Octo 统一调度，既有 Weber 受控维护，现有 Vera 验收。

1. 将 pilot-002 两项与 manifest 对账，补当前读取、检索、diff/回执和 Vera 结论。Me 已变化，按最新内容重读，旧回执保持历史，不覆盖用户新内容。
2. 在同一可信入口明确指定剩余两个 Research 代表目标；完整阅读后按实际类型复核/必要整理，符合要求的文件真实 no_change，不为计数重写长报告。
3. 补已有摘要复用、人工编辑后的定点纠错/补充、多轮表达保全及隔离冲突/恢复合同；已通过且未变化的首批沿用实际证据。
4. 选真实有用途的问题，明确目标后由 Weber 建立或补充知识页，以不同问法实际复用，再进行有依据的明确纠正；记录相关原文版本及回答支持。
5. Vera 逐项核对 spec AC-01～12 在试点中的适用证据，给出通过/需修正/阻塞，Octo 记录 V2-06 放行。

**退出条件：**六份代表材料当前证据、Q58 和其余必需合同通过；不是只有 job done 或第一批几个回答。

### C. V2-08 覆盖当前 Library 全量

先 pika_vault，再 Me/Research/course/Guides、导航、模板和其余目录，包含两个已有 assets Markdown。每批通常 5～10 篇，按长度/上下文负载调整，明确 paths、当前版本、动作和幂等键。

**提交路线：**已执行试点使用真实 Octo turn 首行 `/wiki maintain` 的六个 shlex 路径选择，经 MCP 与核心 `/api/wiki/maintain` 创建 job，job actor 为 Octo、实际 changes actor 为 Weber。后续沿这条独立链路，首行明确每批文件；正文或 scope 的“全 Library”不替代 selection。也可在用户既有授权下使用已认证账户的核心 `POST /api/wiki/maintain` 或本机 operator/helper，将 manifest 转为明确 paths 并记录入口、账户、actor；复用现有 WikiJobs 和锁/恢复合同，不假定未实现的自动目录 scope。

Octo 逐篇检查当前身份/revision、整理或真实 no_change、回执、索引、精确定位/当前内容读取及适用的内容检索。资产使用 include_assets 和直接读取，模板按角色查询。Vera 分类语义抽查；坏规则暂停受影响项并扩大复核，修正后只续未完成或版本变化项。

**退出条件：**末次实际文件枚举与清单一致；每篇有当前整理/复核和检索证据；未处理/冲突/失败/不可检索项为零。其间新建知识页及新增/移动/修改也须对账，42 是本次快照而非永久分母。语义抽查的覆盖限制如实报告。

### D. 之后才启动的条件项

V2-05 Sapphire、V2-09 SmartReader 在用户启动应用开发时实施，包括 App 入口、客户端权限和界面验收；A～C 不分派 App 开发或接入任务。V2-07 以真实可复现的词法漏召回为前提。语言对照、规模扩容、完整来源谱系、旧快照退役/GC 保留各自条件，不作为 A～C 的隐含工作，也不因接口标记 deprecated 就认定旧数据已清理。

## 5. 2026-10-13 晚间续接 A 阶段

开发 HEAD 为 `b54786c`，实际服务工作树为 `b083efe`，20:13 重启；服务源码与开发目录隔离。生产仍为 schema7/sections-v4、没有 readable_body，不能以开发修改或本轮复验推断已升级。晚间只读对账 Library 为 43 份 Markdown，43/43 当前 hash 与索引相符；新增材料纳入最终范围。原四个真实回执保留，既有 Weber 知识 persona SHA 与已应用记录相同，当前 max/default 等用户配置保持。

原两项 Codex 失败沿原测试与原断言复验 **2 passed / 0 failed / 0 skipped**，253.41 秒，源码 fingerprint 前后一致。Me 成功读取 31 个合成文件且仅个人范围；报告单 revision 连续覆盖 0→16000→32000→35263，并保留末尾限制。长报告旧失败的原生记录为 server_overloaded；职业旧错误的 stdout 缺失，原因仍不推断。当前职业复验也经历两次容量错误后续跑成功，以及一次 read-many 超过20项限制后拆批成功；专项通过不等于运行从未出错。

首次复验启动因中文参数 node ID 未按 pytest 转义而退出4，实际未运行测试；原输出保留，修正启动器后先 collect-only 确认精确两项。私有 observer 装配时序未捕获框架 events，已用精确匹配的合成原生 rollout 与 probe 成功返回补证；其中个人工具输出有截断，不能声称完整原生 events 或全部模型可见字节已留存。去重用量 273,883 tokens，含失败及续跑，未推算金额。完整当前门禁仍须在最终结构修复源码上重新完成。

结构资格探针复现内联 script/style/noscript 错计正文、HTML 单图漏计，以及物理 HTML 先丢标题/媒体结构的差异。当前工作树统一支持的结构规则，保留原文和提取坐标；HTML 派生标记缺失时重新提取。现有 Vera 早期只读审查 `8ce5e397180a` 进行中，最终冻结版审查、本机门禁、提交/推送与实际部署仍待完成；A、完整试点与全量门禁未放行。本轮尚未新增生产整理任务。

私有证据为 `wiki-v2-rollout-20261013/evidence/stage-a-takeover-20261013/`：takeover-baseline、original-failures、launch-error-exit4、current-query-recheck、current-query-recheck-native 与 Vera 早期基线。源码/指南 fingerprint、实际提交和检查结果继续在此记录；生产正文及会话不写入仓库。

### 晚间审查后的修正候选（v6 检查版本）

用户后续明确本轮只入库/维护 MD，HTML 转换入库留后续；MD 内嵌 HTML 和媒体仍保全。Vera 早期审查提出 F1～F4，原候选专项440、八关与纯UI通过记录保留，但发现新问题后停止尚在运行的隔离实机检查，避免混用源码；没有取消生产任务。

新候选从完整 Markdown token 流保持隐藏包围与标题角色，再投影到原始分区/窗口；相关/整理区依标题层级继承。最终派生版本改为 sections-v6、HTML 结构标记 html-body-v2，durable 仍7；独立实建的旧 v5 状态重开和 refresh 均正确回退摘要。QueryProbe 保留原断言并加强同请求唯一成功终态校验；13 项隔离回放通过，失败/中断/缺失/重复/跨请求终态不能用部分答案冒充成功。原六份试点、Q58 和全量状态未变。

复用 Vera 同一结构矩阵，当前 93 次选择偏差为0；三个缓存/恢复相关短回归通过。最终专项、整版本机门禁、Vera 同委托续审、提交与部署仍需以新冻结版完成。证据新增 `review-fix-rechecks/`，完整当前检查结果随后记录，不提前放行。

### v6 完整检查与嵌套标题回归

v6 冻结版完成专项594、八关（2398 hermetic、379 frontend）、76纯UI、19 Codex实机和1 browser，均通过、无跳过，源码前后稳定。六个真实Wiki查询均保存同请求唯一成功框架终态；完整报告覆盖35263字符，成功续跑中的recovery如实保留。

独立新样本又复现引用/列表内部规范标题改变页面角色：引用整理说明使外部正文不可检索，引用正文使外部整理记录被错计。此发现影响Markdown，必须修正，旧门禁只证明v6。当前v7/html-body-v3限定引用与列表标题不能改变外部角色，保留引用、代码和媒体；短隔离124结构样本及20MD原分区样本无偏差。新的冻结检查、Vera同委托复核及实际部署仍待完成，生产仍v4，未新增生产整理。

### v7 冻结版门禁与独立审查完成

23:00（Asia/Shanghai）完成的最终冻结版为 sections-v7/html-body-v3，durable schema 仍7。专项 **630 passed**，八关检查全部通过（2434 hermetic、379 frontend），纯 UI **76 passed**，可用 Codex CLI **19 passed / 0 failed / 0 skipped**，Codex browser **1 passed**；源码检查前后无漂移。Claude live 未运行，provider 不可用的历史证据单列，不算通过。证据为私有 `stage-a-takeover-20261013/final-gates-20261013T143257Z/`。

既有 Vera 同委托已通过这版 22 文件独立源码审查，F1～F5 关闭：589 项短回归、原 F5 的60行矩阵及24例实际 WikiStore 复验均通过；100个角色边界、20项渲染/坐标、8个实际旧v5/v6升级和4个既有HTML兼容升级通过。六张非空持久表逐行保全，原文、身份、revision及恢复结果保持。源码审查结果和冻结 SHA 在 `vera-nested-fix/`，不把旧v6门禁转算为新版通过。

当前清单再枚举仍43份MD（pika31、Research6、course4、Me1、Guides1），43项当前hash与索引匹配；仅3个回执匹配当前版本、2个已有通过复核可复用，Me旧回执仍是历史。课程一个旧路径消失，另一个当前路径及不同身份/内容出现；不推断编辑者或纯重命名，后续按当前文件全文复核。全量清单是动态范围，索引覆盖不等于整理或逐篇检索验收。

下一步只提交本轮修复文件，保护用户另外两份未提交文档，再建立当前配置、Wiki durable state与Vault一致恢复点并部署已验证提交。准备脚本和独立审查只写私有执行目录；尚未创建生产维护任务或升级生产索引。部署后仍须核对实际版本、capabilities、资产资格、compact/read-many与身份/回执保全，之后才继续剩余试点和全量，A、V2-06、V2-08仍未放行。

## 6. 2026-10-14 凌晨的实际上线与真实试点

本节记录结构修复之后的执行事实。§2～5 的未提交、旧 sections-v4、未部署及待续接条目属于各自历史窗口，不再代表当前运行状态。

### 上线、配置和恢复

修复提交 `74eab95fa1f418d726c27c562fddacaf5c2c9a1f` 已推送 origin/main，并部署至独立 release `main-74eab95fa1f4`。user service 的实际 MainPID 为 197731，后端运行 durable schema7 / sections-v7 / html-body-v3。前端源码与旧 live release 无差异，69 个既有构建产物摘要一致，复用已有构建。完整本机门禁和独立源码审查沿用 §5 最终 v7 结果；没有因内容维护重复运行门禁或部署。

实际上线后的原生 info、核心配置、capabilities、compact、read-many、当前身份和 revision、领域与显式资产检索已核对，Vera 对采集窗口的 A 验收通过。全文覆盖与配置保证均限定对应窗口；后来维护或用户设置变化使用新版本证据，不继续宣称状态始终不变。既有 Weber 知识段保留，后来认证、owner-scoped 配置更新的当前设置沿用；未恢复旧运行参数，具体操作者没有记录的部分不作归因。

恢复点 `v2-current-20261013T151309Z` 已建立并在隔离副本验证：当时 43 份 Markdown 完整读取、8 个历史 blob、身份及持久账表保全，实际受控历史恢复通过。失败的旧恢复点保留 INCOMPLETE，不作为恢复引用。后续维护和新增知识页有实际 history/receipt；全量结束仍需当前一致恢复点及验证。

### 当前材料、Q58 和独立验收

Octo 经原生 `/wiki maintain` 的明确路径选择和核心 durable assignment 提交，实际写入由既有 Weber worker 执行。首两篇未变，复用已通过结果；另一个论述文章、最新个人记录和两份长报告均已通过当前版本内容及执行合同验收。报告复用已有摘要，只补类型与必要元数据；个人记录保留后来新增事实，不猜缺失字段。两份已有资产 Markdown 完整读取后真实 `no_change`，Vera 核对用途与回执通过；不强迫资产说明套用文章模板，也未操作媒体或项目源码。

Q58 已围绕真实评测问题建立一个 Concept：依据一份库内报告的十个位置，区分吞吐、首帧、在线控制、块承诺、缓冲和证据范围。它没有新增实测，也没有把十个位置算作十份独立证据。不同问法实际复用后，Weber 对表格中的计时起点作一次受控澄清，明确服务端接收后的阶段区间与用户客户端提交起计的端到端区间；只替换一个单元格中的字句，其余正文、元数据、来源及原报告均保持。

最新知识页由原生 read-many 同 revision 连续读取 `[0,5831)`；路径、ID、题名三个精确定位及两个内容查询命中当前版本，两种问法的答案有当前行和字符引用。已保存真实 worker 全文读取、raw locator/hash、patch 参数、changed ranges、前后 revision、diff、receipt/history/blob 及索引。Vera 沿同一委托已通过新修订、Q58 闭环及完整九篇 V2-06 试点，AC-01～12 在适用阶段通过，无阻塞项；Octo 已记录 V2-08 放行。该结论不认证其余 35 篇或全量完成。

| 当前 Library 快照事实 | 数量 | 完成口径 |
| --- | ---: | --- |
| Markdown 文件 | 44 | 含新知识页；结束前仍重新枚举新增、移动、删除及修改 |
| 当前身份、revision 和索引匹配 | 44 | 只证明已保存可定位，不能算全部已整理或检索验收 |
| 当前维护或复核回执 | 17 | 15 份有实际变化，2 份真实 no_change；知识页首次发布和修订只计一个文件 |
| 已有当前独立内容验收 | 17 | 九篇试点／Q58及首批八篇当前版本通过；不延伸到其余文件 |
| 维护完成、待批次验收 | 0 | 首批八篇已由Octo依据独立结论正式验收；新批次尚未提交 |
| 尚未维护或复核 | 27 | 不计完成，继续 pika_vault 优先及其余 Library |

隔离故障合同先定位当前门禁及源码对应的明确断言，不以旧检查进程取消或私有观察器误报替代。新增两份副本的组合验证通过：第一项实际提交后取消 running 任务，旧上下文新写被拒；合法 retry 只为第二项建立新 assignment 并维护，第一项字节、历史和回执不增不改，最终两项完成并入索引。该验证模拟模型、没有生产写入；取消发生在首项写方法结束之后，不覆盖原子写途中取消。现有 prepared/applied、重启、版本冲突、恢复保护后来编辑和索引只补不重写的证据分别保留适用范围。

观察器曾按原始路径查询规范化索引路径而误报三项失败。旧产物和代码保留；改用 doc ID 和核心只读判据后复核既有 completed jobs 通过，没有重建生产索引、重排任务或重新生成正文。实际运行有已恢复的只读 Bash 错误，不宣称所有工具零错误。用量按实际原生主线程、可确认辅助线程及耗时分别记录；缓存属于输入、reasoning 属于输出，不重复相加，无账单依据不推算 tier 或金额。

私有证据仍位于 `stage-a-takeover-20261013/`，新增入口包括 `actual-native-a/`、`current-recovery-verification/`、`current-library-maintenance-ledger.json`、`current-real-pilot/`、`assets-knowledge-real-pilot-rechecked/`、`q58-correction-real/`、`partial-cancel-isolation/` 和各轮 `vera-*-review/`。原 `run.json`/`manifest.jsonl` 的早期状态不单独代表当前完成清单。

私有 `vera-complete-pilot-review/` 和 `V2-08-release.json` 记录独立结论及 Octo 放行。首批 8 份短 Markdown 经可信首行明确选择，按固定幂等键提交实际 durable job `4bdb35129237407da81ba6fd8983708d`，已由既有 Weber 的三个框架窗口完成。八项全部 done，九次受控操作均有本人写前同 revision 全文返回；教育篇先 restructure 插入规范标题，再完整读取中间版本并 normalize，实际复用原摘要。回执链、当前身份及索引机械检查通过；观察器退出4直接原因是教育篇需人工复核，未发现原字句删除或替换。范围外提示来自 Markdown 前清单与含媒体后清单口径不同：同口径44篇仅本批8篇变化，其余36篇一致；没有非 Markdown 前值，不能认证媒体字节不变或推断越界写入。旧观察器和产物保留，私有 helper 已改为显式同口径比较并在启动前校验范围，临时 fixture 通过，未重跑生产任务。

`batches/pika-001/` 保存实际三窗口、输入／输出、差异、全文读取、九步操作、回执、历史及索引证据。三次原生运行均记录 gpt-6.1-sol/ultra 且唯一成功终态；Wiki工具失败零，末窗口有一次已恢复的只读 Bash 路径错误。主窗口合计输入3,454,885（含缓存3,180,672）、输出43,749（含推理14,010）tokens；六个既有 helper 的1,909,391 tokens单列，不猜账单或tier。

首批八篇通过原生 read-many 完整回读共14,750字符，各自 revision 匹配且 next_start 为空；路径及ID共16次精确定位、8次内容关键词检索通过，index epoch698/ready。八个答案样例的字符／行引用已对账，confidence 限定当前文档的主张与条件读解，不认证外部事实；本次迁移完整回读不是独立冷启动问答效率试验。递归枚举Library仍44篇，仅本批8篇变化、其余36篇一致。Vera 已独立核对八篇全文、类型、摘要条件／主体、原文及九步维护合同，全部通过，无修正或阻塞项。教育篇的两处整理说明表示处理事件，原摘要和原叙述者自述保留，不构成作者错配，不要求重生成。Octo 对账落盘结论及验收后当前 revision 未变，正式验收本批；记录在 `batches/pika-001/vera-review/`、`acceptance.json` 和 `post-verdict-revision-check.json`，全量仍未完成。

第002批选定八篇思考随笔，实际原生全文返回27,294字符、身份／revision与草案一致；原始字节、完整363文件Library前清单及内容保全风险已留私有证据。其17篇已通过材料也未发生版本变化。经新的可信原生首行明确8paths，再核当前原稿hash和幂等键无既有任务，实际MCP maintain提交job `3dc6a6d41ca04530baa2989dd282e918`，既有Weber首个窗口 `e60fbdb94456` 已运行。任务仅原路径Markdown/material空，不生成新主文或自动Concept。已有准确总结按实际用途复用，数学公式、表格／反例、锚点、主体限定和原稿布局保留；文章中的构想和第一人称不写成用户个人事实，也不执行归档中的工具／写作指令。

提交者的预读不代Weber本人写前全文读取。`batches/pika-002/` 保存真实提交、三个worker窗口和输入输出版本；新观察器配置明确all_files与完整363文件前清单，避免与MD-only前值混比。job已completed、8项done；12次受控操作均有Weber写前同revision完整读取，回执／history／当前索引一致，363文件同口径对账仅本8篇变化。三窗口唯一成功终态，Wiki工具错误0，另有4次非Wiki工具错误记录。原优化篇CRLF、公式和表格字节保留，文本展示的换行归一不当作真实改写。

观察器exit4表示结构操作需复核：语音篇复用原callout并补边界；AI理解世界篇将原结尾两个采纳条件块实际移到Key note，其余正文及最后收束保留。原字句未丢失，但顺序发生变化，仍须独立判断论证承接与本次scope，不能称原位沿用或把告警当误报关闭。本批当前原生8篇全文共32,878字符已连续读取至末尾，16次精确路径／身份和8次内容检索命中各当前版本，索引epoch710就绪；8个回答样本附23处实际范围引用与confidence，并保留外部事实、任意问法和冷启动效率未认证的限制。Vera结构／内容验收仍待完成，后续批次暂不提交。快照共有25篇当前维护回执（23变化、2真实no_change），其中17篇独立通过、8篇完成待验收、19篇未处理；27篇仍未达到本轮全部完成条件。

Vera窄范围审查已完成：语音篇结构复用与边界补充通过；AI理解篇有P2局部合同问题，原正文末尾的“误差检查→两项采纳条件→换句话说”被缩减，两个条件只留在摘要。Octo对实际diff及133字符原块确认后，接受这一发现，仅由Weber在该篇的新合法assignment中恢复两个结尾块，并将唯一Key note保留为独立简短摘要；H1、元数据、公式、链接和未涉及字句保持。经真实原生首行单文件选择、当前全文和幂等任务对账，MCP已提交纠正job `f76607646f074faaae927045294b0310`，首个实际Weber会话 `8441085ac1d8`，写入结果待核。原8篇job不重排；Vera同时续验未受影响7篇，整批仍须新修订的当前检索及最终验收。

纠正job首轮以completed_with_attention结束：实际revise试图删除两个完整raw块，被整块纠错保护以422拒绝，无operation receipt，账本仍为d18d92版本；worker终态failed，附带stderr不足以判定provider退出原因。Octo已接受这条操作路径不合适，准备沿原job和原幂等键恢复唯一未完成项：revise仅插入简短摘要条件，再用restructure把全部原块各保留一次并搬回正文，不规避保护、不修改框架代码。两步私有fixture验证正文原字句及顺序、元数据／公式／链接保全，实际retry尚未执行。服务2026-10-14 10:36CST已重启，实际release仍main-74eab95fa1f4、MainPID330；重启后的原生配置／API继续核对，旧上线窗口PID保留作历史。

原Vera七篇续审在21:55UTC异常结束且无最终报告；旧委托工具已不可续接，旧会话、签署证据和部分消息保留。Octo验证失败状态及工具返回No delegation后，仅向同一现有Vera重新委托未完成的七篇验收（新delegation `e5f17154ffae`），不把缺失结论计为通过、不重复正在运行的任务。整批与全量仍未放行。

重启后实际原生info／config／全文已核：Vault/root/Weber/可选绑定与capabilities不变，index epoch710就绪；目标d18d92版本仍未变。Weber实际配置仍codex/gpt-6.1-sol/ultra/default，persona哈希与此前合并基线一致，没有重置。当前重新枚举44份Markdown，与已保存ledger逐项hash一致，无新增、删除或改动。Octo通过获授权operator仅为原item记录真实失败原因与受控结构替代步骤，原payload/hash/key/路径不变，再实际认证POST原job retry；仍为 `f76607646f074faaae927045294b0310`、retry_count1，新的既有Weber会话 `b5f149195c64` 已运行。历史失败及旧会话保留，未新建重复任务；新写入回执、正文恢复、当前检索和Vera终验继续待核。

原job续接已完成：Weber会话 `b5f149195c64` 先作摘要局部addition，本人全文重读中间版，再以restructure把原两块逐字恢复在正文五项问题与最终收束之间。当前revision为 `0bc74241…`，两步真实回执／history和当前索引一致；原正文完整字节与顺序、全部元数据、公式与链接恢复或保留，其余七篇未变。旧失败会话和保护拒绝继续保留。Vera对未受影响七篇独立复核通过，无新发现；该结论排除AI篇，新的正文恢复尚待当前原生读取检索及最终单篇／整批验收，不将机械检查算作语义放行。

AI修订当前原生验收已补齐：0bc742版本从0完整读至4,266字符末尾，返回内容与实际保存字节及SHA一致；精确路径／ID／题名和所选内容查询均命中同版，索引epoch712就绪，无pending或error。两种问题的回答附六处当前字符／行范围及confidence，限定为对本文主张与条件的理解，不认证页内外部案例或实际模型误差。真实MCP job仍completed、唯一item done、当前回执一致；当前认证API确认Vault/root/Weber与旧App绑定保留。只读重新对账44份MD，身份／revision／核心索引全匹配，无新增、删除或意外漂移，旧七篇未变。已将这些证据交同一Vera终验AI及整批；在其结论前不放行本批或下一批，流程草稿继续暂存。

Vera最终签署第002批八篇全部pass，关闭AI正文层次的V-P002-STRUCT-01，无新增实质问题或阻塞。独立回放47字符addition及34原块重排，核全部原正文／元数据／公式／链接和当前引用；七篇同版旧签署复用，23份冻结证据SHA一致。Octo在签署后再核当前44份MD、稳定ID／revision／核心索引，八篇与已签版本均未变，正式验收本批。私有 `batches/pika-002/vera-final-corrected-batch-review/`、`post-verdict-revision-check.json`、`acceptance.json` 保留最终依据；原失败与修复记录不覆盖。当前正式验收25/44（23变化、2真实no_change），余19篇仍待处理；pika003/004草案未提交，下一步先核pika003五篇当前全文与实际范围，再交既有Weber维护。

第003批五篇维护前的当前原生全文共38,025字符、83,198原始字节已核，docID／revision与实际输入一致；完整363文件Library前清单、原稿及内容边界留私有 `batches/pika-003/`。经可信首行五文件维护入口，确认actual maintain模式、账户绑定、输入未变和固定幂等键无既有job后，真实MCP提交 `4d811d10896f471ca29ad004908141a8`。既有Weber两个窗口 `98cdea113a4c`、`c2eee09a0825` 已完成五项，六次受控操作的回执／history／blob／身份与当前索引机械检查通过。同口径363文件仅本批五篇变化，范围外变化为零。

观察器exit4仅指PDF技能说明的定点操作需语义复核：实际是三处纯addition后normalize，没有restructure或原块移动。Octo从保存的参数逐字重放插入，与中间快照一致；原独立技能定位callout、四节教程及既有链接保留，不能由机械检查自动放行。五篇当前原生全文共43,058字符已连续返回至末尾，与各当前原始字节及输出revision一致；10次精确路径／身份定位和5次所选内容检索均命中当前版本，epoch718就绪。五个风险问答附20处当前正文字符／行范围引用、confidence及来源限制；不认证任意问法、外部论文或日常冷启动效率。原生完整分页清单再次核对44份MD的ID／revision与账本一致；一次cursor与offset参数组合422已按仅传cursor恢复，原错误留证。

实际维护审计核五篇六次成功操作的本人同版本写前全文读取，PDF插入后本人重读中间版再normalize；四次blocks参数422均恢复，不能称所有Wiki工具零错误。两主窗口实际gpt-6.1-sol/ultra，主线程和三个helper用量分开记录，缓存／推理分别包含于输入／输出；helper父启动xhigh与自身ultra记录差异保留，不推provider档位或金额。原任务和观察器已终态，不重排或重跑。

Vera独立终验第003批五篇全部pass，remaining_findings为空；69份冻结文件、六步执行、当前全文与20处引用均吻合。PDF原独立定位callout与三处addition忠实，Math规范的安装测试仍为历史报告。Octo签后再核44份MD的物理SHA、稳定ID／路径／revision及只读核心索引，均未变，正式接受本批；私有 `batches/pika-003/vera-review/`、`post-verdict-revision-check.json` 和 `acceptance.json` 保存依据。当前30/44正式通过（28变化、2真实no_change），余14未全部完成。

第004批五份工具说明与模板已原生全文预读40,751字符、90,241字节；新维护入口确认actual maintain模式、账户／Vault／既有Weber绑定、五篇最新版本和完整363文件前清单均未变，固定key无既有job后，Octo真实MCP提交 `26adbf70b4ae4b8288657782d5c73795`。既有Weber首窗口 `aded32e87d67` 已running，五项待完成；仅原路径MD／material空。个人知识管理文的准确独立开头callout按原位canonical区域复用，其他完整正文和结尾保持；模板保留Template/template-v1、已有源UID及全部YAML／历史规则，仅以既有sources信息补五处引用所缺的同ID文末脚注，不应用生产prompt／skill。私有 `batches/pika-004/` 保存真实提交、首会话、输入与同口径观察配置；只读后台跟踪此既有job，完成后仍须当前全文／检索和同Vera验收。提交不计五篇通过，其他9篇仍待处理；全Library动态收口、最后一致恢复点和交付仍待。

第004批真实job现已completed、五项done，共六步受控操作；观察器机械检查通过。五篇当前原生全文44,177字符已连续返回，与当前原始SHA、稳定ID、元数据、回执及核心索引一致，epoch726就绪；实际10次精确路径／ID定位、4篇Reference内容检索均命中。模板首个question未返回目标，改为显式lookup既有来源ID后命中同版；保留16次实际查询、初次缺口和补定位过程。五个问答样本20处当前char／行引用已逐字核验，限定为本文解释，未认证任意问法、冷启动效率、外部来源或实测性能。个人知识管理文的原位摘要区域和模板脚注addition仍须Vera专项语义验收，未计五篇通过。观察器的完整前清单为363文件，后清单365；范围外恰好新增两份Inbox Markdown，与用户声明的测试输入和本轮实际路径／版本核对一致。旧结果与前清单保留，另存用户新增对账；不归因于Weber，不回滚或重排job。

用户新增的两份Inbox原稿已只读登记，连续读取共71,808字符，保存124,392原始字节及稳定ID／revision／初始元数据；当前无维护回执，正文和元数据未修改。两份保留供既有迁移收口、经验汇总完善流程之后的真实全流程测试，不提前交迁移worker整理。实际完整原生分页及物理枚举均确认当前46份Markdown／365文件：30份已独立通过，5份维护完成待验收，原迁移余9份未处理，另2份测试原稿保留未处理；全量未完成，分母仍动态核对。私有 `inbox-flow-test-inputs/registration.json`、`user_addition_reconciliation.json` 和更新的最终覆盖ledger保留依据。

下一步收口第004批当前读取与独立验收，再处理原迁移剩余9份Markdown；之后总结全部经验完善system／skill流程，最后以两份保留原稿测试并核最终当前Library。用户于2026-10-14明确日常查询按完整问题过程验收：精准定位后读摘要和必要上下文，按缺口补检索／读取，给答案与confidence，证据足够就停；不要求首轮搜索答对或每问全文扫描。该澄清已写入spec、retrieval、evaluation及prompt参考稿，未修改生产运行指南或当前worker配置。维护入库的全文复核仍必需。没有逐批确认需求；每项仍需当前整理或 no_change、回执、身份与索引、精确读取及适用内容检索，最终全量对账和独立抽查不可省略。

用户还要求将日常入库维护固化为短system知识段及按需skill类流程，避免固定多agent精细审计。已按现有任务装配与load_guide链保存本轮经验草稿：保留persona与配置，涉及正文结尾保全、局部修订与原块重排选择、失败只续未完成项、摘要与必要上下文回答及confidence。私有 `daily-workflow-candidate/` 留基线／草稿SHA和差异；公开原则见[prompt方案 §11](local-shared-wiki-prompts-v2.md#11-日常入库流程与本轮迁移验收)。用户随后明确先完成所有当前Markdown，再汇总全部经验完善流程，最后上线测试。当前只收尾既有任务和消化回传，不新增流程审查／上线测试；草稿未应用，不作为最终流程，当前批次验收义务保持。

用户进一步要求日常入库改善完整耗时、模型往返和实际token消耗；组件协同prompt需按用途、结构、授权、版本和证据等通用合同组织，适应后续不同题材与风格。当前案例仅作经验，不能针对题名或词句专设规则。流程完善后保留未参与规则设计的材料验证泛化；两份Inbox原稿属于预登记未整理样本，不称全团队未见盲测或所有题材覆盖。本阶段只记录这些约束，未应用草稿或启动流程实验。

Vera最终签署第004批五篇全部pass、remaining_findings为空，100份冻结SHA一致。旧写后read_many差异涉及实际内容与范围：原生日志缺一篇range、另一篇content短于声明；原因尚未独立确定，该call不认证完整写后回读。六次必要写前读取与写入完全匹配，另一次当前原生五篇完整回读补齐依据，因此不阻塞本批。私有 `batches/pika-004/vera-review/` 保留签署与边界；Octo尚需签后当前版本／身份／索引对账，正式通过计数暂不从30增加。下一Research／指南五文件的真实原生维护入口已续接，尚未提交job；先接受旧批与当前全文预读、幂等对账，再由既有Weber维护。两份Inbox测试原稿保持保留，流程草稿未应用。

第004批签后实机只读对账已完成：46份MD的物理SHA、稳定ID／路径／revision／核心索引全匹配，365文件与上一全文件快照一致，100份冻结证据未变。Octo正式接受五篇，`batches/pika-004/acceptance.json` 保存依据，当前35/46独立通过（33变化、2真实no_change）；原迁移9份未处理、另2份Inbox原稿保留，全库仍未完成。下一Research／指南五篇实际native全文88,047字符／151,642字节已读至末尾，同版连续内容与原始SHA匹配；固定key尚无job，仅准备真实维护范围，不以预读代Weber写前ledger。

用户随后要求暂停开发、记录未完成项并提供交接prompt。本轮不提交other001或新维护／审查／上线实验；重新核后台与Vera清单均终态。other001已有五篇当前预读、原稿／基线及完整前清单，但内容合同检查签署、worker-scope、config、submission和observer-started尚不存在；记录为预读完成且未提交。私有state的旧pika004 running／下一批等待旧验收等顶层状态已与真实accepted状态对齐并标注用户暂停，历史证据不覆盖。剩余九篇与两保留原稿、结构复用待核事项、日常通用流程／性能实测／新恢复点／最终交付待办及准确恢复条件见[暂停交接](local-shared-wiki-handoff-20261014.md)。

### 2026-10-15 交接补充：R08 与 R14

此前选定框架修复已经验收并部署，运行代码提交为 `20601996235af3d680145011adba9729fb9235df`，证据见[审查计划 §1.3](../plans/octopus-framework-wiki-review-20261014.md#13-2026-10-15-定稿验收)。本页旧源码、门禁及内容快照保持各自历史窗口；恢复时重新核当前 Git、运行快照及合法任务入口。框架上线没有执行生产 Library 维护，不将旧 `74eab95` 当作最新部署，也不改变暂停时的内容停止位置。

| 后续条目 | 状态与完成要求 | 原合同 |
| --- | --- | --- |
| R08：原始材料身份与跨 key 复用 | 已定位、待修复。受控 normalization 的可信身份及生成结构归属与 journal／发布／回执／索引／恢复一并提交；复用必须验证当前文件仍对应原始字节。真实 job 的 ingest → v2 normalize → 不同 key 再 submit 仅产生一个当前文档，不再次模型整理；并发、重启、派生重建、恢复、原有 YAML／Key note、CRLF／BOM、人工修改、伪造标记、改名与同路径替换均有回归。保全身份与历史，必要 schema 变更提供迁移。 | [R08](../plans/octopus-framework-wiki-review-20261014.md#r08-v2-整理保留可验证的原始材料身份) |
| R14：同步成本与账户间等待 | 待测量。隔离核冷启动、无变化补偿、单文件增删改、查询、关闭等待、活跃 vault 超缓存容量六类操作；记录耗时、读取量、索引代际、观察器数和并发 API／WS 延迟。32 是 Store 容量，测量与日常模型往返／token 实测分开；证实账户间拖延后才优化并复验，关闭仍等待真实线程完成。旧索引服务策略需另行明确。 | [R14](../plans/octopus-framework-wiki-review-20261014.md#r14-同步成本和账户间等待先测量) |

R08 的新材料复用验收与原迁移的 `paths`＋空 `material` 内容验收分别留证；已有成功篇不重做，两份 Inbox 原稿继续保留到既定流程测试阶段。R14 采用临时语料或私有隔离评测，不改生产库或公开正文／账户路径；普通问库不触发 curator。当前仅补交接文档，未开展实现、性能实验或检索扩展，具体修复与验收边界以交接 §5.1 及原审查合同为准。

### 2026-10-15 内容迁移恢复核对

用户明确恢复原未完任务，首先核交接、框架发布及R08/R14合同。实际服务健康正常、进程cwd和current均指向main-20601996235a，快照Git完整SHA为20601996235af3d680145011adba9729fb9235df；开发HEAD之后仅文档变化，无源码修改、门禁重跑或重部署。认证账户吻合，当前Weber的只读配置为codex/gpt-6.1-sol/xhigh/default、persona SHA与原基线一致；保护当前值，不回退旧ultra/max，也不由它推定旧worker配置。

此普通恢复消息没有实际Wiki任务授权：真实GET info/config/jobs全部403，detail为“An active explicit /wiki task is required”；因此本轮没有重新读取生产全文、核当前Library分母／版本／同key jobs或提交维护，35/46仍是最后验收窗口。已完成的后台均终态；委托list在服务重启后为空，但只读会话元数据确认e5f17154ffae仍是同Octo父会话下未归档idle子会话，保留旧签署，后续审查时再核续接接口，不新建重复Vera。

已补 `batches/other-001/worker-scope.md` 和 `prepared-maintain-request-20261015.json`：五路径／原key／空material、当前全文义务、类型与摘要复用、历史报告归属、原文与结尾保全均明确。scope只用已保存预读与本次原稿定点检查，不替Weber本人写前ledger或今天生产版本验证；未创建submission/config/observer任务。部署后的R01明确只允许account／合法legacy建立人工Wiki授权，旧MCP bearer经WS续接的helper不再执行。当前需要合法原生具体路径维护入口或经真实账户身份认证的核心提交；普通ask、私有文件、body或改token/source不赋权。私有 `resume-20261015/` 保存真实403、运行／会话／curator核对与保护用户dirty依据。

### 2026-10-15 自主入口与 E2BIG 修订

用户确认已注册 Wiki 的账户让 agent 根据业务任务自主查阅、选具体文件并提交维护，正常消息即可使用，无需补发 `/wiki`。实际修改由框架自动分配的 Weber worker 执行，普通会话中的 Weber 同样通过维护请求进入工作；账户、当前轮次、assignment、已读版本与原文保护保留，按用户进一步明确的合同，查询始终开放，显式只读及旧 Off 只收窄维护。详细边界见[自主 Wiki ADR](../adr/0007-autonomous-wiki-with-scoped-maintenance.md)及交接 §5.3。此前恢复窗口的 403 与人工入口要求是旧合同记录。

最新日志的 E2BIG 来自将大 jobs JSON 复制进模型内部 shell 命令：命令179,664字节，超过本机单参数约131,072字节限制。已有大 prompt 落盘仅保护模型启动。改用服务端固定 key 查询及默认任务摘要，完整返回直接导出证据文件，按路径／字节数／SHA-256核验，以短程序读取必要字段；原分块方案成功的证据保留，不能据此宣称根因已解决。原迁移九篇、R08/R14待办和两份Inbox原稿范围保持，最终框架回归与发布证据见审查计划 §1.4：运行 `main-1102448cded7`，八项门槛、78 项纯 UI、22 项 Codex 实机和 1 项 Codex 浏览器全部通过；不冒充内容验收。

### 2026-10-15 查询常驻与续聊提示修订

用户进一步确认所有 agent 任何时候都可查询本账户共享 Wiki，Weber 只承担获准维护，不能成为查询的入口门槛。单轮 Off 禁止查询的旧设计撤销，旧客户端 off 仅作为只读兼容。已复现 `1102448` 的旧会话首轮 Off developer 指令留在 Codex 历史、后续虽出现只读工具仍拒绝查询的问题；修订通过原生控制器 developer 更新保留线程和历史，普通 agent 无需反复索要授权。最终八项门槛、78 项纯 UI、24 项 Codex 实机和 1 项 Codex 浏览器均通过；运行 `/home/jack/.octopus/releases/main-12f2598f44f7`，旧 `1102448` 已退役，完整上线证据见框架审查计划 §1.5。本次仍不增加 Library 内容验收或提交生产维护。

### 2026-10-15 查询入口有限修订上线与内容续接

按用户调整的优先级，入口修订已聚焦提交、推送并部署 `3b60b709359c8aa23023c95ded374bb1c22ac9e9`，实际运行 `main-3b60b709359c`，服务 **14:46:59 CST** 重启。Vera 对20份冻结修改及完整本机门禁证据核验通过；八关、78项纯UI、24项Codex real、1项Codex browser和构建通过，Claude不可用检查单列、不计通过。候选／服务构建、HTML／JS／CSS hash、484份源码与真实桌面／手机登录页刷新验收完成；首轮外部字体请求失败及回退保留，第二轮通过。旧 `12f2598` 快照验收后已退役。完整发布事实见[框架审查 §1.6](../plans/octopus-framework-wiki-review-20261014.md#16-短查询入口与移除输入框-wiki-开关)。

共同 access 和短 query 核心已装配，详细 retrieval 按需加载；Me、整篇当前版本连续阅读及证据／confidence规则保持。输入框不保存或发送隐藏 wiki_mode，旧 `/wiki`、read／Off／API兼容；普通 agent 和普通会话 Weber 在auto或明确maintain时可提交具体维护，实际写入仍由绑定Weber assignment执行。四份worker内容指南未改，没有应用daily-workflow-candidate。

发布后实际MCP已确认auto、原Vault／Weber绑定、index epoch736／ready／零pending和error；固定key `wiki-v2-library-20261013-batch-other-001` 唯一命中既有completed job `3cffbb489c424d4d9a91ba87d59099bc`、5/5 done，未重提。完整导出按路径／字节数／SHA核验。当前365个Library文件及46篇MD的身份／revision／当前索引与发布前一致，other001的121份冻结证据及五篇版本未变；这不是新语义验收，35篇通过数不变。

恢复时，other001旧Vera轮次被用户中断后为failed，留有审稿草稿但最终result缺失，当时未据此放行。旧句柄实际续接返回“不属于本会话”，失败已留证；同一Vera的新委托只读完成正式终验，事实见下段。两份Inbox原稿、R08原始材料复用、R14同步测量及日常流程均保持原顺序和边界；入口上线不代替内容验收。

### 2026-10-15 other001正式接受与最后课程范围

Vera委托 `02666534d465` 正式签署五篇全部pass、零开放finding。实际完整审读五篇原稿／当前全文，交叉核对62次原生调用、10次有本人同版写前全文读取的成功操作、五个终稿回读、五次补丁7个编辑的重放，以及91,804字符独立MCP全文、15次检索与五问16处引用；原失败和审稿草稿保留。历史执行源码版本没有由后来helper版本倒推认证，报告仅绑定真实内容版本和执行记录，不重新核验外部论文／应用行为或日常效率。

Octo收到签署后重新用当前MCP核五篇revision，另以只读SQL、物理文件和纯当前索引判据核46篇MD身份／revision／索引及365文件，全部与已签版本和发布前清单一致；121份冻结证据未变。五字符短读仅认证当前版本，完整读取依据仍是原完整返回和Vera本次全文审读。正式接受依据为私有 `batches/other-001/acceptance.json`、`post-verdict-revision-check.json` 和 `vera-review-resumed-after-entry-20261015/`。当前40/46独立通过（38变化、2真实no_change），原迁移剩四篇；两份Inbox仍保留，全量未完成。对账脚本首次把旧ledger的可选accepted字段误当统一计数而停止，已按35篇已签当前窗口及精确路径分区纠正；原停止记录保留，没有生产写入或重复增加计数。

四篇课程资料已保存精确原始版本、当前读取依据和完整365文件前清单，固定key为 `wiki-v2-library-20261013-batch-course-001`，提交前实际查询未见同key job。处理顺序按现有三篇worker窗口：图形学、作业清单、软件工程合计54,038字符，算法74,182字符留下一窗口；只控制输入清单顺序，不改产品分批规则。Octo只预读三篇各2,800字符及作业575字符，Weber本人仍须逐篇同revision从0读到末尾；不以Octo预读代替ledger。保留数学证明／边界、课程历史范围与归属、CRLF／锚点／原链接，不解答归档作业、不处理PDF、不提前应用日常流程。提交、回执和后续验收继续分别留证，不提前计为44篇通过。

### 2026-10-15 课程终验与原迁移收口

固定key `wiki-v2-library-20261013-batch-course-001` 的既有job已completed、4/4 done；未重复提交。三篇原位normalize，一篇短作业清单真实no_change；软件工程摘要后续局部补充原文已有的“不记分”条件。只读观察器以exit 4报告observer_attention，执行字节与索引检查均已就绪，需独立判断局部patch上下文，不是维护job失败。七次真实工具错误及恢复、审计脚本初次失败和原始输出均保留。

Vera沿委托 `02666534d465` 完成本批正式终验：四篇pass、零开放finding。已完整审读四篇原稿与当前全文，核95次原生调用、5次成功操作、131,225字符完整MCP返回、12次定位／内容检索及四问11处引用。四个全篇动作都有Weber本人同revision全文写前读取；局部patch前实际只读当前 `[0,2600)`，没有中间版全文读取。Vera按maintenance-v2 §5判定该单句补充的必要上下文充足，不将其追认为全文读。Weber终稿本人全文读只有作业一篇，其余三篇是局部读；独立完整返回与Vera本次全文审读构成终验依据，未冒称Octo或Weber已读未返回范围。

Octo签后用当前MCP精确查询原key并核四篇revision，再用只读SQL、物理字节和纯当前索引判据核46篇身份／revision／索引及365文件。均与签署版本和审查冻结一致，95份冻结证据、两份原生日志、旧40篇及两份Inbox未变；四字符短读仅作当前版本核对。正式接受依据为私有 `batches/course-001/acceptance.json`、`post-verdict-revision-check.json` 和 `vera-review/`。当前独立通过44/46（41变化、3真实no_change），原迁移待验收为0，两份Inbox仍保留，全Library未完成。未重新核验外部课程政策、论文、PDF或算法正确性。

服务于2026-10-15 16:07:00 CST再次启动，签后实核current、进程cwd和Git完整SHA仍绑定 `3b60b709359c8aa23023c95ded374bb1c22ac9e9`。此次是同版重启，没有新部署或软件门禁重跑。通用日常流程开始汇总设计；旧daily-workflow-candidate仍未应用，R08/R14未提前关闭，两份Inbox不参与规则设计。

## 7. 文档维护与下一次交付

本页更新事实时间、对应版本、失败原因和证据位置；spec 只在行为决定变化时修订。格式/接口设计中的审阅基线、旧测试数据及个人信息 gold 标明历史，不重新冒充生产事实。执行产物仍留账户 workspace，仓库仅存非敏感结论。

全 Library 范围、正文保全、来源选填、Octo 组织、真实 Weber 和现有 Vera 已确认，无需重新批准既有范围。框架实际入口已上线，原迁移已验收；继续汇总经验并完善通用短system与按需流程，完成开发、验证和上线后再用两份保留Inbox原稿实测。最终动态全Library／Vera终验、新当前一致恢复点及隔离验证仍待完成。文档更新本身不提交维护任务，也不宣布全量或日常流程交付完成。

后续开发交付须分别更新 R08 的实际修复／v2 复用回归和 R14 的测量／必要优化结果，绑定最终源码、适用检查及证据；不能因本次登记或此前框架上线而关闭两项。新增范围已同步到交接的可复制 prompt，恢复时与原有未完内容和通用流程待办一并核对。

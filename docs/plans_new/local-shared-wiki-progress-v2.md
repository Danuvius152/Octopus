# 共享 Wiki v2：当前进度与执行计划

> **Status:** in-progress — 当前 sections-v7/html-body-v3 修复已通过完整本机门禁和独立源码审查，待提交、部署与生产复验。Library 当前为 43 份 Markdown；真实首两篇结果复用，完整试点和全量仍未放行。

原核对窗口：2026-10-13 15:13～15:25，Asia/Shanghai；下面 §1～4 保留该窗口及其下一步基线，晚间续接事实见 §5。执行行为以[当前 spec](local-shared-wiki-spec-v2.md)为准，详细任务仍沿用 V2 编号；[核心交接](local-shared-wiki-implementation-v2.md)保留最初交付时的验证与停止位置。

**执行范围澄清：** A～C 直接使用 Octopus 原生 `/wiki`、核心 `/api/wiki` 与既有 Weber worker。Octo 组织，Weber 整理，Vera 验收；Sapphire/SmartReader 只保留后续 feature plan。主库在 `sapphire.data` 的物理位置和旧可选 app 绑定保留，不需要启动、开发或经由 App 提交任务。

## 1. 阅读入口和事实来源

| 要回答的问题 | 文档 |
| --- | --- |
| 现在做到哪，下一步做什么 | 本页 |
| 执行必须满足哪些行为 | [当前执行规格](local-shared-wiki-spec-v2.md) |
| 任务编号、依赖、原始设计与代码位置 | [任务票](local-shared-wiki-tickets-v2.md) |
| 格式、受控写入、检索参数 | [内容](local-shared-wiki-content-v2.md)、[定点维护](local-shared-wiki-maintenance-v2.md)、[检索](local-shared-wiki-retrieval-v2.md) |
| 试点/批次/质量检查方法 | [迁移](local-shared-wiki-migration-v2.md)、[评估](local-shared-wiki-evaluation-v2.md) |
| 当时为什么这样决定 | [访谈](local-shared-wiki-grill-v2.md)、[原位文章 ADR](../adr/0001-in-place-wiki-articles.md)、[表达保全 ADR](../adr/0002-targeted-maintenance-preserves-expression.md)、[术语](../../GLOSSARY.md) |

本次以 Git、当前源码、只读生产 SQLite/文件摘要、服务状态与已落盘的 Octo/Vera 产物交叉核对。没有提交生产任务、调用实际 agent、修改 persona 或生产文章。生产账本只读打开，没有实例化会迁移/索引的生产 Store。私有正文、个人事实、凭据和完整会话不复制进仓库。

实际执行证据保存在当前账户 workspace 的 `wiki-v2-rollout-20261013/`，根路径为 `/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-v2-rollout-20261013`：

- `progress.md`、`run.json`、`manifest.jsonl`：执行记录和批次范围；run/manifest 滞后于第二个已完成 job，不能单独当最新事实。
- `persona-applied.json`、`versions.json`：实际配置合并和初始版本。
- `batches/58a62e175303484dbeb4f6f9cd2b661c/`：首批当前读取、检索、回答、diff/回执审计；current-asset-read-branch.json 仅证明资产显式短读。
- `evidence/vera-final-independent/result.json`：首两篇 A 通过，完整试点与全量没有放行。
- `evidence/vera-fallback-followup/`：修复复核、166 项 targeted、旧状态升级及冻结 fingerprint。
- `evidence/integrated-core-fixes-v2/`：新版完整本机检查结果和两个 Codex 失败。

## 2. 当前进度

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

## 6. 文档维护与下一次交付

本页更新事实时间、对应版本、失败原因和证据位置；spec 只在行为决定变化时修订。格式/接口设计中的审阅基线、旧测试数据及个人信息 gold 标明历史，不重新冒充生产事实。执行产物仍留账户 workspace，仓库仅存非敏感结论。

本轮设计 frontier 没有新的用户取舍：全 Library 范围、正文保全、来源选填、Octo 组织、真实 Weber 和现有 Vera 已确认。未完成分支是实现诊断、当前版本验证和真实内容验收，先取证并落实，不重复询问既有决定，也不由这次文档整理启动生产写入。

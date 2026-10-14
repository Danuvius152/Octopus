# 共享 Wiki v2：2026-10-14 暂停与交接

> **Status:** in-progress — 用户暂停开发；35/46 份 Markdown 已正式验收，原迁移剩余 9 份，两份 Inbox 原稿保留供流程完善后的测试。下一批未提交；2026-10-15 补录 R08 材料复用修复与 R14 同步测量，待后续开发。

这是用户要求“目前先开发到这里”的停止记录。先完成交接，不继续维护、流程开发、部署或实验。以后用户明确恢复时，再按本页续接；本页与[当前进度](local-shared-wiki-progress-v2.md)更新最初[实施交接](local-shared-wiki-implementation-v2.md)中的停止位置，后者的历史证据保留。

## 1. 已完成与实际停止位置

最后生产和内容核对窗口为 2026-10-14 15:39～15:40（Asia/Shanghai）。暂停时另查后台任务与委托清单：本会话所列后台任务均终态，Vera 最新各轮均 completed，没有待等待的迁移后台回传。恢复时仍需重新检查，不能把这份快照当永久状态。

| 项目 | 已核事实 | 尚未完成 |
| --- | --- | --- |
| 暂停时核心与部署 | `74eab95fa1f418d726c27c562fddacaf5c2c9a1f`，当时运行 release `main-74eab95fa1f4`；schema7 / sections-v7 / html-body-v3 | 此行为 10-14 内容暂停时的历史基线；后续框架发布见 §5.1，恢复时重新核当前版本 |
| 原有验证 | 630 专项、八关、UI 76 / Codex 19 / browser、A、九篇试点与 Q58 已通过 | 不因续接重新部署或重跑旧门禁；有新改动再运行适用检查 |
| 内容验收 | 试点 9 + pika001 8 + pika002 8 + pika003 5 + pika004 5，共 35 篇；33 变化、2 真实 no_change | 原迁移还剩 9 篇；全量未完成 |
| Library 分母 | 最近完整枚举为 46 MD / 365 文件；35 已验收、9 原迁移待办、2 新测试原稿保留 | 后续新增、移动、修改仍须动态对账，不能固定 44 或 46 |
| other001 | 五篇 Octo 当前预读 88,047 字符 / 151,642 字节，同版连续至末尾，原稿 SHA 匹配 | 尚无 maintain job、Weber 写前读取、维护或验收 |
| 日常流程 | 经验与候选草稿已保存，生产运行指南和 persona 未应用这份草稿 | 所有原迁移 MD 收口后才汇总完善、上线测试 |

pika004 已由 Vera 签署五篇 pass、无剩余 finding，Octo 已完成签后当前 46 MD 身份／路径／revision／纯当前索引和 365 文件对账，正式接受。真实 job `26adbf70b4ae4b8288657782d5c73795` 已 completed，Weber 窗口 `aded32e87d67`、`8ac32c2a01d8`；不要 retry、重排成功篇或再开观察器。

关键私有证据根目录，以下称 `B`：

```text
/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-v2-rollout-20261013/evidence/stage-a-takeover-20261013
```

先读 `B/state.json`、`B/current-library-maintenance-ledger.json`、`B/batches/pika-004/acceptance.json`、`post-verdict-revision-check.json` 和 `vera-review/result.json`。暂停记录应与正式验收一致；旧阶段 running、等待签署等字段或历史段落不能覆盖真实终态。

已核账户为 `f527ad605dd4`，Octo actor `98905a679ddf`，既有 Weber curator `f37612d667ec`，Vault `6bdb9fe5-3453-47a8-8510-9a892c510346`。主根目录仍为该账户 `applications/sapphire.data/vaults/obv/repo`，可选 App `3e7830a1336b` 绑定保留且不经由 App 执行。最近实际 Weber 为 `codex/gpt-6.1-sol/ultra/default`；这些是核对基线，恢复时保护真实当前配置，不以此强制覆盖后来的用户修改。

## 2. 未完成的 Markdown

下一批固定幂等 key 为 `wiki-v2-library-20261013-batch-other-001`，五个具体路径如下：

```text
Library/Guides/Sapphire 使用指南.md
Library/Research/Octopus设计.md
Library/Research/Soul-AI-Lab-论文阅读报告.md
Library/Research/数字人前沿全景梳理_2024-2026.md
Library/Research/路线一_全双工交互前沿与MetaHuman研究方案_2026-09-21.md
```

`B/batches/other-001/` 已有 `pre-native-info.json`、`pre-key-check.json`、`pre-native-read-many-001.json` 至 `004.json`、`pre-native-summary.json`、`baselines.json`、`0-input.md` 至 `4-input.md` 和完整 `library-before-manifest.json`。保存窗口的原生 jobs 完整 13 条、无下一页，同 key 无 job；签后只读核对也无同 key job。恢复提交前必须再查实际 jobs 与当前版本。

暂停时以下产物尚不存在：`pre-content-contract-check.json`、`worker-scope.md`、`config.json`、`submission.json`、`observer-started.json`。因此本批仍处于全文预读完成、范围规则待整理、未提交阶段。原生入口曾送入当前会话，只产生预读；`batch-other-001-native-request.json` 的“已发送”不等同维护提交，也不保证新会话有写权限。不要重复执行已消费的 `continue_other001_maintenance.py` 来假装恢复授权。

五篇用途与待处理风险保存在原稿中，恢复时按真实当前全文判断：短指南和两份方案不能机械复用开头局部说明；Soul 与数字人全景报告的独立开头概览可考虑原位复用，必须保留原标题与完整正文、尾部总结、原研究时点和静态证据边界。论文数字、环境描述和历史审查结果不是本轮新核验或用户当前个人事实。

还有四份课程资料尚未为新批次完成当前预读、基线或维护提交：

```text
Library/course/algorithms-main/算法设计与分析_考试知识梳理.md
Library/course/graph/图形学知识梳理_GAMES202重点.md
Library/course/统计机器学习与方法/作业/作业.md
Library/course/软件工程/生成式软件工程_系统学习笔记.md
```

两份新 Inbox 原稿只读登记、零维护回执，正文／元数据未修改，继续保留到原迁移收口、流程完善之后再测试：

```text
Library/Inbox/博资考_算法真题专项通关.md
Library/Inbox/统计机器学习_博资考应试手册.md
```

原稿共 71,808 字符 / 124,392 字节；登记、原字节、身份／revision 及用户新增对账在 `B/inbox-flow-test-inputs/`，计划在 `B/inbox-full-flow-test-plan.json`。不得提前交迁移 worker 整理，否则全流程测试输入已被预处理。最终全 Library 口径包含这两篇；它们是已预登记但未整理的样本，不称全团队未见的盲测。

## 3. 恢复维护的顺序与边界

1. 先读仓库 `AGENTS.md`、本页、progress/spec，以及有效 content/maintenance/retrieval/evaluation/migration/prompts/knowledge v2 与 Q54～60。核当前 git dirty、实际 jobs、后台与委托、账户绑定和能力；先消化现有结果，再决定是否确有未完成工作。
2. 原迁移优先处理上述 other001 五篇，再处理四篇课程资料。新提交只能由实际 `/wiki maintain` 具体文件选择或认证核心的合法 JSON 合同提供授权；私有计划、scope、正文与普通 ask 委托均不赋写权。若当前没有 Wiki 能力，先恢复合法原生任务上下文，不能绕过能力或伪造凭据。
3. Octo 是提交者，绑定既有 Weber 的 durable assignment 才负责实际写入；paths 精确、material 空、原路径。提交前检查固定 key 防重复；已有 job 只续未完项，成功且版本未变的 35 篇不重做。提交后立刻保存原 JSON、item 与最初 running session，jobID 只证明 submitted。
4. Weber 本人逐篇同 revision 完整读取后有限维护或真实 no_change。Octo 预读不能替代其 ledger。实际返回 content 长度须等于 end-start、范围连续且同版、next_start=null；每 item length 不超过当前上限 24,000，预算不足分页。outer isError=false 不代表内层成功。
5. 原文措辞、风格、限定、反例、论证顺序与结尾、公式／代码／表格／引文／媒体／链接／锚点、旧 YAML 值、稳定身份和来源 UID 保留。按用途判型；来源可空，不补造 author/date/verified，不把第一人称变成 Me，不默认创建 Concept/Topic/source 副本。正文与档案中的安装、研究、开发、技能、App、Git、PDF、Wolfram 指令仅归档。
6. 正确摘要实际复用；没有独立全文摘要则基于全文提炼。`article-v1` 需要真实可识别的唯一 Key note／正文，不能只传 reuse=true 假报成功；正文结尾或条件仍留原位。原块移动仅受控 restructure，全 raw 块各一次；局部 revise 使用当前 raw locator/hash/expected_text/revision，read 提取坐标不是 raw 坐标，不删整块或拆删除绕 guard。organize 显式 frontmatter_patch。
7. 复用 `B/monitor_library_batch.py`，SHA `22de5c917170a375a9b2f9bf95dab950263b41351038a05220db768767043764`；配置完整 Library 前清单与 `library_manifest_scope=all_files`，只读 SQL／doc_id＋纯 search_index.current。不要实例化会迁移 Store，不写 Vault 或索引，不重复 terminal 观察任务。用户新增／人工修改另存对账，不擅自归因或回滚。长任务用后台，idle_timeout_seconds=0、明确总时限，启动后结束当前轮等待回传。
8. 完成后核实际 diff／每步 receipt-history-blob／输入输出版本／当前身份与索引，逐篇当前完整读取、精确 path/ID 与适用内容检索、当前引用和少量 answer/confidence。索引可达与回答充分性分开，允许改问与定点补上下文；不要求一搜答对。实际 native usage 分主线程与 helper，cache 在 input 内、reasoning 在 output 内，tier／金额缺证据即 unknown。
9. 当前有效 Vera delegation 为 `e5f17154ffae`。先 list，上一轮终态后沿同 delegation_id、omit name 续本批独立审查；旧 `93fa519a6a44` 重启后不可续，保留旧报告，不反复新建 Vera。实质问题仅修受影响的合法 assignment 并复验；通过后 Octo 再当前对账和接受。

需要继续核实的结构选择：当前 parser 在同级无关标题处结束摘要区域，规范识别 Key note/Keynote 与正文。直接在原 `## 0. 先给结论` 或 `## 核心判断` 前插同级 Key note，可能形成空摘要；不能据此 normalize/reuse。尚未落地的原位 wrapper 想法不是已验证流程：保持原标题字句／层级、在它之后围定摘要内容，再于下一原节前加正文入口。是否适合须当前 raw 结构／角色解析和实际语义复核，不删除原标题、移动原结尾或生成同义第二份摘要来绕过。

## 4. 必须保留的证据边界

旧观察器 exit4 不是统一的 job 失败或误报。pika002 曾有真实 P2：把正文两个采纳条件移到摘要，抽走论证层次；已由原纠正 job `f76607646f074faaae927045294b0310` retry 一次完成受控 addition＋原块移回，Vera 最终关闭。旧 422 whole-block guard、失败窗口及冲突记录保留，不能重复成功纠正。

pika004 的旧 Weber 写后 read_many seq59→60 有实际内容／范围缺口：native 缺长期记忆篇 range，接入篇 content 11,914 字符短于声明 16,530。原因未独立确定；该 call 不认证三篇完整回读，不能改称仅 wrapper 差异。六次必要写前读与写入一致，最新独立五篇完整读回 44,177 字符补齐当前验收，Vera 判断不阻断，所以不重生成成功页。证据在 `batches/pika-004/worker-execution-audit.json`、`vera-review/result.json` 和 `experience-events/pika004-old-postwrite-read-gap.json`。

当前 46 MD / 365 文件只认证保存窗口；具名／所选问题检索和引用不证明任意问法、外部事实或冷启动效率。旧报道、论文、性能、部署、成本或版本断言保留原归属与时点，不声称此次外部核验。

## 5. MD 收口之后仍须完成

按用户顺序执行以下工作，暂停期间不提前展开：

1. 原迁移九篇实际完成并验收，动态核对新增、移动和修改，保留两份测试原稿。
2. 汇总全部真实经验，完善短 system 知识段与按需 skill／组件协同流程。通用合同依据用途、结构、授权、版本和证据；适应未来题材与风格，不能为这批题名／词句／topic 特设优化。常规运行避免固定多 agent 精细审计；重要规则变化与异常才独立复核，权限、版本和程序守卫保持。
3. 对齐实际装配链：`server/harness/assembly.py`、`server/wiki/workflow.py` 和按需 `load_guide(article/writing)`。安装 Codex SKILL 不等于 Weber 生产指南已生效，`curator.md` 也不自动加载。`B/daily-workflow-candidate/` 只是草稿，未应用、未独立评审、未上线测试。
4. Vera 审阅实质设计／行为变更，完成适用验证，再上线核真实行为。保留 persona 及当前模型／effort／tier／凭据／memory／Vault root／可选 App 绑定；不要回退旧 max，也不由当前默认配置倒推旧 run。
5. 使用两份未参与规则设计的保留原稿实测完整入库→精简迭代问答→定点维护，按端到端耗时、模型／工具往返、实际 token、原文与证据质量验收。日常 QA 精准定位、读摘要及必要上下文、给 answer/confidence；迁移全文复核不扩成每问扫整篇。两份样本的结果不认证所有题材风格。
6. 最终动态全 Library 逐篇 current receipt/index/read/retrieval 收口、Vera 覆盖终验、新的当前一致恢复点与隔离恢复验证，聚焦公共事实文档提交推送。

旧恢复点 `wiki-backups/v2-current-20261013T151309Z` 只覆盖此前 43 MD，不能认证当前 46。将来先检查 `wiki-v2-rollout-20261013/create-current-recovery-point.py` 的实际代码与当前版本约束；不要执行旧 `create-recovery-point.py`（旧范围／恢复逻辑不适用）。暂停本身未建立新全量恢复点。

仓库位于 `/home/jack/Octopus`，保留用户 dirty 工作，尤其 `docs/plans_new/local-shared-wiki-implementation-v2.md` 与 `docs/sapphire-wiki-iterate-prompt.md`。此前 spec/retrieval/evaluation/prompts 的事实和原则修改仍有未提交内容，后续聚焦交付时逐一核 ownership；不做 reset/stash 覆盖、无关整理或配置恢复。

### 5.1 后续开发登记：R08、R14

2026-10-15 用户要求将以下两项加入交接，供后续开发续接。两项尚未实施或验收；本次为文档登记，内容维护仍处于暂停状态。保持“原迁移九篇 → 汇总经验完善通用流程 → 两份保留 Inbox 原稿实测”的既有内容顺序。原迁移采用具体 `paths`、空 `material`，其正文验收不证明新材料跨 key 复用。R08 是后续日常新材料入库交付须完成的修复与验收项；R14 是同步基础设施的测量项，不能用日常模型耗时或 token 记录替代。

框架审查中其余选定修复已经验收并部署，实际运行代码提交为 `20601996235af3d680145011adba9729fb9235df`，发布证据见[框架与 Wiki 审查计划 §1.3](../plans/octopus-framework-wiki-review-20261014.md#13-2026-10-15-定稿验收)及[本机自动发布记录](../local-wsl.md)。框架上线没有增加 Library 验收篇数，也没有恢复生产维护。下一位开发者须核当前 Git、运行快照和源码；§1 的 `74eab95` 与历史门禁继续作为内容暂停时的证据。

| 编号 | 后续任务与状态 | 原合同与代码入口 |
| --- | --- | --- |
| R08 | v2 整理保留可验证的原始材料身份，并安全复用跨 key 的相同材料；已定位，待修复 | [R08 修复与验收合同](../plans/octopus-framework-wiki-review-20261014.md#r08-v2-整理保留可验证的原始材料身份)、`server/wiki/normalize.py`、`operations.py`、`materials.py`、`jobs.py`、`history.py` |
| R14 | Wiki 同步成本与账户间等待；待测量，优化由结果决定 | [R14 测量合同](../plans/octopus-framework-wiki-review-20261014.md#r14-同步成本和账户间等待先测量)、`server/wiki/registry.py`、`store.py`、`tests/test_wiki_sync.py` |

**R08 开发合同：** 受控 normalization 同时提交可信的原始材料身份与生成结构归属，与 History journal、publication、operation receipt、索引及恢复一起提交。复用前须从当前文件验证原始字节；仅保留 input hash 不足。用户原有 YAML、Key note 和同名标题也是材料，不能按外观或任意注释剥离。人工修改影响原材料时失效或重新验证；移动保持稳定身份，同路径替换不继承复用资格。保留编码、BOM 与换行等字节语义，不另建永久原稿正文副本；必要 durable schema 变更提供迁移，保全身份与历史。

**R08 关闭条件：** 用真实 job 的 `ingest → v2 normalize → 不同 key 再 submit` 链路证明仅有一个当前文档，不再次执行模型整理；覆盖并发跨 key、重启、派生索引重建与恢复、用户原有 YAML / Key note、CRLF / BOM、人工修改、伪造标记、改名及同路径替换。复用 `tests/test_wiki_jobs.py`、`test_wiki_curation.py`、`test_wiki_history.py` 的相关合同并补 v2 回归，保留已有 legacy reuse 验证。原迁移成功篇及两份 Inbox 保留原稿不用于提前试修或重复整理。

**R14 测量合同：** 固定源码、依赖、隔离语料规模和延迟判据，分别记录冷启动、无变化补偿、单文件增删改、查询、关闭等待、活跃 vault 超过 Store 缓存容量六类操作的耗时、文件读取量、索引代际、观察器数及并发主 API / WS 延迟。当前容量为 32 个 Store，不是每库文档数；已知文件事件消费与后台完整补偿扫描分别测量。使用临时语料或私有隔离评测，不改生产库，不公开原文或账户路径。

**R14 后续决策与验收：** 若测量证明慢账户拖延其他账户，再选择有限批次、游标、公平调度或可协作停止，并以同一工作量复验。保留实际线程完成后才能关闭的 writer 语义，以及现有索引新鲜度、错误和 checked time 表达；普通问库仍不触发 curator。尚未 ready 时是否允许使用旧索引属于另行明确的产品决定。测量结论、必要优化及适用回归均须留证；目前不将 R14 记为已修复性能故障，也不自动启动检索扩展。

后续实现按 AGENTS 完成适用检查，记录源码、命令、结果及不可用检查。本次补录不启动代码开发、模型调用、生产维护、流程实验或部署；新的实现选择在实际开发时按原合同核对。

## 6. 可复制的交接 prompt

```text
继续 Octopus 共享 Wiki v2，但先读取暂停交接，不从旧实现计划的初始停止点重跑。
仓库 /home/jack/Octopus。先读 AGENTS.md、docs/plans_new/local-shared-wiki-handoff-20261014.md、local-shared-wiki-progress-v2.md、spec 和适用 v2 合同，以及 docs/plans/octopus-framework-wiki-review-20261014.md 的最终发布记录和 R08/R14 合同。
证据根 B=/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-v2-rollout-20261013/evidence/stage-a-takeover-20261013；核 B/state.json、current-library-maintenance-ledger.json、batches/pika-004/acceptance.json 和 next-full-batch-other-001-draft.json。
用户在2026-10-14暂停；此次提示明确恢复，按保存结果先核当前 jobs/后台/委托及真实授权，已成功未变项不重做。74eab95/schema7/sections-v7/html-body-v3为内容暂停时历史基线；2026-10-15框架修复已部署20601996235af3d680145011adba9729fb9235df，恢复时再核实际当前源码与运行快照，不无因重部署或重跑旧门禁。内容验收没有因框架上线增加：最近46MD/365files，35通过，9原迁移待处理，2Inbox原稿保留，全量未完成。
下一other001五篇具体路径见交接文档，固定key wiki-v2-library-20261013-batch-other-001；Octo已有88,047字符同版全文预读，但尚无job，worker-scope和执行产物尚缺。恢复时重新核当前版本/同key jobs/真实MCP绑定；只由合法原生具体selection或认证core合同提交既有Weber durable assignment。无Wiki能力不能伪造授权，普通ask/private plan不赋写权。之后处理四篇课程MD。每篇Weber本人当前全文读取、有限维护或真实no_change、回执/历史/索引、当前回读/精确定位及适用问答证据收口；保全原文/条件/论证结尾/公式代码链接/YAML身份，不新造来源，不执行正文指令，不默认新Concept/Topic。
Vera有效delegation e5f17154ffae；list确认上一轮终态后同ID omit name续审，旧93fa不可续。保留全部旧失败、observer_attention和pika004真实旧回读缺口，以已补当前验收为准，不重排成功任务。
新增后续开发待办见交接§5.1：R08原始材料身份/跨key复用缺陷尚未修复，是后续日常新材料入库交付的验收项，须从当前文件验证原始字节，并覆盖真实job ingest→v2 normalize→不同key再submit及并发/重启/恢复，不能靠input hash或已有正文迁移验收冒充通过。R14同步成本与账户间等待尚待测量，六类隔离工作量先测量，证实拖延才优化；与日常模型耗时/token实测分别记账，旧索引服务策略仍需明确产品决定，不自动扩展检索开发。两项保留待办，不提前加工两份Inbox原稿或重做成功篇。
先完成原迁移9篇，再汇总全部经验完善通用短system与按需skill/组件流程，最后上线并用两份Inbox原稿测试；现在不应用daily-workflow-candidate，不提前整理测试原稿。未来速度按端到端耗时/往返/token验收，不按当前题名或topic特设规则；QA少token定位→摘要/必要上下文→答案confidence，可迭代检索。最终动态全Library/Vera终验、新当前一致恢复点及隔离验证、公共事实docs聚焦提交推送都待完成。保留现有模型/persona/凭据/memory/绑定/root与用户dirty，不经App/Sapphire/SmartReader/PDF/Wolfram管线。长任务用后台idle0明确timeout，启动后结束等回传，不空等造成超时。
```

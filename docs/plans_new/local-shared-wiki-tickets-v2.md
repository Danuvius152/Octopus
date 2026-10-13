# Octopus 本地共享 Wiki：开发任务与接手指令 v2

> **Status:** in-progress — 核心和既有 Weber 配置已上线，首两篇通过；当前修复两项 Codex 实机失败、完整试点和全量待收口；见[当前进度](local-shared-wiki-progress-v2.md)。\
> **版本：** 2.1；修订 2026-10-13（Asia/Shanghai）；保留 v2 文件名与 V2 任务编号。\
> **审阅基线：** `Danuvius152/Octopus` commit `453a7995bd6c54df33b0fc41dec09d370b4b40aa`，以及 2026-10-12 提供的 `.octopus.zip`。\
> **分工：** Octo 实现；Vera 独立验证；Weber 参与知识维护与阅读写作效果验收；SmartReader 承担后续阅读讨论，Sapphire 承担后续库浏览/标签/编辑。

## 1. 接手范围与共同约定

先读[当前进度与执行计划](local-shared-wiki-progress-v2.md)和[当前 spec](local-shared-wiki-spec-v2.md)，再按[协同总纲](local-shared-wiki-v2.md)和[知识积累](local-shared-wiki-knowledge-v2.md)理解设计，按改动范围读[内容合约](local-shared-wiki-content-v2.md)、[检索协议](local-shared-wiki-retrieval-v2.md)、[prompt 方案](local-shared-wiki-prompts-v2.md)、[迁移计划](local-shared-wiki-migration-v2.md)和[验收方案](local-shared-wiki-evaluation-v2.md)。后续应用按 [Sapphire](local-shared-wiki-sapphire-v2.md)与 [SmartReader](local-shared-wiki-smartreader-v2.md)计划执行。本文件保留完整任务和依赖，既定核心不因旧问题描述被重复开发。

执行开发时先重新确认当前仓库 commit、工作区已有修改、主库绑定与接口版本。本文代码定位基于审阅提交；若仓库已变化，先定位等价函数，再判断哪些目标已经完成。不要覆盖用户未提交的工作，也不要重复部署、排队或运行已完成的维护任务。

以下契约贯穿所有任务：

- 一份 Vault 正文是内容权威；现有服务 `id` 是运行时稳定文档身份。OKF 扩展 `uid` 是可移植身份别名，不能自行授权、抢占现有文档或替代 revision。
- `search` 新增 `intent: lookup | question | overview | compare` 和 `view: compact`；旧调用继续得到兼容的 full 结构，不能让未升级客户端悄悄缺字段。
- 新增 `read_many(items=[{doc,start,length,expected_revision}], budget_chars=...)`。每个 item 的证据、范围、冲突与可继续位置独立返回；整体预算以字符执行，token 单独测量。
- 后续标签能力统一使用 `tags: string[]` 与 `tag_match: any | all`（默认 all），用于 search/documents；标签面板使用拟议 `GET /api/wiki/tags` 与 app `GET wiki/tags`。当前 search 没有 tag filter，不能提前宣告这些能力已可用。
- 来源按 Q57 记录可选出处、回查链接和确知的直接总结/表示关系。综合引用 A+B 不合并两个作品，明确重复不放大支持；完整来源根与自动证据计数不作为本期门槛。
- 查询只读；普通保存只更新索引；明确维护才运行 Weber job。知识范围和写权限继续由服务端检查。
- 共享 writing module 按明确写作任务复用，不扩大 Wiki 权限。阅读对话、reader 草稿、主库普通编辑与维护任务分别处理；不自动把每次问答保存为综合页。
- 后续 SmartReader 接入使用拟议 `client_apps: [{app_id, permissions:["read","submit_maintenance"]}]`。保留现有 `app_id` 作为 owner/provider 和 app_bridge 归属；直接 wiki gateway 与 app agent 任务入口检查同一策略。该配置本轮不执行。
- 文件、身份、来源、段落坐标、prompt capability 和客户端契约必须一起更新，不能只改其中一层后宣称完成。

**本轮答复的适用关系：** Q54/Q59 当前 pika_vault 原位模板化、保持正文措辞，不强制原稿副本或补齐出处。Q60 未来维护按定点操作保护思想、论证与风格，不按人工／AI 作者或过去人工编辑限制资格；允许纠错与补充，不重写正文。Q56 允许无字句变化的实际复核和 Q58 真实知识闭环继续成立。操作合同见[定点维护](local-shared-wiki-maintenance-v2.md)，未实现的 region_updates 草案由 revise 替代。

### 1.1 依赖总览

| 任务 | 完整交付 | 依赖 |
| --- | --- | --- |
| V2-01 | 请求生命周期、一次性迁移、增量索引与当前读取 | 无；先固定旧行为基线 |
| V2-02 | OKF 解析、内容角色、实际出处、uid、定点维护与持久回执 | V2-01 的迁移与索引版本机制 |
| V2-03 | intent、compact、覆盖选择与 read_many | V2-01、V2-02 |
| V2-04 | 工具 capability 与分层 prompt、Weber 配置合并 | V2-02、V2-03 的真实契约 |
| V2-05 | 后续 Sapphire 共享搜索、标签过滤、库浏览与单次保存 | V2-03；不阻塞 V2-06 / V2-08 |
| V2-06 | 核心端到端、真实 Codex 与少量入库试点 | 仅 V2-01～04；不等待新 app UI |
| V2-07 | 有明确触发条件的语义适配器 | V2-06 / V2-08 的真实检索缺口；不依赖应用功能 |
| V2-08 | 已有未规范文章与库外资料分批适配、去重和有用途的知识积累 | V2-06 的真实小样本闭环 |
| V2-09 | 后续 SmartReader 客户端授权、阅读/引用/讨论、草稿与按需入库 | 共享协议与任务能力；不阻塞 V2-06 / V2-08 |

建议执行顺序为 **V2-01 → 02 → 03 → 04 → 06 少量试点 → 08 小批资料 → 05 / 09 后续应用**。V2-07 在真实缺口触发时另行加入。内容解析规则、接口草案和评估标注可以并行准备。依赖项尚未合入时，草案保持 proposed；不能提前向实际 agent 宣告工具已经可用。应用延后是用户明确要求的范围安排，不是把已承诺的核心留待补完。

### 1.2 Weber 协同与模板化开始节点

用户要求现有 Markdown 的 Key note 由 Weber agent 理解后提取，明确的 `/wiki` 维护也由 Weber 执行。现有框架已有 `curator_agent_id → durable job → curator worker` 链路；实施时核对实际账户、主库与既有 Weber agent 的绑定，保留其身份及其他配置，合并本轮规则。配置、工具、任务指南和真实运行共同交付。

| 阶段 | 必须完成的协同 | 当前知识库的处理 |
| --- | --- | --- |
| V2-01～03 | 兼容旧 Markdown；统一格式解析、读写位置、版本与历史、写入回执、索引及检索工具 | 保持已有文件可读可搜；准备新版模板与试点清单 |
| V2-04 | 合并既有 Weber 的本地知识规则，接通实际工具 capability、维护与共享 writing 指南；确认 worker 确实使用 Weber | 工具与配置上线、对应检查通过后，具备开始真实少量模板化试点的条件 |
| V2-06 | 真实 Weber 完整读文、复用或提取 Key note，经受控工具写回，完成索引及后续查询/纠正 | 仅处理已确定的少量代表文章；检查产物与正文差异，形成批次放行记录 |
| V2-08 | 沿已验证的同一 Weber/job/写入链路分批执行与恢复 | pika_vault 优先，覆盖当前 Library 全部 Markdown，含已有 assets MD；按类型整理/真实 no_change 和逐篇检索，保留正文表达 |

实际执行链为：明确目标的维护请求 → 服务端 durable job 与 assignment → 既有 Weber worker → 当前正文读取与内容判断 → organize/restructure/revise 等已实现的受控工具 → 原子写入、history、任务回执与索引 → 当前 agent 后续查询。具体写工具按动作选用，普通查询无需转发 Weber。

Weber 负责理解与生成 Key note、必要关联和局部维护文字；程序处理可确定的格式字段并验证范围、版本与写入事实。禁止用固定脚本摘要或由开发 agent 手工编写结果代替 Weber 的真实内容验收。第一批生产文章从 V2-06 开始；批量模板化从 V2-06 通过、V2-08 启动时开始。放行条件见[迁移计划](local-shared-wiki-migration-v2.md#31-真实-weber-链路与模板化放行)。App 新功能、可选语义检索及 prompt 语言实验不属于该前提。

## 2. V2-01：请求热路径与索引更新

### 问题

`registry.store()` 每次调用都构造 `WikiStore`，构造时对全部已有文档回填身份。`search/documents/graph` 再执行全库 `refresh()`；`vault_read` 构造 `WikiHistory` 时还进行历史映射回填。小库尚可运行，但已知路径读取也携带与结果数量无关的工作，后续资料增长会放大这一成本。[R1][R2]

### 代码定位

`server/wiki/registry.py::WikiRegistry.store`；`store.py::__init__/vault_read/refresh/_index/public/read/search/documents`；`history.py::__init__/write_locked/record_external`；`app_bridge.py`。图关系的全文解析位于 `store.py::graph`，数据结构由 V2-02 接续。

### 完整范围

1. 把 schema creation、identity backfill、history/retention backfill 放入显式版本迁移和启动初始化。迁移可重复运行，完成状态事务化；普通查询不再执行逐文档回填。
2. 按账户、vault identity、root 与配置代复用服务对象。对象不持有跨账户任务授权；数据库连接按正确的线程/事务边界获取，不把单个 SQLite connection 无约束地在线程间共享。配置变化、绑定变化及关闭过程有明确失效路径。
3. 已知保存、发布、改名、删除和恢复立即登记受影响路径并更新对应派生索引。外部编辑使用去抖动观察与补偿对账；启动和周期 reconciliation 负责漏事件，不由每次查询遍历全库。复用现有运行框架，索引更新是确定性工作，不创建模型整理任务。
4. 索引更新与业务写入有可观察顺序，返回索引代、待处理变化和错误概况。保存后读当前文件必须有效；索引短暂落后要明示。后台部分失败或文件盘不可用不能推断为全库删除。
5. 同一次 `read` 复用已经读取的 raw、revision 和解析元数据，避免 `_index` 与 `public` 重读文件。仍核对当前文件，不把缓存正文直接冒充当前证据。
6. 保留跨进程锁、prepared journal 恢复、现有身份/ownership/history。重建派生表不能删除整份 `index.sqlite3`。并发写、重启、迁移中断和恢复的状态不能依赖单个进程内缓存。
7. 给后续 compact 提供只读派生元数据入口。旧 full 返回仍可继续提供其原有字段；导航卡与完整证据/来源检查在实现层分开。

### 验收

- 未变化库的真实 search/read 入口不执行全库 `os.walk` 或逐文档 identity/history backfill；不能只测复用 Store 的内部函数。
- 单篇保存只解析受影响文件，不读取未改的正文、PDF 或视频；read 同一文档不重复打开完整字节。
- 外部编辑、改名、删除、新增、进程重启和漏观察事件可恢复到正确索引；盘暂时不可用不清空身份账本。
- 相同语料的现有中文、混合词、跨章节和 revision 用例不回归；实际时间与 IO 分开报告，不能把本轮旧实现探针当改造后成绩。
- 多账户绑定、配置失效与并发读取无串库；当前任务授权仍在请求边界检查。

### 依赖与完成条件

先记录旧实现同一批查询及请求级成本，再实现。迁移、回滚说明、兼容 API 和对应测试一起交付。完成后将“热路径已修复”标为实现事实；未测用户 WSL 时不承诺固定时延。

## 3. V2-02：OKF 内容角色、身份与来源

### 问题

当前 YAML 只消费 `tags/aliases`；标题、description、文档类型、原生 sources 与 uid 没有贯通。模板示例会进入研究候选，`.md.txt` 原稿快照也会作为 TXT 再次召回。`generated` 不能区分“保留原文而加了摘要”与“整篇 AI 综合”。[R1][R3]

### 代码定位

`store.py::_index/indexed_metadata/public/identity`；`search_index.py::sections/index/select`；`extract.py`；`links.py`；`sections.py`；`curation.py::organize/publish/blocks/restructure`。可新增 format/metadata 模块，但不要求重写整个 WikiStore。

### 完整范围

1. 建立一个归一化入口，支持 OKF v0.2/article-v1、旧 `octopus-wiki/octopus-section` 格式和无 YAML 的普通笔记。明确字段优先级与冲突提示，原生 `type` 是开放值；项目角色映射未知时保留原值，不强行猜成 Reference。
2. 将 `title/description/type/sources/generated/status/uid/aliases` 映射到索引可用字段。旧 YAML 和未知字段完整保留；读取旧文不要求先批量改写。
3. 分开文档角色和内容块角色，至少能识别 Reference/个人材料/综合页/Concept/主题导航/Template/source snapshot，以及原文、Key note、相关链接、整理说明和代码示例。不要仅凭 `generated`、第一人称或文件夹名称推断作者、主体、可靠性。Concept/Topic 的识别能力不要求为每份来源新建页面；是否值得沉淀按知识积累与迁移计划判断。
4. 保持原提取文本的字符坐标。用于索引的规范化文本、去除 metadata 的可搜索正文与给人显示的原文可不同，但必须有显式范围映射，不能用清洗后 offset 直接切原文件。
5. `uid` 作为可移植别名绑定现有 `id`。重复 uid 返回冲突诊断和不含歧义的现有身份；不覆盖旧绑定、不改变权限、不自动合并正文。移动/改名保持已有身份，恢复已删除路径时遵循现有 tombstone 规则。
6. 解析 OKF `sources[].id/resource` 与脚注；本地来源可解析到当前内部 id/path，外部来源保持其资源标识；缺失、未读取、版本未知保留明确状态。来源与普通关联链接分别存储，不把链接都当 `derived_from`。
7. 增量维护普通链接与实际出处、已知直接总结/表示关系。普通相关边不传播证据变化，明确重复不占满候选，不把共享参考文献当同一文章。完整来源根、深层闭包和自动证据计数按 Q57 移入 §10 条件项。
8. 原位规范文件作为正式入库文章，不逐篇创建 `.source.md.txt`；仅对确实存在或明确需要的来源副本登记并退出重复候选，同时保留按来源读取。当前 sources 可空，缺失不阻塞模板化或检索，不能根据 `.txt` 或目录名 references 一刀切。模板依然可被“找模板”的 lookup 找到，默认不成为研究证据。
9. 统一 organize/publish/restructure 的 OKF 与元数据适配，保持现有工具兼容；新工具复用同一 frontmatter_patch 白名单与 tags/aliases 输入，省略不清空未知字段或原有出处。uid 保持、实际执行记录由服务端维护；公开 sources 可空，实际任务输入及阅读记录仍真实，不伪造来源以满足旧 publish 非空检查。
10. 按[定点维护](local-shared-wiki-maintenance-v2.md)新增 revise、原始 blocks 定位/选择读取、targeted_revision capability、任务与阅读覆盖校验。纠错和插入由原始位置及 expected_text 确定，不用提取 offset 或整篇 content 替换，不按 AI／人工归属拒绝操作。模板化保持当前 pika_vault 原措辞，正常维护保护思想、论证和风格。
11. 不走旧 original_text 清理重建路线，保留未参与操作的正文、Key note、标签、关联和来源；涉及旧管理记录时更新或撤销过期定位，保留历史。实际修正、插入、结构调整和锚点影响有准确差异，普通保存不自动维护。
12. 新建、模板化、追加、纠错、confirm_sources/no_change 全部接入持久产物回执、worker 完成判定与崩溃恢复。publish/restructure/revise 不能仅落文件后误报失败；无文字变化不伪造文本 change，索引恢复不重复生成，取消和恢复沿用真实版本与提交语义。

### 验收

- 三篇已接受样例和模板能够被正确识别；description 可作候选预览，Key note 不与原文/整理说明混为同一角色。
- 新旧格式同库检索；模板、源快照不会挤掉 Vidu 正文；明确查模板时仍可找到。
- 重复 uid、同名不同文、改名、删除后路径复用都有确定行为；现有 id/history/ownership 不丢失。
- A、B 不因被同一综合引用而合并；明确同材料的重复摘要不放大支持或挤掉另一方向，出处缺失不编造，回查与已知直接依赖提醒可用。
- 原稿的公式、引文、链接、媒体引用和用户编辑区保持；结构检查不自动写入 `verified`。
- 新旧文章及人工编辑后的内容均可在明确范围内定点纠错、补充；仅读后再次变化才冲突，越范围或原字句不匹配拒绝。未涉及字节保持，重复维护不丢核心思想、理由、反例和风格表达；旧请求与管理历史兼容，不要求鉴别或认领作者。
- 旧专题本次新增的解释也可后续修订；重读相关来源确认仍适用时正文保持、对应复核记录更新，未检查依赖不被刷新。各类实际写动作及无文本变化的复核均有正确任务回执，重启后不重复执行已完成工作。

### 依赖与完成条件

依赖 V2-01 的迁移和增量机制。完成 parser、索引 schema migration、兼容读取、受控写入及文档说明；旧库大规模内容改写不属于本票。

## 4. V2-03：检索与证据 API

### 问题

当前同一种 search 响应承载候选、完整 metadata 和来源检查；没有精确对象与宽主题的不同选材策略，也没有跨文档读取的整体预算。主体、来源重叠和主题覆盖不能仅靠模型读更多结果补救。[R1][R4]

### 代码定位

`search_index.py::select`；`store.py::search/read/documents/graph`；`routers/wiki.py::SearchItem/SearchResult/search/read/app_wiki`；`mcp_servers/wiki.py`；生成的 OpenAPI/`web/src/api/contracts.ts`；`scripts/evaluate-wiki-search.py`。

### 完整范围

1. `search` 支持统一 intent：`lookup/question/overview/compare`。完整路径、现有 id、无歧义 uid 别名、真实题名和可靠 aliases 走精确查找；目录或命名对象在用户/上下文明确时约束候选，歧义时返回有限选择。
2. 保留现有词法、中文、跨章节覆盖与文档去重。overview 对当前主题候选做资料角色、主题分面与来源重合选择；已有导航提出候选时，仍检索当前文档索引补充未收录资料。compare 分别取得各对象证据，缺项明示。有独立用途的当前 Concept/综合页可复用已有解释或比较；重要事实仍按其来源与当前版本查证，不简单把所有生成页降权到不可用。
3. 新增 `view=compact`，旧省略 view 的调用仍按 full 兼容。compact 字段以检索协议为准：保留身份、当前路径、标题、description、角色、revision、命中位置与必要状态；完整 sources、诊断和历史按需取。检索候选不逐篇读取原文件或核验所有来源。
4. 新增批量 `read_many`，MCP 工具名为 `read_many`，REST 为 `POST /api/wiki/read-many`，app 路径为 `POST wiki/read-many`；items 使用 `{doc,start,length,expected_revision}`，整体 `budget_chars`。按请求顺序稳定返回；同一文档重叠范围可以合并读取，但保持 item 对应关系和真实原文范围。每项独立携带冲突、失踪、截断与 continuation，不因一项失败抹掉其他成功结果。
5. read_many 与现有 read 使用相同的当前文件校验和读覆盖记录。代码层严格使用 Python 字符坐标；前端不能用 JavaScript 字符串长度替换。全文评价从 0 按返回位置读至结束，预算不足只表示下一批，不是完成全文。
6. 分页在同一索引代保持确定顺序；索引代变化、scope 不可用、部分抽取失败、空结果、预算截断和旧 revision 使用各自状态，不统一伪装为“没有知识”。详细状态以检索协议为唯一字段定义。
7. 所有新增参数和响应同时经过 REST、app 网关手工参数转发、MCP 和 OpenAPI。`app_wiki` 当前显式拆 query params，漏加 intent/view 会使 Sapphire 与 `/wiki` 产生无声差异；新增 read_many 还必须加入 app handler 与任务工具白名单。
8. 以 capability 宣告已实现版本；旧客户端和旧 prompt 继续使用旧参数。新增只读工具继承当前账户/会话任务范围，不能用 uid 或来源链接扩大授权。
9. 后续标签过滤遵循同一 search/documents 字段与派生元数据契约，接口定义参见检索协议；实际标签面板及 tags facets 扩展随 V2-05 交付。多 app client policy 随 V2-09 完整接入。未完成时不在核心 capability 中提前宣告，V2-06 不等待这两项 UI 扩展。

### 验收

- 同一 query、intent、scope、view 从 REST、MCP、当前已绑定 app 网关得到等价候选与位置；旧 full 客户端继续工作。这个网关契约检查不要求先完成 Sapphire 新 UI。
- Me 查询不将转载作者当用户；Vidu lookup 唯一命中不再注入模板/导航作为其他证据；Vidu 局部问题读到具体条件。
- overview 能覆盖真实存在的不同资料方向；新增而未进入旧导航的文章仍可发现；同源总结增多不挤掉独立方向。
- read_many 覆盖混合成功/changed/not_found、中文与 emoji、重叠范围、预算刚好耗尽、长文续读和版本变化；单文档 read 保留 HTTP 409，批量请求按检索协议返回逐项状态。引用可由身份、revision 与原文位置机械回查；跨应用阅读 UI 在 V2-09 验证。
- 记录工具轮次、返回字符/实际 token、当前阅读量和请求延迟。在证据质量相同时评测 compact 节省，不能仅缩短最终答案。

### 依赖与完成条件

依赖 V2-01、V2-02。代码、schema、兼容开关、工具说明和少量关键请求链路一起完成；新增 prompt 在能力上线前不启用。

## 5. V2-04：prompt 与 Weber persona 协同

### 问题

已有 task guides 分层是正确基础，但完整 query 与写作规则会一起进入 curator worker；工具描述和工作流重复较多。新检索流程只有在 prompt、参数与工具实际能力一致时才有效。实际 Weber persona 来自 agent 配置，单改 `prompts/curator.md` 不会自动更新。[R5]

### 代码定位

`wiki/workflow.py::instructions`；`wiki/prompts/{entry,query,maintenance,writing,curator}.md`；`mcp_servers/wiki.py::TaskWiki.list_tools`；`harness/assembly.py::compose_system_prompt`；`wiki/jobs.py::WikiWorker.process`；实际 agent 配置的非敏感 persona 字段。

### 完整范围

1. 按 prompt 文档替换查询、提交、维护与写作稿。system persona 负责 Weber 的独立思考、作者归属与表达；任务指南负责本次选材与停止；schema 负责真实参数；代码继续负责权限与一致性。
2. 用实际 capabilities 选择旧/新工作流。新检索 API 可用时才要求 intent、compact、read_many；回退时使用现有 search/read，不假装新参数已经生效。
3. 个人、精确、局部、宽泛和对比查询有不同起步路径；证据不足才扩展。不得把固定候选数或字符窗口当总阅读上限，也不能把链接存在当已读。
4. curator worker 只加载维护所需的相关性检索规则，不重复加载普通个人问答、完整报告评价等无关部分。提交入口只接受材料/目标、去重排队并报告真实状态，不在入口先做一遍整理。
5. 根据实际现有 Weber persona 合并新增原则，保留智者、读书与思考伙伴身份，以及用户已有定制。模型、reasoning、backend、凭据、私有 memory 与会话默认不动；若要研究不同推理配置，作为独立可比较实验。
6. 避免把当前库目录、全部摘要、个人事实或工具流水账放进常驻上下文。普通下一条消息继续无新 Wiki 授权；历史中已读内容不虚称已被抹除。
7. Octo、Vera 可直接查询共同 WikiService；不让每条用户问题固定经过 Weber 再经过 Vera。必要的大型复核沿现有委托，不建立第二个任务系统。
8. 将 writing 提炼为共享内部模块，明确写作、改写、Key note/综合产物任务复用；普通解释不加载。SmartReader 的未来写作入口、普通 Octopus 写作和 curator 可共享表达原则，各自仍使用已有任务和工具权限。生成 reader 草稿不创建 Wiki 写 grant；用户明确同范围修改不重复确认。
9. 维护 prompt 先判断原文件适配、已有解释更新与新知识页是否有独立用途。来源覆盖、矛盾与未决问题适量保留；不为每篇强制凑 Concept/Topic，不把日常对话自动永久化。
10. 新增 Key note/关联解释或定点修订文字时装配一份共享 writing，不按篇幅跳过。使用真实 revise/blocks capability，模板化保留正文措辞，维护保留思想与风格；未上线不调用不存在的更新，也不绕过人类保存入口。
11. 核对实际 `curator_agent_id` 绑定既有 Weber，维护 job 创建的 worker 会话使用该 agent 的真实 persona 与本次指南。现有 MD 的语义性 Key note 提取和 `/wiki` 维护都经此链路；Weber 自己接收维护请求时也先提交 job，不在普通会话绕过受控写入。

### 验收

- 采集实际装配文本和 MCP list_tools：纯阅读不加载写作规则或写工具；明确写作可复用模块而不扩大 Wiki grant；entry 无 curator 全指南；worker 只有授权动作和所需工作流。
- 新旧 capability 各跑一条真实调用链，参数不悬空；普通聊天和后续普通消息没有新增 Wiki 调用。
- 实际 Weber 对局部问题少读无关内容，对全篇评价从 0 完成；职业缺失和作者第一人称边界答对。
- 实际维护产物保留原文与条件，无强制凑标题/别名/关联；组织完成依据发布和索引回执。
- 对普通解释、明确草稿写作、明确 Wiki 维护各验证一条实际触发链；不以“加载写作指南”替代材料可达、目标确定和实际保存回执。
- 核对真实 worker 会话的 Weber agent 身份、实际装配规则和可用工具；生成 Key note 与既有摘要复用均有阅读及产物记录，不能由通用 CLI 探针或手工成稿替代。
- 对比 prompt 字符、实际输入 token 与任务质量；减少重复不损害必要判断。

### 依赖与完成条件

依赖 V2-02、V2-03 的稳定真实合约。模板稿可以先准备；实际 persona 变更用可审阅差异和版本记录，不覆盖其他 agent 配置。部署状态如实另记。

## 6. V2-05：后续 Sapphire 共享搜索、标签与库浏览

### 问题

附件中的 Sapphire 有独立子串搜索和独立链接解析，与框架索引、身份及排序不一致。用户还希望按标签筛选资料。Sapphire 专注人的库浏览、编辑与文件维护；阅读讨论、引用直达和写作进入 SmartReader。本票按用户要求列为后续规划，不阻塞核心和旧资料首批适配。

### 代码定位

实际 app 的 `server.py` 搜索与保存路由、`note_links.py`、`links.js`、`markdown.js` 及前端搜索/列表/编辑组件；框架 `routers/wiki.py::app_wiki`、`wiki/store.py`、派生 tag 索引与 `wiki/app_bridge.py`。附件 app 源码是审阅依据，开发时核对真实绑定与应用代码工作目录，不能编辑 Git 不包含的假想路径。[R6]

### 完整范围

1. 主库搜索使用共同 WikiService，显式传 intent/view/范围。保留人工浏览入口，替换子串排名；不在 app 新建 FTS、向量库或 LLM runner。
2. 为 search/documents 增加 `tags: string[]` 与 `tag_match: any|all`（默认 all）；支持只有标签的 documents 浏览。实现 `GET /api/wiki/tags` 与 app `GET wiki/tags`，提供当前 scope/查询/角色约束下按文档计数的标签列表。服务端先过滤再排名/分页，不能仅在当前一页对 tags 做前端筛选。
3. 按检索协议统一 Unicode/大小写比较、空标签条件、层级非自动展开、facet 计数和分页语义；索引存派生 doc↔tag 关系，不每次扫描 YAML。端点、schema、MCP 参数、app 手工转发和 capability 一起实现；当前无此能力时 UI 不假装过滤已生效。
4. 结果显示角色、简短 description、标签和命中小节及必要状态。可在 Sapphire 普通预览/编辑，也可将真实身份/revision 与位置交给 SmartReader。SmartReader 尚不可用时维持现有预览，不显示不可执行的讨论按钮。
5. 框架与 app 的链接目标和块/标题锚点共享解析语义；同名、移动、块引用和失效链接一致。不在 Sapphire 复制整套聊天、选区问答、写作编辑器或维护 runner。
6. 普通保存只写一次，再登记对应索引事件；保留预期 revision、冲突、Git、附件和回收站归属。已有明确维护入口沿同一 durable job 提交，不先保存新文再重复导入。“刚创建文件整理”使用真实创建 change_id；旧响应没有时，从匹配当前 revision 的 history 或新桥接回执取得，不由浏览器编造。
7. 绑定主库与 app 其他 vault 分开。现有 `wiki/info` 不含 root，owner app 可使用 `wiki/config` 的真实 root 与 workspacePath 校验；新 consumer 使用 V2-09 的受限绑定信息。不把另一 vault 的筛选/搜索静默转到主库，不改 owner app_id 来接新 app。

### 验收

- 相同 query/范围/标签条件在 app 与服务接口使用相同候选；精确打开不启动模型。
- tags any/all、目录交集、无标签、空结果、标签计数、翻页及更新后刷新符合共同协议；被默认排除的模板/源快照不污染普通标签统计。
- 同名标题、rename、失效目标与块锚点可用或明确提示；进入 SmartReader 时传真实服务 id/revision，不拿浏览器 UUID 当主库身份。
- 一次保存产生一次文本写入与相应索引更新，不产生隐式维护；一次入库产生一个有效 job。
- Git、图片/PDF 预览、回收站和其他 vault 现有行为无回归；新版接口失败不退成不告知的全库子串结果。

### 依赖与完成条件

依赖 V2-03；跨 app 深读连接在 V2-09 可用后启用，其余库浏览/标签能力不要求先完成 SmartReader。按 Sapphire 方案完成选定功能的真实浏览器验收，更新 Iterate 合约。应用代码交付、安装与线上可见分别记录。V2-06 / V2-08 不依赖本票完成。

## 7. V2-06：核心端到端与少量入库试点

### 问题

当前仓库旧交接仍有实际 Weber 查询与维护补验、真实快照退役、旧库整理未完状态。附件缺少线上 Wiki state；静态源码和 40 MD 探针不能证明真实绑定、agent 行为或维护效果。[R7] 在批量整理旧材料前，先用少量真实材料完成“入库 → 读懂/引用 → 日常复用 → 必要纠正”的闭环；应用新 UI 不成为等待条件。

### 代码与材料定位

`tests/test_wiki_*`、`web/e2e/wiki-composer.spec.ts`、`scripts/evaluate-wiki-search.py`、当前 Octopus `/wiki` / Weber 入口；`docs/plans/local-shared-wiki-implementation-handoff.md`。使用已可用 REST/MCP/当前绑定 app gateway 检查契约，不要求新标签面板或 SmartReader 功能。语料、失败样例和配置记录留在适当私有位置，不将个人正文提交到公开仓库。

### 完整范围

1. Vera 按评估文档固定当前语料、gold 文档/章节、可回答性和禁止错误断言；先在隔离副本测试模板噪声、重复 uid、20 份同源摘要、导航遗漏、来源删除、正文版本变化及目录不可用。
2. 对相同问题和配置比较旧实现与各完整变更集，分开测服务、IO、工具输出和真实 Codex 的 token/首响应/完整回答时间。不能将跳过、脚本正常退出或 schema 通过算成行为通过。
3. 在实际当前绑定与已配置 Codex 下，至少覆盖 Me 正向/缺失字段、Vidu 精确局部/全篇、数字人宽主题、一次版本变化重读、一次少量材料维护与随后检索。引用通过服务身份、当前正文和位置核查；新 SmartReader 引用 UI 留在 V2-09。
4. 选择已获准的少量真实资料作为试点，优先包含一篇保留原文的旧文章适配和一份有真实来源的待入库材料；数量与具体文件以当轮任务范围为准。先核对现有文件与接受模板，旧文不因格式缺少字段而全量重写。
5. 检查试点是否形成可用 description/Key note、可靠名称/标签、可追溯来源与必要关联；确有重复使用价值时才形成少量 Concept/Topic 或补充已有知识页。没有独立价值的新页面不计为完成量。
   Key note 的提取、已有摘要是否适用的判断及明确维护由实际绑定的 Weber worker 完成。至少一篇现有 MD 走真实 `/wiki` 维护提交→Weber 全文阅读→模板化写回→当前索引→后续查询的完整链；记录实际 agent/session、job 与产物身份。脚本生成摘要、开发 agent 代写和仅成功调用 Codex CLI 均不代替此项。
6. 使用真实产物回答后续问题，核对主张、条件、作者归属、全文覆盖、原文保全与索引状态。引入一次材料更新或纠正，确认旧解释不会继续作为当前结论。结构 lint、agent 自述和合成用户档案不能替代这些检查。
7. 记录实际运行、问题与产物 diff，并形成 V2-08 可直接使用的试点结论：哪些适配规则稳定、哪些需要人工判断、哪些目录/材料暂不能自动处理。大批旧资料迁移不在试点里一次完成。
   定点维护用隔离用例验证未涉及字节与依赖保持、原始/提取位置、人工编辑后的可维护性、并发冲突、原思想与风格长期保全及越范围拒绝。按 Q58，另至少用一个真实研究问题验证建立知识页→后续不同问法复用→明确纠正；合成 Concept 不替代真实知识价值验收，不要求每篇原文都新增 Concept。
8. 合入门禁以当前 `AGENTS.md`、脚本和本地约定为准。审阅版本 `scripts/check.sh` 包括 ruff、mypy、hermetic pytest、eslint、frontend unit、`tsc -b`、生成合约与 docs index；另按本地协议运行 pure-UI E2E 和可用 Codex real-CLI/browser 项。Claude provider 不可用时按当前 local Codex 约定单列，不算通过。实际命令重新核对，不沿用旧文测试数量，不无限重复无关实验。

### 验收

- 核心评估的必要契约通过，阻塞失败有修复与复验；后续 app UI 和条件语义单独记录，不因未开始这些票判定核心失败。相同证据质量下才能声明更省 token。
- 实际 Codex 的工具参数、当前 revision、引用范围及回答可回查；完整评价覆盖字符 0 至结尾，缺失职业明确说明。
- 试点维护实际生成/更新受保护的产物并完成索引；后续问题能复用已组织知识，来源/条件与纠正可信；普通改动没有变成再次总结，持续冲突有明确结果而不无限重试。
- 新旧文章、旧 full、新 compact、既有 app gateway 与 MCP 同时可用；原文、用户编辑区与无关配置不受损。未实现新 UI 如实保持 proposed。
- 最终交接分别列出 implemented、validated、deployed、未验证环境和仍待用户范围的旧文处理。

### 依赖与完成条件

**仅依赖 V2-01～04。** 先在隔离数据验收，再在当前有效授权范围完成真实试点；试点结论作为 V2-08 的输入。V2-05 标签 UI 和 V2-09 阅读新功能不阻塞本票。旧快照退役/GC 是有独立前提的历史工作，不作为本票隐式副作用。

## 8. V2-07：条件触发的语义适配器

### 问题与触发条件

解析、角色、scope、可靠名称及词法召回完成后，若**本地确有相关正文**，仍存在数个可复现的同义表达/跨语言漏召回，才进入这张票。先收集失败问法与未用于调参的改写问法。文件数量达到某个阈值、看到新向量工具，均不独立构成触发条件。

### 代码定位

V2-03 的统一检索边界、新 semantic adapter、派生索引版本/状态、评估脚本；不更改正文、现有 id 权威或当前证据读取。QMD 事实与版本限制见[验收文档 §9](local-shared-wiki-evaluation-v2.md#9-qmd-的可选采用边界与已核实接口)。

### 完整范围

1. 比较既有 lexical、lexical+semantic、必要时再加有限 rerank，保留词法基线和独立关闭开关。lookup、路径、Me 简短事实不默认增加模型分支。
2. 以可替换 adapter 封装版本、输入文档范围、模型、维度、chunk/上下文格式和 fingerprint。模型或输入变化只重建受影响派生向量，不改身份账本。
3. 若用 QMD，固定并验证实际安装版本；CLI/MCP/REST/SDK 参数不能混用，main 未发布功能不能宣称为 release 能力。避免每个请求启动一次进程或下载模型。
4. 确定一个融合与重排责任方，避免重复 query expansion、两次 RRF 或两次 rerank。来源归并、业务范围、角色、当前文件读取和引用仍由 WikiService 负责。
5. 语义材料以有上下文的正文片段为主；不为每个 chunk 默认生成长期 AI 小摘要。metadata-only 更新只在语义输入实际变化时重算向量。
6. 后端失败、模型缺失、索引落后或取消时仍可使用精确/词法；明确降级原因，不把故障当作无相关知识。

### 验收

- 实际漏例得到补回，保留改写问法也有收益；既有 exact/CJK/scope/来源/当前证据测试不退化。
- 报告候选召回、EvidenceHit、主题覆盖、token 与冷热耗时的净变化；仅更高相似度分数不构成收益。
- Vidu TTFF 在语义服务关闭时仍可用；无默认大模型下载、无跨 scope 外泄、无无限扩写或重排。
- 扩容与同源摘要污染下，向量结果不能挤掉正文和独立来源；必要时保留词法为最终采用方案。

### 依赖与完成条件

依赖 V2-06 或 V2-08 使用中形成的真实缺口证据，不依赖 Sapphire 或 SmartReader。未触发时标记 `deferred: no demonstrated semantic gap`，不影响核心、试点与旧资料适配交付。触发后按本票完整实现和验收，收益不足则关闭并记录判断。

## 9. V2-08：已有未规范文章与库外资料分批适配

### 问题

旧库包含无 YAML、只有少量 tags、已有人工内容、HTML/Markdown 表示和过时导航；库外阅读材料也未必值得完整入库。把全部文件套模板重写会增加成本，破坏原文与稳定路径，并制造更多重复总结。V2-06 的少量真实闭环通过后，才有足够依据推广适配规则。

### 代码与材料定位

V2-02 的内容解析/写入适配，`wiki/jobs.py`、`curation.py`、`history.py`、`links.py`、增量索引；用户明确范围内的现有 Library 文件与待入库材料。具体队列、决策与记录格式按[迁移计划](local-shared-wiki-migration-v2.md)；新知识页价值按[知识积累](local-shared-wiki-knowledge-v2.md)。本票不假设 SmartReader capture 或新 UI 已可用。

### 完整范围

1. 当前用户确认的库内范围为 `Library/` 下所有 Markdown，递归盘点当前文件、revision、表示形式、现有摘要/来源、重复线索与用户编辑区。pika_vault 优先，后续覆盖 Me、Research、课程笔记、导航、模板及其余目录，每批明确到文件。区分已有 Vault 文档、另行获准的库外实际材料、原稿快照、模板/导航与无关 app/skill 资源；不把整个附件当待改文章集。
2. 将资料分成“不需改”“只补元数据/索引”“补必要 Key note/来源与关联”“保留独立表示并关联”“需要进一步判断/材料不足”。优先高频、用户近期要复用的文档，小批推进；不按文档数或生成字数作为目标。
3. 已有文章优先原位兼容整理，保留服务 id、原路径、原文、公式、图片/媒体、引用及人工小修。已有好的摘要直接复用；不能因为它不出自当前 Weber 就再生成一份。普通保存不会自动触发本票。
4. 先用稳定身份、来源 URL/版本、内容 fingerprint、representation_of/derived_from 等真实线索识别重复。同名或标签相同不足以合并；原稿与不同格式表示保留可追溯关系。去重不等于自动删除文件，快照退役/GC 继续走独立前提。
5. 库外材料先取得可保留的实际正文与来源，再在已授权目标范围提交一次 durable maintain。只有网页链接而未取得原文时明确状态；不用模型摘要填充一个声称保留原文的 Reference。已存在同材料时适配现有文件或明确关联，不二次导入。
6. 每批只连接少量确有阅读价值的相关笔记。长期 Concept/Topic/比较页须回答一个明确可复用问题，优先更新已有页面；本地材料只有一篇且无法形成独立解释时可只留下 Reference。新页保留来源、未决问题与矛盾，重要事实不因生成而变成 verified。
7. 已知来源变化时提示直接依赖内容；只有本次范围内才定点维护，不递归重写相关页。思想、观点与风格按操作保全，不按作者或 ownership 分类；明确错误局部纠正，新证据与旧观点不同补说明而不替换立场。使用已验收 revise，来源实际复核仍适用可不改字；本次不更新的依赖不被刷新。
8. 每批记录输入 revision、动作、产物/current revision、真实 job/change 回执、索引状态与失败/待判断项。使用已获准范围连续完成工作，同一范围不重复索要确认；新增无关范围或确有无法决定的目标时处理具体歧义。
9. 对全量清单逐篇验证当前索引、身份/路径精确定位与当前内容读取，领域内容验证题名或正文检索，导航/模板用相应角色和明确范围验证；已符合要求的文件也需真实复核及 no_change 回执。再用真实后续问法验证适配的用途：个人事实仍只来自 Me；Vidu 精确定位继续稳定；宽主题覆盖独立方向；已形成的解释/比较可复用，来源与当前状态可回查。若新摘要只增加噪声，调整角色/更新方式，不继续放大数量。

### 验收

- 每个产物能说明为何保持不变、补元数据、整理原文件或形成独立知识页；不把“全部套了模板”当通过。
- 没有重复身份、重复导入或无来源的假原文；原文与人工修改保全，当前回执和索引实际完成。
- 首批高频查询与维护前相比至少保持准确，宽主题/知识复用的收益有具体例子；同源摘要没有挤掉独立来源。
- Concept/Topic 数量由实际用途决定；没有可复用问题就不创建。旧资料继续可读可检索，无需先全库迁移才能使用。
- 可中断、恢复、幂等继续；失败项有原因与下一步，未处理资料不伪装已规范。生产库不注入摘要污染测试材料。
- Octo 组织全量覆盖核对，Vera 从试点前参与验收并独立检查产物：Library 全量枚举与任务清单对应，每篇均有整理/复核及检索结果；pika_vault 完成只是优先批次完成，未处理、冲突或失败未解决时不计为全量交付。

### 依赖与完成条件

依赖 V2-06 的真实小样本闭环。按明确文件/资料范围完成本批适配与复用验证，再以实际差异调整下一批。V2-05 / V2-09 后续应用不阻塞；V2-07 只有真实语义缺口时加入。本轮设计稿不自动启动该迁移。

## 10. V2-09：后续 SmartReader 阅读、引用、讨论与按需入库

### 问题

SmartReader 已有材料×agent 会话、SSE、选区和摘录，但每轮重复附送最多 150,000 个 JS 字符串单位正文；它的浏览器 UUID 和 DOM UTF-16 offset 不是 Wiki 身份与 codepoint 证据位置。现有 Wiki gateway 又只支持单 owner app，不能通过替换 app_id 接入阅读器。用户要求把这部分放在后续应用规划。[R8]

### 代码定位

附件 SmartReader `app.js::sendMessage/contextKey/captureSelection/navigateToQuote/downloadDocument`、`lib/content.js::readingContext`、`lib/storage.js`、`lib/agents.js`、`reader_server.py`；框架 `registry.py::configure`、`routers/wiki.py::app_wiki`、`app_agent.py`、`models.py::AppAgentTurnRequest`、`wiki/tasks.py`、`wiki/jobs.py`、`app_bridge.py`。开发时核对真实 app 源码目录与安装版本，附件不作为执行脚本。

### 完整范围

1. **先实现多 client 契约。** 保留 `config.app_id` 作为 owner/provider 和现有 app_bridge 归属，新增 `client_apps:[{app_id,permissions:["read","submit_maintenance"]}]`；同账户、真实 app ID、迁移/缓存失效与 legacy 行为一起实现。不得换掉原 owner 或复用另一 app 的身份。
2. 同一 client policy 同时检查直接 `wiki/*`、`agent/chat`、`agent/ask` 和用户手写 `/wiki` 任务创建；context 中的材料文字不授权。read 只授只读；submit_maintenance 只提交获准持久任务并访问相应回执，不顺带开放 PUT、publish、organize 或其他用户/job。现有 MCP 请求普通 `/api/wiki/*`，WikiTask 还无 app 字段；从可信 session.app_id 绑定/解析 client 归属，使普通 REST 的 access/tasks.require、工具 list_tools 和每次调用都取任务与 client 权限交集。worker 写入仍来自真实 job assignment；撤销客户端不默删已接收的 durable job。
3. 区分浏览器-only、reader capture/draft、Wiki-bound 三种材料。主库文档绑定真实服务 id/vault identity/revision，另保留 reader UUID；同标题/URL 不自动绑定。未入库 IndexedDB 内容不能假定 Wiki read 可达。
4. 删除每轮 150k 正文重复注入。已绑定文章用引用与 read/read_many；浏览器材料用所选片段/必要上下文，完整分析则分批实际提供并记录覆盖。频繁按需精读确有必要时才新增受限 capture/read handle，明确账户、app、任务和 revision；没有该 capability 不调用不存在的工具，也不接受任意本地路径或外部文件地址。
5. 完成引用 deep link 和选区映射：Wiki 原文本 codepoint、DOM UTF-16、Markdown block/heading、PDF 页内 quote 各自命名；用 block mapping、exact quote+prefix/suffix 或后端重定位核验。旧 revision、改名、重复文本和歧义不靠首次 indexOf 冒充精准定位。
6. 保留现有 Weber/agent 阅读对话与会话恢复；普通解释不调用 writing。明确写改时复用共享模块，输出对话稿或有身份/revision 的 reader draft；草稿可显示、编辑、保存与恢复。生成/应用草稿不扩 Wiki 权限，app data 写文件也不自动成为主库文档。
7. 用户明确“入库”或明确补入指定 Wiki 文档时，沿既有 durable maintain，提交实际材料、来源、目标/范围与稳定 idempotency key。同范围授权不再确认；跨范围按真实歧义处理。不能先写主库文件再重复作为新文导入，不能每问自动沉淀 Synthesis。
8. 应用只在真实回执后显示保存/发布成功；SSE 断线、页面关闭和重试不造成重复任务。阅读流停止与 durable job 取消分开。引用打开结果使用真实 app ID 与服务身份，不在 URL 中附全文/凭据。
9. 更新 app README/契约和未来迭代提示，与 Sapphire 的“库浏览/标签/编辑 → 在 SmartReader 深读”衔接；不复制 Git、回收站、独立索引或 LLM runner。

### 验收

- 双 app 共用同一文档身份与内容；owner 不变，两个 Wiki 入口按相同权限工作。缺权、撤销、跨 app job、手写 /wiki、伪造 context 各有确定结果。
- 同一材料三轮实际对话不重复发送 150k；同证据质量下记录 payload/工具输出/模型 usage，完整阅读能实际覆盖全文。
- 已入库与未入库、中文/emoji/公式/重复句/PDF、旧版本/改名分别定位正确或如实显示歧义；浏览器 UUID 不流入 Wiki doc 参数。
- 普通解释、明确草稿写改和明确入库三种触发各正确；草稿保存没有误写 Vault，入库只产生一次有效 job，实际文档可读可引用。
- 既有材料导入、会话映射/恢复、选区摘录、PDF 等待、换材料保护、下载不回归；未实现的新能力不显示可点击的成功路径。

### 依赖与完成条件

依赖 V2-03 的共享证据与 capability、V2-04 的任务模块和既有 durable worker。client_apps 是本票第一个必须交付的共享变更；Sapphire 标签面板不构成前置条件。按用户后续启动范围完整交付应用行为与实机证据，当前只记 proposed；不阻塞 V2-06 / V2-08。

## 11. 给 Octo 的接手开发 prompt

下面是用户日后主动发起开发时可复制的完整提示。它现在只是交付文本，不触发本轮开发、配置变更、部署或旧库维护。

```text
请基于当前 Octopus 实现，按本轮范围落地 docs/plans/local-shared-wiki-v2.md 的 v2.1 修订及其配套 knowledge、content、retrieval、prompts、migration、evaluation 和 tickets；SmartReader / Sapphire 新功能是其中明确列出的后续应用票。

先读取当前 AGENTS.md 和实际分支/commit/工作区；保留用户未提交修改。本文设计基线是 453a7995bd6c54df33b0fc41dec09d370b4b40aa，但不要假设仓库仍在这个提交。先核对已有实现和旧 handoff，列出本轮真正需要补的差异，然后继续完成工作，不重做已验收能力。

目标是让个人知识可理解、可查证、可复用、可持续更新。把当前 SQLite/jieba 章节检索、OKF article-v1、现有受控维护和任务 prompt 连接起来，减少重复读取与重复总结。优先移除请求中 Store/History 重复构造迁移和全库 refresh，建立增量索引；再接内容角色与来源、intent/compact/read_many 和 prompt。QMD 只在真实评估证明语义缺口后实施，不是前置依赖。

执行顺序为 V2-01→02→03→04→06 少量真实试点→08 已获准材料小批适配；V2-05 Sapphire 与 V2-09 SmartReader 按后续应用范围启动，不阻塞 06/08。V2-07 按真实缺口触发，不依赖 app。每个已启动变更集完整包含迁移、兼容、真实参数转发、必要测试与文档。新增 API/capability 未上线前不启用要求它的 prompt。旧 search 省略 view 仍 full；新 intent 是 lookup/question/overview/compare；read_many 每项 doc/start/length/expected_revision，整体 budget_chars。

服务 id 是身份权威，uid 不授权或抢占。出处可空，按实际依据记录回查链接与已知直接关系，不要求完整来源根或自动证据计数。generated 记录事件不决定作者或编辑资格；思想、风格、公式、条件、媒体与未涉及字句保留。当前 pika_vault 原位模板化保持原措辞，不另存原稿；普通保存不维护，查询不写回。既有好摘要复用，Concept/Topic 须有独立用途，不强制造页或改写正文。

共享 writing module 按明确写作任务复用。阅读解释不加载写作规则；生成/应用 reader 草稿不扩大 Wiki 权限；用户明确同范围写改不重复确认。明确入库沿既有 durable maintain，凭真实 job/发布/索引回执判断完成，不另造 runner 或问答自动沉淀。

V2-02 新增独立 revise 定点维护和 blocks 原始定位，按本次任务、最新 revision、原字句及读取覆盖提交局部纠错或插入；不以人工编辑历史、AI 作者或旧 ownership 决定资格，不经过 original_text 生成区清理重建。更新受影响旧记录，保留未涉及内容与实际历史。confirm_sources/no_change 有真实回执；所有写动作进入 worker 产物和恢复。Vera 验人工编辑后的正常维护、读后并发冲突及多次维护后原思想和风格仍在，并用真实问题验证知识建立、不同问法复用与明确纠正。

启动后续应用票时，Sapphire 只做库浏览/标签/编辑和深读跳转，标签契约统一 tags:string[] + tag_match:any|all（默认 all），search/documents 在服务端先过滤再排名/分页，tags 列表用共同 API。SmartReader 承担引用、阅读对话和草稿/按需入库；首先实现 client_apps read/submit_maintenance，同时检查 wiki gateway 与 agent/chat/ask、手写 /wiki。保留原 app_id owner/provider，不抢走 app_bridge 归属。删除 150k per-turn 注入；浏览器 UUID/DOM UTF16 不等于 Wiki id/codepoint，未入库材料不能假定 Wiki read 可达。

Octo 负责代码和自测，Vera 对具体差异做独立复核。让当前 agent 直接查询；不要把普通用户问题强制变成三个 agent 接力。Weber persona 合并只改本任务相关文字，保留其模型、reasoning、backend、凭据、私有 memory 和既有定制，除非本轮用户明确要求调整。

验证时使用同一语料与查询对照，覆盖 Me 缺失字段、Vidu 局部/全篇、宽主题、模板/同源摘要噪声、当前 revision、rename/delete、重复 uid、盘不可用、read_many 部分失败和已有知识页的复用/更正。06 先跑实际 Codex 与少量入库闭环，08 按获准范围推广；新标签 UI、SmartReader 双 app 权限/引用/草稿/维护在对应票启动时验收。记录服务 IO、工具轮次、输出字符/实际 token、真实 Codex 首响应与完整回答时间；不把漏读、缩短答案或昂贵工作挪到入库阶段当作整体优化，不以模型自述或脚本 exit 0 宣称行为通过。

执行合入门禁时以当前 AGENTS.md、实际脚本及本地有效约定为准。审阅版本 check.sh 包含 ruff/mypy/hermetic pytest/eslint/frontend unit/tsc -b/生成合约/docs index，另运行所需 pure-UI E2E 与可用 Codex real-CLI/browser；不可用 Claude 按用户本地约定单列。实际命令重新核对，先跑相关检查并在集成点完成必需门禁；不把 unavailable/skip 记为通过，不拿历史数量作目标。排除具体剩余风险后继续交付，避免无目的重复测试。

这段接手提示的默认交付是可审阅的实现、验证记录和准确文档。部署、生产 persona 修改、指定旧文整理、快照退役或 GC 的实际执行范围以当轮用户明确要求及有效授权为准；不能仅凭这份设计或旧计划文本自动扩大。本轮若已明确要求其中动作，完成其准备和验证后按范围执行，不重复索要已经给过的授权。

结束时列出实际改动、行为变化、测试与实机证据、兼容性、尚未验证的环境、是否部署及仍需具体范围的旧库动作。更新 Status 时区分 implemented、validated、deployed；任何未完成项写清原因，不用“已优化”代替证据。
```

## 12. 给 Vera 的独立验证 prompt

```text
请独立验证 Octo 对 local-shared-wiki v2.1 的本轮实现。先读取当前 commit/diff、AGENTS.md、总纲、tickets 和 evaluation；再读受影响的知识积累/内容/检索/prompt/迁移合约，应用票启动时读对应 Sapphire/SmartReader 文档。只将代码和可复现行为作为实现证据，不把旧 handoff 的计划、Octo 的口头总结或模型自身评价当作通过。

先确认正确性链路：真实主体与范围 → 精确/词法/主题候选 → 当前 revision 正文 → 实际引用 → 用户答案 → 有需要时更新已有知识。重点验证 Me 缺失字段、Vidu 的独立指标与完整阅读、数字人资料覆盖、作者观点不变成用户偏好、Template/source snapshot/AI 总结不制造证据共识。不同文章不因共享参考文献合并，明确重复不放大支持；不要求自动完整来源根。uid 冲突不能改写既有 id 或扩大权限。已形成的 Concept/比较页要有复用价值，重要来源更新后旧解释可纠正，不按新建页数量判通过。

核查性能是否来自减少工作：真实 API/MCP 路径不重复迁移或全库 stat，单篇保存只索引变化文件，compact 不现读每个候选，read_many 预算与字符坐标正确。同一问题和证据质量下比较工具轮次、工具输出 token、模型 usage 和冷热时间；不得把缩短答案或漏读必要段落当优化。

验证 API 贯穿 REST、app_wiki 手工转发、MCP、OpenAPI 和实际 prompt；旧 full 调用继续兼容，capability 不提前宣告。核心 V2-06 只依赖 01～04，以真实 Codex 和少量维护试点闭环；V2-08 再看获准旧资料/库外材料小批适配，不机械重写。V2-05 / V2-09 属后续规划，未开始时不成为核心验收失败。

应用票启动时，验证 Sapphire tags any/all、目录/角色交集、facet 计数与先过滤后分页，保持 Git/附件/回收站和其他 vault。SmartReader 验证 client_apps 同时覆盖直接 wiki、agent/chat/ask 与手写 /wiki；不改变 owner app_id，不让 read/submit 变成直接发布权。验证浏览器-only 与 Wiki-bound 材料可达、DOM/Wiki 坐标、连续三轮不重复 150k、旧引用重定位。普通解释、reader 草稿、明确入库触发不同且同范围写改不重复确认。

先用隔离副本做故障与摘要膨胀实验，不向生产库注入测试材料。真实 Codex/浏览器验证只在当轮范围和可用环境内运行；缺少凭据或 provider 不可用如实单列，不读出认证信息、不把 skip 当通过。完整门禁以当前项目脚本和有效本地约定为准，不无限扩大到与改动无关的实验。

对每项发现给出：严重程度、具体文件/函数、最小复现、观察行为、正确行为、修复建议与复验结果。提出修改时联系实际风险，避免只按个人风格要求重构。没有复现的怀疑标为待确认；机械测试通过不能代替作者归属/条件保真/主题覆盖检查。

最终给出可审阅的通过项、阻塞项、未验证项与证据位置，并说明是否支持合入/在当前授权范围部署。不要自动执行快照退役、GC 或旧库批量整理。
```

## 13. 与旧计划的状态边界

Q60 最终维护合同不按 AI／人工作者或人工修改历史判资格。完整来源根、深层传播和自动证据计数按 Q57 属总纲 §10 条件项，核心实现不得把它们重新放进放行门槛。

旧历史文档在2026-10-12记载 W01–W05 已实现/部署，W06 的实际补验、快照退役及 W07 旧库整理尚未完成。[R7] 这些不是当前状态；本套 v2.1 的新增合同和后续实际进度分别记录，旧快照仍有独立前提，不追认旧验收或把旧执行范围自动扩张。

开发中保持三份记录：旧计划的未完事项、v2 各票的真实状态、本次实际授权的执行范围。三者可以有重合，但不能互相替代。已实现的权限、原文保护、历史与幂等机制继续复用；新索引/格式方案替换的实现细节在相关文档标明。

最初设计交付止于 Markdown/ZIP；后续已授权开发、Octo 协调真实 Weber/Vera，以及当前 Library 全量整理，不能把当时的交付边界当今天的停止指令。v2.1 将应用新功能放在 V2-05 / V2-09 后续规划，并新增 V2-08 适配已有/库外资料；这不抹掉旧计划已实现内容，也不把设计当作已执行迁移。实际阶段见[当前进度](local-shared-wiki-progress-v2.md)，部署与配置以当轮核对为准，来源/导航修复与旧库整理按明确文件范围推进。

### 来源与证据

- [R1] [WikiStore](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/store.py)、[registry](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/registry.py)。
- [R2] [WikiHistory](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/history.py)、[app bridge](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/app_bridge.py)。
- [R3] [当前整理实现](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/curation.py)、[OKF v0.2 固定规范](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/ad30107c31c06aec8a7d5636e0d1058118604e6f/SPEC.md)。新字段解释以本套内容合约为准。
- [R4] [章节检索](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/search_index.py)、[API](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/routers/wiki.py)、[MCP](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/mcp_servers/wiki.py)。
- [R5] [workflow](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/workflow.py)、[harness assembly](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/harness/assembly.py)。
- [R6] 附件 `.octopus.zip` 中 Sapphire 的源码与非敏感配置；具体文件事实和接口范围见本套 Sapphire 文档。附件本身未作为代码部署。
- [R7] [旧实施交接](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/docs/plans/local-shared-wiki-implementation-handoff.md)、[旧规格](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/docs/plans/local-shared-wiki-spec.md)。历史状态仅按其记载列出。
- [R8] 附件 SmartReader 非敏感源码；具体文件、当前能力及多 app 边界见本套 [SmartReader 计划](local-shared-wiki-smartreader-v2.md)。框架对应：[app_agent.py](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/app_agent.py)、[AppAgentTurnRequest](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/models.py)。
- 本地合入门禁事实：[AGENTS.md](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/AGENTS.md)、[scripts/check.sh](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/scripts/check.sh)、[web/package.json](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/web/package.json)。本轮仅审阅脚本，没有运行完整门禁。

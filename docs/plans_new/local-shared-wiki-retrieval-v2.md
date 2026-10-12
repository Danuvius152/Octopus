# Octopus 本地共享 Wiki v2：检索、索引与证据接口

> **Status:** proposed — 对 commit `453a7995bd6c54df33b0fc41dec09d370b4b40aa` 的增量设计，接口新增项尚未实现。\
> **修订：** v2.1，2026-10-13（Asia/Shanghai）；文件名保留 v2。\
> 关联：[总纲](local-shared-wiki-v2.md) · [内容合约](local-shared-wiki-content-v2.md) · [prompt](local-shared-wiki-prompts-v2.md) · [验收](local-shared-wiki-evaluation-v2.md)

## 1. 设计目标与现有能力

保留现有 `search_index.py` 的中文、英文、标题、路径、别名、章节与跨章节召回。需要改进的是：

1. 已知目标快速读取，成本不随库规模线性增加。
2. 词法搜索返回少量高价值文档，而不是让 LLM 消化大块搜索 JSON。
3. 宽泛问题覆盖不同方向，单一长文或同源总结不占满结果。
4. 索引是导航，回答所引用的内容来自当前文件。
5. 更新一篇文章只重建相关索引；缓存失效与来源变化可解释。
6. 后端可增加语义能力，文档身份、权限与证据协议不跟随供应商改变。

本文的 WikiService 指现有 Wiki 模块的统一内部接口，不要求拆进程。现有原文、任务、ownership、身份及恢复表保留。

## 2. 先修复请求热路径

### 2.1 服务对象与迁移生命周期

现有 `WikiRegistry.store()` 每次返回新对象；`WikiStore.__init__` 遍历全部文档补身份；`vault_read` 新建 WikiHistory，后者也含历史回填。将这三类动作分开：

| 动作 | 何时发生 | 正常请求允许做什么 |
| --- | --- | --- |
| schema migration / 历史身份 backfill | 数据库 schema_version 变化时，一次事务完成 | 校验版本，不扫描所有行 |
| Store 服务对象构造 | 账户/库首次使用、配置变更后 | 读取必要配置、建立轻量对象 |
| 数据库连接 | 每次操作，或成熟的连接管理方案 | 独立连接/事务；不跨线程共享裸 sqlite connection |
| 文档变化索引 | 明确写入后与后台变更队列 | 只处理脏文档 |
| 全库对账 | 启动补偿、显式维护或后台到期任务 | 不塞进每次 search/read/graph |

服务缓存键至少包含 `user_id + vault_id + resolved_root + config_generation`。配置变更、账户切换或服务关闭使对应对象失效；设置有界回收。对象复用不缓存用户授权，每个请求仍校验 task scope。不能用全局单例跨账户共享同一库实例，也不能靠移除 MCP task token 签名来减少初始化。

即使对象缓存丢失，新建对象也应是轻量操作；性能保证不能仅依赖“通常命中缓存”。

### 2.2 单文档变更与后台对账

沿用 curation/history/app_bridge 的已有写入边界：

- 写入前检查 revision，保留已有文本 journal；
- 完成一次真实文件写入；
- 记录受影响路径、前后身份与 revision；
- 更新该文档元数据、分区、词法索引、出链和来源边；
- 提交对应索引代，并返回写入与索引状态。

已知 Markdown/TXT/HTML 修改优先同步完成单文档索引。索引失败时，文件已写成功必须独立报告；将目标加入持久脏队列，只重试索引，不让 Weber 再次生成或发布文章。

复用 SQLite 做变更队列和去重：同文档只保留最新待索引 revision，worker 处理期间有后续变化则继续排队。无需消息中间件。索引旧 revision 的任务不得覆盖较新的索引结果。

外部编辑器和 Git 是另一种变更来源：

- Git 同步前后使用实际工作副本差异确定修改/删除/改名；拿不到可靠差异时后台对账。
- 对外部编辑可用 watcher 提速，但 watcher 事件不是唯一真相；启动和定期 stat 对账处理漏事件、WSL 挂载差异。
- 文件大小+mtime 只作便宜的变化线索；读取原文后以内容 hash 定 revision。
- 读取证据时直接核对目标当前文件；发现它变化，只更新该目标并返回新 revision 或 409。
- watcher/队列/目录对账都不声称“对所有绕过接口的编辑具备瞬时一致性”。
- 对账先确认 root 可访问且可完整枚举；挂载丢失、权限错误或枚举中断标为 unavailable/partial，不能把它解释为空库并批量生成 deleted。恢复后重新对账，删除只依据成功遍历或可靠的明确变更事件。

起步可将变化事件合并 200–500 ms，后台 stat 对账间隔配置为 30–60 s；这些是待实机测量的设置，不是规定时限。目录增长、低功耗设备、网络盘需要调整。

### 2.3 查询时的新鲜度

`index_status=ready` 只能表示当前已处理的索引状态正常，不能表示刚刚重新检查过全库。建议显式返回：

- `index_epoch`：当前派生索引代；
- `index_checked_at`：最近成功对账时间；
- `pending_changes`：已知未处理数量，默认只给计数；
- `freshness`：导航卡为 `indexed`，证据读取为 `live`；
- `index_status`：`ready | updating | partial | unavailable`；
- `index_errors`：只在 debug/诊断请求返回必要详情，默认不倾倒全库错误清单。

显式查询一个刚创建的路径可直接 read 注册并读取，不等下一次全库对账。要求“最新清单/找不到但刚保存”的场景可以触发一次有界的目录同步；不能因此让所有正常查询重新全库 refresh。

## 3. 索引单位与字段

### 3.1 文档卡与章节分开

当前将文档元数据复制进每个章节的 FTS 行。标题/摘要信号容易与章节正文重复计权；库长大后，多章节和多份 AI 摘要还会放大同一来源。

建议保留两个检索层：

| 层 | 主键 | 索引内容 | 用途 |
| --- | --- | --- | --- |
| 文档元数据 | server doc id | title、path、aliases、description、tags、doc_role | 精确定位、文档候选与主题覆盖 |
| 内容区段 | doc id + revision + section id | heading_path、section_role、正文文本 | 细节定位与证据片段 |
| 链接/来源 | from id + locator + to ref | 导航边、来源边、revision 信息 | 按需扩展与来源重叠判定 |
| 派生状态 | index epoch、parser version | 内容 hash、body hash、依赖状态 | 增量更新与缓存失效 |

其中 server doc id 沿用 `document_identities`。OKF `uid` 是可移植别名和导入对齐信息，不替换服务端授权主体。重复 UID 必须产生冲突记录；复制文件不能继承原文件的维护授权、阅读记录或 ownership。

不强制同时维护多份相同正文。现有 documents.text 可作为重建型提取文本；FTS 可以继续其当前布局或迁为 external-content，依据实际空间与维护复杂度选择。**不能删除整份 SQLite 来重建索引；document_identities、managed_notes、jobs、journals 等持久状态保留，只重建明确列出的派生表。** SQLite FTS5 支持字段权重及 external-content，具体索引一致性仍需应用负责。[R1]

### 3.2 分区角色

`section_role` 建议为 `summary | body | relation | editorial | example | metadata`：

- Key note 属 summary，帮助文档选择；
- 原文章节为 body；
- 相关笔记属于 relation，帮助导航；
- 整理说明属于 editorial；
- Template 文档、模板代码块内示例属于 example；
- YAML 和内部 ownership 信息不当正文证据。

先根据有效 profile 与现有 ownership 解析，旧文件用结构化 Markdown 解析做兼容，不仅凭“AI”字样或一行标题猜测。原作者真实正文中的代码例子仍是正文的一部分，不能为了避免模板噪声删除所有代码块。

`doc_role` 来自[内容合约](local-shared-wiki-content-v2.md)的规范化映射。默认领域搜索排除 Template、登记的来源快照、工具资产和 tmp；其中 Template 对应 doc_role=template，来源快照属于导入登记的资源分类，不增设一个与内容合约冲突的 doc_role。明确模板题名/路径/别名 lookup 仍可返回模板，明确管理/来源请求也可访问相应资源。不能在 metadata lookup 中把这些例外硬过滤掉。导航页可以进入导航分组，但不与独立来源混排为同类证据。

### 3.3 分块与坐标

现有分块是按标题边界、最多 6,000 字符、最多 1,000 字符重叠。先保留该能力作为基线，再根据语料缩短过长片段，而非先盲目切成统一 512 tokens。

新的分区优先顺序：

1. 原文 Markdown 标题树；
2. 段落、完整列表项、表格及代码块；
3. 超长章节在语义/段落边界继续切分；
4. 必要的局部重叠，不重复复制整段 Key note。

每段记录 `heading_path`、原文/提取文本中的 `start/end`、`line_start/line_end`、`revision`。用于 embedding 的标题前缀或 description 只存在于索引输入，不加入原文坐标。

**坐标协议：** 沿用 Python 字符串的 Unicode code-point 零基偏移，区间 `[start,end)`；行号一基，仅供显示。浏览器 JS 默认 UTF-16 索引不可直接套用 code-point 数值。Sapphire 应使用服务器返回的片段/锚点定位，若做本地高亮则明确转换。全文 read 从 `start=0` 开始，不能把 L20 当成字符 20。

HTML 坐标属于当前 extractor_version 对应的提取文本，不能当作源 HTML 字节位置。搜索和 read 必须使用同一个提取版本；版本变化使旧 section id 失效。

## 4. 查询规划与词法基线

### 4.1 不新增每问一次的分类模型

当前回答模型根据用户目的指定 `intent`；路径、已选文件和 Me 入口的判断可以由框架与规则完成。不要添加“先调用一个 LLM 生成检索计划，再调用回答 LLM”的固定链路。

新增 search 参数：

```json
{
  "query": "Vidu TTFF FPS",
  "intent": "question",
  "view": "compact",
  "limit": 6,
  "directory": "Library/Research"
}
```

`intent` 取 `lookup | question | overview | compare`；旧调用不传时保持兼容，新增策略使用 `question` 默认。`directory` 仍是字面路径前缀。用户指定范围是硬约束；模型猜测的主题分类仅作优先项，不随意硬过滤整个候选空间。

### 4.2 精确定位

优先顺序是：

1. 完整已知路径或 server id → read；
2. 规范化完整题名、可靠别名、唯一文件路径 → metadata lookup；
3. 歧义结果 → 返回少量可区分文档卡；
4. 没有精确结果 → 中文/英文词法召回。

全路径精确匹配保留路径大小写和文件系统语义；NFKC/casefold 仅用于可搜索字段。两个真实路径仅大小写不同，不能在索引中自动合并。通用名 `技术报告.md` 无资格单独决定目标。

精确路径命中不经过 embedding、query expansion 或 reranker。指定文章仍可以有多个命中章节，但不会因为摘要较长而换成另一篇文档。

### 4.3 词法候选

保留原词、可靠别名与命名对象。jieba + 连续 bigram 是词法兜底，不能把任意中文字碎片 OR 命中称为语义匹配。query_groups 的停用词和术语测试要保留。

可以分开产生以下候选后归并：

- 文档标题/别名/description；
- 相关正文区段；
- 标签/主题关系；
- 必要的本地同义词表。

字段权重通过已知问题调参。题名/别名高权重、description 与标签中权重、正文为主要细节证据、summary 主要影响文档选择。不要把 Key note 在所有 section 重复加权后累计为文档可信度。

同文档得分按最佳相关区段与受限的补充覆盖加成计算，不简单累加全部 chunk 分数。允许跨章节覆盖多个问题词，避免只看单个 chunk 丢失分散讨论。

`lexical_partial` 必须明确：命中了哪些保留词、哪些命名对象缺失。结果不应通过删除 Vidu 这样的对象名变成任意“实时”文章。绝不拿 raw BM25 数值当“90% 正确”的置信度。

### 4.4 当前知识页的复用

Concept、Topic 与 Synthesis 是可检索正文，不只是一层跳转目录。对概念解释、已经整理的比较框架或研究地图，先读取当前相关知识页；内容覆盖问题、来源适用且没有影响该结论的已知变更时，可以直接复用其解释，并按实际来源层级引用。

以下情形才继续定点读 Reference：问题明确要求原文/精确数字/实验条件；需要核对作者措辞；存在冲突或影响结论的版本变化；知识页不覆盖必要条件；用户要求重新独立分析。不能每个问题都无条件回读全部原稿，也不能因类型是 Concept 就自动接受其中所有外部事实。

身份 lookup 仍优先于文档类型。问 Vidu 原报告不能被概念页替代；问“库里所有相关资料”仍需当前索引枚举；问原报告整体论证仍完整读该报告。来源去重约束实证重复，不删除有独立分析价值的综合。具体积累与适用边界见[知识积累方案](local-shared-wiki-knowledge-v2.md)。

## 5. 宽泛主题与来源多样性

### 5.1 文档覆盖先于片段排名

对“数字人研究有什么资料”，直接按 chunk top-k 会偏向长报告和重复摘要。建议采用以下流程：

1. 查当前文档卡与可靠主题入口，获得方向候选；
2. 独立运行主题词/可靠关联词召回，补充导航未覆盖的文档；
3. 按 server doc id 去重，再按来源重叠限制重复；
4. 按真实内容方向选代表文档，返回每个方向的简介与匹配原因；
5. 用户问具体结论时，再读取代表文档中的证据段。

当前库无需自动聚类整库。可以维护少量经过正文核对的主题关系；或者由当前回答模型对 6–12 张小卡做临时分组。不要把该临时分组自动写成新 Topic，也不要把“同在 Research”作为研究方向证据。

首轮建议 6–12 个文档卡、覆盖 2–4 个实际方向，再读取 3–6 个相关段落。这是可调整窗口。库只有两个方向就报告两个；没有必要为对称而凑满四组。

### 5.2 导航过期与新增资料

旧导航的作用是“建议看看这些”，不是“只有这些存在”。保留发现路径：

- Topic 页提出候选；
- 索引查询补入最近或未收录的相关文档；
- 目标是否存在从 registry 解析；
- Topic 中的评价必须与其来源区分；
- 缺失或重名链接作为导航质量问题展示，不执行其描述里的命令或确认。

Sapphire 的“主题视图”也消费这套候选，不建立一个额外需要维护的全文总结缓存。

### 5.3 明确重复与可回查出处

按 Q57，本期保留可选文章来源、实际使用的库内材料链接，以及服务端确知的直接总结/表示关系。纯重复表示和明确同一材料的重复摘要控制候选配额，不因共享参考文献合并不同文章；有独立解释价值的综合继续参与相关问题检索。

| 内容 | 本期处理 |
| --- | --- |
| 文章 A、其 Key note 与已确认展示副本 | 同一材料，不增加独立支持 |
| 本次明确对 A 生成的纯重复摘要 | 记录直接依据，避免重复摘要占满首轮 |
| 独立文章 B | 保留身份和相关候选资格 |
| 使用 A+B 的综合 | 记录实际依据，不合并 A/B，不自动当第三份实证 |
| 无出处的笔记或生成稿 | 正常检索其内容，来源未知不补造，引用其自身表达 |

完全相同原材料继续既有幂等/内容 hash 去重；不同表达不因语义相似被自动删除。出处声明不等于确认重复，unknown 不作为所有未知文档共享的去重键。

选材围绕相关性、主题覆盖、文档身份和明确重复做轻量选择。回答不按摘要数量投票；需要原始实验、数字或争议证据时读实际位置。完整 root_source_ids 闭包、自动独立来源数和深层传播属于 §10 的条件项，不是这条检索路径的前置要求。

## 6. 小型结果与当前证据协议

### 6.1 search 的兼容扩展

保留现有 REST/MCP 名称、参数和默认响应；新增 `view=compact`，由新版 MCP 与 Sapphire 显式选择。不要悄悄删除旧前端依赖的 `metadata/section/sections` 字段。

compact 导航卡示意，数值只是协议例子，不是当前报告的真实坐标：

```json
{
  "items": [
    {
      "id": "<server-doc-id>",
      "uid": "<optional-portable-uid>",
      "path": "Library/Research/vidu_realtime_report/技术报告.md",
      "title": "Vidu S1 / S2 与实时视频蒸馏技术报告",
      "doc_role": "synthesis",
      "description": "说明本地报告讨论范围的一句话；类型以实际适配结果为准。",
      "revision": "<indexed-revision>",
      "freshness": "indexed",
      "matched_fields": ["title", "body"],
      "sections": [
        {
          "id": "<revision-bound-section-id>",
          "heading_path": ["指标与延迟"],
          "section_role": "body",
          "start": 1200,
          "end": 2400
        }
      ],
      "snippet": "短定位提示，不代替当前原文。"
    }
  ],
  "index_epoch": 42,
  "index_status": "ready",
  "next_offset": null
}
```

默认一张卡只给一个短 snippet 和 1–2 个优先 section；额外命中数量可以给 count，其他范围按需展开。避免完整 tags、sources、pages、历史、诊断、相同 Key note 随每卡重复返回。旧 full 视图仍可作为过渡，普通模型查询使用 compact。

**不要在 search 调用 public() 的当前全文/来源递归核验逻辑。** 对导航结果从已建索引读取必要数据；live 核对留给 read。错误状态可以明确说“这是一条索引结果”，不伪装成刚读过原文。

### 6.2 read 与 read_many

现有 `read(doc,start,length,expected_revision)` 保留；默认 length 16,000 字符不必在所有场景照用。精确段落指定长度，短 Me 文件完整读，长文评价按分页读全。

新增 `read_many`：

```json
{
  "items": [
    {
      "doc": "<doc-a>",
      "start": 1200,
      "length": 1800,
      "expected_revision": "<rev-a>"
    },
    {
      "doc": "<doc-b>",
      "start": 0,
      "length": 1600,
      "expected_revision": "<rev-b>"
    }
  ],
  "budget_chars": 6000
}
```

实现要求：

- 批量限制控制单次资源使用，不限制整个任务能读多少篇；
- 同文档同 revision 的重叠范围先合并，公共 metadata 只返回一次；
- 每个 item 独立返回成功或 `not_found/changed/excluded`，不能把失败段落塞成空字符串；
- 超预算明确返回未读 items/剩余范围，不能静默丢失后半段；
- 只把实际返回的区间记录进任务 read ledger；
- 每段给 live revision、start/end、行号、来源角色和可定位链接；
- 当前文件变化返回 409/changed 信息；旧坐标不得直接套在新版上；
- 读到主库中的来源快照与读到主文章正文是不同身份，引用分别标明。

`read_many` 对应拟议 `POST /api/wiki/read-many`，app 使用 `POST wiki/read-many`。合法批次返回 HTTP 200 与逐项状态；一个 item 版本变化时该项为 changed 并带 409 含义，其他成功项可用。请求 schema/总体授权错误仍返回请求级 4xx。单文档 read 继续沿用 HTTP 409 的既有行为。

原始论文不可用时，工具不用自动上网。用户若问综述自身观点，直接引用综述当前内容；若问原实验事实，标明本地转述层级。

### 6.3 token 预算

`budget_chars` 使用实际字符，不假装是精确 token。若运行时有与目标模型一致的 tokenizer，可额外报告估计 token；无则报告字符与实测 usage，不引入一次模型调用去计数。

建议起步：

| 任务 | 候选输出 | 首轮证据 | 额外模型 |
| --- | --- | --- | --- |
| 已知个人短文件/明确路径 | 0–1 卡 | 完整短文件 | 0 |
| 单篇细节 | 3–6 卡上限，通常更少 | 1–3 个相关范围 | 默认 0 |
| 宽泛主题 | 6–12 卡 | 3–6 个方向代表范围 | 默认由当前回答模型判断 |
| 全文评价 | 先确定唯一文档 | 从 0 到末尾连续分页 | 不因节省预算跳过正文 |

卡片数/字符数是首轮设置，可按证据缺口扩展，不是硬性阅读上限。省 token 来自减少重复和无关内容。

### 6.4 分页与上下文稳定性

保留 offset 兼容旧接口；拟议新增可选 cursor 参数，与 offset 互斥，新客户端可使用包含 `index_epoch + filters + stable_sort_key` 的不透明 cursor。索引代变化时返回需要重启该列表的状态，避免重复或遗漏；不要悄悄用旧 offset 继续遍历变化集合。cursor 是新增 capability，旧端不假定它已经存在。

范围/过滤作用于候选集合本身，不能先全库取 top-k 再过滤 Library/Me。此要求同样适用于未来向量 backend。返回 total 的代价若过高，可在新视图用 has_more；不要为每问算不需要的全量总数。

### 6.5 标签过滤与列表：后续 Sapphire 能力

当前 search/documents 没有标签过滤参数，tags 已被抽取并不等于已经支持精确标签筛选。拟增加以下兼容参数，未实现前不在 capabilities 中声明：

| 接口 | 拟议字段与行为 |
| --- | --- |
| search | `tags: string[]`，`tag_match: any \| all`（有标签时默认 all）；文本条件与标签条件取交集 |
| documents | 相同 tags/tag_match，支持只按标签浏览，不要求提交空 search query |
| `GET /api/wiki/tags` / app `GET wiki/tags` | 当前文本查询、目录、角色/资产范围和已选标签交集内的标签列表；提供标签名称、匹配文档数与可分页状态 |

HTTP 使用重复 tags query 参数表达数组，MCP 使用字符串数组；app 网关必须按列表转发而非逗号拆词。`any` 是命中任一所选标签，`all` 是命中全部所选标签；没有标签等于不施加标签约束。字段细节在实现时同时进入 OpenAPI/MCP，现有调用保持不变。

索引维护派生的 doc↔tag 关系，保留原显示文字，以明确且统一的 Unicode/大小写规范化比较；不扫描 YAML 字符串做子串匹配。不按标签猜文档主体，也不自动把父标签等同于全部后代。层级、标签别名可后续明确加入，本轮不要求全库重命名标签。

过滤发生在 top-k、分页和 facet 统计之前。标签面板数量按不同 server doc id 计算，并遵循同一 scope/排除规则；它表示文档数，不是独立来源数。列表端点接受与 search 等义的可选 query 和过滤条件；有文本时按文本命中集合与当前已选标签、scope、角色的交集计数，无文本时不施加文本约束。counts 来自完整匹配文档集合，不是当前一页或 top-k 候选；先有一份正确的派生统计，不为 facets 启动 LLM/语义扩写。禁止把未获准库的标签通过计数泄露出来。保存标签只更新受影响元数据、tag 关系及相关缓存；不触发摘要重写。

### 6.6 SmartReader 与 Sapphire 的共同服务边界

当前 Wiki 配置只有单一 app_id，app_wiki 严格核对调用 app；app agent 的任务建立又有另一条入口。SmartReader 接入不能通过替换原 app_id 或移除校验实现。后续拟增加 `client_apps: [{app_id, permissions}]`，权限值最初为 `read` 与 `submit_maintenance`，保留 app_id 作为既有 owner/provider 和 app_bridge 归属；旧配置未声明 clients 时保持旧行为。

客户端策略同时覆盖直接 wiki/*、agent/chat、agent/ask，以及用户在 app 聊天里显式输入 /wiki 的路径。只读客户端不会因写作指南、wiki_mode 参数或字符串 context 获得维护权限。submit_maintenance 允许按当前用户指令提交限定任务，实际写入仍由现有 curator/任务协议完成，不顺带开放 PUT 正文编辑。实现细节与会话边界集中于[SmartReader 计划](local-shared-wiki-smartreader-v2.md)。

现有 MCP 调用普通 /api/wiki/*，不再次经过 app gateway；client 身份须从可信 session.app_id 绑定/解析到任务。普通 REST 的 access/tasks.require、MCP list_tools 与每次工具调用同时取任务和 client 策略的有效交集，避免初次通过检查后丢失限制。撤销阻止客户端后续受限调用；已接收的 durable job 按其既有授权与取消语义处理。

SmartReader 浏览器 UUID 和 DOM offset 不是 Wiki 文档身份与正文坐标。已入库材料须映射到 server doc id/revision；未入库材料使用明确的原材料身份和已提供内容。不存在的文件读取能力不能靠把路径写进 context 来补足。

## 7. 链接、缓存与一致性

### 7.1 增量链接索引

每文档解析只产生本文件出链；目标 registry 或 aliases 变化时重解析受影响的未决边。维护 from/to 索引，graph(doc) 查询局部入边和出边，按数量分页，不加载全库。

标准 Markdown、wikilink、嵌入、标题锚点和 block id 共用解析/解析结果合约。代码例子、YAML 和未使用的引用定义不单独产生边；实际 reference-style 链接必须解析对应定义。重复标题使用与渲染器一致的消歧规则。同名短链接没有唯一解时返回 ambiguous，不选择文件遍历顺序第一项。

sources 与普通 links 独立存储。来源 revision 变化标记派生区可能过期，普通 related 边不触发“证据失效”。

知识正文按[定点维护](local-shared-wiki-maintenance-v2.md)更新：人工或 AI 作者不决定资格，明确任务、当前版本、原始定位和实际读取决定可提交的局部操作。未涉及字节和依赖保持，修改影响的旧管理记录更新或撤销过期定位，不让 ownership 阻止后续维护。

Q56 确认无文本变化的复核：重读相关来源、判断原解释仍适用后只更新实际检查过的直接依赖记录。变化提示不直接判定结论错误；当前文章没有公开 sources 仍可检索和使用。普通 related 链接不传播证据变化，未检查部分不被顺带刷新。

### 7.2 缓存键

- 文档解析：`doc id + file revision + parser version`；
- body 区段：`body hash + chunker version`；
- embedding：`embedding_input_hash + chunker_version + embedding_fingerprint`，input_hash 覆盖实际输入的标题/heading/description 前缀及正文，fingerprint 包含模型、维度和格式版本；
- 文档卡：metadata hash；
- 搜索结果：规范化 query + intent + scope/filter + index_epoch + backend version；
- 摘要 freshness：其依据的 body/source revisions；
- 引用定位：doc id + revision + selector。

索引库中的派生表可以清空重建，document identities、ownership、jobs、journals 不能一并清空。外部 uid、来源路径或文字中的“verified”也不能授权写入。

## 8. 语义扩展的可选路径

当前词法命名查询有效，宽主题与跨表达仍有缺口。先修正文档类型、description、可靠标签/主题关系和候选覆盖，再用[验收语料](local-shared-wiki-evaluation-v2.md)判断是否需要语义腿。

### 8.1 接入方式

在 WikiService 内部定义适配器返回统一候选：`doc_id/section_id/score/rank/index_revision/backend`。最初是现有 SQLite；语义适配器返回候选后仍经过相同的 scope、角色、来源去重和 live read。

如果后端原生不能严格限定 scope，应建立符合范围的 collection，或在候选迭代中过滤并继续取下一批直至满足预算/耗尽，显式报告未穷尽；仅对全库 top-10 后过滤会系统漏召回。

混合召回优先用 RRF 这样的排名融合而非直接相加 BM25 与 cosine。精确定位结果可保持确定性优先。rerank 只作用有限候选且有延迟预算，不能每次默认加载大型模型。[R3]

### 8.2 QMD 的版本边界

QMD 可以作为可选本地语义工具，但其文本索引快照不是主库当前事实。适配器将结果映射回 server doc id 后，仍通过 WikiService 读取原文。

本次研究区分了 v2.8.3 与 main：MCP/HTTP/CLI 参数与自动扩写行为并不完全相同；main 的 metadata/filter 能力存在 Unreleased 版本边界，且命名空间不自动等于 OKF frontmatter。不要让 agent 直接绑定这些差异，也不要为后端在每篇文章里复制一套 qmd 专用元数据。具体接口核对见[验收文档 §9](local-shared-wiki-evaluation-v2.md)。

需要上下文增强时，先加确定性的文章标题、heading_path 和必要 description；只有评测显示有效再考虑 LLM 逐块生成额外上下文。Contextual Retrieval 给出了这种增强的研究路线，但其收益不等于本库的实测结果，也不意味着必须为每个 chunk 生成一份摘要。[R2]

## 9. 故障与验收要点

必须测试的行为：

- 无变更时连续 read/search 无全库 walk 和 identity/history backfill；
- 单篇修改只更新该文档及受影响边；索引失败不重复正文发布；
- 普通查询默认不返回 Template、snapshot 和工具文本；
- 精确命名和缺失别名不被宽泛语义结果覆盖；
- broad 检索中同源 100 份摘要不能挤走另一个相关原始来源；
- 不因共同引用或普通关联合并 A、B，明确重复摘要不会放大支持；
- snapshot/type/uid 迁移不改变原文或授予旧写入权限；
- 旧 revision、HTML 提取版本、Unicode offset、重复标题正确失效/定位；
- 当前文件被删除、移动、路径被新文件复用，都不沿用旧证据；
- 中文两字词与长问句、缩写/连字符/中英混合回归通过；
- 旧客户端 full 响应继续可用，新 compact 客户端不依赖省略字段；
- read_many 的失败项、超预算项与 read ledger 一致；
- 一次完成列表分页不会因索引变化静默漏项。
- 概念/综合问题能复用当前知识页，精确原文与变更问题仍按需回溯，避免所有查询固定回读全部来源。

后续应用能力另外验收：tags any/all 与目录交集、标签计数/分页、两个 app 共享同一文档但使用不同客户端权限、DOM 与正文坐标转换、无新增全文反复注入。它们不阻塞核心 V2-06 小样本验收。

Vera 的测试必须包含实际文件/工具输出/回答引用。只断言 prompt 包含“先读原文”不算行为验收。

## 10. What this defers

- 不增加一个与现有主库并行维护的内容服务或全量摘要库。
- 不自动提取 PDF、运行 OCR、分析视频或改动源码资产。
- 不立即把全部旧文改成新模板；parser 首先兼容旧文，格式升级按明确维护任务执行。
- 语义后端、reranker 与更多模型调用有独立开关，未达评测触发条件时保持关闭。
- 索引的全文副本用于检索重建；当前证据与持久身份始终由主库及现有服务管理。
- 完整来源根闭包、深层依赖传播与自动独立证据计数按 Q57 需要实际使用缺口，不作为本期检索门槛。

### 参考

- [R1] [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)：字段检索、bm25 权重、外部内容及 tokenizer。
- [R2] [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)：作为可选上下文增强路线，不移植其性能数字。
- [R3] [Cormack 等，RRF 原始论文](https://cormack.uwaterloo.ca/cormack/cormacksigir09-rrf.pdf)：按排名融合，不据此承诺本库收益。
- [R4] [Carbonell 与 Goldstein，MMR 原始研究](https://aclanthology.org/X98-1025/)：相关性与去冗余的选择目标。
- 当前代码：[store.py](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/store.py)、[search_index.py](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/search_index.py)、[links.py](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/links.py)、[app_bridge.py](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/app_bridge.py)。

# Octopus 本地共享 Wiki v2：Sapphire 库浏览、标签与共享后端

> **Status:** proposed — 基于 `.octopus.zip` 内 Sapphire 源码及 Octopus commit `453a7995bd6c54df33b0fc41dec09d370b4b40aa`。\
> **修订：** v2.1，2026-10-13（Asia/Shanghai）；V2-05 属后续应用计划，不阻塞核心试点与旧资料首批迁移。\
> 用户所称 sappihire 在代码中的实际应用名为 **Sapphire**。本方案不重建当前界面，不搬动 `.data` 或主库。\
> 关联：[总纲](local-shared-wiki-v2.md) · [检索协议](local-shared-wiki-retrieval-v2.md) · [prompt](local-shared-wiki-prompts-v2.md)

## 1. 定位：库浏览与文章阅读协作

Sapphire 负责库目录、标签、搜索、正文编辑、来源和版本；SmartReader 后续负责打开文章或选区与 Weber 深入讨论、引用定位、写作草稿和明确入库。`/wiki` 是当前 agent 的知识任务入口。三者共享文档身份与服务，具体交互各有重心。

| 用户动作 | Sapphire | /wiki / Weber |
| --- | --- | --- |
| 找到一篇笔记 | 搜索结果、目录、筛选、命中段落 | 根据问题定位后读必要证据 |
| 阅读文章 | 保留当前预览与编辑，提供“用 SmartReader 阅读” | SmartReader 中解释、评价、联想、比较 |
| 修改一句文字 | 编辑与保存、草稿冲突、历史 | 无需启动维护 job |
| 新文章入库并整理 | 提交材料或本次真实新建文件 | Weber 受控整理，返回持久任务结果 |
| 看来源是否可靠 | 来源、摘要/正文归属、当前/历史版本 | 在回答里准确说明所用证据 |
| 看研究方向 | 主题分组和可展开文档 | 回答当前问题，按缺口读代表材料 |

不在 Sapphire 内再复制一个模型调度器、维护队列或完整聊天平台。上一版安排在此实现的“问这篇/问选区”和完整阅读对话，优先转入[SmartReader 计划](local-shared-wiki-smartreader-v2.md)。Sapphire 仍可保留已有基础能力，不要求删除现有预览或普通链接跳转。

## 2. 当前实现与需要保留的能力

附件中 Sapphire 已有文件树、Markdown 编辑/预览、KaTeX、callout、PDF.js、图片和 HTML 预览、Git、回收站、草稿、SHA-256 revision 冲突检查、阅读位置、跨库响应隔离及手机布局。它已是一套能用的阅读应用，无需重写。

当前需要对齐的点：

| 文件/函数 | 现有行为 | 改造 |
| --- | --- | --- |
| `data.js:search`、`server.py search` | 走 app 自己的子串扫描，前 80 项截断 | 已绑定主库改走共享 wiki/search |
| `Store.files` | 每次遍历文件树 | 主库目录可消费增量 registry；原件功能保留 |
| `Store.links` | 遍历文件表并为所有 Markdown 刷新链接缓存 | 主库改用增量 graph/links 结果 |
| `note_links.py / links.js` | basename 重名时取首个匹配 | 共享解析协议，歧义显式展示 |
| `markdown.js:metadata` | 只显示 tags，剥去 frontmatter 后渲染 | 从 WikiService 读取规范化 OKF 元数据和分区 |
| `dialogs.js search` | 有 debounce 与 ticket 防过期响应 | 保留，并增加请求取消和结果分页 |
| `data.js:save` | 现有 app save，框架有 app_bridge | 一个动作只走一个 writer，不重复调用 wiki/document |

附件显示的是 app 当前源码；仓库 `docs/sapphire-wiki-iterate-prompt.md` 描述的所有未来控件并不等于已经实现。

## 3. 主库绑定与接口边界

### 3.1 使用已有 app 网关

页面位于 `/apps/<app-id>/`。共享知识请求使用相对于应用根目录的：

- `wiki/info`、`wiki/search`、`wiki/read`、`wiki/documents`、`wiki/graph`；
- `wiki/maintain`、`wiki/jobs/<id>`、`wiki/history`；
- 已有 `agent/agents`、`agent/chat` 或 `agent/ask`。

这些走 Octopus app gateway。**不要把它们拼成 `api/wiki`。** `api/…` 当前属于 Sapphire 自身的文件服务。现有同源身份和 app 绑定继续复用，不在页面代码中保存新 token。

v2 新增能力需要同时加入普通 REST router、app handler table、MCP wrapper、工具可见列表和 API 契约：

- search 的 `intent` 与 `view=compact`；
- read_many 工具，对应拟议 `POST /api/wiki/read-many` 和 app 路径 `POST wiki/read-many`；
- 小型 capabilities/索引状态；
- 增量图和文档角色信息；
- tags/tag_match 过滤，以及拟议 `GET wiki/tags` 标签列表；当前接口还没有这些查询参数。

未上线能力不由前端猜测调用。启动时读取一次绑定和能力，在库切换/配置代变化时刷新；不需要每个键盘搜索都读 info。现有 wiki/info 不返回 root，当前绑定核对使用已存在的 wiki/config 加 info；以后若 info 增加绑定摘要，必须先声明新字段再消费。

### 3.2 多库与独立模式

当前 Octopus 配置是一份账户 Wiki 主库，而 Sapphire 可以切换多个库。这一点必须在 UI 层显式处理：

1. 通过已核实的 `workspacePath`、绑定 root/vault id 和当前 app_id 确认当前显示库与 Wiki 接口归属；
2. 只有匹配的库使用共享 Wiki 搜索、问库与维护；
3. 用户切到另一个 Sapphire 库时，不得静默拿主库结果回答；
4. 未绑定库保留 app 的本地阅读与搜索，并标为“当前库搜索”；问主库是独立操作；
5. 若后续要支持多 Wiki 库，需要扩展注册与授权模型，不能只加一个未校验的请求参数。

Standalone/localStorage 模式保留明确的离线/本地语义，不连接到不匹配的主库，也不声称它具备同样的全文索引、来源判定或维护能力。

当前 config.app_id 既约束 app_wiki，又关联 app_bridge 的 owner/provider；不是任何 app 都可直接调用同一个 wiki/*。SmartReader 后续通过拟议 client_apps 获得 read/submit_maintenance，不转移原 owner，也不复用 Sapphire 身份。配置和 agent/chat 入口一起适配，见[SmartReader 计划](local-shared-wiki-smartreader-v2.md)。附件缺少 live config，本文件不宣称目前主库必然绑定 Sapphire。

## 4. 搜索体验与文档导航

### 4.1 一个搜索框，两个明确目的

默认搜索采用 `intent=lookup` 或 `question` 的文档发现，不调用模型。标签点击同样只查询索引。需要讨论时把当前文档和问题交给获准的 SmartReader/Octopus 会话，不能在每个 debounce 时启动 Weber。

搜索卡优先显示：

- 标题与必要的路径差异；
- 一句 description；
- 命中的章节与短片段；
- 类型：原文/技术报告/综合/导航/模板等，按服务端 doc_role；
- 若相关，来源或生成区过期提示；
- 展开更多命中/同源整理的入口。

不展示 BM25 原始分数、模型供应商、完整索引 JSON 或内部候选调参。诊断工具可以另开一个开发视图。

### 4.2 标签检索

标签面板与文本搜索共同作用于当前显示库，先选实际存在的标签，再按需要加关键词或目录。默认多标签为“同时具有全部标签”，可明确切换“具有任意标签”。比如选“视频生成”与“实时”，含义由模式决定，不隐式扩成所有 AI 文章。

协议沿用[检索协议 §6.5](local-shared-wiki-retrieval-v2.md#65-标签过滤与列表后续-sapphire-能力)：

- `tags: string[]`；`tag_match: all | any`，默认 all；
- 有关键词走 search，无关键词走 documents 的相同过滤；
- 标签面板消费 `GET wiki/tags`；后端对应 `GET /api/wiki/tags`；
- HTTP tags 采用重复 query 参数，数组不拼成一个逗号字符串；
- 过滤先于排名和分页，scope、doc_role/资产排除和 index epoch 一致；
- 标签数量按当前 query、scope、角色和已选 tags 交集中的不同 server doc id 计算，覆盖完整匹配集合，明确它是“文档数”，不叫“独立来源数”；
- 一篇长文不因多个章节重复计数；模板、原稿快照按当前查询模式排除；
- UI 保留标签原显示文字，匹配使用服务端统一规范化，编辑标签不自动生成 Key note。

第一版保留平面标签与已有目录即可。层级标签、自动聚类、全库标签清洗和复杂标签别名不是本票前置。未实现的 tags capability 不在前端假装生效；服务异常说明状态，不悄悄改用子串“近似筛选”。

### 4.3 宽泛主题

对于“数字人研究”，用户可切换“按方向浏览”。后端返回有实际文档的分组，Sapphire 展示方向→代表文章→更多文章。

- 方向来自当前文档和已核对主题关系；
- 默认折叠同一来源的多份摘要；
- 新文章即使没写进旧导航，也可被检索分组；
- 每组显示实际材料数量和类型，不把重复摘要计成独立证据数量；
- “资料方向”与“结论一致”是不同概念，UI 不把文章多显示成共识强。

如果库只有两份相关技术报告，就清楚展示它们各自讨论的不同问题，无需空建一棵复杂分类树。

### 4.4 空结果、部分结果与歧义

- 空结果：保留查询和范围，可提示改用原名/别名或查看当前目录；
- 索引更新中：展示已知结果与更新状态；不得写“库里没有”；
- partial lexical：说明只有部分词匹配，避免伪装成精确题名命中；
- 同名文档：展示完整路径，由当前上下文或用户选择确定；
- 原目标已删除：说明未找到该身份，不能用同路径的新文档替代旧来源。

现有空结果界面有“创建这篇笔记”入口，可以保留人工选择，但不能因为搜索失败自动生成一篇 AI 回答填入库。

## 5. 库内阅读、来源与 SmartReader 跳转

### 5.1 Key note 的阅读地位

显示 Key note、正文、相关笔记时，让用户能看到新增总结的归属。没有必要用刺眼警示给每份 AI 辅助整理打标签；只需在需要判断来源时可展开。

建议 UI：

- 顶部：题名、类型、description、少量标签；
- Key note：可折叠要点；
- 正文：原始文章和既有排版；
- 侧栏：当前章节、出链/反链、来源；
- 整理信息：谁整理、依据哪些来源、是否存在正文变化提示。

普通编辑不把 status 从 stable 自动降成 draft，也不自动排 Weber 重写。若正文变了而旧 Key note 可能过期，只提示其依据的版本不同。

### 5.2 点击引用定位

回答引用与完整阅读讨论的主要打开目标是 SmartReader；以下身份和定位约定在 Sapphire 自身预览中也保持一致。当前 SmartReader 尚无 Wiki deep link，不能仅拼接一个 URL 就认为功能已完成。

新引用结果至少绑定 `doc_id + revision + selector`，selector 可以是实际 heading/block/range。优先打开当前文档并定位当前可验证位置：

- revision 相同：跳转并高亮实际范围；
- revision 已变：请求新定位，显示“内容已更新”，不把旧数字范围套在新文上；
- 无法重新定位：打开文档并给出原引用说明/可恢复历史入口；
- 文件改名：server id 定位新路径；
- 同名短链歧义：列候选，不按首个 basename 跳转。

章节 slug、重复标题后缀、Obsidian `#^block-id` 和 Unicode offset 必须与服务端一致。可以共用 fixtures 和服务端 locator 输出，无需把 Python 解析器硬搬到 JS。

当前 JS 的 `String.slice` 使用 UTF-16；共享 read 坐标是 Python Unicode code points。渲染引用不直接假定两者相同。

### 5.3 来源层级而非一个“可信”徽章

来源面板分别展示：

- 原始资源/当前主文章；
- 本文自身 Key note 与解释；
- 来自这些资源的综合文章；
- 相关但没有引用依赖的笔记；
- 缺失或版本已变化的来源。

`generated` 不是“不可信”标签，`stable` 不是“事实已验证”标签。类型、内容状态、来源是否存在、人工核验记录分别显示。也不默认把用户上传文件视为已核验。

本地报告可以支撑“报告提出了什么”的回答。关于论文实验数值，若只有报告转述，显示转述层级；不能为了让 UI 全绿而伪造源论文或 verified 元数据。

## 6. 保存、整理与 agent 协作

### 6.1 一次动作只写一次

**普通保存：** 沿用现有 Sapphire `api/save` + app_bridge，或明确迁为 `PUT wiki/document`，二选一。成功后消费真实 change/revision/index 状态，不再向另一个接口重复提交同样正文。

**新材料入库：** 完整材料直接交 `wiki/maintain`，由框架去重、原样落盘、排 Weber job。不要先创建同名 Markdown 再提交 material。

**刚创建文件整理：** 交真实创建回执和当前文件清单；框架核验范围。现有 api/save 不直接返回框架 change_id，可以用 wiki/history 找到与路径、当前 revision 对应且 before_hash=null、status=applied 的真实创建记录，再填 maintain 的 created_changes；也可在后续桥接接口显式增加回执。不能在浏览器凭空造一个创建 ID。创建与整理的动作不同，但不重复创建文件。

**选定旧文整理：** 以用户选中的路径建立维护任务。相关旧文是只读候选，界面不得自动勾选全目录或一跳邻居作为可写目标。

这些沿用原规格的既有维护契约，不新增第二套入库规则。

### 6.2 用 SmartReader 阅读与继续讨论

Sapphire 的“用 SmartReader 阅读”传递真实 vault/doc id、revision 与可选 selector，打开账户下实际安装且获准的 SmartReader。当前路径用于显示，服务身份用于定位；URL 不携带正文、凭据或任意本地文件读取权限。启动字段与引用协议统一在 SmartReader 文档定义。

SmartReader 未安装、未取得当前库客户端权限或暂不支持对应格式时，继续使用 Sapphire 已有预览并说明可用方式；不更换 owner app_id 来绕过限制。Sapphire 不向另一个 app 复制一份独立“知识正文”作为同步源。

“问这篇”“问选中部分”、阅读中写草稿与把讨论入库由 SmartReader 的 V2-09 实现。Sapphire 可以提供跳转入口，但不再单独实现第二套 context 拼装与 writing workflow。

### 6.3 维护状态

现有任务状态继续用真实 `queued/running/completed/completed_with_attention/failed/cancelled/interrupted`。显示每项产物路径、复用结果、需处理原因和安全重试能力。

索引恢复不叫“重新生成文章”；正文已发表但索引未完成时，界面分开显示两者。Vera 不因为一个 toast 写“完成”就认定入库成功，必须核对文件、revision 和索引回执。

## 7. 多端编辑与性能

保留现有草稿、revision 比较、跨库 response generation、读写锁、附件流式读取和 Git 行为。新功能增加以下保证：

- 搜索输入变化取消旧请求；旧库/旧查询的晚到响应不能覆盖新视图；
- 搜索分页绑定范围和 index epoch；
- link 面板按需加载局部邻接边，避免打开每篇文章时 graph 全库；
- 当前文档 metadata/locator 与正文使用同一 revision；
- 文件树可分页/按目录加载，成长后不每次下载整个库；
- 反链从共享索引计算，不向所有旧文写回；
- 冷加载大 PDF 不阻塞关键词搜索；PDF 的人工阅读能力和 agent PDF 提取能力分别处理；
- 保存正文不会顺便读取/复制未变附件；
- 外部编辑后提示当前已知状态，不能承诺每个编辑器都参与框架锁。

小库可以保持现有文件树体验；引入分页的触发点是实际目录加载成本，不为几十篇文章先做复杂虚拟树。

## 8. 实施顺序与验收

本票 V2-05 在核心 V2-01～04、V2-06 试点之后按用户安排实施，可与 V2-08 后续批次并行。不是完成已有资料整理所必需的 UI 前置。

| 顺序 | 完整交付 | 验收 |
| --- | --- | --- |
| S1 | 主库身份与 capabilities 对齐、共享搜索 | 当前库结果正确，未绑定库不串库 |
| S2 | 标签过滤/列表与紧凑搜索卡 | any/all、目录交集、分页、文档计数正确，全部不调用模型 |
| S3 | 来源/整理分区、局部反链与本地定位 | 原文与新增解释可辨，重复摘要不伪装独立来源 |
| S4 | SmartReader 可用时的阅读跳转、维护状态 | doc/revision/selector 一致，保存不触发维护；完整阅读对话归 V2-09 |
| S5 | Unicode/多端/手机回归 | 修改、跳转、草稿、Git、PDF和既有阅读位置正常 |

依赖框架新增能力的步骤等 capability 实际可用再启用。Sapphire 和框架提交可以分开，但每个启用功能必须包含端到端契约测试。

用户重点场景：

1. 搜 Vidu → 报告优先 → TTFF 章节；SmartReader 可用时打开相同文档继续讨论；
2. 搜数字人 → 显示当前有证据的研究方向，不只旧导航；
3. 问专业 → Me 当前资料；问职业 → 明确未记载；
4. 修改报告正文 → 原文保存 → 索引更新 → 旧摘要提示可见但不自动重写；
5. 添加 20 份同源总结 → 默认结果仍能看见不同原始来源；
6. 切到另一个库再搜索/问库 → 不串入主库内容；
7. 笔记改名 → 原引用身份可定位；同名新文件不继承旧引用。
8. 标签“实时”+“视频生成”在 all/any 下结果不同且正确；加目录后 counts 与列表一致，无全库扫描或模型调用。

## 9. 可直接用于 Sapphire Iterate 的开发 prompt

以下是以后实施时使用的任务文本，本次仅交付方案：

```text
请基于当前 Sapphire 做增量迭代，先读应用 README、Octopus Wiki v2.1 总纲/检索/内容/SmartReader/本文件和运行中 OpenAPI。此票是后续V2-05，不阻塞核心和首批旧资料迁移。保留主库 .data、目录、原文、附件、草稿、Git、PDF 阅读与现有界面习惯。

确认当前显示的 vault 与账户绑定的 Wiki 主库一致，再使用相对应用根路径 wiki/… 与 agent/…。其他 Sapphire 库保留自己的本地阅读搜索，不把请求静默发给主库。不要建立新知识索引、模型进程或维护队列。

第一批实现共享搜索、compact文档卡、tags/tag_match及标签面板。标签默认all，可选any；过滤先于排名/分页，按文档id计数，不称独立来源数。无关键词用documents。只在真实capabilities支持时使用新参数与wiki/tags，旧接口兼容。重复basename、大小写冲突、重复标题和block-id按共同解析合约处理，不取第一个匹配。

普通搜索与标签点击不调用模型。问这篇/选区和阅读对话优先交给SmartReader，不在此重建第二套context或writing流程。通过实际获准app身份传文档id/revision/selector，不改owner app_id。普通保存只走一个现有writer并更新索引，不启动Weber。明确入库与选定旧文整理沿框架持久任务和真实回执。

显示Key note、正文、来源和整理状态的不同含义；generated不等于二手总结，stable不等于核验。默认隐藏模板/原稿快照的检索噪声，来源仍能展开；同源摘要合并展示。关键引用能回到实际文件位置，revision变化后重新定位。

完成实际浏览器验证：标签any/all/目录交集/counts/分页、Vidu章节、数字人方向、改名/旧revision、跨库晚到响应、草稿冲突、普通保存不维护、手机和PDF/Git回归。SmartReader尚未具备时明确跳转状态，不把它记作本票已有聊天。报告实际改动、验证结果和未完成事项，不用框架接口存在代替UI验收。
```

## 10. What this defers

- 多个 Wiki 主库的服务端注册/授权扩展，不能由 Sapphire 单方假装支持。
- 新的图谱大屏、全库自动聚类和全量 AI 生成概览。
- 重新设计已经满足需要的阅读界面、字体和编辑器。
- 自动执行旧库批量整理、恢复旧快照或 Git 推送；这些不是本次设计文件的执行动作。
- PDF/视频自动理解，保持既有范围。

### 证据与参考

- 附件 `applications/sapphire/README.md`、`data.js`、`dialogs.js`、`server.py`、`links.js`、`note_links.py`、`markdown.js`。未在交付包中复制应用代码或私人资料。
- [当前 app 网关](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/routers/wiki.py)。
- [当前 Sapphire 接口交接](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/docs/sapphire-wiki-iterate-prompt.md)。
- [现有 AppMutation 桥](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/app_bridge.py)。

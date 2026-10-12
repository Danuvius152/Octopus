# Sapphire Iterate：接口兼容交接

更新：2026-10-12。本文件同步本机已上线的 W01–W05 框架合约，供用户之后在 app Iterate 内开发；本轮未开发新的 Sapphire UI，保留满意的 `/wiki` 前端。部署 health 与构建通过，W06 实际 Weber 补验仍未完成；真实 baseline snapshot 尚未退役，旧库尚未批量整理。职业字段与全文零基分页的指南已修正，待实机补验。应用开发仍须核对运行中 OpenAPI 与实际可恢复状态。用户当前优先状态文档，迁移/旧库整理未在本次交接执行。状态见[实施交接](plans/local-shared-wiki-implementation-handoff.md)，目标见[spec](plans/local-shared-wiki-spec.md)。

下面 API 保留基础请求样例并同步新增字段；最终体验约束来自已确认 spec。应用开发前先核对实际后端版本，只接已有能力。保留 `.data`、vault、文件路径、附件、Git/PDF 人工阅读；不重建满意的界面。框架接口上线不代表新的应用控件已经开发或全部模型行为已经验收。

```text
请将现有 Sapphire 增强为 Octopus 本地知识库的阅读与编辑工作台。保留当前应用架构和 UI 习惯，不搬迁、重新导入或重建 .data；主库是 vault `obv`。知识检索、写入约束、维护 prompt 和 Weber 队列属于 Octopus，应用通过已实现接口调用。

先读取当前应用代码、README、Octopus Wiki spec/交接和运行中 OpenAPI，核对实际已上线的框架能力及未完成验收，再结合浏览器按需要改进。保留当前满意的界面；不要复制另一套知识索引、模型进程、维护规则或统一 memory。目标体验仍需应用内实现与验收，不能把框架接口存在当作 UI 已完成。

界面需求：
1. 保留文件夹、搜索、Markdown 编辑、附件、Git 与 PDF 人工阅读。目录承担大分类，标签用于细粒度概念；让 Key note、已有正文和来源链接容易阅读。Weber 新增部分可展示整理者和来源漂移提示，不能把原文与新增解释混为一体；普通小修改保留已整理状态，不要求重新入库。
2. 增加概念标签筛选、出链/反向关联和按目录浏览；入库补的 [[关联]] 只应来自同一具体研究问题等特别相关正文，不能因同目录/大标签乱连。反向关联来自框架索引，不向旧文写反链；现有路径、链接、附件和 frontmatter 保留。
3. 提供明确的“问知识库”操作：用户提交才启用查询。阅读页面可带选中文本与笔记路径，但页面内容、自动摘要和普通 agent 聊天不能隐式启用知识任务。保留可继续的 agent conversation、SSE 流、引用回跳和读取覆盖信息；需要时提供全文阅读，不只把摘要当证据。
4. “入库并整理”用于新材料，或当前操作刚创建的文件清单。明确提交才运行 Weber；普通保存只更新索引，保留已有整理，不自动总结或再次入库。默认复用 Library 现有目录，无匹配用 Library/Inbox。旧文维护是用户明确选择文件后的另一个动作，不能因相关性扩写范围。
5. 显示维护任务的总项数、完成、需处理/失败项、具体产物、取消和重试。框架已实现：维护发表前版本冲突在同任务内有限重读重试，持续变化或无法保护人工内容时保留候选与原因；名称冲突不能覆盖旧文。普通编辑不是重新整理任务。当前实际 Weber 的维护/冲突补验未收齐，UI 必须显示真实任务结果；应用开发时核对后端版本。PDF、工具/tmp/静态资产保留，不自动套笔记模板，不计作整理失败。
6. 按真实接口提供文本变更历史与恢复。框架已实现普通备份 30 天/每篇 10 个不同历史版本，未解决冲突候选 90 天；每日和启动清理，UI 根据真实可恢复状态展示，不对过期版本显示可用恢复。旧 snapshot 的真实退役尚未执行，不能提前显示已退役或已回收容量。人工编辑过的 Weber 整理区保留，普通编辑不要求重新入库；明确维护无法安全处理时才显示候选原因。正文仍是普通文件，应用保存不剥除管理注释。附件与 Git 仍由 Sapphire 决定，框架不自动 Git 提交或复制附件备份。

本机已上线接口（应用开发时再次核对版本，不猜新字段）：
页面位于 `/apps/<app-id>/…`，使用相对于应用根目录的 `wiki/…` 和 `agent/…`。不要把知识请求发给现有应用后端的 `api/wiki`。浏览器沿用现有同源认证；应用后端用现有 `OCTOPUS_APP_TOKEN` bearer 请求 `/apps/<app-id>/wiki/…`。不要把凭据写进源码或分享链接。框架只允许本账户绑定的 Sapphire 访问主库。

GET wiki/info：库身份、curator_agent_id、组织约定与排除范围，无统一知识摘要。
GET wiki/documents?directory=Library&offset=0&limit=30：分页目录；items/next_offset，默认排除资产。
GET wiki/search?query=<关键词>&offset=0&limit=12（可选 directory=Library/Research，按真实库内目录过滤）：items 含 path/title/snippet/revision/metadata/stale。W01 新增 score、snippet_start、section，以及跨章节命中的 sections；每个章节位置为 {title,start,end,line_start,line_end}，与 read 的原文坐标一致。分页按文档去重，返回 total/next_offset、query_terms、match_mode（lexical/lexical_partial）、index_status（ready/partial）与 index_errors。索引错误不等于库中无结果。可用 include_assets=true 明确查看资产，不默认开启。
GET wiki/read?doc=<id或相对路径>&start=0&length=16000：content、revision、line_start/line_end、characters、next_start、metadata、stale。start 是从 0 开始的字符偏移，不是行号；全文从 0 开始，按 next_start 连续读取到 null；同一轮用 expected_revision 检查版本，变化后重取坐标。PDF 的 content/pages 为空、quality 为 excluded。
GET wiki/graph?doc=<id或路径>：nodes、edges；按 edge.from/to 展示当前节点的出链与反向关联，链接导航不修改原文。
GET wiki/image?path=<关联图片路径>：原始 PNG/JPEG/WebP/GIF，最多 10 MiB；PDF 不支持。现有原件阅读功能继续使用应用自己的 raw 接口。

POST wiki/maintain，JSON：
A. 尚未保存的新材料：
{"idempotency_key":"<一次操作固定的唯一键>","material":"<完整原文>","target":"Library/Inbox/新笔记.md","scope":"保留原文，补 Key note、必要标签和关联","sources":[]}
由框架创建并整理新文件；不要先保存同名文件再用这个形式提交。target 已存在时不会覆盖。省略 target 则由框架在 Library/Inbox 生成唯一名称。
B. 本次已创建文件：
{"idempotency_key":"<固定键>","paths":["Library/新笔记.md"],"created_changes":{"Library/新笔记.md":"<实际创建变更 id>"},"scope":"只整理这次新增文件"}
创建变更必须对应该路径、before_hash=null、status=applied、after_hash=当前 revision。使用框架 PUT document 可直接得到 change_id；沿用现有应用保存/导入接口时，保存完成后用 wiki/history 获取本次实际创建记录。只将当前操作的新文件放入清单，不能补入已有相关笔记。框架会验证清单；失败不转为全库维护。
C. 用户明确选中的旧文件：
{"idempotency_key":"<固定键>","paths":["<用户选中路径>"],"scope":"只整理选中旧文，原文逐字保留"}
这是独立的旧文维护；不自动展开目录或关联文档。旧库首轮批量整理的样稿已接受，但仍属单独执行范围，本次应用迭代不启动它。
sources 对象格式为 {doc,revision,location}，只是只读来源，不是写入清单。material 与 paths 二选一。重复同键同请求返回同任务，不同请求返回 409。W04 已上线的换键同原材料复用：待处理的同材料返回已有任务 id；已存在原材料返回 completed 任务，items 中 reused=true，path/output 为复用位置，不创建目标、不重复整理。比较完整材料，可信剥离 Weber 区域；人工改过的区域不剥离。同键不同请求仍为 409。

返回任务有 id/status/items/progress；progress 含 total/completed/attention/failed。框架 worker 的 organize 合约新增 aliases（少量可靠名称，和原生别名合并）与 reuse_existing_summary（阅读判断后沿用用户要点）；非空 key_note 始终补充，不被“摘要”标题压掉；null 可保留可信已有 Weber 要点。提交入口不直接调用 organize，任务 worker 才按维护与写作指南发布。新增关系需要真实当前定位和任务阅读覆盖，使用 [[唯一路径]]；新锚点不存在或显式路径无法解析返回 422，旧坏链不会因此被改写。
GET wiki/jobs?offset=0&limit=30；GET wiki/jobs/<id>；POST wiki/jobs/<id>/cancel；POST wiki/jobs/<id>/retry。
状态包括 queued/running/completed/completed_with_attention/failed/cancelled/interrupted。以真实逐项产物显示完成，不因模型一句“已整理”就判断成功。重试保留完成项，发表前正文冲突最多两次重读，retry_count 跨重启/手动 retry 持久；index_recovery=true 表示已有发表凭证需要恢复或核查，重试不消耗正文预算、不再次发稿。已发表正文、身份或引用发生变化，或落盘与 applied 标记之间中断导致发表状态无法确认时，保留当前正文与凭证，逐项显示 attention/error；不能把 retry 呈现为自动重新生成。items 的 output/output_revision/output_change_id 表示凭证记录的目标，状态不确定时不代表已确认产物；发布项以 index_complete=true 和成功状态确认；reused=true 的项表示只复用已有位置，不再次整理。

worker 的 publish 只新建必要页，已有原文或 concept/source/topic 页不能整页替换，应走保全原文的 organize/restructure。写入时再次核验 assignment 的稳定文档身份；相同路径、相同 hash 的替换文件也不继承旧授权和阅读记录。TXT/HTML 已有配对阅读页时须完整读取该页，其身份也受保护。明确任务仍由维护入口排队，前端普通保存继续只刷新索引。

PUT wiki/document：{path,content,expected_revision,reason}；响应 {change_id,path,revision}。创建时 expected_revision=null，编辑时用读取时版本。也可保留现有应用 save 接口，框架已为其串接共享锁、历史与索引；不要同一保存重复调用两个 writer。
GET wiki/history?path=<路径>&offset=0&limit=50：变更记录、next_offset。W03 新增每条 recoverable:{before,after}；仅为 true 的恢复侧显示可用操作，false 不等于该版本删除文件。普通编辑冲突可能有 proposed_content，整理冲突可能有 proposed_operation；后者是待重读并重新整理的结构化候选，不是可直接覆盖的正文。
POST wiki/restore：{change_id,expected_revision,side:"before"|"after"}，创建新的恢复版本；结构化整理候选的 after 不能直接恢复。
GET wiki/snapshots：基础版遗留全库快照列表。Q53 已同意新文本恢复机制验收后退役冗余 baseline snapshot；界面不将该列表或全库恢复作为新流程依赖，按运行中接口正确处理空列表/已退役能力。不要创建新的全库附件快照。
W03 的 POST wiki/snapshot 创建已退役，返回 410；snapshots 项标记 recoverable，已退役旧快照仍有最小审计但不可恢复。GET wiki/info 新增 history_policy 与 snapshot_creation_enabled=false。
错误使用 HTTP 状态和 JSON detail。403/404/409/422/413 分别按权限/未找到/版本冲突/输入或范围不符/图片太大处理；410 为恢复侧过期或能力退役，503 为派生索引不可用，不能当作正文不存在。保留用户输入并支持合理重试。

问库采用现有 agent/chat（SSE）或 agent/ask（等待整体结果）接口：
{message:"<用户实际问题>",agent:"<选中同账户 agent 名称>",conversation_id:"<续聊 id，可省略>",wiki_mode:"read",context:"<选中文本、当前笔记路径等只读上下文>"}
GET agent/agents 返回可选 name；用 agent 名称创建会话，省略时使用应用默认 agent。续聊传 conversation_id，沿用原会话角色；不要用 agent_id 选择角色，也不要同时发出重复请求。只有明确“问知识库”操作带 wiki_mode=read；普通聊天省略。维护按钮直接调用持久 wiki/maintain，不能把排队工作伪装成同步 agent 问答。

验收：普通保存不触发 Weber；普通聊天不查库；查询引用能定位当前正文；超过单段上限可以继续读取；新材料整理不改任何旧文；创建清单伪造、旧版本和重名不会覆盖；普通编辑保留既有整理且无自动入库，人工修改后的整理区保留；目标版本冲突确实有限重读，无法安全发布才留候选；维护取消/重试、历史恢复和绑定认证正常；保留当前移动端、Markdown/公式/附件与 Git 功能。最后在真实应用页面验证，报告实际改动与结果。
```

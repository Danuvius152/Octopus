# Sapphire Iterate prompt

复制下面整段到 Sapphire 的 Iterate。框架知识接口已实现；请在应用内部迭代界面，保留 `.data`、当前 vault 注册、文件路径与现有 Git/PDF 阅读功能。

```text
请将现有 Sapphire 增强为 Octopus 本地知识库的阅读与编辑工作台。保留当前应用架构和 UI 习惯，不搬迁、重新导入或重建 .data；主库是 vault `obv`。知识检索、写入约束、维护 prompt 和 Weber 队列属于 Octopus，应用通过已实现接口调用。

先读取当前应用代码、README 和现有后端合约，结合浏览器验证后改进。不要复制另一套知识索引、模型进程、维护规则或统一 memory。

界面需求：
1. 保留文件夹、搜索、Markdown 编辑、附件、Git 与 PDF 人工阅读。目录承担大分类，标签用于细粒度概念；让 Key note、已有正文和来源链接容易阅读。Weber 新增部分可展示整理者和待刷新状态，不能把原文与新增解释混为一体。
2. 增加概念标签筛选、出链/反向关联和按目录浏览。反向关联来自框架索引，不向旧文写反链；现有路径、链接、附件和 frontmatter 保留。
3. 提供明确的“问知识库”操作：用户提交才启用查询。阅读页面可带选中文本与笔记路径，但页面内容、自动摘要和普通 agent 聊天不能隐式启用知识任务。保留可继续的 agent conversation、SSE 流、引用回跳和读取覆盖信息；需要时提供全文阅读，不只把摘要当证据。
4. “入库并整理”用于新材料，或当前操作刚创建的文件清单。明确提交才运行 Weber；普通保存更新索引与待刷新标记，不自动整理。默认复用 Library 现有目录，无匹配用 Library/Inbox。旧文维护是用户明确选择文件后的另一个动作，不能因相关性扩写范围。
5. 显示维护任务的总项数、完成、需处理/失败项、具体产物、取消和重试。重名/冲突保留候选结果并展示审查入口；不得用覆盖旧文来“解决”冲突。PDF、工具/tmp/静态资产保留，不自动套笔记模板，不计作整理失败。
6. 提供逐文件变更历史与恢复操作。人工编辑过的 Weber 整理区应保留，显示需处理冲突；不要自动把人工编辑恢复成旧生成版本。正文仍是普通文件，应用不在保存时剥除框架管理注释。

真实接口：
页面位于 `/apps/<app-id>/…`，使用相对于应用根目录的 `wiki/…` 和 `agent/…`。不要把知识请求发给现有应用后端的 `api/wiki`。浏览器沿用现有同源认证；应用后端用现有 `OCTOPUS_APP_TOKEN` bearer 请求 `/apps/<app-id>/wiki/…`。不要把凭据写进源码或分享链接。框架只允许本账户绑定的 Sapphire 访问主库。

GET wiki/info：库身份、curator_agent_id、组织约定与排除范围，无统一知识摘要。
GET wiki/documents?directory=Library&offset=0&limit=30：分页目录；items/next_offset，默认排除资产。
GET wiki/search?query=<关键词>&offset=0&limit=12：items 含 path/title/snippet/revision/metadata/stale；可用 include_assets=true 明确查看资产，不默认开启。
GET wiki/read?doc=<id或相对路径>&start=0&length=16000：content、revision、line_start/line_end、characters、next_start、metadata、stale。全文超过上限时继续 next_start；expected_revision 可检查版本。PDF 的 content/pages 为空、quality 为 excluded。
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
这是独立的旧文维护；不自动展开目录或关联文档。旧库首轮批量整理有单独的样稿确认，本次应用迭代不启动它。
sources 对象格式为 {doc,revision,location}，只是只读来源，不是写入清单。material 与 paths 二选一。重复同键同请求返回同任务，不同请求返回 409。

返回任务有 id/status/items/progress；progress 含 total/completed/attention/failed。
GET wiki/jobs?offset=0&limit=30；GET wiki/jobs/<id>；POST wiki/jobs/<id>/cancel；POST wiki/jobs/<id>/retry。
状态包括 queued/running/completed/completed_with_attention/failed/cancelled/interrupted。以真实逐项产物显示完成，不因模型一句“已整理”就判断成功。重试保留完成项，重新读取未完成项的当前版本。

PUT wiki/document：{path,content,expected_revision,reason}；响应 {change_id,path,revision}。创建时 expected_revision=null，编辑时用读取时版本。也可保留现有应用 save 接口，框架已为其串接共享锁、历史与索引；不要同一保存重复调用两个 writer。
GET wiki/history?path=<路径>&offset=0&limit=50：变更记录、next_offset。普通编辑冲突可能有 proposed_content，整理冲突可能有 proposed_operation；后者是待重读并重新整理的结构化候选，不是可直接覆盖的正文。
POST wiki/restore：{change_id,expected_revision,side:"before"|"after"}，创建新的恢复版本；结构化整理候选的 after 不能直接恢复。
GET wiki/snapshots：现有二进制快照。不要在普通编辑/入库时反复创建全库快照。
错误使用 HTTP 状态和 JSON detail。403/404/409/422/413 分别按权限/未找到/版本冲突/输入或范围不符/图片太大处理；保留用户输入并支持合理重试。

问库采用现有 agent/chat（SSE）或 agent/ask（等待整体结果）接口：
{message:"<用户实际问题>",agent:"<选中同账户 agent 名称>",conversation_id:"<续聊 id，可省略>",wiki_mode:"read",context:"<选中文本、当前笔记路径等只读上下文>"}
GET agent/agents 返回可选 name；用 agent 名称创建会话，省略时使用应用默认 agent。续聊传 conversation_id，沿用原会话角色；不要用 agent_id 选择角色，也不要同时发出重复请求。只有明确“问知识库”操作带 wiki_mode=read；普通聊天省略。维护按钮直接调用持久 wiki/maintain，不能把排队工作伪装成同步 agent 问答。

验收：普通保存不触发 Weber；普通聊天不查库；查询引用能定位当前正文；超过单段上限可以继续读取；新材料整理不改任何旧文；创建清单伪造、旧版本和重名不会覆盖；人工修改后的整理区保留并提示复核；维护取消/重试、历史恢复和绑定认证正常；保留当前移动端、Markdown/公式/附件与 Git 功能。最后在真实应用页面验证，报告实际改动与结果。
```

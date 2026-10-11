# Octopus 本地共享知识库：agent 接口与工作流来源笔记

核验日期：2026-10-11。用途：支撑设计访谈；Q1–Q16 已确认设计共识，当前进入 spec/ticket 评审，尚未实施或部署。
范围：官方协议、格式规范、第一方工程资料；保留现有 Octopus、Sapphire 与用户工作区。
文中“官方事实”是来源直接支持的内容；“项目推论”是结合当前代码给出的设计判断。
没有统一的“所有项目最新最佳实践”；下面列出可核验的原则与本项目需要作出的选择。

## 1. 官方事实：接口与上下文

| 主题 | 来源直接支持的事实 | 一手来源 |
| --- | --- | --- |
| MCP 版本 | `latest` 当前重定向到 `2026-07-28`。该版移除了协议级 session 与 initialize 握手，改用逐请求元数据；这是协议变更，不代表当前 CLI/SDK 已支持 | [规范](https://modelcontextprotocol.io/specification/2026-07-28)、[变更记录](https://modelcontextprotocol.io/specification/2026-07-28/changelog) |
| Tools | 工具设计为由模型发现、选择和调用；服务端必须校验输入并执行访问控制；工具 annotations 的可信度依赖来源，不能替代权限校验 | [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) |
| Resources | 资源通过 URI 标识，适合文件、应用数据等上下文。宿主决定如何纳入上下文；支持列举、读取及可选更新通知，不能假设列举即自动加载 | [Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources) |
| Agent Skills | 一个包含 `SKILL.md` 与可选资源的目录。渐进加载：先名称/描述，再激活后的指令，最后按需读取 supporting files | [Agent Skills 规范](https://agentskills.io/specification) |
| Skill 与 MCP 的职责 | OpenAI 官方将 MCP 的数据、认证、授权与动作，和 skill 的可复用流程、示例、模板及参考材料区分；两者可协同使用 | [OpenAI Skills — Plugins](https://developers.openai.com/plugins/concepts/skills) |
| Codex Skill 触发 | 常驻的是名称、描述和路径，使用时才读取完整指令；支持显式和按描述隐式调用，`allow_implicit_invocation: false` 可关闭隐式调用。此机制仍需在 Octopus 的 headless 回合中验证 | [OpenAI 官方 Build skills](https://learn.chatgpt.com/docs/build-skills) |
| Skill 路由质量 | 2026-09-11 的第一方实践建议缩短描述、准确限制触发范围，并把多流程 skill 根文件作为按需查阅的路由，避免堆积无关指令 | [OpenAI skill 与 prompt 实践](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra) |
| Skills over MCP | 官方扩展 `io.modelcontextprotocol/skills` 已发布，SEP-2640 为 Final。通过 Resources 读取文件；仅调用 `resources/read` 读取 `SKILL.md` 不等于激活；官网明确 SDK/宿主支持仍在实现中 | [扩展说明](https://modelcontextprotocol.io/extensions/skills/overview)、[支持矩阵](https://modelcontextprotocol.io/extensions/client-matrix) |
| 按需检索 | Anthropic 建议保留轻量入口标识，用工具逐步获取相关上下文；预检索与运行时探索可以混合，选择依任务而定 | [Context engineering，2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) |
| 工具设计与评估 | 第一方经验建议减少职责重叠，以真实任务验证工具；长结果提供过滤、分页、范围选择或明确截断；同时观察正确率、耗时、工具调用与 token 消耗 | [Writing effective tools，2025-09-11](https://www.anthropic.com/engineering/writing-tools-for-agents) |

这些是接口语义与工程经验，不是“必须购买某框架”或“必须建立向量库”的规范。
本次不建议顺带升级 MCP 协议；现有两种 CLI 能稳定调用的接口，应先保持兼容。

## 2. 项目现状：本地代码直接支持的事实

以下检查针对当前工作树；其中已有用户未提交改动，未重置、覆盖或提交。

- [mcp_http.py](../../server/mcp_http.py) 挂载内置 MCP namespace，并将同步工具放在线程执行；新增知识 namespace 有现成接缝。
- [assembly.py](../../server/harness/assembly.py) 选择 agent 启用的内置 MCP 集合，两个 harness 共用组装过程；未知 key 会过滤，并非任意新 namespace 自动生效。
- [mcp_identity.py](../../server/mcp_identity.py) 的签名 bearer 携带 `session_id`、`user_id`；[ask_agent.py](../../server/mcp_servers/ask_agent.py) 是根据验证后的 session scope 调用 REST 的既有薄适配器模式。
- [Application backend 合约](../plans/application-backends.md) 已支持独立后台进程、HTTP 代理及 `code/.data/.runtime` 生命周期拆分；后台按请求启动并可能因空闲停止。
- [agent_memory.py](../../server/agent_memory.py) 的 memory 按 agent 分开；它目前不是所有 agent 共享的知识库。
- [memory.md](../plans/memory.md) 记录旧的 memory MCP、DB 与每轮索引注入方案已经撤回。当前 [assembly.py](../../server/harness/assembly.py) 仍每轮加入读取私有 `MEMORY.md` 的协议，但没有把共享 KB 正文直接拼入 prompt；知识库接入不得恢复旧的全库摘要注入。
- Sapphire 的 [README](/home/jack/.octopus/users/f527ad605dd4/applications/sapphire/README.md) 指定工作副本为主库；它已提供 Markdown 编辑、搜索、Git 与版本冲突处理，超出了纯展示 app。
- Sapphire 的 [server.py](/home/jack/.octopus/users/f527ad605dd4/applications/sapphire/server.py) 已有 `file/search/status/links` 接口；保存使用 SHA-256 revision。当前搜索为路径/文本子串匹配，最多返回 80 项，并非语义或混合排名检索。
- [workspace.py](../../server/workspace.py) 约束服务端工作目录等路径；[codex.py](../../server/harness/codex.py) 的完整 turn 当前使用 `--dangerously-bypass-approvals-and-sandbox`。仅在 MCP 中限制 writer，不能证明普通 agent 的原生 shell 无法写同一主库。

## 3. 项目推论：四种角色可以组合

| 层 | 负责什么 | 在 Octopus 中的建议边界 |
| --- | --- | --- |
| 主库与服务 | 持久化、检索、版本、冲突、权限与审计 | 一个权威写入入口；UI 和 MCP 共用它的结果与规则 |
| MCP | agent 调用服务的机器接口 | 以少量明确工具暴露检索、读取、提交成果和维护动作 |
| Skill | 何时检索、如何判断证据、怎样引用与交稿 | 按任务加载规则/模板；不承担存储、权限或服务发现本身 |
| App | 人的查阅、编辑、入库与维护界面 | 可复用 Sapphire，展示主库实际版本和待处理状态 |

因此“做成 app”与“用 skill 交互”并不冲突，也不足以独自回答共享访问问题。
知识库可以有 Application 界面和后台；所有 agent 的一致入口仍需要可调用的服务/API。
如果两个 CLI 暂不能统一原生 Skill 加载，可以先用已验证的短指令与按需读取规则，不能把当前桌面会话的技能目录当成 Octopus 自动集成。

### 已选择：Octopus 共享模块，Sapphire 知识工作台

用户在 Q6 选择 Octopus 原生知识模块：主库操作、检索、提取、版本、授权、审计及维护任务跟随框架生命周期；Sapphire 调用同一服务供人阅读、编辑、提交与管理。
沿用 Sapphire `.data` 的主库，不另建第二份权威知识内容；检索索引和提取缓存是可重建派生数据。
共享模块采用聚合能力，不替换现有 harness、应用、委托、记忆和账户机制。

这适合本项目中长期、跨 agent 的共同能力，是结合现有代码的工程判断，并不是协议规定知识库必须归框架所有。
复用 Sapphire 后端提供全部 KB 服务也曾作为候选；现已选择共享模块，以集中 session/account 作用域和维护生命周期。
Sapphire 现有保存、导入、删除与恢复等写入入口须接入共享版本、锁与审计规则，不能让 app 和框架各维护一套独立写入逻辑。

## 4. 项目推论：agent 日常交互

仅在显式 `/wiki` 任务或用户启用本次知识库开关时，普通 agent 采用 `search → read → 必要时继续搜索 → 带证据回答`。
Weber 负责入库整理、来源校验、去重、正式发布与冲突仲裁；复杂整理可使用现有 `ask_agent` 委托。
这样其他 agent 查资料无需等待 Weber 的新一轮模型回复，Weber 也无需承担所有读取流量。

候选工具契约如下；这是语义草案，不是已经批准的 API 清单：

| 候选动作 | 建议输入/输出 | 用意 |
| --- | --- | --- |
| `knowledge.search` | query、范围/状态过滤、cursor；返回标题、片段、doc_id、revision、出处与分页信息 | 少量高信号候选；截断可继续查询 |
| `knowledge.read` | doc_id、可选 revision、章节/范围；返回内容及精确定位 | 支持原文核验与版本引用；不存在/过期应可恢复 |
| `knowledge.submit` | 成果、来源、目标主题、idempotency key；返回提交编号 | 普通 agent 交稿到 inbox/draft；避免重复沉淀 |
| `knowledge.publish/update` | Weber 身份校验、目标、expected revision、变更说明 | 服务端条件写入；版本变化则返回冲突并要求重读 |

工具名称最终跟随 Octopus MCP 命名；可将必要章节读取合并进 `read`，减少重叠工具。
若当前客户端确实支持，可加 Resources/URI；保持 `read` 工具可用以覆盖资源发现不足的客户端。

每次引用建议至少携带：库 ID、文档 ID/路径、revision、章节/页码、原始来源与来源时间。
原文事实、agent 分析、用户确认的决定分别标记；摘要是入口，不能悄悄替代原始证据。
文件移动要能追踪同一文档；草稿、有效、被替代等状态参与检索过滤；旧结论保留历史。
Git 记录与单文件 SHA-256 revision 职责不同：前者追踪历史，后者校验此次更新的读取基础。

检索引擎按真实问题评估：Sapphire 子串搜索可作为现有基线，必要时增强结构化过滤、词法检索、语义检索或混合排名。
中文短词须专门核验；SQLite 官方说明 FTS5 trigram 的全文查询无法匹配少于 3 个 Unicode 字符的子串，不能把默认分词器当作中文检索已解决。[FTS5](https://www.sqlite.org/fts5.html)
是否加入 embedding，应由同义改写/跨语言问题的实际漏检与成本决定；搜索机制独立于 MCP/Skill。

## 5. 官方事实与项目推论：权限和不可信内容

**官方事实。**MCP 安全文档要求作用域最小化，反对把 token 中的 scope 声明当成完整的服务端授权；代理场景必须避免未校验 audience 的 token passthrough。[MCP Security Best Practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)

**官方事实。**Anthropic 的 2026 年工程经验将模型指令、环境边界与外部内容分开防护；审核过的 connector 仍可能载入被污染的文档，模型层措施不能独立保证安全。[How we contain Claude，2026-05-25](https://www.anthropic.com/engineering/how-we-contain-claude)

**官方事实。**Skill 可以携带代码和指令；来源不可信的 Skill 应检查后使用。Skills over MCP 的 digest 校验确保与 manifest 一致，不证明内容可信。[Skills 工程说明](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)、[扩展完整性规则](https://modelcontextprotocol.io/extensions/skills/overview)

**项目推论。**“所有 agent”默认应理解为同一账户内获授权的 agents，跨账户分享需要独立明确。
caller 的 user/session/agent 从已验证 scope 解析；不能信任模型传入 `writer='Weber'`。
普通 agent 只读正式库并提交草稿；Weber 的写权限需限定内容范围，账户/规则/凭据管理无需随之放开。
是否要求人逐篇确认发布、能否直接维护已批准范围，属于用户的策略决定，不应默认强加额外确认。
用户在 Q15 选择日常编辑统一经 Sapphire 或知识接口，不保留 Obsidian 等编辑器作为并行的日常写入路径。
这使受控写入共享版本检查；原生 shell 仍有文件访问能力，不能把工作流约定写成操作系统强制隔离。
用户在 Q9 选择可信同账户协作与接口授权，不新增操作系统级强制写入隔离。框架检测到意外外部变化时应使索引失效、重新读取并保留冲突结果，不能静默覆盖。
网页/PDF/笔记正文应作为证据数据，不能提升成 system 指令或自动安装 Skill；受信流程说明与导入内容分开。
维护日志记录 caller、来源、变更、expected/new revision；工具不把整个知识库或凭据带入上下文。

## 6. 设计访谈决策记录

第一轮 Q1–Q4：用户于 2026-10-11 均回答“是”。

1. 目标：保留原始材料，Weber 整理持续更新的跨来源主题 Wiki，各 agent 在实际任务中检索并引用，同时保留人的浏览与编辑。
2. 主库：优先沿用 Sapphire 当前 `.data` 中的工作副本。可以借鉴 my-wiki 对文章总结和逻辑组织，以改善检索与阅读；现有正文按 Q7/Q12 逐字保全。
3. 共享：当前 Octopus 账户下现有及未来 durable agents、委托会话和应用调用。CLI 原生子 agent 的工具/skill 传播需单独验证，不能默认等同于 `ask_agent` 创建的真实会话。
4. 职责：普通 agent 可读和提交材料/建议；Weber 自动整理和发布有来源、可追溯、可回滚的知识。用户观点、有效项目决策和原件删除需要明确授权。

用户补充的确定约束：不要在不需要本地知识的任务中强制注入统一 memory 或全库总结。共享知识通过任务相关的按需调用获取，不能让低质量总摘要成为所有任务的默认依据。

第二轮 Q5–Q9 已决定：

5. **仅在用户明确点名知识库时调用。**用户强调 Octopus 包装了 CLI，不能直接把原生 Codex skill 的发现和触发视作完整接入。常驻只保留简短能力描述，不放全库摘要、目录内容或文章。
6. 知识能力放在 Octopus 共享模块；Sapphire 是人的阅读、编辑、入库与维护界面。参考 my-wiki 的 app 增量本次只给建议；实际 app 开发在 Octopus 内进行。
7. Weber 可以按规则重构既有正文的组织、补充模板字段与有来源的说明，但不允许修改原内容。精度由 Q12 明确为逐字保留。
8. 新材料在用户或 agent 明确提交后进入 Weber 维护队列；普通聊天不自动沉淀。职责与方案确认后，完整整理当前旧库，范围由 Q14 确定。
9. 同账户可信协作；接口落实角色、版本、审计与恢复规则。不新增操作系统级强制隔离，不承诺原生 shell 无法接触主库。

第三轮 Q10–Q11 已决定：

10. 使用 `/wiki <问题或任务>` 与 composer 的“本次使用知识库”开关表达结构化意图；不通过文章、任务报告或 app context 中的关键词猜测授权。
11. 授权只限当前知识任务及必要委托；下一条普通消息不自动沿用。已读资料仍在历史中，停止再次加载不等于从模型历史中删除。

第四轮 Q12–Q15 已决定：

12. **现有原文逐字保留。**段落、数字、公式、代码和引文不可改写、删减或纠错；可重排章节、补标题和元数据。新增摘要或解释明确标为 Weber 整理并注明来源。原文错误/矛盾另行标注，无法取得来源的字段留空或标记待核验，不补造内容。
13. 旧库保留文件名、目录与附件路径。新增主题入口、概念页、元数据形成逻辑组织，正文整理不批量搬文件；确需迁移时作为有链接检查的独立任务。
14. 首轮处理 Markdown、PDF、TXT、HTML 及关联图片。PDF 保留原件，建立页级提取与质量记录；视频、源码、配置、字体等保留为资产，不自动转写或蒸馏。
15. **日常编辑统一经 Sapphire 或知识接口。**用户没有接受保留外部编辑器并行写入的建议。受控入口统一版本与冲突检查；应检测意外外部改动，但不把原生 shell 访问宣称为被强制阻止。

### 适配包装层的设计机制（尚未实施）

- [ChatView.tsx](../../web/src/components/ChatView.tsx) 拦截现有斜杠命令，未命中的输入按普通 WebSocket content 发送；没有原生 Codex `/skills` 管理面板。`$skill` 文本可能由底层 CLI 解释，但它本身不是 Octopus 的调用授权字段。
- [start_message](../../server/sessions/turns.py) 将消息变成 `QueuedPrompt`，当前仅带正文与附件。知识请求的结构化元数据应随消息入队，不能暂存为 session 全局布尔值。
- [assembly.py](../../server/harness/assembly.py) 已是 persona、工具说明、记忆协议的共同组装点；可在显式任务中加载短查询/保存/维护规则，接入两种 harness，无需先搭建通用 skills 平台。正常任务不追加知识库指南或正文。
- 修改知识工具集合或指南时，[HarnessRun.spawn_signature](../../server/harness/run.py) 需要覆盖其有效配置，保证 Claude 不复用旧配置的 held process。知识模式不同的 steering 请求需作为新的 queued turn 处理。
- Scheduler 的新会话直接调用 `send_message`，已有会话走 `start_message`；两条路径都需携带任务意图。App 和委托同样采用明确的结构化标志，不扫描报告、文章或 app context 中的关键词来授权。
- Codex 每轮重建 `developer_instructions`，可以停止再次发送知识库指南；同一 `resume` 的历史资料仍存在。受控接口如果要在任务完成后撤回访问，还需任务级授权，而不只依赖当前 session/user bearer。
- 任务授权在受控接口校验，按查询、提交与维护动作区分；普通用户查询不因 `/wiki` 自动获得 Weber 的发布权。已明确提交的材料、获授权的首次整理可成为持久维护任务，不能由正文中的命令自动创建任务。
- Weber 的指南只在获授权的维护任务中加载，不能令 Weber 每条普通消息自动调用主库。原生子 agent 的授权传播须真实验证；未支持时用已有 Octopus 委托会话。
- 接口授权与操作系统强制隔离是不同保证，Q9 已接受前者的合作边界。不能把工具未暴露误写成同 OS 用户的 shell 必然无法访问主库。

## 7. 旧库与 Sapphire：只读核查及界面建议

实际主库为 `/home/jack/.octopus/users/f527ad605dd4/applications/sapphire.data/vaults/obv/repo`。
排除隐藏目录与缓存后，共 780 个文件、约 544.03 MiB：143 个 Markdown、96 个 PDF、71 个 TXT、18 个 HTML、38 张图片、16 个 MP4，以及源码、配置、字体等资产。
Markdown 约 1.51 MiB，PDF 约 387.70 MiB。首轮整理需要考虑 PDF 页级提取与质量记录，不能只给 Markdown 套模板。
本轮未安装提取依赖，尚未判断每个 PDF 是文字型还是扫描型；提取结果质量不足时应明确进入待修复状态，不能把空提取当作完成。

Sapphire 已有 Markdown 编辑/预览、双链、目录、任务框、收藏、阅读位置、PDF 阅读、上传、回收站、Git 历史/diff/同步、基于 SHA-256 的写入冲突检查。
下列是借鉴 my-wiki 的 app 建议，**不是本轮实施清单**；app 建设仍在 Octopus 内执行：

| 建议 | 人的用途 | 框架提供的能力 |
| --- | --- | --- |
| 统一搜索与定位 | 同时搜索概念、来源与原文，筛选类型/主题，跳到章节或 PDF 页 | 共用索引、排序、片段、稳定定位与 revision |
| 来源侧栏 | 从概念追溯到来源、原件、页码、提取质量及来源版本 | 来源关系、版本与页级定位，复用现有双链界面 |
| Weber 维护工作台 | 查看待整理、处理中、待修复、失败原因；按选择范围发起整理、取消、重试 | 持久任务、进度、重试、修订记录与现有委托会话 |
| 文档变更与影响 | 看正文/补充内容 diff，知道哪些概念受影响，恢复为新版本 | 文件 revision、派生关系、审计与冲突恢复；复用现有 Git 和回收站 |
| 材料收件箱 | 提交文件/URL/Markdown 与附件，识别重复并保留原始来源 | 安全导入、去重、提取与维护队列 |
| 逻辑资料库与主题页 | 按主题、类型、状态筛选，跨目录阅读，不搬现有文件 | 元数据、概念与来源多对多关系，派生入口 |
| 当前页面请 Weber 处理 | 对当前页提问或明确请求整理，在真实 agent 会话中查看结果 | 复用 app-agent 通道，结构化表达本次 `/wiki` 意图 |

my-wiki 依据：[README](/home/jack/ref/my-wiki/README.md)、[维护工作台](/home/jack/ref/my-wiki/assets/dashboard/src/main.tsx)、[来源版本界面](/home/jack/ref/my-wiki/assets/dashboard/src/DocumentVersions.tsx)、[文件资料库](/home/jack/ref/my-wiki/assets/dashboard/src/OriginalsDrive.tsx)、[材料入口](/home/jack/ref/my-wiki/assets/dashboard/src/WorkspaceActions.tsx)。
局部概念关系图可以作为阅读辅助，由 Markdown 中的关系派生，避免增加第二套图数据真源；比全库图优先级低。
不复制 my-wiki 的模型接入、会话管理或独立 agent 层；沿用 Octopus 的 harness、app-agent 与委托接口。

### Weber 首轮整理的建议执行规则

- 整理前记录原件清单、内容 revision 与可恢复快照；规划增量目录和模板，先产出可审阅的变更说明。备份不以 Git diff 代替，二进制原件也须可恢复。
- Markdown 的既有正文、前置字段和嵌入链接按原始片段保全；重排片段时验证每个原始片段逐字保留。摘要/概念是新增层，不能取代原始段落。
- TXT/HTML/PDF 保留原件；提取正文与页级出处形成可重建缓存，来源条目记录质量、问题与版本。扫描或损坏材料不得伪装为已成功整理。
- 补充标题、来源、类型、整理状态、概念关系；缺失来源标记待核验，原文矛盾保留并说明，不擅自确定用户立场或替换有效决定。
- 发布前重读 revision；冲突保留待处理成果，重算受影响范围；原文保全、链接检查通过后写入。自动结构检查不能证明新增摘要语义正确，Weber 仍需逐项核对来源。
- 全部范围逐项登记完成/需要人工处理的状态、原因与产物；可被完整核查，不能只抽样后宣称整库整理完成。

检索策略与验收要覆盖：已知文档定位、中文同义提问、跨来源综合、旧决策识别、保存与冲突恢复，以及无关任务不读取/调用 KB 的反例。

后续用两种 harness 跑同一组真实问题，检查来源准确性、过期/冲突恢复、遗漏与延迟。

## 8. 工作流纠正与当前状态

Q16 确认后曾直接编写六个未集成的实现/流程草稿，并在 `.venv` 安装 pypdf 6.20.0。
用户指出应先经过 spec/tickets 阶段；源码草稿已移至 `/tmp/octopus-wiki-unreviewed-20261011`，本轮源码及依赖声明改动已撤回。
pypdf 仍留在 `.venv`，未将该安装视为技术方案或验收通过；未修改实际 Weber 配置、主库或运行服务。

当前顺序为 grill → spec 评审 → tickets 拆分评审/发布 → implement → 框架验收 → 实际 Weber 配置与真实验证 → 首次旧库整理 → Sapphire Iterate prompt。
[规格评审稿](../plans/local-shared-wiki-spec.md)和[任务拆分提案](../plans/local-shared-wiki-ticket-proposal.md)已形成本地文件，尚未发布 tracker 或标记 ready-for-agent。
Weber 的职责和触发边界现在进入规格；其实际 persona 配置只能使用已经验收的接口，并先于生产旧库整理完成真实验证。

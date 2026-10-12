# 共享 Wiki v2：核心交付与 Octo 接手

> **Status:** in-progress — 本轮核心代码、独立审查与必需本机检查已交付；未部署、未执行真实 Weber 整理，下一阶段由 Octo 接续。

## 1. 本轮交付与停止位置

用户于 2026-10-13 调用 implement，随后明确：**开发推进到需要真实 Weber 整理时停止，整理交付文档，由 Octo 在框架内协调 Weber。** 执行基线为 `5af94014369db8328411e239a889907593336664`。设计与范围以[总纲](local-shared-wiki-v2.md)、[任务票](local-shared-wiki-tickets-v2.md)及 Q54～60 的最新回答为准。

| 任务 | 本轮事实 | Octo 接续 |
| --- | --- | --- |
| V2-01 | 账户隔离、生命周期、版本迁移、增量索引、当前读取与后台补偿已实现 | 部署后核对真实库状态与请求成本 |
| V2-02 | OKF 解析、角色、uid 别名、直接来源、原位规范、定点维护与持久回执已实现 | 由实际 Weber 判断 Key note、关联和正文保全效果 |
| V2-03 | intent、compact、read_many、覆盖选择及分页已实现；旧 full 调用兼容 | 用真实问题检查候选覆盖、引用、复用与成本 |
| V2-04 | 框架能力、分层指南与 persona 合并稿已实现 | 合并既有 Weber 配置，验证实际 worker 身份和装配 |
| V2-06 | 隔离机械测试与 Codex 实机检查；真实文章试点尚未执行 | 约六份代表材料的真实闭环与批次放行记录 |
| V2-08 | 未开始生产批量模板化 | V2-06 通过后按清单分批处理 pika_vault |
| V2-05 / 09 | Sapphire / SmartReader 新功能保持 feature plan | 另行实施，不阻塞少量试点或旧资料整理 |
| V2-07 | 未加入语义适配器 | 达到已约定真实检索缺口条件后再决定 |

本轮没有向配置中的实际 Weber 发送任务，没有修改其 persona、真实文章或生产 job，也没有重启部署服务。隔离测试使用临时 agent、合成材料和内存数据库；测试 agent 名称 Weber 不等于用户现有 Weber。用户原有 `docs/sapphire-wiki-iterate-prompt.md` 修改保留，不纳入本次提交。

## 2. 已核对的运行绑定

2026-10-13 的只读核对结果如下；接手时重新通过框架核实，避免把交接时的记录当作永久配置。

| 项 | 值 |
| --- | --- |
| account | `f527ad605dd4` |
| curator_agent_id | `f37612d667ec`，现有 Weber，Codex，未归档 |
| vault_id | `6bdb9fe5-3453-47a8-8510-9a892c510346` |
| owner app_id | `3e7830a1336b` |
| root | `/home/jack/.octopus/users/f527ad605dd4/applications/sapphire.data/vaults/obv/repo` |
| 当前模板化目标目录 | `Library/pika_vault`，以逐批明确文件范围执行 |

主库目录存在，运行服务 `/health` 正常；这些只证明现有服务可达，**不证明本次代码已上线**。当前开发对话没有 Octopus 委托工具及会话授权，因此没有伪造实际账户凭据或直接写生产数据库来代替框架协作。Octo 在自己的真实会话中使用现有任务入口。

## 3. 实现入口与契约

| 入口 | 最终行为与位置 |
| --- | --- |
| 服务对象与迁移 | `registry.py` 按账户、state、vault、root、绑定复用对象，有界回收；`schema.py` 事务化迁移至版本 7，保留身份、历史、prepared journal 和 tombstone |
| 索引更新 | `events.py` 使用 Linux inotify；`store.py` 消费受影响路径、保留目录改名身份；启动及每 30 秒后台补偿，`OCTOPUS_WIKI_RECONCILE_SECONDS` 可配置 |
| 元数据 | `format.py` 统一 YAML / Markdown AST 解析，保留原字坐标、未知字段与旧格式；原生 type 开放，未知角色不猜作者或可信度 |
| 来源与链接 | `catalog.py` 索引实际直接依赖、明确表示关系与普通导航边；外部出处不冒充已读取，不做完整来源根闭包或自动证据计数 |
| 检索 | `retrieval.py` / `search_index.py`：精确身份、真实标题/别名、词法检索、compare 对象覆盖、overview 明确标签分面、明确副本去重；模板/源快照不挤占默认领域检索 |
| 当前证据 | `batch_read.py` 读取实际当前版本，同文重叠范围合并返回；失败项独立，只有实际返回内容计入 read ledger |
| 受控写入 | `normalize.py` 原位插入缺失结构；`raw_blocks.py` 原始位置；`revise.py` 定点补丁、来源复核或 no_change；`operations.py` / `history.py` / `jobs.py` 提供持久回执与恢复 |
| 框架指南 | `workflow.py` 与 `prompts/*-v2.md`；新能力只由新代码加载。旧 `query.md` / `maintenance.md` 保持基线，避免未重启进程动态加载新协议 |

普通查询和保存不会自动整理。核心热请求不全库 walk；已知单篇保存更新对应索引。事件遗漏、溢出和进程重启由后台对账补偿；目录不可用或枚举失败保留账本并报告状态，不推断全库删除。Linux 事件语义依据 [inotify 手册](https://man7.org/linux/man-pages/man7/inotify.7.html)，事件只给变化线索，当前正文仍以实际文件与 revision 为准。

### 3.1 读与检索

- `GET /api/wiki/search`：`intent=lookup|question|overview|compare`，`view=compact|full`；省略保持旧 full 兼容。compact 是 `freshness=indexed` 导航，不授权或证明阅读。`documents` 同样支持 compact。
- `POST /api/wiki/read-many`，app 相对路径 `POST wiki/read-many`，MCP 工具 `read_many(items, budget_chars)`。一次 1～20 项，单项 length 最大 24,000，整体字符预算最大 96,000。旧下划线路径仅兼容别名。
- batch 的实际正文在 `ranges`；`documents` 给文档元信息，`items` 给独立状态、range_id、返回 start/end 和 next_start。重叠内容只返回一次；changed/not_found/excluded/budget_exhausted 不算已读。
- search/documents/graph 的 cursor 绑定过滤条件及 index_epoch；epoch 改变返回 409，从第一页重启，改变条件或同时传非零 offset 返回 422。
- read 与 read_many 的 offset 是提取文本 Unicode code-point 零基半开区间；行号一基。全文评价从 0 连续覆盖至结束，不能拿导航摘要代替正文。

```json
{
  "items": [
    {"doc": "Library/example.md", "start": 0, "length": 16000, "expected_revision": "当前实际 revision"}
  ],
  "budget_chars": 24000
}
```

上例为请求形状示意，不是已执行的生产请求。overview 当前只利用明确标签分面和检索命中，不声称具备自动语义聚类；候选的实际覆盖质量在 V2-06 验证。标签过滤、tags 面板和多 app client 授权仍属后续票，info 未宣告这些能力。

### 3.2 模板化与定点维护

当前 MD 原位成为正式文章；不创建永久 `.source.md.txt`。完整读文后调用 `organize`，**显式传 frontmatter_patch** 选择新版保全路径。共同字段为 title/description/type/x_wiki.profile/body_dialect，tags/aliases 独立传；省略保留已有字段与出处。服务分配或保持 uid、记录实际 generated；模型不能通过 patch 写 verified。

article-v1 使用真实 H1、`## Key note` 与 `## 正文`。缺失摘要由 Weber 理解后生成，已有好摘要传 key_note=null 并按实际判断复用；已有 Key note 但缺正文分界时，需要明确 raw 结构操作，服务不会猜边界。保留正文措辞、条件、反例、公式、代码、引文与媒体引用。个人档案、Concept/Topic 按自身用途组织，不机械套文章模板。

TXT/HTML 转换保留原件，创建获准的配对 Markdown；已有配对页按当前版本独立维护。新版 worker 不走旧 `original_text` 删除再生成路线；旧 organize 请求为兼容仍保留，不能把它当日常定点维护默认动作。

`blocks(view="locators")` 只定位，不算读；随后用 ids 读取实际原始块。raw 坐标独立于 read 提取 offset。MCP `revise` 参数为 `request={...}`，REST `/api/wiki/revise` 直接接收该对象：

```json
{
  "doc": "当前文档 id",
  "expected_revision": "当前实际 revision",
  "operation": "patch",
  "reason": "已确认单位笔误",
  "edits": [{
    "locator": "当前 blocks 返回的 locator",
    "expected_block_hash": "当前 sha256",
    "start": 12,
    "end": 17,
    "expected_text": "10 ms",
    "replacement": "10 s",
    "kind": "correction"
  }]
}
```

start/end 在对应原始块内，必须与实际字句和 hash 一致；上例位置为示意。correction 范围仅覆盖实际错误字句，不能传整个正文块后靠共有开头声称保全。addition/annotation 使用 start=end 插入；补丁不能重叠。服务核对当前版本、assignment 与已读上下文，拒绝整文/整块正文替换；明确标题纠错允许。未涉及字节保持，思想与风格的语义保全仍需检查实际 diff，机械保护不是质量认证。

`confirm_sources` 只更新实际重读的既有直接依赖及其定位，不刷新未检查部分或整篇可信度；`no_change` 要求当前目标真实完整读取，持久记录无变化回执，不造文本 change。运行态 sources 使用 `{doc,revision,location}`，与 OKF 文件 `{id,resource,...}` 分开；当前文章来源可以为空，新建文章省略运行态 sources 也保留输入中已有的可移植出处。

每次操作记录 receipt_id、job/item、目标前后 revision、实际输入来源版本、operation/reason/change_id。job 完成核对输出与索引，不能只凭模型“已完成”。文件已提交而索引失败时恢复索引，不再生成或重放正文。取消保留已提交结果；恢复或重试沿真实版本与已提交回执推进。多输入任务的新页面用 item_doc 指明所属项。

## 4. 既有 Weber 配置合并

`prompts/curator.md` 是仓库参考稿，修改该文件不会自动更新实际 agent 的 system_prompt。Octo 先读取现有 `f37612d667ec` 的非敏感 persona，对“本地知识”段作可审阅合并，保留智者、读书/思考伙伴身份与用户其他定制。**backend、model、reasoning、凭据、私有 memory、默认会话和其他工具配置不随此合并改变。** 不用整份通用稿覆盖真实 persona。

建议合并的核心段如下：

> 仅在 Octopus 明确启用知识任务时，使用本次提供的知识工具与工作流；主库、Key note、分类、关联和发布规则以当前任务指南为准。保留已有思想、论证、条件、反例和带有风格的观点。当前文章模板化保持原措辞，已有好摘要沿用；明确维护时按当前版本原位纠错或补充，未涉及字句保持，不重写正文，不按 AI／人类作者或过去人工编辑限制维护资格。无需修改时记录真实 no_change 回执。用户直接提供的文章可在对话中讨论和总结；读取旧库与把讨论写入主库遵循框架本次明确提供的意图。

常驻 persona 不装全库摘要、个人事实、维护流水账或实际路径列表。普通 read task 装 query-v2 + retrieval-v2；维护入口只装 entry；有效 worker 装 maintenance-v2 + retrieval-v2。worker 通过 `load_guide(module="article")` 按需加载格式约定，生成 Key note/解释/新文字前加载 writing；普通阅读或单纯纠错不常驻写作稿。权限由服务校验，不由这些文字产生。

## 5. Octo 部署、迁移与回滚

1. 读取本提交及工作区状态，保护用户未提交修改。先检查已有 job，避免重复排队。确定没有进行中的写入后，通过既有框架保存主库配置、Wiki durable state 与 Vault 的一致恢复点；不删除 `index.sqlite3` 或旧历史来“重建”。
2. 按 [本地 WSL 安装](../local-wsl.md) 使用现有 `.venv`、pip 与本机 constraints；新增依赖为 `markdown-it-py>=3.0,<5`，本轮安装并验证 4.2.0。不使用 uv，不重做登录，不把认证或个人正文提交进仓库。
3. 由 Octo 在实际获准部署时执行 `systemctl --user restart octopus`，等待 `/health`；如果需要更新 SPA，再执行 `cd web && bun run build`。本机是 user service，后端先于前端构建更新。本文没有执行这些生产命令。
4. 新进程启动进行事务化 schema migration 与后台对账。核对 `/api/wiki/info` 的 vault/curator、capabilities、index_status/index_errors/index_epoch；检查新 read-many、compact、guide 与当前 revision，不只看 health。保存原有 owner app 绑定，不添加未实现 client_apps。
5. 通过真实 agent 管理接口合并 Weber persona，再提交一个明确范围的小样本 job；验证 worker session 的 agent_id 确实为既有 Weber，任务指南与工具来自新能力。

回滚代码与回滚内容分开处理。新版 schema 检测比自身更高的版本并拒绝继续，保留账本；旧版本没有新写入协议的保证，不能直接启动旧 writer 接管迁移后的状态。部署前留一致恢复点，出现问题先停止新的维护，在当前版本检查 prepared recovery 或索引错误。确需回到旧版时，协调恢复匹配的代码与一致 state/Vault，保留期间新产物供核对；不要用旧备份直接覆盖之后的用户编辑。派生索引可重建，身份/历史/回执不可当缓存删除。

## 6. 本轮验证与审查

2026-10-13 完成本轮必需本机检查。只将成功结束的检查计为通过；修复后复验与不可用 provider 分开列示。

| 检查 | 当前结果 |
| --- | --- |
| `scripts/check.sh` | 最终八关全部通过 |
| hermetic backend | 1591 passed / 43 real deselected，60.35 秒；real 另行运行 |
| frontend unit / type / lint / contracts | 334 passed；tsc -b --force、eslint、ruff、mypy、生成合约与 docs index 均通过 |
| pure UI E2E | 71 passed，无跳过 |
| Codex real CLI | 18 个必需用例均有通过结果：完整轮 17 passed；职业来源范围修复后，背景/职业/领域术语三项沿原断言复验 3 passed，无跳过 |
| Codex browser | 1 passed，无跳过，15.5 秒；临时服务与 Wiki 状态 |
| Claude live | provider 不可用：实际请求返回 402 insufficient balance；未计通过，也未运行依赖该 provider 的完整 live/browser 桶 |

额外核对的 `real and claude_bin and not real_claude` 选择到 native-copy 用例，虽然标记为 bin，它实际发起模型请求；同样因 402 失败。按本机 AGENTS 的 Codex 验证约定，该不可用 Claude 检查单列，不冒称通过，不通过改标记或放宽断言掩盖失败。

沙箱禁止 asyncio 内部唤醒套接字发送，最小示例确认 PermissionError；因此异步与浏览器检查在正常本机环境执行，产品没有添加绕过逻辑。初次失败均诊断、修复后复验：账户范围结构检查要求指南入口显式使用 user_id；旧实机探针只认 read/offset，已改为统计实际 read/read-many 返回范围及 cursor。真实职业问法发现读完 Me 后扩散到他人职业文章，查询指南已明确个人事实只在 Me 内取材，缺字段据实说明，领域术语仍检索全库；沿原断言复验，未放宽来源或事实检查。运行中移动指南曾使已加载旧模块的测试进程缺文件，最终指南版本固定后从新进程重跑，不将那轮失败算成通过。

### Standards

独立规范审查的前三项均已修复：目录整体改名后子文件更新的身份保全、不可 JSON 序列化 verified 元数据导致的导航失败、publish 恢复误用新目标 preimage 而遗漏实际输入版本变化。末轮发现的大写 Markdown 后缀 basename 回归已补用例修复，独立复核四项相关回归通过；Standards 剩余发现为 0。新建文章省略运行态 sources 的原生出处保全也已通过复核。对应回归检查实际身份、原字节、来源变化与任务 attention，不仅测函数实现细节。

### Spec

独立规格审查的五类问题及边界均已关闭：整块正文替换保护（包括无空行 YAML/H1/正文，以及替换仍保留共有开头的情况）、合法 ATX/Setext 标题纠错、规范 read-many URL、删除别名后的重解析、basename 歧义/带点号别名语义、worker 查询规则拆分。Spec 最后复核独立运行八项隔离 HTTP 回归通过；没有未关闭的可复现核心合同阻塞。实际思想、文风与知识价值留到用户指定的 Octo/Weber 阶段，未冒称已验收。

### 同语料 HTTP 成本

2026-10-13 在本机使用 40 篇相同合成 Markdown，每篇约 16 KB，基线 `5af9401` 与当前实现各自临时 state/内存 DB。初始化后预热，五次真实 ASGI HTTP 请求中位数；统计限 Vault 内 stat/read-open/walk，包含探针开销，未启动周期对账、未调用模型。

| 操作 | 基线 / 当前耗时 | 基线 → 当前 stat / read-open / walk | 返回 JSON 字节：基线 → 当前 |
| --- | ---: | --- | ---: |
| 已知文章 read 1200 字符 | 2.97 / 3.53 ms | 2/2/0 → 1/1/0 | 1737 → 2148 |
| search Vidu，旧 full | 9.91 / 9.34 ms | 80/12/1 → 0/0/0 | 12858 → 16208 |
| documents limit=12，旧 full | 8.53 / 6.59 ms | 80/12/1 → 0/0/0 | 5175 → 8572 |
| 当前 search Vidu，question/compact | 7.82 ms | 0/0/0 | 10031 |

这个结果证明删除了热路径无关 IO，并没有证明所有单次读更快；新 full 带更多状态，返回量增加，compact 本次比当前 full 少约 38%。它不是实际用户语料、真实 agent token/P95/端到端回答的成绩；正式性能与质量对比由 Octo/Vera 按[评估方案](local-shared-wiki-evaluation-v2.md)执行。

## 7. 实际 Weber 的下一批动作

按[迁移计划 §3.1](local-shared-wiki-migration-v2.md#31-真实-weber-链路与模板化放行)先放行 V2-06 少量试点，再放行 V2-08。不等待新的 app 开发。

先核对迁移文档六份代表材料是否仍存在：Me 档案、三篇 pika_vault 随笔/开发文章、Vidu 技术报告与 models_runtime_deep_dive。它们全部可做读/检索检查，**只向 job 提交本次确需写入且已明确范围的文件**；Me 不凑职业、不套 Reference，长报告不为计数整篇重写。旧模板 `Library/pika_vault/编程与开发/00-文章入库模板.md` 只供对照，按最新 article-v1 和维护合同执行。

实际链路：明确 `/wiki maintain` 请求 → durable job/assignment → 现有 Weber worker → 当前全文读取 → 缺失 Key note 提取或已有摘要复用 → organize/restructure/revise → history/receipt/索引 → 后续查询。Weber 自己接收请求也先提交 job；普通查询直接使用共享服务，不固定转发给 Weber。

试点记录实际 agent/session/job/item、输入输出 revision、parser/prompt/代码版本、操作回执、正文 diff 和检索结果。至少完成：

1. 一篇现有 MD 全文理解后原位规范；新增 Key note 保留主张、条件、理由、反例和作者归属，正文原话/公式/代码/媒体保持。
2. 一篇已有准确 Key note 的文章复用，不循环重写；已符合要求的文件真实 no_change。
3. 长文当前位置与完整覆盖、Me 缺字段、指定 Vidu 与宽主题/对比选材，引用实际原文；预算不足继续读。
4. 人工编辑后仍可维护、一次明确局部纠错或补充。并发冲突、取消、恢复、漏事件和索引失败在隔离副本验证，不故意破坏生产资料。
5. 一个真实研究问题确有独立用途时形成或补充知识页，再以不同问法复用，最后明确纠正；核心思想与有风格观点仍在。不强制每篇生成 Concept/Topic。

Vera 核对这些实际产物和失败断言，Weber 的“已整理”或 schema lint 不替代验收。通过后 Octo 为 pika_vault 建有限批次清单并沿同一 job/受控写入链路执行；只重做具体失败项，不因改一段 persona 文风全库重写。文章来源暂缺正常入库，后续确有总结依据时记录真实出处。

## 8. 给 Octo 的可直接接手指令

> 先读本交接及 v2 总纲/任务票，核对当前 commit、工作区、主库和已有 job。V2-01～03 与 V2-04 框架实现已交付，生产 persona/部署/内容试点未执行；不要重复开发已完成核心，也不要把隔离 Codex 测试当真实 Weber 验收。按本文部署并验证实际 capabilities，保留现有 Weber 身份和其他配置，只合并知识规则。随后在 Octopus 框架内由既有 Weber 执行明确范围的少量文章模板化、Key note 和 /wiki 维护，Vera 独立核对真实 diff、回执、当前引用、思想保全与复用。V2-06 闭环通过后再启动 V2-08 pika_vault 分批整理。Sapphire/SmartReader 新功能继续是后续 feature plan。

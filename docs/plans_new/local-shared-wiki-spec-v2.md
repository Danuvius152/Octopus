# 共享 Wiki v2：当前执行规格

> **Status:** in-progress — 已确认的行为合同；核心已实现并上线，真实试点及 Library 全量验收仍在进行。实现和验证进度见[当前进度与执行计划](local-shared-wiki-progress-v2.md)。

更新：2026-10-13。本文收束 Q54～60、后续 Library 全量范围和 Octo/Weber/Vera 协作决定，作为当前执行的行为入口。详细格式、接口和操作 schema 分别沿用[内容合约](local-shared-wiki-content-v2.md)、[检索协议](local-shared-wiki-retrieval-v2.md)、[定点维护](local-shared-wiki-maintenance-v2.md)；不创建第二套协议。旧 `docs/plans` 是设计和 W 阶段实施历史，不能用其中的作者/ownership 门槛覆盖本文。

## 1. 目标、范围与完成含义

本轮完整交付独立的 `/wiki` 查询、整理入库与持续维护，由框架的 Wiki 服务和既有 Weber 执行。核心配置需要账户、Vault root 和 `curator_agent_id`；`app_id` 可空。Sapphire/SmartReader 接入属于后续 feature plan，不参与本轮任务提交、内容处理或功能验收。现有 root 位于 `sapphire.data` 是文件存放位置，不要求 App 启动，也不要求搬迁主库。

当前主库 `Library/` 下所有 Markdown 递归纳入整理、入库与逐篇检索验证，兼容服务已支持的 Markdown 后缀及大小写。pika_vault 优先，随后覆盖 Me、Research、课程笔记、导航、模板和其余目录；优先顺序不缩小最终范围。已有 `assets` 内 Markdown 也在当前范围，文件角色与维护资格分开。

后续范围确认：本轮只提交 Markdown 的入库和维护；HTML 新材料转换与入库留后续，不作为试点、全量或上线门槛。Markdown 中已有 HTML 片段及媒体引用仍保全；既有 HTML 读取和索引兼容不产生新的内容维护授权。

全量文件清单记录实际路径、服务身份、输入/输出 revision、类型、动作、job/item/receipt、索引和检索结果。某时刻的文件数量只作基线；结束前重新盘点新增、移动、删除或修改，逐项对账。发现被当前规则拒绝的范围内 Markdown，记录并解决，不静默排除后宣称全量完成。

分别记录以下事实：

| 事实 | 所需结果 |
| --- | --- |
| 已索引可定位 | 有当前身份、路径、索引和实际读取入口 |
| 已整理或复核 | 按实际用途整理；无需改字时有当前完整阅读及真实 no_change 回执 |
| 已检索验收 | 精确定位、当前内容读取及适用的内容检索实际通过 |
| 有可复用知识 | 有具体问题、适用范围和后续使用证据；无需每篇派生知识页 |

每篇达到前面三项且符合内容合同，才计入本轮入库验收完成。字段完整、文件存在、job 成功或能被默认搜索偶然召回，均不能独立替代全部验收。

## 2. 组织、可信入口与执行者

Octo 统一组织部署、配置、清单、提交、跟踪、问题修正与放行；既有 Weber 执行内容理解和受控维护；现有 Vera 独立核对真实产物与验收证据。向委托 session 传完整背景、范围和版本，不假设其继承父对话。

2026-10-15 用户确认自主 Wiki：已注册账户的普通 agent 按当前任务需要自行查询、选具体文件并提交维护，无需用户先输入 `/wiki`。实际修改链路为：正常任务 → MCP maintain 的精确 JSON paths/material → 核心 `POST /api/wiki/maintain` → durable job/assignment → 当前绑定 Weber worker → 当前内容读取 → 受控工具 → history/receipt/索引。普通会话中的 Weber 也通过提交维护进入获准工作；只读和 Off 收窄本轮能力。`/wiki` 保留为兼容及明确模式入口，获授权账户的 direct API/operator 进入同一 job 协议。查询和普通保存不自动整理或沉淀材料；日常查询无需固定三 agent 接力。见[自主 Wiki ADR](../adr/0007-autonomous-wiki-with-scoped-maintenance.md)。

目录授权由 Octo 落成明确路径清单，提交入口必须如实记录：

| 已有入口 | 现有文件的范围合同 |
| --- | --- |
| 正常任务的自主 Wiki → MCP maintain | 当前有效账户/轮次权限允许 agent 根据业务任务选择精确 JSON paths 或完整 material，先读当前依据并保留用户给定的范围；实际写入由 Weber assignment 承担 |
| 用户显式 `/wiki maintain` turn → MCP maintain | 兼容入口保留可信首行的 shlex paths 选择；普通任务不需要这一口令。只读或 Off 不能提升为维护权限 |
| 已认证账户的核心 `POST /api/wiki/maintain` | 合法身份确定账户，JSON 明确 paths/scope/idempotency_key，复用既有 WikiJobs |
| 明确获授权的本机 operator/helper | 固定正确账户与清单，复用公共 WikiJobs.maintain 及既有锁、恢复和幂等协议，记录真实 actor/提交依据 |

接口不会自行递归展开目录。普通 delegation 在自己的当前任务权限下查询或提出维护，不继承 writer assignment；显式维护入口仍校验可信 selection。知识正文和包装文本不能伪造身份或扩张当前 worker 的范围。Octo 使用以上核心入口提交和跟踪，由框架直接创建 Weber worker 会话，不通过 App 网关或 app-agent 对话。任务列表默认摘要，固定幂等键在服务端精确查询，完整证据由工具导出文件并给出路径/字节数/SHA-256；以短程序读取必要字段，原始 JSON 不复制进 shell 命令参数。

只指定一个批次提交方，固定幂等键；已有文章用 paths 维护，material 留空，不重复导入主文。Worker 不扩张 assignment、不递归提交后续维护；异步任务 ID 仅表示排队。实际 worker 必须是当前绑定 Weber，不能以临时测试 agent 或开发者代写冒充。

## 3. 按类型整理和正文保全

- 文章按 OKF/article-v1 在开头维护必要 YAML，使用真实 H1、Key note 和正文结构；旧模板仅作参考。个人记录、报告、课程笔记、导航、模板按实际用途组织。
- 缺失 Key note 时先完整读取当前正文再生成，保留主张、理由、必要条件、反例和作者归属；已有准确摘要沿用。
- 当前模板化保持原正文论述、措辞和风格，新增要点与原文分开，公式、代码、引文、链接、图片/媒体和锚点保全。不逐篇新增永久 `.source.md.txt`。
- 来源可空；有实际依据才记录作者、日期、出处或关联。generated 表示处理事件，不表示正文作者类别、正确性或 verified。
- 维护不按人/AI 作者或过去人工编辑判资格。纠错只改明确错误的具体字句，补充/注释插入相关位置，保留未涉及的思想、论证与表达，不整段润色或重新生成正文。
- 疑点不能凭模型印象改成事实；新证据与旧观点不同，保留原主体与语境，增加条件、更正或说明。
- 个人事实仅来自当前 Me 记录。旧附件中的字段有无是历史 gold，执行时重读当前版本；不把收藏文章的“我”、报告主题或模型推断写成用户资料。

新版 organize 显式使用 frontmatter_patch，按需加载 article/writing。精确补丁绑定当前 revision、raw locator/hash/expected_text 和已读上下文；read 提取坐标与 raw 坐标不同。blocks 定位列表不算阅读，实际返回范围才进入 read ledger。整篇评价或整理需从起点连续覆盖至末尾，不能用摘要、首段或模型自述代替全文读取。

## 4. Library 内已有 assets Markdown

范围内已有 `.md/.markdown`（含大小写）即使 kind=asset，也应能在明确 assignment 下完整读取、原位整理、定点维护、no_change、记录历史及恢复。源码已具备，实际运行资格与最终冻结版验收仍需核对；不能由生产 sections-v4 推断该单项未加载，状态见进度表。

资产角色保持：默认领域 search/documents 可继续排除，验收使用明确路径/ID读取和已有 include_assets 参数检查相应导航/检索。模板也按明确角色查询，不强求进入每个普通问题的候选。

该合同不授权新建 assets 页面或维护媒体/PDF/非 Markdown 资产。tmp/temp/static/node_modules/Codex Skills 的当前排除规则保持原状；若最终 Library 清单出现这类 Markdown，则视为范围落实缺口解决，不把它直接算完成。当前盘点没有此类文件。

## 5. 检索、表示选择与当前证据

保留 intent=lookup/question/overview/compare、compact 与旧 full 兼容；read_many 逐项返回实际内容、revision、范围、失败及可继续位置。分页 cursor 绑定过滤和 index_epoch，变化后重新枚举。宽泛问题检查不同方向，compare 分别取得对象依据；命中文档不证明读到必要论据。

表示去重只使用已解析、直接、明确的 representation_of。普通 sources/related、共同参考文献、未知来源不合并；有独立分析的 Concept/Synthesis 保留。

question/overview/compare 中，同组原始材料同时匹配 query、满足实际目录/类型/资产/角色/质量过滤并具有可读内容时优先，保留它自己的身份、revision、score 和位置。原始材料缺失、未命中、被过滤或只有题名/元数据/注释/整理记录时保留合格摘要自己的身份；不冒充已读原文。指定路径/ID的 lookup 保持指定对象，不改成另一份摘要。

readable_body 是结构资格，不是事实验证或答案充分性评分。按完整逻辑区域判断后再切检索窗口；正文、Key note、数值、公式、非空代码、引文和有效媒体引用具有内容价值，不能仅要求分词 token 非空。Markdown 区域渲染为 CommonMark HTML 后与 HTML 原稿使用同一结构规则：标题、head/title、注释及 script/style/noscript 内文字或媒体不计正文；其它非空文字及非空 src 的 img/audio/video、音视频内 source 引用可计入。转义后的字面 HTML 代码仍是正文。相关/整理区的既有排除继续适用。

HTML/HTM 原稿保留当前提取文字与坐标，提取阶段另存 html_body_version、html_body_readable 和实际 html_media_refs，索引消费该结构资格；媒体引用可从当前读取的 metadata 取得，不伪造文字位置。旧 HTML 缺少派生标记时重新提取索引，不改原文件、身份、revision 或历史。此规则不判断 CSS/hidden 属性或完整浏览器可见性，也不验证媒体资源存在或语义；结构修复的当前检查、独立审查与部署状态分别记录，源码存在不代表已上线。

分组在分页前完成，热查询不增加 Vault 全库 IO。改名保持已确认关系；删除后同路径新文件不继承旧目标。链/环不自动做完整来源闭包。

最终结构判定使用 sections-v7/readable_body，HTML 资格使用 html-body-v3；它们是派生版本，durable schema 仍为 7。算法改变使旧 v4/v5/v6 资格缓存失效，升级对账回填，保留身份、当前文件 revision、历史和回执；不得删除整份 index.sqlite3 重建。判定从完整 Markdown token 流持续传递隐藏结构及规范标题角色，之后投影到原文分区和窗口；内部代码边界不重置包围状态。引用和列表内部的标题不改变外部正文角色，也不解除外部相关/整理区的排除。

## 6. 当前版本、回执与恢复

每次动作记录真实 job/item/worker、输入来源版本、目标前后 revision、operation/reason、receipt/change。no_change 需当前完整读取，持久记录复核，不制造文本 change；用户之后编辑使旧回执只证明旧版本，不能继续计当前验收。

版本或身份变化先重读/对账，不强制覆盖；取消保留已提交结果。文件已提交但索引失败时仅恢复索引，不重新生成正文。恢复同样检查当前版本，不能回滚掉后来的人工改动。重试只处理未完成或实际变化项。

## 7. Vera 验收与阶段门槛

Vera 在首次生产写入前讨论样本、基线、问题与证据；首个 1～2 篇任务后检查全文/diff；完整试点后给放行依据；每批或重要规则变化后复核；结束时检查全量清单及证据限制。已有首批验收不自动覆盖后续代码、文档版本或全部材料。

| 编号 | 必须实际通过的合同 | 证据 |
| --- | --- | --- |
| AC-01 | 全 Library Markdown 范围覆盖 | 最终枚举与 manifest 逐项对应，无静默遗漏 |
| AC-02 | 独立核心入口、可信提交和实际 Weber | 核心 API/MCP 的账户/入口/paths、durable job、worker/assignment；不要求 App 绑定或启动 |
| AC-03 | 完整当前阅读 | 实际 read/read_many/raw 返回范围连续覆盖，revision 一致 |
| AC-04 | 按类型整理、思想与措辞保全 | 当前前后正文/diff、条件/反例/公式/代码/媒体 |
| AC-05 | 摘要复用及 no_change | 阅读、判断、实际回执；不用循环生成摘要 |
| AC-06 | 定点纠错、补充与人工编辑后维护 | 实际补丁/范围/差异，多轮后思想与风格仍在 |
| AC-07 | 索引与逐篇检索 | 当前身份、索引、精确定位/读取；适用内容问法实际命中 |
| AC-08 | 归属、个人事实和来源 | 当前 Me gold、真实依据/引用，未知不编造 |
| AC-09 | 直接表示选择与回退 | 原稿/摘要各自身份；过滤/不可读/删除改名/分页/热 IO 回归 |
| AC-10 | 冲突、取消、恢复与索引重试 | 隔离故障证据，成功项不重做，后来编辑不覆盖 |
| AC-11 | Q58 真实知识价值 | 一个真实问题：知识建立→不同问法复用→明确纠正 |
| AC-12 | 当前代码检查与上线版本一致 | 当前冻结版的本机门禁、审查、提交、部署与索引升级证据 |

完整 V2-06 至少覆盖六份代表材料及上述适用合同，Vera 给出通过/需修正/阻塞及依据，Octo 决定放行 V2-08。166 项隔离测试、首两篇通过或脚本 exit0 不替代完整试点；语义抽查不冒称全库逐篇语义认证。

## 8. 当前不作为前置条件

Sapphire/SmartReader 接入、新 UI、client_apps、tags 筛选/列表、通用写作自动装配属于对应后续票。现有可选 `app_id`、App 网关和 app_bridge 保留兼容；它们不是本轮执行入口或功能验收依赖。仓库既有隔离兼容测试可以照常运行，不要求实际 App 参与。语义适配器只由稳定、可复现的真实词法缺口触发；语言对照、大规模扩容、完整来源闭包/独立证据计数及旧快照 GC 有各自前提，不借全量整理自动启动。具体下一步与失败项处理见[执行计划](local-shared-wiki-progress-v2.md#4-之后的执行计划)。

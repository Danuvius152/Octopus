# Wiki 与运行基础缺陷修复：2026-10-17 交接

> **Status:** shipped — 简化后的 A01/A02/A03/A05 实现与本机验收；A04/A06/A07 不在范围。§16 记录当前合同与交付证据，§1–§15 保留历史。

更新时间：2026-10-19。原 F12 停止要求已被用户这次明确恢复选定范围的指令取代。保留已部署的简化基线，不从旧冻结覆盖源码；旧交接、失败和冻结继续保留。开发验证使用隔离合成数据，不在真实 Wiki 制造故障或试修。

**接手结论：只完成 §16 的四项及必要验收、文档和交付，不展开未选建议。** 原 F12 组件批准和 false/pending 报告不改写、不重新累加用例；新实现需要绑定当前版本的证据。代码交付遵循当前 `AGENTS.md` 及 `local-wsl.md`，不打断用户工作。已发布能力见[开发总结](wiki-development-summary.md)，应用未来范围见[未来计划](wiki-future-plan.md)。

## 1. 位置、基线与操作边界

- 仓库：`/home/jack/Octopus`，当前分支 `main`。HEAD 与本机 `origin/main` 引用均为 `438f42cfc629e01bb70c6195d0e28396ce170bd2`；本次整改尚未 commit/push、构建发布、部署或重启。
- 保留 `before-takeover=88d2284b2b2493001be2e945df375d7a228ccc1f`。附注标签 `wiki` 的对象为 `43b3cae8630203423845ddb097141a1703e60ec1`，peeled commit 为 `438f42c…`。不移动标签、不 reset 用户工作。
- 两份反馈在 `docs/review/octopus_code_review_2026-10-17.md`、`docs/review/octopus_remediation_spec_2026-10-17.md`，原件为本会话附件。`docs/review/` 是接管前已有的未跟踪用户材料，未修改，不自动加入提交。原件 hash、阅读记录在任务产物 `status.json`。
- 证据根目录（下文简称 **T**）：`/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-remediation-20261017`。日志、旧失败、冻结源码、设计和审阅均保留在这里；大 JSON 从文件读取，不复制到 shell 参数。
- 所有开发测试只用隔离合成文件，不访问真实 Wiki/私人正文，不执行生产维护。实际 Wiki schema 仍为 **9**，尚未实施 9→10 迁移。
- 共享 `.venv`、`.env`、`node_modules` 可能与当前发布实例有关；不在共享环境安装/试依赖。旧候选环境在 `T/dependencies/candidate-venv/`；只按 pyproject 安装的新环境在 `T/dependency-pyproject-r1/venv/`。
- 遵循 `AGENTS.md`、`docs/local-wsl.md`。纯交接文档不自动 Git 提交；最终代码交付须完成实际门禁、独立审查和自然排空部署，不强停正在运行的任务。

## 2. 模型与验证范围——交给所有审阅者

本机默认 **Codex**。**Claude 未配置/不可用，按用户要求没有探测、安装、登录或运行实机测试；不算通过，也不是本机待补的发布门禁。** 本次已有执行证据全部离线、零真实模型调用。Codex live CLI/browser 尚未执行，不能称为两后端实测通过。

该约定已写入 `AGENTS.md`、`README.md`、`docs/README.md`、`docs/local-wsl.md` 及审阅简报。后续真实验证使用独立会话、既有选定 credential、`gpt-6.1-sol/high`、正常速度，不改 persona 或 Vera/Weber 的代理默认。不得为确认可用性额外付费探测。`knowledge_eval` 仍是 opt-in，不是发布门禁。

## 3. 缺陷状态总表

“已验证”指下列冻结范围，不代表当前未完成工作区或全任务获准发布。

| 编号 | 问题 | 交接状态 |
| --- | --- | --- |
| F01 / PR01 | MCP 2.x 导致新安装导入失败 | `pyproject.toml` 保留 `mcp>=1.30,<2`；按用户反馈移除重复的全量 constraints。无约束干净安装、pip check、导入和 61 项 MCP/账户隔离通过；reference-r2 独立复核已批准依赖简化。Python 3.12 未验证。 |
| F02 / PR02 | schedule origin/session/owner 关联不完整 | 已修复统一目标校验、旧异常关联隔离、派发重检；基础 r5 通过。 |
| F03 / PR03 | scheduled 新会话不继承 agent backend | 已修复 explicit→agent→global 缺省继承和兼容入口；基础 r5 通过。 |
| F04 / PR02 | 归档 agent 仍获新工作 | 已补 closing/drain、恢复/旧会话/派发准入及 owner 顺序；基础 r5 通过。 |
| F05 / PR04 | COUNT 恢复 next seq，碰撞导致消息不可见 | 已分离 count、分配游标和快照水位；历史碰撞阻止新执行并保留原始导出；基础 r5 通过。 |
| F06 / PR05 | 深层/膨胀 YAML 阻断整库 | 已补有界解析、单文诊断、partial readiness 贯通；基础 r5 通过。 |
| F07 / PR06 | 图补解析、消歧及删除失效不完整 | **本段 code+gate 已批准**。批次 UID、reverse keys、dirty/pending、来源 binding/精确 observation、history/external/restore 已接入；BIND01/BIND02 在 r3 关闭，1780/1016 两组回归已独立结算。整体 PR06/发布仍须与 F09/F12/A01 统一迁移及最终门禁协调。 |
| F08 / PR06 | URL 重复解析/解码 | **首段 r2 已批准**：单次解码及 REF01–REF04 全部关闭，1731 项离线、fresh 377 项通过。当前 ROOT 已继续修改批次/图实现，批准不扩成整个 PR06。 |
| F09 / PR07 | maintenance status 全量嵌套扫描、分页不降成本 | **各组件已独立结算**。registry-r2、inputs-r5、projection-r3 及 selection/admission-r2 的实现、适用成本和门禁均获批；最新两环境各 1960 passed。F09 整体随 F12/A01 协调迁移和最终验收结算，未发布。 |
| F10 / PR06 | validate_links N+1 连接/全库成本 | **已接入，PR06 复核中**。名称派生索引、单连接 resolver 与 candidate overlay 已实现；100/1000/5000 热索引分相成本已验收，实际范围和复用依据见下文；PR06 最终批准待结算。 |
| F11 / PR08 | 导航/full/SDK 错误未统一限制完整响应 | **完成冻结范围修复**：后端 **3760 passed / 50 deselected**，隔离 14 个受影响完整模块 **358 passed**；七个未变门禁复用 r1。r3 最终审查 true、ER01–ER03 关闭；不等于本次全部整改或发布批准。 |
| F12 / PR09 | 单文任务默认有任意新页写权 | **r5 本组件 code+cost+gate 已批准**，R301/R302/R303/G301 均关闭。两环境各 **2040 passed / 13 deselected**，静态四项通过；成本有界复用 r4 原五组实测。见 §15。本轮到此收尾，未发布。 |
| F13 / PR10 | 跨 session 附件丢失及部分输入被执行 | 已修复并通过 55 文件冻结审查、八关、fresh 受影响模块和四项离线浏览器。 |
| A01 / PR11 | 后台 Wiki 完成卡、通知与取消/重试闭环 | 未实施；按最新用户要求暂不启动。B03 的取消未结算禁止 retry 等仍保留待办，F12 的授权退休检查不替代整项闭环。 |
| A02 / PR12 | prompt/任务卡/版本与实际工作流对齐 | 未实施；按最新用户要求暂不启动。仅已有必要的 F12 合同同步及本机 provider 范围，不算 A02 整体完成。 |
| A03–A07 | 条件性后续建议 | 未启动；不扩成应用重建、全库压力实验、事件修复或 runtime 改写。 |

## 4. 已完成代码与证据

### 基础 F01–F06：最终 r5

相关入口：`pyproject.toml`、`server/schedule_targets.py`、scheduler/agents/sessions routers、`server/sessions/{base,lifecycle,forking,turns}.py`、`server/db/{base,schedules,sessions}.py`、`server/wiki/{bounded_yaml,format,index_state,maintenance_state,store}.py`。当时冻结另含 `constraints.txt`，该文件在本轮依赖简化中移除，旧证据不改写。序号完整性 UI、调度 UI 和 generated contracts 也在冻结中。具体文件以 freeze manifest 为准。

序号分配绝不能拿来当 GET 快照的 WS 去重水位：后者只取实际返回行的最大 seq。YAML 故障保留原始字节，partial 必须进入共用 readiness；import 的预期领域拒绝保持 4xx。

必要的后台进程组清理修复在 `server/bg_tasks.py`：取消等待实际 termination task，重复取消不能跳过清理；KILL 前只在 `killpg(0)` 的 ESRCH 下提前确认；KILL 后仅 `Z/X` 且线程数为 1 才视为退出，读取不确定继续等待，未确认不得发布终态/移除 owner。没有宣称复现真实 KILL 后 D 态存活，也没有扩大为 cgroup/subreaper 改写。

| 证据 | 实际结果 |
| --- | --- |
| `T/foundation-r5/freeze.json` | 46 文件，SHA `9443de9e4e3b0b4db41d753008dc10366f28203b05e2d1bc7aed4f737b05aa0e`。 |
| `T/foundation-r5/foundation-review-r5/result.json` | `foundation_review_approved=true`，无阻断；注意真实目录嵌套，不另造同名目录。 |
| `T/foundation-r5/checks.json` | 后端 **3691 passed / 50 deselected**；fresh 五个受影响模块 **200 passed**，不是 fresh 全量。 |
| r4 复用范围 | r5 只变后台清理和边界测试，其余七关复用 r4 通过证据；没有冒称 r5 重跑八关。 |
| `T/dependencies/result.json` | MCP 2.x 导入失败已复现；候选安装、pip check、导入、MCP scope 验证通过；Python 3.14.4 实测，3.12 未实测。 |

保留 r1/r2/r3 失败记录：恢复准入过早拒绝、取消后子进程仍存活、跨 DB 旧 runner 引用分别已调查处理；没有以“随机失败”跳过。FR01 owner oracle、FR02 YAML partial、FR03 import 500、FR04 晚 fork、FR05 zombie 主线程仍有活线程均在 r5 审查关闭。

### F13：attachments-r1

后端在排队/steer 前和执行前均验证整组当前 session 附件；缺失、foreign、非法 ID、歧义、symlink、空/超限文件整体拒绝，不静默丢文件继续文字。执行前丢失使用既有结算，终态 `tool_input.rejected_input` 保存完整文字和提交 ID；DB 幂等比较/独立读回均核对该字段。

前端 `useDraftAttachments.ts` 用 UID、owner、generation 与 AbortController 屏蔽旧回报；换 session 重传 File，失败阻止整条发送并可重试。拒绝/排队失败支持恢复原 File；reload 后没有 File 时保留文字、要求重新选足附件，不允许少文件发送。历史下载的授权/fork fallback 保留。

| 证据 | 实际结果 |
| --- | --- |
| `T/attachments-r1/freeze.json` | 55 文件，SHA `9ccee98be8d41c2d0dc3e41d0c3d9d86ab25bf751aab8237cf35aa343d0d0e20`；相对 r5 为 15 路径增量。 |
| `T/attachments-review-r1/result.json` | `attachment_review_approved=true`，无阻断。 |
| `T/attachments-r1/checks.json` | 八关通过：后端 **3708 passed / 50 deselected**、前端 **427 passed**，lint/types/contracts/docs-index 均通过。 |
| fresh 约束环境 | 七个受影响完整模块 **147 passed**，不是 fresh 全量。 |
| 真实浏览器、离线执行 | **4 passed**；实际上传、WS、持久化、下载验证；audit 为 offlineDispatches=3、realDispatches=0。不是完整 E2E/Codex live。 |

三份日志的字节数、SHA 和冻结一致性均已核验；以 checks.json 中路径为准。交接文档更新前，当前 55 文件也与冻结匹配。文档更新后运行代码不变，新 WIP 文件不在该冻结中。排队丢文件、lost-ack 存储、reload UI 是分层证据，未宣称四个浏览器用例连续跑通三者的组合故障。各轮数量有重叠，不累加。

## 5. 新增代码与 F11 当前实现

| 文件 | 当前作用与验证 | 尚缺 |
| --- | --- | --- |
| `server/wiki/references.py`、`resolver.py`、`source_bindings.py` 及引用测试 | 不可变 ParsedReference 接入 indexed resolver；最终 candidate 全文差分、有效 aliases 和 checked doc 进入实际 writer。首段 r2 回归 **1731 / fresh 377 passed**，独立审查 true。新增批次 UID/图与持久 binding/recovery 正在验证，见 §10。 | r1 false 保留，REF01–REF04 已关闭；r2 批准不覆盖后续新增代码。 |
| `server/mcp_result.py`、`server/wiki/model_response.py` | 已在 TaskWiki 安装最终 low-level handler；完整 CallToolResult UTF-8 测量，包含 structured+text、SDK 参数/输出验证和异常结果。Wiki dict tools 显式 unstructured。只有单独 image 内容保留二进制例外，混合文本仍受限。 | r2 离线门禁通过，ER01/ER02 关闭；r1/r2 整体审查均为 false，最终文档核验见 r3。内部 SDK hook 的兼容性由实际 dispatch 回归锁定；不改共享 runtime 或鉴权。 |
| `tests/test_wiki_model_envelope.py`、`tests/test_wiki_envelope_integration.py` | 实际 SDK dispatch + 隔离 HTTP 验证超长 title、compact/full 分页、完整 wrapper、导出阈值、原始坐标和阅读授权。 | 旧 red 不覆盖；新 green 与最终 checks 分别保存。真实模型未调用。 |

F11 当前实现要点：

- `search/list_documents/graph/maintenance_status` 的已验证 task 调用在服务端拟合条目前缀；240 codepoint 的展示字段限制带标记，身份、路径、revision、来源状态与定位保持原值。cursor 按实际返回量和原 epoch/generation/filter 生成，末页被缩短也不跳过条目。`SearchResult` 的 task 返回直接使用已拟合 JSONResponse，避免 response_model 后续默认字段膨胀/截断标记丢失。
- `read/read_many` 的 task full 同样拟合，在最终范围确定后才登记回执；full 最小 metadata 放不下返回 `response_too_large`，不增加阅读证明。无 task 的人工 full 保留既有字段和行为。
- raw `blocks` 支持 `next_offset` 与可选 `start=next_start`；完整块的 start/end/hash/locator 不变，实际正文用 content_start/end 标明，巨大单段可续读。仅最终返回正文登记 raw receipt；locators、错误与未返回后缀不授阅读。
- compact jobs 按实际容量分页且保留 exact key；full/export 以完整 wrapper 大小触发既有授权导出，替换旧 48 KiB 内部 JSON 阈值。summary 自身因 key 过大时明确省略并返回完整 artifact 引用，不伪造截短 key。既有 maintain 提交回执的显式 bounded preview 合同没有在本次扩展重写。
- 必要工具/guide 文字和生成合同同步更新；A02 的整体 prompt/card/version 收束另行实施。F11 不改原始 Markdown、schema、权限和现有 foundation/F13 修复。

中间局部记录：`T/envelope-targeted-r1.log` 为 37 passed / 1 failed，原因是新增测试只取人工 maintenance-status 第一页，却与模型全页结果比较；人工入口原本也分页。改为读取人工全部 cursor 后，`T/envelope-targeted-r2.log` 为 **38 passed**。之后新增超长 key 导出控制，`T/envelope-targeted-r3.log` 为 **39 passed / 20.04s**；最终代码验证以 `T/envelope-r2/checks.json` 为准，不能把局部 green 当最终门禁。

冻结 r1 的实际门禁：后端 **3 failed / 3757 passed / 50 deselected**，其他七关通过（前端 427 passed）；fresh 14 个完整受影响模块 **3 failed / 355 passed**。三项均要求诊断 path 被截成 160 字符，与 F11 完整保留身份路径的合同冲突。没有改回路径截断；r2 在 `test_wiki_navigation_http.py`、`test_wiki_markdown_navigation.py` 断言完整原 path 和有界 error，局部 **48 passed / 4.63s**。原 r1 日志及 false checks 保留。r2 另移除 guide 末尾多余空行，并更新交接；运行 Python 和前端实现不变。

Vera 的 `T/envelope-review-r1/result.json` 最终为 **envelope_review_approved=false、ER01=closed、ER02 阻断**。早先交接误用了中间 true，r2 审查因此以 **ER03** 阻断，`T/envelope-review-r2/result.json` 最终同样 **false**，但明确断言增量通过、ER02 已关闭。两份最终 false 和原失败日志均保留；本页不再把局部代码认可写成整体批准。ER03 更正后的审批仅以 `T/envelope-review-r3/result.json` 为准。

| F11 最终执行证据 | 实际结果与边界 |
| --- | --- |
| `T/envelope-r2/freeze.json` | 78 文件，SHA `c2f9346237e84650f40c5940776cb2525785c0f5caa6ee8cb940faae40a189b3`。两组门禁均 `source_unchanged=true`，主控已核验全部源码 hash。 |
| `T/envelope-r2/checks.json` | **passed=true**。后端 **3760 passed / 50 deselected**；隔离约束环境 14 个受影响完整模块 **358 passed**，不是 fresh 全量。 |
| 七个未变门禁 | 复用 r1 的 ruff/mypy、frontend lint/unit、TypeScript、generated contracts、docs-index 通过记录；前端 **427 passed**。r2 另查改动测试 ruff、docs-index 和 diff whitespace；未冒称 r2 重跑八关。 |
| 两份 r2 日志 | 后端 4,288 字节，SHA `a85c9e4e60f77ad0696c5dd46f8414d570ea81bff8bfc443933b903da3af4e37`；fresh 421 字节，SHA `bf2f8a73380cdec6d54de633bdcff8403de14daa3d8463a3f16ebce1f619e872`。均已核验。 |
| r3 交接更正 | 仅本页和生成 docs index 相对 r2 改变；完整源码、文档和补丁快照在 `T/envelope-r3/`。运行代码及测试与已通过 r2 相同，无需因文字更正重跑后端。 |

r1 的 `changes.diff` 仅含已跟踪文件，不能作为独立完整补丁。r2/r3 的 manifest/source 和包含所有新增文件的 `complete.diff` 是相应版本的恢复依据；补丁已按 HEAD 基线在临时目录实际应用，并逐文件核验。旧冻结不覆盖，不整包覆盖后续用户工作。

`T/envelope-red.json` 是旧 10 failed 的 FastMCP 直接调用基线；`T/envelope-red-r2.json` 是最终 low-level 12 例基线。后者日志 SHA 为 `e669e9ac33c03f47e272f99100a66498390f347303fe065a58342abd7cd05099`。两份都保留；bg “completed/exit 0”只说明预期失败基线收集器成功，**pytest 实际 exit 1，并非缺陷已修复**。

## 6. 设计、审阅结果与待修规则

### PR06：F07/F08/F10 与 B04

以 `T/graph-design-r2.md` 为实现合同，SHA `839870c14ddd65db8b8049c0b7cb7c13c38978d1e7fd3d132242bc30c2128b35`；`T/graph-design-review-r2/result.json` 为 **design_approved=true**。原 `graph-design.md` 及 r1 false 报告保留。

GD01–GD03 在合同层面关闭：语义 selector 与原始 occurrence 分离；literal path 与 URI 不混解码，受控 writer 保留 checked doc；先收全批 UID/default claims 再决策，已有 durable owner 不被抢走，新重复 claim 不先到先得。实现时还必须：

- 接通真正的 `curation.publish/organize`、`revise` writer，不丢 doc 再从 raw path 猜；无 preferred UID 时适配当前 `metadata["uid"]` 调用者，exact doc ID 仍可用。
- 持久来源绑定与可重建图分离，目标确认后不可就地换成同路径 B。等价 `%61/a/./a`、locator 改动、source 自身移动、remove/re-add 都保留证明或明确 unknown。
- exact observation/change side 不等同 revision hash；prepared plan 到 applied 后才能成为确认事实。recover、finish_external、history/copy/snapshot 的 proof/unknown 必须在第一次索引恢复字节前落盘。
- 最终统一 schema **9→10**：先保全 v9 已绑定来源，再失效派生图；部分缺证据为 unknown，唯一整个来源账本丢失不得冒称无损，已升级 durable 表缺失失败关闭。catalog 版本要进入持久重建状态，不只改常量。
- 同批身份/名称就绪后解析边，增删移动、歧义 1→2→1→0、前向 UID、managed/native aliases、anchor 和 absent/losing keys 全部覆盖。图 pending 时 journal/ready 不提前确认。
- 验证单连接、候选索引查询和临时 candidate overlay；100/1000/5000 无关文档分别计同步成本与 resolver 成本，不用任意时间 SLA 替代证据。

### PR08：F11 与 ER01

`T/envelope-design.md` 为首版合同；`T/envelope-design-review/result.json` 为历史 **design_approved=false**，保留原文。当前补充合同为 `T/envelope-design-r2.md`。r1 最终整体审查 false、ER01 关闭；r2 最终整体审查 false、ER02 关闭、ER03 阻断。r3 只更正交接事实并登记 r2 门禁通过，最终审批见 `T/envelope-review-r3/result.json`；旧 false 不覆写。

ER01 的有效要求是覆盖 **SDK low-level CallToolRequest 最终结果**，包括 SDK 构造的异常/验证错误，不能只限制 FastMCP 函数返回值。主控补充核实：本机 FastMCP 1.30 的 `_setup_handlers` 实际使用 `validate_input=False`，故报告中“33,000 字符 limit 先触发 jsonschema”的具体静态例子在默认路径不成立；该对照实测已界内。**长异常仍实际复现外层超预算**，最终守卫已接入以覆盖该路径。受控单测另覆盖显式开启 SDK input validation 和输出验证错误；与实际默认路径对照分开报告。旧审查原文不改，用这些证据继续同轮复审。

已接入的实现使用同一编码/测量合同；最终实际 dispatch 检查 service 拟合后的响应字节，Wiki dict tools 显式 unstructured，full 不能关闭预算。operation-owned prefix builder 按真实返回数重建同 epoch/generation/filter 的 cursor；SearchResult 的 task JSONResponse 保留拟合字段与截断标记。

read/read_many/raw 均先拟合再登记实际区间；metadata 最小项放不下就有界拒绝，不用最终错误替换已授予阅读的 unseen body。raw 完整 hash/locator/start/end 与实际 content_start/end 分开，巨大段落支持 next_offset+next_start 前进。jobs 用包装后字节触发已有授权完整导出，ID/key 不截坏。局部证据通过不等于冻结范围已审查通过，更不等于整项发布批准。

### PR07/09/11/12：组件结算与保留待办

- **F09**：registry、inputs、projection、selection/admission 组件分别结算，见 §11–§13。已保留同事务 dirty/generation、实际完成基线、稳定 selection 与最终准入检查；whole_F09 的最终集成及发布仍未批准，不因本轮结束删除该待办。
- **F12**：r5 已结算精确 batch 身份、有限新页、原 scope 写入、导航追加、原 job 重新准入及退休检查，见 §15。旧宽授权进入 scope_review_required，普通 retry 不能清洗；部署前仍须自然排空旧 active，本轮没有部署。
- **F12 用户补充范围（2026-10-18）**：单文入库允许有依据的 `[[其他文档]]` 强关联、反向关系/检索索引同步及必要导航索引条目；不需逐次确认。单文禁止任意另建正文与这些配套动作分开。grant 复用持久 job/assignment 的授权范围；不为双链或普通索引同步另建审批/任务体系。
- **A01 / B03**：持久 job/item version、多请求者订阅及 outbox，卡片与主 DB 唯一接收共同证明通知；closed/archived origin 也能保存，不额外模型“报喜”。取消马上关写、退休未确认禁止新尝试，更新匹配 attempt/session，不误杀 human correction，不重置预算。
- **A02**：上述服务合同可用后再收束 task card、guide/prompt 版本/hash、权限 manifest；不把 hash 当授权，不改 persona，不假定 Codex 有未验证的跨轮 prompt cache。清理 AGENTS 中陈旧测试数量和旧 restart-before-build 说明；以 local-wsl 的自然排空发布为准。

## 7. 队列与迟到回报的处理

原交接及 F11 的 bg 和 Vera 工作均已结束；F11 r3 最终审批 true，ER01–ER03 关闭。2026-10-18 新增 PR06 回归与后续任务以 `T/status.json` 为准。r1 failed checks、r1/r2 最终 false 审查保留，不因后轮通过改写历史。

| 任务 | 状态与归档 |
| --- | --- |
| bg `4b8c65603c0b` | 基础 r5 完成；freeze/checks/review 见 §4。 |
| bg `3660f4493374` | F13 三组完成；见 `attachments-r1/checks.json`。 |
| bg `2b5c2b882408` | F11 旧预期失败基线收集完成；不是发布通过。 |
| Vera `b1d29366bf81`，work `f6ad21f22a08451d802a09448ec0a7f6` | PR06 r2 设计完成、批准。 |
| 同 delegation，work `354cff6609824dee8b68ba4d6799915d` | F11 首轮设计完成、false/ER01；主控已读全文及记录补充反例证据。 |
| bg `d8a25b9a991c` | F11 r1 两组门禁失败；三项旧路径截断断言，日志和 false checks 保留。 |
| bg `7f8c6aecd0a9` | F11 r2 两组回归通过；3760/358 passed，七个未变门禁复用 r1。 |
| 同 delegation，work `c5af202220eb496d9430f0282310a3a7` | F11 r1 最终 false；ER01 关闭、ER02 阻断。 |
| 同 delegation，work `15843ddc51eb473bb81d560935e76a02` | F11 r2 最终 false；ER02 关闭、ER03 文档误记阻断。 |
| 同 delegation，r3 | 只核验 ER03 文档更正及已落盘 r2 证据，最终结论以 `envelope-review-r3/result.json` 为准。 |

F11 的历史审查使用 `delegation_id=b1d29366bf81`；后续 F09/F12 审查使用 `25b9f3611034`，r5 最终结算已完成。本轮没有待启动的新审查或测试；只有用户明确恢复相应工作时，才按实际范围续同一委托。旧 r1/r2/r3 gate 失败、r4 FR05 false 等均保留，不重写为通过。

## 8. 接手顺序与恢复命令

先看本文件、`T/status.json`、`T/handoff-20261017/manifest.json`。snapshot 保存本机未提交 diff 与非 `docs/review` 改动文件；只用于核对/选择性恢复，**不要整包覆盖未来用户工作**。产物包含 inputs、保护 refs、所有权范围和最后 hash；不含认证内容或真实 Wiki。

**原 F12 停止点（历史）：** 当时止于 F12 r5 及文档。用户已明确恢复的当前范围和新基线见 §16；下列原交接步骤不再授权扩展其他待办。

用户明确恢复开发后建议：

1. 核对 main/工作区及新 WIP hash，读原 spec 相关节和最新审查。不要重复跑已收齐的失败基线或问答样本来“确认额度”。
2. 承接已结算的基础、F11/F13、PR06、F09 组件及 F12 冻结证据；不因迟到回报或本轮停止重复实施、重开已关闭发现。后续新差异另行判断证据适用性。
3. 根据新的恢复范围推进未完成的 A01/B03、P01/S01–S03 等，不把原报告的条件建议自动扩成开发任务。最终迁移仍与 F09/F12/A01 DDL 协调，不访问生产数据测试。
4. A02 在服务端合同完成后再收束；每段冻结、独立审查并核验真实结果，不将本 F12 组件批准扩大为整体验收。
5. 最终集成再跑所需离线/frontend/pure-UI/Codex live 门禁；通过前不提交或发布。只提交本次拥有的代码与必要文档，保留 `docs/review` 用户文件。按 local-wsl 自然排空发布，验证运行 revision，不移动既有标签。

最小本机命令（均在源码仓库；是离线局部回归，不是“可发布”检查）：

```bash
git status --short
git rev-parse HEAD origin/main before-takeover 'wiki^{}'
.venv/bin/python -m pytest tests/test_wiki_typed_references.py tests/test_mcp_result.py -m 'not real' -q
.venv/bin/python -m pytest tests/test_wiki_model_envelope.py tests/test_wiki_read_response.py tests/test_wiki_envelope_integration.py -m 'not real' -q
```

最终发布的 Codex 标记选择仍为 `real and (real_codex or codex_bin) and not real_claude and not knowledge_eval`；浏览器明确区分 audited offline 与真实 Codex。当前无 Codex live 结果，不能复制旧任务的实测为本整改背书。

F11 本轮执行代码回归、文档/index/link/hash 核对与独立复审；不调用真实模型、不提交或重启。整项缺陷整改、最终集成与发布仍未完成。

## 9. 2026-10-18 恢复与新增审查

本节至 §15 保留当时状态；当前范围以 §16 为准。历史的继续、停止与批准均只对当时版本有效。

保留用户新建的 `docs/before-takeover-behavior-review-20261018.md` 及 README 索引，不覆盖其原始结论。报告发现的 P02 与 F12 是同一授权缺口，不重复统计；P01 research 租约/work 透传，S01 held-process 与每轮 Wiki grant，S02 重复控制快照、S03 准备错误分类列入后续验证修复。不能只删除签名字段、跳过控制更新或无界重试来收敛这些问题。

恢复检查曾发现 `constraints.txt` 丢失，并从 F11 不可变冻结逐字节恢复。随后用户指出已有 `pyproject.toml`，经验证不需重复全量锁版，现移除该文件及安装命令中的 `-c`；保留 MCP 必要兼容边界。`T/dependency-pyproject-r1/result.json` 记录无 constraints 的干净安装、pip check、导入和 **61 passed** 的 MCP/账户隔离，Python 3.14.4、FastAPI 0.143.3、MCP 1.30.0；未修改共享环境。账户隔离结构检查适配 included-router context，嵌套前缀和继承 guard 均有断言；Python 3.12 未实测。旧报告对恢复前缺失的记录不改写，旧约束环境也保留。

PR06 新回归最初的 fixture 使用了非 UUID vault ID，部分失败并非产品反例，原日志保留并明确排除。修正 fixture 后，在隔离副本中还原 F11 实现，得到 `T/reference-resolution-red-r2.log` 的 **7 failed**；首段 typed/indexed resolver 的局部为 **32 passed**。这只证明当前边界增量，不关闭 F07 或整个 PR06；完整相邻 Wiki 回归另行记录。

`T/reference-r1/checks.json` 现已结算：**1669 passed / 13 deselected**，源码与 13 文件冻结一致。Vera `T/reference-review-r1/result.json` 为 **reference_stage_review_approved=false**：REF01 关系 token 未编码、REF02 自链补解析丢 source_doc、REF03 overlay 遗漏最终别名、REF04 replacement 片段漏检拼接链接。四项均已用真实 writer/索引入口复现：`T/reference-writers-red.log/xml` 为 **24 failed / 4 passed**，零 errors；没有把通过的旧回归当作反例不存在的证明。

首段修正统一在全部正文及 metadata 修改完成后，从 candidate 全文计算 introduced references，再用最终 native/managed metadata 的内存 overlay 验证一次。organize 两入口生成 URI token 时编码 literal path，现代入口也保存 checked relation source；重解析自链保留 source_doc。revise/restructure 显式替换 tags/aliases 时移除相应可信旧 region 与 managed 字段，保留其他内容并同步 material trace。`T/reference-writers-green-r1.log/xml` 为 **60 passed**；补 restructure 最终别名及 portable comment 投影控制后，`T/reference-writers-green-r2.log/xml` 为 **72 passed**（63 引用、9 账户结构）。旧坏链及代码示例保持原有处理，失败不保存 candidate/别名。

首段最终冻结为 `T/reference-r2/freeze.json`，86 个累计文件，SHA `b595a4b9303ca5797d4e46a7d8f881db14e63f5f21bf21b16d01e5d0b0ea889f`。相对 F11 全冻结叠加 r1 增量的实际差异是 10 个改变/新增路径及删除 constraints.txt，见 effective-first-stage-delta.json；不能把累计带入文件当成本段全改。complete.diff 已实际按 HEAD 恢复并核验，source 和隔离 test-source 保留。`T/reference-r2/checks.json` 为 **1731 passed / 13 deselected** 与无 constraints 新环境 12 个完整模块 **377 passed**，两组零 failure/error/skipped、源码未变；日志 hash 已核验。`T/reference-review-r2/result.json` 的 **reference_stage_review_approved=true、dependency_simplification_review_approved=true**，REF01–REF04 关闭，无新阻断；r1 false 保留。此批准只限 r2 首段，PR06、迁移与发布均未批准。

## 10. PR06 批次解析与来源绑定：继续施工

本节是 r2 批准后的新增量，不使用 r2 回归为当前 ROOT 背书。先在 r2 隔离源码补了 11 个批次/身份用例，`T/reference-batch-red-r2.json/log/xml` 为 **8 failed / 3 passed**，零 errors；原源码和日志均保留。覆盖正反实际目录扫描、前向 UID、portable/default 冲突、已有 owner、别名歧义 1→2→1→0，以及 A 移动且旧路径出现 B 后的等价拼写、locator、remove/re-add。

当前新接入 `server/wiki/uids.py`：收齐全批原始/default claims 再确定未绑定 alias，已有 durable uid_aliases owner 不转移，新重复 claim 不先到先得，无唯一 alias 时不广告 preferred UID；原始 UID 保留为 portable_uid/冲突诊断，文件字节不变。新文档的无冲突 portable UID 优先，已有首选 alias 顺序保留；验证中发现的首选顺序兼容回归已修正。

`catalog.py` 新增所有候选（含 absent/losing）的 reverse keys、catalog_dirty/catalog_state；名称与 UID 完整后才解析 dirty edges。删除前 trigger 收集旧名、incoming 和 UID claims 影响，native/managed aliases、路径优先级、anchor 变化进入同一队列。refresh/synchronize 在同一事务内完成 graph 后再 ACK filesystem journal，普通读回也可恢复 persistent pending；readiness 计入 catalog pending。documents 更新使用 UPSERT 保留旧名影响，单文 writer/read 的 _index 仍默认收尾。claims、lookup 和 pending 是派生投影；来源部分的 schema 9→10 实现见后文，最终发布仍须与 F09/F12/A01 DDL 协调。

最近的 `T/reference-batch-targeted-r3.log/xml` 为 **95 passed / 2 failed**：两项失败明确是来源等价拼写和 remove/re-add 仍会从 A 重绑旧路径 B，须接入持久 binding/精确 observation 与 history 恢复后修复，不能删除或放宽断言。图缓存重建、pending 恢复和失败事务不提前 ACK 的控制已通过；随后补了 UID pending cache 丢失的参数场景，纳入下一轮全相邻回归。当前新批次实现尚未独立审查，不可发布。

全相邻观察 `T/reference-batch-r1/checks.json` 为 **1743 passed / 5 failed**；无 constraints 新环境 13 模块为 **392 passed / 2 failed**。除两项未完成绑定外，三个 trash restore 用例因接回旧身份时临时 documents 索引仍占用路径而失败；新环境原范围没有 history/origins，不能将差异归因于环境。现修正恢复交接：先确认旧身份未在别处生效，再用 detach_path 退役临时身份及派生索引，保留历史 tombstone，随后接回旧身份；重复回放不再次退役已恢复身份。新增旧身份已在别处生效时保留日志、身份、字节及来源证明的拒绝控制。

`T/reference-restore-r1/checks.json` 两环境均 **93 passed / 3 failed**：原三个 restore 回归已通过；新增拒绝控制的测试先因后续普通 read 再次触发保留日志而失败，该断言已更正为读取仍拒绝并直接核对持久身份；其余两项仍是未接入绑定时的旧红灯。原失败记录不改写。

新增 `source_bindings.py` 分离持久 proof 与派生 edges。绑定的 source/selector/base 及已确认目标不可变，unknown 不升级为当前路径猜测；occurrence 独立保留 raw/source 字段、locator、revision、label，observation 以独立 ID/context 对应精确 change side。相同字节的 legacy snapshot unknown 和已证明历史版本有不同 observation，按所选 side 恢复；copy 为新 source 拥有自己的 binding，保留目标证明。删除不级联移除来源事实，remove/re-add 与等价拼写继续保留原目标或 unknown。

prepared change 同事务保存 before/after plan；after 到 applied 才 stage，并在全批 identities/names/UID 完成后确认及投影。head 中的 pending_plan 持久保存恢复策略。external journal 在第一次索引前保存具体 source_policies；capture/trash 另存精确 source_plan。历史旧 side、旧 prepared after、无证明的 legacy restore 在第一次索引前落为 unknown。直接 recover 统一进入既有 writer lock，防止同步再次回放同一 journal。schema 9→10 先按旧 edge 的完整 occurrence 核对保存已知 target，部分缺证据写 unknown，整个旧账本缺失及新 durable 表缺失拒绝；新迁移失败整体回滚，旧 schema9 框架拒绝新版。旧迁移 fixture 同步移除新增表/列以保留真实旧形状，原有保护断言保留。

旧路径影响改用共享 ParsedReference/candidate_keys 与 reverse lookup，保留一次 URI 解码；未知来源在 read/compact/maintenance 中明确显示 unknown。局部证据 `source-bindings-targeted-r4.log` **132 passed**，`source-bindings-writers-r1.log` **124 passed**，补迁移回滚/旧框架 guard 与 percent-path impact 后 `source-bindings-targeted-r5.log` **22 passed**；这些组有重叠，不相加。ruff 与 7 个源码文件 mypy 通过。完整相邻回归、100/1000/5000 分相成本和新实现独立复核尚待结算，不关闭 PR06、不发布。

新冻结预检发现 history.py 是批次阶段新改路径，未包含在旧拥有清单中；此前 test-source 全复制及其 manifest 包含该文件，但旧 ROOT 文件清单的 unchanged 核对范围未覆盖它。已保留失败预检并补入新累计冻结，不能将旧清单的 ROOT 核对范围扩大解释。

`reference-bindings-r1/freeze.json` 累计 95 文件，SHA `dfd9a4d777e7d150b6773258d262087e8871a7642579cce95f7303a34b50ddd7`；完整相邻 **1770 passed / 1 failed / 13 deselected**，pyproject 新环境 19 完整模块 **1006 passed / 1 failed**。同一失败是旧全库 inventory 的 rename recovery 把未变 topic 的来源也降为 unknown；旧 false checks 保留。现只为真正变更的 inventory 路径生成恢复 policy，未变文章保留当前精确 observation；显式 restore 仍使用所选证明，即使字节相同也记录独立 before/after side，不能把它误判为无变化。新增两种 inventory 控制与同字节 external restore 控制，`source-bindings-legacy-inventory-r2.log` **105 passed**。本轮实际产品增量仅在 history.finish_external，验证与说明同步更新。

`reference-bindings-r1/cost-profile/result.json` 已通过且源码未变：100/1000/5000 无关合成笔记，每个规模三次，热索引、无 pending 的同步均 3 连接/5 SQL，单链接校验均 1 连接/8 SQL、4 次候选 SELECT；索引查询计划核验为 SEARCH。初始建库另计（约 0.13/0.67/3.40 秒）；resolver/candidate 阶段约 0.58–0.81ms，不作为任意时限 SLA 或冷索引承诺。r2 不重跑该通过项；依据字节与 AST 增量确认未改测量路径，复用依据另存 task artifact。Vera 本次审查因 provider 的 `workspace routing discovery failed` 中断，没有新增审查结论或批准；按用户“刚才失败的重来”继续同 delegation 重试。r2 完整回归与独立审查未结算前，PR06 保持未关闭。

r2 后续结算：`reference-bindings-r2/checks.json` 为 **1774 passed / 13 deselected** 与 pyproject 新环境 19 完整模块 **1010 passed**，两组源码未变、零 failure/error/skipped，日志哈希已核验。Vera 重试完成，但 `reference-bindings-review-r2/result.json` **未批准**：BIND01 指出旧 unresolved observation 的复制会丢失后来确认的 A，可能重绑旧路径 B；BIND02 指出新 doc 身份未失效 absent exact-doc 候选。通过回归不替代代码批准，原 false 审查保留。

已增加两项真实序列和缺失/跨 owner/跨 selector 证明的拒绝控制，修改前 `source-bindings-review-red-r1.log` **6 failed / 41 deselected**，零 error；修改后定向 **6 passed**，完整来源绑定/批次/history/origins 相邻组 `source-bindings-review-adjacent-r1.log` **126 passed**。复制分支现在按所选 occurrence 的 binding ID、原 owner 和实际 selector 读取持久事实，再为新 owner 按值复制；不改历史 observation，也不从陈旧 unresolved 快照猜当前目标。新文档索引补发 `(doc,id)` reverse key，与原批次 graph/ACK 一起收尾。回归覆盖 A 后确认、A 移动/B 复用、旧 after side 复制、新 owner、新 binding、reopen/派生重建及跨批次来源/导航补解析。r3 全组和独立复审待结算，未发布。

r3 复用 r1 成本证据只限热索引、无 pending 的同步及 resolver 阶段；改动的 catalog.index 位于首次/变更索引路径，旧初始建库耗时仍只属于 r1 实测，不能作为 r3 建库耗时。当时后续 F09 的 projection 与稳定 selection 方案仅保存在 task artifact，尚未实施或批准。

后续 r3 完整回归已结算：`reference-bindings-r3/checks.json` **1780 passed / 13 deselected** 与 pyproject 新环境 19 完整模块 **1016 passed**，两组零失败/错误/跳过、源码未变。Vera 的 `reference-bindings-review-r3/result.json` 批准代码、关闭 BIND01/BIND02；当时 pending 回执保留，随后 `reference-bindings-review-r3-settlement/result.json` 独立核验全部日志/JUnit 与冻结关联，**code_and_gate_approved=true**。这只结算本段，整体 PR06/任务/发布仍 false。

## 11. F09：已批准设计与注册/生命周期首段

`T/maintenance-design-r4.md`（SHA `256305407eb25c59fbd28341c187b886ab6b095dd7804df167554d3a1dd6b76f`）已获 `maintenance-design-review-r4/result.json` **design_review_approved=true**。D01–D04 在 r3 设计关闭，D05 在 r4 设计关闭；r2/r3 拒绝报告原样保留。这是实施前设计结论，没有把拟议并发用例、projection 或 selection 当已实现。

旧实现成本基线 `maintenance-baseline-r1/verified-summary.json` 已核验五组 profile：5000 文暖 next-page 只返回 20 条，仍投影 5000 文、5010 SQL、15044 fetched rows，约 4.5–4.8 秒；单文 14 SQL 却加载全库 docs/jobs/receipts。固定 100 文、每文历史 1→50，next-page median 约 5.37→151.32 ms。均为隔离合成诊断，首次建库另计，不作生产 SLA 或新代码验收。

首段开始接入：config 原子保存非复用 registration generation，同值重试保留代次；协议支持/配置已初始化 marker 与派生状态分离，先只读预检再升级旧字段；完整后续 schema 10 仍须协调，当前中间版本不得部署。Store 在任何构造副作用前登记 permit 和候选资源；block/close/stop 撤销并锁外等待，发布复核 owner epoch/incarnation/blocked/legacy/config。清理失败保留资源及未完成记录、禁止新工作，不提前宣称 drained；注册提交也计入在途 writer。账户迁移受控重开核对真实 journal，不能靠裸 `_store` 绕过 blocked；维护状态入口先准备/borrow，再锁内重检，configure 不再持外层锁等待 Store 构造。

已保留 `registry-generation-red-r1.log` 两项旧合同缺失红灯；第一轮生命周期局部的测试模块导入错误保留日志并已改正。后续 `registry-lifecycle-targeted-r4.log` **65 passed**，覆盖注册 A→B→A、同值/落盘不确定重试、双构造 winner/loser、block/close/取消、缺协议/nonce、旧配置升级中断、迁移，以及既有共享 observer/leases；较早相邻 `registry-lifecycle-adjacent-r2.log` **235 passed** 属于之前的代码迭代，不能替代新冻结完整门禁。组重叠不相加。首段完整离线/fresh 回归和独立代码审查待结算；projection、稳定 selection 与最终准入事务仍在后续实施范围，不关闭 F09，不发布。

补入 stop 等待注册提交的控制，`registry-lifecycle-targeted-r5.log` **19 passed**；最终预检加入只读协议结构校验，缺失/不兼容 marker 不作空缓存恢复。首段最终局部日志与冻结、回归进度以 `T/status.json` 为准，未结算项不能从既有组数推断为通过。

首段 r1 完整两组各 **1799 passed / 1 failed / 13 deselected**，原回执保留。失败是旧用例 stop 后直接读取，没有实际 start；按获批合同补显式重启的隔离副本，两组各 **1800 passed**，但不覆盖后续故障修正。Vera 的 `maintenance-registry-review-r1` 拒绝实现，提出 MREG01–MREG04：最后 lease 的 observer 清理异常漏通知、同值配置重试跳过持久性确认、未注册冷查询丢失 404、初始化后缺失注册字段的公开 config 读取未拒绝。

r2 修正：lease 归零即使清理失败也通知，close 被唤醒后重新检查释放结果；同值重试保留 nonce 并重新 fsync 配置文件及目录；普通冷查询先检查 gate 和配置存在性，锁内再重读，锁路径故障转为明确的 503；公开 config 读取仅允许真正旧版本缺代次，已初始化缺字段返回 503 且不写回。还用屏障复现并修正两处重开竞态：stop 未完成不能 resume/start，启动被 stop 撤销后不能发布后台任务，重叠 stop 也不提前开放入口。

`maintenance-registry-r2-work` 保留 **9 failed / 4 passed** 的四项故障基线、两项重开竞态红灯，以及修正后的迭代日志。新增 schema/refresh 内部构造屏障、outer history/account 锁下冷构造拒绝、受控迁移构造遇 block/close/journal phase 变化的覆盖；refresh 屏障第一版 fixture 未触发冷刷新导致的两项失败，以及补充并发测试时插入位置错误造成的 collection error 均保留，不能视作产品反例。最终当前局部为 `review-faults-green-r6.log/xml` **68 passed**，完整两环境门禁及 r2 独立复审仍待结算。projection、稳定 selection、最终准入事务和 F09 整体仍未完成，本段不发布。

r2 后续结算：`maintenance-registry-r2/freeze.json` 为 103 文件，SHA `d2209effd3f17bcca70b815eb4f963ff05da6d8fc689fde6f25156b993a39000`；完整两环境各 **1827 passed / 13 deselected**，无 failure/error/skipped。`maintenance-registry-review-r2/result.json` 的组件 code+gate 批准为 true，MREG01–04 关闭；`maintenance-registry-r2/settlement.json` 汇总已核验的冻结、日志/JUnit 与独立报告。原 r1 false 和各中间失败保留，不覆盖；此批准不包含后续投影输入层，也不关闭 F09 或发布。

后续投影输入层实现位于 `maintenance_inputs.py`：同事务同步维护 old/new job input/output membership 和文档 dirty token，标量归一化按 job dirty/rebuild nonce CAS 写回；文档写回还复核相关 job 屏障，不能清除更新的 token。输出身份只来自 receipt 或 publication→retention，不查当前路径占用者。来源绑定/head/observation 与直接依赖目标变化进入失效范围；规范化保留 exact true、Python failure truthiness、输出 revision 类型与可选 receipt 匹配语义。重复键或错误结构的 items 直接 SQL 修改在原事务中拒绝，避免 SQLite/Python 不同解析。

WikiJobs 仍拥有 jobs/job_requests DDL，初始化 marker 和失效触发器与建表同事务提交；已初始化后缺表/不兼容不能当空账本重建。v1–v9 只接纳完整兼容旧账本及其现有引用；无 jobs 表却有 receipt/publication 引用时拒绝。新中间 v10 增加 inputs protocol，之前未发布的 registry-only v10 不自动猜测升级；最终 v10 仍需与 selection/F12/A01 统一协调。派生输入表可重建，账户迁移 digest 不把这些缓存误当持久账本。旧版本 fixture 删除新派生触发器/表，并为已有历史 job receipt 保留真实旧 jobs/job_requests，不削弱迁移断言。

`maintenance-inputs-r1-work/targeted-r4.log/xml` 当前 **73 passed**，覆盖输入层与旧 schema/注册相邻组，并检查转义键名不能绕过重复 JSON 键拒绝；这属于未接公开 read path 的输入组件，不证明维护查询已提速。完整离线两环境与独立实现审查待结算；公开 summaries、live old-path details、稳定 selection、完整准入事务和新成本测量继续实施。

输入层 r3 的两组完整离线门禁各 **1861 passed / 13 deselected**，冻结、日志与 JUnit 已核验；`maintenance-inputs-review-r1` 只批准该门禁，实现仍因 MINPUT01–03 拒绝：REPLACE 隐式删除遗漏旧关联、capture 没有同快照归一化屏障、输入和搜索/图缓存组合丢失时残留 trigger 阻断启动。旧批准与拒绝报告均保留，不能据通过用例关闭这些交错。

修订输入缓存版本为 v2：Store 与独立 EventInbox 写连接启用 recursive_triggers；所有本组件事实 trigger 检查该连接要求，直接 SQL 显式关闭时整条语句 ABORT，不能跳过 REPLACE 的旧身份失效。支持开启该协议的 INSERT/UPDATE OR REPLACE，旧新 job/output 的 membership、dirty、归一化项目与原账本同事务回滚。capture 在同一显式读快照中检查相关 job，未归一化返回 updating（None），不能在 normalizer 随后完成时拿旧计算通过发布；归一化仍不清文档 dirty。

schema 先验证持久协议与 job 账本，在同一初始化事务内拆除不兼容输入缓存或缺失依赖缓存的本组件触发器，恢复搜索/图后再用新 nonce 重建并安装完整输入层；中断恢复原 schema、nonce 和事实。已有同名但定义错误的 owned index 也按定义修复，定向检查恢复后查询计划使用该索引，不据此宣称公开查询成本改善。`maintenance-inputs-r4-work/review-red-r1.log/xml` 保留修正前 **13 failed / 6 passed**；修正后的输入与 schema/生命周期相邻组 `review-green-r1` **92 passed**。后续冻结两环境与独立复审待结算，公开 summaries、selection、最终 admission 和 F09 整体仍未完成，未发布。

补入健康重开不更换 nonce/清洁投影、组合缓存丢失仍先拒绝缺失账本或协议的控制，当前局部 `review-green-r2.log/xml` **95 passed**。红灯覆盖新增模块最初 19 项（13 失败、6 控制通过）；后加 3 项只记录绿色验证，不冒充曾运行的红灯。静态预检中共享 fixture 的导入 lint 失败与修正记录保留，最终冻结及两环境完整回归另行关联。

输入层 r4 的两组完整离线回归各 **1876 passed / 7 failed / 13 deselected**，旧 false 回执保留。七项均在旧 fixture 的裸 SQLite 准备写入处违反新的 writer 协议：event_inbox 的四种 binding/watermark 损坏和一个隐藏 pending 控制，以及 lifecycle 的 block/close 冷 refresh 两项，尚未进入原故障/竞争断言。Vera 的 `maintenance-inputs-review-r2` 批准本段产品修正、关闭 MINPUT01–03 与 C01，单独以 MINPUT-G01 拒绝门禁；批准范围不扩成公开维护查询或 F09 整体。

按同一 writer 合同仅在这三处故障 fixture 连接显式启用 recursive_triggers，保留所有原值、pending receipt、cold-refresh 与 drain barrier 断言，也保留 OFF writer 的原子拒绝测试；不改产品 guard 或生产逻辑。先在 r4 隔离副本完成相邻两模块与输入边界组 `maintenance-inputs-fixture-sync-r1/targeted-existing.log/xml` **84 passed**，再同步这三行到 ROOT。产品代码与 r4 逐字相同，r5 完整两环境门禁及 fixture 修正复审待结算；未发布。

r5 后续完整两环境各 **1883 passed / 13 deselected**，日志/JUnit 和 108 owned、396 test-source 逐项核对。`maintenance-inputs-review-r3` 关闭 G01、批准 fixture 与组件实现；原 pending 保留，`maintenance-inputs-review-r5-settlement` 独立结算本段 **code+gate approved**，MINPUT01–03/C01/G01 均关闭。`maintenance-inputs-r5/settlement.json` 关联冻结、检查和独立结算；这仍不是公开 read path 或整体 F09 批准。

## 12. F09：公开维护查询投影接入

当前接入 actor-neutral summary：按精确 doc-ID 批次读取 indexed receipts、normalized items、conflicts 和当前 source proof，计算不持有写事务；按 dirty token、rebuild nonce 与相关 job barrier CAS 发布。保留 receipt rowid 与 job rowid cutoff、no_change、partial item、历史 baseline、人工编辑及纯移动语义；同一输出对应多个输入时保留全部关联，不能以一个 job→item_doc 字典覆盖掉失败输入。

输入派生版本 v3 增加 old/current dirty paths 及 path/doc 索引，让目录暖页不因库内其他目录未计算而扫描全部文章；这些路径与事实 token 同事务维护，成功 CAS 才清除。标记路径用显式 ON CONFLICT DO NOTHING，避免外层 document UPSERT 改写 OR IGNORE 冲突行为。document error readiness 使用精确 partial index；响应同一快照还核对实际 index 和 job normalization barrier。

公开 state 页按 candidate/path/doc 索引计数和分页，仅响应时附加 owner/registration/actor/purpose 绑定的 review；旧 accepted paths 入缓存，incoming old-path links 与名称歧义在响应快照实时读取，不进入 review key。全局 scalar generation/rebuild nonce 取代全库 history hash，仅用于普通观察 cursor；过期返回带 stale_cursor 的 409。checked admission 先 settle 当前单文摘要，再用同一标量/证明规则比较与现有 replay；最终单事务 admission 和 durable selection 尚未实现，不在此段冒充完成。

dependency digest 显式版本化，包含有序 occurrence/selector、reference/locator、recorded revision、当前 durable binding policy/target/proof、目标当前路径/版本及 resolution state。批量读取当前 proof，不从旧 unresolved observation 取目标，不在查询时确认 binding；missing selectors、location 和 recorded-version 变化各自保留，公开响应不暴露原始 source/proof 或 job error/body。

`maintenance-projection-r1-work/read-path-red-r1.log/xml` 的四项红灯分别证明旧暖页/单文全历史读取、policy 未进入 digest，以及多输入覆盖失败项。首轮接入 `read-path-green-r1` **57 passed / 3 failed** 暴露 dirty-path OR IGNORE 与外层 UPSERT 冲突，已用显式 UPSERT 处理；`read-path-green-r2` 维护流程与输入/CAS 相邻组 **121 passed**。后加竞争、持续变化、缓存重建、live old links、未知 selector/locator/version、readiness 屏障及索引计划控制；r3/r4 的一个 move fixture 因目标新目录未先 arm watch 被正确拒绝，补明确刷新准备，原日志保留。当前新模块 `read-path-green-r5` **17 passed**；完整两环境、成本规模和独立复审待结算，不宣称本段或 F09 完成，不发布。

补入成功重放的前后两次读取控制：`replay-snapshot-red.log/xml` **1 failed**，证明不能用较早 store.read 的版本匹配 receipt 后便声称当前成功；现在比较最终读快照中的 current 身份/版本，消除这段窗口的错误成功。新模块最终 `read-path-green-r6` **18 passed**，不据此宣称整个 enqueue 原子事务已实现。`maintenance-projection-r1` 是未审预检快照，10 文 smoke 只验证 runner 与暖查询零历史加载；新最终冻结为 r2，完整规模与两环境离线、实现复审另行结算。并发 `docs/local-wsl.md` 用户技能说明已保留，未由本任务编写，不属于此产品增量。

r2 完整两环境各 **1901 passed / 13 deselected**，但第二组运行期间 `docs/README.md` 偏离冻结，原 checks/validation 因此为 false，不能结算为门禁通过；源码与 test-source 未变，用户文档和原失败回执均保留。Vera 的 `maintenance-projection-review-r1` 另以 MPROJ01/02 拒绝实现：共享大 job 跨批重复读取全部 normalized items，目录 readiness 从全局 dirty membership 起扫。已测干净暖页零历史 hydration 的证据有效，但不覆盖这两个边界。

`maintenance-projection-boundaries-red-r1` 在 3 秒内复现三项失败：513/1025 文共享 job 分别累计加载 1026/3075 items，初建与整个 job 失效后的重建一致；目录外 dirty jobs 从 1000 增至 5000，当前小目录查询从 18600 增至 78600 VM steps。现在按本批直接 input doc 和 receipt/publication 精确 `(job_id,input_doc)` 读取 items，UNION 去重；目录 barrier 从 path 索引中的当前身份起查 member/doc、dirty/job 索引。仅改第一处时两个跨批例通过、目录例仍失败；两处都改后累计 item 加载恰为各文数，0/1000/5000 无关 dirty jobs 均为约 3600 VM steps。保留原读取/CAS/相关 job 屏障与多输入失败语义，补 receipt/publication 远端输入控制。`maintenance-projection-boundaries-green-r2` **23 passed**；新冻结、完整离线和独立复审待结算，不以局部通过代替 F09 或发布批准。

projection-r3 后续结算：114 source/402 test-source 冻结 SHA `784dd9ed9e3c52ece137e40b8a772b7fe4369af2f4546e8449c87861766bca3e`，两环境各 **1906 passed / 13 deselected**，0 failure/error/skipped。1000/5000 文共享 job 全范围投影和整 job 失效只 hydrate 2N items（每文一条历史及一条共享输入），归一化各一次 N 项；五组干净暖查询零历史 hydration/重算。Vera 的 `maintenance-projection-review-r2` 批准实现及组件成本合同，原 pending 保留；`maintenance-projection-review-r3-settlement` 批准本组件 gate 与 code+gate，MPROJ01/02/G01 关闭。冻结、原始日志/JUnit、总回执与 ROOT 文件集合均已核对；用户文档本轮也与冻结一致。`maintenance-projection-r3/settlement.json` 关联结论；不等于整个 F09 或发布完成。

## 13. F09：稳定 selection 与最终准入事务组件已结算

新增持久 selection header/items 与 append-only job provenance，由完整 9→10 迁移初始化并验证 marker、表、约束和保护触发器；已有 v10 缺协议/账本不能当空缓存修复。观察包含 owner、registration nonce、actor/purpose、TTL、精确有序 doc/path/revision 与完整 review。人类、普通 agent 和绑定 app 的 actor 独立绑定；只读/旧 Off 可以观察，selection 不增加 read proof 或写入范围。新增 HTTP 创建/分页及 MCP maintenance_select/maintenance_selection；模型页按完整最终 envelope 缩为实际前缀，不裁 review。

checked daily admission（含无 selection 的旧入口）改为 history→account/shared config lock；所有同步 read 和投影准备在最终 BEGIN IMMEDIATE 前完成。同一连接重检当前事实、活动 task/grant、registration、selection/TTL 与幂等请求，提交 job、canonical alias 和 provenance 后再 wake；selection ID 不进入语义 request hash。原多事务 check_submission 已移除，晚读 replay 控制改用实际 canonical request 与新准入路径，仍保留较晚人工版本不得被历史输出认证的断言。旧历史 baseline 与成功 replay/retained-work 资格分开保留，不用当前 input baseline 否定自己的真实完成结果。

真实缺陷、夹具适配与成本证据分别结算。早期已修复 REPLACE 绕过、触发器过早引用未初始化 jobs、读取不可用误报 missing；job DDL owner 同事务安装 evidence guard，已初始化后缺保护不重建。晚读测试在最后准备之后实际修改/替换并验证拒绝，app 重绑定先验证冷索引 503 再显式补偿。对应红绿日志及完整断言均在 T/maintenance-selection-admission-* 保留，各轮有重叠且不相加。

首个 r1 冻结两环境各 **1943 passed / 9 failed / 13 deselected**，源码一致。九项均是三个旧版本 fixture 保留了未来 selection 表；现由测试专用 helper 先验证当前结构、断言观察/证据全空，再移除未来结构，原身份、来源、历史、恢复和幂等断言保留，定向 **15 passed**。r1 成本脚本在首个样本中错误要求整个准入累计只 hydrate 一次；诊断实测为同一身份在准备投影和最终校验各一次。新测量分开记录阶段、唯一身份和累计加载，逐阶段限制到所选身份及相关历史，并在断言前保存 counters；不据此宣称规模验证通过。

Vera r1 发现 **MADM01**：WikiTask 仍 active 不能证明原 MCP run/work 未撤销。现从已验证 HTTP bearer 保留原 McpScope，沿 checked 路径传入最终 writer，重查 scope_is_live 与精确 token/task/session/owner 绑定；人类/app 路径保持独立。八种新建/复用、selection/旧入口、run/work 撤销组合先 **8 failed**，修正后 **8 passed**；相关准入定向 **23 passed**。Vera r2 已关闭 MADM01/MSEL-G01/C01，批准实现及五组完整规模成本。

`maintenance-selection-admission-r2/freeze.json` 为 **122 source / 407 test-source**，SHA `c1643c850521ac4636b0e8f2c2d8d1c12d06bf90c63af0b318ab8c2fda09aa3d`。两环境完整 Wiki/MCP/账户离线组各 **1960 passed / 13 deselected**，零失败、错误、跳过，原始日志/JUnit、同一用例集合及 ROOT 冻结匹配。`maintenance-selection-admission-review-r2-settlement` 已独立批准本组件 **code+cost+gate**。wrapper 误调 r1 的 false 回执原样保留，正确 r2 回归复用已过成本，没有重测成本或覆盖失败；最终绑定见 `resumed-validation.json` 与 `settlement.json`。全任务、F09 整体和发布仍未批准，ROOT 后续可继续 F12/A01；不把该冻结批准自动扩成后续代码。

## 14. F12：精确授权设计与越权反例

`tests/test_wiki_grant_v2.py` 通过真实 HTTP 和正常 assignment 复现 P02/F12 同一问题：仅授权 `Library/requested.md`，worker 仍能 publish `Library/unrequested.md`，返回 200、落地文件并生成一条操作收据。隔离副本 `wiki-grant-v2-existing-document-red-r2` 保存 **1 failed**、原始日志/JUnit 与文件 manifest；原文未改。首个 runner 在复制可选的 pytest.ini 时失败、未执行测试，另存启动错误，不作为产品证据。

`wiki-grant-v2-design-r1.md` 给出待复核设计：受信准入持久化 V2 grant，既有权限按 doc ID，框架材料 ingest 与 worker 的有限知识页预留分开；一次性创建权沿 prepared/applied/索引证据消费并转成既有身份权。普通 auto 保持有界维护政策，明确 maintain 保留原始命令对象限制；卡片、工具展示、实际 writer 使用同一授权模块。纯创建工作项不伪造旧文观察，旧待办只有可证实范围才升级，其他进入 scope_review_required。该设计及新建工作项的验收尚未批准，产品修正和后续 A01/A02 继续推进；没有发布。

`wiki-grant-v2-design-review-r1` 后续批准设计，保留实现/门禁未批；要求分别处理创建完成证明与历史 baseline，以及封住旧 worker 准入并等待退休。首段已接入 `grants.py` 的持久授权/预留、任务准入、实际 writer 原 scope 复核、同身份续修、卡片/工具列表、材料 ingest 和 creation 工作项。`wiki-grant-v2-worker-recovery-feedback-r1` 在隔离合成数据中 **15 passed**，覆盖单文拒绝新页、合法有限创建及续修、默认目标幂等、明确命令范围、真实落地后恢复同一 change、人工改文保留历史 baseline、5 个输出分批及混合材料 ingest。此前一次 organize 上下文漏传 authority 的 2 项失败已修正；一次 state 响应层级误读仅属测试夹具，原结果均保留。当前相邻回归、旧待办升级/受信重新准入、退休切换和授权账本故障/规模验收仍未完成；此段运行反馈不是 F12 或发布批准。

首段相邻 r2 已有 **219 passed**，零失败、错误或跳过。材料接收改为 ingest，并以既有维护操作清单筛选完成收据，避免将“原始材料已保存”误认为“已整理完成”。用户允许的强关联及必要索引配套范围保持，未采用独立 index_entry 工具草案。

旧库迁移新增受信范围转换：原请求和已有身份可证明时仅转入有限授权，旧全库或执行证据不明的工作保留原 job/key/payload/收据并进入 failed + scope_review_required。owner-only readmit 在原 job 上重新选择有限范围，不重存材料；追加 readmission 事实以记录当前 registration 的重新准入，不覆盖原 grant。普通 retry 不清除范围复核，也不越过尚未退休的 assignment。`wiki-grant-v2-readmission-feedback-r1` 定向 **7 passed**；后续新增材料复用、退休失败和账本丢失/REPLACE 检查仍须随冻结相邻组验收。未变更生产数据或发布。

迁移相邻 r1 已实际 **352 passed**，冻结未变。其后未开始预留的释放/重取定向 **6 passed**；prepared/不确定输出继续保留，不重建。领取后到 assignment 建立前的执行也计入退休检查，预留重取与 retry 排队同事务提交。用户许可的已有导航页追加现被精确正文 grant 阻挡，已有两项 HTTP 红灯；拟通过原 organize 的固定链接追加分支支持，不新增工具/任务/审批，不把索引操作认作主输入整理完成。设计与实现尚须审查，当前结果不是 F12 或发布批准。

后继预留相邻 r2 **359 passed**，原 prepared 发布状态未被清理误恢复；导航 6 项当时明确排除。`wiki-grant-v2-navigation-design-review-r1` 已批准收敛的 organize 追加设计；当前实现只追加 host 生成的本批规范路径链接，保留原字节与旧受管证据，不增加 B 的正文授权。`navigation-feedback-r2` 基础 6 项和 `navigation-management-feedback-r1` 的 6 项局部检查通过；后者包含真实 MCP 默认参数、人工编辑的旧 stale/ownership、后续明确授权整理保留导航区，以及 F09/完成归属。写入边界撤销、同字节替换、真实 worker 与异常恢复已加用例，待当前相邻冻结执行；这些局部结果不替代完整 F12 成本、门禁和独立实现批准。

后继预留相邻 r1 为 **357 passed / 1 failed**；清理未用预留的排他 history 入口误触发发布恢复，使旧 prepared 断言失败。已改为按实际未用行及退休状态决定是否处理，以共享 history 锁等待 writer，不顺带恢复发布。旧断言保持，原不确定发布三变体及混合预留保护等定向 **8 passed**；相邻复验待完成，失败证据保留。

导航与全部相邻/迁移合并组已 **391 passed**，零失败/错误/跳过、冻结未变。新增三篇 grant 查询 VM 用例在 100/1000/5000 条授权下复现 **5300/26100/116800** 步，结果行一直只有三条但连接计划扫描整个 job；改为从本批 chosen 身份 CROSS JOIN 精确 `(job_id,doc_id)` 索引读取后为 **3100/3400/3200**。原红灯、绿灯与旧规模脚本的已完成样本保留；新全规模将区分批次授权开销与 F09 既有精确 total 的索引范围计数，不把 COUNT 的 VM 工作误写为历史 hydration。九个相关产品模块的定向 mypy、Ruff、docs index、diff 检查通过；完整门禁与独立实现批准仍待结算。

r3 后续结算：五组成本实际完成并获本组件限定的独立批准；三项 grant、单篇授权、卡片的工作量不随未分配文档或旧历史比例增长。F09 暖页的精确 total COUNT 仍随候选规模增长；5000 篇宽准入的 setup 实测约 381.4 秒，未宣称恒定或生产 SLA。r3 完整两环境各 **2003 passed / 31 failed / 13 deselected**，源码与用户文档均未变化；Vera 的 `wiki-grant-v2-review-r3` 不批准实现或门禁。

已针对 F12-R303 恢复明确选中 assets Markdown 的既有原位维护例外，创建/未分配导航仍保持限制；F12-G301 的原 task/scope、旧 TXT 历史与 prepared 材料夹具接入修正后，隔离完整相邻/兼容组 **554 passed**。F12-R301/R302 分别用实际 HTTP 复现同 job 跨批路径占位扩权，以及导航 no-change 收据误完成已创建页；原 **3 failed** 保留，修正后 **3 passed**。当前 batch 从 host 选中的 item 捕获不可变 doc/reservation IDs，只投影这些身份的新路径；creation 恢复仅接纳原创建或自身后续真实维护的主项收据。相关测试的 assignment 改为同样的身份捕获，不放松原 scope 或正文保护。新冻结仍须完整两环境、相关成本及同一独立审查复核；F09 整体、whole task、release 均未批准。

r4 已完成绑定静态四项及两环境完整 Wiki/MCP/accounts 离线组：各 **2038 passed / 13 deselected**，零 failure/error/skipped，原始日志/JUnit 与 142 source / 413 runnable 冻结一致。Vera 关闭 R301/R302、认可 G301，批准 r4 五组成本；但指出原 job readmit 对既有 assets Markdown 漏传 existing=True，R303 当时仍阻断。该实际 HTTP 残余已复现并修正：既有文档明确使用既有 Markdown 资格检查，最终同 vault、原 grant doc ID、当前 revision、退休及事务检查保留。

readmit 固定副本的 **6 项定向、559 项完整相邻/迁移/兼容回归**通过，重叠用例不累加；Vera 的 `wiki-grant-v2-readmit-review-r1` 关闭 R303，批准该副本修正。原 job/key/payload/hash/alias/header/授权身份及实际维护收据保全，重新准入追加事实且重放幂等；未选身份拒绝、新建 assets 限制和正文不变均有实际控制。待审副本只变 jobs.py 一处参数与迁移测试，已逐项比对后接回主工作区。后继冻结仍须完整两环境门禁及最终证据结算，未宣称全部整改完成。

成本按同次独立决定有界复用 r4 原五组实测：grant/authorize/card/F09 暖页及宽准入路径不调用 readmit，其他运行材料与 probe 均未变。保留 r4 的 freeze/probe/result 身份，不能改称后继新实测；不证明 readmit 自身、真实负载或 SLA。暖页精确 total COUNT 的规模工作和 5000 篇宽准入约 377.97 秒的原限制继续保留。旧 false/pending、失败记录和各轮实际冻结不覆盖。

## 15. F12 最终结算与本轮停止点

2026-10-18，`T/wiki-grant-v2-review-r5-settlement/result.json` 独立批准 F12/P02 的 implementation、cost（有界复用）、gate 及 code+cost+gate；R301/R302/R303/G301 全部关闭，本组件没有未落盘的验证。该结论仅覆盖 r5 F12/P02，不扩大为 whole_F09、whole_task 或 release，三者仍为 false。

### 已实现的权限与恢复范围

- 授权维护 A 不默认授权任意新建 B；受信任务中的有限知识页创建仍可执行。A 内有依据的 `[[B]]`、派生反向关系和搜索索引同步允许；有效 worker 可在已有导航 Markdown 为本批输入追加固定入口，无需逐次确认。关联页正文不能因此被重写，辅助导航收据不能完成 A 或已创建的 B。
- 领取时捕获本批精确 doc/reservation IDs，卡片、工具与实际 writer 使用同一集合；A 保留身份移走后仍可维护，同 job 未分配的 B 移入 A 旧路径也不能扩权。写入继续复核原活动 scope、注册及当前版本。
- 旧待办范围不明时保留原 job/key/material/receipts 并要求受信重新准入；普通 retry 不能洗掉范围复核。已确认创建输出与后续历史 baseline 分开处理，人工变化或 prepared 不确定性保留，不凭缺文件重新生成。
- 明确选中的 assets/ASSETS 内 `.md/.markdown` 保留原位维护、旧库可证范围迁移及 registration 变化后的原 job readmit。readmit 重放幂等、只追加一次新准入事实，原 payload/hash/aliases/grant/收据和正文保留；未选身份、资产目录新建及未分配资产导航仍按原限制拒绝。

### 最终证据与适用限制

| 证据 | 结算结果 |
| --- | --- |
| `T/wiki-grant-v2-r5/freeze.json` | **142 source / 413 runnable**，SHA256 `62328e02635cab1bd3472af0e610fb62525eb206c74bbd5b414ef1ecd7b7a92a`；运行材料与最后获批候选逐字相同。 |
| `T/wiki-grant-v2-r5/checks.json` | 两环境各 **2040 passed / 13 deselected**，零 failure/error/skipped，exit0、source_unchanged=true；原始日志/JUnit、同一用例集合和用户文档字节已由主控与 Vera 分别核验。两环境用例不相加。 |
| `T/wiki-grant-v2-r5/static/result.json` | 16 个产品模块 mypy、Ruff、docs-index、diff-check 四项通过；不是全后端或全部依赖 body 的类型证明。 |
| `T/wiki-grant-v2-r5/cost-reuse.json` | 按获批适用性有界复用 r4 原五组实测，保留 r4 freeze/probe/result 身份，没有 r5 新测量。 |
| `T/wiki-grant-v2-review-r5-settlement/result.json` | 本组件 `component_code_cost_and_gate_approved=true`，无未关闭发现；whole_F09/task/release=false。 |

成本原测量为 100/1000/5000 篇及每篇 1/10/50 条历史：三项 grant、单篇授权及卡片的工作量不随未分配文档或旧历史比例增长。F09 暖页的精确 total COUNT 仍有随候选量增长的 VM 工作，5000 篇宽准入 setup 约 377.97 秒单列保留；未测量 readmit 自身、真实 Wiki、HTTP/MCP 适配器或生产 SLA。

本轮完整组是 Wiki/MCP/accounts 离线验证；未执行最终全后端、前端、真实 Codex CLI/browser 或发布验收。Claude 未选择或探测，未变 agent 默认、真实 Wiki、生产服务或保护标签。旧 r3/r4 false、pending、红灯和历史阶段字段均保留；最终批准看 r5 settlement，不回写旧冻结的预置未批准字段。

### 收尾与恢复条件

用户要求完成当前工作并完善文档后先结束，本轮到此停止。A01/B03、A02、P01/S01–S03 及整体迁移/发布验收只保留为待办，未启动，须用户明确恢复后再继续。本轮不 commit/push/deploy、不重启或排入新的维护/测试任务。

当时的开发总结与 README 同步只改文档，运行材料仍与 r5 一致；相关检查保存在 `T/wiki-grant-v2-doc-closeout-r1/`。这是上述停止点的历史记录，不能用于证明下节新增代码的验收。

## 16. 简化后的选定收尾：A01/A02/A03/A05

用户已完成代码简化，随后明确选择“**A01，A02，A03和A05就行了，完成这次bug修复优化**”。本轮基线为已发布的 `d546d713903551801a0b6d153c43f2f2614f35e8`；保护 `before-takeover` 与 `wiki` 原引用，保留并行用户文档。选定实现及本机验证已完成，不从旧 F12 冻结覆盖简化后的代码。实际部署提交和运行验收以独立发布回执为准。

本轮证据根目录 **U** 为 `/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-followups-20261018`。设计拒绝、修复前失败、补正和各次源码指纹均保留；`U/review-selected-r3/` 的独立实现审查关闭 R101/R102/R103、R201/R202/G201。最终门禁结算见 `U/review-final-gates/`，实际部署及清理回执位于 `/home/jack/.octopus/users/f527ad605dd4/workspace/wiki-followups-release-20261019/`。临时源码退役后的历史路径由该目录的 `evidence-archive.json` 映射到逐成员核验的证据归档；本页状态不能代替实际发布回执。

| 选定项 | 目的与当前实现边界 |
| --- | --- |
| A01 | 持久 job version/outbox、原入口接收与卡片、精确原 request/attempt 交接、取消退休及幂等 retry。生命周期、账户接管与迁移纳入完整回归，卡片实际取消、断线补齐、刷新和归档通过真实浏览器验证。 |
| A02 | 操作卡、实际可见工具与按需指南共用实时授权视图；policy/guide 版本、有限路径与完整详情入口一致。hash 只标记文本，不授予权限；该轮保留 Codex 每轮 fresh snapshot。后续正常续聊去重及指南按需加载以[当前合同](wiki-development-summary.md#内容与受控操作)为准；本行保留该轮历史验收范围。 |
| A03 | 文件事件 uncertain 时提供 owner inspect/export、具体计划批准与恢复。额外 observer lease 先于真实 drain，分段索引与持久 handoff 阻止提前开放；根替换、故障及来源保全控制通过隔离真实链路验证，生产库不制造 overflow 或试修。 |
| A05 | 已知材料候选先核验，再在 miss 时保留完整补偿扫描。小型合成样本显示命中免目录枚举；未命中多出的查询成本如实保留。不把未扫描当文件不存在，不宣称全库普遍提速。 |

A04/A06/A07 不在本轮：不增加应用重建、多账户压力、运行时大改或其他条件建议。按已有授权交付这四项后结束。Claude 未配置，live 未运行；Codex 必需验证使用既有选定登录和临时测试配置，保留用户代理设置。自然知识评测继续 opt-in。

### 当前候选的状态和恢复合同

原聊天卡片来自 Main DB 的持久接收，唯一身份为 vault/event/subscriber；提交后再确认 Wiki outbox，重放不增加模型轮次，也不编辑旧聊天文字。首账户接管保留原身份，在既有迁移 journal 记录原订阅坐标摘要，并迁移已有接收行；不能将任意 legacy 通知投给当前账户。投递失败保留 pending，有界轮转让后续合法通知得到机会。

取消先阻止新写，再等真实执行退休、原请求终态及已接受产物/索引核验。框架已保存材料但尚未准入模型时，由退休 owner 记录原 attempt 的未准入证明，后续只恢复索引和收据核验，不重新生成材料或消耗未发生的重读预算。提议/冲突仍保留完整详情，但未应用版本不列入“已保存产物”。来源变化、同字节身份替换或缺终态时保留 attention/unconfirmed，不能用 retry 跳过。

混合任务的辅助导航收据独立核验自身输出，不能凭空 item_doc 匹配未创建页面。创建产物只按原 reservation 或非空的主文档身份核验；取消不重置重读预算，显式重试仍按实际重读消耗预算。

恢复管理只供账户所有者，入口为 `/api/wiki/recovery`，不加入 MCP 或 App 工具。下表路径均相对 `/api/wiki`：

| 操作 | 当前行为 |
| --- | --- |
| `GET /recovery` | 不构造普通 Store，读取当前注册、root 可用性、received/applied/armed/uncertain、pending 摘要及已有恢复状态。 |
| `POST /recovery/export` | 导出身份/来源坐标、原 inbox 证据和恢复状态至账户 state 内固定目录，返回 path/bytes/SHA-256；不接收任意文件目标，不复制整库正文。 |
| `POST /recovery/plan` | `{"retain":[]}` 逐项列出保守新身份及旧身份墓碑。需保留身份时传 `doc/path/expected_revision` 的一对一精确映射；当前字节相同本身不证明未知历史。 |
| `GET /recovery/{id}/plan` | 读取完整具体计划和 plan_hash；不能用有界摘要代替批准前查看完整映射。 |
| `POST /recovery/{id}/approve` | 传 `{"plan_hash":"…"}`，只批准这一代的具体计划；尚不改变身份或正文。 |
| `POST /recovery/{id}/apply` | 传同一 plan_hash，有界推进 prepared → mapping_applied → rebuilding → handoff → complete。按返回状态继续同一 id，不另建自动重试任务。 |
| `GET /recovery/{id}` | 读取当前阶段、代际、映射与完成收据；job ID、HTTP 成功或清除旧 uncertain 都不单独证明接管完成。 |

映射只覆盖批准的 cutoff。之后的新事件保留 pending，不能随旧 uncertainty 被确认或删除；普通接收链用同一 observer 实际验证后才 CAS complete、开放入口并释放额外 lease。重建失败保留已提交映射，后续从原游标恢复；完整完成收据处理响应丢失，不能重复执行已接受映射。

来源策略与身份映射同事务保存。保守新身份的旧路径来源保持 unknown；明确保留的身份继续使用原来源观察。cutoff 后编辑导致该文件跳过重建时，普通事件重放也必须沿用这项策略，不能绑定同路径的新来源文件。应用前保留完整事件正文，正常 ACK 后可压缩正文，原事件身份与哈希继续保留。

根目录被替换时，先确认正常 writer 和其余 observer lease 已退休，再保留原 epoch 的证据、接收新 epoch 并重新观察/批准。重启丢失 observer、再次 overflow、注册/文件版本变化都会使原批准失效；不复用旧 move cookie，不猜当前路径占据者就是旧来源。缺失或损坏的 durable ledger 仍失败关闭，不删 DB 或把证据重建为空。

### 本机验证与实际交付

最终完整离线后端 **4231 项通过**，前端单元 **441 项通过**，离线浏览器 **82 项通过**；类型、lint、生成合同、Ruff、mypy 和文档索引检查均通过。mypy 使用仓库现有配置，不宣称检查了所有无注解函数体。原始日志、JUnit/Vitest 结果及源码前后指纹保留各次独立坐标，不累加重叠用例。

本机 Codex marker 选择共 **18 项实际 CLI 测试**：首轮 9 项通过，研究测试在调用模型前因临时 DB 缺凭据元数据失败；补齐该测试准备后，剩余 9 项全部通过。前 9 项只在测试、产品源码与原始证据未变的范围内继承，首轮失败继续保留。真实浏览器的 Codex 会话创建与响应测试 **1 项通过**。使用既有选定登录，临时配置为 `gpt-6.1-sol/high`、正常速度；协议测试自己指定的 effort/速度切换保持原样，无额外付费可用性探针。Claude 实机未运行，不称两后端实测通过；这些协议检查不等于自然知识质量评测。

`U/final-freeze.json` 与最终审查绑定提交范围；README 仅提交本任务生成索引，保留用户手写链接和未跟踪审查材料。独立候选快照先构建并验证资源，再由独立进程等待自然排空后切换。实际 PID/cwd、提交、健康、浏览器、资源哈希、Wiki schema/索引及旧源码退役以发布目录的 `release-acceptance.json` 为准；记录本身不触发另一轮提交或重启。

### A05 同小样本的完整 SQLite 计数

`U/profile-a05-total.py` 在 d546 原源码和当前候选分别使用新进程，输入均为 13 篇合成 Markdown、每个资源目录 20/100 个 HTML，先建库再测热阶段。拦截所有 Python `sqlite3.connect`，在任何 PRAGMA/准入/BEGIN 前挂 trace，包含 inbox 连接及 trigger 回调；trace 次数不是 SQLite VM 操作数。旧探针的部分 SQL 计数不能与此口径混比。

| 阶段 | 原基线 → 当前候选 |
| --- | --- |
| 已有材料命中 | 遍历 8 → 0 目录，枚举 53/213 → 0 文件；连接均为 7；trace 229/709 → 71；读取均为 4 次、132 字节，索引调用均为 1。 |
| 材料未命中 | 保留 8 目录、53/213 文件的完整扫描；连接 4 → 8，trace 192/672 → 229/709，正文读取仍为 0。 |
| 无变化热同步 | 无目录枚举、正文读取或索引调用；连接均为 4，trace 13 → 25，新增恢复准入检查成本保留。 |

结果在 `U/a05-total-cost-{baseline,current}-r1/result.json`，两边源码前后均未变；当前命中单次约 23/24 ms、未命中约 30/54 ms，仅作诊断样本。不据此推断全库、冷启动、多账户性能或生产 SLA，也不把未命中的补偿成本隐去。

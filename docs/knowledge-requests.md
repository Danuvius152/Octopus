# 知识请求与异步工作

`server/knowledge.py` 拥有可信请求合同。入口在包装消息前接管原文；来源要求、维护限制和已经取得的证据分别表达。显式 `/wiki` 的开放问题默认采用本地知识库与相关网页，具体问点和用户限制的解释由共享查询指南负责，不按通道或主题枚举策略。

| 操作 | 拥有的规则 |
| --- | --- |
| `admit` / `for_entry` / `for_account` | 建立新可信入口，保存原要求、账户与注册绑定；不借用会话最近一次任务。 |
| `capture` / `derive` | 模型调用在首个 await 前核对实际活跃 run 和已确认请求；子执行只收紧限制，同时固定直接父任务的返回合同。 |
| `receive_return` | 校验固定任务、目标和 work；父 lease 正常退休不妨碍返回，关闭的 work 抑制自动接管。 |
| `revise` | PATCH 省略保留旧值，权限字段显式 null 拒绝；模型编辑受旧定义和当前调用双方限制，明确的人类控制可建立新授权。 |
| `render` | 向两种后端提供同一来源事实；异步原要求放进可转存的大消息帧，运行配置不重复携带每轮原文。 |

`MaintenanceLimit` 只有提交上限和必须同时满足的选择依据。组合使用逻辑与，保留全部祖先选择。Wiki 的 `require_submission` 在最终写事务中核对主体、注册与完整实际效果，包括新材料、creation、重放订阅和 checked daily。正文写入仍须当前 assignment、request、attempt 与 grant，合同快照不授予写权。

请求消息在启动后端前经 `DB.record_request` 提交并确认。BG、research、委托轮次和计划在自己的持久记录中保存版本化上下文，不保存 bearer 或未来写权限。子执行与返回父任务使用不同上下文，嵌套返回不越过直接父任务。新的返回有自己的 request_id；work_id、delivery_id 保持各自含义。

子执行另存逐层任务要求，嵌套派生保留每层的来源分工和进一步限制；返回直接父任务时恢复当时固定的父合同。只修改维护控制不会清除未修改的来源要求。计划 PATCH 在数据库写入时比较准备所依据的定义，冲突返回 409；原 work 在准备或提交期间关闭时拒绝写入。调度注册在同一数据库写入屏障下重新读取当前定义，迟到的注册不能恢复旧计划。

模型编辑总是继承调用方的具体来源要求，不以 payload 是否含正文区分。fire 也在同一写入屏障内取得已提交的定义快照，随即释放屏障；已回滚的修改不会成为一次执行的依据，后续合法编辑也不回溯影响已接纳的 fire。

旧结果没有合同或版本未知时，仅保留输出、报告和可见交付状态；不据正文补造 read/auto 请求。计划缺少合同会保留并停用，`invalid_reason=knowledge_contract_missing`；经认证的人类可在计划页面查看原消息，明确选择 Wiki 维护控制（API 的 `wiki_mode`：auto/read/maintain）重新授权，再另行恢复运行。模型不能靠改正文或省略字段恢复权限。已捕获的 fire 使用自己的定义快照，计划后续编辑或删除不会改变该次执行。

新的 API、MCP 与界面控制只接受 `auto`、`read`、`maintain`；携带 `wiki_mode=off` 的新 POST 返回 422，在接管对话、任务或执行前拒绝。仅旧计划的模式展示列 `wiki_mode=off` 在所属存储升级中归一为 `read`；执行合同保持原字节，运行消费其中已有的 canonical `read`。缺少或无法验证的合同保留原文并停用，不从正文或旧控制重建权限。已受理客户端输入的原始 payload、payload_sha256 与 client_id 保持不变；确认同一 ID 的受理状态使用现有鉴权保护的 `GET /api/sessions/{session_id}/submissions/{client_id}`，不重新 POST 旧 `off`，也不将它改成 `read` 后重算 hash 并冒称原输入。

Research 的报告完成与后续交付分别保存和广播。交付被抑制时，聊天保留去重的材料通知和原因；卡片展示未交付状态，重新加载仍能查到报告位置和原因。

主要回归位于 `tests/test_knowledge_*.py` 与 `tests/test_wiki_request_limits.py`，同时覆盖真实派发、数据库提交、HTTP、合成 CLI 协议和实际 Wiki 门禁。自然答案的 `knowledge_eval` 仍为 opt-in，不由这些结构测试替代。

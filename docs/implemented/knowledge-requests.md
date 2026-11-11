# 知识请求与异步工作

> **Status:** implemented — 可信来源要求、维护限制、逐层任务派生与原父任务返回合同。

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

合同还保存原工作的 `interaction_policy`，与来源类型、Wiki 三态和维护上限分开。
人工工作的 BG/research/委托返回继承原人工策略，不能因返回是机器注入就改判无人值守；
计划和 Wiki worker 根工作使用 unattended，旧合同缺字段仍为未知，不按会话最近一轮补值。
问题等待和缺失决定的处理归[原工作交互策略](input-reliability.md#原工作交互策略与等待)，
对应持久化和真实异步返回回归见
[test_work_interaction_policy.py](../../tests/test_work_interaction_policy.py)。

子执行另存逐层任务要求，嵌套派生保留每层的来源分工和进一步限制；返回直接父任务时恢复当时固定的父合同。只修改维护控制不会清除未修改的来源要求。计划 PATCH 在数据库写入时比较准备所依据的定义，冲突返回 409；原 work 在准备或提交期间关闭时拒绝写入。调度注册在同一数据库写入屏障下重新读取当前定义，迟到的注册不能恢复旧计划。

模型编辑总是继承调用方的具体来源要求，不以 payload 是否含正文区分。fire 也在同一写入屏障内取得已提交的定义快照，随即释放屏障；已回滚的修改不会成为一次执行的依据，后续合法编辑也不回溯影响已接纳的 fire。

旧结果没有合同或版本未知时，保留仍有当前职责的输出、报告和可见交付状态；不据正文补造 read/auto 请求。现有计划定义缺少合同会保留并停用，`invalid_reason=knowledge_contract_missing`；经认证的人类可在计划页面查看原消息，明确选择 Wiki 维护控制（API 的 `wiki_mode`：auto/read/maintain）重新授权，再另行恢复运行。模型不能靠改正文或省略字段恢复权限。周期 fire 保留已捕获的 prompt 与知识合同，计划后续编辑、暂停或删除不会撤销该次在途执行。一次性 fire 仍须在持久输入接纳事务中比较原定义；读取快照不等于输入已接纳，边界归[计划触发](scheduled-runs.md#captured-fire-definitions)。

新的 API、MCP 与界面控制只接受 `auto`、`read`、`maintain`；携带 `wiki_mode=off` 的新 POST 返回 422，在接管对话、任务或执行前拒绝。缺少或无法验证的计划合同保留并停用，不从正文或旧控制重建权限。

当前启动只接受现行完整主库格式或真正空库，不执行旧格式0–3升级到4的历史流程。当前格式声明、发布期有限转换与资料保全归[存储合同](current-storage-format.md)。旧 `off` 展示值、旧请求清退及当时的转换取舍查[固定历史版本](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/knowledge-requests.md)；不能把旧转换条件当成当前启动能力。

`previous_writers_retired` 仍由受管安装的原生命周期提供，并参与当前输入和 Wiki 任务恢复：它只证明原代 writer 已退出，不证明模型消费、历史通知 ACK 或未知效果成功。独立来源返回仍按自己的身份和合同完成交付。任务恢复与原引用的使用见[Wiki合同](wiki-development-summary.md#恢复与效果核验)和[安装生命周期](installation-lifecycle.md)。

当前三态受理的原始 payload、payload_sha256 与 client_id 保持不变；鉴权 `GET /api/sessions/{session_id}/submissions/{client_id}`、同ID去重、恢复权限上限及唯一后继继续有效。当前缺少旧身份查询结果也不能证明从未受理，客户端不得据此自动重发。详见[输入接纳与恢复](input-reliability.md)。

Research 的报告完成与后续交付分别保存和广播。交付被抑制时，聊天保留去重的材料通知和原因；卡片展示未交付状态，重新加载仍能查到报告位置和原因。

主要回归位于 `tests/test_knowledge_*.py` 与 `tests/test_wiki_request_limits.py`，同时覆盖真实派发、数据库提交、HTTP、合成 CLI 协议和实际 Wiki 门禁。自然答案的 `knowledge_eval` 仍为 opt-in，不由这些结构测试替代。

# DeepSeek Harness：正式后端接入计划

> **Status:** proposed — 以官方 SDK 和同宿主薄插件接入 DeepSeek；锁版能力门、全部消费者与真实模型验收尚未完成。

## 当前基线与交付目标

Octopus `fd7ab9c` 的 [BackendKind](../../server/models.py)及
[注册入口](../../server/harness/__init__.py)只有 `claude-code`、`codex`。
普通 Codex 已使用 App Server；[Research leaf](../../server/research/leaf.py)与
[one-shot](../../server/harness/harness.py)已有真实 spawn 前授权。
这些当前能力不重新立项。DeepSeek 模型选项、effort 映射或可执行文件存在，
都不表示 DSH backend 已接入。

目标是让用户在现有 Agent、会话、命名委派、计划、App、Wiki 和 Research 流程中
选择固定后端值 `deepseek`。DSH 拥有原生模型循环、工具、历史和子任务；
Octopus 继续拥有输入、独立工作派发、问题和业务结果；Harness 拥有运行资源。
保留 Claude 原生支持和 Codex 当前行为，不将用户配置迁到新 backend。

本页合并旧四阶段路线与两份 DeepSeek review 的有效设计，替代 rc.2 的 ACP/headless
条件主稿。**这是未实施计划**，没有因文档归位取得 loader、工具权限、真实模型或生产证明。
现有共同接口由 [Harness](../implemented/harness-layer.md)、
[输入与完成](../implemented/input-reliability.md)和
[安装生命周期](../implemented/installation-lifecycle.md)维护。
G1/G2 与 Codex R2 已交付；DSH 不等待旧 G0-Recursive 强目标恢复，也不以远程 Runtime 为前置。

<a id="deepseek-design"></a>
## 1. 先锁版，再验证公开组合

推荐起点是仓库 **2026-10-30～31 调查记录**中的 `0.2.1-rc.5`：
一个 attempt 一个 Node 宿主，官方 SDK server 加同宿主薄插件，经 stdio 接入。
当时只下载并按 npm SRI 核对发行包、阅读类型和编译源码，没有安装或运行 DSH。
rc.5 不是本次重新认证的最新版本。

一次候选须固定 Node/OS、顶层包、完整解析依赖锁、官方 profile、插件源码/构建摘要、
不含秘密的启动配置摘要及实际 provider/model 能力。分包 dist-tag 当时不一致，
不能分别安装 `latest` 组成运行时；三个 SRI 也不能替代完整依赖闭包。
安装在候选准备阶段完成，普通模型请求不运行包管理器。

| rc.5 的已记录接口语义 | 设计后果 |
| --- | --- |
| `Agent.followup` 是 next-turn；`steer` 是 next-step，idle steer 会开新 turn；`inject` 不唤醒。 | 原生活动内纠偏使用公开 steer，必须防止 idle/closing 竞争。 |
| SDK `session/prompt` 调 followup，返回 messageId；有 resume/wait/shutdown，无独立 steer/cancel/close wire。 | 复用官方 server，只补缺失控制与观察。 |
| 高层 `HarnessSession.run()` 收集到下一次根 idle。 | 普通请求使用低层 wait、有序事件和输入/结果证据。 |
| wait 等根与正在推进的受管后代，再复核根 idle 和 seq；idle 且仅有 parked inbox 的后代可能不阻塞 wait。 | wait 返回不是完整成功。 |
| 子 settlement 父投递失败、部分 flush/disposal 失败可能只 warning 或被 allSettled 收束。 | 退出码、通知和 wait 均不能单独证明父处理或可靠落盘。 |
| ACP 一个 session 仅一个 inflight prompt，事件投影较少；headless 无轮内输入且有裁剪投影。 | 保留选型依据；不为迁就旧 exec 降格已选交互和原生范围。 |

2026-10-31 的升级候选源码 `ce7073d51254c26cd63b22a60835b8f948deb447`
（manifest `0.2.2-alpha.2`）已有 `user-question/ask`、`approval/request`、`session/query`。
源码有接口不代表已验收发行组合，也不自动保留 Octopus 系统到期来源和 tool-call 身份。
开工时先检查可重建的新组合能否删掉薄桥，再按同一能力门选择；不要每请求猜版本或自动换协议。

## 2. 所有权与同宿主薄插件

| 所有者 | 唯一责任与提交点 |
| --- | --- |
| SessionOwner | 原输入 receipt、逐条派发、人工 steer 资格、问题结算、唯一业务 terminal。 |
| HarnessRun | 原 attempt 的准备、spawn、进程组、管道、lease、临时秘密、停止升级及退出确认。 |
| DeepSeek adapter | 原生帧、输入和结果的证据映射；不直接写业务完成、不调度产品 backlog。 |
| 同宿主薄插件 | 精确根绑定、公开原生控制、必要事件/问题桥；不另拥有执行账本或产品 timer。 |
| DSH SDK/runtime | 根创建/resume、inbox、模型循环、工具、原生历史、受管后代、wait 和 handle disposal。 |
| BG/Research/委派 owner | 各自业务结果与回流投递，继续走公共 `admit_return`。 |

产品 SQLite 与 DSH 原生历史记录不同事实。连接内关联索引用于解释原帧和原生消息，
失去证据后保留 unknown；不凭相同正文重建成功，也不新增第三套任务真相。
共同语义使用现有原绑定、配置、输入、完成、退休、问题与排空类型，
不把 Codex 的 threadId/expectedTurnId 扩成全局协议字段。

推荐 `dsh --profile sdk --patch <captured-patch>`，插件**替换 SDK stdio wrapper**，
使宿主只有一个 stdin reader/dispatcher。只从包根导入公开
`HarnessSdkJsonRpcServer`、`JsonRpcLineTransport` 与 Agent/tools 类型。
普通方法经阶段/身份校验后交给 `server.handleRequest()`；不读 private sessions、
不导入 `src` 私有模块，不重写模型循环、工具执行或历史。

首版 allowlist 为 initialize、session/resume、session/prompt、session/wait、shutdown。
initialize 一次，resume 只开精确预期历史且在首输入前；首 prompt 在调用官方异步方法前
同步关闭首输入门，写后 unknown 不重新开门。未知方法和第二 root 在 SDK 前拒绝。
`working-directory/get` 也可能创建 root，不能作为无副作用探针盲目放行。

受信启动配置先给出原 attempt 和预期 session，并在创建前注册公开 `agent/created` 观察。
只保存匹配 session 的 exact-live-root Agent 对象；每次控制核对 registry 仍是同一对象。
同 ID 替换、取消后迟到创建、compaction 变化不自动重绑定。
SDK 仍持有创建 handle，插件只借公开控制引用，最终由 server.shutdown 释放。

拟议 `octopus/*` 仅由 adapter 消费，不宣传为官方能力：

| 方法/通知 | 必要关联与最强事实 |
| --- | --- |
| `octopus/steer` | attempt/root/input/连接内 expectedActivityId；明确拒绝或取得 inbox 接纳证据。 |
| `octopus/cancel` | 原 attempt/root；只证明请求取消，不证明 retired。 |
| `octopus/question` | 原活动、tool-call/rootCallId、反向 RPC 与产品 question；回原调用的类型化结算。 |
| activity/input-observed/subagent | 原生 turn/step/seq、messageId、runId/provider/local/parent；不从 UI 推测身份。 |

明确准入拒绝返回结构化 reason，避免任意 Error 被压成同一 `-32603` 后失去分类；
程序错误保持程序错误。新发行组合正式覆盖某项能力且通过同一反例后，删除对应自有桥。
后端 Node 运行依赖不混入 React 依赖树。

## 3. 输入接纳、steer 与完成

附件、配置和消息规范化等可能 await 的工作先结束。最后同步核验原连接/attempt、
exact root、活动标识、原 turn 仍开放、未 aborted 的活动 signal、插件发送门，再调用 steer；
核验到调用间不 await。仅 `status == running` 不足以排除停止中活动。
没有公开可靠活动证据的窗口明确拒绝，Owner 沿原输入规则排队，不能偷偷开 next-turn。
Stop 先关发送门，原生失败/外部取消也关闭其资格。

| 输入事实 | 能证明什么 |
| --- | --- |
| 产品 receipt | 已接纳原输入，未证明已送 DSH。 |
| 原帧写出 | 可能生效；断连不是未接纳证明。 |
| prompt response / inbox inserted | 对应 messageId 已入队，未证明模型消费。 |
| inbox claimed | 循环取走消息；pre-step 仍可拒绝。 |
| 原生 user/message 与 step/terminal 关联 | 按取得的实际记录判断该输入参与和结果；原生日志事件仍不自动证明磁盘可靠 flush。 |

每条原输入的 `session/prompt` 或 `octopus/steer` 首次写出前，原输入 owner 必须可靠提交
“发送开始、原生接受未确认”的事实；提交失败或提交结果未知时禁止发送。
沿用现有输入记录，不新建表或状态机。该提交和关联注册须在最后同步准入核验
之前完成，核验到调用之间仍不跨 `await`；纯控制宿主 spawn 不等于业务输入发送。
分别验证“记录后写出前”和“可能写出但响应未记录”两个硬崩溃点：恢复保守
保留原 unknown，不能因原生日志查无记录而重发。反向问题答复沿原问题记录，
不另造普通输入。通知可先于 ACK，暂存必须有界，不能按正文/到达时间配对。
首输入和每条人工追加分别保存证据，根成功不能把 unknown 或 discarded 输入一起标 done。
ACK 丢失不自动 resend、换协议或换 backend，同 RPC ID 不是假定的原生去重键。
BG、Research、命名委派继续按公共接纳顺序逐条派发，Stop backlog 与人工例外不变，
不预装到 DSH next-turn inbox 形成第二个产品派发者。

DSH 本次选定范围包括根、受管本地原生后代及必要子结果驱动的父后续处理，
可跨多个原生 turn。未验证的远程 provider、Goal/Schedule、自主后台调度在实际 profile 中禁用。
这是 DSH 的待验收范围，不反写为 Codex R2 已承诺完整 post-final 递归。

提交 success 前同时需要：必要输入有对应处理证据；低层 wait 与有序事件证明选定范围闭合；
必需子结果进入父处理或明确的 caller-delivery 通道；最后相关根 outcome 成功；
完整最终根 item 已持久并映射为现有结果引用。任何一项缺失都不能用非空正文补齐。
显式设 `maxTokensAsSuccess: false`，仍保存原 stopReason；error/refusal/blocked/max-tokens
不被 SDK 的 accepted 投影吞掉。父确已处理 child 失败并恢复时，child 失败不必然使根失败。

settlement 的 `subagent-settled/senderSessionId`、公开 activation/runId 与父输入/步骤形成因果链。
同 child 复用两轮或快完成不得只按 child ID 关联；缺 runId 时先证明单激活及事件顺序。
父投递失败/必需消息 parked 时报告具体未证实，不由 Octopus 再补一条总结 prompt。
测试 A 根初答→B 子正文→C 中间说明→D 根最终→usage-only 尾项，结果只引用 D。
业务仍沿 success/failed/interrupted；unknown 是输入证据，原生原因保留，不另造平行状态机。

## 4. 唯一问题入口与审批

当前 [MCP 装配](../../server/harness/assembly.py)在关闭 ask deadline 时用 `2**31-1` 秒。
rc.5 MCP client 接收毫秒且默认 60000；历史 Node v24.21.0 最小实验中
`2147483647000ms` 被压成 1ms。不能直接换算 sentinel、猜零等于无限或暗加有限截止。

推荐 rc.5 使用公开 `defineTool()` 的同宿主问题桥，省略工具 `timeoutMs`，
等待绑定 `ToolExecution.signal`，由现有 [Question owner](../../server/sessions/owner_questions.py)
唯一解释 deadline、答案与关闭。禁用被替代的 `tool-ask-user`、重复 MCP ask 和 timed 入口，
同步真实工具说明；普通 MCP 工具各保留原 timeout。桥不新增 timer/Question 表。

| 原结算 | 原工具输出必须保留 |
| --- | --- |
| 人类回答 | 原题目 ID、selected 多选、自由文本、human source；不从格式化文本反解析。 |
| 系统到期 | 原 `AUTO_ANSWER_TEXT`、system/auto 来源和到期原因；没有伪造的人类 answers/custom。 |
| closed | 原关闭原因及系统来源；signal 已取消时保留工具取消语义。 |

公开 output schema/render 显式支持判别联合，写回**原 tool-call 的 tool/result**，
不塞入官方不支持的字段，不发 prompt/steer 或伪造 user message。
有限 deadline 的系统指导与无 deadline 等待共用一条路径；到期/答案/Stop 竞争只结算一次，
晚答不能开新 turn。原 connection/attempt/exact root/活动/callId/rootCallId/反向 RPC 都要关联，
不能绑定“最近问题”。

首版问题桥显式 exact-live-root guard：普通 root 支持；命名委派的独立 root 沿原转发；
同宿主原生 child 明确拒绝并使父可见，不生成悬挂问题；leaf/one-shot 不装普通问答。
公开 defineTool 不会自动继承官方 question service 的 root-only 限制。
如实际共同产品保证要求 child 直接问答，则此范围不够，须在同一 owner/桥内验证祖先授权和原通道，
不能降格或假称上游绝对不支持。

审批是具体操作许可，与普通问题分开；选定组合能映射既有批准/拒绝通道才启用，
否则明确拒绝能力或在准备期阻断，不能默认批准。
新发行版若能原生保留全部问题来源/身份/取消，则删除桥中被替代部分，不同时保留两条入口。

## 5. 配置、账户、工具和三类用途

固定 backend 值 `deepseek`，不另设同义 `dsh`。扩展模型枚举、注册、凭据、能力 API、
home 解析、生成前端契约和真实 UI；不为旧 `claude_session_id` 的中立用途顺带迁移字段。
凭据以 account/backend/credential 解析，换 key 复用 PATCH；不按 cwd/key 猜账户。
请求准备时捕获配置，实际 spawn 前复核当前授权；活动请求保留原捕获值。
默认可合法改变，旧会话按有效配置修正后继续，不恢复跨旧会话的保存门禁。

账户/credential 私有 DSH_HOME、项目 cwd、canonical memory 与认证各有归属。
含秘密 patch 由 launch owner 持有到实际退休后释放，不在纯 argv renderer 内创建又删除。
日志按字段掩码实际 key/bearer，不抹去普通 argv、异常链或有用协议诊断。
cwd 校验与 home 分离不等于同 OS 用户的恶意租户沙箱。

MCP 逐 entry 保留原 key/URL、命令参数、environment、独立 bearer、installation/task/work/lease。
不合并全局 token、不重建 URL；初始化失败按工具必要性裁定，关键授权工具失败不能静默降级。
核对 DSH `mcp__<serverName>__<rawName>` 规范化后的实际名称与 prompt 一致。
模型/effort/tier/工具策略来自后端真实能力 API，前端没有第二份静态真相；不支持组合明确拒绝。

| 用途 | 候选装配与必须验证的边界 |
| --- | --- |
| 普通 Agent | 选定 SDK profile、原生工具/后代、授权 MCP、项目规则和 memory；可见 schema 与实际执行 guard 都兑现 allow/deny，包括 scoped/PTC 强行调用。 |
| Research web leaf | 明确 web 工具组合，排除无关 MCP、memory、shell、写入、fan-out；分别验证可见工具、强制调用结果和真实外部效果，不能用 read-only 代替 web-only。 |
| tool-free one-shot/reason | 独立 LLM-only composition：官方 `LlmRuntime.prepareCall()/stream()`、`tools: []`、原 AbortSignal 与公开 BlockAssembler；不加载 agent-loop/dispatcher/MCP/subagents、不建持久 Session。 |

`sdk-minimal` 仍有 shell/image/working-directory，不是 tool-free。
文本 worker 检查唯一 finish 的真实原因；tool-call、error/aborted/max-tokens、缺终态或异常 EOF
报 one-shot 错误，不调用工具、不再 prompt、不 fallback 普通 profile、不重放写后未知请求。
独立文本用途不构成第二条普通 DSH 主路线。

稳定 controller、当轮指令、原生 AGENTS/skills 与 canonical memory 分开。
普通请求保留 DSH 项目规则发现，因为 Octopus assembly 不替代整个项目规则加载。
公开 prompt section 使用 `interpolate:false` 保留 `{{x}}` 字面量；resume 后验证当轮 Wiki 范围和规则实际生效。
真实工具服务端仍是授权权威，不覆盖用户 persona。

## 6. Stop、协议预算、退休和数据交接

Stop 先关产品/插件发送门并关闭原问题，取消原 Agent，再走完整 server.shutdown、
受管 descendants drain、handle dispose、launcher 清理；Harness 最终确认原进程组和 I/O 退出。
root idle 而 child 活动时 cancel 可能无事可做，仍须 shutdown/drain。
取消 Python await/to_thread、ACK 或 iterator 关闭都不证明线程、回调、后代已退。
迟到 spawn/callback 保留原 attempt；旧 writer 未退休不安装同历史第二 writer。

协议 reader 与展示分离：慢 UI 不阻塞 response、问题和 Stop。
rc.5 官方 line transport 直接 output.write、无完整累计预算；组合官方类不自动获得有界保证。
在实际 stream 边界明确字节/帧预算与超限归属，验证分片 UTF-8、跨 chunk 换行、超长正文、
双向相同数字 RPC ID 和真实 pipe 阻塞。可合并 transient delta 不包含工具参数、最终正文或关键事实。
无法持续背压时达到硬限使原 attempt 明确失败并退休，不能无限排队或静默丢关键事件。
预算由现有同类负载和 DSH 实测决定，不为文档整齐发明一套常量。

准备、未完成 RPC、问题回调、后代、关键事件、退出/秘密清理失败都进入原 owner 的排空事实。
业务成功后通知或退休失败，不覆写原结果、不重跑模型。
普通 provider 错误在原 attempt 收束；只有共享状态失信或共同合同要求时才扩大 fail-stop。

输出 delta 用于展示，完整 item 按原身份持久一次；child/partial/commentary 不覆盖根 final。
usage 按可证明的 step/root/attempt 口径，未知保留 null，不重复累加含 child 的 root total。
Monitor 展示原 request/attempt 与准备、TTFT、tail、退休及资源成本，不裁定完成，缺 timing 不造 TPS。

resume 只开同账户/backend 的精确原历史；缺失、不可读或不兼容明确失败，不造空 root 冒充续聊。
fork/rewind 可采用已选 HISTORY_REPLAY 合同，原生 clone/handoff/pull 未经证明不展示为支持。
新增持久 home/history/附件须在同一交付更新
[存储](../implemented/current-storage-format.md)和[备份/账户搬迁](../backup-and-migrate.md)的
收集、排除、恢复与验证；临时秘密、socket 和 RPC 不导入为新授权。
账户搬迁仍遵守原聊天/登录排除范围，不偷换为必须导入所有原生历史。

## 7. 能力门、消费者与完成条件

先做最可能推翻方案的 **D0**：隔离 home/cwd、真实 loader/pipe、假 provider，
验证公开组合；不沿用用户日常 DSH_HOME。每个反例必须有原身份、关键事件和实际退出证据。

| 反例组 | 通过标准 |
| --- | --- |
| 安装/loader、双 first-prompt、第二 root、同 ID 对象替换、迟到创建 | 可重建；单 reader/dispatcher、纯协议 stdout、一次准入、只控制原 exact 对象、迟到资源收束。 |
| steer 与 idle/stopping/abort 竞争、通知先于 ACK、丢 response、claimed 后拒绝、取消丢弃 | 输入各层证据准确；合法 steer 当前活动，其余明确拒绝；unknown 不重发、不因根 terminal 变 done。 |
| 发送前提交失败／未知、记录后写出前硬崩溃、可能写出但响应未记录时硬崩溃 | 未可靠提交不发送；恢复保留原 unknown，原生日志缺失不触发重发；问题答复仍沿原记录。 |
| 根早 idle、孙/子晚回、父再派生、快 child/同 child 多 run、父投递失败/必需 parked | wait 不提前结算；必要父处理有因果证据；final 只选 D；error/max-tokens 保留 partial 而不成功。 |
| 有限/无截止问答、答案/expiry/Stop 同刻、原 child/委派 root、嵌套 tool-call | 唯一 owner 一次结算；人类/系统/closed 保真；超传统 60 秒仍可答/取消；晚答无新轮；guard 与身份准确。 |
| 满输出队列、超长 UTF-8、同时问题/Stop、flush/EOF/shutdown 失败、子进程不退 | 控制可推进或有界失效；必要内容完整或明确失败；原资源继续被持有直至退出/失败接管。 |
| 普通/leaf/one-shot 强行禁用工具，两账户/两 bearer、换 key、resume 配置变化、撤权 | 工具集合、执行与外部效果符合用途；当前授权与原配置不串账户/请求。 |

仅源码支持时标“实验待执行”。需要私有字段、猜日志、sleep、补模型 prompt 或跨协议自动重试
才能通过必要行为时，D0 假设被推翻，回到版本/选型/范围裁决。

| 消费者 | 必须经公开入口验证的结果 |
| --- | --- |
| 普通会话 | new/resume、文本/工具、steer、首输入 unknown、原生后代/父 final、Stop。 |
| 命名委派 | 可追问、原问题转发、最终根结果投影、业务与回流分别结算。 |
| BG/Research 返回与计划 | 原 source/receipt、逐条派发和 Stop backlog、捕获配置/凭据、无 DSH 第二调度器。 |
| App build/ask | 真正根结果才触发完成消费，partial 不作成功；删除等待原资源退休。 |
| Wiki worker | 原 task/grant/lease、撤权/重绑、原执行退休与文件效果分别闭合。 |
| Research web/reason、one-shot | 两种 leaf 与纯文本分别兑现限制、准入、错误和取消清理。 |
| 前端与 Monitor | AgentForm/Harness/凭据/模型控件、问题/任务卡、重连及账户切换；根 idle/child running 仍能 Stop；UI 不解析 DSH RPC。 |
| 备份、搬迁、发布 | 新持久根完整收集/排除/恢复；pending RPC/问题/资源不被排空漏掉。 |

实施按可评审的责任单元推进：D0 锁版/反证 → backend/配置/资源 → 普通生命周期与输入 →
完整范围/问题/三种工具用途 → 全消费者/UI/资料 → 真实候选验收与合同交接。
单元不是用户可用声明；正式启用前必须完成所选全部行为、失败路径与替代路径删除。

正式完成还要求：DSH 真实凭据/模型和浏览器通过；Codex/Claude 受影响共享离线覆盖保全；
唯一普通执行路线，重复 ask/自动 fallback/二后端硬编码遗漏退出；版本、能力和限制写入所属合同。
没有真实 DSH 凭据可完成实现与离线检查，不能称正式可用。
源码、fixture、真 pipe/loader、真实模型、产品浏览器证明不同层面；测试选择和证据复用只由
[testing.md](../testing.md)维护，不在本页复制数量库存或另设门禁体系。

## 8. 失败裁决、非目标与历史

字段、生成契约、配置装配和确定事件映射问题在原 owner 局部修正，重跑受影响反例。
公开 API 不能精确控制、必要父处理不可证明、问题来源不可保真、工具限制可绕过、
资源不可退休或预算不可兑现时，停止正式启用并记录最小反例，重新比较新原生 wire、ACP 或范围。
DSH 失败不自动切 Codex/Claude；软件回退不重放 unknown、不逆迁移新资料或覆盖成功业务。
发布操作归 [本机流程](../local-wsl.md#自动发布)。

本期不建跨请求池、第二 SessionManager/工具循环/任务树、远程原生子任务、Goal/Schedule、
child 直接控制/问答、通用 transcript 浏览或跨后端原生历史转换。
这些需要实际场景、能力证据和独立范围选择；冷启动慢先测量，不自动建立常驻 daemon。

历史输入均固定在 `fd7ab9c`：
[旧 rc.2 条件稿](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/deepseek-harness-backend.md)、
[四阶段路线](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/engineering-runtime-roadmap.md#deepseek-design)、
[10/30 设计](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/review/Implementation_Plan_2026-10-31/Octopus_Phase_4_DeepSeek_Integration.md)、
[10/31 补审](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/review/Implementation_Plan_2026-10-31/04_Phase_4_DeepSeek_Harness.md)。
其中旧 G3 缺口、类名和串行票号不再作为当前未完成清单。

### 固定发行证据索引

下列来源是上述历史调查的固定 metadata/tarball 入口；重组未重新执行外部核查。
未确认 gitHead 时，不把 npm 发行包与 GitHub HEAD 说成同一构建。

| rc.5 包 | 当时核查范围 |
| --- | --- |
| [dsh](https://registry.npmjs.org/@deepseek-ai/dsh/0.2.1-rc.5)、[sdk-app](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-app/0.2.1-rc.5)、[sdk-minimal](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-minimal/0.2.1-rc.5) | launcher/profile、生命周期、maxTokens 默认、实际工具集合。 |
| [sdk-jsonrpc-server](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-jsonrpc-server/0.2.1-rc.5)、[sdk-protocol](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-protocol/0.2.1-rc.5)、[sdk-client](https://registry.npmjs.org/@deepseek-ai/dsh-sdk-client/0.2.1-rc.5) | exports、prompt/resume/wait/shutdown、dispatcher/wrapper、line buffer/write/flush、高层 run。 |
| [agent](https://registry.npmjs.org/@deepseek-ai/dsh-agent/0.2.1-rc.5)、[agent-loop](https://registry.npmjs.org/@deepseek-ai/dsh-agent-loop/0.2.1-rc.5)、[subagent](https://registry.npmjs.org/@deepseek-ai/dsh-subagent/0.2.1-rc.5) | 公开 Agent/registry、steer/cancel、受管后代 drain/settlement、run/source。loop 源码只作观察，不私有导入。 |
| [user-questions](https://registry.npmjs.org/@deepseek-ai/dsh-user-questions/0.2.1-rc.5)、[tool-ask-user](https://registry.npmjs.org/@deepseek-ai/dsh-tool-ask-user/0.2.1-rc.5)、[tools](https://registry.npmjs.org/@deepseek-ai/dsh-tools/0.2.1-rc.5) | root guard、legacy/timed、defineTool/output schema、callId/signal、无 timeout 工具。 |
| [mcp-client](https://registry.npmjs.org/@deepseek-ai/dsh-mcp-client/0.2.1-rc.5)、[system-prompt](https://registry.npmjs.org/@deepseek-ai/dsh-system-prompt/0.2.1-rc.5) | 名称与 timeout；literal section。 |
| [llm](https://registry.npmjs.org/@deepseek-ai/dsh-llm/0.2.1-rc.5)、[llm-deepseek-api-key](https://registry.npmjs.org/@deepseek-ai/dsh-llm-deepseek-api-key/0.2.1-rc.5)、[ACP](https://registry.npmjs.org/@deepseek-ai/dsh-acp/0.2.1-rc.5) | prepareCall/stream/GenerateOptions、API-key adapter、替代协议的投影边界。 |

历史升级机会：[ce7073d5 的问答桥](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/sdk/server/src/user-questions.ts#L34)、
[wire types](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/sdk/protocol/src/types.ts#L17)。
它们不替代选定发行组合的 D0/G4。

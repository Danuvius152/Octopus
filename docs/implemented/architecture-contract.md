# Octopus 当前跨模块契约

> **Status:** implemented — 当前所有权、输入、完成、资源退休、账户与数据保证；不代表生产部署。

本文维护代码库当前跨模块的决策、理由和行为边界。
当前约定直接在所属章节修订；实施顺序、逐轮访谈和已被替代的状态保留在 Git
历史，不再追加到正文。文档中的支持范围不授予代码实施、发布或生产数据清理权限。

公共返回接管已供 BG、Research、委派终态及问题使用，当前合同见
[公共返回接管](#公共返回接管)。会话读取与安装交接同样由下文维护；后续运行时范围由
[工程路线](../plans/engineering-runtime-roadmap.md#phase-2)维护。

## 文档职责

| 文档 | 唯一职责 |
| --- | --- |
| 本文 | 当前跨模块契约、必要取舍与能力边界 |
| [architecture.md](../architecture.md) | 当前组件地图、接口和数据模型概览 |
| [current-storage-format.md](current-storage-format.md) | 当前格式声明、存量验证及当次发布交接 |
| [backup-and-migrate.md](../backup-and-migrate.md) | 完整备份与账户搬迁的范围、命令和恢复操作 |
| [testing.md](../testing.md)、[local-wsl.md](../local-wsl.md) | 验证门禁、证据复用及本机发布操作 |
| [Wiki 当前合同](wiki-development-summary.md)、[知识请求](../knowledge-requests.md) | Wiki 领域行为及跨请求工作、返回身份与通知规则 |

实现事实以相关源码和行为证据核对；上线状态以实际服务及发布回执核对。
较新的文件日期、计划状态、旧测试数量都不能替代这两项证据。

## 所有权与故障模型

| 事实 | 权威与责任模块 | 消费者边界 |
| --- | --- | --- |
| 本代执行资格与旧代退出 | `main`、安装锁、`ServiceHealth`、受管 supervisor | 捕获同一代的真实执行句柄；历史 PID 不授予停止权 |
| 接纳、顺序与执行绑定 | `client_submissions` 和 Database 业务事务 | mailbox 传命令，界面读取投影，不再保存另一份待执行正文权威 |
| 活动执行、问题与生命周期意图 | 每会话一个 `SessionOwner` | `SessionManager` 保留入口、注册、跨会话配额及原 Owner 失败接管 |
| 业务结果 | 主库精确 terminal 及其输入关联 | 通知送达、HTTP 等待者和资源退休分别取证 |
| Wiki 账本归属与准入 | Registry 的注册、Construction 与生命周期 | 普通 Store 借用已准入对象，不自行选择 owner 或触发全量恢复 |
| Wiki 文件效果 | History 原像、prepared/applied、operation receipt、observer inbox | job item 引用效果证据，模型文字和取消确认不能替代实际效果 |
| 账户秘密与配置 | UserManager、账户 DEK、保存的 agent 配置 | 安装初始默认值只用于新资源；显式 Agent 设置按实时继承影响后续请求；启动、更新和搬迁保留用户设置 |

主库使用一个写连接和明确的短事务。`Database.transaction()` 统一连接锁、驱动与
游标结束、总期限及 COMMIT/ROLLBACK；SQL helper 接收事务，不重入公共事务或
跨调用留下未提交 SQL。接纳、claim、终态结算各自完整提交后才发布成功或外部动作。
取消等待者与取消已接管命令不同；取消不能丢失仍在运行的驱动操作。

主库使用 FULL，Wiki 权威连接使用 EXTRA。正常 COMMIT 返回确认提交；存储或
提交结束失信进入统一 fail-stop，不在同代独立回读猜结果、补写或重放业务。
`RETURNING` 只返回事务内的 SQL 结果，提交前不能当作对外成功证据。
接受一个权威存储故障中断本代全部会话的代价，以减少失信后多处在线恢复。
业务冲突、正常取消、认证失败和通知断线仍按各自语义处理，不泛化为 fatal。

受管启动在旧代真实退休、当前恢复完成后才开放 producer 和写入入口。代际准入
使用冻结旧代范围与退休证明，不能靠 managed 标志、隐藏 unknown 或历史 PGID
扫描放行。standalone 不承诺清理未知历史进程。正常发布自然排空；fatal 的强制
退出不替代发布过程。相关实现见 `service_installation.py`、`service_scope.py`
和 `service_health.py`。

## 输入、完成与资源退休

输入事实、送达证据、业务提交、通知和资源退休必须分开。明确拒绝未接纳的请求
不创建输入行，条件恢复后用户可主动用同一 ID/内容重提；已接纳或 unknown 的
身份仍严格去重，客户端不自动重发 POST。同 ID 不同正文冲突。恢复沿
`recovery_of` 保存唯一直接后继，unknown 不改记成功或自动变回待执行。
Dismiss 只改变提示处置，不证明原工作已停止，也不改变原结果或当前准入资格。

Owner 串行修改活动状态；慢准备、spawn、stdin、reader 和退休在受管 I/O task
中执行，结果携带原 request/attempt/work/backend 身份回报，不能在出队时改绑
到“当前请求”。Owner 不等待另一个 Owner；广播负责通知，不驱动第二套权威终结。

Stop 在等待终态入库前同步关闭原 HarnessRun 的新输入与受控工具准入，撤销
run lease 和 Wiki 绑定。关闭准入不取消原输入证明 reader，也不表示资源已退出；
原 Owner 继续记录结算并沿原边界启动和确认清理，迟到接受/拒绝证明仍只细化
原输入。App Server 的已绑定根 terminal 则立即关闭准入并启动退休，存储或通知
背压不能延长工具授权。材料仍可保留到原资源退休，但材料和历史都不能恢复 grant。

App Server 的根 terminal 是 provider 候选。一个 attempt 只接纳一个根 turn；
活动轮内的多次采样、原生等待、嵌套等待和 child followup 均沿原预算。校验根
terminal 后，Run 同步冻结 target、问题回答和工具准入，启动原有界退休；同一
reader 仍以原 connection/request/attempt/work 和 native 身份采集完整晚事件，
不转入 Claude held/idle，也不恢复 grant。EOF、原采集截止和真实退出分别取证；
raw drain 不证明完整 item 已保存。已观察的根 started/delta 缺少完整 item 时
不能成功，未承诺采集或取消前从未产生的 child 答案则不成为成功依赖。

正常结算等待必要前缀和原资源退出。Stop 与 start 等待失败可以先由原 Owner
唯一选定 interrupted/failed，待有限前缀结束后再一次定稿引用、分配 terminal
seq 和入库；迟到终态不改选业务状态。`native_status` 保留实际根终态，与业务
status 独立。选定后的 Stop 不改写原结论，仍暂停派发和关闭准入。退休失败唤醒
原 Owner 并沿 Manager 的既有失败接管和 health 边界结束等待，保留原资源引用，
不靠无限 pending 或新 writer 代替退出证明。记录/通知失败也不重跑模型。

原生根失败携带的错误正文、provider 错误结构和原 request/attempt/native 来源
沿既有 recovery 诊断消息保存，位于 terminal 之前。即使 Stop 或 start 等待失败
已选定业务状态，迟到 provider 错误仍可在刷新后读取；保存诊断不替换已选状态，
不把错误文本纳入根答案，也不触发重跑。

活动计时以首次撤权为截止，退休宽限沿原有界清理记账；原请求仍占槽直到收束。
迟于总预算或 idle 的 terminal 不能因 Owner 存储等待而消除超时。根/子真实进展
可更新活动，重复 started/completed、ACK、详情和轮询不续命；撤权后的晚事件
不重启活动计时。helper 的实际状态和取消原因按原资源事实保留，不证明父已消费
全部结果；独立 BG、Research 和命名委派仍归各自 owner。

单轮结算由原 Owner 的 `OwnerRuntime.settle(execution)` 使用原 Execution、
settlement 和发布上下文完成。Manager 不再回取 `runtime.current/frames` 解释
旧结果，只保留入口、注册、跨会话配额和故障接管。重复结算返回原选定事件；
不把旧终态重新选择给后来的请求。Database 仍拥有输入和 terminal 的短事务，
原 publisher 仍拥有有序交付；慢通知和资源退出不阻塞命令处理。

terminal 确认提交后先保留原 outcome、seq 与 `committed`，再刷新投影并交给
publisher。后两者失败不会清掉提交事实、补选另一结果或重跑模型；Owner 失败时
Manager 接管原对象的完成与资源责任。`done` 表示已结算，可以是 `done + failed`；
重启未结算 claim 及未确认 steer 仍为 unknown，主成功不能替它们确认送达。

| 接口事实 | 保证与限制 |
| --- | --- |
| `SubmissionReceipt.disposition` | 表达是否接纳；取消等待者不撤销已接管工作，重复 delivery 返回原 receipt |
| `RequestCompletion.input_request_id` / `request_id` | 区分原输入与携带请求；未确认发送的 steer 不能套用主请求成功 |
| `recording_state` / 业务 outcome | terminal 事务确认后保留 committed；缺少确认时返回 unavailable，不伪造成功或失败 terminal |
| `notification_state` | delivered、failed、unavailable 独立于业务结果；通知失败不抹掉已提交结果 |
| 原 execution/preparation/reader/retirement 引用 | CloseWork、归档与撤权等待真实资源；外部关闭另行排空 publisher，不能反向等待正在调用自己的 subscriber |

<a id="codex-native-input-evidence"></a>
### Codex 首输入证据与准备边界

opt-in App Server 的首输入和人工 steer 继续使用原 `client_submissions`，不另建投递或结果账本。
Owner 在标记请求 started、启动异步 claim/附件/凭据/配置准备之前，以短事务记录
初始 `not_sent` 并保留首 attempt 身份；真正执行沿用该身份。pending 行只允许初始
未发送证据，不能写发送事实；已证明未接纳的 steer 回队后，原输入可记录后续
普通执行的新 attempt。准备失败与早期 Stop 由此保留恢复
入口；driver 的直接 Harness 调用仍自行初始化同类事实。

| 原生证据 | 含义与后续行为 |
| --- | --- |
| `not_sent` | 未记录业务帧可能发送；spawn、握手、只读查询不算首输入执行。controller 修改的不确定性独立保留。 |
| `unknown` | 原 submission 已先提交可能发送边界及 RPC 身份，随后才能写业务帧。写前存储失败不发送，写后 ACK 丢失不自动重放。 |
| `accepted` | 原 start/steer 响应或匹配的完整 user-message client ID 证明接纳并关联原根 turn；不证明模型消费。 |
| `rejected` | 锁版协议明确未接纳。当前 Owner 的未接纳 steer 可按原身份和顺序回普通队列；首输入或迟到拒绝按原恢复合同处理。通用内部错误不证明拒绝。 |

产品 input、attempt、RPC、native thread 与 turn 身份分别解释；细化不能覆盖已知
身份或倒退证据。thread 与输入证据一起提交，后续 writer 在原资源退休后读取
持久事实，不依赖一次实时事件被消费。driver 报告协议事实，原 Owner 唯一结算。
steer 另保留 `kind`、连接 generation 与预期 target turn；接受证明的 turn 必须与
该 target 相同。连接代次与原生身份共同隔离编号复用，原输入不能迁往下一轮。
写前 marker 与最终 target/lease 校验分别拥有持久不确定性和实际写入资格；原事务
仅在没有 marker 或证据均为 `not_sent/rejected` 时允许当前 steer 回队。

terminal 事务与查询保留未澄清的 unknown；成功根 terminal 不能替输入确认。
Stop 后仅允许细化原来已可能发送的输入的接受/拒绝证明，不能登记新 attempt、
重新发送或改写已提交的 interrupted outcome。已观察的接受/拒绝记录复用现有
取消保护和数据库总时限，不能因 reader 退休取消而丢失，亦不阻断进程取消启动。
除当前 Owner 尚未交付的已证明未接纳 steer 按原序回队外，明确未发送/拒绝者
进入既有恢复列表，用户检查原文和附件后，以带 `recovery_of` 的新身份明确提交。重启按已有
证据区分未发送与未知；旧行的空证据不回填为原生接纳或模型消费。重复浏览器
身份仍返回原回执，界面分别说明原生确认未知与准备变更未知。
原生证据提交只唤醒原 Owner 的合并通知信号；Owner 推进现有 dispatch revision，
使活动中的 Saved inputs 也可刷新为 accepted。接受但没有消费证据的 steer 在根
成功后仍保留限制说明，没有自动恢复执行按钮；回队后已作为普通请求执行的
输入不因旧 steer 证据留下错误通知。资格、顺序和容量见[steer 合同](inline-steering.md)。

### 共享输入与执行观察

执行输入只在 `SessionManager.initialize()` 的输入恢复、会话载入与 fork 恢复全部
成功后开放。数据库引用非空不证明就绪；未初始化、初始化中或恢复失败明确返回
`service_not_ready`，不保存、排队或启动输入。所有来源与 steer 共用持久接纳和
claim，没有无数据库执行分支；待执行队列只持身份与展示/调度投影，正文和附件
从原持久输入读取。结果记录、通知及资源退休仍分别确认，构造、监听、内存查询
和空资源关闭不依赖执行就绪。详见[输入接纳与恢复](../input-reliability.md)。

每个原 receipt 只有一个 completion Future。Delegation 在读取正文前固定原轮次
和嵌套分类；广播只上送问题，不独立结算 result/error 或保存第二份回复正文。
构建状态、App ask/SSE、Research、Wiki 和 scheduler 消费原请求完成事实。
App 的成功 `done` 还要求原正文通知前缀已送达；响应结束不直接释放执行槽位。

### 原执行的观察

原接纳回执的 `execution` 持有 `RequestObservation`：原 input request/work、
驱动完成 Future，以及按尝试捕获的 request/attempt/work/run lease 和 backend。
原 OwnerRuntime 在原 Execution 的完成或故障接管中绑定这些事实；Manager 不回读
当前执行解释旧回执。重建回执没有进程内资源证明时保持 `execution=None`，不因
会话 idle、删除或对象缺失推导成功。

`driver_state` / `wait_driver()` 只观察原输入的驱动收束。Stop 将未启动输入保留
队列时，该输入仍 pending；未知 steer 即使先返回 unavailable，其观察仍跟随原
承载 driver。`attempts` 返回有原身份的资源引用；`wait_resources()` 先等原 driver
不再添加 attempt，再等这些 backend 的实际退出。资源仍存活、退出失败和缺少证明
分别保持 pending、failed、unavailable；等待失败也可传播原异常。

请求驱动结束允许 Claude 宿主供 B 复用，不能据此把 A 捕获的宿主报为已退出。
观察 A 或取消其等待不调用 stop，也不停止 B；资源引用由原消费者持有至观察责任
结束。普通 App 槽只等待原 driver，保留复用政策。安装交接另按
[领域事实与封口收据](#安装自然排空与交接收据)执行，不替代指定资源证明。

Wiki 结束或重试原 work 时，先关闭准入/授权，再加入原 BG、Research、会话和
backend 清理。`require_retired` 使用会话公开的 work/旧会话范围查询与自己的持久
退休确认，不再读取会话私有队列或退休集合。缺少原证明，即使会话仍存在且空闲，
也拒绝重试；旧记录缺少 work 身份时保留原会话范围的保守检查。失败 Owner 可
保留诊断引用，但只有其原 `resource_cleanup` 明确成功后才不再算活动资源；
缺失、pending、失败或取消均不能被当作成功。归档不删除这些诊断来制造退休证明。

### 原请求结果读取

`Database.read_session_result` 在同一已提交快照解释 input/carrier、terminal、
attempt、有序正文、成本和可识别缺失。确认的 inline 输入沿真实承载关系读取；
unknown 输入不借用主请求的成功。旧 terminal 无 attempt 时只回退同 request
已关联文本，不按时间、文字含义或“最佳答案”猜关联，也不建答案引用表。

新 App Server terminal 的 `final_answer` 仅保存绑定根 thread/turn 和有序原消息
seq。Owner 只在完整根 `final_answer` item 已提交后纳入引用；commentary、child、
工具文本和其他根均不属于答案，不追加总结轮。`SessionResult` 与 `LiveReply`
共同校验原 request/attempt、根身份、阶段、消息类型、顺序和 terminal 之前的
范围。引用为空表示合法空答；坏引用返回 `answer_reference_invalid`，不借用其他
行。完整空文本 item 仍保留原记录和引用。既有无引用记录沿原关联读取，不能
把旧空值升级为新格式的完整性证明。

| 消费者 | 保留的正文及完成语义 |
| --- | --- |
| 委派完成返回 | 新原生答案使用共同正文格式；旧 terminal attempt 正文仍连续拼接后 strip。成功空正文用既有占位；坏引用走 reply_unavailable，失败走 agent-error。 |
| App 历史状态与刷新 | 新原生答案使用共同正文格式，历史消息只投影被选中的原生正文行；旧正文仍双换行拼接、不整体 strip。失败可带已保存正文；无 terminal 区分 running/queued/unknown。 |
| App 实时 ask/SSE | `LiveReply` 依原 request/attempt 接收，新 attempt/retry 清旧正文；原生进展使用 progress，根 final item 使用 message。terminal 固定共同选择，成功另需原通知前缀送达。 |

实时消费保留事件流，不强制改为事后 DB 查询；重建 receipt 不虚构 delivered。
新原生正文的共同格式为忽略空片段、按双换行连接后整体 strip；完整记录仍保留
原始空白。旧格式的合法空答和正文行后来丢失不能普遍区分；可识别的 terminal、
会话或输入关联缺失仍显式保留。Claude idle 迟到文本可以保存展示，不提升为
完整尾部交付保证。

### 问题结算

HTTP、WS 和父代理代答将输入及可信来源送到原 Question Owner。Owner 找原
pending/attempt，先纯校验原表单，再不让出地检查 current/到期并 claim，保存后
发布和唤醒。读视图与 Owner 共用 `PendingQuestion.is_current`，即时核对原 attempt、
宿主准入与原生问题资格；状态和过期过滤仍归读方，到期回答与关闭仍归 Owner。
每题有且仅有合法、不重复的单/多选，或非空自定义文字；缺答、非法
选项、单选多选、有效选择与非空文字并存均整份拒绝，不消费问题且不使 Owner 失败。
未用字段的空列表/空文字合法；trim 只判断文字是否为空，保留原正文，委派入口
延续已有的文字 strip 转换。父代理入口只做题目 ID/位置转换，不再持另一份校验规则。

原消息 `tool_input.question_resolution` 保存 version 1、question/request/attempt/
work、处置、来源及答案。答案沿原 `question_index`、标签与可辨明的 `option_index`
关联表单，保留多选顺序与自定义文字，不生成 provider ID。处置区分 answered、
expired、closed；显式关闭保留 `question_closed` 和 reason，系统到期指导不伪装
成表单答案。旧结构和来源缺失保持未知，content 仍是旧 MCP/展示使用的文本投影。

来源沿认证 principal 表达 account 或 agent；父代理路径另带真实委派/父会话关系。
账户认证不证明自然人亲答，HTTP URL 与客户端自报 source 也不证明来源。内部缺少
可信调用来源时保持 unknown，系统到期和内部关闭记录 system。历史和实时消息携带
同一结构，正常 Question UI 不加步骤；这是对原先宽松 HTTP/WS 非法输入的明确收紧。

回答、到期和关闭只处置一次，晚答不能消费新 attempt。claim 后保存失败走原 Owner/
存储故障边界，不发布或返回未保存的成功答案，不用撤回 claim 处理普通校验错误。
这些事实沿现有消息 JSON 保存，不新增活回调、持久问题队列或存储根。

<a id="native-question-control"></a>
### 原生问题与控制写入

opt-in App Server 的原生用户问题沿同一 PendingQuestion/OwnerQuestions
保存表单、结构化答案、可信来源和关闭事实，不再从展示文本反推原生答案。
不可变 binding 同时保留原连接 generation、server request、thread/turn/item、
request/attempt、产品 question 与原生 question IDs。客户端与服务端 RPC 编号
分属两个方向；相同编号或新连接复用编号不能取得旧问题的回答资格。

原生 reader 的有界问题交接独立于展示队列，Owner 在发布拥塞期间仍可登记、
关闭问题并接管控制。根 terminal、Stop、resolved 或断连同步撤销回答资格；
原 MCP 问题也随原生宿主准入关闭而失效。状态和通知仍由原 Owner 落盘、发布，
后到登记的已接纳问题保留请求及关闭前缀，不因入口未显示而遗失。

答案先沿原消息保存，再交给原 Harness 的串行完整帧写出口。取得原写锁后，
最后一个 await 之后复核连接、原问题未 resolved、目标和准入；复核到写帧
之间不让出。存储期间或等待写锁期间失效的答案不会写出，也不跨连接重放。
本地已答记录不证明原生接收或模型消费；不增设 ACK 账本。通知是否已显示或
成功送达不参与实际发送资格判断。产品 deadline 与原生 isBlocking 分别解释。

可提前退休的关联 Stop 先按原 Owner 的首个在途命令信封核对 command ID
及内容，再沿同一控制判定执行；最终被拒绝为 command_conflict 的命令不能
先对宿主产生副作用。Stop 后首输入的迟到拒绝、EOF 等只补原事实，保留
已选 interrupted outcome 与有限采集 reader。未进入 steer writer 的取消可
证明没有 I/O；已进入 writer 则继续依原持久发送证据判断，不能猜测回队。

执行准入关闭与采集归属分开：原 request/attempt 身份保留到有限 reader/proof
清理和资源确认退出。必要完整输出无法容纳或保存时，原请求失败并退休，
已接纳前缀仍先交付；饱和队列之外的失败标记不能被根成功候选覆盖。
已选业务结论不因此重选；记录或退休失信仍沿既有 Owner/Manager 失败接管。
卡片、输出身份和退休投影的消费者约束归[原生子任务合同](native-subagents.md)。

审计原执行结果时使用非空 `terminal_request_id` 及其唯一性，不按消息时间或
`request.content_id` 计数；复制的历史可以保留这些值而不代表新执行。原生父轮的
`turn.completed` 不证明 helper 已完成或父轮已消费其报告；有限已写记录观察不等于
等待活动 helper，归一化 wait 展示也不代替原始超时结果。当前支持范围与下方暂停
项目分开判断。

归档和 rewind 共用原 reservation、退休期限及失败释放。Owner 退出由 Manager
接管原对象与预留；关系冲突不免除已捕获 work 的清理。资源确认后仍需 seal/join
原 publisher；不能确认退休时保留原引用并进入真实故障出口。原 child 已成功而
正文随后丢失时，沿稳定返回身份告知父 work 交付失败，不改写 child 成功事实。

mailbox、控制入口及顺序 publisher 按实际内容计预算，背压到达 producer。
合法大命令可独占空控制通道；未接纳的容量等待者取消后不执行，Owner 失败释放
等待者。超时只结束等待，不丢弃原命令、句柄或数据库责任。

BG 原命令结果、委派原 work 的已提交结论和 Research 报告，与返回接纳、父模型
处理、通知及资源退休分别取证。返回首先核对原 source/delivery/session 的
receipt；已有 receipt 不依赖来源正文仍存在，也不重新唤醒归档会话。

| 原接纳边界的证据 | 来源处置 |
| --- | --- |
| 已有原 receipt | 复用原接纳事实，沿原 completion 观察父处理，不再提交 |
| 明确效果前 busy/mailbox full/quota，且没有 unknown 前史 | 只以原 identity 重试；问题在每次准备/提交前仍须有效 |
| 永久完整性拒绝 | 记停止自动返回，保留来源业务结果和独立原因 |
| ACK 丢失、Owner/通道不可用或超时，可能已经接纳 | 公共入口核实一次原 receipt；仍不能确认时，来源先在原控制对象记 unknown，再完成必要诊断并结束本次观察 |
| unknown 后目标关闭、问题过期或 receipt 仍不可读/不存在 | 保留 unknown；后来的缺失和拒绝不证明以前未接纳 |
| 确定的 identity 正文冲突、未分类错误、取消或存储失信 | 保留并传播原错误/取消；可能有接纳效果时同时保留 unknown；存储失信仍按统一 fail-stop，不猜测补查/补写 |

unknown 只允许原有结算/恢复入口重入时再核对同一 receipt，不新增轮询、重试账本
或重放触发器。用户可以持续看到“返回未确认”；接受这个终点是为了避免常驻
观察责任。只有原 receipt 的证明才能归并接纳事实；会话 idle 不构成证明。
诊断在任何后续 await 前记住，写入失败后的重入只完成诊断或原回执观察，不能
重新进入接纳。原进程、在途 callback 和必要写入各自收束后才释放对应引用。

委派的既有命令注册同时持有其原诊断提交条件。写入失败不能仅凭异常属于
`DelegationError` 就退出责任；已有原 relay/结算重入在注册新命令后接手同一写入，
旧失败命令才可退休。新命令仍阻塞排空直到写入确认或原关系已被实际关闭。
follow-up 的暂存 CAS 仍可回滚；本命令未确认回滚才可在退出时关闭前代 writer。
明确拒绝并恢复前代后，即使另一命令暂存新 round，前代未完成的诊断责任仍阻塞排空。
数据库会话删除入口在提交确认后同步通知 round owner。round CAS 依赖原父子关系，
child 问题说明只依赖原 child：删除 parent 只闭合 CAS writer，不免除仍可写 child
的新旧问题说明。删除 child 才闭合该目标的说明 writer；确认新轮接管则闭合前代。
删除拒绝、归档或 Owner 不可用不构成删除证明，在途执行另行退休。
不新增执行任务、观察触发器或投递账本；完整异常链由现有日志保留并掩码。

BG manager 在原任务行保存 disposition/error；委派 round writer 保存独立
terminal delivery error，不能覆盖业务 error。问题交付观察只活在原 work/question，
说明写原 child 的 delivery notice；父返回失败不关闭仍有效的问题，也不表示
整个 round 已回复。REST、MCP、事件及卡片分别展示业务状态与返回原因，诊断按
实际凭据字段掩码。重启不重放旧 BG/委派终态，也不恢复旧问题回调；新 follow-up
是新的授权轮次。存储表示和有限转换见[当前存储格式](current-storage-format.md)。
BG 详情响应补齐输出时，保留请求开始后实际收到的交付事件（包括清除诊断）；
列表插入或其他实体替换本身不表示新的交付观察，也不能阻止较新详情补齐诊断。

| 来源 | 业务材料权威 | 交付观察及恢复边界 |
| --- | --- | --- |
| BG | 原任务行的状态、退出原因及 stdout/stderr | disposition/error 独立保存；原 receipt 才证明接纳；重启不重放历史终态。 |
| 委派终态 | 原 work 的 completion、最终 attempt 与 child transcript | 原 round CAS 保存 disposition/error；新 follow-up 是新 work，旧终态不重放。 |
| 委派问题 | 原 child 的有效问题与回答队列 | 原 work/question 控制对象保责，说明持久写原 child；失败后仍可人工回答，不建立重启问题队列。 |
| Research | 原 job 状态与完成报告 | 沿用 injection_status、delivery_receipt 和原错误；恢复观察原回执，不重跑已完成研究。 |

这些来源保留不同的异常适配和写入责任。自动输入仍按成功接纳次序逐条派发；
Stop 保留暂停积压，人工纠偏不自动恢复积压，显式恢复才继续原顺序。
没有自动合批、转 steer、notification-only 或默认等待全部独立工作的政策变化。

当前使用 Codex exec 与 Claude stream-JSON：Codex 新输入在运行中排队；Claude
按实际活动通道能力处理 steer，已到达的 idle 消息保留原身份。普通委托完成或
重启不自动归档；人工追问和 parent follow-up 是新 request/round，显式归档与
schedule 策略仍有效。可靠跨请求返回走现有独立工作机制；完整原生尾部及更强
的送达、授权切换保证仍属下方暂停范围，不能从 stdin 能力或首个 result 推断。
普通父会话不因等待独立工作自行归档；归档中断当前输入并撤销队列，合法新返回
可唤醒原会话，但保留已有 Stop 暂停且不重放取消输入。具体返回边界见
[Research 完成与投递](native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler)。

退休责任覆盖启动时捕获的整个进程组，不能用直接 child 退出或管道 EOF 证明
所有 writer 停止；后代可重定向输出继续写入。保留未确认退出的 backend owner，
即使请求、assignment 或 executing 注册已经结束，也不能提前释放配额或允许 Wiki
重试。AppMutation.abort 同样依赖应用后端真实停止；正常退出与忽略 SIGTERM 的
后代分别验证，不能用自动丢弃 done task 的集合代替退休 owner。

BG 在 shell 和输出结束后固定原业务结果，再主动对原进程组执行 TERM、宽限、
KILL 和活跃成员退出确认；普通完成广播及父会话返回都在退休确认后发生。
正常收尾不设置取消原因，之后的取消与关停复用原清理而不改写成功结果。
记录、退休或投递失败均保留原 owner 和结果；退休先于结果持久化，存储失败
不能遗失 writer。后续取消或关停可重试未完成阶段；通知重试沿用原任务身份，
不重跑命令，也不重复已确认的记录或完成广播。关停无法排空时明确失败，不能
提前关闭依赖。覆盖范围限于启动时捕获的进程组，不追踪主动脱离该组的任意后代。

应用彻底删除由应用生命周期记录未完成阶段，关闭相关写入，再让原资源 owner
确认开发执行、BG、确切委派轮次、应用对话及后端进程组/reader 退休。会话归档、
删除或替换不得先抹掉仍承担清理责任的关联；缺失历史不补造。失败保留资料、应用行
及原身份，重启保持关闭并等待显式重试。成功后保留开发聊天原有行为，不设永久
只读；详细阶段与文件边界归 [applications.md](applications.md#5-rest-surface)。

当前 Research 投递已区分接纳错误：`AdmissionIntegrityError` 是效果发生前的永久
完整性拒绝；`SessionAdmissionBusy`、`SessionMailboxFull` 与配额拒绝表示可重试的
明确未接纳。丢失 ACK 后先核实原 source receipt；暂不可读或无可核实回执时保留
`unknown`，不把后来的拒绝当作早先未接纳的证据。永久拒绝与接纳未知都保留完成
报告；程序错误和 `SubmissionConflict` 保留原异常、诊断及原任务，不进入普通
重试循环。存储失信仍沿 fail-stop。恢复只观察原回执，详见
[Research 合同](native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler)。
这些局部错误分类不表示三来源统一返回接管设计已经实施。

### 公共返回接管

[`SessionManager.admit_return`](../../server/sessions/turns.py) 拥有 BG、Research、
委派终态和问题的一次完整返回接管。来源提供原身份和按需正文准备函数，不再自行
组合回执查询、会话唤醒和输入提交。采用回调是为了让材料读取仍归来源，同时把
调用顺序交给会话模块；拆成两个公开阶段会把顺序责任留给调用方，正文引用联合
类型则会让会话模块认识各来源的存储方式。

#### 身份、材料与责任

[`ReturnTarget`、`ReturnBody` 和结果类型](../../server/sessions/submission.py)
只在本次调用内使用，不新增持久事实。`ReturnTarget` 由可信来源按原记录构造，
不能直接承接外部请求字段；目标 session、work 和捕获的 owned binding 必须相符。
缺失 binding 仍可确认已有 receipt，但不能创建新输入。delivery 身份保持
`bg:{task_id}`、`research:{job_id}`、`agent-terminal:{work_id}` 和
`agent-question:{work_id}:{question_id}`。

`prepare` 每次调用至多一次；已有回执或确定不能新接管时可以不调用。它只读取、
构造 `ReturnBody`，或用 `ReturnSkipped(reason)` 表达来源不再需要新投递。空正文
不表示跳过。问题关联的 child session/question 字段必须成对提供，Owner 在实际
执行前再检查问题有效性。准备不得结算来源、启动执行、发送通知或修改授权；
并发调用和容量重试可以重复纯准备，不为准备绝对一次增加锁表或账本。

| 责任 | 所有者与提交事实 |
| --- | --- |
| 原回执、资格、准备顺序、合法唤醒及交接 | 会话模块的公共返回操作 |
| 正文、是否仍需返回、重试节奏与放弃、业务结果 | BG / Research / 委派各来源 |
| 归档成员关系与暂停恢复 | 原 lifecycle；合法恢复保留 Stop，不重放已取消输入 |
| 输入接管、digest 冲突与去重 | 原 SessionOwner 和输入事务；完整输入与接纳事实提交后确认 |
| 父处理、结果、通知与资源退休 | 原 `SubmissionReceipt` 的 completion 及各自原 owner；不新增完成 Future |

#### 顺序与失败边界

先查原 session/source/delivery 的 receipt。已确认接管优先于材料删除、Owner
失败、来源或当前准入关闭；快路径不准备、不唤醒、不重开输入。已经撤销的输入
也只观察原回执，不能重新入队。

没有回执才检查原归属与当前资格。准备前的检查不授予跨 `await` 的许可；准备
期间不持 mailbox、生命周期锁或事务，也不预留执行资源。准备完成后先再查原
receipt，覆盖另一调用已经接纳的情况，再处理来源跳过或重验资格、恢复原会话。
恢复可能等待原生命周期操作；等待结束后再次校验资格，最后由原 Owner 和输入
事务裁定。归档期间的合法返回等待归档结束，不能提前把瞬态 closed 当成永久拒绝。
session 的 reservation、fork 和 closed 由原生命周期锁内及实际 Owner 判断。

唤醒与输入提交仍是两个提交点；本次没有新输入不证明此前没有合法唤醒，接纳
拒绝不自动回滚归档成员关系。并发唯一性与实际提交的 digest 冲突仍由原事务及
Owner 保证，不新增每 delivery 的锁、队列或注册表。普通重复观察无需生成正文；
实际提交的 `SubmissionConflict` 原样传播，不能补查回执后把冲突当成 duplicate。

| 返回或异常 | 调用者合同 |
| --- | --- |
| `SubmissionReceipt` | 已确认接纳的原输入；不证明执行成功，继续观察原 completion |
| `ReturnRefused(reason)` | 本次没有新接纳；原因限于 `source_withdrawn`、`binding_unavailable`、`destination_unavailable`、`admission_closed`、`capacity`、`busy` |
| `ReturnUnconfirmed(cause)` | 原回执暂时无法可靠观察，或提交交接后未获确认；保留原身份与原因，不推断没有接纳 |
| 准备错误、实际正文冲突、未分类错误或取消 | 保留并传播原错误；取消等待不撤销已经交给 Owner 的 Submit |
| 存储失信 / `ServiceUnavailable` | 沿原 fail-stop，不在同代补查猜测，也不转为普通 unknown 重试 |

分类取决于阶段：回执读取的临时容量或期限失败是无法确认，提交处已知的
`SessionMailboxFull` / quota 拒绝才是容量拒绝。归档/fork busy 与生命周期关闭
使用其明确业务分类，不匹配错误文案、不把任意 `ValueError` 当拒绝。拒绝、来源
跳过及可确认的交接异常出口，在存储可信时核实原 receipt；当前没查到不证明另一个
在途提交不会成功，也不构成新接管许可。其他未分类异常不被转换为成功或普通拒绝。

丢确认后公共入口核实一次原回执。来源收到未确认时先记住原诊断，再完成自己的
必要写入并结束本次观察，不在外部重复核实。仅既有结算/恢复重入可观察原身份；
unknown 的准备函数只返回跳过，后来的拒绝不能覆盖原原因。只有无 unknown 前史
的明确容量或 busy 拒绝可按来源策略重试。停止观察保留原材料与业务结果，不更换
delivery、不重新执行来源；接管未确认也不等于 `RequestCompletion` 的执行结果未知。

#### 来源接入与验证

BG 的 `deliver_bg_result` 转换原身份、材料及辅助说明，manager 保有命令结果、
诊断与业务重试。Research 正常注入和启动 unknown 观察都使用公共操作，报告、
queued/processing/processed、浏览器收讫及父处理仍分别由其原 owner 维护。
委派 `_inject_terminal` / `_inject_question` 保留原轮次和问题的重入保护、round
CAS / child notice 诊断写入及原 completion 观察，只消费公共接管的三类结果。
其精确正文、问题关闭及材料删除语义见[委派合同](agent-collaboration.md#53-reply-delivery-the-original-request-completion)。

来源不再组合 `source_receipt`、恢复、`start_message` 或读取准入私有字段来解释
一次接管；生命周期恢复只保留会话模块内部 `_restore_task_return`。Wiki/schedule
仍可使用 `source_receipt` 和通用 `start_message`；Wiki outbox、schedule 首次
触发、App、人工输入保持各自入口。本操作不引入通用任务框架、执行表、持久队列、
恢复机制或 provider 迁移，现有输入、导出及恢复表示不变。独立返回按成功接纳
顺序逐条派发；来源完成时间不决定队列位置。Stop 保留暂停 backlog，人工纠偏
不隐式 Resume，显式 Resume 后继续，来源 work 不并入当前请求的原生尾部。

回归通过真实隔离 SQLite/Owner 和可控本地后端观察原输入、receipt、启动与来源
记录，不替换最终接纳事务：`test_return_admission.py` 覆盖原回执、并发纯准备、
跳过、错误分类、冲突、取消与失信；`test_bg_session_return.py` 和
`test_research_session_return.py` 保留各自实际返回、资格与暂停覆盖。
`test_return_admission_lifecycle.py` 验证真实归档等待及单次委派接纳；
`test_delegation_return_admission.py` 验证准备前不唤醒、准备/恢复期间 work 或问题
关闭，以及 unknown 后材料丢失、Owner 失败仍得到原输入。
`test_research_return_admission.py` 覆盖同样的报告材料丢失场景；委派重试、诊断
失败重入和删除材料的边界由 `test_delegation_return_retry.py`、
`test_delegation_text_deletion.py` 保持。

## 辅助任务的读取与事件合并

BG 与委派的 HTTP 响应模型定义公开字段，BG 记录直接按模型过滤，内部知识上下文
不进入响应。前端从生成契约派生任务类型，保留 WS/历史材料允许缺失的字段；完整
HTTP 快照与稀疏事件不互相冒充，也不各自维护一份字段和状态枚举。

`hydrateSession.observeSession` 拥有完整读取的账户、目标、服务/连接身份、领域
基线、结果与有效期。选择、归档查看、重连、归档替换及 fork 跳转复用这项操作，
页面不再传入 store baseline 或拼 detail 与辅助读取。detail 接受后，导航即可结束；
并行请求的 BG、Research、委派结果须等 detail 确认本次可信服务身份后才能发布。
任一辅助失败只影响所属读取，不把成功 detail 改成队列不可用。辅助独立重试
只读对应接口，取消观察不取消服务器业务。

仅补全列表信息使用 metadata 用途和 `GET /api/sessions/{id}/info`，不下载或应用
transcript；App 列表与委派事件卡片各自持有读取句柄，在其观察结束时取消。
同账户的合法旧会话缓存可以继续更新，迟到读取不改变当前选择或侧栏位置。
`SessionList` 复用原账户/连接/服务身份，并为在途目标保留观察：删除、归档或
已知成员退出时，尚未进入列表的目标也能失效。读取结束即释放，不积累 tombstone
或第二份会话集合。成功、失败和辅助结果共同受此有效期约束。

读取顺序裁定错误归属，领域状态裁定成功快照能否采用。较新成功不被旧失败改成
错误；较旧成功仍可贡献合法完成状态，但不能覆盖较新的同状态阶段或交付诊断。
BG/Research 每个 id 对应一次执行，终态不因另一份 running/pending 快照回退；新
任务使用新 id。两个请求共享 running baseline 时，无论返回顺序，另一份快照的
对象变化都不能遮住合法 completed。

transcript 沿持久 seq、历史窗口与 WS 去重合并；旧详情保留期间的新消息与新问题。
关闭问题不能复活；被较新成功详情替代的旧读取不再重建问题或 active attempt。
原生任务以 `tool_call_id` 归属原任务，委派以 `delegation_id + work_id` 归属原轮次。
同一子会话的下一 work 可以运行，不能按子会话设置全局终态最大值。

Zustand 仍持有唯一任务状态。不可变记录上的弱引用标记只证明 WS/本地权威更新，
快照写入沿用已有标记而不推进它；在途快照保留期间的真实更新与新增任务。BG 的
交付诊断继续独立保护，Research 的浏览器收讫、汇报输入接纳与父处理继续分别
表达。`SnapshotReads` 只为未完成的请求保留排序 token，不保存任务状态或服务版本。

委派专用读取供完整加载和请求卡片轮询共用；卡片仍决定轮询与观察结束，无须加载
完整聊天。合法列表缺少原轮次时停止轮询并显示不可用，不借下一 work 的状态；
失效观察不是权威缺失，连接/服务变化后重新观察。网络失败沿原有有界重试。
真实 hook、卡片、公开加载与辅助重试回归覆盖双成功乱序、真实 WS、新 work、
旧错误、身份切换、未知目标删除、局部失败及取消，不以纯 merge helper 代替入口。

## 安装自然排空与交接收据

各原 owner 的 `drain_blockers()` 即时解释自己的接纳、执行、交付与清理责任。
安装层显式组合 HTTP/WS、会话、BG、Research、委派、Wiki worker/Registry/通知、
AppAgent/backend/bridge、登录、scheduler 与主库事实，不读取其私有队列、锁或
第三方 executor 字段；不另建活动账本、持久 idle 状态或通用关闭事务。
观察只说明本次阻塞，不能跨 await 当作交接许可，也不启动、取消或重绑业务。

会话包括 Owner 命令、准备与迟到 spawn、输入队列、问题、发布、退休和失败接管，
以及短锁外仍被原 owner 持有的配置/lifecycle 操作。BG/Research/委派继续保有原
启动、终态回流和保存责任，已结束但清理未确认的任务仍阻塞。失败 Owner 只有原
清理成功、原通知退出且没有保留输入时才能解除阻塞。空闲 held 模型宿主及 App
server 可留到正常 shutdown 退休，不能仅因进程存活永久等待。

Registry 在排出同步线程前登记原操作，覆盖尚未借用 Store 的空隙；取消等待不
释放仍在运行的线程责任。最终交接以非阻塞短 guard 保护 Registry 与 Store 借用
准入，无法取得或仍有活操作就拒绝，不在事件循环等线程退出。普通调度/借用封口
后，原 shutdown 仍拥有最后同步和资源关闭；同步结束后先观察停止意图，再决定
是否进入下一普通 reconcile 阶段，不能因新准入已关而跳过最终清理。

Wiki 通知从 outbox 读取、主库 receive、会话返回到原 ACK 全程持责。轮询 sleep、
临时失败或业务已终态不能消除未 ACK 责任；事实直接查询原通知账本，不缓存第二
份待投递状态。查询采用统一只读存储入口、零锁等待：正常 BUSY/LOCKED 拒绝交接，
IOERR/损坏等失信存储仍进入统一 fail-stop。无待交付责任的常驻接收循环不阻塞。
App bridge 的准备、文件效果、保留的失败清理及未确认 journal 同样阻塞；durable
证据存在不等于清理完成。AppAgent 响应/原执行与 backend 启停由各原 owner 报告。

HTTP 责任直到原 ASGI app 完成，发出 headers 不是完成点；被动 MCP 接收沿既有
规则处理。WS 只计正在处理输入的区间，空闲连接不阻塞。scheduler 自己观察已排出
回调及注册操作；DB 自己观察 writer 和已有独占操作，shutdown 收尾仍可使用原库。

管理员 `POST /api/installation/quiesce` 在同一事件循环同步段完成最终事实检查、
必要 producer 封口和成功收据发布，跨线程准入由上述 guard 保证。封口前 busy
返回 409，producer 保持开放，等待自然完成；不为发布取消真实工作。第 N 个 gate
失败时没有成功 marker，已经关闭的 owner 保持真实状态并负责可重复收尾，服务
不伪装普通 busy 重开。全部封口成功才设置原 `offline_request` 并返回明确的
`status=stopping` 和原 `request_id`。

同 ID 重试返回同一成功事实；不同 ID 不得冒领。`/ready` 只在成功后带原 marker；
导出的 `managed_source` 同时检查 HTTP 状态、明确 stopping 和原 ID，缺 status、
部分失败或错误 ID 都不是成功。丢响应可按原 ID 找回收据；无法确认就保持未知，
不据此停服务。收据证明安全交接资格，进程及 writer 的实际退出仍由随后正常
shutdown 与外部服务 owner 证明，不把逐域封口称为全安装原子事务。

隔离安装/HTTP/ready/导出、真实同步线程、持久通知、桥接文件及子进程故障注入
回归验证拒绝与放行；实际生产服务不作为故障注入对象。未来新增内部资源在原
owner 纳入事实与退休，新增业务域才扩展安装层的显式组合。

## 公开接口与账户配置

- 全局新建会话或 schedule 显式指定所属 `agent_id`；agent-scoped URL 提供该
  身份，MCP schedule 从已验证会话确定 owner。`session_id` 的执行目标用途保留。
  CLI handoff/import 仍使用其现行账户归属逻辑，不因没有网页入口而删除。
- 外部 WS 发送、控制使用当前请求/命令及目标身份；缺身份明确拒绝，不补身份执行。
  UI 只消费完整 `dispatch_state`，保留 generation/revision 防乱序；活动状态
  缺失或畸形不伪装为空队列，归档详情不创建 live Owner。
- 旧工具批准链已退出。当前问答、完整回答和取消正常工作；历史批准正文可只读
  展示，旧动作按不支持拒绝，不重新启用另一套审批或改写工具设置。
- Wiki 新请求使用 `auto`、`read`、`maintain`。HTTP 使用 `/read-many`、
  `/maintain` 等当前路由；MCP 工具 `read_many` 名称保持，不能按字符串一起删除。
- 正式会话 MCP 使用 HTTP scope；无 session 的 stdio 装配保留参数检查与诊断
  用途。它不是 HTTP 失败后的自动接管，也不豁免当前账户和工具授权。
- 空安装以 setup 证明创建首管理员、账户 DEK 和有 owner 的默认 agent，整个
  账户聚合同事务提交。响应丢失后走正常登录，不重新开户或生成密钥。
  未开户/缺证明不开放无账户业务；非空安装时代数据拒绝启动并保留原件。
- 旧安装接管、memory handoff、token rotation 及专属在线恢复已退役；账户登录、
  密码修改、bearer 撤销、master key、当前 connector 锁和 refresh 仍保留。
- 已有 agent 的名称、persona、模型、强度、速度和工具选择由用户控制。安装初始
  默认值只用于创建，不在启动、版本更新或搬迁时回填。显式保存的合法 Agent 默认
  配置可以使旧会话的继承暂不兼容；保存不扫描并否决所有旧会话，也不转换其 backend
  或历史。旧会话通过当前设置入口修正完整有效组合，实际使用前校验控制项与当前
  凭据授权。捕获、重试和生效时序归[模型设置](model-reasoning.md)。

配置与授权在所属模块统一解析。后台查询中的空 owner 不等于可接受的用户业务
身份；按调用边界校验，不全局替换空值。保存的 canonical 授权根固定比较，访问
路径仍解析 symlink；后来出现的链接不能扩大原授权。App proxy 保留完整多值
query，写桥检查的目标必须与下游执行相同，包含重复值与空值。

MCP 工具调用要求当前授权；正确签名的退休 scope 只可 DELETE 它初始化的确切
transport，完整 scope 含账户、会话、installation、work 和 run lease。新 lease
不能继承旧 transport 的清理权，退休 POST 仍拒绝。核验实际 SDK transport 释放，
不只看 HTTP 状态；401 须区分 method、route、runtime PID，后续清理拒绝不改写
已经完成的业务结果。工具启动失败可能没有 `is_error` 回包，缺少错误行不证明可用。

同 kind 的 connector MCP 是进程级实例；即使 token 命中缓存，也先验证本次
installation 和 owner。ConnectorManager 负责该 installation 的持久刷新与锁；
凭据新鲜度不替代调用权限。外部登录 await 前后的目标 credential owner 都须有效；
`user_id=None` 的后台无过滤查询不等于只选无 owner 记录。
Connector OAuth 在本地安装绑定前可取消，回调单次领取；普通失败重新发起连接。
十五分钟 TTL 限制回调领取，领取后的网络操作使用各自超时，不反向恢复已消费的
回调；取消与本地绑定由原 login 的锁确定先后。

## 当前数据与存储演进

演进基线是实际运行代码能正常使用的完整数据集。保全账户、知识、用户文件、密钥、
归属及仍有效的状态/效果；不因取消旧 API 或不搬旧聊天而删除源端资料。
旧记录在核验、效果收束及当前责任交接后可退出，不永久保存原始请求影子档案。
旧格式 Wiki 任务的限定清理同时要求“已结束”和“责任已收束”；终态名称或日期
不足以判断，不删除正文、现有来源依赖、当前任务或未决效果。实际清理另受操作授权约束。

长期只维护一种当前表示。新空库创建完整 DDL；已有库启动前等值检查当前 marker，
不匹配即拒绝，不自动建空库掩盖问题，也不维护历史升级链。marker 只防错配，不能
证明内容完整。只有结构或语义变化才改 marker，纯代码优化不改。

当次发布负责旧 writer 退出后的存量检查、必要有限转换和结果验证。当前完整声明、
转换边界和验证入口统一在 [current-storage-format.md](current-storage-format.md)，
这里不重复版本号或发布步骤。跨数据库与文件不构成全局事务；部分完成或提交未知
保持业务关闭，按实际持久事实前向恢复。使用兼容当前数据的代码恢复，不回灌旧库
覆盖新文件，也不新建第二份正式知识库。交接与恢复责任结束后，临时转换及其专属
夹具退出；未来格式变更由未来变更承担，不承诺任意旧库/旧包直接打开。

秘密正文由 `credential_secrets` 唯一保存；恢复关系以 `recovery_of` 保存，
对外派生后继信息。删除旧物理列或接口时同步生产者、消费者及测试，不按同名字段
全局删除，不把旧 rejected/unknown 转成可执行或已成功事实。

## 文件归属与长输入

新附件和长输入使用账户下的 `attachments/<session_id>/` 与
`large-prompts/<session_id>/`。上传、下载、发送校验、fork 引用/复制、准备、重试
与清理保持同一 owner；不逐请求改全局 Settings。

存量全局文件按实际 owner 和引用交接；归档会话、fork 后代及原生 transcript
中的绝对路径引用也须处理。归属不明先保留。旧 writer 退休、内容与引用验证后
才能删旧位置；删会话本身不证明文件无人使用。正常新写入应符合安装资源声明，
不能靠补 marker、删除有效 marker 或关闭检查绕过归属问题。

Codex exec 对话和重试沿现有传输：拼装后的 prompt 按 UTF-8 字节计，`<=102400`
使用 argv，`>102400` 完整写文件并让模型读取；用户原文完整持久化。这是明确
保留的选择，账户路径修正不重新打开 stdin 改造，不恢复票43的完整 argv/env
预算项目，也不扩大到 one-shot。运行参数与诊断保留可读信息，只掩码实际秘密。
opt-in App Server 的大输入通过业务 RPC 保持 inline，不走该文件溢出路径；原输入
及附件仍由同一 submission 与文件 owner 保全。

凭据的 `CODEX_HOME` 同时保存认证和原生会话。重新授权借用已有目录；取消、
provider 失败和 spawn 失败不授予删除该目录的权限。用预存 rollout 验证这些
清理出口，不能只验证新建空目录。诊断须区分仍可重读的完整原文、历史中确实发生
的重读和已丢弃的字节；显示预算截断本身不证明原文丢失。

## Wiki 的恢复与效果证据

Registry 统一 Store 创建、注册、冷恢复与准入。新注册只在完整账本和 filesystem
binding 持久化后发布配置；已有配置丢失账本时保留配置与正文并关闭写入，不自动
建库、重造身份。原注册命令重入先验证已有 binding。一次 Construction 使用尚未
公开的候选 Store 完成 observer → history → 索引 → job 恢复，再公开对象；普通
请求不反向触发恢复，不增加独立 recovered 标记或常驻恢复循环。

health 只在冷生命周期绑定；换代前关闭准入并确认 constructor、receiver、Store
退出。旧 observer/journal 不静默接到新 health。存储层隐藏实际连接的持久性设置
和错误分类；Registry、History、job execution 分别承担生命周期、文件效果与任务结算。

文件修改/删除保留同步后的 blob/prepared、实际效果及父目录同步、applied/receipt。
取消由一个 owner 依次表达意图/撤权、释放锁、join 原 writer、核验效果、结算；
pending 表示仍在停止，confirmed 不表示业务成功。恢复不调用模型或重放工具。
job item 使用 typed JSON 与精确 ExecutionRef，同一 attempt 的多批不能混结算。
缺少原执行引用时保留 attention，不猜测从未执行。

通知先以原身份进入主库并提交，再 ACK Wiki outbox；重送幂等，通知失败不改已提交
效果。来源授权要求实际正文及版本/身份/覆盖证据，目录或 ingest 摘要不能替代。
移动和 reread 的身份、路径、revision、字节来自同次读取，以稳定 output_doc
定位结果；旧路径被其他文档占用不能污染其前像。历史展示不自动 restore。

优化保留独立断言：纯进度只使相关投影失效，成员/资格变化才核对 membership；
坐标缓存只持有确切 revision 的有界不可变坐标/hash/章节标签，每次读取新 snapshot
并核验当前权限，不缓存正文、可变 tokens 或授权。`no_change` 仍核验正文、版本、
权限、覆盖并生成真实回执。标题、来源章节与锚点来自同份 CommonMark 结构。

外部文件移入尚未观察的新目录存在身份盲窗；使用现有诊断与显式核对入口，不承诺
靠扫描或哈希重建从未取得的身份。具体领域合同和成本证据归
[Wiki 当前合同](wiki-development-summary.md)，不重复维护另一套任务清单。

## 账户搬迁的开发约定

`server.portability` 提供一个公共离线导出/导入实现，CLI 是薄入口，各领域维护自己
的选择、排除和恢复规则。同版空数据目标、当前 home 布局及可变 home 前缀是支持
边界；用户侧范围和操作步骤只维护在
[账户导出与导入](../backup-and-migrate.md#account-export-and-import)。

每次按真实账户归属、资源注册和目录收集，不能写死今天的应用名、vault 位置或数量。
普通内容增长自动纳入。新增持久类型、字段/关系、根或格式时，同次变更更新
`server/db/portability.py`、`server/portability.py` 或 `server/wiki/portability.py`
的相关规则，明确排除项并验证往返；不预造迁移插件框架。

源端只导出稳定资料：未决写入交原 owner 收束；稳定副本完成后恢复源端，再压缩。
导入先核验，再私有暂存、重绑明确的系统路径和本机身份，持久化后启用。不以单库
一致性冒充跨资源快照，不扫描改写任意正文/第三方代码，不把源执行过程搬到目标。

账户搬迁专项的隔离验收覆盖首次导出、增长后再次导出、源端离线后的独立导入、
原账户登录、agent 配置/memory/Wiki、应用依赖重装后的数据读写，以及中断拒绝。
专项用例、夹具和审查副本按用户要求退役；这是保留的历史验收结论，未来语义变更
仍需当次验证，不免除现行权限、完整性与恢复测试，也不证明生产曾执行搬迁。

## 暂停范围与恢复条件

当前没有承诺完整原生尾部、跨请求宿主复用或新的输入确认保证。
原七项执行扩展仍暂停；其身份、依赖、恢复条件及选定运行时路线的关系
统一见[暂停范围](../plans/runtime-execution-scope.md)。

## Conventions 与验收

配置边界回归由 `test_request_configuration.py` 和 `test_model_settings.py` 覆盖；
`test_terminal_recording.py`、`test_session_owner_failure.py` 与
`test_submission_recovery.py` 使用真实 SQLite 验证原结算和故障接管，包含提交后
投影/通知失败及重复迟到 terminal。能力组合与工具可见性证据归[Harness 合同](harness-layer.md#source-and-verification)。这些离线证据不证明运行版本已发布。

- 一个真实事实只有一个 owner；接口接管时同步全部消费者和删除项。保留承担
  不同职责的 quota、事务锁、文件锁、receipt/journal，不按名称或文件长度合并。
- 不为一般修复引入 Repository/PostgreSQL 抽象、Event Bus、Queue Manager、执行
  表、PDEATHSIG、状态机库、Wiki items 多表化、持久缓存或指南协商状态机。
  新机制须有具体需求和无法由现有 owner 承担的证据；这些既往取舍不是永久技术
  黑名单，需求或事实变化后重新评估，不能由局部修复自行扩大范围。
- 配置更新保全用户选择；动态授权在实际边界检查。已发布 Canonical Vault 仍是
  知识正文权威，框架 SQLite 称“运行数据库”，避免用“主库”混指两者。
- 用调用者实际接口验证正常、拒绝、竞争/取消和提交中断。命中原 work 的真实
  阶段，分别取接纳、送达、提交、通知、退休证据；不靠额外 sleep 拼顺序。
- 测试随被替代行为调整，保留独立拒绝、恢复及共享 fixture 消费者的覆盖。有效
  同版证据复用，命令和门禁只维护在 [testing.md](../testing.md)。源码、隔离验证
  和生产上线分别陈述，历史数量不作为新候选通过证明。
- 机制测试不证明自然模型质量、token 收益、生产 p95、峰值内存或任意并发组合。
  效率结论需要同输入同版本测量，可选质量/规模评测不扩大默认发布范围。
- 热迁移、任意跨版本导入、双机同步、账户合并、额外 backend 和阅读器集成按各自
  产品需求另定范围，不借文档合并恢复暂停项目或新增实施授权。

## 决策与历史证据

### 既有领域接口的取舍

第二阶段选择深化现有 owner，是因为这些变化已经有真实消费者；不是以新增类、
总代码量下降或所有后端使用同一种传输作为完成标准。

| 操作 | 已知变化与集中责任的收益 | 保留的代价和边界 |
| --- | --- | --- |
| S1 返回接管 | BG、Research、委派共同消费准入/唤醒/原身份确认 | 来源的正文、重试和业务结论继续分别持有，返回仍逐条派发 |
| S2 原资源与 S2b 结算 | 宿主/后代形态、完成范围变化时，不要求 Wiki/App 理解运行时私有集合 | 原身份、未知和实际退出证据不能缩成 session idle；业务、记录、通知和退休分别取证 |
| S3 配置 | 模型、凭据与工具配置持续变化，保存/继承/准备捕获共享规则 | 慢准备不占住配置短锁，配置快照不冻结当前授权 |
| S4 运行时 | Claude/Codex 已有差异，后续运行时需要明确能力和送达证据 | 复用 Harness 的资源责任，允许私有 adapter 变化；不预设 ACK、steer 或完整尾部相同 |
| S5/S6 Wiki | 多入口反复遇到重绑、借用、撤权与提交；W1–W4 是现有完整业务操作 | 冷准备、History 文件效果和 Store 事务保留不同范围；简单查询和 App 兼容网关不整体重写 |
| S7 读取 | 更多任务与诊断沿同一读取生命周期接入，页面不再拼时序和 baseline | 各领域保留 seq/身份/终态规则，不迁成无差别 reducer；视图仍拥有观察结束 |
| S8 排空 | 新宿主与资源在原 owner 内加入事实，安装不随私有字段变化 | 保留显式域清单、事件循环封口和跨线程准入；成功收据不代替实际退出 |

完整行字段由 `RecordProjection` 维护、领域解码仍各自负责；Research 的原任务
登记保留失败清理及关停中的迟到交接。多个集合不自动意味着重复权威，不据此
引入 ORM、跨域 TaskManager、统一 Job 总线或另一套会话所有者。

当前约定在上文维护；需要原讨论、实施顺序或逐批验收时查固定版本：

- [原架构执行计划](https://github.com/Danuvius152/Octopus/blob/cf98ecb75d1aa741c31292977ac9d83e727d366f/docs/plans/minimal-architecture-execution-20261020.md)：Owner/事务/输入/Wiki 架构的取舍、票20和其余批次验收范围。
- [原账户简化与搬迁计划](https://github.com/Danuvius152/Octopus/blob/cf98ecb75d1aa741c31292977ac9d83e727d366f/docs/plans/architecture-simplification-20261023.md)：接口退出、存储政策取代关系、搬迁专项验收与当时的发布缺口。
- [Owner 范围失控复盘](../post-mortems/2026-10-21-session-owner-scope-creep.md)、[开户退役复盘](../post-mortems/2026-10-24-account-setup-retirement.md)：保留触发条件、原因及可复用的回归经验。

这些证据限定于其记录版本。本文不复制旧“尚未实施/待发布”状态，也不从历史
验收推导今天的生产版本；本机发布按实际配置和回执核验。

# /research 连续修改中的完成点与责任交接（2026-11-04）

状态：历史复盘。所列反例已有对应修复；内容证据、恢复与工具隔离的现有边界另列，
不代表 Research 已无缺陷或当前部署已验收。

## 摘要

近一个月，`/research` 的修改从 leaf 退出、核验和报告生成，延伸到原会话返回、
取消、排空、重启恢复及浏览器首次创建。最能串起这些修改的解释是：**一个局部**
**完成信号，被拿去替代下游所需的另一种证据。** 文本可解析被当作 leaf 成功，未被
反驳被当作已证实，报告完成被当作主会话已处理，取消落库被当作执行已停止，创建
回执被当作当前界面已有可信状态。这些完成点各有不同的写者、身份和失败窗口。

但仅说“区分完成点”还不够。历史还暴露了配置捕获不完整、等待后权限变化、返回
入口重复编排、恢复查询漏选旧记录，以及测试夹具避开真实交错等问题。它们共同
造成了反复补齐相邻保证，不能一概解释为同一个 bug 修好后又被改坏。开发经验是：
先确定消费者究竟需要哪个事实，再让原操作一直负责到相应提交或退役；对无法证明
的效果保留未知，不能靠重试、默认值或更宽泛的“成功”补齐。

## 调查范围与证据

调查窗口为 **2026-10-04 至 2026-11-04（Asia/Shanghai）**。以本地 `main` 的
`07b83bf2fed2e1d88ca1e782eafe384e86988355` 为代码版本边界，检查相关路径的
Git 历史、修复前后实现、现行合同及回归源码，不只搜索提交标题中的 research。
工作树中其他尚未交付的会话/问题修改不作为本复盘的历史结论。

证据能够说明旧实现允许什么错误结果，以及对应测试如何构造反例；没有据此取得
生产事故日志、每次开发的完整过程或供应商现场复现。本次未重跑产品套件、调用
真实模型或发布；下列测试入口说明覆盖范围，不表示该版本在本轮运行通过，也不
把提交说明里的历史通过数当作正确性的证明。

## 关键时间线

以下节点解释保证怎样逐次补齐，省略仅调整夹具、迁移文件或更新文档的提交。

| 日期 / 提交 | 已建立或修正的保证 | 仍不能由此推出的事实 |
| --- | --- | --- |
| 10-14 `7449f12` | launch 在第一次 DB/广播等待前登记 owner；HTTP waiter 取消不撤销原工作；fan-out 用 TaskGroup 等待兄弟 leaf 清理，shutdown 保留未退役任务。 | 原启动/流水线有 owner，不代表之后的取消控制操作也有 owner；协程退出不证明所有原进程已退出。 |
| 10-19 `203f7f3`、`0594d87` | 保留异步请求的原调用与返回合同；核验区分显式支持、未证实和失败，增加后续处理回执及浏览器版本确认。 | 身份有记录不代表每次返回都按同一规则接管；有报告或有 admission 回执不证明主会话已处理。 |
| 10-25 `fe2200f`、10-26 `737a583` | synthesis 错误/空输出结束为失败并保留材料；在实际 leaf spawn 处检查执行权限。 | 失败不冒充报告，不等于成功报告证据充分；入口选定凭据也不能代替后续撤权检查。 |
| 10-27 `b19f6f9` | leaf 要求成功 provider terminal，失败部分文本不进 findings/votes；报告文件同步后竞争唯一终态；合法新返回可恢复原归档会话。 | 终态事务不拥有取消请求的全部后续动作；归档恢复也不授权重放已经撤销的旧输入。 |
| 10-28 `bbadc28` | pipeline 与 delivery observer 共用登记和排空入口，失败清理继续可见，shutdown 接住原任务晚交出的 observer。 | 统一登记不意味着所有任务都该收到 cancel；控制任务需要保全自己的提交。 |
| 10-29 `b8e0f32` | 区分永久拒绝、容量/忙碌与 ACK 未确认；沿原来源回执核对，不因超时重跑研究。 | 原回执查询成功一次，不代表重启扫描覆盖所有 pending 或历史分裂写窗口。 |
| 10-31 `7827fff` | Research 返回改走共享 `admit_return`，session 拥有回执优先查询与完整接管。 | 共享入口替代重复编排，不自动证明所有启动恢复查询和状态投影都完整。 |
| 11-02 `fd7ab9c` | 排队前捕获有效模型和凭据 ID；配置等待后重查 shutdown，拒绝已关闭/消失的目标。 | 保存 session 字段不等于已经解析 Agent 默认值；捕获选择不等于冻结权限。 |
| 11-03 `983ff39` | 恢复扫描纳入 completed/pending、unknown 和未定回执，读取原持久输入结果；回执与 admission 投影同事务修复；保存报告可独立下载。 | 没查到原输入只有在身份、读取与旧 writer 退休证据成立时，才足以断言未接纳。 |
| 11-03 `2358819` | 首次 `null → G` 后用当前读取确认原创建，不重发 POST；失败保留“已创建、状态待确认”。 | 首次建立代次不是任意跨代接纳旧响应的许可，浏览器确认也不是人已阅读。 |
| 11-04 `4f1418d` | 取消在首次 DB 等待前捕获原 pipeline，登记独立 control 并 shield waiter；重复取消补发必要的原任务信号，shutdown join 控制而不打断事务。 | 返回 cancelled 行仍不是实际资源退休，后者由原 pipeline/leaf owner 确认。 |

## 影响

旧代码与反例支持的后果范围包括：失败材料进入 findings/votes、空报告冒充完成、
取消终态被覆盖或已取消任务继续运行、主会话重复接纳汇报、排队任务换用新默认值，
以及服务端创建成功而界面没有卡片。它们说明旧路径允许这些结果，不表示全部已在
生产发生。能够直接从变更看到的工程代价，是同一链路多轮改写、相邻消费者与恢复
路径反复验证，以及此前局部保证不能回答后一次故障。

## 具体反例与因果

### 1. 有输出、没有反证，都不足以构成有效证据

`0594d87^` 的核验把不可解析 verdict 默认成未反驳，并把 survivors 统称为 verified。
超时、格式错误和“没有找到反例”因此可能借存活进入已核验叙述。该提交把无效 verdict
保留为未知解析结果，要求显式正向支持达到按配置票数计算的严格多数；失败票不能
缩小分母，让少数成功票自动胜出。有效但不够支持的结论仍是 unverified，没有有效
verdict 则是 failed。

这仍没有堵住协议层。`b19f6f9^` 的 web leaf 收集到 JSON 后，流 EOF 即可返回无错误
结果；search 编排也未跳过带 error 的非空文本。于是完整 JSON 后异常断流，甚至
进程零退出但缺少 provider 成功 terminal，都可能提供 findings；部分核验文本也
可能贡献正向票。`b19f6f9` 同时修两端：leaf 判定协议是否成功，编排拒用失败结果。
只修 parser 或只补 prompt，不能建立这条保证。

回归入口：[leaf 测试](../../tests/test_research_leaf.py)的
`test_leaf_eof_keeps_partial_but_requires_success_terminal` 用实际合成子进程分别
输出零退出 EOF、错误退出与成功 terminal；[编排测试](../../tests/test_research_orchestrator.py)
的 `test_parseable_partial_search_is_excluded_from_findings`、
`test_failed_verification_is_never_counted_as_verified`、
`test_verification_requires_positive_majority_despite_failed_votes` 验证结果不混入。

适用边界：这里证明协议成功门槛与计票规则。provider 成功不证明 URL 已实际读过，
多个角度/多次模型投票不证明来源独立，`verified` 也不是客观真理的认证。

### 2. 报告文本、持久产物与任务终态，需要各自的提交依据

`fe2200f^` 用 `(the model produced no report)` 兜底 synthesis 空输出；即使
`LeafResult.error` 有值，也可能继续返回一份 ResearchReport。流程走完因此掩盖了
最终报告根本没有生成。修复让这类任务 failed，保留已收集材料和失败原因，避免发送
成功报告。全部 search 失败而 synthesis 仍返回正文则是另一种情况：流程可以产出
报告，但不能据此宣称问题已有证据支持。

另一个窗口在 `b19f6f9^`：写报告 best-effort，返回空路径也可标 completed；完成和
取消分别更新状态，completion 的 shield 保护清理，却可能让已确认取消被后来完成
覆盖。修复把规则收回真实效果：报告文件及目录项同步成功后，才以
`WHERE status='running'` 的事务竞争 completed；cancelled/failed/interrupted
共用同一终态入口。先赢的是业务提交，后来的回报只能承认原结果。写盘与 SQLite
不是一个原子事务：可能有文件而没有 completed，不能反向由文件存在推导任务成功。

回归入口：[执行回归](../../tests/test_review_execution_regressions.py)的
`test_f16_failed_synthesis_is_failed_and_retains_gathered_evidence`；
[manager 测试](../../tests/test_research_manager.py)的
`test_report_write_failure_never_completes_or_delivers`、
`test_cancel_confirmed_before_report_commit_cannot_become_completed`、
`test_cancel_after_completion_preserves_pending_delivery` 分别注入 fsync 失败和两种
终态胜负顺序。前者验证同步失败处理，不等同于真实断电持久性实验。

适用边界：取消在 completed 后到达，会返回原 completed 并保留报告及后续交付，
不能假称撤销了既有效果；failed synthesis 的材料也不冒充可交付报告。

### 3. 报告完成、输入接纳、主会话处理与浏览器收讫不能共用“送达”

报告可在主会话忙碌或 Stop 暂停时完成，接纳后的汇报仍在队列中。旧字段
`injection_status=delivered` 表示输入已接纳；把它读成模型已处理，会让卡片、排空
和恢复提前结束。`0594d87` 开始保存独立 delivery receipt，后续由原输入的
RequestCompletion 判断处理结果：只有 committed success 才是 processed；结果
不可用、记录未确认和明确失败有各自的解释。通知失败不抹去已提交的处理成功。

`bbadc28` 又补上 owner 的交接：pipeline 完成后，等待主会话结果的 observer 仍是
Research 的责任，不能只记日志就从 drain 消失；shutdown 已取快照后，原 pipeline
交出的 observer 也要被同一轮 join 接住。这里需要统一的是登记与交接规则，而不是
用“报告完成”顺便结束所有后续阶段。

浏览器通过 `/seen` 确认的是匹配 job 下的 request ID 和 receipt version。显示过
queued v1，不等于后来 processed v2 已显示；v1 的迟到回执也不能覆盖 v2。此确认
既不驱动主会话处理，也不成为无人打开页面时自然排空的必要条件。

回归入口：[manager 测试](../../tests/test_research_manager.py)的
`test_delivery_receipts_distinguish_queue_processing_and_current_browser_confirmation`、
`test_natural_drain_waits_for_report_processing_without_a_browser`、
`test_shutdown_joins_observer_created_by_original_pipeline_cleanup`；
[ResearchCard 测试](../../web/src/components/ResearchCard.test.tsx)和
[会话 Research 测试](../../web/src/lib/sessionResearch.test.tsx)覆盖精确版本确认与
旧 queued 回执迟到。前者包含受控 receipt；真实 session 返回另由下一节入口覆盖。

适用边界：processed 证明原汇报输入的成功处理已提交，不证明研究结论正确或用户
已读；浏览器收讫只证明界面报告了该版本，不证明人的理解。

### 4. 取消事务成功后，停止原执行仍有独立责任

10 月 14 日已经让启动和 leaf 清理跨越 HTTP waiter；10 月 27 日又有了唯一终态
事务。但在 `4f1418d^`，`cancel()` 自己仍由请求 waiter 执行：先等待
`confirm_research_outcome`，再按 job key 找 task 发 cancel。真实 UPDATE/COMMIT
期间 waiter 被取消，可以留下 cancelled 行而没有停止 pipeline；再次取消因没有
赢得终态而不发信号。已有 launch owner 和终态 CAS 都不能补上这个断点。

`4f1418d` 将取消作为独立保有的 control：在第一次 DB 等待前捕获原 pipeline；
请求只 shield 等待 control；只要权威行已 cancelled，就对捕获的原 task 发必要且
至多一次的信号。迟到 control 不按 key 取消后来替换的 task。shutdown 把 control
也纳入等待，但只向 pipeline/observer 发取消，避免把完成取消事务的任务一起杀掉。
这解释了为何此处保留两个 registry：两类工作的取消语义不同，不是再建一份状态真相。

回归入口：[manager 测试](../../tests/test_research_manager.py)的
`test_cancel_waiter_cannot_leave_a_committed_cancellation_without_pipeline_stop`
阻塞真实 SQLite UPDATE 或已完成 COMMIT 后的返回，再取消 waiter；
`test_repeated_cancel_stops_a_previously_cancelled_job_and_joins_original_cleanup`、
`test_shutdown_joins_admitted_cancellation_without_cancelling_its_transaction`、
`test_late_cancellation_signals_only_its_captured_pipeline` 分别覆盖补信号、shutdown
和原任务身份。`test_cancelled_http_waiter_still_retires_original_research_cli` 与
`test_research_shutdown_joins_every_parallel_web_cli_retirement` 补实际合成进程退出。

适用边界：control 返回 cancelled 是业务决定；原 pipeline 仍负责 leaf/fetch 退休。
清理超时保留原任务和 blocker，之后 join 同一清理；不能因 cancel 返回、task 从某个
map 移除或第一次 shutdown 超时，就断言进程不存在。该协议也不撤销既有外部效果。

### 5. 创建成功之后，浏览器仍可能没有可信的当前状态

`2358819^` 中，`createResearch` 在 POST 前捕获服务代次；当前详情把代次从 null
建立为 G 后，成功 201 会因观察失效被丢弃。若旧 GET 为空、started 也丢失，服务端
已经创建任务，界面却一直没有卡片。反过来，无条件采用迟到 201 又会把更早的
running 覆盖到已观察的 completed，或污染已经离开的视图。

修复没有把 null 当作任意代次通配符。仅在原账户/token、连接、会话存续和视图仍
有效，且只经历首次 null→G 时，以当前 G 发起一次新的 Research GET，核对原 job
ID；POST 仍只有一次。确认读取失败或没找到 ID，就保留“已创建、状态待确认”及
已有刷新入口；POST 自身无有效回执则报告创建结果未确认。两者不混成创建失败。
原观察有效时可用创建回执补身份/元数据，动态状态继续服从已有事件与快照合并。

回归入口：[会话 Research 测试](../../web/src/lib/sessionResearch.test.tsx)的
`confirms the original creation after the first detail establishes its generation`
及确认失败/缺失、旧空快照、切会话和跨账户/连接/服务变更用例；
[离线浏览器回归](../../web/e2e/research.spec.ts)的
`research submitted before the first detail is confirmed by one current read`
控制详情、201 和读取顺序，断言原 job 卡片、报告链接、一次 POST 和一次新增确认
GET。该浏览器用例模拟 Research HTTP，不证明真实模型已执行。

适用边界：首次建代确认和 `/seen` 版本确认是两件事；前者确认原创建在当前范围的
状态，后者确认展示版本。null→G→H、账户/连接替换、归档/删除、离开原视图均不能
借首次建代规则接纳旧响应；重新查看走正常快照，不自动重发创建。

### 6. 捕获选择不等于冻结权限，原会话暂不可见也不等于返回身份失效

`b19f6f9` 已给排队研究保存 session 配置副本，但模型和 Agent 默认凭据仍在取得
运行槽位后读取。排队期间修改默认值，旧 job 就可能换模型或换身份。`fd7ab9c`
进一步在接纳前通过短配置边界解析有效模型与 credential ID；耗时等待和模型执行
在锁外。配置等待本身也会让目标关闭或 shutdown 开始，所以注册前还需核目标与
接纳状态，不能把“进过入口检查”当作已接纳。

这个快照仅保存原选择。`737a583` 加入的 spawn 授权和后续 Agent 活性检查仍要
使用当前权力：原凭据被删除、账户失效或 Agent 不再可用时结束原任务，不能偷偷
回退另一凭据。把这些都叫“配置一致性”，会在“保留原值”和“应用最新值”之间
反复摆动；选择与授权实际服务于两种不同保证。

返回也有类似区别。任务运行期间原会话可以归档并离开 live map，合法的新返回应
按原 account/agent/work/receipt 恢复同一会话；Stop 的暂停仍保留。已经因归档撤销
的 queued 输入不能重放，删除、撤权、关闭原 work 或缺失合同也不能借“恢复”绕过。
`b19f6f9` 补行为，`7827fff` 再让 Research 使用 session 的共享接管入口，消除它
自己拼 receipt lookup、wake-up 和 admission 的另一套顺序。

回归入口：[manager 测试](../../tests/test_research_manager.py)的
`test_queued_research_keeps_admitted_choices_and_rechecks_authority`、
`test_research_waiting_for_configuration_cannot_admit_after_shutdown`；
[API 测试](../../tests/test_research_api.py)的
`test_research_waiting_for_configuration_rejects_a_retired_session`；
[真实 SessionManager 返回测试](../../tests/test_research_session_return.py)的
`test_new_research_return_restores_original_session_and_stop`、
`test_archive_revokes_queued_report_and_only_new_report_wakes_session`。这里用受控
报告生成器隔离模型，但保留真实 session、SQLite 与返回入口。

适用边界：配置快照不是权限租约；归档不是永久关闭，但返回恢复也不是重新授予原
work 权限。Research 拥有报告与处理观察，session 拥有接纳、恢复和输入顺序。

### 7. 没有收到回执，不足以证明没有效果；恢复也不能顺便重做工作

`b8e0f32^` 对 admission 广泛捕获异常后重试，无法区分确定未接纳与已提交但 ACK
丢失。`b8e0f32` 分类错误，`7827fff` 将完整接管收回共享入口：容量/忙碌明确拒绝
才允许延后尝试；永久完整性拒绝保留报告并停止；未知效果只沿
`research:{job_id}` 查询原回执。任意 ValueError 或程序错误不是重试分类。

即使实时接管正确，恢复仍会漏。`983ff39^` 的扫描把 unknown admission 和未定
receipt 分开处理，没有完整覆盖 completed/pending 且尚无 receipt 的窗口，以及
receipt 已终态但 injection_status 尚未同步的旧记录。新扫描以原持久来源和输入
完成记录重建结果，receipt/admission 同事务修复；重复恢复不制造新输入，也不为
读取回执创建 Owner、唤醒会话或重新读取已不必使用的报告材料。

“查无记录”本身也有证据门槛：只有原 admission 为 pending、保存的调用身份受
支持且账户/会话匹配、权威读取健康、受管启动已证明旧 writer 退休，才可把无原输入
记成未接纳失败。否则保留 unknown；原先 unknown 不因后来缺席就变成确定未发生。
已经保存的报告保持 completed，并有独立下载入口，汇报失败不再等于产物丢失。

回归入口：[真实返回测试](../../tests/test_research_session_return.py)的
`test_report_lost_ack_reconciles_original_input_without_repeating_research`、
`test_unconfirmed_report_is_not_overwritten_by_later_refusal_or_restart`、
`test_permanent_transcript_rejection_retains_report_and_never_retries_after_recovery`；
[共享接管测试](../../tests/test_research_return_admission.py)验证 destination owner
失败且报告文件已删除时仍只读原 receipt；[manager 测试](../../tests/test_research_manager.py)
的 `test_restart_links_original_persisted_input_and_its_real_completion`、
`test_restart_repairs_injection_even_when_confirmed_receipt_is_already_terminal`、
`test_failed_source_read_cannot_be_used_as_proof_of_no_admission` 覆盖旧窗口和读取不确定。
[API 测试](../../tests/test_research_api.py)还覆盖 completed 未汇报材料的下载和账户隔离。

适用边界：这些恢复测试构造真实持久记录并重走恢复，不是整机崩溃实验。当前恢复
修的是原返回观察；running job 在启动 sweep 中标 interrupted，不从中间阶段恢复
研究流水线。存储失信沿既有 fail-stop，不降格成普通 delivery failed。

## 为什么修改会反复，测试却没先拦住

从上述提交关系可以归纳出四个工程原因；它们是基于代码的推断，不是对每次开发者
动机或审查过程的事实断言。

1. **用一个状态替代多个消费者的证据。** completed、delivered、cancelled 都曾被
   赋予超出提交点的含义。修复一个状态后，另一个消费者仍按旧含义行动，于是问题
   换一种表现再次出现。继续加标志不能替代明确每个事实的写者和读者。
2. **owner 只罩住原来那段工作。** launch 已被保有，新的 cancel 控制仍裸露在 HTTP
   waiter；pipeline 已登记，observer 的清理曾会只写日志后消失。每增加一种控制或
   交接，都要核它是否一直由同一责任方保有到真实效果完成，不能外推“已有 owner”。
3. **表示和查询范围没有随语义一起闭合。** 保存 session 副本漏 Agent 默认值，恢复
   查询漏 pending/旧分裂投影，返回路径重复编排。这些缺口不能只靠重命名完成状态
   修复；要检查表示是否足以承载原选择、原身份和待恢复的真实存量。
4. **测试证明了邻近情况，遗漏恰好相反的交错。** `b19f6f9^` 的 leaf 错误 fake 返回
   空文本，验证不了 error 携带可解析 JSON；`2358819^` 的 submitResearch 夹具预设
   `dispatchGeneration="service"`，排除了首次 null→G；`4f1418d^` 的取消终态竞争
   测试没有在真实 UPDATE/COMMIT 期间取消 cancel waiter。另一个场景已绿，不能
   代替这个交接处的反例。

因此，这不是“多写集成测试”一句话能解决的问题。纯编排测试适合证明计票、去重和
失败文本过滤；协议/进程测试证明 terminal 与退出；真实 DB 和 SessionManager 证明
接纳与恢复；前端 deferred/离线浏览器证明观察资格。把关键责任也 mock 成成功，或
用 UI 卡片代替后端退出证据，都可能让更大的套件继续漏掉同一个断点。

## 后续开发应怎样应用

这些是选择验收和评审问题的方法，不要求普通局部修改新增通用状态机或重跑全套。

1. **先写消费者需要的事实。** 涉及跨组件完成时，列出原 job/work/request 身份、
   选择、当前权限、业务提交、输入接纳/处理、观察版本与退休条件。只列本次会改变
   的事实，并写清唯一 writer、提交点和失败去向；避免以“有结果”“任务 done”验收。
2. **先做最能否定当前解释的反例。** 本例优先选“有合法文本却失败”“终态已提交而
   waiter 被取消”“创建成功却跨首次建代”“ACK 丢失但原输入已提交”。记录注入点、
   目标观察和旧代码为何应失败，再选最小充分的测试层次。
3. **每个 await 交接都核原身份和责任。** 配置等待后核目标存续，spawn 前核当前
   授权，写入用真实提交条件，cancel 绑定原 task，done callback 只能释放自己仍
   拥有的登记。不要靠 key 相同就认为还是同一次工作。
4. **分别决定重试工作和重试观察。** 容量拒绝有未接纳依据才延后接纳；ACK 未知读
   原 receipt；浏览器刷新读原 job；shutdown 超时 join 原清理。没有证据的缺席保留
   unknown，不能把这些入口都实现成“再跑一次”。
5. **改表示时一起审计旧存量和所有 writer。** 新字段、终态规则或回执投影加入后，
   检查历史分裂记录、pending 无 receipt、首次对象、重复取消和晚回报。事务解决
   原子性，任务保有解决寿命，来源身份解决关联，不能相互替代。
6. **让责任复用，保留事实差异。** 多消费者共享 session 返回接管；Research 保留
   自己的报告、重试策略和观察。pipeline/observer 可统一登记，cancel control 因
   信号语义不同独立保有；不因都叫异步任务就强行共用一个成功值或取消策略。

选用上述回归入口后，按[测试政策](../testing.md)执行受影响检查并复用仍有效证据。
一次 leaf 协议改动不要求重新测所有 UI，一次文档复盘也不触发真实模型或发布门禁。

## 仍存边界与关联

- **内容充分性仍有后续范围。** 当前 Finding 主要保留 claim/url/angle/verification，
  核验 note、被否定主张、原问点覆盖和来源独立性尚未形成完整证据交付。全部 search
  失败仍可合成正文；这不违反当前流程成功定义，也不证明研究问题已解决。改进归
  [研究工作流计划](../plans/ai-research-workflow.md)，不能在本复盘里写成已实现。
- **隔离由实际能力证明。** web leaf 无继承 MCP/connectors/memory，Codex 保留独立
  exec 路径和只读 sandbox；这些不等于原生工具面严格 web-only，prompt 的 ONLY
  也不是强制边界。推理 leaf 的 tool-free 合同与 web leaf 分别验证。
- **恢复、显示与效果各有范围。** 当前不承诺任意阶段续跑、跨 job 缓存或整机断电
  验收；浏览器 GET 是范围内的一次服务端观察，不是全局最新或人已读证明。本文也
  不新增重试、恢复任务或产品授权。

当前合同由原文档维护，本文只保存因果与回归经验：

- [Research 当前合同](../implemented/native-deep-research.md)：流水线、leaf、终态、
  原返回、配置与恢复责任。
- [前端观察合同](../implemented/frontend-list-reconciliation.md#research-创建观察与浏览器确认)：
  创建回执、首次建代、快照合并与精确版本确认。
- [跨模块完成与退休](../implemented/architecture-contract.md#输入完成与资源退休)、
  [公共返回接管](../implemented/architecture-contract.md#公共返回接管)：session 的责任。
- [SessionOwner 范围失控复盘](2026-10-21-session-owner-scope-creep.md)：责任完整与
  故障观察的问题在 Research 再次出现；本次额外暴露内容证据、首次建代、配置选择
  与当前权力的差异，不能只照搬消费者迁移的解释。

复查历史可先运行以下命令，再用 `git show <上表提交> -- <相关路径>`：

```bash
git log 07b83bf --since=2026-10-04 -- server/research \
  server/db/research.py web/src/lib/research.ts
```

尤其对比 `git show b19f6f9^:server/research/leaf.py`、
`git show 4f1418d^:server/research/manager.py` 和
`git show 2358819^:web/src/lib/sessionResearch.test.tsx`，可以分别重建“有 JSON 无
terminal”“取消提交与停止脱节”“夹具预先建立代次”的具体断点。

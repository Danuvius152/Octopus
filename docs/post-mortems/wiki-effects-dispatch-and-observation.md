# Wiki 重派后，旧聊天仍应能观察当前进度（2026-11-04）

状态：历史复盘。调查窗口为 2026-10-04 至 2026-11-04（Asia/Shanghai），固定代码
基线为 `0f1570877be4e2ed5903377fe93955c454f43906`。依据 Git 差异、该版本实现、
合同和回归源码；未取得本文全部故障发生于生产的记录。原历史调查只做文档核查，
未运行产品套件、真实模型或发布；下列历史测试是回归入口，后续组合核对另列。

本文从[证据、范围与恢复复盘](wiki-evidence-scope-recovery.md)拆出异步
任务专题，回答三个问题：实际效果怎样结算，谁接收之后的自动汇报，旧聊天怎样
观察当前进度。权限、原文保全、来源确认及阅读证据的因果仍归原文。

## 发生了什么

A 聊天提交一个 Wiki job，任务进入需要处理的状态后，用户从 B 聊天显式发起
retry/readmit。B 拥有之后的自动结果返回；用户打开 A 时，仍可在当前
权限下看到同一 job 的新 attempt。旧 A 不应因 B 的工作再次自动生成汇报，也不应
因失去自动返回资格，永远停在旧卡片。

仅按 attempt 过滤，不能完整表达本次派发的接收者；仅从主库“最后接收的通知”
恢复卡片，也不能回答当前状态。通知全部 ACK 后可以没有待投递事件，但任务仍然
存在。反过来，若为了刷新卡片重新订阅或重送模型输入，就把观察变成了执行。

这条链路更早还有另一种事实混用：正文已存在、job 已取消、模型已退出，都不能
独立证明原执行已成功录制、文件效果已确认及原资源已退休。失去回执不等于失去
效果；以“重新运行一次”补通知或录制，可能重复生成或覆盖后来的人工修改。

## 为什么前一次修复还不够

这些修改补的是不同消费者所需的事实。新增自动返回是产品扩展，不计作旧功能
再次回归；其后的派发和观察缺口也不能仅凭提交顺序判成同一不变量被反复破坏。

| 节点 | 当时建立的保证 | 后续消费者需要的另一事实 |
| --- | --- | --- |
| 10-17 [8132a07][commit-recording] | 取消／立即重试交错中，原录制未确认引用仍能保存，不覆盖用户选择的 job 状态 | 当前 job 状态不能证明原请求终态或文件效果 |
| 10-23 [93122cb][commit-settlement] | 原 request terminal、recording、retired 分别保留；正常、取消及恢复共用效果结算规则 | 原执行结算不回答之后由哪个入口接收返回 |
| 10-25 [494fa95][commit-return] → 10-27 [cf98ecb][commit-dispatch] | 先增加向原请求汇总结果，再用独立 dispatch_generation 表达显式重派，并与结果接纳串行化 | 自动返回资格不能决定人查看历史卡片时能否读取当前事实 |
| 10-31 [159723d][commit-retirement] → 11-04 [4f1418d][commit-observation] | 退役按原 work 的资源证明；原聊天通过持久关联在当前授权下读取当前不可变快照 | 进程内引用缺失、通知 ACK 与当前状态可读性，均不能替代各自权威 |

10 月 27 日与 11 月 4 日的修正并不矛盾。前者限制旧订阅者的副作用，后者恢复
人的只读观察；读取不撤回派发交接，也不改写原来已经接纳的返回事实。

## 推翻旧解释的反例

### 1. 重派后旧聊天不自动汇报，但仍能读当前 attempt

触发顺序：A 留下原持久卡片 → 原任务结束并需要处理 → B 显式重派 → 新 attempt
推进 → 所有通知已 ACK → 从 A 的真实 HTTP 入口读取卡片。

权威观察应同时检查两边：A 没有新增自动返回输入；A 读取到的 attempt、
state_version、status 与 Wiki 当前 job 一致，且保留原 origin_request_id。
GET 不得创建订阅或新通知。仅断言“B 收到了结果”漏掉 A 的副作用；仅断言“旧卡片
存在”漏掉当前事实不可达；依赖待投递事件的实现则在全部 ACK 后失去更新材料。

这组对照由[重试回传](../../tests/test_wiki_retry_return.py)中的
`test_retry_in_another_chat_returns_only_to_the_current_dispatcher` 与
[当前卡片](../../tests/test_wiki_card_refresh.py)中的
`test_old_chat_reads_current_attempt_after_human_retry_and_all_notifications_are_acked`
共同固定。后一用例参数化 retry/readmit，确认旧持久通知摘要不变，而 GET 已取得
新状态。它不依赖重新唤醒旧模型来刷新卡片。

### 2. 返回已经接纳，ACK 丢失只补通知，不再生成一份汇报

在一组 job 的返回输入接纳后，让通知 ACK 抛错，再转交其中一个 job 并重送剩余
通知。应保留原 delivery 身份，主库返回输入仍只有一份；不能因组成员或 attempt
变化，重新拼一个去重身份制造第二次汇报。若重派与接纳同时发生，两者必须有
明确的接纳顺序，不能一边通过旧资格检查，一边将新归属提交。

[重试回传](../../tests/test_wiki_retry_return.py)中的
`test_lost_group_ack_and_member_transfer_never_create_a_second_report` 和
`test_return_acceptance_and_retry_have_one_admission_order` 分别注入 ACK 失败与
接纳交错。旧 control 重放另由 `test_old_control_replay_does_not_subscribe_to_a_later_dispatch`
检查：原操作响应丢失，不应使重放者订阅后来的派发。

### 3. 原效果未知或录制未确认，恢复不能重新生成

在文件原子替换前，或替换后但 applied 标记前中断；随后人工编辑甚至恢复成原字节。
恢复先保留原候选、收据和不确定性，不能用当前字节猜历史没有写过。候选字节仍按
历史保留期管理，过期不撤销收据所记录的不确定性，也不授权重新生成。若效果已保存
但索引失败，只恢复派生索引；若原请求录制未知，核对原 session/request 的持久
终态。取消状态已提交也不能授权新 attempt 与尚未退休的原 work 并行写入。

[任务恢复](../../tests/test_wiki_jobs.py)中的
`test_uncertain_publication_receipt_never_starts_another_model_turn`、
`test_published_item_recovery_never_generates_again_or_consumes_reread_budget`、
`test_recording_failure_retry_verifies_original_without_new_model` 分别检查未知发布、
已发布输出和原录制。观察点包括模型轮数仍为一、原 publication 不重复、人工字节
保留及 attention 状态，而不是只看最终存在一篇文章。原资源退役另见
[再准入回归](../../tests/test_wiki_retirement_admission.py)。

### 4. 入队已提交，丢失唤醒后重放还要继续推进

在准入事务提交后、worker.wake 前抛错，再提交相同操作。幂等命中同一 job 只证明
没有重复创建；如果复用路径跳过唤醒，原 queued 工作仍不会推进。
[65b5918][commit-wake] 补上唤醒，[26aa78f][commit-queued] 再直接依据 queued
状态，移除多余 created 镜像。已 running 的任务不应再次唤醒。

[操作边界测试](../../tests/test_wiki_operation_boundaries.py)中的
`test_lost_wake_recovers_original_checked_job_on_replay` 对单项／批量入口注入该
失败，检查 job 身份不变、仅有一条任务且 queued 重放唤醒，running 重放不唤醒。
这与“效果未知时不重做内容”可以同时成立：恢复推进取决于原阶段和已确认效果。

## 在固定基线下怎样成立

| 事实 | 权威与提交／读取边界 | 消费者及保证上限 |
| --- | --- | --- |
| effect receipt、change.applied、输出 revision 与索引 | writer 留下效果证据；jobs.settle_item 匹配原执行并核验实际输出 | 正常／取消／恢复共用结算；文件存在不等于原执行成功 |
| 原 request terminal 与 recording | 原 session/request 的已提交执行事实 | worker 对账原执行；最终答案字符串不代替录制 |
| 原 work retired | 原工作资源 owner；job_execution 核原引用与退休证明 | retry/readmit 准入；live map 缺失不证明资源已退出 |
| dispatch_generation | Wiki 控制事务提交派发代次与订阅；接纳 gate 协调转交和返回 | 后续自动返回；不撤回已接纳输入，也不授予只读观察权 |
| notification receipt／ACK | 原通知接纳与确认记录 | 去重与重送；不证明模型已处理，更不证明文章质量 |
| state_version／current_state | 主库原卡片发现关联；当前注册、权限和 Wiki 读事务取得不可变快照 | 人的当前观察；不创建订阅、模型输入或新派发 |

源码入口为固定版本的[效果结算][src-settlement]、[原执行与退休][src-execution]、
[派发返回][src-returns]及[当前卡片读取][src-observation]。这些维度有不同消费者，
不能用一个 is_complete 合并；本次也不因此引入新的 owner 或工作流引擎。

观察关联以原账户/session/vault/job 发现任务，仍检查当前身份、注册及权限。
换库、任务缺失或当前无权读取时，只保留原获准摘要，标明 current_state unavailable，
不披露新诊断，也不开放依赖当前权限的控制。[当前卡片测试](../../tests/test_wiki_card_refresh.py)
另用 `test_unreadable_current_job_keeps_only_its_original_summary` 覆盖这三类情况。
历史关联不能成为任意查询其他任务的凭据。

当前控制和展示的完整规则继续由[共享 Wiki 合同](../implemented/wiki-development-summary.md)
及[前端列表合同](../implemented/frontend-list-reconciliation.md#wiki-卡片详情与控制回执)
持有；本篇保留固定基线下的错误假设、反例和因果，不另存一份动态状态机。

## 后续核对：输入清理不替代 Wiki 效果

后续原生完成整改允许精确关联、取消且本地清理已确认的纯输入缺项不单独阻断
合法请求成功，同时保留原始记录不完整。该例外不会自动结清 Wiki item：
[组合回归](../../tests/test_mcp_input_completion.py)中的
`test_native_input_gap_does_not_replace_wiki_effect_receipt` 取得原 worker 的
成功终态，但没有 publication receipt，item 仍为 attention，原文不变，模型只运行
一次。它证明完成分类没有绕过效果门槛，不是一次实际发布成功或终稿冷读验收。

已有发布的恢复仍按上文核对原效果、录制与退休；未知写入、reader 截断和未确认
清理不享受纯输入例外。当前判断继续归 Wiki 结算及
[跨模块完成合同](../implemented/architecture-contract.md#输入完成与资源退休)。

## 仍存边界

- **模型流程义务与程序保证。** completed 不认证终稿冷读覆盖或语义理解，具体证据
  见[原文的边界说明](wiki-evidence-scope-recovery.md#终稿冷读是流程义务不是-completed-的程序硬门槛)。
  不为消除措辞边界自动新增状态或持久阅读账本。
- **设计取舍。** 当前状态不可读时保留获准历史摘要；它不代表最新状态。观察不提供
  自动返回资格，也不替代有当前权限和原资源退休前提的显式控制。
- **验证范围。** 已确认的是上述代码路径、差异和回归构造，没有由此确认新的生产
  故障，也没有重新完成全产品、真实模型或部署验收。来源确认的成本候选及未来
  产品能力归[原文](wiki-evidence-scope-recovery.md#仍存边界流程义务与性能候选)
  和已有 [Wiki 计划](../plans/wiki-future-plan.md)，不混入本次运行时结案。

## 可迁移的经验

1. **沿消费者定义完成。** 效果已确认、原执行成功、原资源退休、返回接纳及当前
   可观察分别举证；控制状态或队列现象不能替代原操作的事实。
2. **把限制副作用与保留观察成对验收。** 测 B 收到返回时，也测 A 不被唤醒而仍能
   读当前状态；只修一边，容易用删除历史关系或重新订阅补出下一条缺口。
3. **先确认效果，再选择恢复阶段。** queued 重放需要推进，通知丢失只需重送，
   效果未知必须保留核验；不让一种“重试”默认重做所有阶段。

[commit-recording]: https://github.com/Danuvius152/Octopus/commit/8132a0764b56966f9344a6dba09ca865fec52a23
[commit-settlement]: https://github.com/Danuvius152/Octopus/commit/93122cbb41ac06f83e6f6b542747481548313104
[commit-return]: https://github.com/Danuvius152/Octopus/commit/494fa95522b15afc7710bd1386d67f0568e4a0ae
[commit-dispatch]: https://github.com/Danuvius152/Octopus/commit/cf98ecb75d1aa741c31292977ac9d83e727d366f
[commit-retirement]: https://github.com/Danuvius152/Octopus/commit/159723da0333d3543ebbcd112761f0843550e0a5
[commit-observation]: https://github.com/Danuvius152/Octopus/commit/4f1418da3906fedd7221d9b239774681e785945c
[commit-wake]: https://github.com/Danuvius152/Octopus/commit/65b5918f307d81386343d33272262a20115d31a9
[commit-queued]: https://github.com/Danuvius152/Octopus/commit/26aa78f0aea69387cd5373bfb871802f624966f0
[src-settlement]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/wiki/jobs.py#L1188-L1248
[src-execution]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/wiki/job_execution.py
[src-returns]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/wiki/job_returns.py
[src-observation]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/wiki/job_observation.py#L13-L45

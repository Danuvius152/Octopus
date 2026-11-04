# 会话／委派连续修改中的三个完成点（2026-11-04）

状态：历史复盘。所列反例已有对应修复；运行时消费、模型内容质量和现存提示偏差
分别说明，不代表当前部署已验收。

## 摘要

近一个月，会话／委派的修改从重试文本归属、原请求完成，延伸到保留子会话、归档
返回、公共接纳、嵌套工作收束及删除后的责任撤回。串起这些修改的关键经验是：
**子任务结束、返回接纳、父模型处理是三个事实，必须按原身份分别取证、分别等待。**

子任务成功，只能证明它提交了结论；返回已接纳，只能证明父会话接住了对应输入；
父执行完成，还要能关联到这条输入，才证明运行时已处理返回。子任务列表没有
running 项、父模型后来成功过一次、聊天最后出现了回复，都不能替代这条关联。

这篇承接 [SessionOwner 范围失控复盘](session-owner-scope-creep.md)：10 月 21 日
收口原 receipt 和单一写者，解决了完成事实的归属；后续消费者继续证明，**完成
接口还需要回答“完成的是哪次工作、哪条返回、由哪个执行处理”。** 相邻缺口逐次
暴露与正常能力演进都有证据，不能把整条修改链统称为同一个 bug 被反复改坏。

## 调查范围与证据

调查窗口为 **2026-10-04 至 2026-11-04（Asia/Shanghai）**，固定代码基线为
`0f1570877be4e2ed5903377fe93955c454f43906`。核查相关 Git 历史、修复前后 diff、
接纳和收束实现、现行合同及回归源码。工作树中并行的会话／问题修改不纳入历史结论。

原历史调查没有取得生产事故日志，也没有重跑产品套件、真实模型或发布验收。以下
反例说明旧实现允许什么结果、现有回归怎样阻止它；历史测试名称是复查入口，后续
组合核对单列。关于开发方式的归因是从修改链作出的工程推断，不是每次开发过程的记录。

## 关键时间线

| 日期 / 提交 | 建立或修正的保证 | 仍不能据此推出什么 |
| --- | --- | --- |
| 10-19 [`0594d87`][c-attempt] | 按原 attempt 清理重试前的捕获文本，避免失败尝试混入后来的回复。 | 有文本不等于取得原请求已提交的成功结论。 |
| 10-21 [`fc23b96`][c-owner] | 委派改为观察原 `SubmissionReceipt`，从完成结果读取精确请求／最终 attempt 的正文；广播不再并行裁定业务完成。 | 原请求完成不证明之后的异步子结果已被处理。 |
| 10-23 [`6166de5`][c-session] | 普通委派轮终态后保留子会话；调用者可显式在同一会话发起新工作轮。 | 同一个 `delegation_id` 不等于同一个 `work_id`。 |
| 10-27 [`b19f6f9`][c-archive] | 合法新返回可恢复原归档会话，同时保留原身份、Stop 和关闭边界。 | 能恢复目标不等于输入已接纳，更不等于父执行已处理。 |
| 10-31 [`83bc697`][c-admission] → [`a2f326a`][c-migrate] | 建立回执优先的完整公共 `admit_return`，先迁 BG，再将委派终态／问题迁入。 | 共用接纳顺序不代替各来源的业务完成与消费判定。 |
| 11-03 [`a660fcc`][c-consumption] | 用精确 child work、返回输入、实际执行 carrier 和持久 terminal 证明消费；子责任登记与父轮收束协调提交。 | “没有 running child”或“父后来成功了一次”仍不足以定稿。 |
| 11-04 [`07b83bf`][c-deletion] | 区分 follow-up 尚未提交、明确拒绝、已接纳和未知；确认删除后只撤回确实未接纳的原 staged work。 | 关系变化、ACK 丢失或当前查不到活动执行，不能证明从未接纳。 |

这些改动的工程代价，是同一返回链路多轮改写、共同接口与消费者重复调整、以及
原来通过的局部验收不能回答后来的问题。代码支持提前发布、重复提交或责任滞留
等风险判断；没有证据将它们全部写成已在线上发生的事故。

## 三个完成点分别等待什么

以下以嵌套命名委派为例：上游 A 委派 P，P 再委派 B、C、D。P 是一个受委派的
工作轮；它的成功结果向 A 发布前，需要结清本轮负责的子工作返回。这不是主会话
默认等待所有 BG、Research 或普通人工输入的政策。

| 事实 | 原身份、写者与提交证据 | 应等待的对象 | 不能替代它的邻近信号 |
| --- | --- | --- | --- |
| 子任务结束 | 子 `work_id` 下原输入的 completion、实际执行结果及已提交 round 状态；SessionOwner 提交请求结果，DelegationManager 分类并提交轮次。 | 子工作的精确 receipt／轮次结论；成功正文取自该请求最终 attempt。 | 子进程退出、广播 result、出现文本、live map 清空。 |
| 返回接纳 | `source=delegation`、`agent-terminal:{child_work_id}`、目标 session；原 SessionOwner 的输入事务提交完整输入及接纳事实。 | 公共 `admit_return` 确认的原 `SubmissionReceipt`；未确认时保留原身份继续原有观察。 | 工具返回 delegation ID、恢复归档成功、来源卡片显示 completed、提交函数已经被调用。 |
| 父模型处理 | 原返回输入的 `request_id` → `execution_owner` → 已提交 `terminal_request_id`；inline／native 输入还需相应的确认送达证据。 | 该返回原 receipt 的 completion，并核对持久输入和 carrier 的关联；父轮成功发布还须满足本轮收束条件。 | 父会话 idle、任意一次父 success、队列暂时为空、通知已出现。 |

代码把“处理”限定为**对应输入由实际执行承载，并取得所需送达证明和已提交终态**。
这不证明模型在内容上认真采用了每条结果。失败 child 的错误也可以是父模型已处理
的一条完整返回；反过来，成功 child 的返回可能一直没有被父模型处理。

业务结论、返回接纳、父处理之外，通知送达和资源退休也各有 owner，不能从上表
任何一行自动推出。具体合同见[输入完成与资源退休](../implemented/architecture-contract.md#输入完成与资源退休)
及[命名委派生命周期](../implemented/agent-collaboration.md#52-sub-session-lifecycle)。

## 推翻旧假设的具体反例

### 1. 三个 child 都结束，父成功一次，仍然欠两条返回

P 发起 B、C、D；三者很快结束，返回都已排入 P。P 的发起轮结束时，没有 running
child；随后 P 处理 B 并成功结束一轮，但 C、D 仍排队。

`a660fcc^` 以活动 child 和 `_awaiting_sub_relay`／`_deferred_request_id` 判断是否
还需等待。布尔标记能挡住“派出子任务就立刻把前言回传”，却不能表达欠的是 B、C
还是 D，更不能证明哪条输入已由父执行处理。一次后续成功可能过早解除等待。

修复让父轮只保留未结清的 `_child_work_ids` 和成功 `_candidate_request_id`。
[`delegation_consumption`][s-consumption] 从持久输入查询实际 carrier 及其 terminal，
只有确实处理的 `agent-terminal:{work_id}` 才能结清对应子责任；本 work 的相关
delegation 输入都已处理，最后一个输入的 carrier 又是成功 candidate，才允许
[`_save_round`][s-seal] 提交 completed。父 success 先成为候选，不能直接成为发布许可。

同一执行可以处理多条已确认送达的输入；关联正确时，不需要为了“再综合一次”
强制增加模型调用。父失败或取消仍按原有政策结束，不把成功收束门槛扩成无限等待。

### 2. 会话可以续用，旧 work 的责任不能被新轮覆盖

B 已结束但返回还没被 P 处理；调用者又在同一个 B 会话发起 follow-up。当前记录
已经指向新 `work_id`。若 P 只按 child session 的当前状态判断，旧 work 的返回
责任就会随着记录替换而消失，或者旧 completion 错写新轮。

因此，稳定的 conversation ID 用于复用上下文，稳定的 work ID 用于原业务责任，
delivery ID 用于一次返回，request／carrier 用于输入与执行关联，attempt 用于
结果正文选择。它们回答不同的问题。父轮持有精确子 work 引用，回调捕获原 round，
新 follow-up 不能改写旧工作的历史；不能只在回调时按 session ID 找“当前记录”。

这也解释了为什么不需要另建返回账本：正文、输入、送达及 terminal 已有权威来源，
round 只保存不能从“当前 child”重建的责任引用和候选身份。

### 3. “先检查没有子任务，再定稿”仍有提交竞争

即便改成精确 child 集合，若“登记新 child”与“父轮定稿”独立提交，检查和写入
之间仍可插入新责任：父轮刚判定 ready，新 child 已接纳，父结果却已经发往 A。

[`_commit_admission`][s-admission-order] 在父轮 save lock 下，通过原 CAS 事务一起
写子 round 和父责任；父轮 seal 使用同一协调点，并在写事务内复查消费事实。
子责任先提交，父候选就继续等待；seal 先提交，迟到 setup 就被拒绝，尚未接纳的
空 child 按既有路径清理。已登记的责任不会落在父结果发布之后。

这里还必须保留成功 candidate：一个或多个 setup 都可能在接纳前被拒绝。最后一项
确实未接纳的责任撤回后，应重新判断已有 candidate 并允许直接收束，而不是丢掉
成功事实，再启动一次模型执行来补“完成信号”。

11 月 4 日的删除修复补齐同一原则的失败出口：follow-up 已暂存新 round，但还没
提交输入，原 parent／child 被确认删除；这时没有数据库行可以回滚。只有未提交或
明确拒绝、没有原工作资源、且删除已被证实时，才本地关闭**该 staged work** 并撤回
它对应的父责任。已接纳或 ACK 未知继续保有原责任；关系被替换也不等于删除。
不能用一个“CAS writer 已关闭”标记同时证明这几件事。

### 4. ACK 丢失后，等待回执不能变成重做业务

返回已经被父会话接纳，提交确认却丢失；随后 child 正文被删除、目标 Owner 不可用
或父会话归档。若重入先读正文、检查当前 Owner 或重新唤醒，就可能把既有接纳当作
失败；若直接重发，更可能制造第二次输入或效果。

[`admit_return`][s-return] 先按原 source／delivery／session 查 receipt。已有 receipt
就复用，不再准备材料或唤醒；无回执才做资格检查、纯正文准备、再次查回执、合法
恢复和最终接纳。准备不能修改来源结论，实际输入去重与 digest 冲突由 Owner／事务
裁定；来源继续拥有正文、重试分类和自己的业务结果。这样共享的是完整顺序责任，
而不是让三个调用者各自拼接“查询、恢复、提交”。

已接纳不等于父处理完成，后者继续沿原 receipt 等待。ACK 未确认也不等于未接纳：
只有明确效果前的 busy／capacity 等拒绝，且没有 unknown 前史，才按原身份重试。
unknown 在原控制对象记住；原有结算重入可以核对同一 receipt，不能因后来目标关闭
或回执仍不可读就改写为从未发生，也不新增轮询器、重放队列或重跑 child。

### 5. 重启后的最后一条 success，不能重建丢失的收束顺序

恢复若只查“本轮最后一条请求是否成功”，无法判断进程退出前还有没有新责任、
排队返回或尚未提交的 seal。聊天顺序也不是输入消费关系。

固定基线以已提交 completed round 快照作为持久 seal；未收束 running round 在重启
后标为 interrupted，不从旧布尔标记或最新 transcript 推导成功。保留子会话供查看
和显式 follow-up，不自动重放旧终态或运行 child。恢复策略保护的是已经证明的
事实，不承诺重启后自动完成原先所有工作。

## 为什么前一次修复还不够

**验收停在生产者出口。** 清掉旧 attempt 文本、观察原 completion、恢复原会话、
提交返回各自都是有效修复，但后续消费者需要的是另一事实。只测“返回到了”没有
问“哪条输入处理完了”，接口就会在下一个消费者接入时再次变化。

**用当前状态代替原责任。** running 数量、一个等待布尔值和“父又成功了一次”
都丢失了关系与顺序；复用会话后，这种表示更加不足。增加一个 parent_done 标志
无法补回已经丢掉的 child work／input／carrier 关联。

**串行 owner 被误当作跨提交点的完整保证。** 单一写者消除并行裁定，却不会自动
协调 child 责任与父 seal，更不会让取消 waiter 撤销已接纳效果。发现新提交点后，
应先修正责任模型，再决定哪个既有锁、事务或原操作承担它。

**测试等待了相邻事件。** child 已结束、coroutine 已返回、队列为空、卡片更新，
都可能早于目标事实。10 月 21 日复盘中异步退休的错误观察点，在这里表现为把输入
接纳当作处理。增加测试数量不能代替把关键交错和最终证据放进同一个验收。

## 回归应怎样取证和等待

原历史调查核对了下列回归源码。它们用可控执行／交错和持久数据库验证运行时
关系，没有证明真实模型的内容质量；该次调查未重新执行这些测试。

| 要推翻的错误假设 | 注入交错与目标观察 | 回归入口 |
| --- | --- | --- |
| child 全结束，父成功一次就够了 | 两／三个 child 先结束，每释放一次父输入执行都检查尚未发出外层返回，直到最后一条处理后只返回一次；同时覆盖复用同一 child。 | [test_delegation_consumption.py](../../tests/test_delegation_consumption.py)：`test_fast_owned_replies_wait_for_each_exact_parent_input` |
| 一次成功 terminal 可以覆盖同 carrier 的任意输入 | 多条返回绑定同一执行，逐条确认送达；缺确认时不结清，全部确认后还要候选身份匹配。 | [test_delegation_state.py](../../tests/test_delegation_state.py)：`test_one_execution_consumes_multiple_owned_returns_only_after_confirmed_delivery` |
| child 登记和 seal 各自正确就够了 | 分别让 child 提交或父 seal 先发生，检查等待与明确拒绝；一／多个 setup 被拒绝后重查原成功 candidate。 | [test_delegation_consumption.py](../../tests/test_delegation_consumption.py)：`test_child_admission_and_parent_seal_have_one_order`、`test_withdrawing_unadmitted_child_rechecks_the_already_successful_parent` |
| ACK 丢失或材料删除允许再提交 | 真正先接纳再丢 ACK，并让 Owner 失败；之后材料准备必须不被调用，仍返回相同 request ID，输入／执行只有一次。 | [test_return_admission.py](../../tests/test_return_admission.py)：`test_lost_ack_then_owner_failure_observes_original_input_without_preparing_again` |
| 取消等待者就撤销输入 | 在真实接纳后取消交接 waiter，确认原输入仍由 Owner 完成；并发准备只提交一条输入，正文冲突保留原异常。 | [test_return_admission.py](../../tests/test_return_admission.py)：`test_cancelling_the_handoff_waiter_does_not_revoke_admitted_work` 等 |
| 删除关系就能退休全部暂存责任 | 在 CAS 后、接纳前／后以及丢 ACK 时删除 parent／child；只撤回精确未接纳 work，accepted／unknown 仍阻塞排空，关系替换不能冒充删除。 | [test_delegation_restart_deletion.py](../../tests/test_delegation_restart_deletion.py) |
| transcript 最后一条成功就是工作轮成功 | 只写看似相关的历史消息不能证明消费；发起轮成功但嵌套返回未处理时，重启分类为 interrupted，不重放旧回复。 | [test_delegation_state.py](../../tests/test_delegation_state.py)：`test_transcript_order_cannot_prove_return_consumption`；[test_delegation_restart_nested.py](../../tests/test_delegation_restart_nested.py)：`test_nested_first_turn_success_is_interrupted_when_its_relay_never_finished` |

测试应先选择最能推翻旧设计的情景：**让所有 child 比父发起轮更早结束，再只放行
一个父返回执行。** 若此时已经向上游发出最终结果，单次子任务、顺序返回和大量
绿色测试都不能挽救这个完成定义。取证必须落到原输入和提交事实；测试里的有限
超时只负责暴露未完成，不能把等待超时解释成业务未发生。

## 后续核对：准确结论不代替返回消费

后续完成分类整改收窄的是原请求的业务判断，不改变本文的三个完成点。
[组合回归](../../tests/test_mcp_input_completion.py)中的
`test_exact_input_gap_keeps_named_delegation_original_result_consumption`
通过原 mounted MCP 取消与受控原生记录，验证准确输入缺项可保留合法成功，最终
仍等原 `agent-terminal:{work_id}` 返回输入提交。child 的成功与父接住通知不能合并；
父工作轮还须按上文持久输入、carrier 和 terminal 关系结清子责任。

共享规则也没有把原生 helper 改成独立命名委派。另一个关联 child 存在 connector
写入缺项时，不因 root 的输入调用已清理而放行；原始缺项诊断继续可查。
这项核对验证原运行时关系，不证明父模型已在语义上正确采用全部返回内容。

## 当前边界与可迁移的开发经验

固定基线仍有一处可以直接确认的指导偏差：实际
[`_compose_child_request`][s-prompt] 称 single-turn delegation，并说一轮结束会把
everything you have written 回传；[实际正文选择][s-body] 在有 `final_answer` 时只取
选中的最终答案，嵌套工作还受上述返回消费与收束约束。这是提示与实现的差异，
没有证据证明它已造成某次生产失败。以后修正这项指导时，应核对真正送入模型的
提示及普通委派、嵌套工作、commentary／final 分离的代表性行为；只改文档快照或
断言新字符串，不能证明模型行为已改善。本次仅记录这项边界，不修改运行时提示。

命名委派与原生 subagent 也不能混用证明：本文的 work／receipt／round seal 属于
命名委派；原生父轮 `turn.completed`、工具 wait 返回和 helper 生命周期遵循各自
[原生子任务合同](../implemented/native-subagents.md)。独立 BG 不自动成为父 work
必须消费的子责任，人工积压也不在这项成功收束保证内；本经验不引入自动合批或 steer。

后续开发可以把此次经验落实为以下动作：

1. **先写消费者需要的证据。** 修改异步返回时，列出原工作结论、返回接纳、处理、
   通知和退休各自的身份、写者、提交点与等待者。只涉及其中一项的局部修复按实际
   范围处理；跨项时补齐交接，不因共用 done 字样而扩大保证。
2. **沿原输入建立处理关系。** 从已有输入、送达和 terminal 推导可重建的事实；
   仅持久保存无法从当前对象重建的原责任。新增状态前先说明它填补了哪项证据缺口。
3. **同时验证正常收束和责任撤回。** 检查 child／seal 两种提交顺序，以及明确未
   接纳、已接纳、unknown 和关系删除。回滚资格取决于原操作阶段，不取决于异常
   名称或当前对象是否还在。
4. **共享完整操作，再迁移来源。** 用真实返回消费者验证回执优先、材料准备、恢复
   和输入提交的共同接口；先选能暴露不同需求的消费者，验证共同事实与异常出口，
   再迁移其余调用者。“接口先冻结”不要求脱离真实调用者预先设计抽象，也不妨碍
   新证据修正语义。来源保留业务结论与重试政策，迁移时删除旧并行编排路径。
5. **交付时报告证据上限。** “子任务完成”“返回已接纳”“父执行已处理”“审查意见
   已被读完并处置”分别说明。后三者不能由前一项的状态替代；质量判断还需阅读
   返回正文并核对结论，不能从 completed 元数据自动产生。

现行行为继续由[委派合同](../implemented/agent-collaboration.md)和
[公共返回接管](../implemented/architecture-contract.md#公共返回接管)维护；本篇保留
反例、修复理由和回归观察点，不另建第二份运行时合同。

[c-attempt]: https://github.com/Danuvius152/Octopus/commit/0594d873dabc0a90e7208680c5b971001d10d83f
[c-owner]: https://github.com/Danuvius152/Octopus/commit/fc23b9687cdf5d04b41ba7b29ac8c3d4466edf45
[c-session]: https://github.com/Danuvius152/Octopus/commit/6166de570c2d74543b294f5c98e7e10bf6f54b49
[c-archive]: https://github.com/Danuvius152/Octopus/commit/b19f6f9deaa36245d80ed3c283d6e64dc257d978
[c-admission]: https://github.com/Danuvius152/Octopus/commit/83bc6971a5f0a824439c1ef5eb761aac177851f7
[c-migrate]: https://github.com/Danuvius152/Octopus/commit/a2f326a6b8f667c346c0580f24e7025fbc7f7b55
[c-consumption]: https://github.com/Danuvius152/Octopus/commit/a660fccf3a1298b5da89079195301454363bb374
[c-deletion]: https://github.com/Danuvius152/Octopus/commit/07b83bf2fed2e1d88ca1e782eafe384e86988355
[s-consumption]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/db/sessions.py#L40-L66
[s-seal]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/delegations.py#L437-L510
[s-admission-order]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/delegations.py#L400-L450
[s-return]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/sessions/turns.py#L431-L516
[s-prompt]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/delegations.py#L1240-L1272
[s-body]: https://github.com/Danuvius152/Octopus/blob/0f1570877be4e2ed5903377fe93955c454f43906/server/delegations.py#L1629-L1649

# 子任务都已结束，父委派仍可能提前发布结果（2026-11-04）

状态：下列返回消费与收束缺口已有修复。历史调查窗口为
2026-10-04 至 2026-11-04（Asia/Shanghai），固定基线为
`0f1570877be4e2ed5903377fe93955c454f43906`；提示规则另按当前代码核对。

## 摘要

上游 A 委派 P，P 再派 B、C、D。三个 child 都已结束，P 处理 B 后成功了一次，
C、D 的返回却仍在队列里。旧实现按活动 child 数和等待布尔值判断收束，可能在
必要返回尚未处理时，已经把 P 的成功结果发给 A。

修复沿原 child work、返回输入和实际父执行建立消费关系，成功先作为候选，全部
本轮责任结清后才发布。子任务结束、返回接纳和父执行处理分别取证；“处理”仍只
表示运行时关联与终态，不认证模型是否正确采用了材料。

## 影响与证据

旧实现和回归构造支持提前发布、重复提交、旧轮责任丢失或暂存责任滞留的后果判断。
未取得将这些情况全部确认成线上事故的日志；可直接确认的是返回链路多轮修改及
接口、消费者的重复调整。

证据来自历史差异、接纳/收束源码及测试。本次文档改写没有重跑产品套件、真实
模型或发布验收。以下命名委派关系也不等同于原生 subagent 生命周期。

## 关键时间线

| 日期 / 提交 | 修正 |
| --- | --- |
| 10-19 `0594d87`、10-21 `fc23b96` | 文本按原 attempt 选择，业务结论观察原 SubmissionReceipt，不再由广播并行裁定。 |
| 10-23 `6166de5`、10-27 `b19f6f9` | 子会话可续用，合法新返回可恢复原归档会话；work 身份仍独立。 |
| 10-31 `83bc697`、`a2f326a` | 完整回执优先的 admit_return 先用于 BG，再接管委派终态和问题返回。 |
| 11-03 `a660fcc` | 按 child work、原返回输入、实际执行和持久 terminal 证明消费，协调 child 登记与父轮定稿。 |
| 11-04 `07b83bf` | 删除后只撤回确实未接纳的原 staged work，accepted/unknown 责任继续保留。 |

## 根因与修复

### 1. “没有 running child”丢失了尚未处理的返回

`a660fcc^` 用活动 child 与 `_awaiting_sub_relay`、`_deferred_request_id` 判断
等待。它能挡住派出子任务便回传前言，却无法区分欠 B、C 还是 D。一条后续成功
可能清掉全部等待。

修复保留未结清的 `_child_work_ids` 和成功 `_candidate_request_id`。从持久输入
沿 `agent-terminal:{child_work_id}` 找到 request、execution_owner 和已提交 terminal；
inline/native 输入还需相应送达确认。只有本 work 的必要返回都已处理，最后输入的
实际执行又匹配成功候选，才能提交 completed round。

同一执行可处理多条已确认送达输入，不强制再加一次综合调用。失败 child 的错误
也能构成已处理返回；父失败或取消按既有政策结束，不把成功门槛扩成无限等待。

### 2. 会话复用不能覆盖旧 work 的责任

B 已结束但返回未处理，调用者又在同一子会话发起 follow-up。若 P 查的是 child
当前记录，旧返回责任会随新 work 消失，或旧 completion 写到新轮。

conversation ID 用来续用上下文，work ID 识别原业务，delivery ID 识别返回，
request/carrier 识别输入处理，attempt 选择正文。父轮保存精确子 work 引用，回调
捕获原 round。已有输入、送达和 terminal 是消费事实的来源，不另建平行返回账本。

### 3. 检查完成与登记新责任之间仍有竞争

若 child 登记和父轮 seal 分开提交，刚检查没有未决责任后就可能插入新 child，
父结果却已经发布。修复在父 save lock 下，通过原 CAS 事务一起写 child round 和
父责任；seal 使用同一协调点，并在事务内重查消费。

child 先提交则父继续等；seal 先提交则迟到 setup 明确拒绝，清理未接纳空 child。
若一个或多个 setup 都被拒绝，最后一项责任撤回后重查已有成功候选，允许直接收束，
不丢弃成功事实再调用模型补信号。

删除也不能一概回滚：只有确实未提交或明确拒绝、无原工作资源且删除已确认，才
关闭对应 staged work 并撤回它的责任。accepted 或 ACK unknown 继续保有；关系
替换不证明删除，CAS writer 关闭也不能证明输入从未接纳。

### 4. ACK 丢失后的观察被误当成重做业务

返回已接纳、确认丢失后，正文可能被删除，Owner 可能失败，目标也可能归档。
重入若先读正文或重新提交，就可能把原成功接纳误判成失败，甚至多造一条输入。

[admit_return](../../server/sessions/turns.py)先按原 source/delivery/session 查 receipt。
已有回执就复用；没有才检查资格、准备纯正文、再查回执、合法恢复和接纳。正文
准备不修改来源结论，去重和 digest 冲突由 Owner/事务裁定，来源保留自己的业务
结论及重试分类。

只有明确效果前的 busy/capacity 等拒绝、且无 unknown 前史，才按原身份重试。
unknown 只继续核原 receipt，不能因后来目标关闭或仍查不到回执就改成未发生；
不重跑 child。接纳之后，父处理继续沿该 receipt 等待。

### 5. transcript 最后一条 success 不能重建 seal

重启时，最后成功请求不能说明此前有没有排队返回、新责任或未提交定稿。恢复以
已提交 completed round 快照为 seal，未收束 running round 标 interrupted，保留
会话供查看和显式 follow-up，不自动重放旧结果或运行 child。

## 为什么测试漏检

单 child、顺序返回、队列为空或父后来成功一次，都避开了最小反例：让多个 child
先于父发起轮结束，再只放行一条父返回执行。若此时已向 A 发布结果，完成定义就
已经失败。单一 writer 也不能替代 child 登记与 seal 的跨提交协调。

| 交错与目标观察 | 回归入口 |
| --- | --- |
| 快速多个 child、同一会话多 work，每处理一条都查外层返回，直到最后只发布一次 | [消费](../../tests/test_delegation_consumption.py)：`test_fast_owned_replies_wait_for_each_exact_parent_input` |
| 同一执行承载多条返回，逐条确认送达；transcript 顺序不能替代消费关系 | [状态](../../tests/test_delegation_state.py) |
| child 与 seal 两种提交顺序，setup 拒绝后重查成功候选 | [消费](../../tests/test_delegation_consumption.py)：`test_child_admission_and_parent_seal_have_one_order` |
| 真接纳后丢 ACK、删材料或取消 waiter，仍复用原 request 且只执行一次 | [返回接纳](../../tests/test_return_admission.py) |
| 删除前后区分未接纳、accepted、unknown 与关系替换 | [删除恢复](../../tests/test_delegation_restart_deletion.py) |
| 发起轮成功、嵌套返回未处理时重启，必须 interrupted 且不重放 | [嵌套重启](../../tests/test_delegation_restart_nested.py) |

[组合回归](../../tests/test_mcp_input_completion.py)另验证精确纯输入缺项可保留合法
原请求成功，命名委派仍需提交原返回并结清消费；其他关联写入缺项不会随之放行。
运行时成功与模型内容质量仍分别验收。

## 防护、教训与当前边界

当前首轮和 follow-up 提示共用 [`_DELEGATION_PROTOCOL`](../../server/delegations.py)，
要求收齐必要子结果，按实际工作完成返回。有效 final-answer 引用只选择最终答案；
无引用按既有片段策略，显式空答案不借其他文字补齐，无效引用不借用旁支内容。
commentary 和工具输出不保证传给调用者。该规则已与实际正文选择对齐；字符串
一致和确定性回归仍不证明模型一定遵循。

- 异步结果沿原 work、input、carrier 和 terminal 证明处理，当前列表状态不能代替。
- 同时验证正常收束、未接纳责任撤回和未知效果保留，不能只测试成功发布。
- 共享完整返回接管，来源继续拥有业务结论；迁移时删除旧的重复编排。
- 审查结果到达仍需读取并处置正文，completed 元数据不能自动产生质量判断。

该成功门槛属于受委派 work 的必要子责任，不扩成主会话等待所有 BG、Research
或人工输入，也不新增自动合批或 steer。原生 helper 见[原生子任务](../implemented/native-subagents.md)；
现行命名委派与返回合同见[委派](../implemented/agent-collaboration.md)和
[公共返回接管](../implemented/input-reliability.md#公共返回接管)。

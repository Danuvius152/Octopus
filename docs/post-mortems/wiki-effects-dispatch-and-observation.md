# Wiki 重派后，旧聊天的卡片停在旧进度（2026-11-04）

状态：下列效果、派发和观察缺口已有修复。调查窗口为
2026-10-04 至 2026-11-04（Asia/Shanghai），固定历史基线为
`0f1570877be4e2ed5903377fe93955c454f43906`。

## 摘要

A 聊天提交 Wiki 任务，用户从 B 重派后，后续自动汇报应转给 B，A 中的卡片却仍需
允许用户查看当前进度。旧实现把自动返回资格、持久通知摘要和人的当前观察混在
一起：限制旧订阅者副作用后，也可能让旧卡片永远停在原 attempt。

修复把执行 attempt、派发代次与只读观察分开。读取当前状态不重新订阅或唤醒旧
模型；效果恢复也沿原收据核对，不为补通知重新生成文章。

## 影响与证据

旧代码与回归构造显示，用户可能看不到重派后的当前进度；ACK 丢失又可能使一组
结果重复汇报。更早的结算缺口还允许把文件存在、取消状态或模型退出当成原执行
成功，恢复时可能重复生成或覆盖后来人工修改。

证据来自 Git 差异、固定版本实现和回归源码，未取得全部故障发生于生产的记录。
本次文档改写没有运行产品套件、真实模型或发布；下列测试是机制与观察点的入口。

## 关键时间线

| 日期 / 提交 | 修正的事实 |
| --- | --- |
| 10-17 `8132a07`、10-23 `93122cb` | 保留原录制引用，分开核原 request terminal、recording、资源 retired 和文件效果。 |
| 10-25 `494fa95` → 10-27 `cf98ecb` | 增加结果自动返回后，以 dispatch_generation 表达显式重派，与返回接纳协调排序。 |
| 10-31 `159723d` → 11-04 `4f1418d` | 原资源按证明退役；旧聊天通过持久关联和当前权限读取当前快照。 |
| `65b5918`、`26aa78f` | 入队提交后丢失唤醒，queued 重放继续推进；running 重放不再唤醒。 |

新增自动返回是产品扩展；后来的派发与观察修复补齐其消费者，不能仅按提交顺序
认定为同一个 bug 重现。

## 根因与修复

### 1. 自动返回接收者不能决定谁能查看当前状态

复现顺序是 A 留下持久卡片、B 显式 retry/readmit、新 attempt 推进、全部通知 ACK，
再从 A 的真实 HTTP 入口读取卡片。最后通知不再待投递，但任务仍存在；靠通知恢复
只能得到旧摘要，重新订阅又会给旧聊天制造副作用。

修复通过原账户/session/vault/job 关联发现任务，以当前身份、注册和权限读取 Wiki
事务快照。A 仍能取得新的 attempt、state_version 和 status，保留 origin_request_id，
但 GET 不创建订阅、通知或模型输入。换库、任务缺失或无当前权限时只保留原获准
摘要，标明 current_state unavailable，不披露新诊断或开放当前控制。

[重派回传](../../tests/test_wiki_retry_return.py)验证只给当前派发者返回；
[卡片刷新](../../tests/test_wiki_card_refresh.py)在全部 ACK 后读取旧聊天，并覆盖
换库、缺任务与撤权。两边必须同时成立，不能只测 B 收到了结果。

### 2. 返回已接纳，ACK 丢失不能制造第二份汇报

一组 job 的返回接纳后，ACK 抛错；此时再转交其中一项并重送剩余通知，组成员和
attempt 已变化。若重新拼去重身份，就会提交第二份汇报。

修复保留原 delivery 身份，只补通知。重派与返回接纳共享明确顺序；已接纳输入
不被后来的转交撤回。旧 control 响应丢失后的重放，也不订阅之后的新派发。

[回传回归](../../tests/test_wiki_retry_return.py)用
`test_lost_group_ack_and_member_transfer_never_create_a_second_report` 和
`test_return_acceptance_and_retry_have_one_admission_order` 注入这两个窗口，
检查主库原输入只有一份。

### 3. 原效果未知时，恢复没有重新生成的依据

在 replace 前、或 replace 后 applied 标记前中断，再人工编辑或还原字节，当前
文件都无法证明历史有没有写过。候选和收据保留不确定性；候选过期也不撤销收据，
更不授权再生成。已保存但索引失败只修派生索引，录制未知则查原 session/request
持久终态；取消状态不允许新 attempt 与尚未退休的原 work 并行写。

[jobs.py](../../server/wiki/jobs.py)以同一结算规则检查原效果、applied、输出身份、
revision、索引与原请求成功终态。[任务恢复](../../tests/test_wiki_jobs.py)分别覆盖
未知发布、已发布输出和录制失败，观察模型轮数仍为一、publication 不重复、人工
字节保留及 attention；[退役准入](../../tests/test_wiki_retirement_admission.py)
验证原资源不能因 live map 缺失就被视为退出。

原生根请求有合法成功终态也不能代替 Wiki 效果。[组合回归](../../tests/test_mcp_input_completion.py)
的 `test_native_input_gap_does_not_replace_wiki_effect_receipt` 在纯输入缺项已本地
清理、但没有 publication receipt 时，仍保留 item attention、原文不变和一次模型执行。
它验证效果门槛，没有证明实际发布或终稿语义。

### 4. 幂等复用成功，还需要推进原 queued 工作

准入事务已提交、worker.wake 前失败，再提交同一操作。只返回相同 job 能防重复，
若跳过唤醒，原任务仍不前进。修复依据 queued 状态补唤醒，移除多余 created 镜像；
running 不重复唤醒。

[操作边界回归](../../tests/test_wiki_operation_boundaries.py)覆盖单项和批量入口，
检查身份不变、一条任务、queued 重放唤醒及 running 重放无额外动作。恢复推进和
不重做内容并不冲突，选择取决于原阶段和已确认效果。

## 为什么前一次修复还不够

先前测试分别证明了任务结算、B 收到返回或卡片存在，没有把“A 无新副作用，但在
所有 ACK 后仍可读取当前 attempt”放在同一对照里。文件存在、通知 ACK、进程退出
和用户可读性各属不同消费者，不能用其中一个替代其余事实。

原执行成功、效果结算、资源退休、返回接纳、模型处理与人的观察继续分别取证。
通知 receipt/ACK 不证明模型采用内容；当前快照也不授予写权。相关实现入口为
[执行与退休](../../server/wiki/job_execution.py)、
[返回派发](../../server/wiki/job_returns.py)和[当前观察](../../server/wiki/job_observation.py)。

## 教训与适用边界

- 限制副作用时，同时验收合法只读观察；不能靠删除历史关系解决旧订阅者问题。
- 恢复先确认原效果和阶段：通知丢失补通知，queued 丢失唤醒补推进，未知发布保留核验。
- 观察、执行和派发分别使用原身份，历史关联仍须通过当前授权。

completed 不认证终稿冷读覆盖或语义理解，见
[冷读保证边界](wiki-evidence-scope-recovery.md#终稿冷读是流程义务不是-completed-的程序硬门槛)。
现行规则由[Wiki 合同](../implemented/wiki-development-summary.md)与
[前端卡片合同](../implemented/frontend-session-observation.md#wiki-卡片详情与控制回执)
维护；权限、来源与阅读证据的其他因果见[证据复盘](wiki-evidence-scope-recovery.md)。

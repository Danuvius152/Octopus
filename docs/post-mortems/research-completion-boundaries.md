# Research 把局部完成信号当成了整条任务完成（2026-11-04）

状态：下列完成与交接缺口已有修复，内容充分性等边界保留。历史调查窗口为
2026-10-04 至 2026-11-04（Asia/Shanghai），基线为
`07b83bf2fed2e1d88ca1e782eafe384e86988355`；后续组合证据另列。

## 摘要

Research 曾接受“有 JSON 但没有成功 terminal”的 leaf，把未被反驳的主张称为
verified；报告流程也曾用占位正文掩盖合成失败。异步链路继续把报告完成、原会话
接纳、模型处理、取消落库和浏览器创建确认混用，局部成功因此掩盖了下游未完成。

修复分别定义协议成功、报告提交、返回处理与原资源退休，原操作负责到真实交接。
执行成功、产物保存、证据充分和用户问题解决仍分别判断：全部 search 失败后，
当前流程可以合成说明材料不足的正文，不能据此宣称研究已有充分证据。

## 影响与证据

旧代码和反例支持这些后果：失败文本进入 findings/votes，空报告被标 completed，
已取消任务继续执行，主会话重复接纳汇报，排队任务换用新默认配置，以及服务器
创建成功但界面没有卡片。它们未全部取得生产事故日志；可确认的工程代价是同一
链路多轮修改、相邻消费者与恢复路径反复验证。

依据为 Git 差异、实现、合同与回归源码。本次文档改写未运行产品套件、模型或
发布验收；历史测试链接说明反例构造，不能作为本轮新增通过结果。

## 关键时间线

| 日期 / 提交 | 修正 |
| --- | --- |
| 10-14 `7449f12` | 启动先登记 owner；waiter 退出不撤销流水线，fan-out 等兄弟 leaf 清理。 |
| 10-19 `203f7f3`、`0594d87` | 保留原请求合同；核验要求正向多数，返回处理与浏览器版本另有回执。 |
| 10-25 `fe2200f` → 10-27 `b19f6f9` | 拒绝失败/空合成，要求 leaf 成功 terminal，报告同步后竞争唯一终态。 |
| 10-28 `bbadc28` → 10-31 `7827fff` | 保有 delivery observer，分类拒绝与未知 ACK，返回使用公共 admit_return。 |
| 11-02 `fd7ab9c`、11-03 `983ff39`、`2358819` | 补配置捕获、旧返回恢复查询与首次建代后的原创建确认。 |
| 11-04 `4f1418d` | 取消控制保有事务到原 pipeline 信号，shutdown 等待控制提交。 |

这些修改补齐不同消费者的事实；配置、查询范围和测试交错也各有遗漏，不能只改
状态名，或将整条修改链一概称为同一个 bug 重现。

## 根因与修复

### 1. 有输出、没有反证，都不足以构成有效证据

`0594d87^` 把不可解析 verdict 默认成未反驳，survivors 又统称 verified。超时、
格式错误和未找到反例因此可能进入已核验叙述。修复要求显式正向支持达到配置票数
的严格多数；失败票不缩分母，有效但支持不足是 unverified，没有有效 verdict 是 failed。

协议层还有独立缺口：`b19f6f9^` 的 web leaf 收到 JSON 后，EOF 即可无错误返回；
search 编排也未拒绝带 error 的非空文本。完整 JSON 后断流、零退出但无成功 terminal
都可能贡献 findings，部分失败核验也可能投票。修复同时要求 leaf 协议成功与编排
排除失败结果，只改 parser 或 prompt 不够。

[leaf 回归](../../tests/test_research_leaf.py)以合成子进程构造 EOF、错误退出和成功
terminal；[编排回归](../../tests/test_research_orchestrator.py)检查可解析失败文本
不进 findings，失败核验不计正向票，少数成功不能因失败票胜出。协议成功仍不证明
URL 已读或来源独立，多次投票和 verified 也不是事实真理认证。

### 2. 报告文本、持久产物与任务终态，需要各自的提交依据

`fe2200f^` 用 `(the model produced no report)` 填补空合成，LeafResult.error
也可能继续形成 ResearchReport。修复让合成错误或空输出 failed，保留材料与原因。
全部 search 失败但合成仍产出正文是另一情况，只能说明流程产出了报告。

`b19f6f9^` 还允许 best-effort 写盘返回空路径后 completed，完成与取消分别更新。
修复在报告文件和目录同步后，以 `WHERE status='running'` 的事务竞争唯一终态。
cancelled、failed、interrupted 共用该入口；先提交的业务结果保留。文件和 SQLite
不构成跨资源原子事务，可能有文件而没有 completed，不能反向猜测成功。

诊断材料也可能写失败：合成已失败，若在终态前建目录、打开或序列化异常，job
会继续 running。材料边界现在处理自身保存失败，原合成失败仍走终态事务；用户
取得原错误和经掩码的材料失败原因，完整栈留日志。DB、registry 与实际退役异常
仍沿原责任传播，不能一起降格成诊断不可用。

[manager 回归](../../tests/test_research_manager.py)注入 fsync 失败、取消先赢和完成
先赢；[执行回归](../../tests/test_review_execution_regressions.py)保留失败合成的
材料。同步故障测试不等于断电实验。完成后到达的取消不能撤销既有报告与待交付；
失败合成材料也不冒充报告。

### 3. 报告完成、输入接纳、主会话处理与浏览器收讫不能共用“送达”

主会话忙碌或 Stop 暂停时，报告可以 completed，返回仍排队。`injection_status=delivered`
原来只表示接纳，不能结束所有消费者。修复沿原 RequestCompletion，只有已提交
success 才记 processed；记录未知、结果不可用与明确失败各自保留，通知失败不
抹去已提交处理成功。

pipeline 交出的 delivery observer 继续由 Research 保有，shutdown 取快照后新增
的原 observer 也要 join，清理失败不能只写日志就从 drain 消失。浏览器 /seen 则
确认匹配 job 的 request ID 与 receipt version；queued v1 不代表 processed v2
已展示，迟到 v1 不能覆盖 v2，无人开页面也不妨碍自然排空。

[manager 测试](../../tests/test_research_manager.py)覆盖精确版本、无人观察时排空
和晚交 observer；[ResearchCard](../../web/src/components/ResearchCard.test.tsx)及
[会话读取](../../web/src/lib/sessionResearch.test.tsx)验证 UI 回执。processed 不认证
研究质量，界面收讫也不证明人已阅读或理解。

### 4. 取消事务成功后，停止原执行仍有独立责任

`4f1418d^` 的 cancel waiter 先等 confirm_research_outcome，再按 job key 找 task。
在真实 UPDATE/COMMIT 期间取消 waiter，会留下 cancelled 行但不停止 pipeline；
再次取消又因没赢终态而不发信号。启动 owner 和终态 CAS 都没覆盖这次控制操作。

修复在首次 DB 等待前捕获原 pipeline，登记独立 control 并 shield waiter。权威行
已 cancelled 时补必要且至多一次的停止信号，不按 key 取消后来的替代 task。
shutdown join control，仅取消 pipeline/observer，保全控制事务；两类登记服务于
不同取消语义，没有增加第二份业务状态。

[取消回归](../../tests/test_research_manager.py)在 UPDATE、COMMIT 返回、重复取消
和 shutdown 中制造窗口，检查原任务信号；同文件的合成 CLI 用例核实际进程退休。
cancelled 是业务决定，清理超时仍保留原任务和 blocker，之后 join 同一清理，
不能因 map 移除或第一次超时就宣称资源已退出，也不撤销既有外部效果。

### 5. 创建成功之后，浏览器仍可能没有可信的当前状态

`2358819^` 在 POST 前捕获服务代次。详情先把 null 建为 G 后，201 因观察失效
被丢弃；若旧 GET 为空且 started 丢失，服务器有任务、界面无卡片。无条件采用
迟到 201 又可能覆盖已观察到的 completed 或污染新视图。

修复仅在原账户/token、连接、会话和视图仍有效，且只经历首次 null→G 时，以
当前 G 新读一次快照核原 job ID，POST 仍一次。确认失败或缺 job 保留“已创建、
状态待确认”，不重发创建；原 POST 无有效回执则是创建结果未确认。正常有效观察
中，创建回执只补身份/元数据，动态状态按现有合并规则。

[会话测试](../../web/src/lib/sessionResearch.test.tsx)与[离线浏览器](../../web/e2e/research.spec.ts)
控制详情、201 和 GET 次序，核一次 POST、原 job 卡片和确认读取。旧夹具预设
generation=service，绕过了该窗口。浏览器用例模拟 HTTP，不证明真实模型执行。
null→G→H、跨账户/连接、归档/删除或离开视图均不适用首次确认；/seen 是另一事实。

### 6. 捕获选择不等于冻结权限，原会话暂不可见也不等于返回身份失效

只存 session 配置，模型和 Agent 默认凭据仍在拿到运行槽后读取，会让排队 job
换用新选择。`fd7ab9c` 在接纳前解析有效模型与 credential ID，耗时工作在锁外；
配置等待后再检查目标和 shutdown。快照保留选择，不冻结授权：实际 leaf spawn
仍核当前账户、凭据和 Agent，不可用就结束，不能回退另一身份。

合法新返回按原 account/agent/work/receipt 恢复归档会话并保留 Stop；归档撤销的
queued 输入不重放，删除、撤权、关闭原 work 或缺合同仍拒绝。Research 使用公共
接管入口，session 负责接纳、恢复与顺序，Research 保有报告和处理观察。

[配置回归](../../tests/test_research_manager.py)、[API 回归](../../tests/test_research_api.py)
检查排队选择、等待后关闭与撤权；[真实会话返回](../../tests/test_research_session_return.py)
保留真实 SessionManager/SQLite，用受控报告生成器隔离模型。

### 7. 没有收到回执，不足以证明没有效果；恢复也不能顺便重做工作

广泛捕获异常后重试，混淆明确未接纳与已提交但 ACK 丢失。修复只对明确容量/忙碌
拒绝延后接纳，永久完整性拒绝保留报告并停止，unknown 沿 `research:{job_id}`
查原回执；任意 ValueError 或程序错误不是重试分类。

启动查询也补齐 completed/pending 无 receipt、unknown 和 receipt 已终态但旧投影
未同步的存量。按原持久输入重建，receipt/admission 同事务修复；不创建 Owner、
唤醒会话或再读已不需要的报告。查无记录仅在原 pending、调用身份受支持且匹配、
权威读取健康、旧 writer 退休已证实时，才可记未接纳失败；原 unknown 不因缺席
改成未发生。报告保持 completed，可独立下载。

[真实返回](../../tests/test_research_session_return.py)、
[共享接纳](../../tests/test_research_return_admission.py)与
[manager 恢复](../../tests/test_research_manager.py)覆盖丢 ACK、材料已删、永久拒绝、
旧分裂投影及读取失败。[下载 API](../../tests/test_research_api.py)检查账户隔离。
这些是持久记录恢复测试，不是整机崩溃实验；running job 启动标 interrupted，不从
中间阶段续跑流水线。存储失信仍按 fail-stop，不能改成普通 delivery failed。

## 为什么测试没有先拦住

旧 leaf 错误 fake 返回空文本，没有测试 error 携带可解析 JSON；前端夹具提前建代，
没经过 null→G；取消终态竞争也没在真实 UPDATE/COMMIT 中取消 cancel waiter。
这些邻近正例不能证明目标交接。

回归按事实选层：编排测计票和失败过滤，合成进程测 terminal/退出，真实 DB 与
SessionManager 测提交恢复，deferred HTTP/浏览器测观察资格。把关键责任 mock
成成功，增加整套测试条数仍可能漏同一窗口。

[原生输入组合回归](../../tests/test_mcp_input_completion.py)另证明报告与返回处理
独立：精确纯输入缺项已清理时返回可成功，加入未知写入后处理失败；两边都保留
原报告、completed 及各一次研究/返回，不为补回执重生成报告。这不替代 leaf 或
真实研究内容验收，长人工等待与无人值守缺决定仍按原工作政策区分。

## 教训与适用边界

- 完成定义从消费者需要的事实出发，跨组件时列原 job/work/request、writer、
  提交点、接纳/处理、观察与退休，不以有文本或 done 验收。
- 原选择与当前授权分开；等待后在实际接纳、spawn 和写回边界检查。
- 先分清重试工作还是重试观察，unknown 不靠默认值、重跑或更宽的成功来消除。
- 新控制和 observer 都需明确保有责任；共享接管顺序，保留各领域效果与取消差异。

Finding 的 claim/url/angle/verification 不构成完整问点覆盖、反证理由与来源独立性
交付，改进归[研究工作流计划](../plans/ai-research-workflow.md)。web leaf 不继承
MCP/connectors/memory，Codex 独立 exec 与只读 sandbox 也不等于严格 web-only；
prompt 的 ONLY 不是强制边界，tool-free 推理 leaf 另验。

当前合同见[Research](../implemented/native-deep-research.md)、
[前端创建与确认](../implemented/frontend-session-observation.md#research-创建观察与浏览器确认)
和[公共返回接管](../implemented/input-reliability.md#公共返回接管)。
当前不承诺任意阶段续跑、跨 job 缓存或浏览器全局最新；验证选择由[测试政策](../testing.md)维护。

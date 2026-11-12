# 后台任务：命令结果、返回与资源退休

> **Status:** implemented — 独立命令身份、持久接纳、有界输出、超时取消及原来源返回。

`BgTaskManager` 拥有独立 shell 工作，父模型请求可以先结束。任务 ID、所属账户／
会话和原 work、命令、输出、业务状态及返回诊断保持各自含义。后台命令完成不
表示父模型已处理结果；返回接纳和资源退出也分别取证。

## 接纳与启动

`POST /api/sessions/{session_id}/bg-tasks` 先验证当前账户、live session、工具
scope、原 work 与[可信知识请求](knowledge-requests.md)，不借用会话最近一次请求。
manager 在任何 OS 命令执行前登记原启动责任并提交任务行；存储失败或提交未知
不继续 spawn。spawn 前及迟到句柄交接仍检查关停和原 work 资格。取消启动等待者
不能遗失迟到的进程；原启动 owner 继续补偿、登记或清理。

启动方把原补偿 task 或 running 责任登记好后，同步撤销 caller 的取消资格并完成
有限的 handoff Future。原 work／session 退休即使在交接前捕获 reservation，也只
等待这个交接事实，再 join 原 cleanup／running；不等待 caller 后续无关工作，
更不能在同一 caller 内等待自身。等待者取消不能取消 handoff，交接完成也不能
代替进程、终态存储或返回责任完成。

命令在捕获的会话工作目录中通过 shell 运行，进程使用独立 process group。
返回 task ID 时正常路径已经取得进程句柄；调用者之后读取任务记录观察结果，
不等待父回合保持活动，也不把命令作为新的普通会话请求重放。

## 输出与时限

stdout、stderr 并发读取，各保留最多 200 KiB 的尾部，超限丢弃最早字节并在
最终文本插入截断标记，`truncated` 显式告知信息不完整。UTF-8 解码使用替代字符；
这里不承诺还能读取被丢弃的原字节。REST 详情提供保留的完整尾部，事件/卡片可
只带摘要；浏览器合并规则归[辅助读取](frontend-list-reconciliation.md#会话打开与辅助数据)。

`idle_timeout_seconds` 默认 60；stdout 或 stderr 任一流首次产生字节后，才开始
以两流最近一次输出为基准检查共同静默。0 明确禁用 idle 检查。
`timeout_seconds` 默认 1800、必须为正整数，从原任务启动
预算计算，包含没有任何输出的时间。禁用 idle 不禁用总时限，也不免除显式取消。
两项参数属于任务，不按命令名增加例外。

| 结束事实 | 业务状态／原因 |
| --- | --- |
| 正常退出0 | `completed` |
| 正常非0 | `failed`／`exit_nonzero` |
| idle／总时限 | `failed`／`idle_timeout` 或 `total_timeout` |
| 用户／原work取消 | `cancelled`／`user_cancel` 或 `work_cancelled` |
| 服务关停／外部信号 | `interrupted`／`server_shutdown` 或 `external_signal` |
| 启动等待失败且没有明确停止原因 | `failed`／`startup_failed` |

启动补偿保留原 reservation 的明确停止原因；单独取消启动 caller 不冒充用户取消
或服务关停。迟到 spawn 的实际异常沿原错误渠道保留，不用外部等待取消改写它。

## 结果、退休与返回顺序

shell 和输出结束后先固定原命令结果，再主动对启动时捕获的进程组执行 TERM、
宽限、必要的 KILL 和活跃成员退出确认。shell leader 退出不能证明仍持管道或
重定向输出的后代已退出。覆盖范围限于捕获的进程组，主动脱离该组的任意后代
不在保证内。正常收尾不改记取消；结果固定后的取消加入原清理，不重选成功状态。

迟到启动没有正常输出消费者时，由原补偿任务并发消费两条 pipe，直到双 EOF 和
原 `proc.wait()` 完成；leader 已退出也必须完成该证明，不能先等 wait 再消费
背压中的输出；该补偿路径尚未启动正常 reader，每条 pipe 仍只有一个消费者。
进程证明、终态写入均成功后才移除 reservation；写入失败保留原失败
cleanup，后续退休传播原错误，不以移除引用制造关停成功。

退休确认之后才持久化终态、广播普通完成并向父会话返回。记录、退休或交付失败
均保留原 owner 和已经固定的结果；后续取消／关停可重入未完成阶段，不重跑命令、
不重复已确认记录和广播。无法确认退休时，任务继续阻塞安装排空。

父返回以 `bg:{task_id}` 调用[公共返回接管](input-reliability.md#公共返回接管)，
先确认原 receipt，再按需读取材料和检查原资格。manager 保存命令结果及独立
`delivery_disposition`／`delivery_error`。明确容量拒绝可以沿原身份重试；永久拒绝
记 `stopped`，可能已接纳但无确认记 `unknown`。后来的不存在或拒绝不擦掉 unknown；
仅原 receipt 证明接纳，父会话 idle 不能证明模型已消费。

## 读取、取消、重启与关停

列表和详情同时核对账户、session、task 归属，归档聊天仍可只读其历史任务。
取消返回的 `cancelled: bool` 表示本次是否发起／加入停止动作，不是同步清理完成
或外部效果撤销的证明。缺少进程内句柄的历史行也不能凭 `false` 证明 OS 资源已退出。
原 work／session 退休捕获并 join 对应启动和执行责任。

重启把遗留 running 行标为 interrupted，不自动执行旧命令或重放终态返回。该
历史标记本身不杀旧 OS 进程；受管安装的原代退出证明和当前代资源责任归
[安装生命周期](installation-lifecycle.md)。关停封闭接纳，join 启动、进程组、
终态保存与交付；未收束则明确失败，保持数据库等依赖及原引用可用于继续收尾。

## 源码与回归入口

源码：[manager](../../server/bg_tasks.py)、[REST](../../server/routers/bg_tasks.py)、
[返回适配](../../server/sessions/turns.py)。
`tests/test_bg_tasks.py`、`tests/test_bg_session_return.py` 和相关 BG 生命周期测试
覆盖持久接纳、超时／输出、迟到 spawn、后代退休、终态返回及失败保全。
测试选择与通过证据复用见[testing.md](../testing.md)；这里描述代码合同，不声明
本次重跑了产品测试或生产已发布。

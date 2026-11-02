# 阶段、故障诊断与可行动告警

> **Status:** proposed — 将原执行事实连成可读阶段和故障入口，增加少量可采取行动的告警。

当前 Monitor 已有用量、进程与 Connector 等观测，普通请求、BG、Research、
命名委派也各自保留身份。本计划增加的是用户怎样解释工作停在哪里、该采取
什么动作。当前计时与 TPS 边界见[Codex Monitor 合同](../implemented/codex-performance-monitor.md)，
退休与业务结果的关系见[执行合同](../implemented/architecture-contract.md)。

## 第一段：从事实得到可解释阶段

选一个真实诊断路径，关联原 request/work/attempt，显示准备、执行、等待问题、
投递、退休等有证据的阶段与原记录入口。没有心跳证据时不显示“仍在工作”的
假动画；观测过期、缺失与已知停滞分别表达。阶段是原 owner 事实的投影，不
新建第二份可裁定业务完成的状态机。

凭据显示可理解的账户 label 与需要重连的原因，Connector 失效有原 installation
和具体操作入口。保留异常链、stderr、协议上下文及 provider/request/耗时，
按字段掩码真实 token、密钥和凭据，普通 prompt/argv 保持可诊断。

## 告警的选择与退出

先选少量用户能采取行动的条件，例如需要重连、重复终态失败或已知资源退出
异常。每种告警定义事实来源、触发窗口、去重、账户受众、恢复条件与关闭选项。
告警失效或确认不改变原业务结果；异常通知重试不重跑工作。

现有进程超时不是常驻 LLM Watchdog。用量可见也不等于能精确推算没有观测到
的解码 TPS。阶段诊断不要求每轮增加模型分析；程序能确定的条件直接计算。

## 后续范围与完成条件

| 条件诊断范围 | 触发与验证 |
| --- | --- |
| CLI/协议/配置健康 | 用户需要解释不兼容时，复用已有能力发现，区分未安装、不支持、配置错误、授权过期和临时服务失败；不自动联网调用模型/刷新凭据，也不探测用户未启用后端。 |
| 委派总览/运行树 | 从已有 delegation/work/session 事实派生当前账户只读视图；每项能跳回原工作、等待问题与投递失败，不新增 Run 表或推断“全树已完成”。 |
| MCP 同步工具线程池 | 观察到阻塞调用接近容量或排队/公平性反例时，先测同时长占用调用及等待时间。机制变更归具体 MCP 模块，保全 ContextVar 权限与取消，不默认把所有模块改为 async。 |

按账户用量、托管场景的窄安全审计和费用阈值各有独立需求；费用必须有可信价格
和用量来源。任意重试参数不因监控页存在而开放，其行为仍归执行策略 owner。

所选故障能从用户入口定位到原任务和具体可操作原因；等待问题、投递迟滞和
退休迟滞不互相冒充。告警跨账户隔离、重复事件不轰炸、恢复后可关闭，观测
缺失不制造运行成功。适用验证见[测试政策](../testing.md)。

来源：[上游 turn safety](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/turn-safety.md)、
[credential reauthorization](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/harness-credential-reauth.md)、
[polish 的告警目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/polish-2026-09.md)。
旧现场数量与阈值不是本分支当前性能结论。

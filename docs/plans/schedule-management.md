# 可编辑、可解释的定时任务

> **Status:** proposed — 连通定时任务编辑、原执行历史与跳过原因，并单独选择周期漏跑策略。

当前有开关、删除、Wiki mode 控制及后端更新能力；普通 prompt/recurrence 的
完整管理页编辑尚需补齐。现行 fire、overlap 与归属规则见
[定时执行合同](../implemented/scheduled-runs.md)和[Agent 工具合同](../implemented/schedule-tool.md)。

## 第一段：编辑与原执行可追踪

用户能编辑实际需要的 prompt、时间/周期与目标，保存后读回有效值。界面须说明
时区、一次性/cron/interval 的选择和验证失败原因；不能仅改变显示文字而未更新
运行中的调度注册。运行或等待中的原 fire 保留身份，编辑只按明确生效规则影响后续执行。

编辑 name/prompt/recurrence/timezone 时明确清空互斥字段，显示实际 next-run；
错误和冲突保留用户草稿，跨 tab 晚回执不覆盖更新。结构化 recurrence 不额外
调用模型解析；时区、DST 与过期一次性时间的处理均有可见反馈。

历史视图链接原 fire、session、input receipt 及结果。没有执行的情况显示可证明的
skip 原因，例如前次仍 outstanding 或目标不可用；不为显示“运行过”新增另一份
完成账本。手动执行/跳过等控制若选入范围，也使用同一接纳与 overlap owner。

历史按原 request/receipt 区分每次 fire，尤其 origin 模式多次共用会话时不能只
看 last-run。保存当次定义、时间与原结果链接，成本仅展示可观测值，unknown 与
录制失败如实表达。分页按账户隔离；若现有事实不足，再决定最小持久记录，
不预先建通用 Run 系统。overlap、准入关闭、无效目标与执行失败分别显示，
skip 不伪造完成请求，也不由 badge 自动触发补跑。

目标账户、Agent 生命周期及 Wiki 授权在保存和实际 fire 时都保持校验，错误不
把旧目标自动改绑到同名对象。普通日程编辑不授予新的知识或工具权限。

## 周期停机策略是单独选择

[scheduler.py](../../server/scheduler.py)已经在启动注册时补触发未消费的一次性
任务，原 fire 的消费由目标/输入接纳事务决定；这不是待实现的 catch-up。

待决定的是周期任务停机后的策略：跳过、合并一次或有限补跑。选择后需明确上限、
与原 fire 的关联、时间窗口、overlap、配置变更和未知结果处理。不能在重启后
无界重放，也不能因回执丢失重复一次性任务。历史保留与清理归
[数据保留计划](data-retention.md)，本页负责可见性和选择。

## 完成条件

用户经公开管理页编辑目标字段，刷新后与运行有效值一致；无效输入不破坏既有
日程。一次执行可追踪到原输入与结果，skip 有事实依据。选定周期策略通过停机、
重启、重复触发与 unknown 场景，不重复消费一次性 fire。适用检查见[测试政策](../testing.md)。

来源：[上游 schedule tool](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/schedule-tool.md)、
[scheduled runs](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/scheduled-runs.md)。
旧 catch-up 缺口不再沿用；执行 UI、保留政策与调度语义分别交付。

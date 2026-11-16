# 定时任务管理能力

> **Status:** proposed — 完整定义编辑、执行历史与周期补跑政策的缺口。

后端可更新定义；管理页主要提供开关、删除和 Wiki 模式，没有完整 prompt/recurrence 编辑。未消费的一次性任务可在启动注册时补触发；没有统一周期停机补跑政策。

待决范围：选择具体编辑或历史查询流程。last_run_at 与防重叠的文档/实现差异见[执行合同](../implemented/scheduled-runs.md#time-fields-and-contract-discrepancy)，需先确认预期。

当前合同：[领域说明](../implemented/schedule-tool.md)。本条记录需求边界，不预选实现方案，
也不构成开工、发布或数据操作授权。具体设计由获授权的活动任务规格维护。

# Agent 协作许可、进度与人工接管

> **Status:** proposed — 补调用许可、父卡只读进度和原问题接管，原生 child 控制按 provider 能力单独选择。

当前命名委派已按会话/工作轮次追踪返回和精确取消；原生 helper 也已有观察与
保存详情。当前合同见[命名委派](../implemented/agent-collaboration.md)与
[原生子任务](../implemented/native-subagents.md)。待补的是明确许可、方便定位
子工作以及人工接管，不以摘要替换原工作事实。

## 第一段：许可与只读进度

选定 Agent 之间 allow/deny 的实际产品规则，说明默认值、保存 owner 与撤权
时机。关系在实际调用时仍须有效，不能仅创建会话时查一次；同账户不自动等于
允许所有委派，跨账户始终沿当前主体边界拒绝。

父卡显示原子工作记录、当前可证明的进度、原问题和原结果入口。工作卡摘要不
作为“子任务完成”证据，通知送达与业务完成也分别显示。复用现有读取/观察
合同，保全原 work id、账户、晚到事件和失效目标，不新建进度状态机。

许可的目标发现与执行检查一致，start/followup 都受规则约束；保存配置、迁移、
归档/删除和拒绝原因一起完成。父卡按 delegation_id/work_id 订阅只读 preview，
刷新后恢复已保存内容并限制 payload/节流；部分回复不自动注入父模型，业务完成
仍由原 receipt 一次回交。

问题转交/回答快捷操作绑定原 question、原工作和其当前状态。已经回答、过期、
取消或归属变化的问题不可由新卡片再次提交；人工回答仍由原问题 owner 接管。

问题快捷转交先以真实二步漏答/误用或明确产品需求为触发，保留 first-writer-wins
与每跳 authority，不把 child 问题自动越过 caller 发给用户。父 Agent 对运行中
named delegation 的定向修正另按明确需求接到现有 steer/后续输入，绑定原 work
与实际 backend 能力；不能借 followup API 偷换成第二个派发者或自动重开已完轮次。

## 原生 child 控制是独立条件项

先调查目标 provider 对精确 child、活动阶段、权限和停止/输入支持什么，再
决定暴露哪些控制。不能绕过 Codex 当前 child 直接输入限制，根终态后的控制
也不能重新打开原 receipt。停止指令的回执与原资源退休分别取证。

自定义角色定义到 Codex 配置的映射归[角色计划](codex-native-roles.md)，不在
这里再维护一套模型/工具配置。更强 post-final 递归仍归[暂停执行范围](runtime-execution-scope.md)。

## 后续冲突处理与完成条件

memory 并发更新先构造真实同一文件同时编辑场景，再决定单写者、显式版本
冲突或现有文件能力是否足够，不先增加全局分布式锁。

选定许可在实际使用时生效，撤权/跨 scope 拒绝不创建工作；父卡能定位原工作
与结果；人工回答保留原问题所有权。若选入 child 控制，支持范围与拒绝反馈
都有实际 provider 证据，取消/迟到事件不影响下一轮。验证见[测试政策](../testing.md)。

来源：[上游协作后续目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/agent-collaboration.md)、
[native subagents 后续目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/native-subagents.md)。

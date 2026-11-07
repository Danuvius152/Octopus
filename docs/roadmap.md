# Octopus 后续路线

本页说明下一步的选择、依赖与启动条件；[文档索引](README.md)负责查找全部文档，
具体设计和完成条件由各主题计划维护。计划进入目录不代表已经开工，也不构成实施授权。

## 当前起点

工程收敛的第一、二阶段及 Codex R2 已按各自范围交付：普通 Codex 请求使用
App Server，Research 使用独立 exec leaf，纯文本调用使用无工具 worker。
输入接纳、公共返回、问题、业务结果与资源退休分别有明确责任，见
[架构](architecture.md)及[输入与完成合同](implemented/input-reliability.md)。

Codex 当前保证活动根内协作、原生输入接纳证据和根终态后的有界退休；
任意 post-final 多层递归自动推进仍无保证。
[G0 版本实验](reference/codex-app-server-g0.md)保留有限通过和强目标反例，
不能将二者合写成“全部原生工作完成”。DeepSeek backend 与 Codex 自定义角色映射尚未实现。

这些是代码与合同状态；当前安装是否运行该版本，须按实际发布回执核验。
[原四阶段路线](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/engineering-runtime-roadmap.md)
保留历史顺序与当次证据，后续变更直接更新所属主题。

审查修复还包括模型凭据迟到拒绝、Native 完成与 Question、委派逐份回信消费、
OAuth 认证表示、Claude 诊断归属和 Connector 授权失败，分别归现行主题合同。
已证实的可放弃 sampling started 不再阻止根成功，真实工具和已证明 final 缺口
仍失败；见 [Native 完成边界](implemented/codex-backend.md#root-completion-and-selected-answer)。
第三方写入重试幂等仍独立待选，不将局部有限连接重试扩大为恢复重构或全工具
效果证明。上述为实现状态，不表示本机已发布这一版本。

## 建议先处理什么

按实际需求选择[多租户剩余项](plans/multi-tenancy-gaps.md)中的 OAuth 发起浏览器
绑定策略及待验证性质；Agent DELETE 校验和 Connector refresh 分类已有实现。
更强托管政策和 OS 隔离另行选择，
不将全部 SaaS 能力设为本机功能的共同前提。

随后按实际重点选择独立范围：

- [DeepSeek backend](plans/deepseek-harness-backend.md)：先锁定可重建发行组合，
  通过公开 API、真实 loader、steer、完整结果、问答与退出的能力门，再接全部消费者。
- [Wiki 与阅读应用](plans/wiki-future-plan.md)：选择多 App、标签、引用或 reader 旅程，
  先确定 owner/client 授权。
- [Research 工作流](plans/ai-research-workflow.md)：补证据结构与反证保全，复用现有研究执行。
- [Codex 原生角色](plans/codex-native-roles.md)：验证角色映射和能力，独立于完整递归闭合目标。

## 继承的产品完善

每个主题选择一个完整用户场景交付，各自维护依赖和验收，彼此没有统一串行阶段。

| 方向 | 首要用户结果 |
| --- | --- |
| [App 使用与维护](plans/application-evolution.md) | 秘密配置、问题/附件交互、已知版本回滚及其数据边界。 |
| [定时管理](plans/schedule-management.md) | 编辑、执行历史、跳过解释与明确的周期漏跑策略。 |
| [Connector 能力](plans/connector-capabilities.md) | 长结果完整读取与敏感写入的明确授权。 |
| [诊断与告警](plans/observability-and-diagnostics.md) | 可证明的任务阶段、活动证据和用户能处理的告警。 |
| [数据保留](plans/data-retention.md) | 数据分类、引用关系和清理预览；保持默认保留历史。 |
| [会话输入](plans/session-input-controls.md) | 看清有效配置、补充附件、显式停止后改变方向。 |
| [Agent 协作控制](plans/agent-collaboration-controls.md) | 调用许可、原工作进度与问题的人工接管。 |
| [历史与恢复](plans/session-history-and-recovery.md) | 差异预览、文件归因与只读分支比较。 |

## 由实际场景启动的方向

| 方向 | 启动条件 | 第一段范围 |
| --- | --- | --- |
| [稳定接口与 octo CLI](plans/octo-cli.md) | 有明确脚本或 App 数据消费者。 | 选定操作、scope 与薄 CLI，共用业务 owner。 |
| [工作视图、Router 与 Channels](plans/agent-router-and-channels.md) | 跨会话追踪或统一输入成为实际问题。 | 先做只读工作视图，Router 和单个 Channel 分别选择。 |
| [Runtime providers](plans/runtime-providers.md) | 需要第二执行环境或不互信租户隔离。 | 保持 Local 完整能力，并验证一个真实第二 provider。 |

DSH 复用当前 Harness、接纳、结果和退休合同，不以远程 Runtime 为前置。
稳定接口依赖消费者和权限范围，不以 Router 为前置；Router 也不要求先实现全部 CLI。
通用 App scope 归稳定接口计划，Wiki 客户端权限归 Wiki 计划。
跨环境承诺必须覆盖进程、文件、双向网络、历史和资源退休。

## 暂停范围与保留选择

[运行时执行扩展](plans/runtime-execution-scope.md)按具体断言保持暂停：
完整 post-final 递归等强目标需新版本能力证据与明确范围；R06 模型配置核验保留独立恢复条件。
Wiki R14 额外性能实验继续按 [Wiki 计划](plans/wiki-future-plan.md)暂停。

自动返回保持成功接纳后的逐条派发；返回合批、自动 steer、unknown 重放、
旧全局 token rotation、云基础设施采购和全量多 Agent 平台不随文档整理恢复。
改变这些选择时，先在所属计划写清用户结果、责任和失败边界。

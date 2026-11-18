# 当前能力缺口与待决需求

当前实现的责任和失败边界见[架构](architecture.md)与[领域合同](catalog.md)。
本页列出尚需选择的范围；条目顺序不定义永久优先级，也不恢复暂停任务。
选择实际用户目标后，具体设计和验收进入该任务的活动规格。

| 待决主题 | 当前缺口 |
| --- | --- |
| [Agent 协作许可与人工控制](plans/agent-collaboration-controls.md) | 调用许可、进度展示与人工接管的待决范围。 |
| [跨会话工作视图与外部输入](plans/agent-router-and-channels.md) | 统一工作查询和外部渠道分派尚未定义。 |
| [Research 证据、阅读与复现](plans/ai-research-workflow.md) | 原问点覆盖、来源独立性、完整阅读和可复现实验的缺口。 |
| [App 配置与交互能力](plans/application-evolution.md) | App secret、问题交互、版本恢复和 [SmartReader 阅读上下文覆盖](plans/application-evolution.md#smartreader-阅读上下文覆盖)。 |
| [Codex 自定义原生角色](plans/codex-native-roles.md) | Codex 尚无与已保存子角色配置对应的原生角色映射。 |
| [Connector 完整结果与权限](plans/connector-capabilities.md) | 超长结果取回和写入权限政策的待决范围。 |
| [数据保留与清理范围](plans/data-retention.md) | 尚无统一按龄清理全部账户资料的政策。 |
| [DeepSeek 后端支持缺口](plans/deepseek-harness-backend.md) | DeepSeek 尚未注册为独立 Harness backend。 |
| [账户隔离的未支持性质](plans/multi-tenancy-gaps.md) | OAuth 浏览器绑定、宿主隔离和产品政策的待决范围。 |
| [执行诊断与告警需求](plans/observability-and-diagnostics.md) | 跨领域诊断入口和可行动告警尚未形成统一产品合同。 |
| [通用资源 CLI 缺口](plans/octo-cli.md) | 当前 octopus CLI 尚不是通用 octo 资源管理接口。 |
| [原生运行时的能力边界](plans/runtime-execution-scope.md) | 任意 post-final 递归和额外实机核验未纳入当前承诺。 |
| [第二执行环境](plans/runtime-providers.md) | 尚无经过验证的第二 Runtime provider。 |
| [定时任务管理能力](plans/schedule-management.md) | 完整定义编辑、执行历史与周期补跑政策的缺口。 |
| [会话分支与文件还原边界](plans/session-history-and-recovery.md) | 同路径文件归因和外部效果恢复的缺口。 |
| [会话输入与配置反馈](plans/session-input-controls.md) | 活动请求配置、附件及改方向反馈的待决范围。 |
| [Wiki 与阅读应用的能力边界](plans/wiki-future-plan.md) | 多 client 授权、标签查询和阅读材料交接的缺口。 |

定时执行的 `last_run_at` 及防重叠保证存在明确的
[代码与合同差异](implemented/scheduled-runs.md#time-fields-and-contract-discrepancy)。
这类差异须先核对已确认需求，不能通过文档整理追认实现偏差。

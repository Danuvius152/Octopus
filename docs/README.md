# Octopus 文档导航

本页回答“这个任务先读哪里”。先进入对应章节；当前任务未覆盖时查
[完整目录](catalog.md)。[路线图](roadmap.md)说明未完成工作的优先级与依赖，
[架构总览](architecture.md#system-overview)说明组件怎样连接。版本观察、审阅材料
和历史事故由[目录](catalog.md#版本参考与历史证据)按需进入。

| 位置 | 职责 |
| --- | --- |
| 指南及 `architecture.md` | 使用、操作步骤与组件责任关系。 |
| [`implemented/`](implemented/) | 当前代码遵循的领域合同、边界与必要理由，不代表已部署。 |
| [`plans/`](plans/)与[路线图](roadmap.md) | 未完成目标、启动条件与依赖；计划入表不授予实施权限。 |
| [`testing.md`](testing.md) / [`local-wsl.md`](local-wsl.md) | 验证政策 / 本机操作。 |
| [`reference/`](reference/) / [`post-mortems/`](post-mortems/) | 固定版本观察 / 对未来诊断有用的因果证据。 |
| 账户 workspace、`review/`、`.scratch/` | 临时任务和审阅材料；路径与退出规则见[任务约定](agents/issue-tracker.md)。 |

文档维护先读[文档规则](AGENTS.md)，动作范围遵循[根规则](../AGENTS.md#repository-and-upstream-maintenance)。
完成计划将当前合同与理由合并回领域 owner，退出旧执行正文；实现、验证和部署分别取证。

## 按任务查找

| 任务 | 首读范围 | 扩读条件 |
| --- | --- | --- |
| 理解组件与事实 owner | [组件责任](architecture.md#components)、[故障边界](architecture.md#ownership-and-failure-boundaries) | 改行为再读对应领域合同 |
| 完成消费者或失败交接 | [原请求完成](implemented/input-reliability.md#原请求完成与失败交接) | 要确认资源退出读[执行观察](implemented/input-reliability.md#原执行的观察)和[Harness](implemented/harness-layer.md#execution-and-resources) |
| BG/Research/委派返回接管 | [公共返回接管](implemented/input-reliability.md#公共返回接管) | 改材料、重试或恢复才读[BG](implemented/background-tasks.md)、[Research](implemented/native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler)或[委派](implemented/agent-collaboration.md#53-reply-delivery-the-original-request-completion) |
| 输入接纳、恢复或问题 | [输入任务导航](implemented/input-reliability.md) | 改原生写帧或 MCP 清理才读其指向的运行时章节 |
| Codex MCP 配置 | [Native MCP configuration](implemented/codex-backend.md#native-mcp-configuration) | 涉及输入或恢复保证读[首输入证据](implemented/input-reliability.md#codex-native-input-evidence) |
| compact 或上下文比例 | [compact 输入](implemented/input-reliability.md#native-compaction-requests)、[样本与绑定](implemented/model-reasoning.md#上下文样本与配置绑定) | 改原生协议读[Codex](implemented/codex-backend.md#native-compaction)或[Claude](implemented/harness-layer.md#claude-native-compaction) |
| 后端、steer、重试或原生子任务 | [Harness](implemented/harness-layer.md)、[轮内引导](implemented/inline-steering.md) | 按边界进入[Codex 章节](implemented/codex-backend.md)、[瞬态恢复](implemented/harness-transient-retry.md)或[子任务](implemented/native-subagents.md) |
| Agent 配置、模型或记忆 | [Agent](implemented/agent-refactor.md)、[模型设置](implemented/model-reasoning.md)、[记忆](implemented/memory.md#current-content-ownership) | 凭据变化读[凭据生命周期](implemented/harness-credential-reauth.md) |
| 账户、格式或搬迁 | [账户](implemented/multi-tenancy.md)、[存储与交接](implemented/current-storage-format.md) | 实际搬迁读[完整操作](backup-and-migrate.md#account-export-and-import) |
| 后台任务、定时或安装排空 | [BG](implemented/background-tasks.md)、[定时运行](implemented/scheduled-runs.md)、[安装生命周期](implemented/installation-lifecycle.md) | 改原请求完成再读完成合同 |
| 应用、构建或后端进程 | [应用](implemented/applications.md)、[应用后端](implemented/application-backends.md#6-lifecycle) | 改 App 对话读[Agent 访问](implemented/app-agent-access.md) |
| 普通 Wiki 查询 | 当前 query 指南与工具（按任务 load_guide） | 开发引擎才读[Wiki 工程导航](implemented/wiki-development-summary.md)；可信工作要求见[知识请求](implemented/knowledge-requests.md) |
| 前端列表、会话或卡片 | [前端任务导航](implemented/frontend-list-reconciliation.md) | 改可见性读[置顶](implemented/sidebar-pins.md)，移动布局读[手机端](implemented/mobile.md) |
| 日常验证或文档修改 | [快速检查](testing.md#fast-checks-during-development)、[主题入口](testing.md#topic-regression-entry-points)、[隔离](testing.md#markers-providers-and-isolation) | 提交/发布才读[相应矩阵](testing.md#release-and-documentation-checks)；文档改动核对实际改变的源码事实 |
| 发布已验证版本 | [自动发布完整操作](local-wsl.md#自动发布) | 环境失效才读[重建](local-wsl.md#环境重建)，手机故障读[公网入口](local-wsl.md#手机公网入口) |

## 使用与配置指南

- [用户指南](user-guide.md)：启动、Agent 和手机访问。
- [Connector 设置](connectors-setup.md)：浏览器中的 GitHub、Gmail 和自定义 OAuth。
- [Agent 提示词](agent-prompts.md)：可复制的角色配置快照；编辑文件不会更新保存配置。
- [本机测试](local-wsl.md#本机测试)：直接选择 PATH/代理、离线目录或 live 凭据与网络条件。
- [领域词汇](../GLOSSARY.md)、[任务与 skills 约定](agents/issue-tracker.md)、[状态标签](agents/triage-labels.md)和[领域文档规则](agents/domain.md)。
- [完整合同与计划目录](catalog.md)：生成索引、活动审阅及固定版本证据；新增或移动正文后运行现有目录生成器。

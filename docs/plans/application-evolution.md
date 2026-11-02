# App 使用与维护闭环

> **Status:** proposed — 补齐 App 秘密配置、问题/附件交互与可解释的构建版本恢复。

当前 App 已有托管、构建、后端进程、Agent 对话、停止和删除；本计划只描述
增量。当前合同见[应用](../implemented/applications.md)、
[后端](../implemented/application-backends.md)与[Agent 接入](../implemented/app-agent-access.md)。
`app_agent.py` 已向消费端发送 question 事件，不等于 App 已有完整答复交互。

## 第一段：秘密配置与原问题交互

选一个实际 App，打通配置秘密后运行、看到待答问题、回答并继续同一请求。

- 秘密独立于生成代码与构建日志保存。读取/注入受账户、App 和当前运行权限
  约束；只把所需秘密交给选定消费者，错误和日志按实际字段掩码。
- 问题由原请求与 question id 定位。App 关闭页面、SSE 断连和取消执行是不同
  动作；重新连接先读原问题，过期或已取消的问题不能被新页面回答。
- 附件经过现有受理和文件证据链。明确格式、体积、所属会话及保留边界；浏览器
  不能提交任意宿主路径当作可读授权，已接受附件不能静默丢弃。

App scope 与 Agent allowlist 归[接口计划](octo-cli.md)，Wiki client 授权归
[Wiki 计划](wiki-future-plan.md)；这里消费其结果，不维护第二份权限模型。

## 构建版本与回滚

保留一个已知成功的代码版本，并提供恢复前的版本与差异说明。选择的恢复范围
必须说明是否包括依赖、静态资产、后端启动配置和 schema；运行数据另有所有者。
代码回滚不能伪称数据同时逆转，也不能自动覆盖用户后来编辑的文件。

恢复期间协调原构建、运行后端与当前消费者，失败后保留原工作和可定位的错误。
新持久版本、秘密或关系同时定义导出/排除/恢复，遵循[搬迁合同](../backup-and-migrate.md#account-export-and-import)。

## 完成条件

一个真实 App 能配置秘密并正常运行，秘密不进入生成源码和日志；用户能看见并
回答所属待答问题，取消/过期/断连不串答。附件实际可读且归属可核。
选定恢复范围能回到已知版本，明确保留的数据和人工文件不受损。跨账户、撤权、
重复操作及失败恢复均通过适用验证，检查选择见[测试政策](../testing.md)。

## 后续范围与来源

| 范围 | 启动条件与完成条件 |
| --- | --- |
| CPU/内存与公平资源预算 | 现有进程数量上限不能约束一个 App 的 CPU/内存。选可实际强制的环境机制，覆盖 install/start 和整个子进程组；超限有 owner、日志、状态及确认退出，账户份额与恢复明确。timeout/RSS 采样不冒充强隔离。 |
| 多进程/独立服务 | 先验证一个受管前台进程组能否满足；只有真实 App 需要独立健康/日志/重启声明时才扩展，不把派生 worker 当作必需重写 manifest。 |
| 分享、发布与模板 | 先选静态导出、只读分享或独立托管的一种真实模式。读者不继承 App token、Agent、Wiki 写入或 backend secret；动态执行和账户数据授权单列，不随文档计划开公网入口。 |
| Agent/Application Market | 个人 All 页不是分发市场。有真实模板复用用户后，先交付可审查的配置/模板分发，排除 secret、个人路径与身份；店铺/安装计数/通用 market 不默认必做。 |

更强 App sandbox 归[租户隔离政策](multi-tenancy-gaps.md)，不在此处用进程预算
替代安全边界。更细的受控 Secrets 注入按所选运行模式补齐。

来源：[上游 applications](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/applications.md)、
[application backends](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/application-backends.md)、
[app-agent access](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/app-agent-access.md)
的未完产品目标；实现复用本分支已有 App 生命周期。

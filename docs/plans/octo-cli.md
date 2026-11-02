# 稳定接口、薄 CLI 与访问凭据

> **Status:** proposed — 围绕真实消费者建立稳定数据接口、薄 CLI、PAT/设备管理与受限 App scope。

当前安装命令为 `octopus`，已经有服务启动、会话交接/拉取和账户搬迁操作；这不
等于已经交付名为 `octo` 的通用数据 CLI。入口见 [cli.py](../../server/cli.py)
与 [console script](../../pyproject.toml)。本计划继承稳定接口方向，先选择真实
消费者需要的完整操作集，再决定命令名与兼容版本入口。

## 第一段：一个消费者只通过接口完成任务

可从“读取自身 Agent/session、分页读取原结果、读取获准 Wiki 材料”中选择
一个实际消费者。先核现有路由是否足够，只有具体兼容需求才增加版本入口；不
预先把全部路由搬到 `/api/v1`，也不复制一套业务写入实现。

账户身份、run lease、work id、知识来源及任务授权须穿过 CLI、SPA、MCP、App
入口，仍由原业务 owner 在使用与提交时维护不变量。路由完成身份解析不代替
业务操作对所有权、活动配置与并发前提的检查。

选定的有副作用操作必须有原请求身份与结果查询路径：提交后断连时继续查询
同一操作，不能把缺少响应当作“没有执行”而再写一次。

## PAT、设备与 App 权限

[UserManager](../../server/users.py)和[token 存储](../../server/db/users.py)已有
issue/list/revoke 底层能力。待补的是可理解的命名 PAT、设备/session token 管理、
薄 CLI 凭据发放及撤销体验；不要把底层方法存在写成完整管理界面已经完成。

- 明确 token 的受众、有效期、label、最后使用及可撤销范围；原始 bearer 只在
  发放时显示，列表不返回可重用秘密。CLI 本地凭据有保存和退出规则。
- App scopes 与 Agent allowlist 在实际调用时生效；默认只获得选定用途所需
  能力。不能把一个会话 bearer 扩成整个账户数据权，也不能只靠 UI 隐藏按钮。
- 设备退出、PAT 撤销、账户禁用与已经发生的 provider 效果分别处理。撤销先
  阻止后续使用，原任务取消/退休继续由业务 owner 完成。

Wiki owner/client grant 的细节归[Wiki 未来计划](wiki-future-plan.md)，此处仅消费
它的授权结果。App secrets、构建版本与问答 UI 归[App 演进](application-evolution.md)。

## 完成条件

选定消费者无需数据库或任意宿主文件旁路即可完成目标；分页和体积边界明确。
跨账户、越 scope、过期和撤销调用均在副作用前拒绝且原状态不变。断连后能沿
原身份查回结果。不同入口复用同一业务 owner，现有 MCP 与 App 行为保持一致。
新增持久凭据/关系同时更新[搬迁合同](../backup-and-migrate.md#account-export-and-import)。

## 后续选择与来源

公开第三方 API、静态二进制、全资源 CRUD 和 SDK 在出现具体消费者后分别选择；
不要求先完成 Router、DeepSeek 或远程 Runtime。旧安装级全局 token rotation
已经退役，不因建设 PAT 管理而恢复。

数据 MCP namespace 是否改指统一接口或退役也须逐项选择：比较同一实际操作
在 CLI/MCP/API 的权限、结果、发现成本、错误恢复、延迟与 token，替代路径可用
且兼容策略明确后才退出旧入口。ask/BG/delegation 的交互生命周期不因“薄 CLI”
被当作多余包装删除。未来 Ledger/Ontology 路由只随已选产品能力增加，静态
binary 由真实镜像分发需求触发，语言绑定由真实外部消费者触发。

来源：[上游 octo 设计](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/octo-cli.md)、
[上游 token 管理目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/token-rotation.md)。
继承的是消费者与撤权目标，接口和执行边界以本分支为准；验证选择见[测试政策](../testing.md)。

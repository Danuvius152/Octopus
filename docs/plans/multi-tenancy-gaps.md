# 账户边界：具体剩余行为与产品政策

> **Status:** proposed — 优先收敛 Agent DELETE 作用域与 Connector refresh 分类，按证据级别保留其余账户政策。

本页按 `fd7ab9c` 源码重核旧租户审计，只描述剩余变化。静态路径确认、待验证
性质和产品政策分开；没有运行利用复现或生产检查。现行保证归
[账户合同](../implemented/multi-tenancy.md)、[Connector 合同](../implemented/connectors.md)
与[凭据刷新合同](../implemented/harness-credential-reauth.md)。原审计的账户数、
现场结果和旧漏洞实现不代表当前部署。

## 源码可确认的剩余行为

### Agent DELETE 的副作用前作用域检查

[DELETE 路由](../../server/routers/agents.py)先调用 scoped `get_agent`，但无结果
仍调用不带 `user_id` 的 [AgentManager.delete_agent](../../server/agent_manager.py)。
后者按全局 id 读取、删除行并删除 memory 目录，route 的 `assert agent is not None`
发生在副作用之后。archive 路径则已在 scoped 查询为空时拒绝，两者不能合并为
“都已修复”。

静态影响条件是已知目标 Agent id、目标非 system 且没有 session；不能扩写成
任意 Agent 均可被删除，也不能将事后 500/assert 当作删除未发生的证据。
修复范围是在副作用前拒绝跨账户目标，并让业务删除入口持有/校验调用者范围。
完成条件：两账户经公开 DELETE 及业务入口测试拒绝，目标行与 memory 目录
保持不变；合法删除和 protected/有 session 拒绝行为仍正确。

### Connector refresh 的临时与永久错误分类

[get_access_token](../../server/connector_manager.py)在 provider refresh 周围
`except Exception`，统一保存 `needs_reconnect=True`。临时 timeout/429/5xx
与永久 invalid_grant 尚未在这里分开，不能把全部刷新失败都解释为授权失效。

先通过真实 manager 的隔离测试保留该反例，再定义错误分类、可行动提示与
安全的重试边界。完成条件包括临时错误不伪造永久撤权、永久错误准确要求重连、
成功刷新清除旧错误且保全原 refresh token；有副作用的 provider 工作不因重连
自动重放，完整异常链继续可诊断。

## 还需验证或选择的性质

| 主题 | 当前证据与剩余范围 | 完成条件 |
| --- | --- | --- |
| OAuth 发起浏览器绑定 | state、TTL、claim 和账户绑定已有；callback 不接收发起浏览器 cookie，因此该性质未提供。先选择跨设备 UX 与 binding 策略，不声称已复现账户接管。 | 明确支持的跨设备流程；错误/取消/重复 callback 不错绑安装，新增 token/cookie 有实在用途。 |
| Connector 同主元数据 | 正常 set-enabled 写入会核账户；loader 按 agent id join，token 使用端仍核 owner。剩余检查针对异常链接/内部写入边界，未证明 token 外泄。 | 构造异常链接，元数据与使用端拒绝行为一致，既有安装不串账户。 |
| `schedules_changed` 通知 | 当前仍为全局空通知，仅产生时间信号/额外 refetch，不含业务内容。 | 若选择缩小受众，按原 owner 发送且断连重连仍能刷新；不将它描述成业务载荷泄露。 |
| WS query bearer 与日志 | WS 接受 query token；实际日志是否泄漏尚未核实。 | 用合成 token 贯通相关日志链路，按实际字段修掩码，不复制生产凭据。 |
| 宿主环境 | 部分 Harness 继承宿主 env，App backend 另有 allowlist。 | 明确隔离目标后用合成 sentinel 检查；保留必要 PATH/proxy，不凭代码推断已泄露实例秘密。 |
| legacy Connector 诊断路径 | `_shared.py` 明确保留环境诊断路径，不能凭旧稿判无 caller。 | 逐调用方确认责任和替代路径后才删除，不重建旧架构。 |

`enable_by_default` 的退出同样先核实际读取/写入方与既有配置兼容，不凭旧清理
建议在启动时重写用户保存的工具集合。

依据：[OAuth routes](../../server/routers/connectors.py)、
[Agent Connector 关系](../../server/db/agents.py)、
[Connector scope](../../server/mcp_servers/connectors/_shared.py)、
[Schedule 通知](../../server/routers/schedules.py)。

## 已处理的旧待办与明确例外

这些主体不再作为待开发同一修复：Connector token 按调用 scope 取得，custom
请求拒绝绝对 URL/穿越与跳转，disabled 账户在 token 使用端检查；WS/控制操作
核 session owner，schedule 在保存与 fire 校验目标，session archive 事件带 owner。
Agent/credential 绑定与 reauth 交换前 owner 检查、同凭据并发 refresh owner、
Codex staging 登录已有实现。新开户/邀请注册事务创建 protected default Agent，
普通启动不替已有账户自动补建。

全局 Connector catalog 的修改、custom kind 与 OAuth client 写操作已要求
`AdminUser`；已认证账户读取 catalog 与不含 secret 的 client 信息是明确共享
读取边界，不重复列为缺失管理权限。旧安装 token 与全局 rotation route 已退役。

核对入口：[custom 请求](../../server/mcp_servers/connectors/custom.py)、
[scope routes](../../server/routers/ws.py)、[schedule targets](../../server/schedule_targets.py)、
[凭据 owner](../../server/sessions/credentials.py)、[现有回归](../../tests/test_tenant_isolation.py)。
这里的测试路径供后续选择，不冒充本次运行通过证据。

## 独立产品政策

| 政策 | 选择前必须明确 |
| --- | --- |
| 无 binding 时使用 host 默认登录 | 支持的账户与授权范围；显式坏 binding 仍拒绝，保留当前获准本机流程。 |
| disconnect 后撤销 provider grant | 本地删除与外部 revoke 的效果、同 grant 多安装、失败提示及恢复；不将本地断开称为已撤销。 |
| 禁用账户后的既有工作 | 新准入/后续使用拒绝与已发 provider 请求、工具调用、OS 进程退休分开；签出不是全部停止。 |
| 执行容量 | 针对 global/account/one-shot/Research 定义目标与当前反例，保全已接纳返回与顺序。 |
| 不互信账户隔离 | 选定容器/OS 用户/网络等部署信任模型；全局 credential-id 目录形状本身不证明 HTTP 越权。 |
| SSO、自助恢复、计费与多进程 OAuth state | 真实托管消费者、故障与恢复责任；不是本机修复的共同前置。 |

pending OAuth 跨重启/多进程共享只在实际部署需要时选择，届时定义 TTL、一次
claim、取消与恢复，不把丢失登录草稿直接推导为业务授权失效。SSO、邮件验证与
自助密码重置分别需要身份恢复与滥用限制。请求限流/配额也以明确账户和操作
反例为前提，保全已接纳工作及其结果读取；不设一个无法结束的全站安全大门禁。

命名 PAT/设备管理归[稳定接口计划](octo-cli.md)；账户删除/crypto-shred 归
[数据保留](data-retention.md)；按账户用量/审计归[可观测性](observability-and-diagnostics.md)，
Connector 配额与正式发布准备归[能力计划](connector-capabilities.md)。不各存一份规格。

每次只激活一个有完整用户结果的范围，经公开入口验证 read/use、拒绝无副作用、
取消/晚到与原身份。检查遵循[测试政策](../testing.md)，不沿用旧实机邮箱演练
作为常驻操作授权。修复后将现行合同归回 owner，移除本页对应剩余项。

历史：[原始第二轮审计](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/multi-tenancy-gaps.md)、
[上游租户清单](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/multi-tenancy-gaps.md)。

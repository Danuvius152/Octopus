# Connector 完整结果与写入权限

> **Status:** proposed — 补长结果的受限取回和敏感写授权，再按需求扩展 provider 与操作 scope。

当前 Connector 已有账户 installation、Agent enablement、typed MCP 与 token
刷新。现行合同见[Connectors](../implemented/connectors.md)。
[_shared.py](../../server/mcp_servers/connectors/_shared.py)对工具结果设有体积上限；
截断后提示收窄查询不等于已经提供完整原结果的受限读取路径。

## 第一段：完整结果与明确授权

长结果提供分页或完整读取入口，句柄绑定原账户、installation、请求与实际返回
版本。定义长度、有效期、保存位置、撤权和过期后的重取语义。展示有界与业务结果
完整分别说明，超限不代表操作没执行；不得经任意文件旁路泄出未授权响应。

敏感写操作的许可应由实际调用可验证的事实决定，复用已有问答/授权及工具边界。
不能依赖“模型通常会先问”。先选少数真实动作，例如 Gmail 草稿发送，明确用户
确认的是对象、内容和操作；变化后旧确认能否复用有清楚规则。

写入授权、provider 已接纳和网络结果 unknown 是不同事实。当前 HTTP 调用已按
[资源操作合同](../implemented/connectors.md#resource-http-operations-and-unknown-effects)
禁止未知写入的自动重放并交还核对目标。未来为具体写动作启用重试时，须先核验
正式幂等/去重能力并沿用供应商认可的原身份，随真实消费者增加正向回归；没有
幂等/查询能力时继续保留未知及人工处置，不从本地 UUID 或一般重试建议推导保证。

## 后续能力与职责

**主动失效提示与显式健康检查：** 用户可主动触发有超时/速率限制的只读探测，
显示未检查、检查时间、可用、临时失败与需重连。没有记录错误不等于健康，基类
默认成功不冒充 provider 真实验证，探测不执行写操作。失效通知关联原 installation
与拥有者，复用[可观测性计划](observability-and-diagnostics.md)的去重/账户受众，
后台周期探测另行选择，不因此创建日程。

细粒度操作 scope、按会话临时权限、新 provider 和明确失效提示分别选择，权限
撤销在实际使用时生效。按账户配额与共享 OAuth client 的供应商正式发布/审核
准备属于面向更多外部账户的范围，启动时核官方现行要求；历史 Google Testing、
审核/CASA 说明不直接成为当前本机使用失败。

provider grant 撤销、refresh 错误分类、OAuth binding 与元数据边界归
[租户剩余计划](multi-tenancy-gaps.md)。Notion 只有明确外部同步工作流时才选
最小操作集，不默认再做一层与 Wiki 重复的知识存储。

## 完成条件

原结果超限后能在原权限范围完整取回，跨账户、撤权和过期句柄被拒绝；分页不
遗漏或错拼不同结果。选定敏感动作仅在有效授权下执行，重复确认/重连不重复
外部效果。失败、unknown 与已执行结果可辨，日志保留诊断且秘密按字段掩码。
新增持久结果的保留/搬迁归[数据保留](data-retention.md)与[账户搬迁](../backup-and-migrate.md#account-export-and-import)。

来源：[上游 Connector 后续范围](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/connectors.md)。
验证使用选定 provider/操作与[现行测试政策](../testing.md)，不据此操作生产邮箱。

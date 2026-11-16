# 账户隔离的未支持性质

> **Status:** proposed — OAuth 浏览器绑定、宿主隔离和产品政策的待决范围。

OAuth callback 用 state 关联原 login，未绑定发起浏览器 cookie。schedules_changed 是无内容的全局通知，查询仍按账户隔离。账户授权不提供 OS 进程沙箱。

以下静态路径仍需按实际消费者核对，尚无据此复现泄漏的结论：

| 边界 | 当前证据与核对条件 |
| --- | --- |
| WS query bearer 与日志 | [WS 入口](../../server/routers/ws.py)接收 query token；用合成 token 核对实际服务、代理及日志链路的记录和掩码，不能仅凭 URL 参数认定日志已泄漏。 |
| 宿主环境继承 | [Claude 启动参数](../../server/harness/claude_code.py)从宿主环境复制后应用凭据与设置；App backend 有独立环境白名单。先明确部署隔离目标，再用合成 sentinel 核对所需变量与继承范围，不能据此认定实例秘密已泄漏。 |
| Connector 同主元数据 | [安装 loader](../../server/db/agents.py)按 Agent ID 关联安装；正常 set-enabled 写入核对账户，token 使用端另核 owner。异常关联和内部写入的元数据范围仍需构造验证，不能将 loader 形态直接等同于 token 外泄。 |

待决范围：按实际部署与威胁模型选择要补足的性质；缺少某性质不等于已经复现账户接管或数据泄漏。

当前合同：[领域说明](../implemented/multi-tenancy.md)。本条记录需求边界，不预选实现方案，
也不构成开工、发布或数据操作授权。具体设计由获授权的活动任务规格维护。

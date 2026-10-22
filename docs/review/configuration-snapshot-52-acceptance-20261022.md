# 票52：请求配置快照与动态授权

本次沿用 configuration lock、SessionOwner、PreparedRun 和 Harness 的授权回调，
不新增配置管理器、持久快照或 RunBinding。完整绑定迁移仍属于票23。

## 责任与完成点

会话字段是持久默认值的内存投影。请求装配进入既有配置协调边界后，一起捕获
backend、model、effort、tier、credential ID 与 agent 默认值；请求上下文保留
原 Owner 的隔离副本。读取 agent 时不再配上更早复制的会话默认值。
装配之后更新默认值只影响下一次捕获，自动重试继续消费原 PreparedRun。

已有会话的 backend 在创建时已固定；agent backend 默认值继续影响新会话，
不会把正在运行的 Codex 会话换成另一个 provider。

凭据删除经同一个配置协调入口：SQLite 在一次短事务中清除会话绑定并删除凭据，
成功返回后才传播默认值。agent 外键和 secret 级联沿用原机制。
删除旧的独立 `clear_credential_from_sessions()` writer；删除失败不先清空内存或
持久会话绑定。已装配请求保留原 credential ID，不因此改用其他登录。

动态授权与配置默认值分开：现有 RunConfig 回调捕获选定身份，实际 OS spawn 与
每条 stdin 用户帧前重新检查账户及所选凭据的存在、owner、backend、状态。
账户与凭据在同一短只读事务中检查，避免在两次授权读取之间提交撤权后仍继续执行。
删除或需重连会阻止新的使用，已撤销 lease 在等待授权后仍不能写入。
这不撤销过去的外部效果，不要求默认值更新主动打断已开始的 provider 工作。

常驻进程持有实际启动时的签名，其中包含选定 credential ID、认证值或 home。
后续请求只比较原进程身份与新候选身份，不拿新凭据重新计算旧进程身份。
凭据变化时先退役旧进程，再启动使用新身份的进程；这份 hash 只随原 HarnessRun
存活，不是新的授权权威或持久执行账本。

## 回归证据

`tests/test_request_configuration.py` 通过真实 REST 配置入口与隔离 Python 协议
子进程验证以下行为，不调用真实 provider：

- 更新与装配交错：真实进程曾收到旧 session model 加上新 agent 的 effort、tier、
  credential；修复前失败，修复后收到完整的新组合。
- 装配后更新：当前请求收到完整旧组合，下一请求收到新组合。
- 凭据已解析、spawn 未发生时删除或标为需重连：修复前仍执行，修复后拒绝并保留原输入。
- 真实进程出生后、初始 stdin 前删除：修复前仍收到输入，修复后没有输入效果且原进程退役。
- SQLite trigger 拒绝删除：修复前会话绑定已丢失；修复后内存与数据库默认值均保留。
- agent backend 更新：原会话继续使用原 backend，新会话继承新的默认 backend。

七项新测试通过。模型设置、授权停止、凭据 API、绑定与动态账户授权定向组合
109项通过（添加 backend 默认值测试之前的版本）；最终候选的完整检查另行记录。
原始日志在 `.scratch/minimal-architecture-20261020/validation/ticket52/`。

Vera 对首候选指出两项 P1，均已实际复现：常驻进程在 A→B 后仍使用 A（删除 A 后
反而拒绝有效 B），以及账户禁用提交后仍提交 spawn。对应回归先失败再转绿，
两个真实进程证明旧身份已退役；授权顺序测试核对实际事务提交与 OS spawn 提交顺序。
修正后定向组合223项通过。首候选门禁在修正前停止，其部分结果不冒充新候选完成。
规范轴首候选另列的1项P2与上述 held 身份问题相同；额外两项 `env_secret` 离线协议
用例确认即使 A、B 密钥值相同，身份更换仍重建进程，删除 A 后 B 仍成功。
这两项不是 Claude 实机测试，不扩大本机 provider 验证范围。

## 最终候选与门禁

实现基线为 `1bb5152cc9bab83d58777908a1d7872b0de65503`，最终受检 tree 为
`b0904f6321d343acedca0f928c9bbb06d28b73f4`。随后只补本节验收记录，源码与测试
未再改变；用户同期调整的开发验证规则及其他已有文档工作不混入本修复提交。

| 检查 | 实际结果 |
| --- | --- |
| 完整 hermetic 后端，排除 real 与 knowledge_eval | 5110 passed、63 deselected，1277.46秒 |
| 前端 unit | 58个文件、496项通过 |
| ruff、mypy、eslint、TypeScript、生成合同、docs-index | 全部通过；mypy覆盖217个源码文件 |
| 当前可用 Codex live marker 选择 | 18 passed、5155 deselected，791.85秒 |

完整日志为上述目录的 `fast-gate-v2.log` 与 `live-codex-v2.log`；
`scripts/check.sh` 的8项检查全部通过。实机选择覆盖原生 helper 身份、跨轮恢复、
问题等待、memory、fork、research、schedule 与 Wiki；使用隔离配置和既有登录，
默认模型为 `gpt-6.1-sol/high`、正常速度。

Vera 两轴独立复核已返回：Standards 的原P2已解除，当前0项；Spec 的两项P1均已
解除，当前0项。Spec 复核 tree `14d6780e77ca51a63686ac9f5276490ade3f9490` 的
源码与最终候选一致；后续仅增加两项同密钥凭据回归和对应文档，Standards 已复核
最终 tree。评审只读复用执行方日志，没有重复运行套件。

本次未运行浏览器 E2E、Claude 实机或 opt-in 知识评测，均不计为通过；未推送、
未重启或部署服务。这是配置与授权修复的交付证据，不替代票51的整体发布门禁。

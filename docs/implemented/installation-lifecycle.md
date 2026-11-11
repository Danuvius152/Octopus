# 安装生命周期：准入、自然排空与交接

> **Status:** implemented — 安装资源归属、领域排空事实、原请求交接收据与正常退出边界。

`InstallationOwner` 持有当前安装的独占存储锁，受管配置额外绑定实际服务 owner、
资源根、host 与 port。资源 marker 证明安装归属，不能靠改 marker 接管别人的
数据。standalone 仍须通过安装锁及归属检查，但没有受管 supervisor 的原代退休
证明，不能承诺清理未知历史进程。离线操作取得同一所有权前，受管服务必须实际
退出且没有未结束 Job。

## 启动与就绪

受管启动先核验实际服务身份和旧代退出，再完成数据库当前格式、输入、会话、
fork 与 Wiki 恢复。恢复失败不开放 producer 和执行输入；数据库对象存在不表示
ready。`/health` 是存活信息；`/ready` 反映 `ServiceHealth` 的 starting／ready／
stopping／故障事实，不能用 health 200 替代业务准入。

`previous_writers_retired` 只用于原代输入／Wiki 引用恢复，不把历史 unknown 改为
未执行，也不伪造通知 ACK。原代 PID 数字、managed 标志或 session idle 均不能
代替真实退出。正常发布等自然排空；fatal 强制退出属于既有故障边界。

## 领域事实与封口收据

各原 owner 的 `drain_blockers()` 即时解释自己的接纳、执行、交付与清理责任。
安装层显式组合 HTTP/WS、会话、BG、Research、委派、Wiki worker/Registry/通知、
AppAgent/backend/bridge、各 Harness 的 one-shot、登录、scheduler 与主库事实，
不读取其私有队列、锁或第三方 executor 字段；不另建活动账本、持久 idle 状态
或通用关闭事务。
观察只说明本次阻塞，不能跨 await 当作交接许可，也不启动、取消或重绑业务。

会话包括 Owner 命令、准备与迟到 spawn、输入队列、问题、发布、退休和失败接管，
以及短锁外仍被原 owner 持有的配置/lifecycle 操作。BG/Research/委派继续保有原
启动、终态回流和保存责任，已结束但清理未确认的任务仍阻塞。失败 Owner 只有原
清理成功、原通知退出且没有保留输入时才能解除阻塞。空闲 held 模型宿主及 App
server 可留到正常 shutdown 退休，不能仅因进程存活永久等待。
当前仍有“未执行却保留 blocker”的准备失败缺口：
[Research scratch 创建失败](native-deep-research.md#6-researchmanager-async-job-like-bgdelegationsscheduler)和
[Delegation 引用循环 symlink](agent-collaboration.md#52-sub-session-lifecycle)。
因此 blocker 不是活模型或子进程的证明，也不能因数据库已有 failed 就绕过原 owner。

one-shot 在首次可能挂起前由原 Harness 登记，公共调用超时或二次取消后仍持有
迟到 spawn、原进程组和私有配置清理。只有真实退出与清理都确认才解除 blocker；
task.done() 不能代替退休成功，失败证据继续阻塞。quiesce 同步关闭各 Harness
准入，shutdown 等待原退休 future；详细责任归[Harness 合同](harness-layer.md)。

OAuth 登录从首次完成请求接管到完整凭据提交持有同一 task。等待者离开或登录
显示 cancelled 都不能释放仍在执行的 provider/提交责任；GC 不移除活 task。
quiesce 同步关闭 OAuth 登录准入，正常 shutdown 在关闭数据库之前加入原 task。
未确认退休仍保留 blocker，关闭失败按 writer 失败保留数据库，而不是清空登录表。
具体取消回执与有界等待归[凭据合同](harness-credential-reauth.md#claude-oauth-operation-ownership)。

Registry 在排出同步线程前登记原操作，覆盖尚未借用 Store 的空隙；取消等待不
释放仍在运行的线程责任。最终交接以非阻塞短 guard 保护 Registry 与 Store 借用
准入，无法取得或仍有活操作就拒绝，不在事件循环等线程退出。普通调度/借用封口
后，原 shutdown 仍拥有最后同步和资源关闭；同步结束后先观察停止意图，再决定
是否进入下一普通 reconcile 阶段，不能因新准入已关而跳过最终清理。

Wiki 通知从 outbox 读取、主库 receive、会话返回到原 ACK 全程持责。轮询 sleep、
临时失败或业务已终态不能消除未 ACK 责任；事实直接查询原通知账本，不缓存第二
份待投递状态。查询采用统一只读存储入口、零锁等待：正常 BUSY/LOCKED 拒绝交接，
IOERR/损坏等失信存储仍进入统一 fail-stop。无待交付责任的常驻接收循环不阻塞。
App bridge 的准备、文件效果、保留的失败清理及未确认 journal 同样阻塞；durable
证据存在不等于清理完成。AppAgent 响应/原执行与 backend 启停由各原 owner 报告。

HTTP 责任直到原 ASGI app 完成，发出 headers 不是完成点；被动 MCP 接收沿既有
规则处理。WS 只计正在处理输入的区间，空闲连接不阻塞。scheduler 自己观察已排出
回调及注册操作；DB 自己观察 writer 和已有独占操作，shutdown 收尾仍可使用原库。

管理员 `POST /api/installation/quiesce` 在同一事件循环同步段完成最终事实检查、
必要 producer 封口和成功收据发布，跨线程准入由上述 guard 保证。封口前 busy
返回 409，producer 保持开放，等待自然完成；不为发布取消真实工作。第 N 个 gate
失败时没有成功 marker，已经关闭的 owner 保持真实状态并负责可重复收尾，服务
不伪装普通 busy 重开。全部封口成功才设置原 `offline_request` 并返回明确的
`status=stopping` 和原 `request_id`。

同 ID 重试返回同一成功事实；不同 ID 不得冒领。`/ready` 只在成功后带原 marker；
导出的 `managed_source` 同时检查 HTTP 状态、明确 stopping 和原 ID，缺 status、
部分失败或错误 ID 都不是成功。丢响应可按原 ID 找回收据；无法确认就保持未知，
不据此停服务。收据证明安全交接资格，进程及 writer 的实际退出仍由随后正常
shutdown 与外部服务 owner 证明，不把逐域封口称为全安装原子事务。

隔离安装/HTTP/ready/导出、真实同步线程、持久通知、桥接文件及子进程故障注入
回归验证拒绝与放行；实际生产服务不作为故障注入对象。未来新增内部资源在原
owner 纳入事实与退休，新增业务域才扩展安装层的显式组合。

## 源码、使用者与验证

安装身份及锁见[service_installation.py](../../server/service_installation.py)，
准入与交接见[main.py](../../server/main.py)、
[installation_admission.py](../../server/installation_admission.py)和
[service_health.py](../../server/service_health.py)。
`tests/test_installation_drain.py`、`tests/test_installation_admission.py`、
`tests/test_installation_storage_failure.py`覆盖领域阻塞、失败封口、原请求收据、
存储失信和真实资源责任。源码核对不代表本次执行了这些测试。

离线账户导出与发布都消费同一原请求交接结果，不各自猜测安全 idle。
操作步骤分别归[账户搬迁](../backup-and-migrate.md#account-export-and-import)和
[本机发布](../local-wsl.md#自动发布)，存量布局与有限转换归
[存储合同](current-storage-format.md)。本文不维护第二套 systemd 命令或升级脚本。

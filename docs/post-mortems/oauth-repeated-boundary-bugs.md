# OAuth 在交换成功后，仍可能失去提交与清理责任

状态：已修复下列责任缺口，保留进程内恢复等适用边界。调查窗口为
2026-10-03 至 2026-11-05（Asia/Shanghai），历史代码基线为
`0a56386315548b6e9df6251166220749fc0ce4aa`。

## 摘要

Claude 登录管理器在 exchange/mint 后报告成功，凭据却仍由 router 落库；
Connector 已有刷新锁，等待者退出后仍可能丢掉轮转 token；Codex CLI 成功退出，
也不证明正式 auth 文件、凭据事务和 staging 清理均已完成。

这些缺口出现在同一操作的不同交接处。修复让原操作负责到完整提交和实际退休，
同时分别校验账户、远端身份、当前授权及配置。设备登录、callback 和 refresh 的
效果不同，各自保留生命周期，没有合成一套通用 OAuth 状态机。

## 影响与证据

Git 前后差异和回归构造表明，旧路径允许迟到结果覆盖新授权、首次绑定产生竞争、
临时故障被记为需重连，以及历史成功更新当前错误对象。工程代价是多轮修改与
相邻消费者反复验证。未取得据以确认账户接管、凭据泄露或生产数据损坏的事故日志，
也没有供应商变更的归因证据。

本篇区分模型凭据 OAuth、Connector OAuth 和 Codex 设备登录。下列测试是可复查
入口，本次文档改写没有重新登录、运行产品套件或发布。

## 关键时间线

| 日期 / 提交 | 发现或修正 |
| --- | --- |
| 10-19 `94eaa29`、10-25 `fe2200f` | 移除共享 MCP token 缓存，按当前 scope 同次解析 installation、token 与 API 目的地；设备重授权使用 staging。 |
| 10-26 `737a583`、10-28 `bbadc28` | 同 installation writer 协调之外，补首次远端身份归并及元数据/secret 原子绑定。 |
| 10-31 `1d026c2` → 11-03 `9599150` | Claude finalizing blocker 仍漏 router 落库，原任务的范围扩至完整凭据提交。 |
| 11-03 `a660fcc` → 11-04 `324a913` | Connector 先保留失败分类与原 grant，再补 waiter 退出后的轮转保存及原定义捕获。 |
| 11-04 `07b83bf` | Codex 原登录统一保有进程、auth/DB 发布及清理，status 改为只观察。 |

这些节点分别补齐不同事实。一次修复中的锁或 owner，不会自动覆盖另一条登录链路。

## 根因与修复

### 1. 管理器 success 早于完整凭据提交

`9599150^` 的 submit_code 完成 exchange/mint 后置 success，router 随后加密、
保存凭据并补 token bundle 到期字段。finalizing blocker 只覆盖前半段；HTTP 断开、
取消或 shutdown 时，后半段没有同一个原任务保有。

修复由原任务捕获不可变的账户、login、code、label 和 reauth target，负责 validate、
exchange、mint/bundle 与完整凭据事务。匹配的重复调用等同一结果，冲突参数拒绝。
committing 前取消等待实际退出，进入后保全唯一 committer；未确认取消保留 blocker。
外部授权、本地提交、HTTP 回报和资源退休分别记录。

模型凭据 refresh 也要保有原任务，并按捕获的账户、backend、认证类型、密文与
有效状态条件写回。等待期间发生 reauth、撤权或删除后，旧请求不能覆盖新授权。
选择了失效凭据不能悄悄回退宿主登录；`9e50e46` 还从进程环境和私有 settings
清除竞争认证来源，验证 CLI 真正采用所选凭据。

### 2. 同对象锁没有解决首次身份创建

两个独立 callback 可以同时查无 installation，各自生成 ID。唯一约束能拒绝
竞争者，却不能让两者都取得同一已提交安装。一次 login 的 claim 也不能解决两个
login 指向同一远端身份的归并。

修复先按 `(user_id, kind, external_id)` 协调首次身份，再按 installation 协调
writer，事务一起提交元数据和 secret。空远端身份不任意合并，相同远端身份跨内部
账户仍隔离。callback 的一次 claim、TTL、取消与绑定各自保留职责。

### 3. 刷新分类正确，仍不保证轮转结果被保存

Connector 曾广泛捕获异常并标记 needs_reconnect，把 transport、429、5xx 升级成
持久授权失效。修复保留 HTTP 状态和机器错误码：只有 400/401 的明确 invalid_grant
构成相应永久拒绝，5xx 即使正文含该字符串仍属临时失败。

资源请求另捕获 token、expiry、API base 与 grant_stamp。迟到拒绝写回比较原授权：
applied 才标记，obsolete 保留替代授权，gone 表示对象已不可用；未知写回结果单独
报告。实现见[失败分类](../../server/connectors/errors.py)与[授权快照](../../server/connectors/grants.py)。

即使分类正确，waiter 在收到轮转 token 后、保存前退出，仍会丢结果。`324a913`
让 manager 保有 refresh 到保存和退休，shield waiter，shutdown join 原任务。
保存异常保留日志并向已加入的 shutdown 传播；退休超时保留任务并使关闭失败。
custom 定义在实际请求前捕获，删除或同名重建不能把新 endpoint/client 交给旧操作。

### 4. 进程退出、正式发布和清理被当成同一个结束点

Codex readiness 必须同时有 URL/code；唤醒只结束等待。错误或取消后迟到的 rc=0
不能改写终态。reauth 在 staging 进行，失败保留旧 home、配置、绑定与原生历史。

原登录任务在实际进程组退出、材料校验后，由唯一 committer 重验账户与目标，
发布 auth 文件、保存匹配凭据并等待原 COMMIT。无人轮询 status 也能发布。
清理失败保留历史成功与目标占用，重试只补原清理，不能再次发布或复活已删除凭据。
未知 COMMIT 按存储 fail-stop 处理，不靠补查或回滚猜测结果。这不提供跨文件/DB
崩溃原子性。

### 5. 历史成功被用来更新当前对象和视图

关闭窗口、取消 HTTP 等待与取消原登录是不同动作。UI 保留原请求供 Wait again，
成功后重读当前列表，按账户和视图代际拒绝迟到回报。已删除的 credential 不能因
历史成功重新插回列表，关闭观察也不能冒充授权操作已取消。

## 为什么测试漏掉了交接

manager 测试只断言 token/success，router 测试把 submit_code stub 成成功；
两者分别有效，却绕过 exchange→mint→commit 的完整寿命。顺序绑定和同 ID 刷新
也没有制造两个首次 callback、delete/reauth 或 waiter 在保存期间退出的竞争。

| 反例与目标观察 | 回归入口 |
| --- | --- |
| 在交换、mint、save lock 和 COMMIT 中取消 waiter，检查原操作、完整 secret/expiry、冷重开及 blocker | [OAuth 操作](../../tests/test_oauth_operation.py)、[模型刷新](../../tests/test_credential_refresh.py)、[Connector manager](../../tests/test_connector_manager.py) |
| provider 等待时 reauth/delete/revoke/disable，迟到成功与失败均不得覆盖新授权 | [归属](../../tests/test_credential_reauth_ownership.py)、[grant 失败](../../tests/test_connector_grant_failures.py) |
| 两个 login 同时解析相同远端身份，secret 写失败须整体回滚 | [真实路由](../../tests/test_router_connectors.py)、[绑定数据库](../../tests/test_connectors_db.py) |
| 临时/永久故障及定义替换，核原类别、grant_stamp 和 endpoint/client | [错误分类](../../tests/test_connector_grant_failures.py)、[定义生命周期](../../tests/test_connector_custom_lifecycle.py) |
| 无 status 观察仍发布、清理失败仍占用、取消后迟到 rc=0 不成功 | [设备登录](../../tests/test_codex_login.py) |
| 宿主认证来源竞争、对象删除或切换页面后旧成功到达 | [Claude](../../tests/test_harness_claude.py)、[私有配置](../../tests/test_private_cli_settings.py)、[Harness UI](../../web/src/components/HarnessPage.test.tsx)、[Connector UI](../../web/src/components/ConnectorsPage.oauth.test.tsx) |

11 月 5 日的既有实机记录还暴露了验收目标错误：后台任务继承默认 CODEX_HOME，
测试了另一份旧登录，token_expired 不代表正在对话的账户失效。核对原进程与绑定、
传入正确既有目录后，该次最小模型调用、21 项 Codex 实机与两项浏览器验收通过，
成功轮次没有认证域请求。这是历史验收输入的纠正，不能外推为 OAuth 已无故障；
当前选择与网络核验方法见[本机测试](../local-wsl.md#本机测试)。

## 教训与适用边界

- 画出原操作到真实提交、使用端和退休的责任链。锁协调竞争者，事务保证本地提交，
  原任务保有 waiter 退出后的工作；三者不能互相替代。
- 在实际使用和写回处核权，首次创建先确定业务身份；检查行仍存在并不充分。
- 同时验证历史结果和当前对象，真实 CLI、MCP 目的地与 UI 各需自己的观察点。

callback state/TTL/claim 不提供发起浏览器 cookie 绑定；本地 disconnect 不保证
provider grant 撤销。pending login 与这些操作责任属于进程内状态，不承诺重启、
多进程共享或 SIGKILL 后跨文件/DB 原子恢复。未收到的轮转响应仍可能有未知远端
效果，不自动重试轮转。

当前规则由[Connector 合同](../implemented/connectors.md)、
[凭据刷新与发布](../implemented/harness-credential-reauth.md)和
[Harness](../implemented/harness-layer.md)维护；产品缺口见[账户计划](../plans/multi-tenancy-gaps.md)。
历史断点可用 `git show 9599150^:server/oauth_login.py` 与同版 router、测试复查。

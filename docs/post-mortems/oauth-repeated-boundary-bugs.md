# OAuth 连续修复中的责任边界遗漏

状态：历史复盘。整合至 2026-11-05 的已知修复、剩余边界与验证经验；不代表所有
OAuth 问题已关闭或当前部署已验收。

## 摘要

近一个月，OAuth 相关修复从调用账户、重授权目标，延伸到刷新、首次绑定、取消、
数据库提交、进程退役和前端迟到回报。所查样本主要是同一责任链上不同缺口被逐次
发现，不能一概解释为“同一个 bug 修了又被改回来”。共同原因是：
**修复围绕一个函数、状态或一种并发，正确性却要求原操作在身份、权限、提交和退役之间连续成立。**

Claude 登录的 `finalizing` blocker 没有自动覆盖 router 的落库；Connector 的
installation 锁没有自动覆盖首次身份创建、waiter 退出后的轮转保存或定义替换；
Codex 的成功退出也没有自动证明正式凭据已发布、staging 已清理。把操作交还原 owner
后，还必须分别核对权限、失败分类、持久化效果、历史结果和当前对象，才能形成
消费者可使用的保证。

## 调查范围与证据

调查窗口为 **2026-10-03 至 2026-11-05（Asia/Shanghai）**，整理代码基线为
`0a56386315548b6e9df6251166220749fc0ce4aa`。核对相关路径的 Git 历史、前后差异、
所属合同与回归源码，不只搜索提交标题中的 OAuth。

这里有三类需要分别验证的链路：

- **模型凭据 OAuth**：Claude 授权码交换、API key mint 或 token bundle、凭据提交、
  主动刷新、CLI 实际采用的认证来源。
- **Connector OAuth**：第三方 callback、远端身份查询、账户所属 installation 绑定、
  secret 写入、后续 MCP 调用与刷新。
- **Codex 设备登录**：设备 URL/code、用户授权、CLI 进程退出、`auth.json` 发布及
  credential 行保存；它与前两者共享部分责任问题，但有独立进程和文件生命周期。

本文没有生产事故日志或外部供应商复现回执，不能据此声称发生账户接管、凭据泄露、
生产数据损坏，或归因于供应商近期变更。本次是文档整理，没有重新登录、运行产品
套件或发布；下列测试说明覆盖方式，既有实机记录也不构成本轮新增通过声明。

## 关键修改链

| 日期 / 提交 | 修正的行为 | 前一保证仍漏掉的事实 |
| --- | --- | --- |
| 10-05 `3f8bd4a`、`88d2284` | 账户化设计提出“一次调用、一个 owner、一个 installation”；这是设计记录，日期按上海时区换算。 | 文档提出 owner，不证明每个使用点和提交点保持同一归属。 |
| 10-14 `7449f12` | Codex login 在等待 spawn 前登记原任务，关闭接纳后确认进程退出，未确认则保留记录并报错。 | 设备登录的进程 owner 不自动拥有另一登录链路的 DB commit；发 kill 或清 map 也不代表退休。 |
| 10-19 `94eaa29` | Connector 去掉共享 MCP context 的 token 缓存；模型凭据 reauth 校验目标账户，写入时再核归属。 | 当前 installation、API 目的地及网络返回后的权限仍需在实际使用处确认。 |
| 10-25 `fe2200f` | Connector 按当前 scope 解析 installation，custom token 与 API base 同次读取；Codex reauth 使用 staging home，并串行化 status 持久化。 | token 正确不等于目的地正确；重授权失败清理不能删除旧授权、配置和原生资料；status 串行化仍不等于原登录拥有发布。 |
| 10-26 `737a583` | Connector refresh、更新、删除和 reauth 使用同 installation 锁；DB 同事务确认对象存在。显式模型凭据失效停止回退宿主登录。 | 同 ID 的 writer 协调不覆盖首次创建；没有选择凭据和选择的凭据失效也不是同一身份语义。 |
| 10-27 `18fe07f` | 模型凭据 refresh 由 SessionManager 保有，按捕获版本条件写回，分类临时失败与明确失效，并限定完整响应总时限。 | refresh 的完整 owner 不自动覆盖 login completion，也不自动迁入 Connector 的另一条实现。 |
| 10-27 `b19f6f9`、10-28 `bbadc28` | callback 一次 claim、TTL 限制接纳、取消与绑定协调；首次绑定按远端身份键再按 installation 锁协调，元数据与 secret 同事务提交。 | 同 login 去重、首次身份归并和本地原子提交解决不同问题，不能互相替代。 |
| 10-30 `9e50e46` | 显式 Claude 凭据覆盖进程环境及私有 settings，清除竞争认证来源，文件寿命随实际进程管理。 | token 存取正确不证明 CLI 重读宿主配置后仍采用所选凭据。 |
| 10-31 `1d026c2`、11-03 `9599150` | 先为 Claude `finalizing` 加 blocker，再让原任务覆盖 validate、exchange、mint 和完整凭据提交；固定请求，重复等待同一结果。 | 状态 blocker 漏掉 router 后续落库；HTTP 等待结束和窗口关闭不证明原操作取消。 |
| 11-03 `983ff39` | Codex readiness 同时要求 URL 和 code；错误／取消后迟到成功退出不能改写终态。 | 唤醒、单个字段或 `rc=0` 不足以证明当前阶段成功。 |
| 11-03 `a660fcc` | Connector 保留 HTTP/status/error 分类；临时失败不自动要求重连，资源拒绝按原 grant stamp 条件写回。 | 旧拒绝不能污染新授权；正确分类仍不保有 waiter 退出后的轮转结果。 |
| 11-04 `324a913` | Connector manager 保有 refresh 到结果保存及退休，shield waiter、shutdown join；custom 定义替换不向旧操作提供新配置。 | 一次外部请求、原配置与完整保存必须同时成立，单次调用计数不足以证明。 |
| 11-04 `07b83bf` | Codex 原登录保有 CLI、正式 auth 文件／凭据事务发布及清理；status 只读，同目标占用保留到实际退休。 | 已提交成功不等于清理结束，重试清理不能重新发布或复活已删除凭据。 |

这些修改的工程代价是同一领域多轮增加协调和回归，反复重审相邻消费者。“上次
修过 OAuth”无法回答这次究竟保证了哪一段行为。代码支持身份混用、迟到覆盖、
安装归并失败、错误重连、取消与效果不一致等后果判断，不证明它们全部在线上发生。

## 具体反例与根因

### 1. 登录成功停在组件出口，实际提交还没有 owner

`9599150^` 的 `OAuthLoginManager.submit_code` 在 exchange/mint 后置 success，
router 再加密、保存凭据并补 token bundle 的到期字段。manager 的成功早于完整
存储，cancel 只改状态，`1d026c2` 的 blocker 也只罩住 finalizing。因此“已有状态机”
和“已经加 drain”仍不能回答断连或 shutdown 后由谁完成剩余写入。

修复让原任务保有不可变的账户、login、code、label 和 reauth target，覆盖 validate、
exchange、mint 或 bundle 及完整凭据事务；router 不再补写。匹配的重复调用等待
同一结果，参数冲突拒绝。进入 committing 前取消撤销后续阶段并等待实际退出，
进入后保全唯一 committer。取消未确认仍保留 owner/blocker，不把发出取消解释为
没有效果。外部授权、本地提交、HTTP 回报和资源退休分别有证据。

### 2. 一次入口检查和一把对象锁，没有覆盖身份的变化与首次创建

OAuth 至少涉及登录操作、内部账户、远端账户和本地 credential/installation。
它们在首次绑定、reauth 和迟到 refresh 时并不总是一一对应。共享 MCP context 的
token cache 撤掉后，仍需从本次 scope 同时确定 installation、token 与 API 目的地；
显式失效的凭据也不能悄悄回退到宿主登录。

Connector 已有同 installation 的 refresh 锁，`737a583` 还要把 delete/reauth 纳入；
`bbadc28` 又补首次绑定，因为两个 login 可同时查无记录，各选新 ID 插入。唯一约束
可以拒绝竞争者，却不能让两者都得到同一已提交安装。远端身份键决定归并对象，
installation 锁协调该对象的 writer，事务保证身份、元数据和 secret 一起提交。
首次归并使用 `(user_id, kind, external_id)`；空远端身份不随意合并，同一远端身份
跨内部账户仍须隔离。

权限还会在 provider 等待期间改变。模型凭据按捕获的账户、backend、认证类型、
密文和有效状态条件写回；旧请求即使正常结束，也未必有权覆盖新 reauth 或撤权。
只在入口核对，或只检查“这行还在”，都不足以证明迟到回报仍可写。

### 3. 刷新必须保留失败类别、原授权、原配置和操作寿命

模型凭据刷新在 `18fe07f` 已区分临时与永久失败，但 Connector 的旧实现仍广泛
捕获异常并标记 needs_reconnect。共同名称没有让一个链路的保证自动迁入另一个。
`a660fcc` 保留 provider HTTP 状态和机器错误码：transport、429、5xx 属临时失败，
即使 5xx 正文含 invalid_grant 也不改变分类；只有 400/401 的明确 invalid_grant
构成相应永久 grant 拒绝。临时不可用不能升级为持久授权失效。

资源请求的迟到 401 另有原授权快照。token、expiry、API base 与 grant_stamp 同次
捕获，拒绝写回事务比较当前账户、installation 和材料：applied 才标记当前授权，
obsolete 保留替代授权，gone 表示对象不再可用。未知标记结果也有独立说明，不能
用一次旧拒绝诱导当前授权重连。证据见[错误分类](../../server/connectors/errors.py)、
[授权快照](../../server/connectors/grants.py)与[分类回归](../../tests/test_connector_grant_failures.py)。

即使分类正确，waiter 在 provider 返回轮转 token 后、保存前取消，仍可能丢失已收到
的结果。`324a913` 由 manager 保有原 refresh 到保存和退休，等待者被 shield，
shutdown join 原任务。保存失败由完成回调记录异常并移除已结束任务；已加入等待的
shutdown 传播非预期失败。退休超时则保留未完成任务并使关闭失败。custom 定义删除
或同名重建也不能让旧操作改用新的 endpoint/client 配置，原配置在实际操作前捕获。

[轮转回归](../../tests/test_connector_manager.py)在响应前、save lock 和 COMMIT 等
阶段取消 waiter，重开 SQLite 检查原 grant；[定义替换回归](../../tests/test_connector_custom_lifecycle.py)
在远端等待期间换定义，检查旧操作仍使用原配置。一次外部调用、正确的原授权与
完整的后续提交必须一起成立；单看 refresh 只调用一次不能证明这些保证。

### 4. 设备登录退出、凭据发布和真实清理是不同完成点

Codex readiness 必须有完整 URL/code，唤醒事件只结束等待，不自动给出成功。
发生错误或取消后，迟到 `rc=0` 不能改写原终态。reauth 使用 staging，失败保全旧
home、配置、绑定和原生历史。

`07b83bf` 将 CLI、正式 auth 文件／凭据事务发布及清理统一交给原登录任务。
登录前保有原账户、目标和目录，实际进程组退出及材料校验后，由唯一 committer
重验当前账户与目标，替换 auth 文件、保存匹配元数据并等待原 COMMIT。无人轮询
status 也能发布；status 只观察历史结果和当前可用对象，不再承担持久化。

同一目标在原任务和 staging 清理确认前继续占用。已提交后的清理失败保留历史成功
和 blocker，重试只补原清理；取消观察者不取消 committer，删除则与原发布排序。
未知 COMMIT 沿存储 fail-stop，不补查猜测提交、不再发布，也不擅自回滚旧文件。
这不是跨文件／DB、跨进程崩溃的原子性承诺。

[原登录回归](../../tests/test_codex_login.py)分别验证无人观察仍发布、提交等待中
取消观察者、与删除排序、清理失败仍保留占用和旧 auth 保全。这些保证不能倒推为
早期已有，也不能从一次成功退出外推到全部资源都已退休。

### 5. 历史结果与当前对象、观察视图混在一起

HTTP 断开、弹窗关闭、显式取消和登录完成是不同动作。原操作成功也不保证对应
credential 现在还存在。UI 保存原请求供 Wait again，消费明确取消结果，成功后
重读当前列表，并按账户／视图代际拒绝迟到回报。已删除对象不能因历史成功重新
插回列表；关闭观察也不能被理解成取消授权操作。

这些问题的共同根因是验收停在一段责任：一次 claim 不涵盖两个 login 的首次绑定，
两次 refresh 串行不涵盖 delete/reauth，token 返回不涵盖提交，URL 出现不涵盖设备码，
历史 success 不涵盖当前列表。应把经验变成各链路共同回答的验证问题，不能强行把
设备登录、callback 和 refresh 塞入同一个通用状态机。

## 为什么原测试没有识别这些交错

问题并非完全没有真实 DB、并发或前端测试，而是它们之间留下了责任缝。

- `9599150^` 的 manager 测试断言 token 与 success；router 测试使用真实 DB，
  却把 submit_code stub 成成功。它们分别测交换和存储，未验证 exchange→mint→commit
  在断连、取消和 shutdown 中始终由原任务保有；旧取消用例只取消尚未提交 code 的登录。
- 同 ID 的并发 refresh 不覆盖删除或 reauth；顺序 upsert 不覆盖两个首次 callback；
  单 login 重复 claim 不覆盖两个 login 解析到同一远端身份。
- manager 状态、provider 返回、唤醒和窗口关闭，都可能早于完整提交、权威取消结果
  或进程退休。断言这些邻近事件，不能证明目标效果。
- 模拟 HTTP 的 UI 用例能证明界面消费回执，后端竞争仍需真实路由、阶段屏障和临时
  SQLite 验证；反过来，后端正确也不证明旧响应不会更新新账户或新弹窗。

| 要推翻的假设 | 交错与目标事实 | 代表性回归入口 |
| --- | --- | --- |
| waiter 退出就是操作结束 | 在 exchange、mint、保存及 COMMIT 等阶段取消观察者，检查同一次操作、完整 secret/expiry 和实际 blocker。 | [test_oauth_operation.py](../../tests/test_oauth_operation.py)、[test_credential_refresh.py](../../tests/test_credential_refresh.py)、[test_connector_manager.py](../../tests/test_connector_manager.py) |
| 旧请求仍有写权限 | provider 等待时 reauth/delete/revoke/disable/改变 owner，迟到成功和失败都不能覆盖当前授权。 | [test_credential_reauth_ownership.py](../../tests/test_credential_reauth_ownership.py)、[test_connector_grant_failures.py](../../tests/test_connector_grant_failures.py) |
| 单 callback 正确就保证唯一安装 | 两个独立 login 同时解析同一远端身份，并注入 secret 写入失败，检查同一安装与整体回滚。 | [test_router_connectors.py](../../tests/test_router_connectors.py)、[test_connectors_db.py](../../tests/test_connectors_db.py) |
| 失败就应重连，定义替换与旧请求无关 | 注入 429/5xx/transport、400/401 invalid_grant、迟到资源拒绝及定义替换，核原类别、原 stamp 和原 endpoint/client。 | [test_connector_grant_failures.py](../../tests/test_connector_grant_failures.py)、[test_connector_custom_lifecycle.py](../../tests/test_connector_custom_lifecycle.py) |
| status、进程退出或 success 代表全部结束 | 无 status 观察完成发布，取消提交观察者、与删除排序、staging 清理失败，核唯一发布、历史成功和原占用。 | [test_codex_login.py](../../tests/test_codex_login.py) |
| 邻近事件或环境配置足以证明当前认证 | 仅 URL／仅 code、超时后 rc=0、宿主竞争 key/token/helper/header、reauth fail/cancel，核完整阶段及实际认证来源。 | [test_codex_login.py](../../tests/test_codex_login.py)、[test_harness_claude.py](../../tests/test_harness_claude.py)、[test_private_cli_settings.py](../../tests/test_private_cli_settings.py) |
| 历史回报可以更新当前界面 | 成功后删对象再等，切换账户／弹窗，检查不复活对象、不修改新视图。 | [HarnessPage.test.tsx](../../web/src/components/HarnessPage.test.tsx)、[ConnectorsPage.oauth.test.tsx](../../web/src/components/ConnectorsPage.oauth.test.tsx) |

这些证据不证明每个旧版本的全套测试都曾跑绿，也不要求每次修复重跑所有 OAuth
链路。按本次改变的责任选择反例，复用仍有效证据，具体遵循[测试政策](../testing.md)。

### 实机验收也必须观察原凭据

11 月 5 日的既有验收记录中，后台任务继承服务默认 CODEX_HOME，与正在正常对话
的凭据绑定不同。默认旧登录返回 token_expired，不能据此要求当前账户重新授权。
核对原进程和 agent 绑定、显式传入当前既有目录后，该次记录中的最小模型调用、
21 项 Codex 实机和两项浏览器验收通过，成功轮次没有认证域请求。

这次纠正的是测试启动输入；只读接口 200 只能作为诊断线索，真实模型及网络审计
才补齐相应验证。本文复用既有记录，没有在本轮重复实机，也不从一次通过推出
OAuth 或远端撤销问题已消失。当前目录选择与网络核验规则归[本机测试](../local-wsl.md#本机测试)。

## 可迁移的开发经验

1. **先写完整责任链。** 列出操作、账户、远端身份、本地对象、唯一 writer、实际
   commit、使用端及退休条件；新发现 writer 或提交点时先修正模型，状态名不能
   替代责任范围。
2. **在真正使用与写回处核权。** provider 请求前确认授权，返回后用事务或条件更新
   处理 reauth、删除、撤权和账户失效；首次创建还要先找到正确的业务身份。
3. **分开锁、事务和操作保有。** 锁协调竞争者，事务保证本地提交，原 owner 保有
   waiter 退出后的效果。三者各有用途，不能互相代替。
4. **保留效果与失败的真实边界。** 临时失败、明确失效和未知效果分别判断；取消
   不撤销已发出的外部效果，未知 COMMIT 不靠重试猜测，历史成功不复活已删除对象。
5. **验证真实消费者和最难交错。** 核 CLI 认证优先级、MCP 当前 scope、custom API
   目的地、UI 当前视图和恢复端；检查原操作次数、完整持久效果及实际退休，不能
   只断言函数返回、中间状态或一份通过数。

## 仍存边界与关联

Connector 的临时／永久失败分类、原 refresh 保有及定义替换已有上述修复，不再
作为当前未完成项。仍需保留以下适用边界：

- callback 的 state、TTL 和一次 claim 不提供发起浏览器 cookie 绑定；disconnect
  删除本地安装不保证 provider grant 已撤销。
- pending login 和上述操作责任属于进程内状态，不承诺重启恢复、多进程共享或
  SIGKILL／存储 fail-stop 后跨文件与 DB 的原子恢复。当前进程未收到的 provider
  轮转响应仍可能有未知远端效果，不自动重试轮转。
- Codex 设备登录、Claude OAuth 与 Connector API 的实机结果不能相互替代。
  没有事故证据或部署需求时，不能把这些边界写成已发生攻击或本轮修复任务。

当前行为和产品选择继续由所属文档维护，本文保留因果与回归观察点：

- [Connector 合同](../implemented/connectors.md)、[配置指南](../connectors-setup.md)、
  [账户计划](../plans/multi-tenancy-gaps.md)。
- [凭据刷新与发布](../implemented/harness-credential-reauth.md)、
  [Harness 登录边界](../implemented/harness-layer.md)。
- [登录操作](../../server/oauth_login.py)、[凭据 writer](../../server/oauth_credentials.py)、
  [模型 refresh owner](../../server/sessions/credentials.py)、[条件写回](../../server/db/credentials.py)。
- [Connector manager](../../server/connector_manager.py)、
  [callback 与取消](../../server/routers/connectors.py)、[绑定事务](../../server/db/connectors.py)、
  [设备登录](../../server/codex_login.py)。
- [SessionOwner 责任范围复盘](session-owner-scope-creep.md)：相同的责任连续性问题，
  OAuth 还需额外检查首次身份、认证来源及跨链路保证不能外推。

复查历史可运行 `git show <上表提交> -- <相关路径>`。例如
`git show 9599150^:server/oauth_login.py`、
`git show 9599150^:server/routers/credentials.py` 及对应旧测试，能重建
“manager 已成功、router 仍在提交”的责任断点。

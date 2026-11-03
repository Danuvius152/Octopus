# OAuth 连续修复中的责任边界遗漏（2026-11-03）

状态：历史复盘。下列缺陷已有对应修复，仍存边界单独列出；不代表所有 OAuth 问题已关闭或当前部署已验收。

## 摘要

近一个月，OAuth 相关修复从调用账户、重授权目标，延伸到刷新、首次绑定、取消、数据库
提交、进程退役和前端迟到回报。所查样本主要是同一责任链上不同缺口被逐次发现，不能
一概解释为“同一个 bug 修了又被改回来”。共同原因是：**修复围绕一个函数、一个状态或
一种并发，正确性却要求整条操作在身份、权限、提交和退役之间连续成立。**

最直接的例子是 Claude 登录：10 月 31 日已经把 `finalizing` 列为 drain blocker，
11 月 3 日仍需把实际数据库提交收回原登录任务。此前“token 已拿到”的成功、HTTP
请求的寿命和“凭据已完整落库”的完成并不是同一个事实。测试分别证明了各层，交接处
却没有被一起验证。

## 概述

调查窗口为 **2026-10-03 至 2026-11-03（Asia/Shanghai）**，沿本地 `main` 历史检查
相关路径及其消费者，不只按提交标题搜索 `OAuth`。最终代码基线为
`6ab383ac2b1f5adc784ef1e7e72c3a756b67e332`；分析期间从 `7a830e0` 新增的该提交
未修改本文涉及的 OAuth、Connector、登录 UI 及其回归测试路径。

这里有三类需要分别验证的链路：

- **模型凭据 OAuth**：Claude 授权码交换、API key mint 或 token bundle、凭据提交、
  主动刷新、CLI 实际采用的认证来源。
- **Connector OAuth**：第三方 callback、远端身份查询、账户所属 installation 绑定、
  secret 写入、后续 MCP 调用与刷新。
- **Codex 设备登录**：CLI 输出设备 URL/code、用户授权、进程退出、`auth.json` 发布与
  credential 行保存。它与前两者共享部分责任问题，但有独立进程和文件生命周期。

证据来自提交前后代码、现行合同和回归测试源码。本文没有生产事故日志或外部供应商
复现回执，不能据此声称出现过实际账户接管、凭据泄露、生产数据损坏，或归因于供应商
近期变更。本次是代码与历史调查，没有重跑产品套件或发起真实 OAuth 登录；文中测试名
说明覆盖了什么，不构成该版本本轮执行通过的声明。

## 时间线

以下为能解释反复修补的关键节点，并非所有碰到相关文件的提交。

| 日期 / 提交 | 修正的行为 | 前一保证仍漏掉的事实 |
| --- | --- | --- |
| 10-05 `3f8bd4a`、`88d2284` | 账户化设计提出“一次调用、一个 owner、一个 installation”。这是设计记录，日期按上海时区换算。 | 文档提出 owner，并不证明运行时代码已在每个使用点、提交点保持同一归属。 |
| 10-14 `7449f12` | Codex login 在等待 spawn 前登记原任务，关闭接纳后确认进程退出，未确认则保留记录并报错。 | 已给设备登录进程建立 owner，并不代表 Claude exchange 到 DB commit 的另一链路也有完整 owner；发 kill 或清 map 不能代表退役。 |
| 10-19 `94eaa29` | Connector 去掉共享 MCP context 的 token 缓存；模型凭据 reauth 校验目标账户，并在存储写入时再核归属。 | 缓存撤掉后，调用的 installation 和 API 目的地仍需取自当前 scope；请求开始时的 owner 检查也不能代表网络返回后的权限。 |
| 10-25 `fe2200f` | Connector 按当前请求 scope 解析 installation，custom 的 token 与 `api_base` 同次读取；Codex reauth 改用 staging home，并串行化 status 持久化。 | token 正确不等于目标地址正确；重新登录失败的清理不能删除旧授权、配置和原生会话。 |
| 10-26 `737a583` | Connector refresh、更新、删除、重授权使用同 installation 锁；DB 在同事务中确认 installation 存在，避免删除后留下孤立 secret。模型凭据显式绑定失效时报错，停止回退宿主登录。 | “两个 refresh 只执行一次”没有涵盖其他 writer；首次绑定还没有既有 installation ID。“没有选择凭据”与“选择的凭据失效”也不能共用默认身份语义。 |
| 10-27 `18fe07f` | 模型凭据 refresh 由 SessionManager 保有，等待者取消不撤销刷新；按捕获的凭据版本条件写回，分类临时失败与明确失效，限定完整响应总时限。 | 旧 provider 回报不能覆盖新 reauth、删除或撤权；网络错误不能直接变成持久化的“必须重连”。这次修的是 refresh，不自动覆盖 login completion。 |
| 10-27 `b19f6f9` | Connector callback 一次 claim，TTL 限制接纳；取消与本地绑定协调，提交后返回权威结果；UI 关闭等待取消竞争的结果。 | 一个 login 只处理一次，不保证两个独立 login 首次绑定同一远端身份时得到同一 installation。 |
| 10-28 `bbadc28` | 首次绑定先按 `(user_id, kind, external_id)` 协调，再用 installation 锁；元数据、身份选择与 secret 在同事务提交；refresh 写回核当前账户可用性。 | 旧 installation 锁未覆盖首次创建；即使分别给新 ID 加锁，也不足以协调同远端身份。事务原子性与身份串行化承担不同责任。 |
| 10-30 `9e50e46` | 显式 Claude 凭据同时覆盖进程环境和私有 settings，清除竞争认证来源，文件由实际进程生命周期管理。 | 设置一个环境变量，不证明 CLI 在重读宿主配置后仍使用所选凭据；token 存取正确也不证明使用端正确。 |
| 10-31 `1d026c2` | Claude OAuth 增加 `finalizing` drain blocker。 | blocker 只跟踪一个状态；token 返回后由 router 继续落库，shutdown 仍只清登录记录，完整操作没有唯一保有者。 |
| 11-03 `9599150` | Claude 原登录任务覆盖 validate、exchange、mint 和完整凭据提交；固定原请求，重复等待同一结果；明确取消提交边界，UI 按账户/视图处理迟到回报。 | HTTP 等待结束不代表操作结束，关闭弹窗不代表取消；历史成功结果也不能重新插回已删除的凭据列表。 |
| 11-03 `983ff39` | Codex readiness 必须同时具备 URL 和 code；错误/取消后迟到的成功退出不能改写终态。 | 唤醒事件、URL 已出现、进程 `rc=0` 都各自不足以证明当前登录阶段成功。 |

## 影响

Git 和回归用例能支持的影响，是这些旧路径存在以下错误结果的可能及对应修复：

- 共享上下文或旧请求身份绕过当前调用范围；正确 token 与错误 API 目的地组合。
- refresh 的迟到成功/失败覆盖新授权，或临时 provider 故障把可重试凭据标成需要重连。
- 首次并发绑定无法可靠归并为同一 installation，元数据与 secret 的写入边界不完整。
- 取消、断连、shutdown 与 exchange、落库或进程退出交错时，返回状态与实际效果不一致。
- Codex 重授权失败清理伤及旧 home；旧 UI 回报更新新视图，或历史成功掩盖当前对象已删除。

这些是代码缺陷的后果范围，不能当作全部已经在生产发生。确定的工程代价是：同一领域
多轮增加协调和回归用例，反复重审相邻消费者；“上次修过 OAuth”无法回答这次究竟
保证了哪一段行为。

## 根因

### 根因 #1：完成点停在组件出口，没有覆盖完整业务效果

`9599150^` 的 `OAuthLoginManager.submit_code` 在 exchange/mint 后置 `success`，
router 接着加密、保存凭据，再补 token bundle 的到期字段。manager 的成功先于存储
完成；cancel 只改登录状态，不能完整支配正在执行的 provider 工作和 router 后续写入。
`1d026c2` 新加的 drain blocker 因而也只罩住 `finalizing`，没有罩住后续提交。

这解释了为什么“已有登录状态机”“已经加 drain”仍会出新问题：被管理的对象只是登录
的一段。`9599150` 的关键变化是原任务一直负责到完整提交，router 不再补写；进入
`committing` 前取消可以撤销后续阶段，进入后则保全唯一 committer 并等待真实结果。
外部授权已发生、本地写入成功、HTTP 回报到达和资源退役分别保留自己的证据。

### 根因 #2：身份与权限被当成入口检查，没有贯穿等待后的使用和提交

多账户之后，一个 ID、一个进程级 MCP 实例或一次成功校验，都不足以代表后续的当前
权限。这里至少有四个身份：登录操作、内部账户、远端账户、本地 credential/installation。
它们在首次绑定、重新授权和迟到 refresh 时并不总是一一对应。

`94eaa29` 去 token cache 后，`fe2200f` 仍要修当前 scope 与 custom API 地址；
`18fe07f` 则将 refresh 的写回条件扩到所捕获的账户、backend、认证类型、密文及有效
状态。旧 refresh 可以正常结束，却已无权覆盖现在的 secret。修复落在实际消费或写入
边界，才能挡住“检查过、随后变化”的路径。

### 根因 #3：锁的对象和事务范围没有对应到真实竞争

Connector 原先已能让同 installation 的两个 refresh 串行。`737a583` 把其他修改者
纳入同锁，处理 refresh 对 delete/reauth 的竞争；`bbadc28` 又补首次绑定，因为旧路径
查无记录后直接生成新 ID 并插入，没有获取 installation 锁。两个 login 可同时查无记录，
各选新 ID 直接插入；唯一约束可以拒绝竞争者，却不能让两者都取得同一已提交安装。

这两次修复并不互相替代。远端身份键决定应该归并到哪个 installation，installation
锁协调这个对象的修改；数据库事务保证身份、元数据和 secret 一起提交。只写“加锁”
或“事务化”，没有列出所有 writer 和首次创建路径，就会在另一种竞争上继续补洞。

### 根因 #4：观察者寿命、操作寿命与当前可用性混在同一个结果里

HTTP 断开、弹窗关闭、显式取消和登录完成，是不同动作。refresh 已在 `18fe07f` 保证
等待者退出后操作仍有 owner；Claude login 直到 `9599150` 才获得对应的完整保证。
Codex 还有单独的进程输出、设备码准备、授权窗口、文件发布和 status 持久化。

同样，原操作成功是历史事实，不保证对应 credential 现在仍存在。新 Claude UI 保存
原请求用于 **Wait again**，消费明确的取消结果，成功后重新读取当前列表，并用账户和
视图代际阻止旧回报更新新界面。Codex readiness 修复则说明：一个邻近事件可以唤醒
观察者，但不能替代阶段的完成条件。

### 根因 #5：修复验收没有系统覆盖相邻操作，经验未自动跨链路成立

从变更顺序看，修复多次围绕已经暴露的一种交错展开：同 ID refresh、单 callback claim、
等待 token、等待 URL。下一轮才补另一位 writer、首次身份、实际落库或完整设备码。
据此可以推断，验收模型对责任交接的覆盖不足；仅凭 Git 不能进一步断言每次开发和
审查时实际采用了什么思考流程。

同名领域也容易掩盖差异：模型凭据 refresh 已分类临时/永久错误，Connector refresh
当前仍广泛捕获异常并标 `needs_reconnect`。一个链路的保证没有自动扩散到另一个。
经验应转成共享的验证问题，各链路依实际责任回答；不能因为名字都叫 OAuth，就强行
把设备登录、callback 和 token refresh 塞进一个通用状态机。

## 为什么测试没有捕获（真正的失败）

问题并非此前完全没有真实 DB、并发或前端测试，而是测试之间留下了责任缝。

1. **分层都测，交接被 mock 掉。** `9599150^` 的
   `test_oauth_login.py::test_submit_code_happy_path` 断言 manager 返回 token 与
   `success`；`test_credentials_api.py::test_oauth_complete_persists_credential`
   用真实 DB，却把 `submit_code` stub 成成功。它们分别验证交换和存储，无法证明
   exchange→mint→commit 在断连、取消、shutdown 中始终由原任务负责。旧
   `test_cancel_is_idempotent` 只取消尚未提交 code 的登录。
2. **测了一种并发，外推到另一种。** 旧 `test_concurrent_token_refreshes_once`
   不覆盖删除或 reauth；顺序的 `test_complete_install_upserts_same_account`
   不覆盖两个首次 callback；一个 login 的重复 claim 也不覆盖两个 login 解析出同一
   外部身份。`737a583`、`b19f6f9`、`bbadc28` 分别增加这些不同的交错。
3. **观察点早于目标事实。** manager 状态、provider 返回、唤醒事件、窗口关闭，都可能
   先于完整提交、明确取消结果或进程退役。`9599150` 的新操作测试在真实 HTTP→SQLite
   链路中阻塞 exchange、mint 和 save，分别断言操作次数、数据库效果、返回结果与
   drain blocker；`983ff39` 对设备输出不完整及迟到退出建立了反例。
4. **UI 与后端证据被混用。** `ConnectorsPage.oauth.test.tsx` 和
   `connector-cancellation.spec.ts` 能证明 UI 消费 `success/cancelled` 回执；其 HTTP
   被模拟，后端竞争要由 `test_router_connectors.py` 的阶段屏障另行验证。反过来，
   后端正确也不证明旧响应不会污染新账户或新弹窗。

这些证据说明旧测试不足以识别对应故障。它们不能单独证明每个旧版本的全套测试当时
实际跑绿；本文不把新增测试数量或某次历史通过数当作 OAuth 完整正确性的证据。

## 防护措施

后续 OAuth 修复的验收应回答以下问题，并按实际变更选择能推翻设计的用例。

1. **先画完整责任链。** 写清操作身份、账户、远端身份、本地对象、唯一 writer、实际
   commit 点、使用端及退役条件。登录成功必须说明成功到了哪一层；新发现另一位
   writer 或另一个提交点时，先修正责任模型，再补机制。
2. **逐个检查等待后的权力。** provider 请求前查当前授权，写回时用事务或条件更新
   核对当前身份与版本；列出删除、reauth、撤权、账户失效对迟到成功和失败的处理。
   只在入口查一次，或只核“这行还在”，不足以证明旧回报仍可写。
3. **区别首次身份与既有对象。** 新对象还没有 ID 时，明确用什么业务身份协调；再说明
   谁负责对象级串行化、谁负责元数据与 secret 原子提交。保留空远端身份不能被随意
   合并、同远端身份跨内部账户仍隔离的用例。
4. **取消以效果边界为准。** 区分断连、关闭观察、显式取消和超时。进入 commit 前停止
   后续阶段并确认实际退役；进入后保全 committer。取消未确认就保留 owner/blocker，
   回报实际未知或失败，不能把“发了 cancel”写成“已经没有效果”。
5. **验证真实使用端和恢复端。** 除 token 存取，还核 CLI 认证优先级、MCP 当前 scope、
   custom API 目的地；reauth 失败保全旧授权、配置、绑定和原生资料。UI 使用历史结果
   后读取当前对象，拒绝旧账户/视图回报。
6. **测试最可能推翻当前模型的交错。** 下表给出已有回归入口；每次新增用例写明注入点、
   观察事实和旧代码为什么应失败。用事件屏障控制顺序，在公开入口与真实临时存储上
   验证结果、完整效果、远端操作次数及退役，避免只断言中间状态。

| 要推翻的假设 | 故障或交错 | 关键观察 / 已有回归入口 |
| --- | --- | --- |
| 等待者退出等于操作结束 | refresh 或 complete 期间取消 HTTP waiter，再次等待或关闭 DB | 同一次操作、完整 secret/expiry、实际 blocker；`test_credential_refresh.py`、`test_oauth_operation.py`。 |
| 旧请求仍有写权限 | provider 等待期间 reauth/delete/revoke/disable/改变 owner | 迟到成功和失败都不能覆盖当前授权；`test_late_refresh_cannot_overwrite_current_authority`、`test_credential_reauth_ownership.py`。 |
| 单 callback 正确即可保证唯一安装 | 两个独立 login 同时完成同远端身份解析；secret 写入失败 | 同一已提交 installation、metadata/secret 一致、失败回滚；`test_two_first_oauth_callbacks_commit_one_installation_and_both_succeed`、`test_authorization_secret_failure_rolls_back_metadata_and_identity`。 |
| Cancel 或成功响应代表当前对象 | exchange/mint/commit 各阶段取消，成功后删除对象再重等，切换账户/弹窗 | 权威取消结果、原任务退役、不会复活对象或修改新视图；`test_oauth_operation.py`、`HarnessPage.test.tsx`、`ConnectorsPage.oauth.test.tsx`。 |
| 收到邻近事件即阶段完成 | Codex 仅 URL、仅 code、deadline 前后到达、超时后 `rc=0` | 两字段齐备、终态不复活、原进程退出；`test_codex_login.py`。 |
| 设置凭据就会采用该凭据 | 宿主同时配置 key/token/helper/header；reauth fail/cancel | 实际调用配置选中原凭据，旧 home/绑定仍可用；`test_harness_claude.py`、`test_private_cli_settings.py`、`test_codex_login.py`。 |

这些是修复与评审的问题集，不要求每次重跑所有 OAuth 链路、增加重试层或引入分布式
协调。验证范围和证据复用仍遵循[测试政策](../testing.md)。

## 经验教训

- “已经有 owner”要追问它是否一直拥有到真实提交和退役；状态名不能替代责任范围。
- 一个局部修复成立，不会自动证明相邻 writer、首次创建、另一登录链路和实际消费者。
- 锁、事务、条件写回解决不同问题；明确竞争身份和提交事实后才能判断是否完整。
- 失败分类会改变持久化授权状态。暂时不可用与明确失效必须有不同的证据门槛。
- 绿测试的价值取决于它是否能制造目标交错，并在真正的效果边界观察结果。
- 历史成功、当前可用、通知送达和资源退出各有自己的真相；重试观察不能重做外部效果，
  也不能复活已经删除的本地对象。

## 仍存边界与关联

截至上述基线，以下内容不能被本复盘当成已经关闭的历史缺陷：

- **Connector refresh 分类仍未对齐模型凭据刷新。**
  `ConnectorManager.get_access_token` 仍把 provider/transport 异常广泛标为
  `needs_reconnect`，临时 timeout/429/5xx 也可能设置该标记；这是现代码和现合同都
  承认的剩余行为，改进范围归[账户计划](../plans/multi-tenancy-gaps.md)。
- **浏览器绑定、远端撤销和进程间恢复有独立选择。** Connector callback 的 state、TTL、
  一次 claim 不提供发起浏览器 cookie 绑定；disconnect 删除本地安装，不保证 provider
  grant 已撤销。pending login 为进程内状态，不承诺重启恢复或多进程共享。没有相应
  事故证据和部署需求时，不能把这些边界写成已发生攻击或本轮修复任务。
- **版本证据与运行验收分开。** 本文没有新增真实供应商登录或发布验收；Codex 设备登录、
  Claude OAuth 与 Connector API 的实机结果也不能互相代替。

当前行为和后续范围由原文档维护，本文保留因果与验证经验：

- [Connector 合同](../implemented/connectors.md)、[配置指南](../connectors-setup.md)。
- [凭据刷新与重新授权合同](../implemented/harness-credential-reauth.md)、
  [Harness 登录边界](../implemented/harness-layer.md)。
- [登录操作](../../server/oauth_login.py)、[凭据 writer](../../server/oauth_credentials.py)、
  [refresh owner](../../server/sessions/credentials.py)、[条件写回](../../server/db/credentials.py)。
- [Connector manager](../../server/connector_manager.py)、
  [callback 与取消](../../server/routers/connectors.py)、[绑定事务](../../server/db/connectors.py)、
  [设备登录](../../server/codex_login.py)。
- [SessionOwner 范围失控复盘](2026-10-21-session-owner-scope-creep.md)：同样需要检查
  责任连续性；本次额外暴露首次身份、认证来源与跨链路保证不能外推的问题。

复查历史可运行 `git show <上表提交> -- <相关路径>`；其中
`git show 9599150^:server/oauth_login.py`、
`git show 9599150^:server/routers/credentials.py` 及对应旧测试，能直接重建
“manager 已成功、router 仍在提交”的责任断点。

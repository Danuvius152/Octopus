# 架构复审与简化建议（2026-10-20）

> 对象：`docs/review/architecture-analysis-20261020.md`（下称"原报告"）及当前工作区
> （`main` 5d99de3 + 已暂存的 CR-03 输入恢复改动）。
> 方法：逐条对照源码核实原报告结论，再从"哪些复杂度是防 bug 必需的、哪些是
> 错误前提导致的"角度重新审视。本文只读代码，未运行测试或复现脚本；
> 每条结论标注了依据的文件位置。

## 0. 结论先行

1. 原报告的 3 个 P0 与 2 个 P1 经核实**不成立或定级过高**，其中两条"修复建议"
   照做会引入真 bug（§1）。
2. 当前实现真正的问题不在"缺少抽象层"，而在**一个贯穿全局的错误前提**：
   把进程内嵌入式 SQLite 当成"可能丢失确认的远程存储"来防御。由此衍生出
   提交后用新连接回读校验、`unconfirmed` 状态、补偿重试任务等大量分支
   （`server/` 下 `unconfirmed` 出现 110 次）。去掉这个前提，可以删除而不是
   重组相当一部分代码（§2.1）。
3. 第二个根源是**会话状态由多个协程共享、靠锁加"await 之后再核对身份"来保护**。
   改成每会话单一所有者（actor）后，大部分重新核对与手工回滚可以消失（§2.2）。
4. 原报告推荐的 Repository / Event Bus / Queue Manager / 换 PostgreSQL 抽象，
   对单人本机部署是**增加**复杂度，不建议做（§4）。
5. Wiki 在已确认的 fail-stop + 启动时终止旧进程前提下，可删除 `unconfirmed`
   结算、`execution_unknown`、取消见证和后台轮询，恢复收敛到一条启动流水线（§5）。
6. 本次复审新发现 2 个实际缺陷：已"忽略"的未知输入仍会永久阻塞会话；
   认领后、进程启动前崩溃会让会话阻塞到主机重启（§3.1）。

## 1. 原报告结论核实

| 原编号 | 原结论 | 核实结果 | 依据 |
|---|---|---|---|
| P0.1 | `claim_input` 越权读取附件元数据 | **不成立**。`claim_input` 只从派发循环与 steer 写入器内部调用（`turns.py:888`、`turns.py:2579`），参数来自已入队的服务端 `QueuedPrompt`；用户可达入口在 `ws.py:107-108` 和 `routers/sessions.py` 已校验会话归属。没有用户可控的 `request_id` 能到达它。 | `server/sessions/client_submissions.py:123` |
| P0.2 | `process_group_exited` 在 boot 不可读时应返回 `True` | **建议方向错误**。该函数的契约是"只在有证据时说已退出"。把"无法验证"改成"已退出"会在旧进程仍存活时放行新写入者，恰好制造它要防止的并发写。真实问题是另一种情况，见 §3.1。 | `server/execution_identity.py:14-26` |
| P0.3 | steer 两次提交之间崩溃导致漏检写入者 | **不成立**。崩溃时行处于 `claimed`，重启时 `recover_client_submissions` 会把所有 `claimed` 改为 `unknown`（`db/client_submissions.py:180-189`），随后被 `uncertain_submission_writers` 检出。合并成一个事务更整洁，但不是数据竞争。 | `server/sessions/turns.py:2617-2627` |
| P1.4 | `terminal_request_id` 缺索引，50 万行扫描 | **不成立**。已有唯一部分索引 `messages_terminal_request ON messages(session_id, terminal_request_id)`。 | `server/db/base.py:579-582` |
| P2.6 | 分页游标差一，应改为 `rows[limit][0]` | **不成立，且建议会漏行**。查询条件是 `rowid < before`，游标取本页最后一行 `rows[limit-1]` 正确；改成 `rows[limit]` 会跳过第 `limit+1` 行。真实问题是路由在 SQL 之后再过滤（`routers/sessions.py:388-389`），可能返回短页甚至空页但带游标。 | `server/db/client_submissions.py:191-203` |
| 耦合 | Wiki 依赖全局 `session_manager_instance` | **不成立**。`server/` 中不存在该名字；`knowledge.py` 不导入 `sessions`。实际的全局可变状态是 `server/wiki/tasks.py:387` 的 `tasks = WikiTasks()`，见 §2.3。 | grep 结果 |
| 硬编码 | `BackendType` / `JOB_KIND` 枚举 | 名称不符（实际为 `BackendKind`，未找到 `JOB_KIND`）；对两个后端而言枚举是合理的，不是问题。 | `server/models.py:17` |
| P2.9 | 附件整读进内存 | 上限 25 MB/文件（`attachments.py:29`），本机场景影响有限；可以顺手改为流式，但不是架构问题。 | — |

原报告其余统计（"42 次状态检查/轮""7 层嵌套""圈复杂度 150+"）没有给出测量方法，
本文不沿用。可核实的数字：`turns.py` 2747 行；`Session` 有 45 个私有字段；
`turns.py` 有 24 处函数内导入（循环依赖的征兆）。

## 2. 复杂度的三个根源

### 2.1 把嵌入式 SQLite 当作"可能丢确认"的远程库

**现象**

- 每次写入后，用一个**新开的只读连接**回读整行，比对后才认为"已提交"：
  `reserve_client_submission`、`settle_client_submission`、
  `claim_client_submission`、`set_steer_delivery`、`complete_client_submission`
  （`db/client_submissions.py:78,99,124,140,175`），以及
  `get_committed_terminal`（`db/sessions.py:573-590`）。每次回读都是一次
  `aiosqlite.connect`（新线程 + 打开文件）。
- `claim_client_submission` 为确认一次更新执行了 SELECT → UPDATE → COMMIT →
  SELECT client_id → 新连接 SELECT 共 5 步。
- 失败时返回 "unconfirmed"，再由上层建立补偿：`_submission_recorders` /
  `_submission_retries` 两个字典和关停时重试（`turns.py:568-584,683-688`）；
  委托记录 `record()` 约 90 行 CAS + `_pending_snapshot` 推断，同一句
  `"Delegation state saving failed or is unconfirmed"` 抛了 6 次
  （`delegations.py:244-311`）。

**为什么这个前提不成立**

库在本进程内，`PRAGMA synchronous=FULL` + WAL（`db/base.py:419,428`）。
`commit()` 只有两种结果：正常返回即已持久化；抛异常即未提交（或连接已坏）。
"写成功了但确认丢了"只会以**进程死亡**的形式出现，而进程死亡已由启动恢复
（`recover_client_submissions`、`_recover_incomplete_forks` 等）处理。进程活着时
再回读校验，验证的是 SQLite 自身。

**为什么现在又"需要"它**

真正的诱因是同一条连接上混合了两种提交语义：转录消息走延迟批量提交
（`_defer_commit` / `_maybe_flush`，最多 0.5 s 或 32 条，`db/base.py:38-39`、
`db/sessions.py:421-423`），其它写入立即提交。于是"本连接能看到的行"不等于
"已落盘的行"，只好另开连接读"真正提交了的"。这是一个局部设计决定在全局制造
不确定性。

**建议**

1. 让延迟批量只属于一个专用写连接（或干脆取消：WAL 下逐条小事务开销很低，
   先测再定）。其余写入使用普通事务，读当前连接即为已提交状态。
   删除 `committed=True` 参数及其 7 个调用点、`:memory:` 特例分支。
2. 采用**失败即停（fail-stop）**：提交抛出 I/O 类错误时，关闭准入、标记库不健康、
   让 systemd 重启（`Restart=on-failure` 已配置），由启动恢复统一裁决。
   不在运行中逐行推断"也许写了也许没写"。这比现在更正确：SQLite 发生
   `SQLITE_IOERR` 后继续用同一连接本来就不安全。
3. 状态迁移用单条语句完成比较并设置（SQLite 3.46.1 支持 `RETURNING`）：

   ```sql
   UPDATE client_submissions
      SET execution_state = 'running', execution_owner = :owner, state_version = state_version + 1
    WHERE session_id = :sid AND request_id = :rid AND execution_state = 'pending'
   RETURNING client_id, payload;
   ```

   返回行即迁移成功，无行即被他人抢先或状态不符。一次往返替代 5 步。
   `reserve` 用 `INSERT … ON CONFLICT DO NOTHING RETURNING`。
4. 删除 `_submission_retries` 和关停重试：写失败走 fail-stop，崩溃走启动恢复，
   不存在第三种需要运行时重试的情况。委托 `record()` 同理可收缩为一次 CAS。

预期收益：`db/client_submissions.py` 约减半；`delegations.py`、`token_rotation.py`、
`app_agent.py`、`applications.py` 中的 "unconfirmed" 分支大多可直接删除。
fail-stop 已确认先在 Wiki 采用（§5）；推广到主库其它模块仍待决定（§6）。

### 2.2 会话状态由多个协程共享，靠"await 后重新核对身份"保护

**现象**

- `Session` 有 45 个私有字段（`sessions/base.py`），其中问答相关 9 个、
  steer 相关 4 个、派发相关 10 余个，由派发驱动、内层回合、steer 写入器、
  中断、关停、问答计时器等多个任务并发读写。
- 保护手段是 4 把锁（`_lock`、`_dispatch_lock`、`_steer_lock`、`_command_lock`）
  加上每次 await 之后的身份核对。典型的 `_steer_writer`（`turns.py:2542-2659`）：
  - 在锁内先认领、再合并知识上下文、再记录输入；
  - 然后核对 5 个条件（`_steer_open`、`_backend is backend`、`_settlement is owner`、
    `_knowledge_context is previous`、`work_is_open`）判断世界是否已变；
  - 再手工安装新的知识上下文和 Wiki 任务，写帧失败时逐项回滚；
  - durable 与非 durable 两条路径交错（`durable_client_input` 在 `turns.py` 中出现 9 次），
    出队、登记 inline 请求的代码在两处重复（`turns.py:2622-2623` 与 `2646-2648`）。
- `start_message` 对准入条件检查两次（锁外 `turns.py:797`、锁内 `turns.py:837`），
  两处条件还不完全一致（锁内多了 `_closed_agents`）。

这些分支每一条都在防真实的交错，在当前结构下**确实必需**；但需要它们的是结构本身。

**建议：每会话一个所有者（actor）**

- 每个会话一个长生命周期协程，持有该会话的全部可变状态；外部只通过
  `asyncio.Queue` 投递命令（`Submit`、`Steer`、`Interrupt`、`Answer`、`Close`），
  等待其返回的 future。
- 后端进程事件也作为消息进入同一队列。状态只有一个写者，锁全部删除；
  "await 之后世界是否已变"的问题不再存在，因为 await 期间别人无法改它。
- 回合内长时间 I/O（进程启动、写帧）在 actor 内顺序执行；需要并发的（读后端事件流）
  作为子任务，把结果投回队列。
- steer 变成 actor 内一个普通分支：`if running: write_frame else: enqueue`，
  无需单独的写入器任务、`_steer_ready` 事件和"窗口开关"。
- `SessionManager` 退化为 `dict[id, SessionActor]` 和创建/关停；
  现有 mixin 拆分（`ProcessesMixin` 等）可以保留文件划分，但方法变成 actor 的处理器。

这比原报告"把 turns.py 拆成 DispatchCoordinator / TurnLifecycle / RecordingManager"更有效：
按类拆分不减少共享状态，交错问题会原样跟着搬家。

迁移可分步：先让 steer 写入器与派发驱动合并为同一协程（收益最大、范围最小），
再逐步把中断、问答移入队列。

### 2.3 同一份请求上下文存两处，需要手工同步

- 知识上下文同时存在于 `session._knowledge_context` 与全局
  `wiki_tasks.active[token].knowledge`（`server/wiki/tasks.py:387` 的模块级单例）。
  steer 合并时两处都要安装、失败时两处都要回滚（`turns.py:2607-2633`）。
- 建议：`WikiTask` 只保存 `session_id` 与 token 权限，知识上下文按需从会话 actor
  读取（或 `WikiTask` 持有对会话 `RequestContext` 对象的引用，替换引用即生效）。
  一处真相，回滚只需不替换。
- `WikiTasks` 作为模块单例也让测试必须清理全局状态；改为由 `main.py` 创建并注入
  到需要它的组件，与 `deps.py` 已有做法一致。

## 3. 输入恢复（CR-03）链路的具体问题

### 3.1 新发现的缺陷

1. **"忽略"后仍永久阻塞。** `dismiss_client_submission` 只设 `dismissed=1`，
   而 `uncertain_submission_writers`（`db/client_submissions.py:152-157`）不过滤
   `dismissed`。用户在恢复卡片上点了忽略，卡片消失，但
   `require_retired_writers` 仍拒绝该会话所有新输入，界面再没有可操作的入口，
   只能等主机重启。
2. **认领后、进程启动前崩溃，阻塞到主机重启。** `claim` 记录 `execution_boot`，
   `process_group` 在进程启动后才写（`turns.py:1600-1602`）。若在两者之间崩溃，
   重启后该行为 `unknown`、`process_group=NULL`、boot 相同，
   `process_group_exited(None, boot)` 返回 `False`，会话被阻塞；而此时根本不存在
   任何写入者。原报告 P1.7 指向的就是这个现象，但把它误归为"历史数据"，并给出了
   不安全的修法。
3. **恢复列表短页。** SQL 取 `limit+1` 行后，路由再按派生状态过滤
   （`routers/sessions.py:388-389`），可能返回少于 `limit` 条甚至 0 条但带
   `next_cursor`。且每行调用一次 `get_committed_terminal`，每次新开一个连接（N+1）。

### 3.2 写入者存活检测可以整体替换

当前做法是"记录进程组 + boot id，新输入到来时检查旧进程是否还在，不在才放行"，
不确定就阻塞用户。更简单且没有死锁的做法：

- 服务由 systemd 管理，`KillMode` 默认 `control-group`：服务停止或崩溃重启时，
  cgroup 内所有子进程（包括 `start_new_session=True` 产生的新进程组）一并被杀。
  在这个部署里，服务重启后**不可能**还有旧写入者。
- 对非 systemd 运行（终端里直接 `octopus serve`），在**启动恢复阶段主动收尾**：
  对 boot 相同且记录了进程组的行执行 `killpg(SIGTERM)` → 超时 → `SIGKILL`，
  确认 `ProcessLookupError` 后把行标为终态。收尾在准入开放之前完成，
  运行期不再需要 `require_retired_writers`。
- 进程组在 `spawn` 返回后才能获知，"已 spawn 但未记录进程组"有一个很小的窗口。
  给子进程设置 `PR_SET_PDEATHSIG(SIGKILL)`（`preexec_fn` 中调用 `prctl`），
  服务进程死亡时子进程随之终止，不依赖 systemd，也覆盖这个窗口。
  于是 `process_group IS NULL` 的 `unknown` 行可判定为没有存活写入者，
  仍保持 `unknown` 供用户核对（模型可能已收到输入），但不再阻塞新输入。
  （注意 PDEATHSIG 只作用于直接子进程；CLI 再派生的孙进程由进程组 `killpg` 兜底。）

这样可删除：`require_retired_writers`、`uncertain_submission_writers`、
`start_message` 中的阻塞检查及其错误文案，`process_group_exited` 只剩启动时一次调用。
"阻塞直到主机重启"这类用户无法处理的状态消失。

### 3.3 状态模型：把执行状态、投递不确定性和结果拆开

**现状**

- `SubmissionExecution.state` 有 10 个取值（`submission_models.py:22`），混合了
  存储状态（`reserved/queued/claimed/completed`）、派生结果
  （`succeeded/failed/interrupted`，由 `status_from_row` 联表 messages 得出）和
  准入结果（`rejected`）。
- 魔法值 `reason_code='steer_delivery_unknown'` 在 8 处被特判，其中 SQL 里用
  `CASE WHEN reason_code='steer_delivery_unknown'` 覆盖迁移目标
  （`db/client_submissions.py:163-164,182`），Python 里用它决定是否忽略终态
  （`sessions/client_submissions.py:35,147`）。它实际上是一个没有被建模的状态。
- `receipt`（JSON）、`execution_state`、`dismissed`、`recovered_by`、
  `recovery_of` 共同决定"是否可恢复""是否已接纳"，判断散布在 SQL 与
  `status_from_row` 的 5 个分支中，并生成 3 种不同的 `message` 文案。

**建议的存储状态（5 个）**

```text
pending ──claim──▶ running ──terminal committed──▶ done
   │                  │
   │ restart /        │ restart / steer write failed
   │ withdraw         ▼
   └──────────▶ withdrawn        unknown
```

- `pending`：已保存、未开始。替代 `reserved` + `queued`（二者的区别只是
  "receipt 是否已写"，见下条）。
- `running`：已认领。替代 `claimed`。
- `done`：终态消息已提交；成功/失败/中断由 `messages` 联表得出，不存第二份。
- `withdrawn`：确定未执行，可恢复。
- `unknown`：可能部分执行，需用户核对。steer 写帧失败时**直接**迁移到这里，
  不再借 `reason_code` 绕过。
- `dismissed` 与 `recovered_by` 是对 `withdrawn/unknown` 的用户处置，保持为独立列。
- 拒绝（准入失败）不建行，或在同一事务中删除预留行；它不是需要跟踪的执行。

`receipt` 列可以删除：准入在会话 actor 内一步完成，行存在即已接纳
（拒绝不建行），`disposition`（steered/accepted）可作为一列短字符串。
这也消除了 `settle_client_submission` 及其回读校验。

迁移规则写成一张表 + 一个纯函数：

```python
TRANSITIONS: dict[tuple[State, Event], State] = {
    ("pending", "claim"): "running",
    ("pending", "withdraw"): "withdrawn",
    ("pending", "restart"): "withdrawn",
    ("running", "finish"): "done",
    ("running", "delivery_unknown"): "unknown",
    ("running", "restart"): "unknown",
}
```

SQL 只执行 `UPDATE … SET state=:to WHERE state=:from RETURNING …`，不再有 `CASE`。
状态展示（`status_from_row`）只做投影，不做推断；可单元测试，覆盖全部组合。

### 3.4 其它可直接简化的点

| 位置 | 现状 | 建议 | 是否防 bug 所必需 |
|---|---|---|---|
| `sessions/client_submissions.py:21-28,135-137` | 准入和认领时各对全部附件做一次 SHA-256（单文件上限 25 MB），比对检测"附件被改" | 附件以随机 id 一次写入后不再修改（`attachments.py:139-144` 只有 `write_bytes`）。上传时计算一次哈希存入记录；认领时比对 `stat` 的 size + `st_ino` + `st_mtime_ns` 即可 | 否。防的是应用内不存在的写路径 |
| `sessions/client_submissions.py:71-120` | `submit_client_message` 嵌套双层 try、两个 `assert`、5 种返回形状，异常后再读状态猜结果 | 准入在会话 actor 内单步完成：预留行 → 入队 → 返回；失败即异常。返回统一的 `ClientSubmissionStatus` | 否，§2.1 前提的产物 |
| `routers/ws.py:132-137` | `elif client_request_id:` 分支不可达（带 id 的请求已在 114-119 行 `continue`） | 删除。并评估无 `client_request_id` 的旧路径是否还需要（前端始终发送，`useWebSocket.ts:764-765`） | 否 |
| `routers/ws.py:70-146` | `send_message` 手写 isinstance 校验 + 内嵌 `reject` 闭包，约 75 行 | 用 Pydantic 模型（`SendMessageFrame`）解析 WS 帧，校验失败统一回 `submission`/`error` | 否 |
| `sessions/turns.py:568-588` | `_complete_request` 为 durable 请求另起后台任务写库，登记两个字典用于重试；`_withdraw_requests` 用集合判断把 `reason` 映射为 `state` | actor 内同步写库（fail-stop 处理失败）；`state` 与 `reason` 分别传入，不从字符串推导 | 否 |
| `sessions/turns.py` 函数内导入 24 处；`client_submissions.py:13` 反向导入 `turns.QuotaExceeded` | 循环依赖靠延迟导入回避 | 把 `QuotaExceeded`、`QueuedPrompt` 等共享类型移到叶子模块（如 `sessions/types.py`），依赖方向单向 | 否 |
| `turns.py:1600,2618` | `getattr(backend, "process_group_id", None)` + `isinstance` 判断 | `HarnessRun` 已有该属性（`harness/run.py:252`），在协议中声明后直接访问 | 否 |
| `Session` 问答 9 个字段（`_pending_questions`、`_pending_question_events`、`_pending_question_answers`、`_question_answer_attempts`、`_question_timers`…） | 同一 `question_id` 的状态分散在 5 个字典，增删需同步 | 一个 `dict[str, PendingQuestion]`，`PendingQuestion` 内含 event、answer、attempt、timer | 否。分散存储本身就是漏删一项的 bug 来源 |
| `web/src/hooks/useWebSocket.ts:669,727,772` | 同一 "Admission was not confirmed…" 回复对象写了 3 次 | 一个常量构造函数；随 §2.1 落地后 `state: "unknown"` 的准入回复大多可删除 | 否 |
| `web/src/lib/submission.ts:21-35` | UUID 生成三级降级，最后一级用 `Math.random` | `getRandomValues` 在非安全上下文同样可用，保留前两级即可 | 否 |

## 4. 不建议采纳的原报告建议

- **Repository 模式 / 为 PostgreSQL 预留抽象**：单机单用户、嵌入式库，没有迁移需求。
  多一层接口只会让每个查询多一次转写，并掩盖 `RETURNING`、部分索引这类正好能简化代码的 SQLite 能力。
- **Event Bus 中间件**：现有 `_broadcast` 已是订阅表，审计与过滤没有实际需求。
- **统一 Queue Manager**：浏览器、调度、委托、后台任务最终都进入同一个
  `start_message`；会话 actor（§2.2）的命令队列就是统一入口，不需要另起服务。
- **`transitions` 等状态机库**：5 个状态 6 条迁移用一张字典表即可，引入依赖不划算。
- **按类拆 `turns.py`**：见 §2.2，先消除共享状态，文件划分随之自然出现。

## 5. Wiki：在 fail-stop + 主动清理旧进程前提下的简化

> 用户已于 2026-10-20 确认两项前提（仅针对 Wiki 先行）：
> **存储写失败即停**；**启动时主动终止旧执行进程**。
> 本节审阅了 `wiki/jobs.py`、`job_execution.py`、`job_notifications.py`、
> `history.py` 的恢复与结算路径，其余模块只看了结构与文档字符串。

### 5.1 两个前提带来的新不变量

1. **运行中不存在"写了但不确定"。** Wiki 库（每账户一个，`sqlite3` 同步连接，
   `store.py:338`）提交只有成功或异常两种结果；I/O 类异常直接让服务退出。
2. **启动后不存在旧写入者。** 开放准入、启动 `WikiWorker` 之前，上一代所有
   模型进程已被终止并确认。

由此，Wiki 中所有不确定性只剩一个来源：**上一代进程在某个中间点死亡**。
它只需在启动时处理一次，并且处理时没有任何并发参与者。

### 5.2 现状：为同一个不确定性准备了多条路径

| 机制 | 位置 | 它在防什么 | 新前提下 |
|---|---|---|---|
| `require_retired`（11 处调用）检查 `WikiWorker.executing`、`tasks.assignments`、`tasks.active`、会话队列、`_backend_retirements`、`_recording_blocked` | `jobs.py:705-724` | 旧执行可能仍在运行 | 运行期：退化为"该 job 的会话空闲"一个判断；启动后：恒为真 |
| `settlement_phase` 四态 `none/pending/confirmed/unconfirmed` 及数据库触发器 | `job_events.py:15-16,155-156` | 取消后无法确认结果是否已记录 | `unconfirmed` 消失；取消同步完成为 `confirmed`，`pending` 只在取消进行中短暂存在 |
| `settle_cancelled`（约 40 行）+ 通知循环每秒轮询未结算取消 | `jobs.py:533-571`、`job_notifications.py:38-43,77-92` | 结算失败后需后台补做 | 取消在一次调用内完成：终止 → 核对产出 → 写结算；删除轮询 |
| `witness_unprepared_cancellation` + `cancelled_before_admission` 标记 | `job_execution.py:126-160`、`jobs.py:552-554,920-926` | 证明"取消时模型尚未启动"，才能免于核对 | 无论模型是否启动，进程都已被终止，统一按 history 核对产出；见证机制删除 |
| `worker_request.phase` 五态 `prepared/accepted/unknown/rejected/settled` 及迁移表 | `job_execution.py:72-116` | `unknown` 来自"结果记录未确认" | `unknown` 不再产生；剩 `prepared → accepted/rejected → settled` |
| `execution_unknown` 项：重启时找不到执行身份，**永久**要求新建任务 | `jobs.py:630-632,873-874` | 无法证明旧执行已停 | 旧进程已终止，可按 history 核对后正常 retry；该状态删除 |
| `WikiWorker.run` 捕获一切异常、指数退避、`recover=True` 重跑恢复 | `jobs.py:1194-1234` | 存储暂时不可用 | 存储异常 fail-stop；只有业务错误（`WikiError`）标记该 job 失败 |
| `_claim_job` 用 `shield` + `settle_claim`，`run` 的取消分支调用 `_finish_failed_job` | `jobs.py:1236-1251,1222-1226` | 停机时 job 卡在 `running` | 留在 `running` 即可，下次启动 `recover_jobs` 统一转为 `interrupted`；删除两条停机补偿路径 |

后三行属于同一模式：停机路径、运行期补偿、启动恢复**三处**处理同一件事。
保留启动恢复一处。

### 5.3 必需、应保留的机制

以下复杂度防的是真实存在的非原子性或外部写入者，fail-stop 不能替代：

- **history 日志的 `prepared → applied/interrupted/conflict` 按哈希裁决**
  （`history.py:280-321`）。文件替换与 SQLite 提交天然不能同一事务，进程死在
  两者之间只能靠哈希判断。这是 Wiki 启动恢复的核心，保留。
- **外部变更捕获（`external-change.json`、`event_inbox`、`events`）**。用户编辑器和
  Sapphire 应用（`app_bridge.py`）是真实的外部写入者。
- **写入授权（`grants`）与路径预留**。这是权限边界，不是容错。
- **`prepare` 在 `start_message` 之前持久化 request 身份**（`job_execution.py:32-69`）。
  这是启动恢复能找到"上一代执行了什么"的唯一依据，保留；但其中对 `prior.phase`
  为 `unknown` 的拒绝分支可删除。

### 5.4 建议的实现

**W1. 统一的执行进程登记表（替代只覆盖浏览器输入的 `process_group` 列）**

Wiki worker 回合以 `source="wiki"` 调用 `start_message`（`jobs.py:1446`），
不是 durable 浏览器输入，当前**没有任何进程组记录**。主动清理需要覆盖所有模型进程：

```sql
CREATE TABLE executions (
    process_group INTEGER PRIMARY KEY,
    boot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT,
    started_at TEXT NOT NULL
);
```

- `HarnessRun.start` 拿到 pgid 后立即插入；进程确认退出（`_retire_backend` 完成）后删除。
- 子进程设置 `PR_SET_PDEATHSIG(SIGKILL)`，覆盖"已 spawn 未登记"的窗口。
- `client_submissions.process_group/execution_boot` 列改为引用此表或删除。

**W2. 启动顺序固定为一条流水线**

```text
1. 打开主库与各账户 Wiki 库
2. 对 executions 中 boot_id 等于当前 boot 的每个进程组：
   SIGTERM → 等待 → SIGKILL → 确认 ProcessLookupError；然后清空该表
   （boot_id 不同的行直接删除；无法读取 boot_id 时按同 boot 处理并终止，
     宁可多杀一次，不放行）
3. 每个 vault：WikiHistory.recover()（哈希裁决 prepared 变更、收尾外部变更）
4. recover_client_submissions()；WikiJobs.recover_jobs()
5. 开放准入；启动 WikiWorker 与通知接收
```

任何一步抛出异常即退出进程，不进入半恢复状态。systemd 单元需补
`StartLimitIntervalSec`/`StartLimitBurst`，持续的磁盘错误会停在失败状态而不是
无限重启，并在 `systemctl --user status` 中可见。

注意：MCP bearer 由主密钥签名、无服务端状态（`mcp_identity.py:182-205`），
`work_is_open` 依赖的 `_closed_works` 也只在内存中（`mcp_identity.py:32-38`）。
重启后旧进程手里的 bearer 仍能通过签名校验。Wiki 工具还需在内存中的
`tasks.active` 找到任务，推测会被拒绝，但本次未验证。第 2 步的终止让这个问题
不再依赖推测。

**W3. `recover_jobs` 变成确定性的分类**

启动时没有存活写入者，每个 `running/interrupted` job 的每个未完成项只有三种情况：

| 证据 | 处理 |
|---|---|
| 主库有该 `request_id` 的终态消息 | 按终态结算该项（现有 `_recover_recorded_item` 路径） |
| 无终态，history 中有本 job 的 `applied` 变更 | 产出已落盘，核对索引后标 `done` 或进入 `index_recovery` |
| 无终态，无 applied 变更 | 模型被中断且未产出，标 `pending`，可直接 retry（重读当前 revision，沿用 `retry_count`） |

`execution_unknown` 与 `recording_failure` 两类"需人工核对"状态都不再需要。
仍需要人工的只有 history 裁决出的 `conflict`（文件既不是 before 也不是 after），
它本来就由 history 呈现。

**W4. 取消 = 终止 + 核对，一次完成**

```text
cancel(job):
    标记 status='cancelled'（settlement_phase='pending'）
    终止该 job 会话的模型进程并等待确认退出
    reconcile_cancelled_effects(job)   # 现有逻辑，去掉 require_retired 与 409 重试
    写 settlement_phase='confirmed'
```

失败即异常向上（fail-stop 或 409 返回用户），不留后台重试。删除
`witness_unprepared_cancellation`、`cancelled_before_admission`、通知循环里的
`_unsettled` 轮询、`settlement_phase='unconfirmed'`。

**W5. 一个写事务上下文替代重复的锁 + 注册核对**

当前 `registry.config(...) != self.registration` 比较出现 16 次，
`_account_lock + registration_lock` 组合出现 15 次，`BEGIN IMMEDIATE` 出现 28 次，
不少函数在锁外读取、锁内再比较"是否被改"（如 `release_unused`、`readmit`、
`reconcile_cancelled_effects` 末尾）。引入：

```python
@contextmanager
def write_txn(self, *, history: bool | Literal["shared"] = False):
    """Locks, registration check and one IMMEDIATE transaction, in that order."""
    with ExitStack() as stack:
        if history:
            stack.enter_context(WikiHistory(self.store).locked(shared=history == "shared"))
        stack.enter_context(registry._account_lock(self.user_id))
        stack.enter_context(registry.registration_lock(self.user_id))
        if registry.config(self.user_id) != self.registration:
            raise WikiError("Wiki registration changed", 409)
        db = stack.enter_context(self.store.connect())
        db.execute("BEGIN IMMEDIATE")
        yield db
```

读取与判断都移进同一个事务，"锁外读、锁内比"的二次校验随之删除。
单用户本机场景下持锁读取的代价可以忽略；如个别读取确实很慢（全文读取），
再单独评估，不预先优化。

**W6. job 项从 JSON 标志位集合改为显式状态**

`jobs.items` 是 JSON 数组，每项靠 20 多个可选键的组合表达状态
（`worker_request`、`previous_worker_requests`、`recording_failure`、
`recording_recovery`、`execution_unknown`、`index_recovery`、`index_complete`、
`cancelled_before_admission`、`retry_count`、`previous_revisions`…）。
`readmit` 需要手工列出 7 个键逐一拷贝以"保留执行证据"（`jobs.py:848-851`），
漏一个就丢证据。

W3、W4 落地后，大部分标志位已无意义。剩余部分规范化为表：

```sql
CREATE TABLE wiki_job_items (
    job_id TEXT NOT NULL,
    item_key TEXT NOT NULL,           -- doc:<id> | reservation:<id>
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','attention','skipped')),
    revision TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (job_id, item_key)
);
CREATE TABLE wiki_job_attempts (       -- 替代 worker_request / previous_worker_requests
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared','accepted','rejected','settled')),
    PRIMARY KEY (job_id, attempt)
);
```

`readmit`/`retry` 只改 `state`，历史留在 attempts 表中，不再拷贝。
单项更新变成一条 `UPDATE`，不必读出整个 JSON、改一项、整体写回并比较。

**W7. 合并常量小模块，权限检查收口**

- `maintenance_contract.py`（19 行）、`limits.py`（17 行）、`capabilities.py`（14 行）、
  `formats.py`（23 行）合并为一个 `wiki/constants.py`。
- 模块文档字符串大量重申"never grant / confer no authority"，说明权限约束分散在各处。
  在 MCP 工具入口做一次能力检查（调用者 + 操作 + 目标 → 允许/拒绝），内部函数信任已校验的输入。
  这一项需要逐个工具梳理，建议放在 W1–W6 之后。

### 5.5 预期删除量（估算，未逐行统计）

- `jobs.py`：`require_retired` 大部分分支、`settle_cancelled` 约一半、`_claim_job` 的停机补偿、
  `run` 的退避重试与取消补偿、`_retry` 中 `execution_unknown`/`cancelled_before_admission` 分支。
- `job_execution.py`：`witness_unprepared_cancellation` 整个函数、`transition` 的 `unknown` 分支。
- `job_notifications.py`：`_unsettled` 与对应轮询。
- `job_events.py`：`settlement_phase` 的 `unconfirmed` 取值与相关触发器分支。
- 前端 Wiki 卡片中对 `unconfirmed`、`execution_unknown` 的展示分支（未逐一核对）。

### 5.6 迁移注意

- 现有库中 `settlement_phase='unconfirmed'` 与 `execution_unknown` 的项：
  升级后第一次启动走 W2 流水线，旧进程已终止，按 W3 规则重新分类即可，
  不需要单独的数据迁移脚本；但迁移代码要先把这两类标记读出再分类，不能直接丢弃。
- W6 的表结构变化需要一次性迁移 `jobs.items` JSON → 两张表，迁移前备份 Wiki 库。
- 浏览器输入（§3.2）与 Wiki 共用 W1 的进程登记和 W2 的启动流水线，建议一起落地，
  避免两套进程记录并存。

## 6. 建议的推进顺序与待决事项

1. **先修 §3.1 的缺陷 1、3**（`uncertain_submission_writers` 过滤 `dismissed`；
   恢复列表在 SQL 中完成过滤并 JOIN 终态）。范围小，不依赖其它决定。
2. **W1 + W2：进程登记表与启动流水线**，同时覆盖 Wiki 与浏览器输入（§3.2），
   删除运行期写入者阻塞，解决缺陷 2。
3. **W3 + W4：Wiki 恢复与取消收敛**，删除 `unconfirmed`、`execution_unknown`、见证机制和轮询。
4. **W5、W6：Wiki 写事务上下文与 job 项规范化**（含一次 Wiki 库迁移）。
5. **§3.3 浏览器输入状态模型收敛**。
6. **§2.1 主库存储语义调整**：拆分批量提交连接、删除回读校验。
   影响 delegations、token rotation、applications，需要单独计划文档。
7. **§2.2 会话 actor**：先合并 steer 写入器与派发驱动，再迁移其余命令。

已决定（2026-10-20）：

- Wiki 采用 **fail-stop**，并在**启动时主动终止**旧执行进程（§5）。

仍需用户决定：

- fail-stop 是否同样推广到主库的其它模块（§2.1；delegations、applications 等）。
- 转录消息的批量延迟提交是否保留（保留则放到独立连接；取消需先测吞吐）。
- 无法读取 boot_id 时，按"同一次启动"处理并终止记录在案的进程组（W2 第 2 步的默认值）。
  这样最多误杀一个复用了相同 pgid 的无关进程；在 WSL + systemd 下 boot_id 一直可读，
  风险很低，但需要确认可以接受。

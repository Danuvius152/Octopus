# P1 问题整改：CR-03 输入恢复

> **Status:** superseded by minimal-architecture-execution-20261020.md — CR-03 暂存实现并入最小机制架构计划，以下保留原范围决定与历史验收，不再作为单独发布入口。

2026-10-20 新的[执行计划](minimal-architecture-execution-20261020.md)及其[设计合同](../review/octopus_minimal_architecture_spec_20261020.md)
已纳入 CR-01～CR-20，并替换本文件的在线补写、逐写独立回读和跨启动 PGID 解锁策略。
本文件的实现与测试记录描述此前 CR-03 候选，不代表新架构已经实现或通过验收。

## 1. 目标与依据

针对本地审查材料 `docs/review/octopus_code_review_20261020.md` 及其
拟议实施规格 `docs/review/octopus_implementation_spec_20261020.md`，结合当前
单人、本机 Codex 使用方式确认修复范围。审查优先级与本机实施决定分别记录；
未实施的问题不标记为已修复。

## 2. 已核实事实与待确认问题

- 认领安装创建的首个账户为管理员；未建立账户、仍使用安装 token 时属于
  operator/legacy 身份。账户禁用已有管理员网页入口。
- CR-02 的完整 argv 日志由 `server/harness/run.py` 输出；后端默认 INFO。
  按[本机服务配置](../local-wsl.md#服务管理)，通过用户级 systemd journal 查看。
  本轮未读取真实日志，未确认 journal 的持久化方式、保留期或读取权限。
- 本机 Codex 的常规长度用户输入与内部指令可能进入 argv 日志；HTTP MCP
  bearer 的值通过环境变量传递，不属于这条日志已确认的泄漏内容。
- CR-02 是否纳入本轮尚待用户决定；此前日志脱敏已按本机风险列为可选，见
  [运行时修补计划](runtime-interface-repair.md)中的 Q25 范围决定。
  不将本次风险询问当作同意修复或同意忽略。
- CR-03 指服务进程停机时撤回尚未执行的排队输入：正文仅在内存，首次
  accepted 回执已持久化；重启后正文及队列消失，同 client ID 查询或重试仍
  返回旧回执，不会重新排队。浏览器对已确认接纳的纯文本不保留恢复草稿。
  清队列不删除已保存的聊天历史；当前正在执行的请求按停机中断处理。
- 此前 Q11 已明确选择不持久化待处理队列、不跨重启恢复或自动重放，且不将
  其列为必须补齐的工作，见[原决定](runtime-log-repair-20261013.md#10-what-this-defers)。
  CR-03 的状态澄清与输入恢复需求须分别确认，不把本次解释当作恢复持久队列
  的授权。用户随后明确同意 §3 的方案并要求开始实施；该决定更新浏览器输入的
  纯内存边界，保留服务重启后不自动执行的原则。

## 3. CR-03 已确认方案与一手依据

用户于 2026-10-20 明确同意本节方向并授权实施：
“已接纳的输入可找回，执行状态可核验；重启后由用户决定重发”。
复用 `client_submissions` 和 `SessionManager`，不引入新队列服务。

1. 返回 accepted 前，在同一 SQLite 事务中保存请求身份、原文、附件引用及
   排队事实；持久化失败或不确定时不返回确定接纳。SQLite 的原子提交使同一
   事务内的数据库变更全部发生或全部不发生。[SQLite 原子提交](https://www.sqlite.org/atomiccommit.html)
2. 所有进入 CLI 的路径都先持久领取请求，领取提交失败或不确定时不得触发
   CLI；这样未领取才可证明尚未进入执行入口。SQLite 事务仅覆盖数据库，
   **不能把外部 CLI 的副作用一并原子提交**，因此不承诺端到端 exactly-once。
   后一句是对该事务边界的工程推论。[SQLite 事务](https://www.sqlite.org/lang_transaction.html)
3. 重启后按持久事实分类；原文和附件引用沿正常权限、保留及删除生命周期
   保存，附件不可用时明确提示，不悄悄以缺附件的输入重发：

   | 重启前的可核验事实 | 重启后的行为 |
   | --- | --- |
   | 已接纳、尚未领取 | 原请求记为“未执行即撤回”，说明因服务重启撤回，保留正文供恢复到输入框后手动重发；不自动执行 |
   | 已领取，缺少确定终态 | 显示“执行中断，结果未知”；保留输入与现有输出供检查；不自动重跑 |
   | 已提交确定终态 | 显示该终态，不重新执行 |

4. 同一 client ID、相同载荷只指向原请求，查询或重试返回其最新状态，
   不再次入队；不同载荷拒绝。保留首次接纳回执作历史事实，同时另报执行
   状态与更新时间。人为重发产生新 ID 并关联原请求，特别是结果未知时先
   展示现有记录供检查。[Azure 异步请求应答](https://learn.microsoft.com/en-us/azure/architecture/patterns/asynchronous-request-reply#problems-and-considerations)
   明确建议重复 key 返回已有状态资源；[AWS 幂等 API](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
   支持调用方 request ID、同 ID 不同参数拒绝及身份与变更原子记录。
   AWS 所说的语义等价不要求冻结首次响应字节；本项目最新状态的接口设计
   依据 Azure 的状态资源模式。

已领取但缺少确定终态时，允许新的执行前须确认旧执行已退出或已被可靠隔离；
新服务的内存中没有旧任务，不能单独作为旧执行已停止的证据。

这会改变此前“排队正文仅在内存”的边界；仍保留“不跨重启自动执行”原则。
原先的 `WAL + synchronous=NORMAL` 可应对应用进程崩溃，但断电或系统崩溃
可能回滚已提交事务。本次为关键接纳、领取与终态采用 `FULL` 同步保证；
接纳及领取后读回已提交存储，不能以未提交的同连接视图当作确认。
[SQLite 同步与持久性](https://www.sqlite.org/pragma.html#pragma_synchronous)
[RFC 9110 §15.3.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.3)
规定 HTTP 202 表示已接纳但未完成，并建议提供当前状态和状态监视入口；
它不强制持久化。本项目 WebSocket accepted 也不等同于 HTTP 202。

## 4. 实施边界

- 复用浏览器提交记录及派发器；内部 schedule、Wiki、bg 与 delegation 保留
  各自既有业务账本，不引入新队列服务，不自动重放历史工作。
- 保存原文、附件证明、提交及实际执行归属；原文按需查询，恢复列表分页返回摘要。
  手动恢复重新校验附件内容；缺失或改变时明确要求重新附加完整文件。
- 新人类提交可关联原撤回输入，保留独立的新身份；重复恢复不创建第二个请求。
  原接纳回执保留，当前执行状态和已提交终态另报，不冻结旧回执作为当前事实。
- 对执行结果未知的请求不提供一键重跑。受管启动清场与输入恢复接管已捕获旧代的
  退役证明，不再依赖历史 PGID；同代资源仍由原 Owner 收束。standalone 没有受管
  证明时保留原进程组/boot 检查。进程退出不会改变历史 unknown。
- 保存失败的 recorder 保留责任，停机检查不能将仅记录过日志的失败视为成功。
  旧数据没有保存原文时保持未知，不回填或发明丢失的正文。

## 5. 实现与验收边界

- `server/db/client_submissions.py` 保存原始输入、附件大小/内容证明、接纳回执、
  执行领取与归属、恢复来源及提醒状态。既有数据库原位增加字段；旧记录保留
  原回执，不虚构正文。领取、追加输入的交付标记及完成状态均核对已提交视图。
- `server/sessions/client_submissions.py` 在打开执行入口前保存和领取输入，
  同一身份重试只查询原记录。`turns.py` 同时覆盖普通派发和执行中追加输入；
  追加输入写入可能已发生而确认丢失时记为未知，不放回待执行队列。
- 完成记录属于会话管理器，失败时保留 recorder 并在停机时重试；失败不能
  被日志观察消费后丢失。父请求完成不能证明结果未知的追加输入已送达或退出。
- 恢复接口按会话所有权鉴权，分页只列摘要，正文按需读取；附件恢复核对原
  大小与 SHA-256，发生变化时不以同名文件替代。删除会话级联删除提交记录。
- `RecoverableInputs` 在会话中提供查看、恢复至输入框、分页和隐藏提醒。
  恢复本身不发送；用户再次发送才创建带来源的新身份。异步下载期间切换会话
  或修改草稿不会覆盖新草稿；缺失附件须明确重新附加，才能发送。

回归分两层验证：后端测试使用实际 SQLite 关闭/重开和合成子进程覆盖停机及
执行入口；浏览器新增用例验证刷新、恢复和明确点击发送的边界，其接口使用
受控响应，不将该用例宣称为真实服务重启测试。原有 Codex 实机测试独立运行。

## 6. 本机验证记录（2026-10-20）

| 检查 | 实际结果 |
| --- | --- |
| 后端 hermetic：`not real and not knowledge_eval` | 最终复跑进行中；首轮 3 处旧参数断言失败已修正，相关回归已通过 |
| 恢复专项：`tests/test_submission_recovery.py` | 14 passed；包含关闭/重开、去重、故障提交、真实合成进程、附件、分页/越权、旧数据与未知追加输入 |
| Codex 实机：`real and (real_codex or codex_bin) and not real_claude and not knowledge_eval` | 18 passed，4498 deselected；依 marker 选择，未调用知识评测 |
| 前端单元 | 58 files / 496 passed |
| 纯 UI 浏览器：`bun run test:e2e:fast` | 88 passed；执行审计 offline=3、real=0 |
| Codex 浏览器：`bun run test:e2e:llm e2e/codex.spec.ts` | 1 passed，单 worker；与实机 CLI 串行运行 |
| Ruff、mypy、ESLint、TypeScript、生成契约、文档索引、生产构建 | 通过；TypeScript 使用 `tsc -b --force` |

**Provider 范围：Codex 为上述实际结果；Claude 当前不可用，真实 CLI/浏览器
测试未运行。** 离线协议测试不替代 Claude 实机验证；Wiki `knowledge_eval`
未选择，不计为通过。

## 7. 发布记录

待完整后端门禁通过后提交并推送本任务改动，按 `docs/local-wsl.md` 的自动发布
流程构建独立候选、等待自然排空、原子切换并核验健康、真实浏览器及资源哈希。
工作区并行的 Wiki prompt、审查材料及 README 手工修改不纳入本次提交。

## 10. What this defers

- **CR-01：本轮不实施。** 用户于 2026-10-20 明确当前多账户不重要，账户
  禁用问题暂不处理。保留既有账户及 provider 行为；该撤权缺陷仍然存在。
  如后续实际使用多账户或需要撤销他人访问，再重新确认此项的实施范围。

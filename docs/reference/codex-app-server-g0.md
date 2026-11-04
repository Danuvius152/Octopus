# Codex App Server G0：0.166.0 锁版观察

本页保存 2026-11-01 的两组协议实验及适用条件，以及同一锁版的后续收尾观察。
前两组历史实验未因后续修复而重跑。
普通 Codex 的现行职责由 [Codex 合同](../implemented/codex-backend.md)维护；
强目标的恢复条件见[暂停执行范围](../plans/runtime-execution-scope.md)。

| 实验目标 | 当时结果 | 对当前设计的约束 |
| --- | --- | --- |
| G0-Recursive：父正式结束后，任意层级晚回仍自动推进并闭合 | 有反例，未通过。 | 不能声称当前 R2 解决任意 post-final 递归。 |
| G0-Base：单个活动根内协作、输入证据及有界退休的协议前提 | 有限范围通过。 | 支持对应锁版前提；不替代 Owner、数据库、UI 和消费者验收。 |

## 固定环境与方法

两组实验使用同一 standalone Linux musl binary：Codex **0.166.0**，SHA256
`b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8`。
[发行 tag](https://github.com/openai/codex/releases/tag/rust-v0.166.0)解析到源码
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`。
官方 sigstore Rekor body 声明的 binary digest 与本机一致；当次没有执行签名信任验证。

该 binary 执行 `app-server generate-json-schema --experimental --out <隔离目录>`，
全量 schema bundle SHA256 为
`8aabf260bb66e9ca9ae5d20253f443971834644fe4659c85f8b1cc9e1d22dd5c`。
旧 0.165.0 等版本的 schema 检查属于更早历史，不能替代此环境。

主要方法是真实 binary 配合 loopback Responses SSE 假 provider，独立
`CODEX_HOME`/cwd、read-only、low effort、无登录和无 MCP。
它能控制何时释放原生模型结果并检查后继请求体，避免用短暂静默推断永久不再工作。
真实模型验证另外单列。

<a id="codex-g0-evidence"></a>
## G0-Recursive：完整递归目标的反例

当次明确开启 `multi_agent_v2` 与 `wake_on_agent_messages`。
先由 exec 建立历史并退出，再由新 stdio 宿主精确 resume；确认旧内容后才发业务输入。

- 一层 child 晚回后，根自动续跑，实际后继模型请求包含 child 结果。
- 递归轨迹采用目录中明确带 v2 元数据的 `gpt-6.1-sol`：根首完成、child 完成、
  根后继完成后才释放 grandchild 结果。孙完成后，child 仍只有原先两次模型请求，
  没有包含孙结果的后继输入；直接向该 child 输入返回 `-32600`。
- 初次递归试验采用没有 v2 子协作能力元数据的模型，spawn 返回 unsupported call。
  这是探针前提错误，没有计作递归反例；仅打开 feature 不能证明子工具实际可用。

实测与锁版源码的
[wake 准入](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/session/turn_input.rs#L714)
一致：无显式 trigger/durable sleep 时，自动消息 wake 排除 non-root agent。
[直接子输入限制](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/thread_input.rs#L8)
又阻止客户端以 child turn/start 补齐。反例依据包括原请求体与准入规则，
没有用一秒观察窗口证明永久静默。

[回流实现](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/agent/control/completion.rs#L48)
先发 completed activity，再尽力向父入队；
[turn 收尾](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/tasks/mod.rs#L703)
还会记录 pending input，并在发完成事件后检查下一轮工作。
completed、历史存在和父模型实际处理是不同事实。

该实验只证明安装 binary 的局部协议行为，没有覆盖全部重新派生、同 child 第二轮、
快 child、回流失败/仅记历史、ACK 丢失/steer 竞争、Stop 三阶段以及产品兼容矩阵，
也没有真实远端模型或 Octopus 装配验收。**G0-Recursive 的失败结论仍保留。**

<a id="codex-g0-base-evidence"></a>
## G0-Base：R2 单根合同的有限通过

该组针对一个 attempt 绑定一个已接纳根 turn：允许根活动中的协作，
根正式终态后冻结业务输入并有界退休。2026-11-01 的协议前提验证通过，
当次 Standards/Spec 两轴及 Vera 独立复核没有阻断发现。
它没有解决已结束中间父的任意递归自动推进。

沿用上述 binary/source/schema，独立 home/cwd、`gpt-6.1-sol/low`，
真实 v2 工具面与 wait 启用。正常路径显式关闭
`wake_on_agent_messages`、`goals`、`sleep_tool`，不开放其他根输入 writer。
受控 provider 无登录，逐项保留配置和原始请求体。

源码的
[pending-work 准入](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/session/turn_input.rs#L715)
区分 trigger、durable sleep 与 message wake；
[原生 followup](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/agent/control/api.rs#L120)
拒绝 root 目标。有限静默窗口不是单根保证的独立依据。

当次 27/27 个确定性 native 场景、2 项 transport 回归、探针 lint 和类型检查通过。
逐项核验 33 次原生进程退出、完整帧与 reader join；26 次使用 TERM，
原生场景没有需要 KILL，另用拒绝 TERM 的静默受控进程实测 KILL 分支。
隔离 home/cwd 没有遗留进程。

| 边界 | 当时观察及其限制 |
| --- | --- |
| 活动协作 | 根与中间父先实际 wait，再释放孙结果；孙/子结果进入各自父的下一模型请求。原生 followup 完成同 child 第二轮，根仍只有原 turn。进度、任一 child 完成、steer、timeout 都可使 wait 返回，不能解释成 join-all。 |
| 根身份与终态 | start ACK 可关联确切 thread/turn；root completed/failed/interrupted、child/tool 完成、final item 与进程退出分别可见。wake-on 负对照的新 turn.id 可变化而 rootTurnId 沿用首轮，必须核确切 turn.id。终态 items 可能为 summary/notLoaded，完整输出依据 item/completed。 |
| 输入证据 | steer 接受、错身份/终态后拒绝有证据；通知可先于 ACK。ACK 丢失时完整 userMessage.clientId 可证明接纳；确认全丢或断连只能保留 unknown，即使 oracle 已知模型执行。spawn/initialize/thread 准备无业务请求，exec argv 的发送边界独立。 |
| 问题与压力 | 原生问题、审批、dynamic tool 有双向编号及 thread/turn/item 或 callId。双方数字 id=0 不冲突。取消后迟答、未知 response、问题等待中断连通过；2,000,028-byte 完整 commentary 下控制 write 可返回，通知与 ACK 仍依赖持续 stdout drain。request_user_input 仅 root 可用，child 被拒绝；child 审批仍可能晚到。 |
| 根先结束与退休 | 根成功可与残余 child interrupted 并存；根 terminal 后可采集完整 child partial 和 late approval。活动 wait、child、问题、无 stdout 长工具及终态后残余工作均可取消并收束。ACK 不等于退出，产品 partial/parent_done 映射另行验收。 |
| 历史与配置 | 同 home 精确 resume 旧 exec、thread/inject_items 注入 controller、权限及 300,018-byte JSONL 输入通过。beforeTurnId fork 排除指定 turn。恢复、fork、只读查询无隐式模型请求；禁用 goals 后保存的 active goal 不续跑。退休后新宿主可 thread/read，详情读取不依赖旧宿主。 |

历史策略选择同一独立 `CODEX_HOME` 内 native thread/fork。
整 home 复制反例显示 paginated storage 留有绝对 rollout 路径，可能 stale-path 拒绝或仍指原 home；
修改 SQLite 或绕过校验不能建立原生可移植合同。
late mailbox 在宿主退出后没有进入 source/fork 恢复后的显式请求，
因此须在退休前保存已采集结果，不能依赖重启重捞。
已测 300 KB 输入也不是任意 JSONL 长度保证。

## 单列的真实模型观察

当次使用已选既有登录的 symlink、独立 home/cwd 和阻断 auth.openai.com 的
CONNECT 代理，未观察到认证/刷新请求。真实根、子、孙逐层等待并返回
`G0_LIVE_ROOT=899`，根 commentary 和 final_answer 分开，宿主正常退出。
这只证明一次模型选择了活动等待；逐层请求体消费由确定性场景证明，未推断 TLS 内内容。

<a id="residual-root-command-retirement"></a>
## 残留根命令的连接收尾

后续针对同一 0.166.0 binary digest 的 loopback Responses SSE 实验，使用独立
home/cwd、合成 provider 和一条已输出后继续等待的命令，不读取登录、不请求
远端模型。根先正常结束时，直接关闭 stdin 的宿主退出 0，但客户端没有收到
命令的完整结果；保持连接并发送 `thread/backgroundTerminals/clean`，等待完整
item 后再关闭，则收到 `status=failed`、`exitCode=-1` 与原输出，宿主同样退出。
锁版 [连接关闭顺序](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/lib.rs#L1196)
先移除 outbound，之后才清理线程；因此 rollout 中有完成记录不证明 wire 已送达。

再让同一二进制、同一合成 provider 经过实际 `HarnessRun`、原驱动和 stdout
reader，沿产品未修改的 500ms protocol-interrupt 窗口收尾：该次清理阶段为
2.225ms，完整错误工具结果及原 request/attempt/native 身份均被采集，未产生
`native_collection_incomplete`，原进程组确认退出。这个数值只是一条协议可行性
观察，不是任意负载或任意工具的时延保证；超过产品期限仍保留完整性失败。
正常/Stop、ACK 与完整事件的两种顺序、同名 child、无残留、清理错误及缺事件
由当前 `test_codex_app_server_completion.py` 的受控宿主回归覆盖。
产品的退休与结果规则见 [根完成合同](../implemented/codex-backend.md#root-completion-and-selected-answer)。

## 上游流重试遗留的未完成消息

2026-11-03 在同一锁版 0.166.0、产品 revision `d40d830` 的 Octo 会话“5”
观察到当时未修复的边界。逻辑 request 为
`8b6f3810d1b0421897aa4bdf4f6f2767`，原生 turn 为
`01a1a00c-e5e0-7963-b6e1-29cc82d5733f`。原生 `logs_2.sqlite`、rollout、
产品结果与服务日志相互印证以下顺序（北京时间）：

- 14:20:22，Codex 开始消息 `msg_038e3a369ed7703f016ae97d2693348191a68a0cb368b31408`。
- 14:20:42，`codex_core::responses_retry` 记录流断开，TLS peer 未发送
  `close_notify`；原生运行时在 201ms 后重试 sampling request（1/5）。
- 14:20:53，替代消息 `msg_00803522baa3cd4f016ae97d3fe3048191b0878ec9a54ac763`
  完整返回；rollout 保存 `item_completed`、完整 response item 和 `task_complete`。
  产品 seq 684 保存完整最终答案，原生状态 `completed`，进程退出码 0。
- 同次产品 seq 685 却以 `protocol_incomplete` / `native_collection_incomplete`
  结束，指出旧消息未完成，`recording_integrity=incomplete`；已保存答案未丢失。

当时驱动把 started/delta 消息保留在 `_items`，仅原 ID 的 complete 使其结算。
原生重试可能放弃旧消息、以新 ID 完成；关闭连接时旧项仍为 started，完整性
检查因此把这次已恢复的原生轮次判失败。真实现场未保存完整 wire，不能据此
断言重试必有或必无 `error(willRetry=true)` 通知。

修复前产品经过真实 SessionManager、SQLite、Harness stdout reader 和
受控 stdio 宿主重放：`旧消息 started → 新 ID 的完整 final → completed terminal`
稳定触发同类失败；中间另加 `error(willRetry=true)` 仍失败，不含旧 started 的
对照成功（2 项预期成功断言失败、1 项通过，1.20s）。这证明驱动的状态组合
缺口，不是对真实 wire 的逐字重放。现有重试诊断用例没有遗留 started 消息；
缺完整 root item 用例没有原生重试，二者分别通过不能证明此组合正确。

后续处理必须保全真正缺失的消息、工具结果和已发生效果；不能用清空所有
pending item、换 ID 即视为成功或自动重跑整个请求来隐藏此问题。
当前[根完成合同](../implemented/codex-backend.md#root-completion-and-selected-answer)
已按 producer 完成义务修正；以上保留当时缺陷及首红证据，不代表当前实现或部署。

## Pending child work identity evidence

2026-11-04 的产品受控回归使用原 framed stdio peer，依次发送 pending child、
wait started、child turn bound 和 wait completed，不发送直接 child terminal。
修复前，原 wait 已捕获的对象在绑定时被替换，Owner/SQLite 最终把卡片保存为
interrupted；正确性断言先失败。原地绑定后，同一卡片保存 completed、实际 child
turn 与原 request 来源，根答案及结果不变。纯适配器负对照另外覆盖第二轮先开始、
旧 wait 晚到、直接终态重复和 wait 先于绑定完成。

该方法证明产品内部身份不变量，未运行新的真实0.166.0 provider 顺序探测，也不
证明真实 producer 必然能产生该序列；前述 G0 锁版观察不能补造这一可达性证据。
当前职责与回归入口归 [Native 子任务合同](../implemented/native-subagents.md)。

## 同锁版源码与局部重试核验

剩余修复使用相同源码 revision `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`，
没有改 Codex 源码或升级 CLI。核对链包括
[Responses sampling](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/session/turn.rs)、
[non-tool mapping](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/stream_events_utils.rs)、
[wire item adapter](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/item.rs)
与 Extension web-search producer。Core 的缺 action 会映射为明确 Other，
Extension 实际调用 started 可为 action=None；图像可选字段重叠，不足以判来源。
这证明哪些序列可能合法，不证明上述现场 wire 的每一帧；产品 stdio→Owner→DB
受控回归另行核对 sampling 成功、真实工具缺口、summary 和错误原因。

真实 0.166.0 CLI 另以隔离 CODEX_HOME、合成 API key 和本地拒绝端点探测。
CLI `config/read` 与 thread override 均确认
`features.unbounded_connection_retries=false`；配置 `stream_max_retries=1`、
`request_max_retries=0` 时，HTTP 断流观察到 2 POST；开启 WebSocket 后为
3 GET 加 2 POST，有限 fallback 后 native failed，原进程组退休已确认。
独立关闭端口探针从 stderr 观察到两次 `is_connect=true` POST 连接失败，
一次约 208ms 退避后以 `httpConnectionFailed` 结束。端点没有接收请求，
因此其 HTTP 记录为空不能误写成“零次连接尝试”。
[原生 retry 分支](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/responses_retry.rs)
解释无限开关、有限 sampling 重试和 fallback 的作用域；这些局部计数不代表
整轮所有模型/工具调用，也不构成墙钟总上限。探针没有使用账号登录或外部模型。

[认证 manager](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/login/src/auth/manager.rs)
可缓存、reload 和自行刷新授权，文件发布与 Octopus DB 更新又不在同一效果点。
本轮没有得到该进程实际采用某份目录凭据的可靠证明，因此产品采用
[条件失效合同](../implemented/harness-credential-reauth.md#4-run-loop-wiring-serversessionsturnspy)
的保守分支，而不从当前 auth.json 反推历史采用。

## 证据定位与边界

当次审计材料位于 `~/.cache/octopus-tests/codex-app-server-g0-base-20261101/`：

- `matrix-1/receipt.json`：命令和 14 个探针源码摘要，SHA256
  `fa66c2daad2e600a2f6c0055b6e6ebb8fd98b80d429a6d6f9723836cff4f5999`。
- `evidence-manifest.json`：trace/config/summary/stderr 摘要清单，SHA256
  `9f7ca33513d8e0c94c8f6628c52e632525f4d31ee2e2d6b90a87f85b62707c9c`。
- `live-nested-2/` 单列真实模型轨迹；先前失败目录记录探针前提错误，不混入通过数。

这些目录是可清理的当次材料，固定结论、版本、条件和反证保留于本页及
[原始路线快照](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/engineering-runtime-roadmap.md#codex-g0-evidence)。
协议实验没有修改产品 transport，也没有证明 Owner/DB/UI、正文消费者、Stop、恢复或 fork saga 正确。
后续 R2 产品交付有自己的验收；验证选择和证据复用由 [testing.md](../testing.md)维护，
此处历史通过不能替代任何新版本或新行为的适用检查。

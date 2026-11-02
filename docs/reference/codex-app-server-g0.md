# Codex App Server G0：0.166.0 锁版观察

本页保存 2026-11-01 的两组协议实验及适用条件。本次文档重组未重新运行实验。
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

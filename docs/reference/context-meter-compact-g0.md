# 上下文圆环与原生 compact：前置协议取证

本页保存 2026-11-07 的版本来源、隔离协议观察和尚缺的证据。它不表示上下文
圆环或 `/compact` 已在 Octopus 实现、验收或部署。普通执行的当前合同仍见
[Harness](../implemented/harness-layer.md) 与 [Codex](../implemented/codex-backend.md)。

## 固定环境与来源

Octopus 源码基线为 `96fd21734b775bda101127678c4186b4aef4906b`，源码声明的
主库格式为 7、Wiki 格式为 17；此次未读取或转换生产数据库。

| Codex | 目标源码 commit | 本机 binary SHA-256 |
| --- | --- | --- |
| 0.166.0 | `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` | `b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8` |
| 0.167.0 | `086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a` | `cdcb45aaa01d0f14e298985e09465aebccd47f64ecd298aea44814af7b62bbda` |

两者均运行 `--version` 并计算本地 digest。0.166.0 对应既有
[G0 锁版](codex-app-server-g0.md#固定环境与方法)；0.167.0 是本次实际选中的
standalone 版本，源码 commit 由官方 `rust-v0.167.0` tag 解析。没有执行新的
签名信任验证；本地 digest 与官方源码之间的关系是发行版本关联，不是二进制
可复现构建证明。报告阶段的 0.167 alpha schema 不替代这些观察。

这次读取了两版的 `CompactTask`、compact handler、local/remote 安装路径、
重算、token 协议与 App Server 请求处理。两版 local 安装、remote 安装、
token-budget compact、CompactTask 和 ThreadTokenUsage 文件逐字相同；其他
文件有变化，按相应函数核对，没有将整个 0.167 当作 0.166 的同义版本。

## 方法、原始材料与首次失败

每个版本各运行 local、remote V2 两组实验。每组使用三个独立 App Server 宿主：
建立合成历史后退出；新宿主精确恢复原 thread 并压缩后退出；第三个宿主再精确
恢复同一 thread，发送普通合成输入。检查后续 provider 请求确实含压缩后的历史。

每组独立 CODEX_HOME/cwd，不读取登录、真实会话或生产配置。provider 只在
loopback 提供合成 Responses SSE；`requires_openai_auth=false`、WebSocket 关闭。
remote 组以 provider capability 明确启用 V2，不借用真实 ChatGPT 凭据。
模型为 `gpt-6.1-sol/low`，配置窗口 100000，自动压缩阈值 1000000；显式关闭
async_compaction、token_budget、wake、goals 和 sleep。原生通知的有效窗口为
95000，不能以配置值 100000 替换它。

所有代理变量指向本地拒绝外部 CONNECT 的服务，loopback 在 NO_PROXY 中。
原生启动尝试访问 chatgpt.com、github.com，0.167 还访问 api.github.com，均被
返回 403，未转发；没有观察到 auth.openai.com。这里记录的是探针代理可见的
目标，未做系统级全部网络流量审计。实际生成/压缩/续聊共 12 次 provider 请求
均由 loopback 服务保存，不是真实模型验证。

首次实验整体退出 1：探针错误地将上述预期拒绝与 provider 协议错误放在同一
列表。四组业务断言都执行到末尾，12 个宿主均退出 0、无需发信号，stdout/stderr
reader 已 join、原进程组不存在。原始失败结果没有改写。随后对已保存帧、请求体、
回执和退休结果做独立检查，四组协议断言均满足；这不是一次重新运行成功的 suite。
探针已将拒绝的 CONNECT 独立记录，认证域尝试仍判失败；修改后没有重跑原实验。

[可重放摘录](context-meter-compact-g0.json) 保存四组从 compact 写入到原 turn
终态的连续 client/server 记录，保留原 RPC/thread/turn/item 身份、序号、原始
frame、相关 config/read 投影、退出结果及原文件 digest。它是**真实 binary +
人工 provider** 的观测，不是人工构造的 App Server 帧。resume replay 也保留在
该连续区间。完整请求体、stderr 和隔离 native 状态暂留本次测试运行目录，未把
大份原生历史或所有默认配置复制入库；摘要布尔值不能替代这些原始材料。

## Codex 的已观察事实

| 版本 / 路径 | ACK / turn-start | item-start / 重算 / item-completed / terminal | 重算 last.total / total.total |
| --- | --- | --- | --- |
| 0.166 / local | 10 / 12 | 13 / 18 / 20 / 22 | 12527 / 2580 |
| 0.166 / remote V2 | 10 / 12 | 13 / 14 / 15 / 17 | 12426 / 1240 |
| 0.167 / local | 10 / 12 | 13 / 18 / 20 / 22 | 12527 / 2580 |
| 0.167 / remote V2 | 10 / 12 | 13 / 14 / 15 / 17 | 12426 / 1240 |

序号均为各自第二宿主的记录序号，不可跨宿主比较。四组均得到空 `{}` ACK、
区别于原消息的新 turn、精确匹配的 contextCompaction item 和 completed turn。
重算通知先于 item-completed，`last.totalTokens` 非零、其他 last 分项为零；
累计 total 没有被重算清零。原生窗口内样本与累计消耗确为不同口径。

四组第三宿主均恢复原 thread。local 的后继输入含原生安装的合成摘要，remote
的后继输入含原生保存的 encrypted compaction item；不是仅核对 session ID 或
退出码。压缩模型的输入/output 统计不能当安装后的窗口：local 的重算 last
甚至大于合成 provider 报告的累计消耗，这是原生历史估计口径的正常区别。

四组中 compact 写入后、ACK 之前还收到旧 seed turn 的 token replay。仅按
“写入 compact 以后到达”取样会误认旧观测；必须核对原 thread/turn 及边界归属。
这些 replay 不能成为压缩后的样本，也不能凭到达时间解除持久快照的失效。

本次**没有实际观察到 compact 新 turn/item 早于 ACK**。两版
[请求处理](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/app-server/src/request_processors/thread_processor.rs#L2578)
均先提交 Op::Compact，再返回空响应，没有建立 ACK 先于任务通知的同步保证。
产品受控 peer 仍须稳定重放通知先到、丢 ACK 和失败无 item 的序列，不能把本次
四条 ACK 先到的轨迹推广成顺序保证。

两版
[compact handler](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/core/src/session/handlers.rs#L266)
先以 Replaced 中止旧任务；这次只执行空闲压缩，没有实测忙时替换。
[local](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/core/src/compact.rs#L425)
及
[remote 安装](https://github.com/openai/codex/blob/086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a/codex-rs/core/src/compact_remote_v2_installation.rs#L247)
的源码与实际帧均支持安装后重算先于完成。

async 与 token-budget 在这次实验中明确关闭，只读取了源码；没有运行自动
压缩、多 item 或候选丢弃实验。当前所选 credential 根配置没有显式设置这两个
feature，源码默认关闭；这不是对所有项目覆盖、requirements 或生产生效配置的
完整读取。实际启用配置的核验和相应分支验收仍需由后续实施/发布取得。

## Claude 来源与未关闭的能力门

仓库历史 [CLI notes](claude-cli-protocol.md) 记录 v2.1.143 的 VM0 形状，未留
本次需要的 usage/modelUsage/compact 原始帧。当前代码已经使用 stream-json
stdin，不能把历史 argv prompt 的观察算作当前 transport 的新通过项。当前
fake_claude_cli 是人工 fixture，也没有这些新字段。

官方 Python SDK v0.2.144 的
[parser](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.144/src/claude_agent_sdk/_internal/message_parser.py#L199)
直接读取 CLI assistant 的 usage/model/id、外层父工具与 session 身份，并读取
result.modelUsage。对应
[类型](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.144/src/claude_agent_sdk/types.py#L1177)
定义 contextWindow，并说明 canonicalModel 是计价身份；它不是根实际模型对应
map key 的验证材料。这些是固定版本客户端源码，不是 CLI 生产者的转录。

同份 result 类型说明 `subtype=success` 仍可能同时有错误，slash command 的
terminal_reason 可缺失。故不能仅看 success 判 compact/no-op，也不能要求所有
本地命令都必须带 terminal_reason=completed。官方
[命令文档](https://code.claude.com/docs/en/agent-sdk/slash-commands#compact-history-with-compact)
说明历史不足可成功但没有 boundary；现行文档的示例不能证明本机版本按当前
argv 发出所需字段。compact_boundary 的 pre_tokens 只描述压缩前用量。

仍缺有版本/digest/配置来源的当前 headless 有序流，尤其是根 input/cache 与
同 session、精确实际模型的 contextWindow 配对，以及 compact/no-op 后沿原
session 续聊。尚不能宣布双后端协议前置任务 resolved，也不能将 Claude 长期
显示未知当成功交付。此次未执行、探测、安装或登录 Claude；常规安装政策由
[验证规则](../testing.md#markers-providers-and-isolation)维护。

## 后续验证责任

| 证据 | 本次范围 | 后续责任 |
| --- | --- | --- |
| G0-C1 / C3 | 两版空闲 local/remote V2 的精确恢复、完成与安装后样本；真实模型未验证 | 产品 driver 关联与单次发送由 Codex compact 切片验证 |
| G0-C2 | 源码不保证 ACK 先到；真实轨迹均 ACK 先到 | framed peer 验证逆序、丢 ACK、准备失败、无 item 的失败终态 |
| G0-C4 | handler 替换活动任务的源码依据 | Owner 验证自然收束后发送、队列优先级、Stop 与取消 |
| G0-C5 | 已读自动/async/token-budget 源码；本次关闭后二者且未触发自动压缩 | 实际启用分支与多 item、未安装候选由后续后端切片验证 |
| G0-A1 | SDK 固定源码和官方文档；缺目标 headless 原始流 | 保持前置阻断，取得有来源的既有 capture 或经明确授权的专项验证 |
| G0-A2 / S1 | 尚无产品实现 | 后续 Claude Harness 和 Owner/DB/HTTP/WS 回归 |

协议可达性、产品模拟验证、真实模型和生产发布分别报告；历史 G0、源码及本次
受控 provider 不能替代未执行项。验证复用遵守 [testing.md](../testing.md)。

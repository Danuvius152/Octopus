# 2026-11-03：MCP 配置与运行资源边界

本页记录第四批取证的固定版本、重建方法、结果和不能外推的边界。当前行为归
[Harness](../implemented/harness-layer.md)、[Codex](../implemented/codex-backend.md)
与 [Wiki](../implemented/wiki-development-summary.md#文档字节支持与资源拒绝)，本页不决定生产部署。

## 版本、预算与方法

- 基线为 `7a830e07fb5c93b16a671d2a2d94a040d3b93e2c`；controller 扫描与准入前
  Wiki 测量使用原路径，准入后另测本批实现。最终代码审查清单为31个产品/测试文件，
  清单 SHA256 `6e901612ddd2aabca53d784fea393dc87aa956ad16aafa0d54feec7c6185fbae`。
- Codex **0.166.0**，binary SHA256
  `b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8`。
  Python 3.14.4、WSL2 Linux 6.18.54.1、glibc 2.43、20个逻辑 CPU
  （Intel i5-14600KF）、约16 GiB 内存；依赖使用该基线的仓库环境。
- 事先建议预算：单次扫描 p95≤1秒、本地准备≤3秒；Wiki 工作进程≤256 MiB、
  四并发合计≤1 GiB、read/index≤2秒。它们是本次判定线，不是既有 SLA。
  首次索引只测一次，因此只能和2秒目标比较，不能报告其 p95。
- 实验停止线独立于目标预算：Python 子进程 CPU 90秒、墙钟95秒、实际 RSS
  512 MiB。父进程20 ms采样 `/proc/PID/status` 的 VmRSS，超线停止进程组；
  子进程 VmHWM 记录自身峰值。native 准备单次15秒并每10 ms检查 native RSS。
  采样停止可能短暂越过阈值，不是硬内存配额。
- 使用隔离临时目录、合成正文/身份和本地无害 MCP。生产资料只读文件数量及大小；
  不读真实历史正文、不修改正式配置，不发模型请求或认证请求。文件生成后读取，
  **没有清 OS cache**；不能将首次调用称为冷盘读。
- 扫描单线程5次；四线程3批，记录提交到线程开始的排队延迟，并用5 ms ticker
  观察事件循环延迟。Wiki 每样本新建真实 Store，首次 refresh 一次，其余操作各3次；
  四线程读取同一个已索引文件。p50 为中位数，p95 取排序后 `min(n-1,int(.95*n))`，
  这些小样本的 p95 实际为最大值，不能视作稳健总体分位数。

只读规模统计：history 1017文件、2,288,060,931字节，p95 6,838,733、最大
238,176,033字节。Wiki Markdown 174篇、最大142116字节；HTML 19篇、最大
4710041字节；TXT 71篇、最大2131400字节。因最大文件超出原计划，测量前将普通
history 上限从50 MiB扩到256 MiB、Wiki从4 MiB扩到8 MiB；compacted 大行仍仅测
名义10 MiB。没有将真实库推向 OOM。

## MCP configuration and discovery

用独立 HOME/CODEX_HOME 与受信项目目录，配置一个本地 FastMCP `proof` 工具，
仅返回固定文本，无外部效果。分别准备 stdio 与本地 HTTP `/mcp` 服务；HTTP
middleware 只记录合成 Authorization 标记。按产品 `_mcp_config_args` 生成 invocation
字段，用同一 binary 执行 `mcp list --json` 作解析诊断；真正发现走如下控制序列：

1. 启动 `codex app-server`，initialize（experimentalApi=true）后发 initialized。
2. `config/read(includeLayers=true,cwd=project)`，保留 active layers/effective config，
   对选中 namespace 调用产品准备检查。
3. `thread/start` 只创建控制对象，然后 `mcpServerStatus/list(threadId=返回的id)`。
   记录 connected/disabled、工具列表及合成 HTTP 身份收据，关闭 stdin 并等待进程退出。
   **不发 turn/start**。探针即使发现 product_refusal 仍采集 native 发现作为反证，
   不表示产品会绕过拒绝。

| 隔离样本 | 观察 |
| --- | --- |
| 空配置 | thread 工具列表为空。 |
| home 或受信 project 的额外 namespace | 仍可连接并发现 proof；Octopus 选中集合不是原生 MCP allowlist。 |
| 同名 HTTP 的 URL/bearer-env 字段覆盖 | 生效为 invocation URL/身份，可发现工具；home bytes不变。 |
| 同名 HTTP ↔ stdio | URL/command 残留导致 native 配置拒绝；产品在业务输入前给可定位诊断。 |
| 同名 http_headers 或 stdio env 残留 | 产品检测注入身份冲突；不重写原生文件补救。 |
| namespace inline table，或 `mcp_servers={}` | 表仍合并，不能据此清除 native entries；Research 只保证无 Octopus 注入。 |
| 同一 home、同名 ask、两次不同 invocation | HTTP 收据分别为 Bearer synthetic-A / synthetic-B，未串写 home 配置。 |
| enabled=true、自定义 startup=20s/tool=45s、本地默认 environment/空 deny list | 均 connected 且发现 proof，产品允许；不能把未覆盖的无害策略都判成冲突。 |
| enabled=false | native disabled、无工具；产品保留对所选 namespace 的明确拒绝。 |
| 不注入前一次的 namespace，改注入 other/third | 只保留本次注入和 native 条目，没有前次 invocation 写回 home。 |

Claude ordinary 的 strict 配置和 text worker 的 tools=[]/none 由现有离线合同覆盖，
本次没有启动/登录 Claude。工具发现、工具调用当前授权和效果提交分别取证。
当前 [官方 MCP 说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
用于核对 enable/timeout 字段语义；本表的合并和发现结论来自上述固定 binary。

## Controller scan and native preparation

重建普通扫描样本：每行以 `json.dumps` 编码一个 `response_item`，payload 为
assistant message、`output_text="synthetic "+"x"*3800`，末尾换行。写入
`floor(size*1048576/len(record))` 条；首行 session_meta.id 为 synthetic-thread，
末尾追加 `controller_message("synthetic current controller")` developer 消息。
扫描 `_latest_checkpoint(path,"synthetic-thread")` 并核对输出；另以 wrong-thread
确认拒绝。变体在普通样本后追加：

- compacted：replacement_history 含一条长为 size*1048576 的 assistant 文本和当前
  controller；名义10 MiB样本实际约20 MiB，其中一行约10 MiB。
- rollback：`event_msg.payload.type="thread_rolled_back"`，期待无当前 checkpoint。
- invalid：controller 的 sha256 前缀加 broken；legacy：旧 `Current Octopus controller
  snapshot for this turn. Legacy` 文本；期待对应需更新分类，不复用前一有效值。

| 普通样本 MiB | 扫描 p50/p95 ms | 四线程批次 p95 ms | 线程排队 p95 ms | 进程峰值 MiB |
| --- | --- | --- | --- | --- |
| 1 | 0.985 / 1.616 | 5.829 | 1.972 | 49.38 |
| 10 | 9.811 / 10.243 | 47.136 | 2.103 | 49.38 |
| 50 | 48.935 / 49.796 | 230.323 | 1.900 | 49.37 |
| 256 | 252.147 / 259.297 | 1158.363 | 2.888 | 49.42 |

普通样本 loop delay 最大4.591 ms；compacted 的扫描 p95 29.214 ms、四线程116.068 ms、
进程峰值169.16 MiB，loop delay p95 9.951 ms、最大38.142 ms。10 MiB rollback/
invalid/legacy 扫描 p95 为11.375/9.810/11.782 ms。不能由普通256 MiB推导同尺寸大行成本。

真实准备另用 native thread/start 返回的原 id/path，在其原路径写合成 rollout。
session_meta 顶层与 payload 都带 `timestamp="2026-11-03T00:00:00Z"`，payload
含 id、cwd、originator=codex_cli_rs、cli_version=0.166.0、source=cli、
model_provider=openai；其他记录也加顶层 timestamp。经原 HarnessRun/AppServerDriver
执行 exact resume，输入 recorder 为内存空实现，拦截所有 `write_input`：只允许
观察待写的 turn/start 并在管道写入前抛出专用停止异常；原退役入口负责清理。
计时从 run.start 开始到该拦截点，包含 spawn/控制握手/配置/原生 resume/controller
准备，**不含 Manager数据库录制、完整准入或模型执行**。

| 名义样本 | 三次准备 p50/p95 ms | 到达业务写前拦截 |
| --- | --- | --- |
| ordinary 1 MiB | 304.241 / 329.855 | 3次 |
| ordinary 10 MiB | 333.368 / 359.458 | 3次 |
| ordinary 50 MiB | 528.149 / 610.833 | 3次 |
| invalid / legacy / rollback / compacted 10 MiB | p95 350.030 / 353.647 / 357.394 / 396.863 | 各3次 |
| ordinary 256 MiB | 无成功准备耗时 | 0次；三次 native RSS均越过512 MiB停止线 |

扫描处于本次预算内，保留现有 to_thread，不新增 cache；native 256 MiB准备未通过。
单 RPC原超时20秒不能用作整段准备预算，也不能把合成样本成功扩大成真实恢复保证。

## Wiki Store cost and admission

每样本使用新 vault/state 与固定合成 vault id。内容为下面的 unit 重复
`floor(size*1048576/len(unit.encode()))` 次；HTML 两种另包 `<html><body>` 与
`</body></html>`。记录**实际字节**，不把名义档位当精确大小。

```python
units = {
    "markdown": "# Synthetic heading\n\n中文正文 😀 alpha beta gamma.\n\n",
    "text": "中文正文 😀 alpha beta gamma.\n",
    "html": "<p>中文正文 😀 <strong>alpha beta</strong> gamma.</p>\n",
    "html-long": "<span>中文😀alpha</span>",
}
```

先建空 WikiStore，再写文件；测第一次 refresh，继而测 read_snapshot(length=16000)、
public(row)、真实 derived_from 边触发的 source revision 刷新、refresh 和四线程
read_snapshot。依赖样本是独立小 Markdown，边绑定真实源 id/revision；更新源 mtime
触发刷新。每次最后 close Store。仅测 extract() 不能替代这些路径。

| 准入前档位 | 单次首次索引：MD / TXT / HTML / 长行HTML，秒 | 全过程峰值：同序，MiB |
| --- | --- | --- |
| 0.25 MiB | 1.055 / 0.563 / 0.656 / 0.455 | 204.82 / 184.05 / 184.19 / 174.84 |
| 1 MiB | 5.369 / 1.063 / 1.345 / 0.748 | 300.32 / 226.25 / 226.00 / 203.89 |
| 4 MiB | 停止 / 3.123 / 4.292 / 1.939 | >512 / 425.11 / 385.64 / 335.02 |
| 8 MiB | 停止 / 停止 / 停止 / 3.681 | >512 / >512 / >512 / 507.22 |

单次0.5 MiB密集 Markdown首次索引2.093秒、峰值229.28 MiB。部分普通格式在1 MiB
仍满足建议预算，统一256 KiB因此是保守支持政策，不能声称它是必要或最大安全阈值。
只读统计中会受影响的是6个HTML和1个TXT、0个Markdown；原文件不删除/截断。
四并发是同进程、同文件、已有索引读，不证明四个独立文档并发首次索引满足总预算。

准入后用0.24 MiB再次覆盖四格式，实际251604/251640/251666/251662字节；首次索引
0.995/0.518/0.591/0.444秒，读 p95 19.320/10.824/11.312/9.830 ms，峰值
201.81/181.40/184.46/175.00 MiB。public p95 5.62–6.70 ms，dependency revision
6.38–7.13 ms，refresh 67.09–74.76 ms，四读批次65.58–101.48 ms。
8 MiB四格式全部明确拒绝，索引记录拒绝约67–69 ms，read拒绝约11–12 ms，
峰值121.73–133.20 MiB；原文件保留。

原0.25 MiB的两种HTML实际262166/262162字节，略超过262144，不能作为精确边界测试。
127/128/129字节的缩小政策回归另行验证 limit−1/limit/limit+1；实际代码常量256 KiB。
正确性覆盖未知大小、增长、替换、raw入口、旧FTS保全、同hash缩小/重启恢复、来源
version_unknown、写入/Sapphire写前拒绝、锚点目标、整批snapshot两侧预检、恢复cursor
与迁移原文保全。资源政策不改变任意外部shell访问的权限边界。

## 方法修正与验证证据

早期 RLIMIT_AS=512 MiB限制的是虚拟地址空间，甚至阻止 native 初始化，不能用作
实际RSS结论；已另存失败材料并以上述VmRSS复测替代。Linux rusage.maxrss也可能
继承启动器pre-exec峰值，改为进程VmHWM。明确 MemoryError 才能按该类失败报告；
exit70且空stderr仅能报告异常退出。全局 MCP inventory不能证明选定 thread 工具面，
最终按 threadId发现；早期缺timestamp的native准备样本也未记为通过。

原始 JSON 的 SHA256 摘要如下；本页已保留重建配方和判定所需数据，不依赖临时脚本
路径作为唯一说明。原始任务材料仍在活动工作区，未作为产品数据或默认门禁加入仓库。

| 原始结果 | SHA256 |
| --- | --- |
| mcp-results.json | 85df98a0ea2a6545d5c2866f1c2e785e41b25a3abdb00f550ffbb920cbb0d7d6 |
| mcp-neutral-results.json | de26848f8f314e25e6cb437200f5049700a20d68587d62f9b22d773add02c05e |
| resource-results.json | 3115ceccffef69c1c500e6afcd5a22eb57745e1c8ae9cfa5e6fc80ebd3c486df |
| resume-results.json | 544608ddcbf9e4ec2249947be5f0a659f146938d48fad485a082f8b1a8a416d5 |
| post-budget-results.json | 3c2d6290336ac2e32d1dbae803f0b1fd1d1833e5fb59d34dc63f0849fa08a55a |
| wiki-midpoint.json | 6dae4e9cef768b07c2c01f66afc915549e0284c401f2c83bfbe94e267fd8c228 |

本批完整 check.sh 的其他八项门禁通过；backend首次6382通过、8失败：七个旧文件
观测点需迁到实际有界reader，另一个YAML集成用例先触发新增文档预算。修正保留原
一致性、字节与锁断言，十个完整相关模块189通过、3 deselected。随后独立审查发现
snapshot当前目标、锚点读取和MCP无害字段误拒，补反例修正后五完整模块161通过，
mypy252源文件通过；未重复运行未失效套件。Standards/Spec支持审查及Vera固定代码
独立审查均已解决其证实问题。以上是当次验证记录，不是维护中的最新测试数，也不
代表代码已推送、发布或在真实库上启用新准入政策。

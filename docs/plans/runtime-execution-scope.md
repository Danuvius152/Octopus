# 运行时执行扩展：暂停范围与恢复条件

> **Status:** paused — 按已交付 Codex R2 重基准七项执行扩展，保留更强承诺及独立 R06 核验的恢复条件。

普通 Codex App Server 已交付：每 attempt 宿主与原生根绑定、start/steer 的原生
接纳证据、活动根内协作、有限尾部保全与有界退休均属当前实现。Claude 已有
条件进程复用。当前保证见[Codex](../implemented/codex-backend.md)、
[输入](../implemented/input-reliability.md)与[执行合同](../implemented/architecture-contract.md)。
不能再把 App Server/HOST 或原七票一概写成“未实施”，也不能据此宣布整包完成。

## 七项旧执行范围按断言重基准

票号仅供历史溯源，与已完成的账户搬迁专项无关。暂停的是尚未承诺的增量，
不会因别的改动交付而自动恢复，也不进入当前常规发布门禁。

| 原票 | 当前已有部分 | 仍暂停或有条件的增量 |
| --- | --- | --- |
| 22 调用与资源 | 活动根协作、有界退出采集、结果终态与资源退休分开 | 任意 post-final 多层子树自动闭合，以及超出有限采集的保证。 |
| 23 先退休再绑定 | Codex 每 attempt 宿主/lease、原身份约束；Claude 条件复用 | 强制每个新 Claude 逻辑请求换进程等更强政策，需单独选择。 |
| 24 送达与控制 | Codex start/steer 的 not_sent/unknown/accepted/rejected 证据 | 全部 argv/stdin/Answer 通道更强的实际消费证明；accepted 不等于已处理。 |
| 28 输入与终态 | 普通输入、唯一 terminal 关联及原生输入证据独立保全 | 所有通道已消费输入集合的更强原子结算语义。 |
| 31 消费者组合 | 接纳、返回、历史、问题、停止与现有消费者回归 | 上述尚暂停承诺对应的组合验收；没有宣布本次已运行旧验收矩阵。 |
| 42 指南与权限 | 普通/leaf/one-shot 的真实装配、配置捕获与权限边界 | 将来新宿主政策需要的 persona/指导调整。 |
| 43 外部大小 | RPC、argv/env、Claude frame 等按实际传输区分 | 尚未证明的全通道组合大小保证，不将 RPC 画成旧 exec 的 E2BIG 模型。 |

原票及历史依赖见[固定旧稿](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/runtime-execution-scope.md)。
当时的 22→23→24→28→31 顺序只适用于恢复的相关责任；42/43 在所选 23
政策之后按归属安排，31 不是 42 的额外依赖。已由当前实现吸收的责任不重复开工。

## 恢复条件与保留反例

恢复须用户明确产品范围与维护责任，并取得实际 provider 能支持该强保证的新
证据。[G0 参考](../reference/codex-app-server-g0.md)分别保存 Recursive 反例和
Base 有限通过，不能互相替代。静默等待、强制 wait 提示词、自动 exec/resume
补报告、provider 补丁或旧实验都不代替协议保证。

选择恢复后，先检查最可能推翻设计的完整场景：

- **资源与旧请求：** 正文可见但工具未收齐，原 receipt 终态后又来 idle/子结果，
  与新输入、Stop、旧 Wiki 调用交错。partial、usage、已知失败与原身份保全，
  不重开旧 App/委派响应；父 success 不掩盖已知 child 失败。
- **绑定与权限：** 原资源仍 held 时，新请求需要不同 context/授权。先满足
  选定退休政策再绑定，旧 grant 不延长/转绑。若选择每请求换进程，明确额外
  启动成本及丢失未到尾部的取舍；退休 CLI 不取消独立 BG/Research/委派。
- **输入与结算：** 一个已确认 steer、另一个 unknown 与 Stop/慢发送交错，
  在提交两侧中断。仅有可靠证据的集合进入所选结算，pipe drain 不冒充 provider
  确认，迟到证据不扩大已结算集合，unknown 不自动重发。
- **消费者与指导：** 原 receipt 后旧 Stop、idle、独立返回交错，输入一次接纳，
  ACK 丢失仍幂等，one-shot 与人工优先保留。普通请求/隔离 leaf 的工具承诺
  匹配真实装配；动态权限不被预热缓存，自定义 persona 不被覆盖。
- **大小边界：** 等字符数、不同 UTF-8 字节数的正文叠加 env/指南，分别验证
  合法完整传输、接纳前拒绝、接纳后真实失败；按 RPC、单 argv、完整 argv/env、
  Claude frame/Answer 分别取证，原输入可核，不靠截断正文解决。

raw/trace 完成屏障、自动补报告、动态 bearer、ACP/宿主池等不随普通修复启动。
Codex 自定义角色是[独立 proposed 计划](codex-native-roles.md)，不与完整递归绑成一包。

<a id="paused-model-validation"></a>

## 暂停的模型配置核验：R06

此项从 Wiki 计划移入，保持用户此前暂停决定，且与递归尾部没有先后依赖。
用户恢复 Claude 范围后，先按当前代码和 provider 可用性核验，不复制旧缺陷结论。

Claude session 从 Codex Agent 继承 reasoning/tier 时，按实际 backend 与字段来源
处理：不消费的继承值不参加校验；明确向不支持 backend 提交非空值仍拒绝。
省略保持绑定、null 恢复继承，不清除 Agent 默认或丢 transcript 来绕开。
完成条件覆盖跨 backend/credential、session override、归档/运行中修改、持久化
与 preflight 一致性，具体当前规则归[模型合同](../implemented/model-reasoning.md)。

验证选择和成功证据复用归[testing.md](../testing.md)。本次文档重组未恢复这些
实施、Claude live 或额外性能实验。

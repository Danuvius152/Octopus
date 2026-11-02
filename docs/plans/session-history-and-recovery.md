# 会话分支、文件归因与安全恢复

> **Status:** proposed — 补差异预览、dirty 起点与文件效果归因，保全人工修改并保守处理无主副本。

full-copy fork 和 seq rewind 已有实现；当前行为见
[fork](../implemented/session-fork.md)与[rewind](../implemented/session-rewind.md)。
本计划解决恢复时的具体文件安全和可理解性，不重新建设历史系统。

## 当前限制与第一段交付

[safe_revert_preflight](../../server/fork_helpers.py)主要通过 dirty 路径与 Agent
touched 路径集合检查风险，并依赖 fork 起点等前提；尚不能凭路径记录证明
同一路径中每一字节是谁修改。文件路径有记录不等于完整 pre/post blob 或人类
编辑归因。不能把当前 safe-revert 概括成能自动保全任意同路径人工改动。

首选交付是恢复前差异预览与明确拒绝/选择规则。用户能看到将影响哪些文件、
依据哪个起点、有哪些无法归因的修改；无法安全判断时保留文件并说明原因。
若选择 dirty 起点快照，说明采集范围、内容/权限、失败行为及保留退出策略，
与[数据保留计划](data-retention.md)一致，不把快照默认为永久副本。

## 分支比较、停止与恢复

分支比较先只读，明确比较的是对话、原生历史、代码还是文件系统状态。自动
merge、provider undo 和字节级效果归因是独立后续选择，不能从能展示 diff
推导自动合并安全。

忙时 rewind 经已有 Stop 与原资源退休关系协调，文件仍可能被写入时不得执行
貌似成功的回退。旧 attempt 的晚到输出仍归原历史，不覆盖新分支。外部副作用
仅可披露与定位，回退代码不表示邮件、网络写入或数据库效果已经撤销。

无主 fork 副本先证明归属、活动进程和引用，提供可核对的预览；不能只按目录名
或年龄自动删除。新增持久快照与关系同步更新[账户搬迁](../backup-and-migrate.md#account-export-and-import)。

## 条件候选

| 能力 | 启动条件与完成边界 |
| --- | --- |
| dirty 起点快照 | 限定可管理 Git 目录，保留实际 tracked/untracked/index、排除项与体积上限；重启、取消、迁移和 GC 各有 owner。整目录复制不作为每 turn 免费快照，恢复前重新检测后续编辑。 |
| 无 row 副本处置 | 对以后创建的副本保留可证明归属的 manifest/creation state，预览大小/来源；活跃/未完成恢复材料、未知来源目录及共享 parent cwd 不自动删除。 |
| 精确 Codex native rewind | 先证明产品 message seq 到 native input/turn 无歧义且截断粒度兼容。只有兼容分支点启用并与 replay 对照，其他继续 replay，不外造 native transcript。 |
| 跨 session 上下文摘取 / Channel rewind | 先选引用摘录或移动语义，明确附件/来源/账户；Channel 使用稳定消息选择及与浏览器一致的 preview/副作用确认。引用历史不冒充原生历史 graft。 |
| Codex CLI handoff/pull | 有明确同账户同 backend 续接场景后，验证官方可读/可恢复格式、附件与身份；当前同 home thread/fork 和账户备份不能证明原生 CLI 导入导出，不伪造 native 状态或自动跨 provider 转换。 |
| Connector 定向补偿 | 仅为有真实 undo API 的 endpoint 选择，绑定原动作、权限与时间窗口，经用户明确确认。补偿是新外部动作，单独记录结果，不把原历史改成“未发生”。 |

运行中 rewind 先提供等待闲置或停止当前工作后再 rewind 的清楚选择，原资源退休
后重新取得 preview。实时不停机快照仍是独立条件研究。

## 完成条件

选定恢复操作先展示实际差异，人工同路径修改、dirty 起点、无主目录及运行中
写入都有明确保留/拒绝行为。原历史与分支身份可查，失败不半覆盖文件；只读
比较不改变内容。适用测试从最易误删人工修改的场景开始，遵循[测试政策](../testing.md)。

来源：[上游 fork 目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/session-fork.md)、
[rewind 后续目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/session-rewind.md)。

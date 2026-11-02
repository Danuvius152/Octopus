# 第二执行环境与 Runtime provider

> **Status:** proposed — 有真实第二环境需求后，验证文件、网络、历史、取消与资源退休的完整 provider 边界。

本计划继承上游独立分支的 Runtime provider 方向，尚未选定第二执行环境。
当前本地 Harness、会话 Owner、App backend 与安装生命周期已有职责边界。
DeepSeek 是模型 backend，接入本机 DSH 不以抽象远程执行环境为前置。

## 启动条件与能力盘点

先选一个 Local 无法满足的真实工作和第二环境，说明它需要的代码/文件位置、
数据持久性、双向网络、凭据、成本与故障边界。按当前 fork 重新盘点消费者，
不能把上游分支描述成已合入 main，也不能把早期 exec 假设套到 App Server。

盘点须覆盖普通交互、tool-free 调用、Research leaf、App 进程、命名委派、
schedule、Wiki、输入/结果读取、Monitor、历史与 fork。支持、不支持和需本地
协作的能力逐项明示；不能只因能启动一条 shell 就把 provider 标为可用。

Local 盘点也包括 BG、登录、附件/spill、路径转换和回调；不沿用上游“七个
spawn 点”当完整消费者清单。provider 的显式环境不能额外继承未授权 env，
具体本机 allowlist 强化由[租户计划](multi-tenancy-gaps.md)拥有。

## 所有权与第一段交付

Provider 拥有选定环境内的执行资源、文件访问和原资源身份；SessionOwner 仍
拥有输入/结果，领域任务仍拥有业务效果。一次 attempt 绑定的远端资源不能在
断连后被猜测为已死亡，也不能把发出 signal 作为资源退休证明。

第一段用同一工作在 Local 和一个真实第二 provider 跑完整闭环：准备输入、
启动、问题/控制、读取结果、保存必要文件、停止、确认退休。明确远端失联时
哪些事实可查、哪些保持 unknown，如何重新关联原资源，以及何时可安全释放
额度或启动替代尝试。恢复不自动重跑未知外部效果。

历史/fork 的文件与原生会话身份一起处理。远端路径不冒充本机可读路径，导出
明确收集、排除、外部依赖及不能复活的进程/lease，遵循[账户搬迁](../backup-and-migrate.md#account-export-and-import)。

远端 file primitives、fork step、端口/回调、离线 ensure 诊断和 held pool 的
公平策略按真实 provider 所需能力交付，Local 同时实现新增合同。hostile-tenant
隔离只由实际声明隔离的 provider 证明，功能 conformance 适用于所有 provider。
每账户数据库在有明确物理隔离需求时才讨论，不把分库当本机逻辑账户边界缺失。

## 完成条件

真实第二环境验证双向网络、文件与持久状态、原生历史/fork、取消、失联、恢复、
晚到事件和资源退休；现有 Local 的完整能力继续通过适用检查。原资源身份、
账户权限、输入接纳和结果提交保持可追踪，不新增第二份任务完成状态机。
只做模拟适配器或安装检查不能宣布第二环境可用。

## 条件范围与来源

Kata/k3s、云控制面、裸金属采购、统一 wire 协议和静态 CLI 均需独立产品需求；
不因接口可能有用而先建平台。验证遵循[测试政策](../testing.md)，当前未运行任何
远端实验或基础设施操作。

来源：[上游 runtime-provider 分支设计](https://github.com/archeryue/Octopus/blob/01c66314c003b8cd0246fd2b6f3a2b6943842626/docs/plans/runtime-providers.md)，
固定于独立分支；不代表上游 main 或本分支已有实现。

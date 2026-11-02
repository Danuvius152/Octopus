# Codex 原生角色映射

> **Status:** proposed — 将已保存的角色定义映射到 Codex 原生配置，保全 persona、权限与原生工作身份。

本计划只补 Codex 自定义原生角色。当前普通 Codex 已使用 App Server，原生
helper 的观察、保存详情与有界退休已有实现；角色映射未因此成立。
当前行为见 [Codex 合同](../implemented/codex-backend.md)与
[原生子任务合同](../implemented/native-subagents.md)。

## 当前差距与设计边界

账户 Agent 的 persona、backend、model、reasoning、speed 与命名委派已有各自
配置和继承规则。Claude 的原生子角色装配也不等于 Codex 具有同样的角色配置。
本计划需要打通“保存定义→运行捕获→provider 配置→实际原生角色调用”的链路，
不能只生成一个不会被当前宿主读取的文件。

角色配置属于账户 Agent；一次逻辑请求在准备阶段取得配置锁时捕获有效配置，
自动重试沿用原捕获值。Harness 负责把允许的字段映射到 provider 可识别的配置
及其生命周期，SessionOwner 继续拥有输入和完成。角色映射不新建另一套命名
Agent 委派，也不修改已有 persona。

## 首选交付范围

1. 锁定实际 Codex 版本，调查可用角色发现、配置字段、模型/effort 支持及加载时机。
   明确哪些字段可映射，哪些组合需拒绝；未证明的能力不显示为可用。
2. 从保存配置生成当前运行实际读取的角色配置，规定账户、会话、宿主的作用域，
   以及修改后何时生效。临时文件随其宿主退出，不污染宿主全局用户配置。
3. 将一次自定义角色调用与原生 child 身份、原根 attempt、有效角色版本关联。
   父卡和保存详情能解释实际使用的定义，不能只显示表单中的下一轮选择。
4. 延续当前工具、Wiki 与来源边界；角色 persona 和工具返回不授予额外权限。

## 完成条件

- 一个保存的自定义角色能经公开入口被真实原生调用，且定义版本与运行证据可追踪。
- 修改、继承、清空 override、无效字段与不支持组合具有一致行为；拒绝不会改坏旧配置。
- 两账户配置隔离，撤权后的新使用被拒绝；已有 Claude 角色与命名委派回归通过。
- 角色文件和宿主资源随原生命周期退出，取消和迟到事件仍归原工作身份。

验证遵循 [testing.md](../testing.md)，先检查配置“存在但未被使用”的反例。

## 条件范围与非目标

原生 child 的人工控制归[协作控制计划](agent-collaboration-controls.md)。任意
post-final 多层递归自动继续归[暂停执行范围](runtime-execution-scope.md)，不是
角色映射的前置条件。实施本计划不恢复完整递归承诺，也不开放所有 Agent 的全部工具。

来源是[旧工程路线中的未完角色目标](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/plans/engineering-runtime-roadmap.md)。
它保存当时的决策背景；当前配置事实以源码与上述合同为准。

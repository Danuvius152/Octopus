# 票53：代际准入与回归依据

本次只完成受管启动对旧代 writer 的接管，不改输入状态模型、provider finality、
Wiki 恢复算法或发布流程。旧输入的 unknown、原文、执行身份和重送去重均保留。

## 责任与完成点

- `InstallationOwner.acquire()` 先取得安装独占权，核验实际 systemd MainPID、
  cgroup 和控制组终止策略。生成 unit 的 prestart 检查先确认组内无旧 writer。
  首次从非受管部署移交仍须按原规范确认旧执行已结束；此修复不提供跨安装清场。
- 输入恢复在同一个事务中分类旧记录并捕获其中已 claim/unknown 的执行代次。
  只有已核验的受管启动才把这份冻结集合交给 SessionManager 作为退役依据。
  集合只活在本次初始化中，不增加持久状态，不修改原执行证据。
- 每次初始化使用新 dispatch generation。新准入与实际派发共用
  `require_retired_writers()`，只有冻结集合中的旧代免查历史 PGID。
  本代迟到 spawn、stdin、工具及退休仍由原 Owner 和原资源的 join 收束。
- Dismiss 只提交显示处置，通知不可用不否决已提交结果，也不复活失败 Owner。
  没有启动证明的 standalone 沿用保守的 process-group/boot 检查，不发送历史清理信号。
- 受管启动仍在既有全服务恢复门完成后才开放 producer；存储错误沿用 fatal。

## 回归证据

`tests/test_service_supervisor.py` 使用一次性 systemd user unit、完整隔离的配置与
存储，以及 Python 子进程回放 Codex 协议；不访问模型、不重启生产服务。

1. `before_spawn`：公共 WebSocket 输入已 claim，在实际 OS spawn 前退出73。
2. `before_registration`：真实子进程已经出生，但数据库 PGID 仍为空时退出73。
   重启后直接核对原 PID 已消失。两个窗口均要求新输入实际产生文件效果，原输入
   仍 unknown，同身份重送没有重跑原工作。
3. `late_spawn`：本代真实子进程出生后暂不返回句柄。Stop 设置暂停并保留原请求；
   新人工输入可以被接纳排队，但不能执行。原句柄返回并实际退役后，只有新输入
   产生效果，原输入结算 interrupted，同一服务 PID 持续运行。

前两个测试在修复前均命中目标窗口并因新输入被拒绝而失败；修复后均通过。
第三个测试验证已有 Owner 责任没有被启动证明绕过。

`tests/test_submission_recovery.py` 另外验证恢复协议：启动后新出现的 unknown
不属于旧代集合，即使 Dismiss 也必须阻止新准入和已排队输入的实际 dispatch。
原 standalone 重启拒绝测试及 `test_session_owner_review_fixes.py` 中 Dismiss
提交顺序、通知不可用、失败 Owner 不复活的覆盖继续保留。

本次原始日志保存在 `.scratch/minimal-architecture-20261020/validation/ticket53-*`。
最终候选的 fast gate 与三项 supervisor 回归由 Octo 执行，独立审查复用同版证据。
没有将未运行的 live Codex、Claude、浏览器 E2E 或生产部署计为通过。

## 最终结果

受审代码 tree 为 `9b2fe3af4c8fe5b9f8ee939125d5bb4aa7f403fb`；本节只追加结果，
不改受审源码和测试。Vera 的 Standards 与 Spec 两轴均为0项发现。

`./scripts/check.sh` 在后端测试中触及1200秒总时限，当时已经输出4890个通过标记，
未得到完整进程退出结果。按本次 collection 的5098个选中 node ID，从首个未完成
用例续跑其余208项，得到 `208 passed in 59.50s`；前4890项没有重跑。
原日志、collection、续跑列表及数量核对保存在 `ticket53-progress.txt` 与对应日志。
这是同一候选的一次分段完整覆盖，不宣称原 check 进程正常退出。

fast gate 的其余检查均取得成功退出：ruff、mypy（217个源码文件）、eslint、
vitest（58个文件／496项）、`tsc -b --force`、生成合同及文档索引。
最终3项真实 supervisor 回归为 `3 passed, 10 deselected in 14.05s`。

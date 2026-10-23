# before-takeover → HEAD 代码审阅报告

审查日期：2026-10-21（Asia/Shanghai）\
固定版本：`before-takeover` = `88d2284b2b2493001be2e945df375d7a228ccc1f`\
被审对象：`main` = `5ccaa7c`，共 75 个提交，另含未提交工作树\
规模：615 个文件，+119,733 / -10,119（生产代码约 4 万行，其余主要为测试与文档）\
审阅方式：规范（Standards）与规格（Spec）两轴并行复核，随后逐条静态核验。\
限制：审阅沙箱为只读，`pytest`、`ruff`、`mypy`、`tsc` 与生成契约检查均未能执行；\
下文全部结论来自静态阅读，不代表门禁已通过。

## 1. 范围与方法

- 提交范围：`git log before-takeover..HEAD`，最近三个提交为 `0594d87`、`5d99de3`、
  `fc23b96`（单写者会话与存储生命周期）与 `5ccaa7c`（文档）。
- 工作树：`git diff before-takeover` 含 `server/service_installation.py`（+132/−10）、
  未跟踪的 `scripts/check-storage-layout.py` 与 `tests/test_service_layout.py`，以及
  `docs/review/session-owner-20-acceptance-20261021.md` 的删除（见 §5，已修复）。
- 规格来源：提交信息未引用 issue，因此以仓库内计划文档自身的 Status 与合同为准，
  重点为[最小机制执行计划](../plans/minimal-architecture-execution-20261020.md) §3.2（票 20）、
  [token-rotation](../plans/token-rotation.md)、[wiki-bug-remediation-20261017](../plans/wiki-bug-remediation-20261017.md) §16，
  以及两份日期版规范 [octopus_implementation_spec_20261020](octopus_implementation_spec_20261020.md)、
  [octopus_minimal_architecture_spec_20261020](octopus_minimal_architecture_spec_20261020.md)。
- 覆盖局限：约 120k 行新增内容无法逐行通读。生产代码优先覆盖
  `server/sessions`、`server/wiki`、`server/harness`、路由层与前端五个热点；
  615 个文件中约 600 个未深入阅读。真实并发行为、Wiki 恢复/租约路径、
  `web/src` 其余组件与 `web/tests` 未核实。

## 2. 规范（Standards）

### 2.2 判断项（代码异味基线）

4. **重复代码** — [owner.py](../../server/sessions/owner.py)：`call`（114–129）、`call_question`（131–159）、
   `call_runtime`（164–193）是同一段入队逻辑的三份拷贝：require → 计长 → 容量检查/等待 →
   建 future → 挂 `_observe_reply` → 追加 `_commands`/`_controls` → 计字节 → 清事件 → 唤醒 →
   `asyncio.shield`。建议抽出 `_enqueue(envelope_cls, command, size, ordinary=True)`。
5. **数据泥团** — `with registry._account_lock(uid), registry.registration_lock(uid), store.connect():`
   这类三元组在六个 Wiki 模块中反复出现（[jobs.py](../../server/wiki/jobs.py) 第 105、454、500、569 行等；
   共 34 处 `registration_lock`，9 处与 `store.connect()` 同行）。建议提供
   `registry.locked(uid)` 上下文管理器，避免加锁顺序漂移。
6. **重复代码** — 不透明 JSON 扫描器存在两份：
   [codex_activity.py](../../server/harness/codex_activity.py) 第 42 行按字节匹配，
   [codex_metrics.py](../../server/harness/codex_metrics.py) 第 16 行按字符串匹配并配 `_skip()`（第 26 行）。
   若字节/文本差异并非刻意，应合并为一个读取器。
7. **体量债务** — 新增超过 1,000 行的模块：`owner_runtime.py`（1,728）、`wiki/jobs.py`（1,643）、
   `routers/wiki.py`（1,097）、`wiki/history.py`（1,084）、`wiki/store.py`（1,038）。
   `WikiWorker._process`（jobs.py 1317–1586，268 行）已是 `server/` 内最大的函数，
   超过 [polish-2026-09](../plans/polish-2026-09.md) §3 A1 引作上限的 `bg_tasks._run_task`（217 行）；
   本轮触及的 server 文件中共有 44 个函数达到或超过 100 行。这正是 A1 要清除的债务。
8. **优先级与可读性** — [jobs.py](../../server/wiki/jobs.py) 第 1527 行
   `if completion.state == "recording_failed" or receipt.accepted and completion.recording_state != "committed":`
   仅因 `and` 优先于 `or` 才成立；1486–1494 的 settled/unknown 迁移在 1509–1515 又被重写一遍。
   建议加括号并合并为一个辅助函数。
9. **约定漂移** — 新增的 1,445 行 `def` 中有 768 行没有返回类型标注（Wiki 模块最集中），
   与 AGENTS.md《约定》“Python 3.12+、类型提示”不一致。但 `[tool.mypy]` 明确设置
   `disallow_untyped_defs = false`（[pyproject.toml](../../pyproject.toml) 第 98–99 行），
   因此这是判断项而非硬违规；至少应为新增的 Wiki/会话公共接口补标注，而不是悄悄扩大既有豁免。

## 3. 规格（Spec）

### (b) 未被要求的行为

3. **未提交改动改变了安装期认领，而不只是清理** — 计划第 5 行写
   `只完成票20的修复、同版验证、独立复核和文档收尾；这些工作已完成，本轮停止`。
   但 `_claimed_paths` 位于 `configure_service` 路径上（service_installation.py 第 516 行，
   定义在第 161 行），因此该改动会改变受管安装在 setup 时认领/标记哪些目录，
   属于在“已停止”的轮次内引入新的安装行为。

### (c) 看似实现但不完整

4. **“单写者”覆盖状态、问题与生命周期，但不覆盖执行配置** — 计划第 256 行写
   `SessionOwner 串行修改活动执行、问题及生命周期意图`；然而
   `server/routers/sessions.py:155` 使用 `configuration_lock`，第 227–229 行直接写库后
   `setattr(session, key, value)` 修改 model/effort/tier/credential；
   `server/routers/credentials.py:176` 直接 `live.credential_id = None`。
   轮次不可变性目前只靠 `capture_configuration` 在出队时快照（owner_runtime.py 第 334 行）维持。
   核实补充：同一把 `configuration_lock` 也被 Owner 在 `owner_runtime.py:1092` 使用，
   因此不是完全脱离 Owner 的裸写；问题在于串行化被拆成两套机制，而不是路由完全无保护。

### 核实成立的部分

- `_recording_blocked`/`_reconcile_recording`/`_submission_recorders`/`_pending_snapshot`/`final_task`
  已无残留；`session.status =` 只出现在 `owner_runtime.py:384`；
  reset 经由 `owner.call_runtime(Reset())`（`sessions/turns.py:633`）。
- token rotation 与计划 §2 一致（`token_rotation.py:307–326`）。
- Codex TPS 与计划 §2 一致（`monitor/query.py:168–171`、`harness/codex_metrics.py:144–155`）。

### 未核实

pytest/ruff/mypy/浏览器/live 运行；615 个文件中的约 600 个；`web/`；
Wiki §16 A01/A03 的恢复、观察者与租约路径；真实并发下 SessionOwner 竞争；
turn-safety 重试循环全文；对真实账户安装执行存储清理。

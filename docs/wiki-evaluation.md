# Wiki evaluation method

以下用于明确委托的质量或效率评测；日常、提交和发布门禁仍由
[Test Coverage](testing.md#test-coverage) 选择，不自动运行可选评测。
执行前使用[本机隔离、凭据与网络约束](local-wsl.md#本机测试)。
优先测量用户关心的单库约100篇规模，长文、大批次和多库只作增长诊断。
质量与原文保全合格后，统计完整任务端到端耗时和全部输入、输出 token，包含
错误、纠错、绕路与重试。调用数、轮数和缓存明细用于解释结果，不单独设下降门槛；
不要求每次严格新旧 A/B，也不从提示词字数推算实际 token 收益。

优化先查无效候选与提交、失败后绕路、同版重复读取、导出和验收，再按真实收益
调整 prompt、工具回包或读写流程；已有规则足够时修复工具与流程中的冗余。
保留权限、版本、事务、正文新鲜度及材料复用合同，减少调用不构成放宽依据。
质量样本绑定资料版本，开发题与保留题分开，不为已见题增加关键词或正则特例；
调参后重跑保留题不再算未见初测。明确要求自然问答质量时，走实际聊天入口、
自然问题和线上共享提示；额外引导的委托只证明相应条件下的链路。

保留真实失败、待定结论及实际成本；未知消耗不补零，缓存输入与推理输出不重复
相加。有效同版结果按验证政策复用，不为证明提速重复已验收的付费样本。
一次样本不证明普遍提速或 persona 的因果效果；用量和失败原始记录留在本次
任务产物，长期文档只保留可解释的结论与其条件。

## Knowledge and intent commands

维护同义措辞、询问、否定、范围及文档内指令辨识归 `knowledge_eval`，固定 low
effort，并保留工具调用、入队范围和提交者未改文件的断言。常规实机仍保留完整
维护提交 → Weber 执行 → 持久化结算，以及读取、原生助手、resume 指令更新和
大任务导出协议；授权、写入范围、事务与恢复留在离线门禁。

```bash
.venv/bin/pytest tests/test_wiki_intent_real.py --run-real --run-knowledge-evals -m knowledge_eval
.venv/bin/pytest tests/test_wiki_query_real.py --run-real --run-knowledge-evals \
  --knowledge-eval-output=/path/to/fresh-query-evidence -q
```

问答命令调用真实 Codex，使用隔离合成样本，不读取个人笔记。输出必须用新目录；
通过仅表示执行留证成功，语义验收见下文，不为补文档记录重跑。

普通聊天评测使用真实 WebSocket `send_message` 和正常共享提示；不以委托代替，
不追加逐题评阅提示或强制 Wiki 模式。registration JSON 包含 `source_root`、
`source_manifest_sha256`、所选 `credential_id` 及一至四项 `questions`
（各有 `id`、`question`）；gold 不随问题提交。摘要按
`tests.wiki_query_evidence.source_manifest` 做 Unicode JSON 编码，
`sort_keys=True, separators=(",", ":")`，再取 UTF-8 SHA-256。

```bash
.venv/bin/pytest tests/test_wiki_chat_real.py --run-real --run-knowledge-evals \
  --wiki-chat-registration=/path/to/registration.json \
  --knowledge-eval-output=/path/to/fresh-chat-evidence -q
```

该会话固定 `gpt-6.1-sol/high`、正常速度与既有 credential，不改 agent 默认、
回退登录或先发额外付费探针。报告保留隔离 DB、认证宿主、文件访问和源绑定的
实际限制；新 cwd/空 memory 不证明盲测，资料变化后须按当前版本评阅。

## Frozen Wiki reference

`tests/wiki_baseline.py` and `tests/fixtures/wiki-baseline-questions.json` own the
seeded 100-document corpus and sixteen questions, split into development and
holdout sets. Bind each question to source revisions and expected documents;
keep the gold material outside the model request. Use a stable vault identity
as well as a stable seed: regenerated UUIDs can change UID spelling, rank ties,
error samples and cursors even when the source text is identical.

`tests/fixtures/wiki-baseline-reference.json` retains the reproducible old-code
pages and the original timing limits. Its provenance separates observations,
the first limit capture and the explicit full-response count addition. It
contains synthetic data and relative source identities, without private runtime
paths. Absolute timing limits describe the recorded machine. On another
environment, establish its baseline before changing the implementation; keep
the old failures and avoid increasing limits in response to a failed candidate.

For a comparable environment, select this bounded measurement explicitly:

```bash
.venv/bin/pytest tests/test_wiki_phase1_baseline.py \
  'tests/test_wiki_efficiency_matrix.py::test_incremental_changes[100]' \
  --run-wiki-perf -m wiki_perf \
  --wiki-baseline-reference=tests/fixtures/wiki-baseline-reference.json \
  --wiki-perf-output=/path/to/fresh-candidate.json -q
```

Without a reference, the same entry produces a new baseline. Warm cases use
fourteen samples, cold builds six, with nearest-rank p50/p95. The prospective
noise rule is owned by `tests/wiki_cost_probe.py`; candidate comparison consumes
the stored limits by test/case/dimensions. Count SQLite cells delivered to
Python and actual UTF-8 response bytes. Instrumented query plans, file reads and
Python allocation peaks run separately from uninstrumented timing; they do not
measure physical SQLite IO or RSS. Frozen pages retain all output fields and
ordering except the declared observation-time normalization.

## Model evidence and retirement

Register the actual model, effort, tier, source manifest and selected login
before running paid cases. Ordinary-chat cases use the normal WebSocket entry
and shared runtime instructions; dedicated-query cases retain their additional
context as a separate condition. Check actual native developer messages and
tool-return guide digests when claiming a runtime guide became effective.

`tests/wiki_query_evidence.py` records actual returned ranges and raw usage.
Full-text evidence requires gap-free `[0,end)` coverage of the same revision;
preserve CRLF, CR, mixed newlines and codepoint coordinates when reading evidence
snapshots. Execution success leaves the raw semantic status `not_reviewed`.
Record manual answer/citation review separately, bound to the raw report hash.
Reconcile unique native response usage with CLI totals; cached input and
reasoning output are subsets, and missing totals, native tier or USD stay unknown.

On failure, the query probe cancels its original request and waits for its
resource retirement. Fixture drain additionally waits for that request's
completion and original owned tasks, credential refresh and held processes
before closing the callback API and database. A failed prerequisite keeps its
dependencies available; retain the original error and cleanup diagnostics
separately. These test-lifecycle seams do not establish a new public execution
retirement API. Small parameter/coordinate/cleanup regressions remain hermetic;
real answer questions use `knowledge_eval`, and real benchmark execution uses
`wiki_perf`, including benchmark-harness regressions.

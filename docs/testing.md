# Testing and verification

This is the authoritative verification policy. [AGENTS.md](../AGENTS.md#verification)
keeps the everyday summary; this file owns the complete requirements and commands.

## Verification

Use fast checks for repeated development iterations and full checks before a
code commit. **Both exclude `knowledge_eval` and `wiki_perf` by default.** These
optional tiers require a separate evaluation or measurement task and explicit
opt-in; they are outside commit and release gates.

**Offline correctness covers both Claude Code and Codex.** Include each backend's
affected argument/configuration, protocol, session lifecycle and simulated-process
cases when changing shared behavior. Backend-specific changes run the relevant
backend's regressions; the full hermetic gate retains both backends. These tests
use controlled fixtures, fake CLI subprocesses or local HTTP mocks, without a
real provider CLI, model request or login. Claude's exclusion from live testing
must not become a filename filter or skip in the offline gate.

**Routine live validation remains Codex only**, for both pytest and browser E2E.
Report offline coverage and live-provider coverage separately. Offline success
does not establish Claude's actual authentication, model output or provider-side
effort behavior; a separately authorized live investigation does not change this
default test selection.

### Existing tests when adding a transport

The transport matrix currently retains Claude stream-JSON, Codex exec and the
explicitly selected Codex App Server. Keep each existing test's business guarantee
and choose its fixture from what it tests:

| Test intent | Adaptation and evidence |
|---|---|
| Shared request, Owner, persistence, notification or retirement behavior | Retain the original public assertions. A synthetic profile can isolate capability/ownership combinations; it does not establish a real transport's wire behavior. App Server adds controlled-stdio cases through the original Run, Owner and SQLite. |
| Claude or exec wire behavior | Keep the original wire fixtures. Replace retired Harness setup such as `StdinMode` with its actual driver, and old input helper names with the public operation; keep acceptance, UUID, no-send and cleanup assertions. See [Harness core](../tests/test_harness_core.py), [Claude](../tests/test_harness_claude.py) and [Codex](../tests/test_harness_codex.py). |
| Native App Server RPC and races | Use [native submission](../tests/test_codex_app_server_submission.py), steering, questions, output and completion peers. They separately cover acceptance versus processing, original target/generation, late proof/items, blocked storage, budgets, complete-item integrity and actual process-group cleanup. Do not substitute an exec event parser. |
| Stored/wire shape | Update expected additive fields or the declared format only with the corresponding validity and old-record coverage. Keep byte/association/integrity checks. The shared [recording replay](../web/src/lib/recordingReplay.json) is checked against backend snapshots and frontend replay. |
| Reply consumers and UI | Preserve legacy rendering and formatting, then add root/child interleaving, commentary versus final, empty/bad references and reload checks. [Native result consumers](../tests/test_codex_app_server_results.py) exercise actual App and named-delegation entry points. |

For a timing-contract change, move a deterministic barrier to the newly owning
boundary and retain the original failure/recovery proposition. For example,
losing internal `session_started` delivery must not prevent precise resume from
committed input identity; the test must still allow the finite native output
reader to finish. Do not globally change old tests to App Server, remove their
assertions or turn incompatibilities into skips. A selected live test that skips
does not certify App Server acceptance. Default cutover has its own integration
scope; the explicit exec leaf and text-worker uses retain their tests.

### Fast checks during development

Get feedback on each change promptly with the smallest relevant test set:

- **Backend:** run affected test files or node IDs:
  `.venv/bin/pytest <affected-files-or-node-ids> -m "not real and not knowledge_eval and not wiki_perf" -q`.
  Start with the regression scenario, then adjacent cases. Run Ruff on changed
  Python files and check types regularly with `.venv/bin/mypy`.
- **Frontend:** `cd web && bun run test <affected-test-files>`; use
  `bun run typecheck` when TypeScript or shared interfaces change. This invokes
  `tsc -b --force`; the solution-level `tsc --noEmit` checks zero source files.
- **Integration changes:** add the specific offline API/browser regression
  needed to exercise the changed boundary. Run live tests when the question
  needs the installed CLI or provider, using this installation's selection.

Reuse unchanged preparation across read-only variations and keep the assertions
for each case. Content reads that supply authorization evidence remain real
reads. Use the named `short_process_shutdown` fixture only where the default
process-exit grace period is outside the tested contract; signals and joins stay real.

Before starting a check, inspect existing results and running checks. Give each
check one executor across agents; reuse successful evidence while its code,
configuration, dependencies and environment remain unchanged. Rerun only the
invalidated checks or a concrete new failure hypothesis, and state that reason.
Reuse installed dependencies, browser binaries and download caches; provision
them once per required version, while keeping mutable test state isolated.
Confirm that commands actually produced their expected test summary or build
artifacts; exit zero or a help screen alone is not passing evidence.

When a full run fails only in obsolete test-specific fixtures and product code
and shared fixtures are unchanged, update those fixtures and run the complete
affected modules. Combine that evidence with the unchanged passing results;
preserve the original failure rather than reporting that run as passed.

For a suite stopped by a time limit, resume uncompleted nodes only when the
collection and completion records establish the exact remaining set. Retain
the original log, remaining-node list and segmented results.

Update or remove tests with the behavior they cover. Preserve independent
current-behavior assertions and check shared fixture consumers before retiring
them; do not obtain green by weakening assertions or adding skips/xfails.
Include that adaptation in each ticket's acceptance, not a final cleanup batch.
When responsibilities move, move their rejection and recovery coverage with them.
Temporary acceptance tests explicitly designated for removal leave their
decision and validation evidence, not a permanent default-suite cost.

Offline browser specs import `test` from `web/e2e/test.ts`. Their HTTP requests
stay on loopback servers or explicit local mocks; remote fonts, images, scripts
and other incidental assets are blocked. Bundle application assets locally.
Provider/network integration uses the explicit live tier and its focused cases.

Keep this feedback loop focused while editing. The hermetic selection over all
of `tests/` is the full backend suite. Fast checks establish local correctness;
complete the full checks at the commit boundary.

### Full checks before code commits

At the code commit boundary, complete the check set in `./scripts/check.sh`:
the full hermetic backend suite, backend/frontend lint and types, frontend unit
tests, generated contracts and the docs index check. Use the script for a
combined run, or use the pre-commit hook for its gates and run the remaining
checks separately. Give each check one executor. The backend
selection includes Wiki correctness and excludes live integration and both
optional tiers; see Test Coverage for individual commands.

The full backend gate uses four pytest-xdist workers grouped by file, with worker
restart disabled. Focused development checks and live-provider checks stay serial;
parallelism is selected by the full-gate commands, not global pytest `addopts`.

**All required correctness checks must pass before committing.** Investigate
and fix failures. Reuse passing evidence for unchanged code, environment and
checks; rerun checks invalidated by subsequent changes. Release adds the
installation-specific checks below.

### Release and documentation checks

| When | Required validation |
|------|---------------------|
| Before release on this Codex installation | All commit gates, **offline E2E**, **live Codex pytest** and **Codex browser E2E** from Test Coverage below; then follow [自动发布](local-wsl.md#自动发布). Include supervisor checks when managed-service behavior changed. |
| Documentation only | Affected links, index and diff checks; follow [repository action policy](../AGENTS.md#repository-and-upstream-maintenance). |

Complete the release checks during validation and reuse valid results. When the
user says to release, proceed directly with [自动发布](local-wsl.md#自动发布);
routine release preparation does not open another review or approval round.

## Test Coverage

Use `.venv/bin/pytest tests/ --collect-only -q` with the relevant marker/opt-in
selection for current cases and counts. Commands below start at the repository
root; use the PATH setup in [local-wsl.md](local-wsl.md#本机测试).

| Suite | Command | Scope and execution boundary |
|-------|---------|------------------------------|
| Backend hermetic | `.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" -n 4 --dist=loadfile --max-worker-restart=0 -q` | Full correctness for **Claude Code and Codex**, including simulated CLI processes, Wiki sources/jobs/maintenance, permissions, transactions and recovery. No live model or external network dependency; guards reject real model CLI execution and production callback port 8000. |
| Wiki performance (opt-in) | `.venv/bin/pytest tests/ --run-wiki-perf -m wiki_perf -v -s` | Large-dataset projection and grant-lookup cost measurements. Outside default full checks and release gates; small correctness and query-boundary checks remain in the hermetic tier. |
| Live Codex pytest | `.venv/bin/pytest tests/ --ignore=tests/test_codex_login_real.py -m "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval" -v` | Actual Codex execution, streaming/tools, scheduling, memory, fork, research and Wiki through the selected existing login. New device authentication is excluded from routine release checks. Use the isolated credential setup in [本机测试](local-wsl.md#本机测试). |
| Supervisor integration | `.venv/bin/pytest tests/ -m "real and supervisor" -v` | Isolated systemd user units: installation ownership, environment/token handling, process retirement and managed restart. No provider CLI or production-service mutation. |
| Frontend unit | `cd web && bun run test` | Stores, HTTP/WS events, components, input lifecycle, account/service generation boundaries and Wiki job cards. Vitest tests are colocated with source. |
| Offline browser E2E | `cd web && bun run test:e2e:fast` | Real browser plus isolated backend: login/accounts, sessions, input recovery, sidebar/pins, attachments, Wiki cards and page readiness. Uses a test-only harness and verifies an offline execution audit; actual provider behavior needs the live tier. |
| Codex browser E2E | `cd web && bun run test:e2e:llm codex.spec.ts` | Real Codex session creation, response streaming and Monitor reporting through the product backend. Needs the installed, signed-in Codex CLI. |

The development-only App Server path has an additional explicit browser case:
`cd web && bun run test:e2e:llm codex-app-server.spec.ts`, with
`OCTOPUS_CODEX_TRANSPORT=app-server` and `OCTOPUS_CODEX_APP_SERVER_BINARY` selecting
the [G0-pinned binary](implemented/codex-backend.md#opt-in-app-server-start-and-precise-resume).
It checks original-receipt deduplication and precise native resume after reloading
and reopening the same session. Without that selection the case skips; a skip is
not transport acceptance. The default exec browser case remains independently
required. App Server live pytest covers both exec-origin and native-origin history
in `tests/test_codex_app_server_real.py`; these no-descendant cases do not establish
the later interaction, descendant or Monitor contracts.

Native question/control and output regressions use controlled stdio peers in
`tests/test_codex_app_server_questions.py` and `tests/test_codex_app_server_output.py`.
They exercise the original Owner, SQLite and publication path with explicit
storage, writer, reader and process-group barriers: dual RPC IDs, late answers,
Stop/conflicting controls, input-proof waits, retained late items, scoped work
rounds and failure before/after child terminal. Shared Harness/Owner regressions
retain both Claude and Codex offline coverage. The selected nested G0 frame
fixture is protocol replay, not a new live execution.

`tests/test_codex_app_server_completion.py` exercises original Owner admission,
root status, active/retirement budgets, stalled storage with real TERM/KILL,
Stop selection before recording, finite late items and start-wait failure.
Missing necessary root items and legal empty replies have separate cases.
Retirement-failure cases retain the actual process-group cleanup and inject lost
exit proof; they observe the existing fatal OS-exit boundary without terminating
pytest. Shared failure probes separately exercise the real service exit.
`tests/test_codex_app_server_results.py` drives actual App ask/SSE/history and
named-delegation consumers; session-result tests reject bad references without
fallback. These controlled peers establish product timing and persistence,
without claiming a new live-provider run or full R2 cutover. The shared replay
fixture also checks additive native fields across backend snapshots, WS replay
and frontend refresh. A lost internal session-start notification remains distinct
from cancellation of the finite output reader.

`cd web && bun e2e/run.ts offline native-output.spec.ts --workers=1` verifies
native root/child partials with the same item ID, snapshot-card anchoring,
per-item completion, reconnect and reload in the actual browser. Explicit local
HTTP/WS fixtures isolate presentation from native protocol and persistence;
backend process tests supply that separate evidence. Source-adjacent store and
transcript tests cover stale snapshots and service-generation changes. Retain
failed logs and combine unchanged full-run evidence with affected-module fixes
under the reuse policy above; passing browser mocks do not certify R2 cutover.

`tests/test_codex_app_server_completion.py` checks Stop before outcome selection,
after selection before commit, after commit before notification, and after
receipt delivery. `tests/test_codex_app_server_stop.py` adds the controlled
process/SQLite path through question waiting, nested helper observations,
silent process-group cleanup, independent BG returns, manual correction and
explicit queue resumption. Service reentry retains initial/steer unknown and
accepted-without-consumption evidence without another business send. Existing
recovery and original-resource tests retain unconfirmed-writer refusal.
`tests/test_native_child_details.py` exercises the authenticated saved-item API,
delayed child binding, precise pages/ownership, archived and damaged records,
and reads concurrent with Stop. Native detail component and browser tests
check late pages, same-service reconnect and saved body after reload, keeping
chat high water and model execution unchanged.

`tests/test_codex_app_server_effects.py` drives the real build, schedule and Wiki
worker entrances with original receipts, actual artifacts/publication receipts
and a real redirected process-group retirement barrier. It covers empty answers,
helper partials, root failure, Stop/revocation, pending questions and failed
notification without another execution. `tests/test_codex_app_server_errors.py`
keeps typed bound-root auth separate from child/tool/stderr and internal retry,
including error notification before ACK and generic RPC uncertainty. Repeated
retry/child error diagnostics cannot renew idle or cause a second send.

`tests/test_codex_app_server_fork.py` covers native actual ID, creation unknown,
saved-strategy cold cleanup, retirement before installation, copied result/detail
reads after source deletion, product-seq steer rewind and retained input uncertainty.
Post-ACK unexpected turn/item/request and unreadable-protocol cases retain the
actual artifact, row and working-directory effects through cold recovery;
the retirement test checks that ACK alone never opens automatic cleanup.
Real Git update/rename cases cover root/child and complete-only observations;
unrelated edits and connector JSON cannot acquire file-revert attribution.
The selected `test_live_old_and_native_history_fork_continue_independently` cases
in `tests/test_codex_app_server_real.py` verify real pinned native fork, recalled
context and independent file effects for exec-origin and native-origin sources.
Their existing-login/network isolation follows the live policy below; they do not
establish default cutover or stronger recursive completion.

### Markers, providers and isolation

- **`real` selects live-provider and supervisor integration.** `real_claude`
  and `real_codex` require a signed-in CLI; `claude_bin` and `codex_bin` require
  the binary. Availability probes run at setup in `tests/conftest.py`, so
  collection and hermetic checks do not invoke model CLIs.
- **This installation's live gate selects Codex only.** Claude's live CLI and
  browser tests remain outside routine checks and the release gate, regardless
  of whether its binary is installed. Keep both backends' offline coverage and
  supported agent settings; do not probe, install or authenticate Claude for
  routine checks. Report unexecuted live coverage explicitly.
- **Persistent state stays in temporary test roots**, including databases,
  account/Wiki/workspace paths and provider output. Live tests reference the
  existing selected credentials without copying secrets or changing defaults.
  Before installation, migration or supervisor write checks, resolve every
  resource and provider home outside the managed installation and account
  workspaces. Inspect all EnvironmentFiles actually loaded by the unit,
  including repeated properties; verify configuration before claiming or
  installing resources.
- **Browser E2E uses separate offline and live buckets.** The package scripts
  isolate servers; `@llm` selects real-provider cases and the offline bucket
  excludes them. Run Codex browser checks with one worker. The combined
  `bun run test:e2e` command needs both providers configured; use this
  installation's release selection instead.
- **Default Codex live checks use `low` effort**, enforced for harness runs and
  one-shots by `tests/conftest.py`; raw CLI calls set it explicitly. Assert
  protocol events, actual tool data and persisted effects. Do not grade generated
  Wiki wording or require the model to emit optional reasoning records; replay
  tests cover acceptance of those records.
- **`knowledge_eval` is opt-in**, deselected before setup/CLI probes and outside
  fast checks, full checks and release gates. It requires
  `--run-knowledge-evals`; `wiki_perf` independently requires `--run-wiki-perf`.
  Both are excluded from `scripts/check.sh` and the Python pre-commit hook.
  Setup and evidence rules are in
  [本机测试](local-wsl.md#本机测试). Assess answers against recorded source
  versions; execution success alone is not semantic acceptance. Natural-language
  maintenance intent variations also use this tier, retaining their tool and
  effect assertions. The default live tier keeps the full maintenance flow and
  Wiki protocol checks; authorization and write boundaries remain hermetic gates.
- **`wiki_perf` is independently opt-in**, including when answer evaluations are
  enabled. Keep correctness cases at the smallest useful size and reuse setup
  across read-only variations; mark dedicated scale/cost measurements instead
  of excluding Wiki behavior tests as a group.

## Wiki evaluation method

以下用于明确委托的质量或效率评测；日常、提交和发布门禁仍由
[Test Coverage](#test-coverage) 选择，不自动运行可选评测。
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

### Frozen Wiki reference

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

### Model evidence and retirement

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

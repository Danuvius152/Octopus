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

The transport matrix retains Claude stream-JSON, ordinary Codex App Server and
the independent Codex exec leaf/text worker. Keep each existing test's business guarantee
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

`tests/harness_profiles.py::process_profile` declares a synthetic process peer's
actual driver and capabilities. Replacing an argv builder alone cannot turn the
native App Server profile into an argv peer. Shared tests retain their original
business assertions, while native input evidence and RPC behavior require the
native controlled-stdio fixtures. Cold subprocess fixtures follow the same rule.

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
Tag the model-dependent case, rather than a mixed UI/model suite, with `@llm`.
[`web/tests/e2eSelection.test.ts`](../web/tests/e2eSelection.test.ts) collects the
actual Playwright specs in both modes without starting servers: UI-only cases
remain offline, while Chat, schedule and native-provider cases keep their live
selection. All E2E files are checked by `tsconfig.e2e.json` through the ordinary
`bun run typecheck` and build commands.

Keep this feedback loop focused while editing. The hermetic selection over all
of `tests/` is the full backend suite. Fast checks establish local correctness;
complete the full checks at the commit boundary.

### Full checks before code commits

At the code commit boundary, complete the check set in `./scripts/check.sh`:
the Python dependency-pins check, full hermetic backend suite,
backend/frontend lint and types, frontend unit
tests, generated contracts and the docs index check. Use the script for a
combined run, or use the pre-commit hook for its gates and run the remaining
checks separately. Give each check one executor. The backend
selection includes Wiki correctness and excludes live integration and both
optional tiers; see Test Coverage for individual commands.

The pins gate checks the declared runtime + `test` + `dev` dependency closure
against `constraints-python.txt`, including the recorded Python/platform.
Install verification tools with `.[test,dev]`; a runtime-only installation uses
`.` and need not contain Ruff, mypy, pytest or the verifier's `packaging` parser.
Run the verifier from the verification environment and pass the actual service
interpreter with `--python`. Rebuild and constraint-update commands belong to
[the local environment guide](local-wsl.md#环境重建).

Mypy enables `arg-type`, `union-attr` and `misc`: argument compatibility,
attribute access across union members, and the remaining miscellaneous checks.
There is no global suppression of those codes. The two local exceptions explain
the HTMLParser subclass's Final override and ZipExtFile's incomplete IO stub.
Optional database bindings retain their lifecycle-specific skip, domain error
or initialization invariant; cold recovery and shutdown are not all input-ready
operations. This gate retains the configured unchecked untyped bodies and
missing-import policy; success does not claim whole-repository strict typing.

The full backend gate uses four pytest-xdist workers grouped by file, with worker
restart disabled. Focused development checks and live-provider checks stay serial;
parallelism is selected by the full-gate commands, not global pytest `addopts`.

Bare `pytest` selects offline correctness. Tests marked `real` are deselected
before setup unless `--run-real` is present; a marker expression alone cannot
enable provider probes or supervisor integration. The optional `knowledge_eval`
and `wiki_perf` switches remain independent. A test carrying several opt-in
markers requires every corresponding switch.

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

### Conditional automation work

The upstream polish plan's remaining offline-E2E automation goal is retained:
choose its execution frequency, isolated environment and acceptable elapsed cost
before adding it to an automatic gate. `scripts/check.sh` currently runs browser
E2E only with `--e2e`; this candidate does not change that default or the release
requirements above. Adopt automation when a repeatable offline environment and
measured cost justify it, then verify that failures are visible and no live
model or production state is used. Source: the
[fixed upstream polish plan](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/polish-2026-09.md).

## Test Coverage

Use `.venv/bin/pytest tests/ --collect-only -q` with the relevant marker/opt-in
selection for current cases and counts. Commands below start at the repository
root; use the PATH setup in [local-wsl.md](local-wsl.md#本机测试).

| Suite | Command | Scope and execution boundary |
|-------|---------|------------------------------|
| Backend hermetic | `.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" -n 4 --dist=loadfile --max-worker-restart=0 -q` | Full correctness for **Claude Code and Codex**, including simulated CLI processes, Wiki sources/jobs/maintenance, permissions, transactions and recovery. No live model or external network dependency; guards reject real model CLI execution and production callback port 8000. |
| Wiki performance (opt-in) | `.venv/bin/pytest tests/ --run-wiki-perf -m wiki_perf -v -s` | Large-dataset projection and grant-lookup cost measurements. Outside default full checks and release gates; small correctness and query-boundary checks remain in the hermetic tier. |
| Live Codex pytest | `.venv/bin/pytest tests/ --run-real --ignore=tests/test_codex_login_real.py -m "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval" -v` | Actual Codex execution, streaming/tools, scheduling, memory, fork, research and Wiki through the selected existing login. New device authentication is excluded from routine release checks. Use the isolated credential setup in [本机测试](local-wsl.md#本机测试). |
| Supervisor integration | `.venv/bin/pytest tests/ --run-real -m "real and supervisor" -v` | Isolated systemd user units: installation ownership, environment/token handling, process retirement and managed restart. No provider CLI or production-service mutation. |
| Frontend unit | `cd web && bun run test` | Stores, HTTP/WS events, components, input lifecycle, account/service generation boundaries and Wiki job cards. Vitest tests are colocated with source. |
| Offline browser E2E | `cd web && bun run test:e2e:fast` | Real browser plus isolated backend: login/accounts, sessions, input recovery, sidebar/pins, attachments, Wiki cards and page readiness. Uses a test-only harness and verifies an offline execution audit; actual provider behavior needs the live tier. |
| Codex browser E2E | `cd web && bun run test:e2e:llm codex.spec.ts codex-app-server.spec.ts` | Real default Codex creation, complete streamed response, Monitor, original-receipt deduplication and precise native resume after reload/reopen. Needs the pinned, signed-in Codex CLI. |

### Topic regression entry points

Select the smallest check that can disprove the changed behavior. The current
contracts own their detailed counterexamples; the table below connects those
boundaries to verification without maintaining a second per-file inventory.

| Changed boundary | Contract and representative checks |
| --- | --- |
| Native start, input evidence, questions, completion and recovery | [Codex](implemented/codex-backend.md), [input reliability](implemented/input-reliability.md), [shared completion](implemented/architecture-contract.md); `test_codex_app_server_*` and `test_codex_native_recovery.py` |
| Original App/SSE/build/schedule/Wiki/delegation consumers | The same Codex contract plus [applications](implemented/applications.md), [Wiki](implemented/wiki-development-summary.md), [delegation](implemented/agent-collaboration.md); native results/effects tests retain the original receipt and real artifact/publication checks |
| Output, native items, image paths and child details | [Native children](implemented/native-subagents.md), [Monitor](implemented/codex-performance-monitor.md); output/items/images/details tests and `native-output.spec.ts` / `native-items.spec.ts` |
| History, creation uncertainty and file attribution | [Fork](implemented/session-fork.md), [rewind](implemented/session-rewind.md), [storage](implemented/current-storage-format.md), [backup](backup-and-migrate.md); fork/preservation tests and the live old/native-history cases |
| Read/WS/reconnect lifecycle | [Frontend reads](implemented/frontend-list-reconciliation.md); colocated store/transcript/component tests and offline browser fixtures |
| Wiki HTTP, maintenance and database teardown | [Wiki](implemented/wiki-development-summary.md); `test_wiki_test_runtime.py` and affected API/job tests |
| Wiki document resource policy | The [document contract](implemented/wiki-development-summary.md#文档字节支持与资源拒绝); `test_wiki_document_budget.py`, API/curation/history/recovery regressions cover bounded reads, refusal before effects, stale index/source projections and recovery |
| Background work and installation drain | [BG](implemented/background-tasks.md), [installation](implemented/installation-lifecycle.md); affected BG/service/handoff tests |

A controlled peer or browser fixture proves the boundary it actually exercises.
It does not establish a new live-provider run, deployment, or arbitrary recursive
completion. Shortened-clock tests do not establish an actual 300-second wait.
Keep failed initial runs and passing targeted follow-ups distinct when composing
evidence under the reuse policy above.

### Shared fixtures and protocol evidence

- `tests/native_app_server.py` keeps its function-scoped runtime, original Manager,
  SQLite database and cleanup together. `tests/session_runtime.py` owns shared
  dispatcher observations. Import support modules directly, not case modules.
- Short steer-ACK and accepted-input idle budgets begin after original-root
  preparation or acceptance commit. A test changing runtime idle budgets must
  wake the Owner; otherwise it probes startup or sleeping dispatch, not the
  intended deadline. Retirement tests retain real process-group cleanup even
  when injecting loss of exit proof.
- `tests/wiki_api.py` owns HTTP account/Manager/database fixtures;
  `tests/wiki_job_runtime.py` adds authenticated job helpers.
  `tests/wiki_jobs.py` is a separate store-only fixture and cannot substitute
  for the HTTP account. Teardown through `db.close()` drains turns, joins Owners
  and only then closes storage. Frontend WS lifecycle tests reuse
  [`Socket`](../web/src/test-support/socket.ts) with explicit callback ordering.
- `tests/byte_reads.py` observes binary `read()` on actual file handles, retaining
  fileno/context behavior and explicit before/after barriers. Use the actual I/O
  boundary when checking source-version consistency, replacement, byte cost or
  history locks; observing only `Path.read_bytes` misses bounded readers.
- Ordinary App Server has no transport opt-in. `OCTOPUS_CODEX_APP_SERVER_BINARY`
  must resolve to the [pinned binary](implemented/codex-backend.md#opt-in-app-server-start-and-precise-resume).
  Live exec-origin history is seeded only by an external test CLI, then resumed
  through the product path; there is no ordinary product exec fallback.
- The native question smoke enables `features.default_mode_request_user_input`
  only inside that test. Its question/answer/file-effect/SQLite/retirement
  evidence does not imply ordinary Default-mode tool availability. HTTP MCP
  questions remain a separate guarantee; recovered attempts retain the original
  question owner and every original attempt must retire.
- The [0.166 schema fixture](../tests/fixtures/codex-app-server-0.166-contracts.json)
  comes from the verified binary's `app-server generate-json-schema --experimental`.
  Schema/frame replay is protocol evidence, not a new model call or permission
  grant. The pinned local HTTP 503 probe is fault injection, not an upstream
  outage; capacity or media-generation observations prove only their observed
  stages. Saved artifact, row and file effects remain independently verifiable.

### Optional resource experiments

The [2026-11-03 resource and MCP experiments](reference/runtime-resource-boundaries-20261103.md)
are optional, fixed-version observations, outside default gates. Their recipes
use synthetic files and isolated native homes; configuration/discovery probes do
not send model input. Repeating measurements requires explicit resource stop
conditions. Small boundary/correctness regressions remain in the hermetic suite;
this does not add a permanent benchmark platform or resume paused Wiki evals.

### Math-rendering compatibility

[`MarkdownMath.test.tsx`](../web/src/components/MarkdownMath.test.tsx) exercises
inline/display math and inherited-trust rejection in both actual chat and file
preview pipelines, as well as renderer options with an inherited `trust` value.
KaTeX is fixed at 0.18.2 for
[GHSA-238p-pmpm-9mq7](https://github.com/KaTeX/KaTeX/security/advisories/GHSA-238p-pmpm-9mq7).
The package override also upgrades `rehype-katex` and `micromark-extension-math`'s
0.16.x requirements: upgrading only the direct CSS dependency leaves a vulnerable
renderer in those consumers. Keep the override until their declared ranges
support the patched renderer. The
[`math-rendering` browser smoke](../web/e2e/math-rendering.spec.ts) checks real
chat/file preview math, layout and locally loaded fonts; only model-based file
name resolution is mocked.

Shared JSON fixtures are imported as data, including the Wiki command cases used
by frontend and backend tests. Do not widen Vite's filesystem roots to make an
external `?raw` import pass: patched Vite checks those raw reads more strictly.
The `vite-filesystem` browser regression verifies that the development server
still rejects both the shared backend fixture and backend configuration outside
the web roots. Vitest and its V8 provider keep the same exact locked version when
applying security updates; the older coverage baseline below retains its original
measurement versions.

After a transitive security update, audit every locked copy. A patched hoisted
package can coexist with an older copy retained by a parent's exact pin. Prefer
a compatible parent update that adopts the fix, and preserve distinct dependency
branches rather than forcing them onto one major version. Validate contract
generation when updating its dependency chain, as well as frontend tests and the
production build. Report advisories that remain outside the callers' declared
ranges separately from fixes verified in the current tree.

### Coverage measurement (opt-in)

Coverage is diagnostic and has no percentage threshold. Install the backend
test extra and the locked frontend dev dependencies first. The Python command
below measures `server/`, including unexecuted modules, xdist workers and Python
service subprocesses; `pyproject.toml` enables branch measurement and coverage's
subprocess patch only when coverage is requested. Vitest and its V8 provider
are pinned together. Frontend coverage includes unexecuted `src/` TS/TSX files
and excludes tests, setup, test support/fixtures and generated API contracts.
Browser execution is outside this unit-test coverage denominator.

```bash
octopus_test_base="$HOME/.cache/octopus-tests"
mkdir -p "$octopus_test_base"
octopus_coverage_root=$(mktemp -d "$octopus_test_base/coverage.XXXXXX")
mkdir "$octopus_coverage_root/tmp"
export TMPDIR="$octopus_coverage_root/tmp"
export COVERAGE_FILE="$octopus_coverage_root/.coverage"
.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" \
  -n 4 --dist=loadfile --max-worker-restart=0 -q --cov=server --cov-branch \
  --cov-report=term --cov-report=json:"$octopus_coverage_root/backend.json" \
  --cov-report=html:"$octopus_coverage_root/backend"
(cd web && bun run test:coverage --coverage.reportsDirectory="$octopus_coverage_root/frontend")
```

The 2026-11-02 follow-up established the following baseline on Python 3.14.4,
pytest-cov 7.1.0 / coverage 7.17.0 and Vitest/V8 4.0.18. Backend observations
combine the full initial offline run and affected-module reruns with unchanged
production sources; first failures remain recorded. Frontend results use the
successful full unit run after the KaTeX upgrade, with test-only fixtures
excluded at measurement time. These are observations, not a release threshold
or a claim about live-provider coverage.

| Scope | Executable lines | Branches |
| --- | --- | --- |
| Backend offline | 34,287 / 37,527 — **91.37%** | 10,733 / 13,072 — **82.11%** |
| Frontend unit | 4,144 / 4,981 — **83.19%** | 4,333 / 5,668 — **76.44%** |

### Markers, providers and isolation

- **`real` selects live-provider and supervisor integration with `--run-real`.** `real_claude`
  and `real_codex` require a signed-in CLI; `claude_bin` and `codex_bin` require
  the binary. Availability probes run at setup in `tests/conftest.py`, so
  collection and hermetic checks do not invoke model CLIs.
- **`native_codex` checks the configured App Server executable.** The native
  live module retains `real_codex` for release selection, verifies the selected
  G0 binary, and probes the selected login with that executable in an isolated
  home. An invalid binary pin fails validation. PATH Codex availability applies
  separately to exec-origin history seeding. The active-root steer smoke checks
  original turn/generation identity, accepted input, a file effect and retirement.
  Report required live cases as passed, skipped, failed or unexecuted; collection
  and skipped cases do not establish acceptance. HTTP MCP question tests do not
  prove the native server-request/reply contract.
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
  `--run-knowledge-evals` and, for real execution, `--run-real`;
  `wiki_perf` independently requires `--run-wiki-perf`.
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

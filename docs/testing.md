# Testing and verification

This is the authoritative verification policy. [AGENTS.md](../AGENTS.md#verification)
keeps the everyday summary; this file owns the complete requirements and commands.

## Verification

Use affected checks during development and the full gate before a code commit.
`knowledge_eval` and `wiki_perf` are independent opt-in tiers outside commit
and release gates; commands and selection rules are below.

**Offline correctness covers both Claude Code and Codex.** Include each backend's
affected argument/configuration, protocol, session lifecycle and simulated-process
cases when changing shared behavior. Backend-specific changes run the relevant
backend's regressions; the full hermetic gate retains both backends. These tests
use controlled fixtures, fake CLI subprocesses or local HTTP mocks, without a
real provider CLI, model request or login. Claude's exclusion from live testing
must not become a filename filter or skip in the offline gate.

**Routine live validation remains Codex only**, for pytest and browser E2E,
regardless of Claude's installation or earlier investigations. Do not probe,
install or authenticate Claude for routine checks. Report offline and each
provider's live coverage separately; unexecuted checks are not passes, and
Claude live checks are not a pending gate for this installation.

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

Move timing barriers with their owning boundary, retaining the failure/recovery
assertions and allowing finite output readers to finish. Do not globally convert
old tests to App Server or replace incompatibilities with skips. The explicit
exec leaf and text worker retain coverage. Synthetic peers, including cold
subprocesses, declare their actual driver/capabilities through
`tests/harness_profiles.py::process_profile`; replacing argv alone cannot change
a native profile into an argv peer.

Manual compact regressions in `tests/test_codex_compact.py` use authenticated
HTTP/WS and the original Owner, SQLite and Harness with a controlled framed
stdio peer. They distinguish ACK/root/effect/terminal, including a proven root
that outlives the lost-ACK RPC deadline, pre-write proof, late Stop effects,
recording failure and non-replay after restart. These are product protocol
checks, not live-provider or production-release evidence. Composer and result
tests cover original-ID GET confirmation, account/session/draft protection and
attachments without duplicating the server state machine.

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

### Full checks before code commits

At the code commit boundary, complete the check set in `./scripts/check.sh`:
the Python dependency-pins check, full hermetic backend suite,
backend/frontend lint and types, frontend unit
tests, generated contracts and the docs index check. Use the script for a
combined run, or use the pre-commit hook for its gates and run the remaining
checks separately. See [Test Coverage](#test-coverage) for individual commands.

The pins gate checks the declared runtime + `test` + `dev` dependency closure
against `constraints-python.txt`, including the recorded Python/platform.
Install verification tools with `.[test,dev]`; a runtime-only installation uses
`.` and need not contain Ruff, mypy, pytest or the verifier's `packaging` parser.
Run the verifier from the verification environment and pass the actual service
interpreter with `--python`. Rebuild and constraint-update commands belong to
[the local environment guide](local-wsl.md#环境重建).

Mypy checks `arg-type`, `union-attr` and `misc` without global suppression;
local exceptions explain the HTMLParser Final override and ZipExtFile IO stub.
Preserve lifecycle-specific handling of optional database bindings. Configured
untyped-body and missing-import exclusions mean passing is not whole-repository
strict typing.

The full backend gate uses four pytest-xdist workers grouped by file, with worker
restart disabled. Focused development checks and live-provider checks stay serial;
parallelism is selected by the full-gate commands, not global pytest `addopts`.

**All required correctness checks must pass before committing**, applying the
evidence-reuse rules above. Release adds the installation-specific checks below.

### Release and documentation checks

| When | Required validation |
|------|---------------------|
| Before release on this Codex installation | All commit gates, **offline E2E**, **live Codex pytest** and **Codex browser E2E** from Test Coverage below; then follow [自动发布](local-wsl.md#自动发布). Include supervisor checks when managed-service behavior changed. |
| Documentation only | Affected links, index and diff checks; follow [repository action policy](../AGENTS.md#repository-and-upstream-maintenance). |

For documentation, check local targets and anchors (including inbound links
when headings move), then run `.venv/bin/python scripts/gen-docs-index.py --check`
for both generated indexes and `git diff --check`. Do not run product suites,
builds, model calls or deployment for documentation-only changes.

Complete the release checks during validation and reuse valid results. When the
user says to release, proceed directly with [自动发布](local-wsl.md#自动发布);
routine release preparation does not open another review or approval round.

### Conditional automation work

`scripts/check.sh` runs browser E2E only with `--e2e`. Before making it automatic,
choose frequency, isolation and acceptable measured cost; ensure visible failures
without live models or production state. This does not change release gates.
The remaining automation goal comes from the
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
| Codex browser E2E | `cd web && bun run test:e2e:llm codex.spec.ts codex-app-server.spec.ts` | Real default Codex creation, complete streamed response, Monitor, original-receipt deduplication and precise native resume after reload/reopen. Needs a compatible, signed-in Codex App Server CLI. |

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
| Socket identity, session deletion and drafts | [Account delivery](implemented/multi-tenancy.md#websocket-delivery), [list confirmation](implemented/frontend-list-reconciliation.md#会话列表与跨重连写入确认); `test_ws_delivery.py` uses mounted sockets/SQLite barriers, and `session-deletion.spec.ts` exercises actual REST deletion through two live sockets and pages |
| MCP call cancellation and original command/waiter retirement | [Question ownership](implemented/architecture-contract.md#mcp-question-retirement); `test_mcp_question_cancellation.py` uses real mounted FastMCP cancellation, delayed CreateQuestion admission/receipt, communication-failure cleanup followed by cancellation, waiter-release barriers and failed-cleanup shutdown; `test_ask_mcp.py` retains actual transport error/408 behavior |
| Exact cancelled input gap and completed-root presentation | `test_mcp_input_completion.py` joins the mounted SDK, original Owner/Run, persistence and real App/build/delegation/schedule/Wiki consumers; missing metadata, associated child effects, reader cutoff and held waiter release remain blocking. Native completion/results and UI message tests cover the same outcome and loaded answer references. |
| Page question drafts and active-interaction pause | `QuestionLifecycle.test.tsx`, `QuestionPrompt.test.tsx` and `test_question_resolution.py` retain original submission identity through remount, unknown receipts and later edits; `question-interaction.spec.ts` uses actual Tab/Shift+Tab, pointer and programmatic focus. `test_work_interaction_policy.py` owns deadline, pause and hard-cap adjudication. |
| App response timeout and served client | `test_app_response_recovery.py` exercises original JSON/SSE/status and authenticated module routes; `web/tests/appAgentClient.test.ts` consumes the same served source, including chunking, aborted/error reads, exact status identity and product base URLs. The native live module includes an actual App build using the runtime client brief. |
| Work interaction policy and missing decisions | [Original-work policy](implemented/architecture-contract.md#原工作交互策略与等待), [parent opportunity](implemented/agent-collaboration.md#unattended-questions); `test_work_interaction_policy.py` covers real admission/returns, native wire and controlled question clocks; unattended parent/domain tests retain actual receipts, parent Stop before/after solver admission, schedule consumption, Wiki effects and failure causes |
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
- `tests/_fixtures/jsonl_frames.py` splits snapshots on byte newlines before UTF-8
  decoding/JSON parsing. Readers retain active-writer suffixes; explicit writer
  completion rejects an unfinished suffix, while complete malformed frames fail
  immediately. Final EOF delivers new complete frames exactly once and advances
  the offset. `test_native_protocol_framing.py` uses real append/pipe/RPC barriers
  without sleeps. Its chunked-input regression failed on `07240d1b8fd1` and passed
  with the shared reader on 2026-11-04 (Linux/WSL, Python 3.14.4, pytest 9.1.1,
  pinned 0.166 fixtures and Python constraints, hermetic selection without
  transport overrides). This proves fixture framing, not product work identity.
- `tests/session_fork_support.py` owns the function-scoped fork source, SQLite
  and HTTP fixture; `tests/session_held_process.py` owns the original live
  process, readers and their teardown for rewind and failed-Owner tests.
  Scenario barriers and assertions stay in the consuming case files. Neither
  shared fixture imports case modules or creates resources at import time.
  Frontend read tests share only Promise preparation through
  `web/src/test-support/deferred.ts`; callers still arrange request/event order,
  reset stores and settle their own promises.
- Short steer-ACK and accepted-input idle budgets begin after original-root
  preparation or acceptance commit. A test changing runtime idle budgets must
  wake the Owner; otherwise it probes startup or sleeping dispatch, not the
  intended deadline. Retirement tests retain real process-group cleanup even
  when injecting loss of exit proof.
- `tests/wiki_api.py` owns HTTP account/Manager/database fixtures;
  `tests/wiki_job_runtime.py::active_worker` returns exact headers/task/scope,
  never the newest shared-table entry. Register retirement before waiting on
  creation, retaining the original task so late-created sessions are deleted.
  Close task/assignment, retire the session, then await cancellation and effects;
  finish the exit stack and callback host stop before propagating cancellation.
  The original `JobOwner` proves no model submission only for this controlled
  fixture, not real-worker retirement. Store-only `tests/wiki_jobs.py` cannot
  replace HTTP accounts. `db.close()` drains turns and joins Owners before
  closing storage. Frontend WS lifecycle tests reuse
  [`Socket`](../web/src/test-support/socket.ts) with explicit callback ordering.
- Connector diagnostic fixtures use long credential values that do not collide
  with ordinary prose. Assertions inspect the Connector's own logger, preserve
  exception/cause context, and check masking alongside unchanged storage and
  retired work. An unrelated raw asyncio exception copy cannot establish that
  the Connector diagnostic is readable. Product masking still covers short secrets.
- `tests/joint_recovery_process.py` guards `Popen.__init__`, retaining the real
  generic process class, communication, exit and reaping protocols. Its
  process-protocol phase evaluates the actual MCP process annotation even on
  interpreters that defer annotations, then exercises a controlled subprocess
  and the model-CLI rejection guard. Import order cannot replace this guarantee.
- UI lifecycle tests wait for the actual button and pagination state needed for
  their next interaction. The Wiki history regression holds the original
  by-id HTTP reread pending after history has rendered, exposing the interval
  before pagination is ready. Store updates or request counts alone do not
  establish DOM readiness; use observable waits without sleeps or wider timeouts.
- `tests/byte_reads.py` observes binary `read()` on actual file handles, retaining
  fileno/context behavior and explicit before/after barriers. Use the actual I/O
  boundary when checking source-version consistency, replacement, byte cost or
  history locks; observing only `Path.read_bytes` misses bounded readers.
- Ordinary App Server has no transport opt-in. `OCTOPUS_CODEX_APP_SERVER_BINARY`
  selects the [native executable](implemented/codex-backend.md#opt-in-app-server-start-and-precise-resume);
  no binary digest or exact release is required. Live tests exercise the required protocol.
  Live exec-origin history is seeded only by an external test CLI, then resumed
  through the product path; there is no ordinary product exec fallback.
- The native question smoke enables `features.default_mode_request_user_input`
  only inside that test. Its question/answer/file-effect/SQLite/retirement
  evidence does not imply ordinary Default-mode tool availability. HTTP MCP
  questions remain a separate guarantee; recovered attempts retain the original
  question owner and every original attempt must retire.
  G0 emitted `isBlocking:false` in the live Default-mode run; assert optional
  delivery explicitly rather than treating the tool name as blocking evidence.
  Human blocking/idle suspension uses explicit true frames plus a controlled
  question clock in `test_work_interaction_policy.py`; it is not a live-provider
  blocking observation. The unattended live case retains the actual partial
  file, missing-decision closure and absence of dependent effects.
- The [0.166 schema fixture](../tests/fixtures/codex-app-server-0.166-contracts.json)
  comes from the verified binary's `app-server generate-json-schema --experimental`.
  Schema/frame replay is protocol evidence, not a new model call or permission
  grant. The pinned local HTTP 503 probe is fault injection, not an upstream
  outage; capacity or media-generation observations prove only their observed
  stages. Saved artifact, row and file effects remain independently verifiable.

### Test fixture remediation evidence

On 2026-11-04, T-01–T-04 used baseline `4f1418da3906fedd7221d9b239774681e785945c`
and regression patch SHA256 `bd8053033019f38043d005c7b921b08f8149e149d7c1d09701a7944ecfe5b9b5`.
Strengthened assertions exposed diagnostic-token collisions, the generic `Popen`
replacement, assignment/job leaks, late creation, interrupted teardown and DOM
readiness. Repairs passed all nine `check.sh` gates and affected helper consumers;
initial failures were not skipped or weakened. Environment: Linux/WSL, Python
3.14.4, pytest 9.1.1, MCP 1.30.0, AnyIO 4.15.1, Node 24.21.0, Bun 1.4.2 and
Vitest 4.1.12 under the recorded constraints/lockfile. The joint-recovery module
also passed on isolated Python 3.12.16 with the same Python constraints. These
offline, isolated results do not establish live/browser acceptance or deployment.

### Session delivery and interaction retirement verification

Tickets 10–14 used baseline `07b83bf2fed2e1d88ca1e782eafe384e86988355` on
2026-11-04; Vera cleared Standards and Spec for patch SHA256
`d14251663ca7c3736b468f2313094215ac574b7d9ac6efa83266322c6962114c`.
Mounted SDK/waiter barriers and original parent admission/Stop cover cleanup
surviving later cancellation and closure of the parent-solver opportunity.
Initial mypy/backend/frontend failures were repaired and their complete modules
rerun; final Owner/MCP checks, lint/types and indexes composed with unchanged
passing full-gate evidence. Offline E2E, selected live Codex cases and both Codex
browser cases passed, including a repeated real 65-second HTTP question wait.
The native question's first failure wrongly assumed blocking despite
`isBlocking:false`; the corrected assertion did not alter product frames.
Blocking/idle suspension remains controlled-frame evidence. Network audits used
the same login with no authentication-domain attempts, login or refresh.
This does not establish deployment or close tickets 15–20.

### Question draft, input cleanup and App recovery verification

Vera cleared Standards and Spec from baseline `0f1570877be4e2ed5903377fe93955c454f43906`
for code/document patch SHA256 `af4c3666811a7de835a8cb08cc16338e5214bdaa9beb2113923d65f07f02435c`.
Mounted MCP/consumer tests, page identity checks and real browser focus gestures
cover the boundaries listed above. A missing client-module structural audit
entry failed the full gate; repaired route/App modules and final SDK tests,
lint and types passed alongside unchanged full-gate evidence. Offline E2E's
paused-dispatch reconnect failure remains unexplained: its unchanged module
subsequently passed, including five repetitions, without sleeps, skips or relaxed
assertions. Selected live Codex cases (including an actual App build) and both
Codex browser cases passed using the conversation's existing login, with no
authentication-domain attempts or refresh. An earlier background run used the
service's different login, skipped live cases and failed browser authentication;
that is failed setup evidence, not a product pass. Deployment still requires
separate publication receipts.

### App, login and delegation ownership regressions

CR-01–CR-03 use public entry points, controlled peers and real SQLite/files:

- App/Wiki request-lifetime tests cover target loss, capacity, effects, 202 handoff
  and failed cleanup. Proxy lock/body barriers require original-request retirement
  before replacement; poll-close failures survive later `done`, and weak references
  detect accumulated successful responses.
- `test_codex_login.py` places barriers at publication COMMIT, separately from
  key/read commits. It covers observer cancellation, target overlap, disable/delete,
  labels, whole-group exit, compensation/reentry and drain blockers. Actual
  `os.stat` failure plus `os.lstat` checks cover suppressed directory errors;
  group-exit/watchdog barriers preserve publishing. The isolated publication
  probe distinguishes known rollback from unknown acknowledgment. `HarnessPage`
  checks historical success against current lists and retired view/account guards.
- `test_delegation_restart_deletion.py` retains accepted/unknown input responsibility;
  only confirmed resource-free, unadmitted setup retires after relationship deletion.
  Command/return tests retain typed-refusal rollback and the previous question writer.

These prove process-local offline ownership, not provider authentication, crash
recovery or deployment. A 202, terminal UI label or cancelled waiter cannot replace
an effect/retirement receipt.

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
  live module retains `real_codex` for release selection and probes the selected
  login with that executable in an isolated home. Protocol incompatibility fails
  in the product path instead of an exact-version gate. PATH Codex availability applies
  separately to exec-origin history seeding. The active-root steer smoke checks
  original turn/generation identity, accepted input, a file effect and retirement.
  Report required live cases as passed, skipped, failed or unexecuted; collection
  and skipped cases do not establish acceptance. HTTP MCP question tests do not
  prove the native server-request/reply contract.
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
- **Opt-in switches are cumulative.** Bare `pytest` selects offline correctness;
  marker expressions alone do not enable probes. `real`, `knowledge_eval` and
  `wiki_perf` require `--run-real`, `--run-knowledge-evals` and `--run-wiki-perf`
  respectively, with every applicable switch required before setup/CLI probes.
  The two optional tiers are excluded from `scripts/check.sh` and the Python
  pre-commit hook. Keep small Wiki correctness checks hermetic; mark dedicated
  scale/cost measurements rather than excluding Wiki behavior as a group.

## Wiki evaluation method

以下用于明确委托的质量或效率评测；日常、提交和发布门禁仍由
[Test Coverage](#test-coverage) 选择，不自动运行可选评测。
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

### Knowledge and intent commands

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

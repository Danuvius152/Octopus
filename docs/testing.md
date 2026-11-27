# Testing and verification

This is the authoritative verification policy. [AGENTS.md](../AGENTS.md#verification)
keeps the everyday summary; this file owns the complete requirements and commands.

## Verification

Run affected checks during development. Run the full gate at the delivery
batch's code-commit boundary.
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

## Existing tests when adding a transport

Fixtures preserve the business assertion and the boundary that owns it. A failure
injection must reach that boundary; refusal during unrelated setup is not evidence
for rollback or retirement. Isolated installation tests bind the real producer
identity and restore only their own copies.

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

Manual compact regressions in `tests/test_codex_compact.py`,
`tests/test_claude_compact.py` and `tests/test_queued_compact.py` use public
session/admission/control APIs, authenticated HTTP/WS where applicable, and the
original Owner, SQLite and Harness with controlled stdio peers. They distinguish
ACK/root/effect/terminal, including a proven Codex root
that outlives the lost-ACK RPC deadline, pre-write proof, late Stop effects,
recording failure and non-replay after restart. These are product protocol
checks, not live-provider or production-release evidence. Composer and result
tests cover original-ID GET confirmation, account/session/draft protection and
attachments without duplicating the server state machine.
Claude fixtures consume the saved 2.1.319 frame shapes; documented successful
no-op is explicitly conditional synthetic evidence, not an observed outcome of
that binary. `tests/claude_runtime.py` retains the real ClaudeDriver, unlike
generic `process_profile` JSONL peers which borrow parsing without declaring
native history collection. Terminal/EOF-before-Owner-consumption barriers verify
Stop saves the finite boundary, invalidation and effect under the original input.
The shared queue cases cover previous steer, native rejection, human questions,
actual process retirement, independent BG returns, claim/cancel and paused quota.
`queued-compaction.spec.ts` checks both backends across tabs/refresh using a
mocked authoritative server; it proves browser reconciliation, not provider I/O.

## Fast checks during development

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

Verify each ticket with the checks that exercise its acceptance criteria and
affected behavior. After related tickets are integrated, run the focused
regression for their shared behavior.

### Fixed candidates and parallel acceptance

Before starting final review and focused checks, pin a recoverable candidate
in a dedicated execution checkout. It contains the actual source, including
new files, and any additional check inputs that Git does not capture. Record
the revision or snapshot, the checkout and command working directories, and
the identity and location of the extra inputs. Keep those inputs fixed
throughout execution. Before/after input hashes supplement the candidate
record and help detect drift.

Give Vera and the focused checks the same candidate. Run the applicable
regressions, type checks and generated-contract checks while Vera reviews.
Record each check's command, scope, result and evidence location. Both tracks
must pass on the integrated candidate before the [full commit gate](#full-checks-before-code-commits)
and applicable [release checks](#release-and-documentation-checks) start.

### Selecting checks and reusing evidence

Reuse unchanged preparation across read-only variations and keep the assertions
for each case. Content reads that supply authorization evidence remain real
reads. Use the named `short_process_shutdown` fixture only where the default
process-exit grace period is outside the tested contract; signals and joins stay real.

Before starting a check, inspect existing results and running checks. Use one
executor per check. Reuse a passing check from a completed run after
verifying that its summaries or build artifacts are readable and its command,
selection, relevant code, configuration, dependencies and environment still
apply. Link the original evidence in the task's acceptance record.

Before scheduling a rerun, record the evidence that carries forward, the changed
inputs or concrete failure hypothesis, and the checks needed to close the
remaining coverage gap. Start by reproducing failures at their nodes, then cover
the affected modules and consumers of any changed shared path. Expand to a full
suite only when targeted checks cannot close a required coverage gap. Record that
gap before launching the suite.

Keep the first failed run and write every rerun to a new evidence directory.
When a failure appears under parallel execution, record the first run's
selection, actual worker count, distribution, failing node and worker. Use
`./scripts/check.sh --release --pytest-target <node> --output <new-directory>`
for an isolated check with execution evidence. When testing a concurrency or
load hypothesis, match the original candidate, settings and worker count and
include enough neighboring tests to reproduce contention. Use
`--pytest-workers <count>` with `--release` and multiple `--pytest-target`
files to retain the original worker count and file-based scheduling in a
focused diagnostic run. Use
`./scripts/check.sh --release --gate pytest --output <new-directory>` if the
full selection is needed. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` to the original
count when automatic selection would differ. Eight workers with a single
selected node still give that node no neighboring work. Apply the same
distinction to browser projects and repeats. A passing isolated run records
non-reproduction in isolation. The original failure and its cause remain
separate from the repeat result.

Use `./scripts/check.sh --output <new-directory>` for the full commit check set.
Add `--e2e` to include Playwright. For scoped reruns, repeat `--gate <id>` to
run only the named checks. `--gate pytest` runs the full hermetic backend suite.
Use `--pytest-target <file-or-node-id>` for serial hermetic pytest on explicit
Python test files or node IDs under `tests/`. Repeat the option for multiple
targets and add `--gate` options for any other checks needed. The runner records
this pytest scope under `pytest-focused` with its own JUnit report.
Use `--pytest-input <file>` for each ignored helper or data file the selected
tests depend on. The path must name a file within the checkout. The runner
includes these files and the targets' ancestor `conftest.py` paths in the
fingerprint and lists those paths under `source_files` for the focused gate.
Each snapshot follows links for these paths inside the checkout and records the
resolved path and contents. A link to a path outside the checkout invalidates
the evidence.

Use a new output directory outside the worktree, preferably in the task's
account-workspace directory, or use a Git-ignored path inside the worktree.
Omitting `--output` keeps terminal-only execution. The
[runner](../scripts/check_runner.py) owns the command table and records actual
argv, cwd, selection scope, exit code, duration and a combined stdout/stderr log
per gate. Ordinary failures do not suppress later selected checks.

`results.json` starts as `incomplete`, updates atomically after each gate and
becomes `complete` only after execution and final input capture. A complete,
fixed-input run with `success=true` and a zero runner exit establishes that
every listed gate passed. For delivery, account for each required gate ID and
scope using the recorded evidence. In a complete run with
`inputs_changed=false` and no run-level evidence errors, reuse a gate with
`status=passed`, `returncode=0` and `evidence_error=null` whose inputs still
apply and whose artifacts are intact. Use the follow-up rules below for a
failed test gate and preserve its original outcome.

Each sequential gate requires Linux subreaper support, default
`SIGCHLD` handling and no preexisting children of the runner. Ignoring/reaping
child signals externally can lose the true exit status and is rejected before
spawn. On success, failure or cancellation, the
runner terminates and reaps that gate's remaining descendants, including orphans
and inner sessions, before completing it. Cancellation remains pending through
cleanup, leaves `interrupted` and suppresses later gates. The original subreaper
setting is restored after confirmed retirement; unconfirmed retirement fails
the run. An abruptly killed run remains incomplete. The runner adds no process
group: an outer BG owner retains its original group, but that alone cannot reap
inner groups after the runner is killed. Log short writes, flush/fsync failures
and result-write failures fail the runner. No previous evidence is overwritten
or automatically reused.

### Retiring a candidate's checks

For focused checks that may need retirement, start the actual entry point
under the coordinator from the recorded execution directory:

```sh
# In the execution checkout. Use an unused absolute path for the control directory.
.venv/bin/python -m scripts.targeted_check run "$control" -- ./scripts/check.sh --gate mypy
```

From another terminal in the same checkout, use the same control path. Once
the directory exists, request a stop and check for a retirement record:

```sh
.venv/bin/python -m scripts.targeted_check stop "$control"
.venv/bin/python -m scripts.targeted_check status "$control"
```

Make the checkout importable when the command's cwd is outside the repository
root, for example with `PYTHONPATH=/path/to/checkout` for a direct Bun check
from `web/`. Use a new control directory per execution and separate report
directories. The coordinator holds the original runner from startup through
exit, including shell toolchain setup, direct Bun entry points and any final
input capture performed by the command. A stop request prevents further gate
or browser-partition dispatch.

Once the original runner and descendants have retired, the coordinator
saves the original child PID and exit code before releasing the reservation.
It uses the process handle even if the signal interrupted startup before the
launch record was written. If no child started, the record has no PID or child
exit code. The outer command returns 130 for `SIGINT` and 143 for `SIGTERM`
or soft stop after retirement is confirmed. A cleanup failure after recording
retirement preserves that exit code and the readable record. The original
control directory still owns the reservation until `cleanup` succeeds.
Unconfirmed retirement leaves the reservation in place without publishing
a retirement record. A caught signal with unconfirmed retirement exits 1.

If a focused check fails before review completes, preserve the fixed candidate
and its failure evidence. Wait for Vera's verdict on that candidate and assess
the results together before changing it. When a verified review finding
requires changes, request a soft stop on the remaining checks. Repeat
`status` until the coordinator records proof that the original runner and
its descendants retired. An interrupted report or the outer background task's
terminal status alone cannot prove retirement. If proof is missing, preserve
the isolated checkout and its resource references until it can be established.
If Playwright resource cleanup remains pending after confirmed retirement,
retry `.venv/bin/python -m scripts.targeted_check cleanup "$control"`.
After retirement, bring the persisted failures and review findings together.
Pin the replacement, rerun known failing cases and affected paths first, and
send it back through the existing Vera delegation.

### Test evidence and remaining coverage

In evidence mode, pytest emits JUnit and Vitest emits its native JSON in the
same execution as the terminal reports. Their counts come from those files.
Release pytest records the controller's workers started, failed node IDs and
the worker ID for test and collection failures reported by a worker. A worker
crash fails the gate even if pytest exits zero, and records the worker ID
whether or not a test was assigned. Empty execution also fails the release
gate. Missing or invalid test reports leave counts unknown and fail evidence
generation. Gate exit codes and failed reports remain available in the evidence.

Focused pytest under `check.sh --pytest-target` with `--output` records failure
details as the controller receives them. For direct Vitest evidence, run this
command from `web/` with an unused absolute evidence directory and a separate
JSON report path:

```sh
OCTOPUS_VITEST_EVIDENCE_DIR="$events" bun run test --reporter=default --reporter=json --outputFile="$report"
```

For direct Playwright, set `OCTOPUS_E2E_EVIDENCE_DIR` to a new directory before
running `bun run test:e2e:fast`. Keep the immediate diagnostics with the native
logs, selection and reports.
An error record can identify a case to rerun while leaving its final outcome
and the completion of neighboring cases unknown.

Input records include HEAD, tracked and non-ignored untracked source contents,
the focused pytest inputs above, file modes and symlink targets, configuration
hashes, Python's actual installed packages, the installed frontend dependency
tree and tool versions. Treat focused runs with undeclared ignored dependencies
as observations of that run. Only named non-secret environment selectors are
included. Changed before/after inputs make the run unusable as passing evidence.
Keep inputs fixed throughout execution. Matching endpoint hashes cannot detect
an intermediate edit that was later reverted, and package versions establish
the inventory, with relevance and reuse determined by the policy above.
The full hermetic, supervisor and live Codex pytest gates refuse to start when
`PYTEST_ADDOPTS`, `PYTEST_PLUGINS` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD` is set
to a nonempty value. The same applies to direct invocations of the live pytest
runner. Focused release diagnostics refuse the two plugin overrides.
Focused pytest runs with inherited `PYTEST_ADDOPTS` retain the actual execution
and exit code, and the runner marks their requested scope uncertified.

Reuse installed tools, browser binaries and caches. Provision a missing required
version once; isolate mutable state rather than reinstalling dependencies.
During final validation keep the source fixed, especially while Vite watches it:
an edit can reload the browser and discard its in-memory selection.

When a full run fails only in obsolete test-specific fixtures, fix the fixtures
and identify every consuming module. If product code and wider-reaching test
inputs are unchanged, run those modules in full. Check the original failures
are covered, combine the still-valid passes and follow-up by passing test
identity in their native reports, and link the original failed report in the
acceptance record.

After an interrupted suite, run the uncompleted nodes when the collection and
completion records establish the exact remaining set and the completed portion
still applies to the fixed replacement candidate. Account for required
setup, teardown, projects and repeats in that determination. Cases still
running at retirement belong in the remaining set. Retain the original log,
remaining-node list and segmented results. A collection list and early failures
alone leave completion unknown when the final report is missing. After rerunning
known failures and affected paths, rerun any required focused selection whose
completion is still unproven in full to close the focused gate. Once focused
checks and Vera pass, rerun any wider required suite with unproven coverage in
full at its existing commit or release gate.

Update or remove tests with the behavior they cover. Preserve independent
current-behavior assertions and check shared fixture consumers before retiring
them; do not obtain green by weakening assertions or adding skips/xfails.
Include that adaptation in each ticket's acceptance, not a final cleanup batch.
When responsibilities move, move their rejection and recovery coverage with them.
Temporary acceptance tests explicitly designated for removal leave their
decision and validation evidence, not a permanent default-suite cost.

### Browser ownership and execution

Use the [real-account fixture](../web/e2e/test.ts) for offline integration specs
and the [mock chat fixture](../web/e2e/mock-chat.ts) for isolated transcript
rendering with explicit API and WebSocket responses. Requests stay on loopback
or explicit mocks. Bundle assets locally and mark model-dependent cases
`@llm`. The [selection check](../web/tests/e2eSelection.test.ts) collects the
actual specs without servers. Ordinary typecheck covers `tsconfig.e2e.json`.

Each real-account attempt, repeat or rebuilt worker receives a real isolated
account. Read its bearer at call time through `current-account.ts`. Tests of
signed-in behavior may start the app with that test's registration bearer.
Tests of login, logout, expiry or revocation exercise the relevant interaction. Creation
receipts and returned IDs grant cleanup authority, including account-local
successors with lost responses; names do not. Teardown checks inventories and
mutations, retaining both body and cleanup errors. Archived sessions stay in
the isolated runtime: their public DELETE is unavailable. Test accounts need
no product deletion feature, and cache residue is acceptable.

Mock chat provisions only its own page and context. Unmatched API requests
and WebSocket connections or messages are blocked and recorded on the test.
Vite hot reload uses a distinct loopback socket. Independent chat-follow-output
cases opt into file-level parallel scheduling within the ordinary two-worker
budget. Keep the original browser frame observation for drift and first-paint
assertions. The [Markdown rendering tests](../web/src/components/MessageBubble.rendering.test.tsx)
cover the formula source-to-glyph matrix. Browser representatives cover live-to-saved
selection position, font loading, ranges spanning several glyph nodes, and identical
glyphs in distinct fraction positions.
The four warm presentation cases each test one round trip in the
default suite. Use `bun run test:e2e:chat-performance` in `web/` for ten round
trips per case, compositor JPEGs and latency percentiles.

The [runner](../web/e2e/run.ts) uses native Playwright collection to preserve
project/grep selection and unmatched-selection errors, then executes only the
selected [scopes](../web/playwright.config.ts), one exact project per child:

| Phase | Preparation and ownership |
| --- | --- |
| Ordinary `chromium` | Parallel workers; one backend/Vite pair for all repeats. |
| Main `installation` | One worker and one reused service pair for global connector configuration, memory guards and the Wiki worker. Connector teardown checks restoration of the initially empty configuration; pin order remains account-local. |
| Empty accounts installation | Only selected first-account stories; a fresh accounts pair per batch repetition, without ordinary services. |

`test:e2e:ui` retains native UI/host/port options, opening each selected phase
once and exposing only its project/scope. Internal collection stays headless;
closing one phase releases its services before the next. Native UI handles
reruns; use Cleanup/Setup to reset an empty accounts story. Every Setup cycle
needs a complete offline guard/readiness audit, including swallowed refusals.
Closing before Setup reports no audited execution. The
[UI protocol checks](../web/tests/e2eUI.test.ts) stop at the GUI execution boundary.

Mutable state and provider output use `~/.cache/octopus-tests`; normal teardown
stops services and removes its own runtime. Account cleanup errors still fail;
residual cache directories do not. Local environment setup belongs to
[the WSL guide](local-wsl.md#离线目录与浏览器).

## Full checks before code commits

Run the check set defined in [`scripts/check.sh`](../scripts/check.sh) once on
the final candidate before the agreed delivery batch's code commit. A standalone
code change is its own batch. Choose one executor, either the combined script
or the hook plus checks it does not cover. If the candidate changes, reuse
valid results under the policy above and rerun affected required checks.

When applicable hook commands already have valid passing evidence, exclude
only those commands for that commit with
`LEFTHOOK_EXCLUDE=<comma-separated-command-names> git commit ...`.
Take names from [`lefthook.yml`](../lefthook.yml) and record their evidence in
the existing acceptance record. Leave uncovered commands enabled and complete
all required gates; excluding duplicate execution does not waive a gate.

Install Git hooks with `./scripts/setup-hooks.sh`; `--check` verifies installation
without changing it. Hooks source `scripts/toolchain-env.sh` so noninteractive Git
can resolve the local JS toolchain. Missing or stale hooks fail the full gate.
Python edits under `scripts/` also trigger backend lint, tests and import checks.
Hook patterns include direct children and nested files. Frontend checks also
cover E2E/tooling tests, imported JSON fixtures, root configuration and dependency
inputs, plus the explicit cross-directory inputs consumed by those tests. The
[selection regression](../tests/test_check_hooks.py) exercises the installed
Lefthook scheduler; unrelated guide edits do not run product checks.

The pins gate checks runtime + `test` + `dev` against `constraints-python.txt`,
including Python/platform. Runtime-only installations need not contain test
tools: run the verifier from the verification environment with `--python`
pointing to the actual service interpreter. Rebuilding dependencies belongs to
[environment recovery](local-wsl.md#环境重建), not routine validation.

The pre-commit pytest hook runs `./scripts/check.sh --gate pytest`. The shared
gate reads its pytest configuration from `pyproject.toml`, groups tests by file
and disables worker restart. It uses pytest-xdist's CPU-based worker count with
a cap of eight and records `PYTEST_XDIST_AUTO_NUM_WORKERS` in the evidence when
set. Focused development checks and live-provider checks use serial pytest. To save
structured evidence from the hook, set `OCTOPUS_PYTEST_OUTPUT` to a new evidence
directory. When pytest runs, the summary displays its nonzero exit code and the
evidence directory retains the native report and execution log when available.

**All required correctness checks must pass before committing**, applying the
evidence-reuse rules above. Release adds the installation-specific checks below.

## Release and documentation checks

| When | Required validation |
|------|---------------------|
| Before release on this Codex installation | All commit gates, **offline E2E**, **Codex browser E2E** and any required **live Codex pytest** capabilities from Test Coverage below. Then follow [自动发布](local-wsl.md#自动发布). Include supervisor checks when managed-service behavior changed. |
| Documentation only | Affected links, index and diff checks; follow [repository action policy](../AGENTS.md#repository-and-upstream-maintenance). |

With the [selected existing Codex login](local-wsl.md#live-凭据与网络) configured,
run `./scripts/check.sh --release --output <new-directory>`. Add
`--codex-live-target <test-file-or-node>` for each affected Codex capability
whose contract needs a provider, such as native questions, app generation,
research, image output, history migration or long MCP waits. Use
`--codex-live-full` for Codex CLI or selected model changes, shared runtime
protocol changes and uncertain impact, and when checking complete provider
compatibility. Record the required live scope in the release acceptance record.
Each explicit target must contribute a case after native pytest selection. An
unmatched target stops the suite before any of the selected cases execute.

Add `--supervisor` for managed-service changes. It selects
`real and supervisor` and requires at least one passed case. To fill gaps
using existing evidence, select release `--gate` IDs such as `offline-e2e`,
`codex-live-pytest` or `codex-live-browser`. Selecting the live pytest gate
explicitly with no targets requests the full live pytest capability matrix.
Each release gate verifies the scope reported by its Codex runner. Focused
live pytest evidence includes the selected files and their ancestor
`conftest.py` inputs, including Git-ignored source. The optional
`knowledge_eval` and `wiki_perf` tiers remain separate.

The release report lists all selected stages from startup, with pending work
marked `not-run`. A test failure leaves the run nonzero and allows the remaining
independent stages to run. The offline browser runner records each selected
partition's command, effective worker count when Playwright reports it, native
JSON and offline audit, with Playwright traces retained on failure. Playwright
rejects focused `.only` tests during collection and partition execution. Stage
totals require valid counts from every partition. Selected live Codex gates
run independently and retain their own native reports, raw logs and network
audits. While their output is redirected, they print elapsed time every 15
seconds. Use separate output
directories for follow-up executions and link them to the first run in the
acceptance record.

For documentation, check local targets and anchors (including inbound links
when headings move), then run `.venv/bin/python scripts/gen-docs-index.py --check`
for both generated indexes and `git diff --check`. Do not run product suites,
builds, model calls or deployment for documentation-only changes.

Finish the release matrix during validation. The delivery owner then commits,
pushes and follows the linked publication procedure using the same evidence;
routine preparation opens no additional review or approval round.

## Test Coverage

Use `.venv/bin/pytest tests/ --collect-only -q` with the relevant marker/opt-in
selection for current cases and counts. Commands below start at the repository
root; use the PATH setup in [local-wsl.md](local-wsl.md#本机测试).

| Suite | Command | Scope and execution boundary |
|-------|---------|------------------------------|
| Backend hermetic | `./scripts/check.sh --gate pytest` | Full correctness for **Claude Code and Codex**, including simulated CLI processes, Wiki sources/jobs/maintenance, permissions, transactions and recovery. No live model or external network dependency; guards reject real model CLI execution and production callback port 8000. |
| Wiki performance (opt-in) | `.venv/bin/pytest tests/ --run-wiki-perf -m wiki_perf -v -s` | Large-dataset projection and grant-lookup cost measurements. Outside default full checks and release gates; small correctness and query-boundary checks remain in the hermetic tier. |
| Live Codex pytest | `.venv/bin/python scripts/codex_live.py --suite pytest --output "$CODEX_EVIDENCE_DIR"` | Full live Codex selection through the explicitly selected existing login; excludes device authentication and optional evaluation/performance tiers. Set `CODEX_HOME` and a new evidence directory using [the common acceptance entry](local-wsl.md#live-凭据与网络). |
| Supervisor integration | `.venv/bin/pytest tests/ --run-real -m "real and supervisor" -v` | Isolated systemd user units: installation ownership, environment/token handling, process retirement and managed restart. No provider CLI or production-service mutation. |
| Frontend unit | `cd web && bun run test` | Tooling tests in `web/tests/` use Node. The other Vitest tests use jsdom and the DOM setup. Both projects keep Vitest's native file matching and share one invocation and evidence reporter. |
| Offline browser E2E | `cd web && bun run test:e2e:fast` | Real browser plus isolated backend: login/accounts, sessions, input recovery, sidebar/pins, attachments, Wiki cards and page readiness. Uses a test-only harness and verifies an offline execution audit; actual provider behavior needs the live tier. |
| Codex browser E2E | `.venv/bin/python scripts/codex_live.py --suite browser --output "$CODEX_EVIDENCE_DIR"` | Real default Codex creation, complete streamed response, Monitor, original-receipt deduplication and precise native resume after reload/reopen. Uses the same isolated credential, network and evidence checks as pytest; the default `--suite all` runs both serially. |

## Topic regression entry points

Select the smallest check that can disprove the changed behavior. The current
contracts own their detailed counterexamples; the table below connects those
boundaries to verification without maintaining a second per-file inventory.

| Changed boundary | Contract | Representative checks |
| --- | --- | --- |
| E2E ownership and runner selection | [Browser ownership](#browser-ownership-and-execution) | `resource-isolation.spec.ts`, `e2eOwnership.test.ts`, `e2eExecution.test.ts`, `e2eUI.test.ts`: real account isolation, concurrent pin writes, visible cleanup errors and native selection. |
| Secure browser IDs and archive recovery | [Original-operation confirmation](implemented/frontend-session-observation.md#归档恢复的原操作确认), `web/eslint.config.js` | `secureIdLint.test.ts` checks the production `randomUUID` restriction and helper/test exceptions; helper tests retain secure randomness and local failure. Archive management retains direct archived navigation, first-generation overlap, original-ID confirmation and a single POST. |
| Native start, delivery and recovery | [Codex](implemented/codex-backend.md), [input evidence](implemented/input-reliability.md#codex-native-input-evidence) | `test_codex_app_server_submission.py`, `test_codex_native_recovery.py` |
| Context sample, native compact and busy selection | [Samples](implemented/model-reasoning.md#上下文样本与配置绑定), [compact Owner](implemented/input-reliability.md#native-compaction-requests), [Claude](implemented/harness-layer.md#claude-native-compaction) | `test_session_context.py`, `test_claude_context.py`, `test_codex_compact.py`, `test_claude_compact.py`, `test_queued_compact.py`, `context-meter.spec.ts`, `queued-compaction.spec.ts` |
| Original request settlement and failure takeover | [Completion](implemented/input-reliability.md#原请求完成与失败交接) | `test_terminal_recording.py`, `test_session_owner_failure.py` |
| App/build/schedule/Wiki/delegation completion | [Result selection](implemented/input-reliability.md#原请求结果读取) and each domain owner | `test_codex_app_server_results.py`, `test_unattended_domain_consumers.py` |
| Native output, images and child details | [Codex items](implemented/codex-backend.md#native-questions-and-output), [children](implemented/native-subagents.md), [Monitor](implemented/codex-performance-monitor.md) | `test_codex_native_items.py`, `native-output.spec.ts`, `native-items.spec.ts` |
| Native file-change Result | [Transcript view](implemented/frontend-transcript-view.md), [rewind](implemented/session-rewind.md) | `test_browser_messages.py`, `test_codex_app_server_fork.py`, `MessageBubble.test.tsx`, `archived-session-restoration.spec.ts` |
| Chat file links, `/showme` and file reading | [Viewer interaction](implemented/frontend-interaction-state.md#文件查看意图), [authorized sources](implemented/multi-tenancy.md#viewer-file-access) | `test_file_viewer.py`, `test_showme_ai.py`, `ChatView.showme.test.tsx`, `FileViewerDialog.test.tsx`, `markdown-preview.spec.ts`, `pdf-preview.spec.ts` |
| Fork/rewind, creation uncertainty and file attribution | [Fork](implemented/session-fork.md), [rewind](implemented/session-rewind.md), [storage](implemented/current-storage-format.md) | `test_session_fork.py`, `test_session_owner_rewind_retirement.py` |
| Reads, WS, reconnect and deletion | [Frontend](implemented/frontend-list-reconciliation.md), [socket delivery](implemented/multi-tenancy.md#websocket-delivery) | `test_ws_delivery.py`, `test_browser_session_restoration.py`, `sessionList.test.ts`, `ArchivedSessionsDialog.test.tsx`, `ChatView.test.tsx`, `session-deletion.spec.ts` |
| Focused failure evidence and retirement | [Fixed candidates and parallel acceptance](#fixed-candidates-and-parallel-acceptance), [retirement](#retiring-a-candidates-checks) | `test_check_runner.py`, `test_check_hooks.py`, `test_targeted_check.py`, `e2eEvidence.test.ts` |
| MCP original-command/waiter retirement | [MCP retirement](implemented/harness-layer.md#mcp-question-retirement) | `test_mcp_question_cancellation.py`, `test_ask_mcp.py` |
| Exact cancelled-input exception and root presentation | [Root completion](implemented/codex-backend.md#root-completion-and-selected-answer) | `test_mcp_input_completion.py`, `test_codex_app_server_completion.py` |
| Page question draft and answer association | [Question drafts](implemented/frontend-interaction-state.md#页面问题草稿与原回执), [settlement](implemented/input-reliability.md#问题结算) | `QuestionLifecycle.test.tsx`, `test_question_resolution.py`, `question-interaction.spec.ts` |
| Interaction policy, deadline, pause and missing decision | [Original-work policy](implemented/input-reliability.md#原工作交互策略与等待), [parent opportunity](implemented/agent-collaboration.md#unattended-questions) | `test_work_interaction_policy.py`, `test_unattended_parent_questions.py` |
| App response timeout and served client | [App access](implemented/app-agent-access.md) | `test_app_response_recovery.py`, `web/tests/appAgentClient.test.ts` |
| Wiki HTTP lifecycle and effect recovery | [Wiki](implemented/wiki-development-summary.md#恢复与效果核验) | `test_wiki_test_runtime.py` and the changed operation's API/job tests |
| Wiki byte refusal and stale projections | [Resource policy](implemented/wiki-development-summary.md#文档字节支持与资源拒绝) | `test_wiki_document_budget.py`, `test_wiki_search_resource_state.py` |
| Return admission, original work and installation drain | [Returns](implemented/input-reliability.md#公共返回接管), [BG](implemented/background-tasks.md), [installation](implemented/installation-lifecycle.md) | `test_return_admission.py`, `test_installation_drain.py` |
| Permanent deletion, source publication and question settlement | [Delegation](implemented/agent-collaboration.md#reply-delivery-the-original-request-completion), [Research](implemented/native-deep-research.md#researchmanager-async-job-like-bgdelegationsscheduler) | `test_work_publication.py`, `test_return_admission_lifecycle.py`, `test_research_manager.py`, `test_delegation_return_retry.py`, `test_unattended_parent_questions.py` |

A controlled peer or browser fixture proves the boundary it actually exercises.
It does not establish a new live-provider run, deployment, or arbitrary recursive
completion. Shortened-clock tests do not establish an actual 300-second wait.
Keep failed initial runs and passing targeted follow-ups distinct when composing
evidence under the reuse policy above.

## Shared fixtures and protocol evidence

Use [`tests/accounts.py`](../tests/accounts.py) to start an already owned
business test. It reuses one valid test password hash per worker while
creating each account's database record, default agent, key, workspace and
real bearer independently. Tests of registration, password behavior and KDF
work exercise the account APIs.

- `tests/native_app_server.py` keeps its function-scoped runtime, original Manager,
  SQLite database and cleanup together. `tests/session_runtime.py` owns shared
  dispatcher observations. Import support modules directly, not case modules.
- `scripts/check_test_imports.py` enforces that rule for static Python imports
  under `server/`, `tests/` and `scripts/`, in both the full gate and pre-commit.
  Historical consumer/module/symbol exceptions are frozen in
  `scripts/test-case-imports-baseline.json`; new consumers or imported symbols
  fail. `--prune-baseline` only removes retired exceptions. Move new shared
  helpers into support modules instead of adding exceptions.
- `tests/_fixtures/jsonl_frames.py` splits snapshots on byte newlines before UTF-8
  decoding/JSON parsing. Readers retain active-writer suffixes; explicit writer
  completion rejects an unfinished suffix, while complete malformed frames fail
  immediately. Final EOF delivers new complete frames exactly once and advances
  the offset. `test_native_protocol_framing.py` uses real append/pipe/RPC barriers
  without sleeps. This proves fixture framing, not product work identity;
  retain fixture version and provenance with the raw evidence; see
  [native protocol fixture scope](#native-protocol-fixture-scope).
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

## Native protocol fixture scope

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
  The retained live Default-mode fixture carries `isBlocking:false`; assert
  optional delivery rather than treating the tool name as blocking evidence.
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

## App, login and delegation ownership regressions

Use public entry points, controlled peers and real SQLite/files:

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

## Optional resource experiments

Resource measurements use synthetic files and isolated native homes, outside
default gates. Configuration/discovery probes send no model input. Before a
measurement, fix the source, CLI, dependency versions, workload and resource stop
conditions. Retain raw inputs, responses, failures and environment metadata.
Small boundary/correctness regressions remain in the hermetic suite.

实际输入成本与质量基线也属于明确委托的可选测量，不加入日常、提交或发布
门禁。固定源码、CLI／依赖、有效模型配置、来源、调用预算、样本数及历史／缓存
条件；受控 native peer 的传输与消费证据和 live 模型语义评阅分别保留，未做 live
的案例不据字节结果宣称质量不退化。记录各实际帧和最终工具响应，原始证据先
计量再脱敏；输出使用新目录，既有报告、帧、审计和 manifest 不得覆盖。耗时专项
与边缘测量不因每次实施或审查而重跑；同版证据复用，只有相关变化或具体失败
才补测受影响部分。质量评阅、未知用量及原资源收尾沿用
[评测方法](wiki-evaluation.md#model-evidence-and-retirement)。

## Math-rendering compatibility

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
applying security updates. Record measurement versions with coverage artifacts.

After a transitive security update, audit every locked copy. A patched hoisted
package can coexist with an older copy retained by a parent's exact pin. Prefer
a compatible parent update that adopts the fix, and preserve distinct dependency
branches rather than forcing them onto one major version. Validate contract
generation when updating its dependency chain, as well as frontend tests and the
production build. Report advisories that remain outside the callers' declared
ranges separately from fixes verified in the current tree.

## Coverage measurement (opt-in)

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

Record coverage with the source revision, dependency versions, selection and
excluded fixtures. Line and branch coverage describe that run; neither proves
live-provider behavior or establishes a release threshold.

## Markers, providers and isolation

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

Explicitly commissioned Wiki quality/cost work follows [Wiki evaluation](wiki-evaluation.md).
It is opt-in, outside ordinary development, commit and release gates; keep the
source/version, semantic review, actual usage and original-resource cleanup evidence
required there. Routine selection remains in [Test Coverage](#test-coverage).

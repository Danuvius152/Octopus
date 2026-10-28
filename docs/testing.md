# Testing and verification

This is the authoritative verification policy. [AGENTS.md](../AGENTS.md#verification)
keeps the everyday summary; this file owns the complete requirements and commands.

## Verification

Use fast checks for repeated development iterations and full checks before a
code commit. **Both exclude `knowledge_eval` and `wiki_perf` by default.** These
optional tiers require a separate evaluation or measurement task and explicit
opt-in; they are outside commit and release gates.

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
| Backend hermetic | `.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" -n 4 --dist=loadfile --max-worker-restart=0 -q` | Full correctness, including Wiki sources/jobs/maintenance, permissions, transactions and recovery. No live model or external network dependency; guards reject real model CLI execution and production callback port 8000. |
| Wiki performance (opt-in) | `.venv/bin/pytest tests/ --run-wiki-perf -m wiki_perf -v -s` | Large-dataset projection and grant-lookup cost measurements. Outside default full checks and release gates; small correctness and query-boundary checks remain in the hermetic tier. |
| Live Codex pytest | `.venv/bin/pytest tests/ --ignore=tests/test_codex_login_real.py -m "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval" -v` | Actual Codex execution, streaming/tools, scheduling, memory, fork, research and Wiki through the selected existing login. New device authentication is excluded from routine release checks. Use the isolated credential setup in [本机测试](local-wsl.md#本机测试). |
| Supervisor integration | `.venv/bin/pytest tests/ -m "real and supervisor" -v` | Isolated systemd user units: installation ownership, environment/token handling, process retirement and managed restart. No provider CLI or production-service mutation. |
| Frontend unit | `cd web && bun run test` | Stores, HTTP/WS events, components, input lifecycle, account/service generation boundaries and Wiki job cards. Vitest tests are colocated with source. |
| Offline browser E2E | `cd web && bun run test:e2e:fast` | Real browser plus isolated backend: login/accounts, sessions, input recovery, sidebar/pins, attachments, Wiki cards and page readiness. Uses a test-only harness and verifies an offline execution audit; actual provider behavior needs the live tier. |
| Codex browser E2E | `cd web && bun run test:e2e:llm codex.spec.ts` | Real Codex session creation, response streaming and Monitor reporting through the product backend. Needs the installed, signed-in Codex CLI. |

### Markers, providers and isolation

- **`real` selects live-provider and supervisor integration.** `real_claude`
  and `real_codex` require a signed-in CLI; `claude_bin` and `codex_bin` require
  the binary. Availability probes run at setup in `tests/conftest.py`, so
  collection and hermetic checks do not invoke model CLIs.
- **This installation validates Codex. Claude is unavailable; its live CLI and
  browser tests are not run and are outside this installation's release gate.**
  Report that scope explicitly. Hermetic protocol coverage of both backends
  does not establish a passing two-provider live suite. Preserve supported
  backends and agent settings; do not probe, install or authenticate Claude.
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

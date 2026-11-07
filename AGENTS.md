# Octopus Development Rules

## Do It Right The First Time (no MVPs, no future-polish)

**If we choose to do something, we do it perfectly — right now, in
this session.** No "minimal fix", no "MVP for now", no "we'll polish
this later". No deferral of cleanup to a "follow-up" item.

This rule is non-negotiable. Specifically that means:

- Never ship a half-done implementation and document the rest as
  "future work". If the full thing isn't worth doing right now, then
  don't start it at all.
- Never park the cleaner version in a "deferred work" note
  *instead* of doing it. Genuine deferrals (work that needs a real
  second use case, an external dep, or a user decision) belong in
  the owning plan's deferred-scope section; nothing else is a
  legitimate place to stash "felt too long".
- Never add a comment like `# TODO: handle X properly later` or `#
  HACK: works for now`. If `X` matters, handle it in this change.
  If it doesn't matter, delete the comment.
- When the user asks "fix this", interpret it as "fix it the way a
  careful engineer with infinite time would" — not "ship the
  smallest patch that no longer crashes".
- "MVP" is not a status the user has to accept. There is no future
  in which a later session will go back and polish; in the AI era
  we have the bandwidth to do it right *now*, here, in one go.

This rule exists because past sessions repeatedly took the shortcut
and then had to be told to go back and do the real thing. Skip the
shortcut. Do the real thing the first time.

## Scope and completion

Completeness means the behavior the user authorized, its real consumers,
failure paths and removal of the replaced path. The quality standard above
does not expand the task to adjacent products or speculative requirements.

For changes to ownership, concurrency, lifecycle, authorization, persistence
or an external protocol, briefly state the affected facts, writers, commit
points, consumers and failure scope before implementation. Revise that model
when evidence invalidates it, before adding another state, retry or fallback.
Routine local edits need no new plan. Stop when the agreed acceptance
conditions and applicable checks are satisfied.

Preserve concurrent work, current user data and saved agent configuration.
A design choice or feasibility report does not authorize implementation,
production data cleanup or a transport migration. Record genuine dependencies
and deferred scope in the owning plan; report unresolved work accurately.

## Project Structure

```text
server/
  sessions/             SessionOwner: input, questions, execution and retirement
  session_manager.py    Session entry points, cross-session quota and registration
  harness/              Runtime profiles, subprocess I/O, event normalization, login
  db/ + database.py     SQLite persistence, current schema and transactions
  routers/              REST and WebSocket entry points
  wiki/                 Shared Markdown knowledge, retrieval and assigned maintenance
  research/             Cross-turn research orchestration and isolated model leaves
  mcp_servers/          Agent tools; mcp_http.py + mcp_identity.py own HTTP scopes
  delegations.py        Named-agent work rounds and parent result delivery
  scheduler.py          Scheduled runs; bg_tasks.py owns background shell tasks
  agent_manager.py      Saved agent configuration; agent_memory.py owns memory paths
  applications.py       Application build sessions and completion projection
  app_agent.py          Running applications' conversations with agents
  app_backends.py       Supervised application processes and code/data/runtime roots
  connectors/           Providers; connector_manager.py owns login and refresh
  users.py + crypto.py  Accounts, owned keys and credentials
  workspace.py          Account paths; portability.py owns offline account transfer
  cli.py                Serving, session handoff/pull and account export/import
web/src/                React UI, zustand state, WebSocket handling and unit tests
web/e2e/                Playwright; test.ts is the shared offline entry
tests/                  Backend correctness and explicitly selected integration tiers
scripts/                Checks, generated contracts and documentation index
docs/                   Current guides, contracts, unfinished plans and version references
.scratch/               Grill drafts, specs and tickets; see local task conventions
```

Read the relevant part of [architecture.md](docs/architecture.md) for component
relationships. Use [the roadmap](docs/roadmap.md) for future priorities and
dependencies, and [the documentation index](docs/README.md) to find a topic.
For completion consumers or Owner failure takeover, read
[original-request completion](docs/implemented/input-reliability.md#原请求完成与失败交接).
For captured execution/exit proof, read
[execution observation](docs/implemented/input-reliability.md#原执行的观察);
for process or MCP-call retirement, read
[Harness resources](docs/implemented/harness-layer.md#execution-and-resources) or
[MCP retirement](docs/implemented/harness-layer.md#mcp-question-retirement).
Business outcome, notification delivery and resource retirement have separate authorities.

Persistence changes follow [current storage and publication handoff](docs/implemented/current-storage-format.md).
New persistent types, relationships or roots must also update
[account portability](docs/backup-and-migrate.md#account-export-and-import)
collection, exclusions, restoration and verification in the same change.

## Verification

During implementation, start with the target regression and affected checks.
Before a code commit, complete the gates in `scripts/check.sh`; before release,
also complete this installation's offline E2E, live Codex pytest and Codex browser
checks. Documentation-only changes need affected links, index and diff checks.

Give each check one executor. Reuse successful evidence while its code,
configuration, dependencies and environment remain unchanged; investigate
failures and rerun only invalidated checks or a concrete new failure hypothesis.
Inspect the actual test summary or build output before claiming success.

### Test Coverage

The authoritative suite commands, fixture rules, evidence-reuse conditions and
release matrix are in [testing.md](docs/testing.md). Read the relevant sections
before running checks; do not maintain a second test-count inventory.

### Backend validation scope

Offline correctness covers both Claude Code and Codex, including backend-specific
protocol and simulated-process tests. Shared harness or session changes require
the affected coverage for both backends; Claude's live-test exclusion does not
exclude its offline tests. This installation's live validation scope remains
Codex only; do not probe, install or authenticate Claude for routine checks.
Keep persistent test state isolated and use the selected credentials without
copying secrets or changing agent defaults. `knowledge_eval` and `wiki_perf`
remain independently opt-in and outside default commit/release gates.

## Common commands

```bash
# Focused development checks
.venv/bin/pytest <affected-files-or-node-ids> -m "not real and not knowledge_eval and not wiki_perf" -q
.venv/bin/ruff check <changed-python-files>
.venv/bin/mypy
(cd web && bun run test <affected-test-files>)
(cd web && bun run lint)
(cd web && bun run typecheck)

# Documentation-only checks
.venv/bin/python scripts/gen-docs-index.py --check
git diff --check

# Full code-commit gates; reuse existing passing evidence
./scripts/check.sh

# Local development
.venv/bin/uvicorn server.main:app
(cd web && bun run build)
(cd web && bun dev)
```

Commands start at the repository root. The backend serves `web/dist/`, so
frontend source edits need the build command above for backend-served previews;
Vite HMR uses port 5173. The running release is a separate snapshot: restarting
the service does not publish the development tree.

## Conventions

- Python 3.12+, type hints and async/await; React 19, strict TypeScript and zustand.
- Use `useSessionStore.getState()` in state-mutating callbacks/effects to avoid
  selector-driven render loops.
- Use the existing Python venv and pip, not uv. Keep fixed application assets
  local; use installed test tools and fail clearly when a dependency is missing.
- Resolve the nvm JS toolchain and `~/.local/bin` Codex on PATH for noninteractive
  checks. Use [local test setup](docs/local-wsl.md#本机测试) for PATH, proxies and
  credential isolation, and [service management](docs/local-wsl.md#服务管理) for operations.
- Read only task-relevant references. For implementation tasks, follow
  [documentation update timing](docs/AGENTS.md#writing-and-maintenance).
  For documentation, persona or memory changes, read those documentation rules
  and use the existing [documentation index](docs/README.md) to find the owner.

## Agent skills

### Issue tracker

This project uses local Markdown files as its issue tracker. Grill tasks keep
intermediate artifacts and derived specs/tickets in `.scratch/<task>/`; other
new temporary tasks default to the account workspace. Read
[local task conventions](docs/agents/issue-tracker.md) before publishing or
fetching specs, tickets or maps. Routine work does not require tickets.

### Triage labels

Use the five default triage labels in
[triage-labels.md](docs/agents/triage-labels.md).

### Domain docs

Use the root `GLOSSARY.md`, current contracts in `docs/implemented/` and
unfinished formal plans in `docs/plans/`. Read
[domain.md](docs/agents/domain.md) for consumption and decision placement.

### Local tasks

Use the account workspace by default for new temporary tasks, experiments and
isolated source worktrees; grill task material follows the directory convention
above. Edit deliverable code in the selected repository checkout or worktree.
Test runtime state follows its separate isolation rules; the test cache is not
a general development directory.

Durable decisions and final evidence belong in existing `docs/` owners before
temporary task material is removed. Preserve active tasks at their current
paths until their processes and references can be retired or updated safely.
Skills support the task's scope and existing authorization.

## Repository and Upstream Maintenance

`origin` is the independently maintained `Danuvius152/Octopus`; `upstream` is
`archeryue/Octopus` for requested updates. Released milestone tags are immutable.

- Keep secrets, runtime data and unrelated or concurrent work out of commits.
  For implementation tasks, commit/push the authorized, verified code and its
  required documentation to `origin` in focused commits, unless the user asks
  to leave the work uncommitted. Include documentation corrections completed
  after the code commit in that delivery.
- For documentation-only tasks, including designs, specs, reviews, local
  tickets and agent guidance, complete the applicable documentation checks
  and leave changes uncommitted by default. Commit or push only when the user
  explicitly requests that action; reuse authorization already given for the
  same task. These tasks do not trigger application builds, full product
  suites, model calls, restarts or deployment.
- After pushing validated `main` code, follow [自动发布](docs/local-wsl.md#自动发布)
  without another confirmation: prepare the candidate, wait for natural drain,
  switch matching backend/assets and retire the old worktree. Preserve running
  or pending work and keep the old service if preparation fails.
- On a user-requested upstream sync, protect uncommitted work, fetch and inspect
  changes, then merge `upstream/main`. Preserve local behavior and configuration;
  do not hard reset or blindly resolve conflicts. Use the same validation and
  deployment flow for the resulting code.

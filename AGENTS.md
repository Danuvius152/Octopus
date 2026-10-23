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
  the relevant plan doc's §10 "What this defers"; nothing else is a
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
| Before release on this Codex installation | All commit gates, **offline E2E**, **live Codex pytest** and **Codex browser E2E** from Test Coverage below; then follow [自动发布](docs/local-wsl.md#自动发布). Include supervisor checks when managed-service behavior changed. |
| Documentation only | Affected links, index and diff checks; follow Repository and Upstream Maintenance below. |

Complete the release checks during validation and reuse valid results. When the
user says to release, proceed directly with [自动发布](docs/local-wsl.md#自动发布);
routine release preparation does not open another review or approval round.

## Test Coverage

Use `.venv/bin/pytest tests/ --collect-only -q` with the relevant marker/opt-in
selection for current cases and counts. Commands below start at the repository
root; use the PATH setup under Conventions.

| Suite | Command | Scope and execution boundary |
|-------|---------|------------------------------|
| Backend hermetic | `.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" -n 4 --dist=loadfile --max-worker-restart=0 -q` | Full correctness, including Wiki sources/jobs/maintenance, permissions, transactions and recovery. No live model or external network dependency; guards reject real model CLI execution and production callback port 8000. |
| Wiki performance (opt-in) | `.venv/bin/pytest tests/ --run-wiki-perf -m wiki_perf -v -s` | Large-dataset projection and grant-lookup cost measurements. Outside default full checks and release gates; small correctness and query-boundary checks remain in the hermetic tier. |
| Live Codex pytest | `.venv/bin/pytest tests/ -m "real and (real_codex or codex_bin) and not real_claude and not knowledge_eval" -v` | Actual Codex execution, streaming/tools, scheduling, memory, fork, research, Wiki and login contracts. Use the isolated credential setup in [本机测试](docs/local-wsl.md#本机测试). |
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
  [本机测试](docs/local-wsl.md#本机测试). Assess answers against recorded source
  versions; execution success alone is not semantic acceptance.
- **`wiki_perf` is independently opt-in**, including when answer evaluations are
  enabled. Keep correctness cases at the smallest useful size and reuse setup
  across read-only variations; mark dedicated scale/cost measurements instead
  of excluding Wiki behavior tests as a group.

## Project Structure

### Core

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/sessions/` — SessionOwner serializes mutable execution, question and lifecycle state; managed Harness I/O reports original results. For completion consumers, failure handoff or archive/retirement changes, read `docs/plans/minimal-architecture-execution-20261020.md` §3.2 and its ticket20 evidence pointer. Business outcome, notification delivery and resource retirement have separate authorities.
- `server/database.py` — SQLite persistence layer
- `server/scheduler.py` — APScheduler-based recurring task runner
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `server/routers/` — REST + WebSocket routers (`sessions`, `schedules`, `agents`, `applications`, `credentials`, `connectors`, `delegations`, `research`, `ws`)
- `server/fork_helpers.py` — Pure helpers for session tree-rewind (`/rewind`): git-anchor capture at turn-start, side-effect classification over parent rows, safe-revert preflight + git-stash execution. Backend-agnostic and side-effect-contained.
- `server/research/` — Native deep research orchestration (`docs/plans/native-deep-research.md`): `ResearchManager` (async job lifecycle, phase pipeline, concurrency cap, cancel + reap), `orchestrator` (scope → search → dedup → verify → synthesize phases), `leaf` (throwaway `HarnessRun` sub-turns for web-search leaves and `run_oneshot` for reasoning leaves), `schemas` (JSON schemas for scope/findings/synthesis). Agent-invoked via `mcp__research__deep_research`; user-invoked via `/research <question>`. Result injected as a follow-up turn; a `ResearchCard` tracks progress in the UI.
- `server/mcp_servers/research.py` — MCP namespace exposing `mcp__research__deep_research(question)` to agents; thin HTTP shim to `/api/sessions/{sid}/research`. Returns `research_id` immediately so the model's turn ends cleanly.
- `docs/plans/token-rotation.md` — the install-era `OCTOPUS_AUTH_TOKEN` is both
  the credential clients send and the Fernet key for unowned (NULL-owner)
  secrets. Owned secrets use the master key → per-account DEK
  ([crypto.py](server/crypto.py); see [architecture.md](docs/architecture.md)'s
  `crypto.py` row). Rotation re-keys only the install-era set, rewrites the env
  files, swaps the live setting and retires processes carrying the old token.
  A password change re-encrypts nothing, and dropping a DEK crypto-shreds that
  account. No restart, nothing to edit by hand.
- `docs/plans/scheduled-runs.md` — What a scheduled fire is: the overlap
  guard that stops a slow schedule stacking runs, the `[scheduled:…]` marker
  that tells an agent nobody is waiting to answer questions, and
  `last_run_session_id` linking "last run" to the session it happened in.
- `docs/plans/native-subagents.md` — Native sub-agents, surfaced: Claude
  Code's `Task`/`Agent` runs and Codex's `spawn_agent`/`wait` collaboration
  calls normalized onto one `SubagentUpdate`, shown as a live card keyed on
  the spawning tool call's id (never its name — the CLIs rename it), and
  `agents.subagents` → `--agents` so an Octopus agent brings its own helpers.
- `server/app_agent.py` — Conversations a running application holds with an agent (`docs/plans/app-agent-access.md`), under `/apps/{id}/agent/*` with a scoped `OCTOPUS_APP_TOKEN`. A conversation is a real session (`origin='app'`, `sessions.app_id`). The manager registers before submission, streams session events, and consumes the original receipt; successful `done` requires the acknowledged result and delivered notification prefix. `chat` streams SSE; `ask` awaits the same operation.
- `server/app_backends.py` — Supervised backend processes for Applications (`docs/plans/application-backends.md`): an app declares a backend with an executable `start.sh` at its root (optionally `install.sh`), Octopus allocates a port, runs them with a minimal environment, waits for the port to accept, proxies `/apps/{id}/api/*` to it, and stops it when idle. Three directories with three owners: `<slug>/` code (rewritten by a rebuild), `<slug>.data/` the app's own state (never touched), `<slug>.runtime/` installed dependencies (deletable to force a clean reinstall).
### Applications

- `server/applications.py` — Applications (`docs/plans/applications.md`). Owns the app directory, build session (`origin='application'`) and build-status projection. The original request completion triggers the entrypoint check, including direct build-session chat; broadcast supplies current running progress. Unavailable outcomes end the build explicitly. `POST /{id}/build` runs another turn in that same session. Archiving
(`POST /{id}/archive` / `/unarchive`) keeps the row **and** the files so the
Applications page's Archived section can restore an app exactly as it was;
the name index is live-only, so an archived name frees up (same rule as
agents, which gained `POST /api/agents/{id}/unarchive` for the same section).
- `server/routers/applications.py` — `/api/applications` CRUD **plus** `/apps/{id}/{path}` — the app itself, streamed out of its directory with traversal + symlink guards, `Cache-Control: no-store`, and bearer / `?token=` / `octopus_app_token`-cookie auth (an iframe can't send an Authorization header)
- `server/agent_manager.py` — Agent CRUD (durable assistant definitions that own sessions/schedules)
- `server/agent_memory.py` — Per-agent native memory (`docs/plans/memory.md`): one canonical markdown dir per agent (`<agents_dir>/<id>/memory/`), shared by both harnesses. Claude points its auto-memory at it via `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`; Codex via an injected `developer_instructions` blurb naming the dir (its native `features.memories` pipeline is unused — it doesn't run in headless `exec`). Memory is decoupled from both harnesses' config/auth dirs — `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are never touched, so auth and `--resume` transcripts are unaffected. Pure path helpers + idempotent provisioning.
### Accounts

- `docs/plans/multi-tenancy.md` — one site, many people: the label becomes a
  username, the token becomes a password, and every account gets its own rows,
  keys and directories. The install itself is an era: before the first account
  `OCTOPUS_AUTH_TOKEN` is the way in and `user_id` is NULL on every row; after
  it, the token opens nothing and every row has an owner. `POST
  /api/auth/bootstrap` is the one-way door between the two — it re-encrypts
  every stored secret under the new account's key, adopts the orphan rows *and
  the live `Session` objects*, and keeps the working directories the existing
  sessions already use as that account's `extra_roots`.
- `server/users.py` — accounts: scrypt password hashing (stdlib, so no new
  dependency), `UserManager` (create / authenticate / set-password / disable /
  issue-token / resolve-token / invites / register). A password is spent once
  for a bearer that can be revoked without changing it; the row keeps only
  digests.
- `server/crypto.py` — `master_key()` (env wins, else a 0600 file created with
  `O_EXCL`) plus per-account DEK wrap/unwrap. Generating the key rather than
  demanding one is deliberate: a key an operator has to invent is a key that
  ends up guessable or lost, and losing it loses every stored secret.
- `server/workspace.py` — `paths_for(user_id)`: every `settings.*_dir` as a
  function of an account, with the pre-accounts answer being the legacy layout.
  `confine()` resolves symlinks *before* comparing, because a check against the
  literal path is decoration.
- `server/bootstrap.py` — the upgrade above, in one operation, reporting what it
  moved rather than asking to be trusted.
- `server/db/users.py` — `users` / `invites` / `auth_tokens`, `adopt_orphan_rows`
  (idempotent by `user_id IS NULL`, not by a ledger stamp), and `claim_invite`
  whose use-count check is inside the UPDATE so two registrations cannot both
  win the last use.
- `web/src/components/SignIn.tsx` / `AccountPage.tsx` — the sign-in screen asks
  whichever question the install can answer (`GET /api/auth/state`), and the
  account page is where an install becomes an account, a password changes, and
  an admin manages invites and people.

### Agents, memory and delegation

- `server/delegations.py` — Agent delegation (`docs/plans/agent-collaboration.md`). Each immutable work round consumes its exact receipt completion, classifies nested work before reading committed reply text, and submits one stable parent relay. Broadcast forwards questions. Answer uses the child's Owner record; cancellation joins the captured original work branch. The delegation id is the child session id; follow-up creates a new work round in that conversation. No reply-text mirror or parallel result/error finalizer.
- `server/mcp_servers/ask_agent.py` — MCP namespace exposing the four delegation tools to the model: `mcp__ask_agent__ask` / `cancel` / `answer` / `list` (the Python functions are `ask_agent` / `cancel_agent_task` / `answer_agent_question` / `list_agent_tasks`; the `@mcp.tool(name=…)` decorators expose the short forms). `ask(request, name=…, delegation_id=…, files=…)` is bimodal: `name` starts a fresh child session, `delegation_id` continues a prior one in the same child transcript; exactly one id is required. Same shape as the bg + ask built-ins (the session comes from the call's verified scope — `mcp_http` / `mcp_identity`); thin HTTP shim to the `/api/sessions/{sid}/delegations` routes, including continuation via `/follow-up`. Included in new agents' default MCP set; existing agents retain their saved choices across startup and upgrades.
- `server/mcp_servers/schedule.py` — MCP namespace exposing an agent's own schedules (`docs/plans/schedule-tool.md`): `mcp__schedule__{create,list,update,delete}`. Thin HTTP shim to the session-scoped `/api/sessions/{sid}/schedules` routes, so the agent is derived from the call's session and can neither see nor touch another agent's schedules. The recurrence is stated outright (`cron` / `interval_seconds` / `run_at`) rather than parsed from English — the caller is already a model, so there is no second one-shot in the loop. Included in the creation defaults; saved tool choices remain user-controlled.
### The harness boundary

- `server/mcp_http.py` + `server/mcp_identity.py` — The tool namespaces, served in-process (`docs/plans/polish-2026-09.md` §4 B1): each is mounted at `/mcp/<key>` over streamable-HTTP instead of spawned as a stdio subprocess per session (7 per session, ~39 MB PSS each), and identity rides in a per-session bearer because Codex has no per-server header. `build_servers` also moves every `def` tool body onto a worker thread — FastMCP dispatches a sync tool on the event loop, and these bodies call back into this same process, so on the loop each call deadlocked until its own timeout.
- `server/deps.py` — Request-scoped dependencies (`§3 A2`): routers declare `session_manager: SessionMgr` and FastAPI resolves it, so a test overrides one dependency instead of monkeypatching a module global in every router. The singleton stays for the background paths (scheduler, delegations, bridges).
- `server/aio.py` — `drain_cancelled` / `stopped_within`: the two teardown shapes that were written as `except (asyncio.CancelledError, Exception): pass` in eight places (`§8 F4`). The cancellation is expected; anything else is the task's last words and now reaches the log.
- `server/harness/` — Harness layer: the single boundary for all model/runtime interaction (`docs/plans/harness-layer.md`). One `Harness` class + one `HarnessRun` engine, configured by a `RuntimeProfile` *value* per backend kind (`claude_code`, `codex`) — no per-framework subclasses. Holds `assembly` (shared per-turn MCP/system-prompt assembly), `run` (subprocess+JSONL engine + PATH helpers), `registry` (`get_harness`/`available_backends`), `login` (LoginDriver protocol). Capabilities are derived from the profile; `run_oneshot` powers backend-agnostic `/schedule` parsing.
### Connectors

- `server/connectors/` — Connector framework: `base` (ConnectorBase + backend-neutral MCP entry), `oauth` (provider protocol + redirect-URI login manager), `registry`, built-in `github`/`gmail`, and `custom` (user-defined kinds + generic OAuth provider + `resolve_connector`)
- `server/connector_manager.py` — Connector business logic (install upsert, in-app OAuth-client config DB→env resolve, token-refresh lifecycle, custom-connector CRUD)
- `server/mcp_servers/connectors/` — Per-kind MCP namespaces (`github`, `gmail`, generic `custom`) + shared token/truncation helpers
- `docs/plans/composer-and-time.md` — Three affordances the transcript and
  composer were withholding: `messages.created_at` revealed on hover
  (`MessageTime`, attached once in `ChatView.renderMessage` so every message
  shape gets it, and stamped in `_persist_message` so the WS event carries the
  same value as the row — NULL and blank for rows older than the column, never
  invented); shell-style ArrowUp/ArrowDown history (`lib/composerHistory.ts`,
  pure; `lib/injectedTurns.ts` is the one list of user-role messages the user
  never typed, shared with the rewind picker); and `/model`, a per-session
  override in `sessions.model` that wins over `agents.model` — the same shape
  `sessions.backend` has. The route stores any string on purpose;
  `RuntimeProfile.models` is a shortlist for the picker, empty for Codex
  because nothing here establishes which names `codex -m` takes.
- `docs/plans/sidebar-pins.md` — The sidebar is shortcuts, the page is
  everything: `pinned` + `pin_order` on agents and applications, the sidebar
  lists only pinned rows (drag or keyboard to reorder, `@dnd-kit`), and the
  Agents / Applications pages' **All** tab (the section's "+", then All)
  lists every row — pinned, not pinned, archived — with pin, open/chat,
  archive, restore. Nothing in the UI hard-deletes: removal is always an
  archive (a sidebar session's button is `archive?replace=false`). An
  unpinned row joins the sidebar only while it needs you
  (`lib/sidebarPins.ts`). Unpinned is still live (delegation, schedules,
  serving); archived is out of use. Agents now broadcast `agent_*` events.
- `docs/plans/mobile.md` — Octopus on a phone: the drawer that puts itself
  away, 16px fields (iOS zoom), touch hit areas and hover-only affordances,
  headers that shed context rather than function, and the safe-area insets a
  home-screen install needs. Three separate triggers kept apart: `max-width`
  for layout, `hover: none` for input, `env(safe-area-inset-*)` for hardware.
  A touch test must set `isMobile`/`hasTouch`, not just a narrow viewport —
  resizing triggers the `max-width` rules and leaves `(hover: none)` unmatched,
  which is how the sidebar shipped with every row's delete button revealed at
  once.
### Frontend and tests

- `web/` — React frontend (Vite + TypeScript). The interface follows
  `docs/plans/console-redesign.md`: `src/styles/tokens.css` is the whole
  palette + type system (blue `#2563b8`, Hanken Grotesk over IBM Plex Mono for
  anything technical), the sidebar is `SidebarAgents` (two-level: agents with
  their sessions) + `SidebarApplications` + `SidebarManage` (Schedules /
  Connectors / Harness summary rows) + `SidebarAccount`, and every manage or
  create surface is a **main-area page** behind the store's `mainView`
  (`SchedulesPage`, `ConnectorsPage`, `HarnessPage`, `AgentFormPage`,
  `ApplicationFormPage`) rather than a dialog. `PageHeader` is the shared
  breadcrumb bar.
- `tests/` — Backend tests (pytest)
- `web/src/**/*.test.{ts,tsx}` — Frontend unit tests (vitest, colocated with source)
- `web/e2e/` — End-to-end tests (Playwright, auto-cleanup after runs)

## Commands

> **Frontend gotcha**: the backend serves `web/dist/` (the built SPA),
> not `web/src/`. Any source change needs `cd web && bun run build`
> before `octopus serve` / `uvicorn server.main:app` users will see it.
> For live HMR, run `cd web && bun dev` and hit the dev server's port
> (5173) instead of the backend.
>
> **Shipping to the running service**: follow
> [自动发布](docs/local-wsl.md#自动发布). Build and validate the candidate
> snapshot before the switch, wait for natural drain, then atomically switch
> the backend and its matching built assets. The development tree and the
> running release are separate; restarting does not publish unverified edits.

```bash
# Full integration checks (includes the full hermetic backend suite)
./scripts/check.sh
./scripts/check.sh --e2e               # + both complete E2E buckets (both CLIs)

# Backend
.venv/bin/pytest tests/ -m "not real and not knowledge_eval and not wiki_perf" -n 4 --dist=loadfile --max-worker-restart=0 -v
.venv/bin/uvicorn server.main:app       # start server (serves web/dist/)

# Frontend
cd web && bun run test                  # run frontend unit tests
cd web && bun run build                 # typecheck + build (refreshes web/dist/)
cd web && bun dev                       # live dev server on :5173

# E2E (Playwright)
cd web && bun run test:e2e              # both buckets, headless
cd web && bun run test:e2e:ui           # Playwright UI
cd web && bun run test:e2e:fast --reporter=list  # verbose offline E2E
```

## Conventions

### Language and style

- Backend uses Python 3.12+, type hints, async/await
- Frontend uses React 19, TypeScript strict mode, zustand for state
- Use `useSessionStore.getState()` (not hook selectors) inside callbacks/effects that mutate store to avoid re-render loops
### Environment traps

- The JS toolchain (`bun`, `node`, `npm`, `npx`) lives under `~/.nvm/versions/node/*/bin`; the local standalone `codex` is exposed through `~/.local/bin`. Prepend both directories for frontend and real-CLI checks (`export PATH="$HOME/.nvm/versions/node/<ver>/bin:$HOME/.local/bin:$PATH"`). See `docs/local-wsl.md` for the current installation and proxy settings.

## Repository and Upstream Maintenance

`origin` is the independently maintained `Danuvius152/Octopus`; `upstream` is
`archeryue/Octopus` for requested updates. The GitHub fork relationship is removed.

- Use the existing venv + pip (no uv), signed-in Codex and `.env` / `OCTOPUS_*`
  configuration. Keep secrets and runtime data out of Git; local setup is in
  [local-wsl.md](docs/local-wsl.md).
- Commit/push authorized, verified code to `origin` in focused commits, with
  necessary related docs. Exclude concurrent or unrelated work.
- Docs-only: check affected links, index and diff; leave changes unstaged and
  uncommitted unless the user requests otherwise. No automatic push, builds,
  full suites, model calls, restarts or deployment, even after a requested docs commit.
- After pushing validated `main` code, follow [自动发布](docs/local-wsl.md#自动发布),
  including natural drain and old-worktree retirement, without another confirmation.
- On a user-requested upstream sync, protect uncommitted work, fetch and inspect
  upstream changes, then merge `upstream/main`. Preserve local behavior/config;
  do not hard reset or blindly resolve conflicts. Code changes use the same
  checks/build/deployment flow above.

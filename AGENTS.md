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

## After Every Code Change

You MUST verify your changes before considering them done:

Wiki `knowledge_eval` is opt-in, deselected before setup/CLI probes, and is
not a release gate. Assess answers against recorded source versions, not exact
wording or fixed personal facts. Permission, source-preservation, revision/index,
pagination, receipt, settlement and CLI/browser checks remain required.

1. **Backend tests**: run the hermetic tier and the available provider's
   real-CLI tier. This installation defaults to **Codex**; **Claude is not
   installed/configured and its live tests are not selected**. Do not probe,
   install or authenticate Claude merely to run this task's checks. Offline
   protocol tests for either backend remain part of the hermetic tier. A
   machine with both providers must validate both. Select live tests by marker,
   not by listing filenames:

   - `pytest -m "not real and not knowledge_eval"` — the hermetic tier, with
     no CLI required. This is the release selection; `knowledge_eval` remains
     opt-in even if its fixtures do not call a model.
   - `pytest -m real` — all live-provider cases. `real_claude` and `real_codex`
     want an installed, signed-in CLI; mixed-provider cases require both.
     `claude_bin` and `codex_bin` need only the binary. Apply the local provider
     selection below instead of probing providers this installation lacks.

   The markers resolve in `tests/conftest.py:pytest_runtest_setup`, so the
   availability probe fires at setup and never at import — evaluating it at
   import is what used to make a plain `--collect-only` spawn a real `claude`
   call (16.44 s vs 0.80 s). Run with the nvm bin prepended so `codex`
   resolves (see Conventions).
2. **Frontend unit tests**: `cd web && bun run test`.
3. **TypeScript check**: `cd web && bun run typecheck` (`tsc -b --force`).
   Not `tsc --noEmit`: `web/tsconfig.json` is a solution file — `"files": []`
   plus two references — so `--noEmit` on it checks *zero* files and exits 0
   whatever is broken. It passed for months while `bun run build` failed.
4. **E2E tests**: `cd web && bun run test:e2e` (Playwright auto-starts servers).
   It *is* the two buckets, run as two
   invocations — not one run that covers both. The accounts spec needs a second
   backend and dev server of its own (`playwright.config.ts`: claiming an
   install retires the token every other spec signs in with), and with those two
   idle servers up the real-CLI half failed a different Chat test on each of two
   runs. So the buckets are also the unit of iteration —
   `bun run test:e2e:fast` (pure UI — login / accounts /
   sessions /
   dialogs / sidebar / sidebar pins / virtualized chat / attachments / etc.) and
   `bun run test:e2e:llm` (real LLM, **one worker** — two
   concurrent real-CLI turns on this box time out about one run in three, a
   different test each time, and serially the bucket is faster anyway — chat, /schedule,
   an agent scheduling itself, /showme, /archive, mcp__bg__run, AskUserQuestion, agent-collaboration,
   notifier, codex sign-in, handoff/pull). Anything that drives a real
   `claude` / `codex` turn carries `@llm` in its describe title; the
   `:fast` script uses `--grep-invert @llm`. `codex.spec.ts` needs a
   signed-in `codex` (`codex login --device-auth`); a lapsed login fails
   it rather than hiding it.

**All required release checks must pass before committing.** Investigate and
fix failures; do not dismiss them as "flaky" or "pre-existing". Unavailable,
skipped or deselected checks do not count as passes.

### Local Codex validation

On this WSL installation, require hermetic backend, frontend unit/types,
pure-UI E2E, Codex real-CLI/browser, lint, generated-contract and docs-index
checks. Reports and reviewer briefs must state the provider scope explicitly:
**Codex: actual results; Claude: unavailable, live tests not run**. Never call
this a passing two-provider suite. Claude-only and mixed-provider live tests
are outside this installation's release gate, not an unresolved release task.
Do not change the project's supported backends or existing agent settings.
The Codex selection is
`real and (real_codex or codex_bin) and not real_claude and not knowledge_eval`.

Opt-in evaluation setup, registration and evidence rules are in
[local-wsl.md](docs/local-wsl.md#本机测试). Preserve failures and raw usage;
execution success still needs independent semantic review. Do not change agent
defaults or add paid availability probes. Fixed event-replay assertions remain
valid protocol checks.

## Test Coverage

| Suite | Tool | Command | Scope |
|-------|------|---------|-------|
| Backend unit | pytest | `pytest -m "not real and not knowledge_eval"` | Config, models, session lifecycle, DB/migrations, REST/WS, harness protocols, integrations, scheduling, accounts, workspace/tenant isolation, MCP and Wiki. No CLI or network. |
| Real-CLI tier | pytest | Local marker selection above | Actual available-provider execution, recording, recovery, delegation and CLI contracts. Deselected providers are not passes. |
| Frontend unit | vitest | `cd web && bun run test` | Stores, HTTP/WS handling and components, including persistent task cards and account/service generation boundaries. |
| E2E | Playwright | `cd web && bun run test:e2e` | Real browser and server persistence. Run the offline `:fast` bucket separately from the configured-provider `:llm` bucket. |

For what any individual test covers, ask the suite rather than this table:
`pytest --collect-only -q` with the relevant marker selection. Record actual
counts and provider scope from that run's logs; counts here would become stale.

## Project Structure

### Core

- `server/` — Python backend (FastAPI)
- `server/cli.py` — CLI entry point (`serve`, `handoff`, `pull`)
- `server/database.py` — SQLite persistence layer
- `server/scheduler.py` — APScheduler-based recurring task runner
- `server/jsonl_parser.py` — Claude Code JSONL session parser
- `server/jsonl_writer.py` — JSONL writer for session export
- `server/routers/` — REST + WebSocket routers (`sessions`, `schedules`, `agents`, `applications`, `credentials`, `connectors`, `delegations`, `research`, `ws`)
- `server/fork_helpers.py` — Pure helpers for session tree-rewind (`/rewind`): git-anchor capture at turn-start, side-effect classification over parent rows, safe-revert preflight + git-stash execution. Backend-agnostic and side-effect-contained.
- `server/research/` — Native deep research orchestration (`docs/plans/native-deep-research.md`): `ResearchManager` (async job lifecycle, phase pipeline, concurrency cap, cancel + reap), `orchestrator` (scope → search → dedup → verify → synthesize phases), `leaf` (throwaway `HarnessRun` sub-turns for web-search leaves and `run_oneshot` for reasoning leaves), `schemas` (JSON schemas for scope/findings/synthesis). Agent-invoked via `mcp__research__deep_research`; user-invoked via `/research <question>`. Result injected as a follow-up turn; a `ResearchCard` tracks progress in the UI.
- `server/mcp_servers/research.py` — MCP namespace exposing `mcp__research__deep_research(question)` to agents; thin HTTP shim to `/api/sessions/{sid}/research`. Returns `research_id` immediately so the model's turn ends cleanly.
- `docs/plans/token-rotation.md` — `OCTOPUS_AUTH_TOKEN` is both the
  credential clients send and the key `crypto.py` derives to encrypt every
  stored secret, so changing it is one server-side operation (`POST
  /api/auth/rotate`): re-key the secrets, rewrite the env files, swap the live
  setting, drop the processes carrying the old one, and hand the new token to
  the clients already holding the old one. No restart, nothing to edit by hand.
- `docs/plans/scheduled-runs.md` — What a scheduled fire is: the overlap
  guard that stops a slow schedule stacking runs, the `[scheduled:…]` marker
  that tells an agent nobody is waiting to answer questions, and
  `last_run_session_id` linking "last run" to the session it happened in.
- `docs/plans/native-subagents.md` — Native sub-agents, surfaced: Claude
  Code's `Task`/`Agent` runs and Codex's `spawn_agent`/`wait` collaboration
  calls normalized onto one `SubagentUpdate`, shown as a live card keyed on
  the spawning tool call's id (never its name — the CLIs rename it), and
  `agents.subagents` → `--agents` so an Octopus agent brings its own helpers.
- `server/app_agent.py` — The conversations a *running* application holds with an agent (`docs/plans/app-agent-access.md`): `/apps/{id}/agent/{agents,conversations,ask,chat}`, mounted under the app's own path so its page reaches them at `agent/…` relative to itself and its backend reaches them with the scoped `OCTOPUS_APP_TOKEN`. A conversation is a real session (`origin='app'`, `sessions.app_id`), so it resumes across turns and can be read in the chat view; the manager registers a turn on the broadcast bus *before* starting it and converts session events into the vocabulary an app consumes (`delta`, `message`, `tool`, `question`, `done`, `error`), streamed as SSE by `chat` or awaited whole by `ask`.
- `server/app_backends.py` — Supervised backend processes for Applications (`docs/plans/application-backends.md`): an app declares a backend with an executable `start.sh` at its root (optionally `install.sh`), Octopus allocates a port, runs them with a minimal environment, waits for the port to accept, proxies `/apps/{id}/api/*` to it, and stops it when idle. Three directories with three owners: `<slug>/` code (rewritten by a rebuild), `<slug>.data/` the app's own state (never touched), `<slug>.runtime/` installed dependencies (deletable to force a clean reinstall).
### Applications

- `server/applications.py` — Applications (`docs/plans/applications.md`): agent-built static web apps. Owns the app directory under `~/.octopus/applications`, the *build session* (a normal session with `origin='application'` whose working dir is the app dir), and a `building|ready|failed` status **derived** from whether the entrypoint exists when a build turn ends — the manager subscribes to the SessionManager broadcast bus (the DelegationManager pattern), so a change typed straight into the build session updates the badge too. `POST /{id}/build` runs another turn in that same session. Archiving
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

- `server/delegations.py` — Agent-to-agent delegation manager (`docs/plans/agent-collaboration.md`). Subscribes to the SessionManager broadcast bus; on a tracked child session's `assistant_text` / `result` / `error` / `question_request` events, captures + finalises and injects an `[agent-reply:<name> delegation=<id>]` (or `agent-question`, or `agent-error`) follow-up turn into the parent session via the same `start_message` path bg-task delivery uses. Cycle and depth-3 guards walk `parent_session_id`. `answer_pending_question(delegation_id, choice)` drains the child's oldest pending question on the parent's behalf — same Event-signal machinery the human UI uses (first to drain wins). The delegation id IS the child session id; no parallel id space, no new persistence table.
- `server/mcp_servers/ask_agent.py` — MCP namespace exposing the four delegation tools to the model: `mcp__ask_agent__ask` / `cancel` / `answer` / `list` (the Python functions are `ask_agent` / `cancel_agent_task` / `answer_agent_question` / `list_agent_tasks`; the `@mcp.tool(name=…)` decorators expose the short forms). `ask(request, name=…, delegation_id=…, files=…)` is bimodal: `name` starts a fresh child session, `delegation_id` continues a prior one in the same child transcript; exactly one id is required. Same shape as the bg + ask built-ins (the session comes from the call's verified scope — `mcp_http` / `mcp_identity`); thin HTTP shim to the `/api/sessions/{sid}/delegations` routes, including continuation via `/follow-up`. Added to the default per-agent MCP set; the migration backfills it onto every pre-existing agent row.
- `server/mcp_servers/schedule.py` — MCP namespace exposing an agent's own schedules (`docs/plans/schedule-tool.md`): `mcp__schedule__{create,list,update,delete}`. Thin HTTP shim to the session-scoped `/api/sessions/{sid}/schedules` routes, so the agent is derived from the call's session and can neither see nor touch another agent's schedules. The recurrence is stated outright (`cron` / `interval_seconds` / `run_at`) rather than parsed from English — the caller is already a model, so there is no second one-shot in the loop. In the default per-agent MCP set; the startup backfill adds it to every pre-existing agent.
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
- `web/src/**/*.test.ts` — Frontend unit tests (vitest, colocated with source)
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
# Backend
.venv/bin/pytest tests/ -v              # run backend tests
.venv/bin/uvicorn server.main:app       # start server (serves web/dist/)

# Frontend
cd web && bun run test                  # run frontend unit tests
cd web && bun run build                 # typecheck + build (refreshes web/dist/)
cd web && bun dev                       # live dev server on :5173

# E2E (Playwright)
cd web && bun run test:e2e              # run e2e tests (headless)
cd web && bun run test:e2e:ui           # run e2e tests with Playwright UI
cd web && npx playwright test --reporter=list  # verbose output
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

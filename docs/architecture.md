# Octopus Architecture

**Octopus is a personal agent platform.** It turns the local **Claude Code**
and **Codex** CLIs into durable, always-on agents you reach from a browser,
or your phone. It drives the `claude` / `codex` CLIs directly via
their stream-JSON protocols — there is **no `claude-code-sdk` dependency** and
no extra per-token API cost beyond the CLI's own auth (your subscription or an
attached API key).

One install can host **multiple accounts**, each a fully isolated Octopus of
its own (see **Accounts & isolation** below); it stays a personal product —
there is no cross-account sharing. Design:
[`implemented/multi-tenancy.md`](implemented/multi-tenancy.md).

This doc describes the *current* system design. Detailed current contracts and
their reasons live in [`implemented/`](implemented/); unfinished changes live
in [`plans/`](plans/). CLI/stream-protocol research notes live in the
`*-notes.md` files. `server/db/schema.py` (`_SCHEMA`) is the source of truth for
the data model — this doc describes it conceptually rather than pasting SQL.

The [architecture contract](implemented/architecture-contract.md) owns cross-component
decisions, conventions and the paused execution scope.
Obsolete online interfaces, installation-era adoption/rotation and startup
migration chains have since been retired. Current SessionOwner, transaction,
authorization and Wiki recovery responsibilities remain the foundation.
`_SCHEMA` now declares the complete current main layout; startup creates empty
storage or requires its exact current marker. Publication owns retained-layout
verification and any finite conversion, as described in the
[current storage contract](implemented/current-storage-format.md).

## System Overview

```
             Phone / Browser
              │  REST + WebSocket
              ▼
        ┌──────────────────────────────────┐
        │  FastAPI server (uvicorn) :8000   │  serves API + built SPA on one port
        │  ┌─────────────┐                  │
        │  │ REST routers│                  │
        │  │  + /ws      │                  │
        │  └─────┬───────┘                  │
        │        │                          │
        │        ┌──────▼───────┐            │
        │        │SessionManager│            │  session entry points and quota
        │        └──────┬───────┘            │
        │        ┌──────▼───────┐            │
        │        │ SessionOwner │            │  one mutable execution owner per session
        │        └──────┬───────┘            │
        │        ┌──────▼───────┐            │
        │        │   Harness    │            │  RuntimeProfile per backend kind
        │        └──────┬───────┘            │
        └───────────────┼────────────────────┘
                        │ subprocess (stream-JSON over stdout)
            ┌───────────┴────────────┐
            │  claude --print …      │   or   codex exec --json …
            │  + injected MCP servers│        (bg · ask · connectors)
            └────────────────────────┘
                        │
            ┌───────────┴───────────┐
            │ Cloudflare Tunnel      │  (optional) public HTTPS via trycloudflare.com
            └────────────────────────┘
```

### Single-port serving

In production `octopus serve` serves everything from one port (default 8000):

- API routes (`/api/*`, `/ws`, `/health`) are FastAPI routers, registered first.
- Every other path serves the built SPA from `web/dist/` via
  `StaticFiles(html=True)` mounted at `/`.
- The frontend uses `window.location.origin` for REST and derives `ws://`/`wss://`
  from `window.location.protocol`, so it works behind any proxy, tunnel, or
  HTTPS terminator with no config.

In development, Vite serves the SPA on port 5173 with HMR and proxies `/api`,
`/ws`, `/health` to the backend on 8000.

## The agent model

An **Agent** is the durable definition of an assistant: name/avatar, system
prompt, model, default backend (`claude-code` | `codex`), an attached credential,
an optional reasoning-effort default (Codex),
its MCP/tool set, tool allow/deny policy, and enabled connectors. Agents **own**
their Sessions and Schedules. Each account starts with its own protected
**Default Agent** (`is_system=1`).

Global session and schedule creation requires an explicit, authorized
`agent_id`. Agent-scoped URLs supply that identity themselves; their request
bodies contain the creation values. A schedule's optional `session_id` selects
an execution target belonging to that agent. MCP schedules take their owner
from the authenticated current session. CLI handoff/import keeps its account
scoped selection of the protected agent.

The chat header and `/model` offer independent session overrides for model and
reasoning effort. Both inherit agent defaults when cleared, apply on the next
logical turn, and retain the conversation's resume id. Model metadata comes
from the selected runtime, with credential ownership checked before reading.
See [model and reasoning settings](implemented/model-reasoning.md).

- A **Session** is one conversation thread (an instance of talking to an agent).
  It carries the backend resume id, working dir, origin (`user` | `schedule` |
  `delegation` | `fork` | `application` | `app`), an optional `parent_session_id` (set when the
  session was spawned by another agent via `mcp__ask_agent__ask` — see
  [`implemented/agent-collaboration.md`](implemented/agent-collaboration.md)), and an
  `archived` flag.
- `SessionManager` reads the owning agent's config *directly each turn*, so
  editing an agent is picked up by its open sessions on their next turn (no
  restart, no re-bind).

## Account portability

`server.portability` provides offline `export_account` / `import_account` and
the CLI exposes `export-account` / `import-account`. Each archive contains one
account's current durable rows, files, registered application code/data and Wiki
state, with a build fingerprint and verified relative inventory. Collection
follows current ownership and registrations, so later content uses the same
entry point. `server/db/portability.py` classifies relational ownership;
`server/wiki/portability.py` preserves historical evidence and rebinds current
registration and file identity. Native model login, old conversations and
schedules are excluded. See [scope and operation](backup-and-migrate.md#account-export-and-import).

For a running managed source, the lifecycle owner waits for natural idle, seals
HTTP/WebSocket and background admission, then hands off to the offline owner.
External writers stop before this handoff; Wiki receives final events before
its observers close. The exporter releases the installation lock and restores
source readiness before compressing its stable copy. Import verifies before
writing, stages privately and publishes under that same lock. A persistent
incomplete-import guard blocks startup before key/database initialization;
removing it only enables already durable, complete data.

## Accounts & isolation

One install serves several people, each fully isolated (design:
[`implemented/multi-tenancy.md`](implemented/multi-tenancy.md)):

- **Accounts.** A `users` row is a username, a scrypt password hash, an admin
  flag, and a wrapped **data-encryption key** (DEK). Signup is invite-only
  (`invites`); a login mints a **session bearer** stored hashed in `auth_tokens`
  (30-day TTL) — that bearer, not the password, is what every later request
  carries. An empty installation uses `POST /api/auth/setup` to create its first
  administrator, wrapped DEK and owned protected agent in one SQL transaction.
  `OCTOPUS_AUTH_TOKEN` proves setup authority only; ordinary business requires
  an account or authorized MCP scope. Missing proof configuration or retained
  business data blocks setup. A lost setup response is recovered by ordinary
  login, which also completes missing account directories without replacing keys.
  Both the sidebar and Settings sign-out actions await the same device-bearer
  revocation before clearing local authentication; a network failure still
  allows local sign-out.
- **Row ownership.** Every owned table carries a `user_id` — `agents`,
  `sessions`, `applications`, `backend_credentials`, `connector_installations`,
  `notifiers` — and routers resolve the caller's id (the `ScopeUser` /
  `CurrentUser` / `AdminUser` deps in `server/deps.py`) so a query never crosses
  accounts. Schedules are owned transitively through their agent.
- **Key isolation.** A server-held **master key** wraps each account's DEK, and
  that DEK — not the access token — encrypts that account's credentials and
  connector tokens, so even direct DB access cannot read one account's secrets
  with another's key. Password changes preserve the DEK and can revoke other
  account bearers; losing the master key or wrapped DEK loses access to secrets.
- **Workspace confinement.** Each account gets a directory under `users_root`
  (`~/.octopus/users/<id>/`) holding its workspace, agent memory, applications
  and uploads (`paths_for(user_id)` in `server/workspace.py`). A session's
  working dir must resolve *inside* that workspace (a relative path means "inside
  my workspace"); admins can grant extra roots. Each account also has its own
  concurrency cap (`max_concurrent_turns_per_user`).
- **Admin.** Admins mint/revoke invites, list and disable/enable people, and the
  Monitor page is operator-only. It stays a personal product: no shared
  workspace, no cross-account delegation.

## Components

### Backend (`server/`)

| File | Purpose |
|---|---|
| `main.py` | FastAPI app + lifespan. Clears the `CLAUDECODE` env var so a nested `claude` subprocess behaves normally. Wires DB, SessionManager, ScheduleRunner, ConnectorManager, AgentManager, the in-process MCP namespaces, the metrics store, optional CloudflareTunnel. Registers routers, `GET /api/backends`, `GET /health`, and the SPA static mount. |
| `config.py` | Pydantic settings from `.env` (prefix `OCTOPUS_`) — see **Configuration** below. |
| `auth.py` / `users.py` | Accounts: empty-installation setup → first admin and owned default agent, login → session bearer, invite registration, password change and admin operations. Setup proof is checked only at `/api/auth/setup`; `deps.py` resolves the current business identity and owner. Startup refuses retained pre-account business inputs. |
| `crypto.py` | Encryption at rest: a server-held **master key** wraps each account's data-encryption key (DEK), which Fernet-encrypts that account's secrets. A password only authenticates. |
| `models.py` | Pydantic request/response models + enums (`SessionStatus`, `MessageRole`, agent/schedule/connector/credential DTOs). |
| `session_manager.py` | Session entry points, registration and cross-session quota; delegates mutable per-session behavior to SessionOwner. |
| `sessions/` | SessionOwner serializes mutable execution, input, question and lifecycle state; managed Harness I/O reports original results. Completion consumers follow [the execution contract](implemented/architecture-contract.md#输入完成与资源退休): business outcome, notification delivery and retirement have separate authorities. |
| `harness/` | The single boundary for all model/runtime interaction (see below). Also normalizes **native sub-agents** ([`implemented/native-subagents.md`](implemented/native-subagents.md)): Claude Code's `system/task_*` events and Codex's `collab_tool_call` items collapse onto one `SubagentUpdate`, keyed on the spawning tool call's id, and `agents.subagents` renders to `--agents` so an Octopus agent can bring its own helpers. |
| `agent_manager.py` | Agent CRUD (the durable assistant definitions). |
| `agent_memory.py` | Per-agent native memory provisioning under the owning account's `agents/<id>/memory/`. |
| `delegations.py` | Named-agent delegation and continuation by `delegation_id` ([design](implemented/agent-collaboration.md)). The original request's completion determines each round's reply or error; question events relay pending questions separately. Owns cycle/depth guards, delivery identity, continuation and descendant cancellation. Completion does not by itself archive an ordinary delegation session or prove its execution has retired. |
| `scheduler.py` | `ScheduleRunner` — APScheduler runner for recurring prompts, **interval and cron**, fired per agent into fresh auto-archiving sessions. |
| `schedule_ai.py` | Natural-language `/schedule` parsing — turns "every weekday at 9am" into a cron/interval spec via the agent's own harness (backend-agnostic). |
| `database.py` / `db/` | SQLite (`aiosqlite`, WAL, FK cascade, `busy_timeout`, `synchronous=FULL`). `db/schema.py` defines current format 5; `PRAGMA user_version` is its sole format marker. Startup matches it before keys or writers; genuinely empty storage creates the complete current DDL directly. Ordinary reopening does not interpret or convert historical data. Publication verifies the full retained structure and current facts after old writers retire, before the candidate starts. Credentials and recovery relationships have one authority in `credential_secrets` and the successor's `recovery_of`. Short transactions confirm the original driver's COMMIT; uncertain storage enters fail-stop. Current input and unfinished-fork recovery remain owned by `SessionManager`. See [current storage and publication](implemented/current-storage-format.md). |
| `jsonl_parser.py` / `jsonl_writer.py` | Read/write Claude Code JSONL session files for `octopus handoff` (import) and `octopus pull` (export → local `claude --resume`). |
| `applications.py` | Agent-built applications ([design](implemented/applications.md)): account-owned directories, build sessions (`origin='application'`) and status projection. Progress can mark a build running; the original request's committed completion and entrypoint determine its final status. Direct build-session requests use the same completion observer. Archive keeps rows and files; the name index is live-only. |
| `app_backends.py` | **Application backends** ([`implemented/application-backends.md`](implemented/application-backends.md)) — an executable `start.sh` at the app root IS the declaration; no manifest. Allocates a port, runs install/start with a **minimal** environment (never the server's, so Octopus credentials cannot leak into app code), waits for the port to accept, proxies `/apps/{id}/api/*`, reaps when idle. Three directories, three owners: `<slug>/` code (a rebuild rewrites it), `<slug>.data/` the app's own state (never touched), `<slug>.runtime/` installed deps (delete to force a clean reinstall). |
| `app_agent.py` | Application↔agent conversations ([design](implemented/app-agent-access.md)): resumable app-owned sessions and scoped SSE (`delta`/`message`/`tool`/`question`/`done`/`error`). Original-request completion confirms recording, outcome and notification delivery before `done`; response completion alone does not release the execution slot. |
| `mcp_http.py` | **The MCP namespaces, served in-process** ([`plans/polish-2026-09.md`](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/polish-2026-09.md) §4 B1) — each tool namespace is mounted at `/mcp/<name>` over streamable-HTTP instead of spawned as a stdio subprocess per session. Both CLIs build the tool name from the config key, so keys are unchanged and `mcp__bg__run` stays `mcp__bg__run`; connectors mount once per *kind* while their key stays per-installation. Replaces 7 subprocesses per session (~39 MB PSS each). |
| `mcp_identity.py` | Per-session scope for those namespaces. One HTTP endpoint serves every session, so identity travels with the request: master-key signatures identify the session/account/run and each call checks its current authority. Claude passes the bearer as a header, Codex as an env var it names; that asymmetry stops at the profile boundary. |
| `bg_tasks.py` | Cross-turn background shell tasks: spawn, stream capture (bounded), idle watchdog, cancel, persistence in `bg_tasks`. |
| `large_prompts.py` | Spills any synthesized prompt over ~100 KB to a file and hands the model a small `Read` pointer (avoids `E2BIG` on argv). |
| `attachments.py` / `file_viewer.py` | Account-owned session attachments; working-dir-scoped file reads for the viewer. |
| `connector_manager.py` + `connectors/` | Connector framework (see below). |
| `credentials` (`oauth_login.py`, `oauth_providers.py`, `oauth_errors.py`, `codex_login.py`) | Stored backend credentials: API keys, OAuth logins, and Codex device-auth in-app login. |
| `tunnel.py` | `cloudflared tunnel --url` subprocess manager; parses the public URL, monitors, stops gracefully. |
| `notifiers/` | Notification destinations (webhook), pluggable. |
| `fork_helpers.py` | Pure helpers for `/rewind`: git-anchor capture at turn-start, side-effect classification over parent rows, safe-revert preflight + git-stash execution. |
| `research/` | Native deep research orchestration (see below). |
| `cli.py` | `octopus serve` (`--tunnel`), `octopus handoff`, `octopus pull`. |

### Harness layer (`server/harness/`)

The harness is the **only** place that talks to a model runtime. There is one
`Harness` class and one `HarnessRun` subprocess engine, configured by a
`RuntimeProfile` **value** per backend kind — *no per-framework subclasses*
(design: [`implemented/harness-layer.md`](implemented/harness-layer.md)).

| File | Purpose |
|---|---|
| `harness.py` | The `Harness` front door (one per backend kind). |
| `profile.py` | `RuntimeProfile` — the per-backend data record + small collaborators (argv builder, event parser, capability flags). Capabilities (e.g. native memory, premature-exit recovery) are derived from the profile. |
| `run.py` | `HarnessRun` — universal subprocess + JSONL stream engine (4 MiB line limit, graceful shutdown, PATH discovery incl. nvm). |
| `assembly.py` | Shared per-turn assembly: MCP server selection, system-prompt composition (in-app tools blurb, connectors blurb, memory blurb), working-dir absolutization. |
| `events.py` | Backend-neutral `HarnessEvent` DTOs (`text`, `thinking`, `tool_use`, `tool_result`, `question_request`, `result`, `error`, `session_started`). |
| `claude_code.py` | Claude profile — `claude --print --output-format=stream-json` argv, event normalization, JSONL transcript codec, `run_oneshot`. |
| `codex.py` | Codex profile — `codex exec --json` argv (per-turn `-c` TOML overrides for MCP), event normalization, `run_oneshot`. Runs exactly once per turn. |
| `codex_text.py` | Codex one-shot model-only worker: native authentication/configuration, subscription text HTTP with no tool dispatcher, completed-text result. The common Harness owns admission and process-group retirement; [capability contract](implemented/codex-backend.md#source-and-verification). |
| `registry.py` | `get_harness(backend)` / `available_backends()` (a kind appears only when its CLI resolves on PATH; `claude-code` is always listed). |
| `login.py` | `LoginDriver` protocol for in-app credential login flows. |

### In-app MCP servers (`server/mcp_servers/`)

Normal session turns use in-process streamable-HTTP namespaces, selected from
the saved agent configuration and the current call scope. Harness assembly
renders the configuration for each CLI; connector and Wiki tools additionally
depend on their current bindings and authority. Defaults for newly created
agents do not replace saved tool selections. Stdio construction remains for
contexts without a live session scope; it is not the normal per-turn transport.

| Server | Tool(s) | Purpose |
|---|---|---|
| `bg.py` | `mcp__bg__run` / `cancel` / `list` | Fire-and-forget shell commands that run **across turns**; the result arrives as a follow-up turn. |
| `schedule.py` | `mcp__schedule__create` / `list` / `update` / `delete` | Manage durable schedules owned by the authenticated session's agent. |
| `ask.py` | `mcp__ask__user(questions)` | Structured multiple-choice question rendered as a form in the UI; long-polls for the answer. Caller-aware: when invoked from a delegation-origin session the question is also injected as an `[agent-question:…]` turn into the parent agent's session — same pending queue, two answer producers. Replaces the old permission-prompt hack. |
| `ask_agent.py` | `mcp__ask_agent__ask` / `cancel` / `answer` / `list` | Agent-to-agent delegation. `ask(request, name=…, delegation_id=…, files=…)` is bimodal: pass `name` to spawn a fresh child session under the named agent, or pass `delegation_id` to continue a prior delegation in the same child session so the target keeps her transcript across rounds; exactly one id is required. `answer` drains a child's pending question with the parent's chosen label; `cancel` stops a delegation (cascade-cancels descendants); `list` enumerates this session's delegations. Same env-injection + bg-task-style follow-up-turn pattern as `bg`. Design: [`implemented/agent-collaboration.md`](implemented/agent-collaboration.md). |
| `connectors/github.py`, `gmail.py` | typed API tools | Built-in connector tools (see Connectors). |
| `connectors/custom.py` | generic `request(method, path, …)` | One tool for any user-defined OAuth2 API. |
| `connectors/_shared.py` | — | Token fetch+cache, 401→reconnect, 32 KB truncation. |
| `research.py` | `mcp__research__deep_research(question)` | Start a native deep-research job. Thin HTTP shim to `/api/sessions/{sid}/research`; returns `research_id` immediately so the model's turn ends cleanly. See Native Deep Research below. |

### Connector system (`server/connectors/` + `connector_manager.py`)

Connectors give an agent OAuth-authorized access to third-party APIs as MCP
tools. Enablement is **agent-scoped**; setup is **browser-only** (paste the
OAuth client id/secret in the UI — no env edit, no restart). The OAuth redirect
URI is derived from the incoming request, so it's correct behind a tunnel.
Built-ins: **GitHub**, **Gmail**. **Custom** kinds (authorize/token URLs, scopes,
PKCE, API base) are defined entirely from the browser and backed by a generic
OAuth provider + the generic `request` tool. See
[`connectors-setup.md`](connectors-setup.md) and [`implemented/connectors.md`](implemented/connectors.md).

Per-kind OAuth client creds live in `connector_oauth_clients`; installations in
`connector_installations` (+ split-secret `connector_installation_secrets`);
the agent↔installation join is `agent_connectors`; custom kinds in
`custom_connectors`. The manager handles install upsert, DB→env client-config
resolution, and server-side token refresh-on-near-expiry behind a per-install lock.

### Frontend (`web/src/`)

React 19 + TypeScript (strict) + Vite + zustand + Tailwind v4 + Radix.

| Area | Files |
|---|---|
| Shell & accounts | `App.tsx`, `SignIn.tsx` (sign-in / create-first-account), `SidebarAccount.tsx` (account row + menu), `AccountPage.tsx` (workspace roots, password, invites, people), `OctopusLogo.tsx`, `SettingsDialog.tsx` |
| Sidebar (two-level) | `SidebarAgents.tsx` (agents → their sessions), `SidebarApplications.tsx`, `SidebarManage.tsx` (Schedules / Connectors / Harness summary rows), `SidebarSectionHeader.tsx`, `SidebarEdgeToggle.tsx` (collapse), `SortablePins.tsx` (`@dnd-kit` pin reorder). Design: [`implemented/console-redesign.md`](implemented/console-redesign.md), [`implemented/sidebar-pins.md`](implemented/sidebar-pins.md) |
| Main-area pages (store `mainView`) | `AgentFormPage.tsx` (create/edit agent — prompt/model/backend/credential/tools/connectors/avatar via `EmojiPicker.tsx`), `ApplicationFormPage.tsx`, `SchedulesPage.tsx`, `ConnectorsPage.tsx`, `HarnessPage.tsx`, `MonitorPage.tsx`, `ItemLibrary.tsx` (the **All** tab — every agent/app: pin / open / archive / restore), `PageHeader.tsx` (shared breadcrumb) |
| Chat | `ChatView.tsx` (virtualized via `react-virtuoso`; Enter-to-send, Esc-interrupt, queued-message badge, per-message "Fork from here"), `MessageBubble.tsx`, `MessageTime.tsx` (hover-for-time), `SlashCommandMenu.tsx` (`/schedule`, `/remember`, `/research`, `/showme`, `/rewind`, `/fork`, `/archive`, `/model`, `/reset`), `ModelPickerDialog.tsx` (`/model`), `ForkDialog.tsx` (message picker + side-effect confirm + optional git-revert), `ArchivedSessionsDialog.tsx` |
| In-turn cards & tools | `FileViewerDialog.tsx` (`/showme`), `BgTaskChip.tsx` (bg task status), `QuestionPrompt.tsx` (ask form), `MessageBubble.tsx` (historical tool approval records), `AgentDelegationRequestCard.tsx` + `AgentDelegationEventCard.tsx` (delegation status + injected reply/question/error turns), `SubagentCard.tsx` (native sub-agents), `ResearchCard.tsx` (deep-research phase progress) |
| Backends / creds / apps | `BackendPanel.tsx` (backend pick), `CredentialPicker.tsx`, `AppIcon.tsx`, `ApplicationView.tsx` (app iframe pane) |
| State | `stores/sessionStore.ts`, `hooks/useWebSocket.ts`, `hooks/useViewportHeight.ts`; `lib/hydrateSession.ts` owns detail/bg request baselines and snapshot application, while callers retain navigation, list adoption and error policy |

## Data Flow

### Sending a message (Web UI)

```
ChatView → useWebSocket.sendMessage()
  → ws: {"type":"send_message","session_id","client_request_id","content","attachment_ids":[]}

ws.py receives → submit_client_message()
  → persist input, attachment proofs and submission identity
  → SessionManager.start_message() admits to the existing dispatcher
  → persist and return the admission receipt
  → dispatcher commits and verifies the execution claim before executor I/O
    → persist and broadcast user_message (no optimistic transcript row)
    → large_prompts.spill_if_large(...)        (only if huge)
    → broadcast status: running
    → HarnessRun spawns the agent's backend CLI with injected MCP servers
      → streams HarnessEvents: assistant_text / tool_use / tool_result /
        question_request / result
      → each event is persisted (messages) and broadcast to clients
    → broadcast status: idle
```

Admission is historical; status queries also report the current execution state
and any committed terminal. The same client identity never enqueues a second
execution. On restart, unclaimed browser inputs become withdrawn and can be
restored to the composer; claimed inputs without a provable terminal remain
unknown. Recovery never sends automatically. A manual resubmission has a new
identity linked to the withdrawn input, with current authorization and files
checked again. Verified managed startup retires only the generations captured
by input recovery, without changing their unknown outcomes or replaying them.
Admission and dispatch share that frozen retirement evidence. Current writers
remain with their original SessionOwner until the captured resources retire;
standalone startup retains conservative process-group/host-boot checks.

### `/showme` — the in-app file viewer (browser only)

`/showme <reference>` is an **explicit user gesture** for opening a file in
the in-app viewer modal. ChatView intercepts it client-side, POSTs the raw
reference to `/api/sessions/{id}/showme/resolve`, and the resolver
(`server/showme_ai.py`) runs a one-shot model call (via the session's own
harness) that sees the last few messages of the conversation, returning JSON
with either `{"path"}` or `{"message"}`. On `path`, the client opens
`FileViewerDialog` directly — no model turn appears in the chat, no MCP tool
fires. The agent is **never** instructed to open the viewer on
its own: it can't tell whether anyone is at the screen.

### Questions and historical tool records

The `mcp__ask__user` MCP tool POSTs questions to
`/api/sessions/{id}/questions`; SessionManager broadcasts a `question_request`
(the Web UI renders `QuestionPrompt`) and long-polls for the
answer, which is delivered back to the model. An unanswered question
auto-answers after `ask_user_question_timeout_seconds` so headless
scheduled sessions never wedge. Normal CLI turns bypass native tool approvals;
agents confirm sensitive actions through `ask`/connector confirms. Unsupported
WS actions receive the generic refusal; controls with command identities retain
the usual authorization, replay and conflict rules. Historical tool approval
requests remain readable from saved messages as static records, without actions
or a live approval lifecycle.

### Cross-turn background work

`mcp__bg__run` spawns a detached shell task (`bg_tasks.py`) that survives across
turns; when it exits, Octopus injects a follow-up turn carrying the captured
output (spilled to a file first if large). An idle watchdog SIGTERMs tasks that
go silent after producing output. Details: [`post-mortems/2026-05-18-bg-pipeline-hardening.md`](post-mortems/2026-05-18-bg-pipeline-hardening.md).

### Native Deep Research (`server/research/`)

`/research <question>` (or the `mcp__research__deep_research` MCP tool) starts a
multi-phase orchestrated research job managed entirely by Octopus — no dependence
on the Claude Code `/deep-research` workflow skill (which hangs inside a headless
turn). Design: [`implemented/native-deep-research.md`](implemented/native-deep-research.md).

**Pipeline phases (asyncio, all within Octopus):**

1. **Scope** — `run_oneshot` (tool-free) decomposes the question into 3–6 angles.
2. **Search + gather** — one scoped harness sub-turn per angle (parallel,
   semaphore-bounded), allowed only the harness's native web tools. The leaf
   executor (`research/leaf.py`) resolves the agent's credential, runs a throwaway
   `HarnessRun` with a minimal config (no MCP servers, no connectors, no memory dir),
   captures the final text as JSON findings, and discards the sub-turn.
3. **Dedup + rank** — pure Python over the returned `{claim, url}` findings.
4. **Verify** — top-ranked claims each get K independent web sub-turns
   (adversarial "try to refute"; majority-refute kills the claim).
5. **Synthesize** — `run_oneshot` merges survivors into a cited report.

**Concurrency and safety:** a per-job semaphore (configurable, default ~4–6) plus
a global cross-job cap (`research_max_concurrent_jobs`) bound all sub-turns. A
hard overall timeout and idle heartbeat prevent the hanging that plagued the CLI
Workflow. Cancellation reaps all in-flight `HarnessRun` subprocesses. A boot sweep
marks any `running` job `interrupted`.

**Delivery:** the report is written to `<working_dir>/research/<id>.md` and
injected into the session as a follow-up turn (`[deep-research:<id>] …report…`),
the same path bg-task delivery uses. A `ResearchCard` in the UI tracks phase
progress and exposes a cancel button.

**Backend-agnostic:** both Claude Code (`WebSearch`/`WebFetch`) and Codex
(`web_search`, enabled via `-c tools.web_search=true`) support research leaves.
A backend without web tools simply returns an "unavailable" message.

## WebSocket protocol (`/ws`)

**Client → Server**

```json
{"type":"send_message","session_id":"…","client_request_id":"…","content":"…","attachment_ids":[]}
{"type":"interrupt","session_id":"…","client_command_id":"…","request_id":"…","attempt_id":"…","generation":"…","revision":0}
{"type":"answer_question","session_id":"…","client_command_id":"…","request_id":"…","attempt_id":"…","question_id":"…","answers":[…]}
```

Input and command IDs are nonempty strings of at most 128 characters. Retry the
same input with the same ID and content; a new ID represents new input. Controls
target the current request and attempt. An idle Stop supplies explicit `null`
for both targets and the current dispatch generation/revision. Answers must cover
every question. Invalid identities receive `error`; valid identities retain their
correlated `submission` or `command_receipt`, including refusals.

**Server → Client**

```json
{"type":"user_message","session_id":"…","content":"…","attachments":[…]}
{"type":"assistant_text","session_id":"…","content":"…"}
{"type":"tool_use","session_id":"…","tool":"Bash","input":{…},"tool_use_id":"…"}
{"type":"tool_result","session_id":"…","tool_use_id":"…","output":"…","is_error":false}
{"type":"question_request","session_id":"…","request_id":"…","attempt_id":"…","question_id":"…","questions":[…]}
{"type":"submission","session_id":"…","client_request_id":"…","accepted":true,"request_id":"…"}
{"type":"command_receipt","session_id":"…","client_command_id":"…","operation":"interrupt|answer_question","accepted":true}
{"type":"result","session_id":"…","cost":0.03,"turns":2,"duration_ms":5000,"is_error":false}
{"type":"status","session_id":"…","status":"idle|running"}
{"type":"error","session_id":"…","message":"…"}
{"type":"queued|dequeued","session_id":"…"}
{"type":"session_archived|session_unarchived","session_id":"…"}
```

## REST API

Business endpoints require `Authorization: Bearer <token>` from an account or
an authorized MCP scope. Owned resources are scoped to that account; administrative
operations require the corresponding account authority. Auth state, login and invite registration have their
own admission rules; setup checks its dedicated proof on an empty installation.

Use the running FastAPI instance's `/docs` and `/openapi.json` for the exact
methods, request models and response schemas. For a checkout, follow the router
registrations in [main.py](../server/main.py) to [routers/](../server/routers/).
The schema describes that instance or revision, not whichever service happens
to be deployed elsewhere. The WebSocket protocol remains documented above.

Password changes update the verifier and revoke other account bearers while
preserving the caller's token. They do not re-wrap the account DEK; key ownership
is described under [accounts and isolation](#accounts--isolation).

## Data model

`server/db/schema.py` `_SCHEMA` is authoritative; this is the conceptual map.
SQLite, WAL with `synchronous=FULL`, foreign-key cascade. Startup accepts the
current format only; publication verifies retained data and owns any finite
conversion. Do not duplicate the schema in docs.

- **`client_submissions`** — browser input, attachment proofs, immutable admission
  receipt, execution owner/state and manual recovery provenance. Scoped through
  its owning session and deleted by the session's foreign-key cascade. The
  process-local dispatch queue is not restored or automatically replayed.

- **`users`** + **`invites`** + **`auth_tokens`** — accounts (username, scrypt
  `password_hash`, `is_admin`, wrapped `dek_wrapped`), invite codes (use count +
  TTL), and hashed session bearers (`user_id`, `kind`, `expires_at`). Every
  owned table below carries a `user_id` (`agents`, `sessions`, `applications`,
  `backend_credentials`, `connector_installations`, `notifiers`); routers scope
  queries by it via `server/deps.py`. `user_id IS NULL` is the install-era
  (pre-account) owner. Design: [`implemented/multi-tenancy.md`](implemented/multi-tenancy.md).
- **`agents`** — durable assistant definition (prompt, model, backend,
  credential, `mcp_servers`, tool allow/deny, `is_system`, `archived`). Owns the rest.
- **`sessions`** — one thread: `working_dir`, `claude_session_id` (backend
  resume id, name kept for back-compat), `agent_id`, `origin`
  (`user`|`schedule`|`delegation`|`fork`|`application`|`app`), `backend`, `credential_id`,
  `archived`. Delegation rows additionally carry `parent_session_id`
  (the caller session, `ON DELETE SET NULL` — orphaning beats
  mass-delete) and `delegation_request` (the verbatim original prompt
  for UI display) — see [delegation identity](implemented/agent-collaboration.md#1-invocation-and-identity).
  Fork rows additionally carry `forked_from_session_id` (the session that was
  branched), `fork_after_seq` (last copied message seq; the rewound message
  is at seq+1 on the parent), `fork_needs_replay` (wrap retained history into
  the first turn when replay is required), `fork_metadata` (ephemeral: prefilled prompt + first-turn note,
  cleared after first result), `fork_revert_record` (durable: git-stash outcome
  and stash ref), and `fork_status` (`initializing`|`reverting`|`ready`) for
  crash recovery — see [rewind recovery](implemented/session-rewind.md#567-crash-recovery) and
  [`implemented/session-fork.md`](implemented/session-fork.md).
- **`messages`** — ordered history per session (`role`, `type`, `content`, tool
  fields, `cost`, `attachments`). User-message rows additionally record
  `git_head` and `git_status_clean` (captured at turn-start) so the fork
  safe-revert preflight can verify the working tree was clean at the branch point.
- **`schedules`** — recurring prompts owned by an agent: `interval_seconds` **or**
  `cron`+`timezone`, `recurrence_label`, `origin_session_id`, `enabled`.
- **`backend_credentials`** + **`credential_secrets`** — credential metadata and
  its secret, stored split; the secret is Fernet-encrypted under the owning
  account's DEK; refresh/`needs_reconnect` lifecycle.
- **`connector_installations`** + **`connector_installation_secrets`**,
  **`agent_connectors`**, **`connector_oauth_clients`**, **`custom_connectors`** —
  the connector tables (see Connector system).
- **`notifiers`** — notification destinations.
- **`bg_tasks`** — cross-turn background task state (`status`, `exit_code`,
  captured stdio, timestamps).
- **`research_jobs`** — native deep-research job state: `question`, `status` (`running`|`completed`|`cancelled`|`failed`|`interrupted`), `phase`, `completed_at`, `injection_status` / `injected_at` / `delivery_error` (delivery tracked separately from completion so a job can be "done but report queued"), `report_path`, and per-phase progress counters. A boot sweep marks any `running` row `interrupted`.

## Memory

Each owned agent gets one canonical markdown dir,
`<users_root>/<user_id>/agents/<id>/memory/`, shared
by both backends (design: [`implemented/memory.md`](implemented/memory.md)):

- **Claude Code** points its auto-memory there via the
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` env var — **not** `CLAUDE_CONFIG_DIR`, so
  auth and `--resume` transcripts are untouched.
- **Codex** receives the directory in an injected `developer_instructions`
  blurb and reads/writes it with ordinary file tools. Octopus leaves Codex's
  native automatic-memory pipeline disabled; memory wiring does not relocate
  the selected credential's `CODEX_HOME`.

Memory is decoupled from both harnesses' config/auth dirs. The dir is
provisioned on agent create, kept on archive, removed on hard delete.
Common Wiki agreements and brief collaboration/checkout pointers use the same
account's existing `agents/shared-memory/`, outside any individual agent's
deletion lifecycle. Development, review and research memory stays with its
responsible role. Persona, memory
and project-document ownership and task-based reading are defined in the
[memory contract](implemented/memory.md#current-content-ownership).

## Configuration (`OCTOPUS_*`)

The complete setting names, defaults and environment-file selection live in
[Settings](../server/config.py); environment names use the `OCTOPUS_` prefix.
See [local configuration](local-wsl.md#配置和数据) for this installation's actual
paths and service binding, and [backup and migration](backup-and-migrate.md) for
the resources those settings select.

`OCTOPUS_AUTH_TOKEN` is an explicitly configured setup proof for creating the
first administrator on an empty installation; its default is empty. It is not
business API/WebSocket authentication or a data-encryption key. Accounts use
bearer tokens; the master key wraps account DEKs. Existing account data and
configuration follow the [account contract](implemented/architecture-contract.md#公开接口与账户配置).
Connector OAuth clients resolve in-app configuration before environment fallback;
their operating instructions are in [connector setup](connectors-setup.md).

## Key design decisions

Attachment and prompt-file operations carry the verified session owner through
admission, execution, retry and fork reads. Deleting a session removes its rows
and closes its HTTP access; it does not prove that native transcripts, exports
or an already authorized response have stopped using the original file paths.
Those files remain in the account directory without automatic orphan cleanup.
Descendant attachment IDs are materialized before parent-row removal; a copy
failure retains the parent and its retry entry. An in-progress fork or archive
reservation refuses deletion with 409 until that original operation finishes.

- **One harness, profiles not subclasses.** All model interaction goes through a
  single `Harness` + `HarnessRun` engine parameterized by a `RuntimeProfile`
  value per backend. Adding a backend is a new profile, not a new class tree.
- **Native CLI turns.** Octopus spawns `claude --print` / `codex exec --json`
  and parses their stream-JSON itself (`harness/run.py`). Codex tool-free
  one-shots use the dedicated text worker; native Codex still owns their
  authentication and default configuration. There is no `claude-code-sdk`
  dependency.
- **Agent-centric data model.** Sessions and schedules hang off an agent; `SessionManager` reads agent config live each turn, so edits
  take effect on the next turn without restart.
- **Single-port, same-origin.** API + SPA on one port; the client derives all
  URLs from `window.location`, so tunnels/proxies/HTTPS work with zero config.
- **`ask` MCP over permission prompts.** Structured questions are an MCP tool
  with a UI form + long-poll + auto-answer timeout — robust for headless
  scheduled sessions where no human may be watching.
- **Session branching — two commands.** `/rewind` forks a
  conversation to any prior user message; the harness layer owns the per-backend
  resume strategy (both Claude and Codex use HISTORY_REPLAY: wrap the truncated
  transcript into the first turn's user-message channel; turn 2+ resumes natively).
  `/fork [name]` duplicates the current session onto an independent full copy of
  the working directory using a native transcript copy (Claude: copy the JSONL
  and rewrite session id + cwd; Codex: copy the rollout file) so the fork resumes
  with genuine context. Both commands produce `origin='fork'` session rows with a
  `forked_from_session_id` pointer and inherit the parent's account in both
  memory and persistence; fork broadcasts are scoped to that account. Startup
  repairs older unowned forks using consistent parent/agent ownership, keeping
  transcripts and archive flags intact. The sidebar builds a disclosure tree
  from this DAG. File revert on `/rewind` uses `git stash` gated on a strict preflight (clean
  tree at branch point, matching HEAD, only agent-touched files dirty). Designs:
  [`implemented/session-rewind.md`](implemented/session-rewind.md) and
  [`implemented/session-fork.md`](implemented/session-fork.md).
- **Per-turn safety net.** Every harness turn runs with a configurable idle
  timeout (`turn_idle_timeout_seconds`, default 300 s) and an overall cap
  (`turn_max_seconds`, default 0 / disabled). Active tasks have no total duration
  limit unless the administrator enables one. The child subprocess is spawned in its
  own process group (`start_new_session=True`) so `stop()` kills the whole
  group, not just the direct child. Idle stops recover automatically up to
  `turn_idle_max_retries` times (default 2), continuing the captured conversation
  after partial output. `retrying:true` marks nonterminal errors and failed
  results so delegation/app callers keep waiting. The overall cap includes all
  attempts; exhausted recovery surfaces `turn_timeout`. Manual interruption
  cancels recovery. Design:
  [`implemented/turn-safety.md`](implemented/turn-safety.md).
- **Three-tier failed-turn disposition.** After a turn fails, the run loop
  classifies the error: (1) auth-credential rejection (401/revoked/expired) →
  flag the bound credential `needs_reconnect` and stop — re-auth won't fix
  itself; (2) transient backend error (5xx/overloaded/dropped stream) →
  bounded exponential retry (max 2, resumes from captured session id when
  output was already streamed); (3) everything else (quota/credit/billing) →
  surface as-is. Classifiers are backend-declared pattern sets in
  `RuntimeProfile`. Designs: [`implemented/harness-credential-reauth.md`](implemented/harness-credential-reauth.md)
  and [`implemented/harness-transient-retry.md`](implemented/harness-transient-retry.md).
- **Hardened bg pipeline.** Large prompts spill to a file (`E2BIG` guard),
  premature CLI exit after a tool use auto-respawns once, and an idle watchdog
  reaps silent bg tasks. See [`post-mortems/2026-05-18-bg-pipeline-hardening.md`](post-mortems/2026-05-18-bg-pipeline-hardening.md).
- **Secrets split + encrypted.** Credential/connector secrets live in dedicated
  `*_secrets` tables, Fernet-encrypted under the owning account's DEK (a
  master-wrapped per-user key), read
  only by the in-process MCP namespace at tool-call time.
- **Current declarations and explicit handoff.** Schema changes update the complete
  current declarations. Publication supplies any finite conversion and verifies
  retained facts; startup does not traverse historical migrations.

## Running

```bash
# Production (single command)
cd web && bun run build && cd ..    # build the SPA (once)
octopus serve                       # API + UI on :8000
octopus serve --tunnel              # + public HTTPS via Cloudflare Tunnel

# Development (hot reload)
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload   # backend
cd web && bun dev                                                        # frontend :5173

# CLI
octopus handoff [--session-id ID] [--name N]   # import a local Claude Code session
octopus pull SESSION_ID [--cwd DIR]            # export a session to local JSONL
```

## Tech stack

- **Backend**: Python 3.12 · FastAPI · uvicorn · pydantic-settings · aiosqlite
  (WAL) · APScheduler · cryptography (Fernet) · MCP namespaces served in-process
  (streamable-HTTP) · httpx.
- **Model runtime**: `claude` + `codex` CLI subprocesses (stream-JSON) via the
  harness layer — no `claude-code-sdk`.
- **Frontend**: React 19 · TypeScript (strict) · Vite · zustand · Tailwind v4 ·
  Radix · react-markdown · react-virtuoso.
- **Tunnel**: Cloudflare Tunnel (optional, `cloudflared`).

## Tests

Verification scope, suite commands, evidence reuse, provider selection and
release gates live in [testing.md](testing.md). The everyday development summary
is in [AGENTS.md](../AGENTS.md#verification).

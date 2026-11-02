# Octopus Architecture

**Octopus is a personal agent platform.** It turns the local **Claude Code**
and **Codex** CLIs into durable, always-on agents you reach from a browser,
or your phone. Ordinary Claude turns use the CLI's stream-JSON protocol;
ordinary Codex turns use `codex app-server` RPC. Authentication and provider
billing follow the selected credential; there is no `claude-code-sdk` dependency.

One install can host **multiple accounts** with account-scoped state and
credentials for trusted same-host execution. This is not OS isolation from
hostile tenants; known enforcement gaps remain explicit in the account contract.
There is no cross-account sharing product surface. Design:
[`implemented/multi-tenancy.md`](implemented/multi-tenancy.md).

This doc describes the *current* system design. Detailed current contracts and
their reasons live in [`implemented/`](implemented/); unfinished changes live
in [`plans/`](plans/), selected through [the roadmap](roadmap.md).
Version-bound protocol observations live in [`reference/`](reference/).
`server/db/schema.py` (`_SCHEMA`) is the source of truth for
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
                        │ checked process I/O and retirement
            ┌───────────┴────────────┐
            │  claude --print …      │   or   codex app-server (RPC)
            │  + scoped MCP tools    │        + scoped MCP tools
            └────────────────────────┘
```

An optional HTTPS tunnel sits in front of the server's browser/API port.
It is not part of model subprocess communication. Codex Research uses a
separate constrained exec leaf; tool-free text calls use a dedicated worker.
Neither is an implicit fallback for ordinary Codex conversation. The complete
runtime boundaries are owned by [Harness](implemented/harness-layer.md) and
[Codex](implemented/codex-backend.md).

### Single-port serving

In production `octopus serve` serves everything from one port (default 8000):

- API routes (`/api/*`, `/ws`, `/health`) are FastAPI routers, registered first.
- Every other path serves the built SPA from `web/dist/` via
  `StaticFiles(html=True)` mounted at `/`.
- The frontend uses `window.location.origin` for REST and derives `ws://`/`wss://`
  from `window.location.protocol`, so it works behind any proxy, tunnel, or
  HTTPS terminator with no config.
- Ordinary HTTP responses negotiate GZip above 1 KiB (compression level 5),
  reducing long conversation transfers without changing the JSON contract.
  MCP HTTP namespaces bypass compression using the same root-relative path
  matching as their mounts: even an SSE-excluding compressor can retain headers
  until the first body, while an idle MCP stream must send them immediately.
  Already encoded responses and WebSockets keep their existing transport.
- HTTP metrics and the 404 cache policy handle ASGI messages directly, without
  task queues that can lose an SSE header during cancellation. Metrics record
  first-header latency by route template and method, retaining the long-poll
  408 classification. Every 404 overrides `Cache-Control` to `no-store` so a
  missing asset cannot remain cached after publication. Other messages and
  application errors propagate unchanged.

In development, Vite serves the SPA on port 5173 with HMR and proxies `/api`,
`/ws`, `/health` to the backend on 8000.

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
| `harness/` | The single boundary for model/runtime interaction. Native helpers normalize to `SubagentUpdate`: legacy cards retain tool identity, while App Server work keeps typed request/attempt/thread/turn source independently of the display cache. Claude renders saved `agents.subagents` through `--agents`; [native helper contract](implemented/native-subagents.md). |
| `agent_manager.py` | Agent CRUD (the durable assistant definitions). |
| `agent_memory.py` | Per-agent native memory provisioning under the owning account's `agents/<id>/memory/`. |
| `delegations.py` | Named-agent delegation and continuation by `delegation_id` ([design](implemented/agent-collaboration.md)). The original request's completion determines each round's reply or error; question events relay pending questions separately. Owns cycle/depth guards, delivery identity, continuation and descendant cancellation. Completion does not by itself archive an ordinary delegation session or prove its execution has retired. |
| `scheduler.py` | `ScheduleRunner` — APScheduler runner for recurring prompts, **interval and cron**, fired per agent into fresh auto-archiving sessions. |
| `schedule_ai.py` | Natural-language `/schedule` parsing — turns "every weekday at 9am" into a cron/interval spec via the agent's own harness (backend-agnostic). |
| `database.py` / `db/` | SQLite (`aiosqlite`, WAL, FK cascade, `busy_timeout`, `synchronous=FULL`). `db/schema.py` defines current format 7; `PRAGMA user_version` is its sole format marker. Startup matches it before keys or writers; genuinely empty storage creates the complete current DDL directly. Ordinary reopening does not interpret or convert historical data. Publication verifies the full retained structure and current facts after old writers retire, before the candidate starts. Credentials and recovery relationships have one authority in `credential_secrets` and the successor's `recovery_of`. Short transactions confirm the original driver's COMMIT; uncertain storage enters fail-stop. Current input and unfinished-fork recovery remain owned by `SessionManager`. See [current storage and publication](implemented/current-storage-format.md). |
| `jsonl_parser.py` / `jsonl_writer.py` | Read/write Claude Code JSONL session files for `octopus handoff` (import) and `octopus pull` (export → local `claude --resume`). |
| `applications.py` | Agent-built applications ([design](implemented/applications.md)): account-owned directories, build sessions (`origin='application'`) and status projection. Progress can mark a build running; the original request's committed completion and entrypoint determine its final status. Direct build-session requests use the same completion observer. Archive keeps rows and files; the name index is live-only. |
| `app_backends.py` | **Application backends** ([`implemented/application-backends.md`](implemented/application-backends.md)) — an executable `start.sh` at the app root IS the declaration; no manifest. Allocates a port, runs install/start with a **minimal** environment (server credentials are not inherited; same-host code is still trusted), waits for the port to accept, proxies `/apps/{id}/api/*`, reaps when idle. Three directories, three owners: `<slug>/` code (a rebuild rewrites it), `<slug>.data/` the app's own state (never touched), `<slug>.runtime/` installed deps (delete to force a clean reinstall). |
| `app_agent.py` | Application↔agent conversations ([design](implemented/app-agent-access.md)): resumable app-owned sessions and scoped SSE (`delta`/`message`/`tool`/`question`/`done`/`error`). Original-request completion confirms recording, outcome and notification delivery before `done`; response completion alone does not release the execution slot. |
| `mcp_http.py` | **The MCP namespaces, served in-process** ([`plans/polish-2026-09.md`](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/polish-2026-09.md) §4 B1) — each tool namespace is mounted at `/mcp/<name>` over streamable-HTTP instead of spawned as a stdio subprocess per session. Both CLIs build the tool name from the config key, so keys are unchanged and `mcp__bg__run` stays `mcp__bg__run`; connectors mount once per *kind* while their key stays per-installation. Replaces 7 subprocesses per session (~39 MB PSS each). |
| `mcp_identity.py` | Per-session scope for those namespaces. One HTTP endpoint serves every session, so identity travels with the request: master-key signatures identify the session/account/run and each call checks its current authority. Claude passes the bearer as a header, Codex as an env var it names; that asymmetry stops at the profile boundary. |
| `bg_tasks.py` | Cross-turn background shell tasks: spawn, stream capture (bounded), idle watchdog, cancel, persistence in `bg_tasks`. |
| `large_prompts.py` | Legacy transport prompt spill over ~100 KB, with a small `Read` pointer to avoid argv `E2BIG`. App Server keeps its input inline in RPC; original input remains persisted. |
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
| `run.py` | `HarnessRun` — universal subprocess + JSONL stream engine, graceful shutdown and PATH discovery incl. nvm. Ordinary lines remain bounded at 4 MiB; the native image reader has a separate [bounded media allowance](implemented/codex-backend.md#native-generated-images). |
| `driver.py` | Per-run protocol operations: argv rendering, preparation, input encoding and parsing. `ArgvDriver` handles initial EOF; `ClaudeDriver` lives in `claude_code.py`. The profile owns capabilities; HarnessRun retains processes, leases, readers and retirement. |
| `assembly.py` | Shared per-turn assembly: MCP server selection, system-prompt composition (in-app tools blurb, connectors blurb, memory blurb), working-dir absolutization. |
| `events.py` | Backend-neutral `HarnessEvent` DTOs (`text`, `thinking`, `tool_use`, `tool_result`, `question_request`, `result`, `error`, `session_started`). |
| `claude_code.py` | Claude profile — `claude --print --output-format=stream-json` argv, event normalization, JSONL transcript codec, `run_oneshot`. |
| `codex.py` | Codex assembly helpers, explicit independent Research exec leaf, legacy event parser and `run_oneshot` text-worker entry. The registered ordinary profile is App Server; no exec resume/fallback entry remains. |
| `codex_app_server.py` | Ordinary G0-pinned stdio RPC driver: preparation, exact native resume, input/root correlation, bidirectional questions, native output and passive scoped measurement. Original submissions, OwnerQuestions and Owner retain input, answer and settlement authority; [scope and limits](implemented/codex-backend.md#opt-in-app-server-start-and-precise-resume). |
| `codex_items.py` | Pure native item conversion and bounded inline-image projection before event queues/storage/WS. Preserves native status and fields without borrowing exec semantics; owns no execution, recovery or persistence. |
| `native_output.py` | Bounded projection of original native child/work facts. Carries typed source and reconciles final observations after resource retirement, independently of the UI card cache; no scheduler or parent-consumption oracle. |
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
| State | `stores/sessionStore.ts`, `hooks/useWebSocket.ts`, `hooks/useViewportHeight.ts`; `lib/hydrateSession.ts` owns complete/detail/metadata reads, identity, baselines and auxiliary publication; `sessionList` owns target observation invalidation, and views own navigation and ending observation |

## The agent model

An **Agent** is the durable definition of an assistant: name/avatar, system
prompt, model, default backend (`claude-code` | `codex`), an attached credential,
an optional backend-supported reasoning-effort default,
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

## Ordinary request lifecycle

1. REST and WebSocket authenticate the account and resolve the session owner.
   An input identity, body, attachment hashes, trusted knowledge context and
   queue order commit together before admission succeeds. A lost reply is
   checked through that original identity; unknown inputs are not replayed.
2. `SessionOwner` chooses the next input and captures the complete effective
   settings under the configuration lock. Slow credential/attachment preparation
   runs outside that lock, and current authority is checked at the actual effect.
   Legal Agent-default edits can leave an old session needing explicit repair.
3. `HarnessRun` owns the original process group, checked I/O and retirement.
   Claude uses stream-JSON and can conditionally retain a compatible host;
   ordinary Codex owns one App Server host/root per attempt and later requests
   resume the captured native thread after the prior writer exits. Its separate
   Research exec leaf and tool-free worker do not replace ordinary execution.
4. The driver reports original request/attempt/native facts. Native acceptance,
   model consumption, root outcome, saved final items, notification delivery and
   actual resource exit remain distinct. Root terminal or Stop closes business
   input/tool authority immediately while the original reader may finish bounded
   collection. Stop also pauses the backlog; it does not erase effects.
5. The original Owner selects and commits one business result, retains that
   fact before updating projections/publishing, and retires the captured
   resources. Each consumer observes the same original receipt and completion.
   Recording, notification or cleanup failures do not authorize a model replay.

The complete cross-component relationships are in
[the execution contract](implemented/architecture-contract.md#输入完成与资源退休).
[Input admission](implemented/input-reliability.md) owns receipts, evidence and
explicit recovery; [model settings](implemented/model-reasoning.md) owns capture
and inheritance; [Harness](implemented/harness-layer.md) and
[Codex](implemented/codex-backend.md) own transport-specific qualifications.

### Reading the conversation

The browser keeps one Zustand display state. Session selection, reconnect,
metadata reads, BG/Research/delegation details and history pages use observations
bound to the original account, session, connection, service and read lifetime.
A failed auxiliary read does not turn a successful transcript load into a failed
input queue. A cancelled observer ends reading, not server business execution.
The [frontend contract](implemented/frontend-list-reconciliation.md) owns
snapshot/event ordering, list updates and stable history-page scroll anchors.
Selecting a session updates it in place and keeps sidebar ordering under the
user's control; [pins](implemented/sidebar-pins.md) owns visibility and archive.

### Questions and tool authority

`ask` MCP and native Codex questions join the same original Question Owner.
The complete form is validated before claim/save; answers, expiry and closure
are recorded once against the original attempt. A saved answer does not prove
native receipt or model consumption. A configured deadline and an indefinitely
waiting question are separate product choices, both retaining cancellation.
Historical tool-approval messages remain readable, without restoring the retired
approval execution path. Current signed MCP scope contains account/session,
installation, work and run lease; valid signatures do not keep a retired grant
alive. See [question and control ownership](implemented/architecture-contract.md#native-question-control).

## Auxiliary and background paths

### Cross-turn background work

BG commands, Research and named-agent delegation own independent work identities;
the parent model may finish before them. Each source owns its materials and
business result. The common `SessionManager.admit_return` operation owns original
receipt lookup, qualification, material preparation order, lawful wake-up and
input submission. A source's success does not prove that its parent received or
processed the return. Inputs dispatch one at a time in successful admission order;
Stop preserves the paused backlog, and a new manual correction does not implicitly
resume it. No automatic batching or conversion of independent returns into steer
is part of this behavior.

| Path | Domain responsibility | Current contract |
| --- | --- | --- |
| BG shell work | Persistent ownership before spawn, bounded output, deadlines, captured process-group retirement and original-result return | [Background tasks](implemented/background-tasks.md) |
| Named-agent delegation | Same-account conversation and work rounds, nested questions/relay, original completion and exact-work cancellation | [Agent collaboration](implemented/agent-collaboration.md) |
| Research | Pipeline/report completion, bounded native web leaves, captured configuration, follow-up processing and retirement | [Native Research](implemented/native-deep-research.md) |
| Scheduled run | Agent-owned definition and fire snapshot, overlap/one-shot admission and original run completion | [Scheduled runs](implemented/scheduled-runs.md), [schedule tools](implemented/schedule-tool.md) |
| Native helper | Provider-owned root/child activity and saved observations within the original resource lifetime | [Native subagents](implemented/native-subagents.md) |
| Application build | Current build session's original completion plus real entrypoint verification | [Applications](implemented/applications.md) |
| Running App→Agent conversation | Scoped per-app access, original ask/SSE/status and independent response/driver lifetimes | [Agent access](implemented/app-agent-access.md) |

Native root success does not prove every helper result was consumed. Arbitrary
post-final recursive continuation remains a [paused increment](plans/runtime-execution-scope.md),
with version-bound positive and negative evidence in [G0 observations](reference/codex-app-server-g0.md).

## Deployment and data boundaries

### Installation lifecycle

`InstallationOwner` holds the storage lock and validates resource ownership.
Managed startup additionally checks the real service owner and previous writer
retirement before recovery and producer admission. Each domain exposes its own
`drain_blockers()`; installation handoff combines them without reading private
queues or keeping a second activity ledger. Quiesce's original request receipt
proves successful sealing, while subsequent shutdown and the external service
owner prove actual exit. Partial sealing does not publish a success marker.
[Installation lifecycle](implemented/installation-lifecycle.md) owns the full
contract; [local operations](local-wsl.md) owns systemd/publication commands.

### Accounts and isolation

Agents, sessions, schedules, applications, secrets and managed workspace paths
belong to accounts. First-account creation and registration create the protected
Default Agent transactionally; ordinary startup does not backfill missing Agents
or rewrite saved configuration. Passwords authenticate, digest-backed bearers
support revocation, and a server-held master key wraps each account's DEK.

This is application-level isolation for trusted same-host execution, not an OS
sandbox against hostile tenants or the host administrator. Known scope gaps and
exceptions are explicit in [the account contract](implemented/multi-tenancy.md)
and [remaining policy/security work](plans/multi-tenancy-gaps.md), including the
current Agent DELETE gap; archive and hard deletion cannot be conflated.

### Persistent state and portability

SQLite owns accepted inputs, exact terminal association and business state;
Wiki Markdown owns knowledge content while its separate ledger owns identity,
source/effect receipts and notifications. Current declarations live in
[main schema](../server/db/schema.py) and [Wiki schema](../server/wiki/schema_ddl.py).
Startup accepts the exact current format or genuinely empty storage. Publication
owns retained-fact verification and any finite conversion after old writers exit;
there is no automatic historical migration chain or second authoritative cache.

[Storage handoff](implemented/current-storage-format.md) owns format/transaction
and new-resource portability rules. [Backup and migration](backup-and-migrate.md)
distinguishes transcript export, same-version offline account transfer and full
installation backup. A forked working copy is user data, not rebuildable cache;
[full-copy fork](implemented/session-fork.md) and [rewind](implemented/session-rewind.md)
have separate native-history, file and interruption semantics.

### Memory and instructions

Saved persona owns role and collaboration behavior; project documents own
engineering/operating contracts; current tools own invocation authority. Canonical
per-Agent Markdown memory holds stable preferences and short project pointers.
Claude points its native memory to that directory; ordinary Codex injects the
explicit directory/read-write protocol through controller instructions. Neither
moves auth/native history from its selected runtime home. Account shared memory
holds genuinely shared agreements outside one Agent's deletion lifetime.
[Memory](implemented/memory.md) and [Harness assembly](implemented/harness-layer.md#instruction-assembly)
own the details; a copyable [persona snapshot](agent-prompts.md) does not update
saved settings by itself.

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

## Verification and further reading

[The documentation index](README.md) routes by task; [the roadmap](roadmap.md)
orders unfinished choices. Current source contracts do not establish deployment.
[Testing](testing.md) owns commands, fixture isolation, evidence reuse and release
gates. Version-bound protocol observations remain in [reference](reference/);
selected causal incidents remain in [post-mortems](post-mortems/).

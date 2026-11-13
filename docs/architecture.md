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

Current contracts stay with their subject owner. The component groups below link
directly to them; [paused execution increments](plans/runtime-execution-scope.md)
retain their own dependencies and restart conditions. Current format declarations
and publication handoff belong to [storage](implemented/current-storage-format.md).

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
`/apps`, `/ws` and `/health` to the backend (`OCTOPUS_API_PORT`, default 8000).

## Components

Each group names its authority, handoff and failure boundary. Source paths are
entry points, not a second exhaustive file or API inventory.

<a id="backend-server"></a>
### HTTP, WebSocket and scoped tools

REST routers translate authenticated requests; WebSocket delivery validates the
current account and session at its last awaited read before sending a frame.
In-process HTTP MCP namespaces check current account/session/installation/work/run
authority per call. A signed retired scope grants no new effect. These entries
hand commands to domain owners rather than settle work themselves. Contract:
[account delivery and scope](implemented/multi-tenancy.md#websocket-delivery).
Source: `routers/`, `deps.py`, `mcp_http.py`, `mcp_identity.py`.

### Sessions and original requests

One `SessionOwner` serializes input, questions, execution and lifecycle intent;
`SessionManager` owns entry points, registration, cross-session quota and original
Owner failure takeover. Managed I/O reports captured request/attempt/work results.
Consumers follow the original receipt; committed outcome, notification and resource
exit remain separate. Contract: [original completion](implemented/input-reliability.md#原请求完成与失败交接).
Source: `session_manager.py`, `sessions/owner_runtime.py`, `sessions/turns.py`.

<a id="harness-layer-serverharness"></a>
### Harness and native protocols

`Harness` is the model-runtime boundary. `RuntimeProfile` declares capabilities
and supplies a per-run driver; drivers interpret wire facts while `HarnessRun`
owns processes, leases, readers, backpressure and retirement. Claude stream-JSON
can retain a compatible host; ordinary Codex has one App Server host/root per
attempt. The constrained Research exec leaf and tool-free text worker retain
their narrower entry points. Contract: [Harness](implemented/harness-layer.md).
Source: `harness/harness.py`, `harness/run.py`, `harness/profile.py`.

### SQLite and managed files

SQLite owns accepted inputs, exact terminal associations and business state.
Short transactions confirm COMMIT before success is published; uncertain storage
enters fail-stop. Format markers admit the exact current layout; publication owns
retained-data verification and finite conversion. Account file paths and their
references retain their own owners across fork and deletion. Contract:
[storage and publication](implemented/current-storage-format.md).
Source: `db/schema.py`, `db/base.py`, `workspace.py`.

### Independent work

BG, Research, named delegation and scheduler own their work identities, materials,
results and cleanup. Session return admission owns original-receipt lookup,
preparation order, qualification, lawful wake-up and input handoff. Source success
does not prove parent processing; sources retain retry and diagnostic ownership.
Contract: [public return admission](implemented/input-reliability.md#公共返回接管).
Source: `bg_tasks.py`, `research/manager.py`, `delegations.py`, `scheduler.py`.

### Shared Wiki

Registry owns registered Store construction, cold recovery and admission; History
owns file effects; jobs/grants and Worker coordinate authorized attempts and
settlement. Markdown is content authority; the ledger preserves identity,
sources, receipts and outbox facts. Root success, cancellation and index readiness
cannot substitute for each other's evidence. Contract:
[Wiki engineering tasks](implemented/wiki-development-summary.md).
Source: `wiki/registry.py`, `wiki/history.py`, `wiki/job_execution.py`.

### Applications

ApplicationManager projects a build's original completion plus actual entrypoint
to status. Entrypoint PATCH uses the same terminal projection checks for active
inputs, original completion and preparation ownership; saving metadata alone
cannot end an active build. See the [application lifecycle contract](implemented/applications.md#4-lifecycle).
AppAgent owns scoped
conversations, original ask/SSE/status and request slots. BackendSupervisor owns process instances, active uses and confirmed cleanup;
code, durable data and rebuildable runtime have different lifetimes. Failed
retirement preserves files, identity and deletion responsibility. Contract:
[applications](implemented/applications.md).
Source: `applications.py`, `app_agent.py`, `app_backends.py`.

<a id="in-app-mcp-servers-servermcp_servers"></a>
<a id="connector-system-serverconnectors--connector_managerpy"></a>
### Accounts, Agent configuration and providers

UserManager and account DEKs own authentication and secrets. AgentManager owns
saved defaults; SessionOwner captures effective request settings under a short
configuration lock. Slow preparation releases that lock and rechecks authority
at effects. ConnectorManager owns each installation's refresh/binding lifecycle;
cached credentials do not bypass account or installation checks. Contract:
[configuration capture](implemented/model-reasoning.md#saving-defaults-and-repairing-an-existing-session),
with [Connector](implemented/connectors.md) owning external grant details.
Source: `users.py`, `agent_manager.py`, `sessions/configuration.py`, `connector_manager.py`.

<a id="frontend-websrc"></a>
### Frontend projections

React views consume one Zustand display state. Domain list/read modules own HTTP
observations, sparse WS events, write receipts and order reconciliation. They bind
results to account, session, connection, service generation and read lifetime;
cancelling a read does not cancel server work. Views own navigation and ending
observation. Contract: [frontend task navigation](implemented/frontend-list-reconciliation.md).
Source: `web/src/stores/sessionStore.ts`, `web/src/lib/hydrateSession.ts`,
`web/src/lib/sessionList.ts`, `web/src/hooks/useWebSocket.ts`.

### Installation and service lifetime

InstallationOwner holds exclusive storage ownership; managed supervisor admission
requires old-writer exit and current recovery. Domains expose their own drain
blockers. Installation seals those facts and issues the original handoff receipt;
shutdown and the external service owner still confirm actual exit. Failed or
partial sealing cannot publish success. Contract:
[installation lifecycle](implemented/installation-lifecycle.md).
Source: `service_installation.py`, `service_scope.py`, `service_health.py`.

## Ownership and failure boundaries

Commands, native I/O and notification delivery carry their original identity.
One real fact has one authority; a read projection, mailbox or resource handle
does not become another authority. Keep transaction, quota, file-lock and
receipt responsibilities distinct even when they share a name or timing.

| Fact | Authority | Consumer boundary |
| --- | --- | --- |
| Current-generation execution eligibility | Installation/service owner | Capture the actual resource; an old PID grants no stop authority. |
| Input acceptance, order and claim | Database transaction + original SessionOwner | UI/queues read projections; unknown is not unaccepted. |
| Business outcome | Exact committed terminal and input association | Notification, parent processing and actual exit require their own proof. |
| Wiki file effects | History prepared/applied and operation receipts | A model answer or cancelled waiter cannot prove effects. |
| Saved configuration and secrets | Account/Agent/credential owners | Startup and updates preserve user choices; current authority still applies. |

Ordinary conflict, cancellation, authentication rejection and notification loss
remain local business outcomes. Authoritative storage/COMMIT uncertainty uses
[the shared fail-stop boundary](implemented/current-storage-format.md#事务与失信边界);
the process does not independently reread, repair or replay work in that generation.

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
   An eligible compact is selected from that same queue after current work and
   its resources retire, ahead of ordinary backlog without changing its order.
   Previously admitted steer retains delivery eligibility; later inputs queue.
   Pending-only cancellation cannot interrupt a claimed operation. See
   [compaction inputs](implemented/input-reliability.md#native-compaction-requests).
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

[Original completion](implemented/input-reliability.md#原请求完成与失败交接) owns
consumer settlement and failure takeover. [Input admission](implemented/input-reliability.md) owns receipts, evidence and
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
native receipt or model consumption. The original work carries interaction
policy through children and returns. Human native blocking can wait without a
question deadline while retaining Stop, execution termination and whole-turn
limits; unattended work records missing decisions without a long human form.
Async MCP cancellation retains its original creation/cleanup responsibility
until the question is settled and actual waiters are released.
Historical tool-approval messages remain readable, without restoring the retired
approval execution path. Current signed MCP scope contains account/session,
installation, work and run lease; valid signatures do not keep a retired grant
alive. See [question and control ownership](implemented/codex-backend.md#native-question-control).

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
Unattended question-solving returns have one opportunity: Stop expires an
unstarted solver and its original receipt, without preserving it for Resume.
The [delegation contract](implemented/agent-collaboration.md#unattended-questions)
owns that question-bound lifetime.

| Path | Domain responsibility | Current contract |
| --- | --- | --- |
| BG shell work | Persistent ownership before spawn, bounded output, deadlines, captured process-group retirement and original-result return | [Background tasks](implemented/background-tasks.md) |
| Named-agent delegation | Same-account conversation and work rounds, nested questions/relay, original completion and exact-work cancellation | [Agent collaboration](implemented/agent-collaboration.md) |
| Research | Pipeline/report completion, bounded native web leaves, captured configuration (effort/tier forwarding is incomplete), follow-up processing and retirement | [Native Research](implemented/native-deep-research.md) |
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
contract; [local publication](local-wsl.md#自动发布) owns systemd/publication commands.

### Accounts and isolation

Agents, sessions, schedules, applications, secrets and managed workspace paths
belong to accounts. First-account creation and registration create the protected
Default Agent transactionally; ordinary startup does not backfill missing Agents
or rewrite saved configuration. Passwords authenticate, digest-backed bearers
support revocation, and a server-held master key wraps each account's DEK.

This is application-level isolation for trusted same-host execution, not an OS
sandbox against hostile tenants or the host administrator. Known scope gaps and
exceptions are explicit in [the account contract](implemented/multi-tenancy.md)
and [remaining policy/security work](plans/multi-tenancy-gaps.md), and archive and hard deletion cannot be conflated.

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
own the details. Persona changes take effect through
[saved Agent settings](user-guide.md#保存-one-line-role-和-system-prompt),
not documentation examples.

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
configuration follow the [account contract](implemented/multi-tenancy.md).
Connector OAuth clients resolve in-app configuration before environment fallback;
their operating instructions are in [connector setup](connectors-setup.md).

## Verification and further reading

[The documentation index](README.md) routes by task; [the roadmap](roadmap.md)
orders unfinished choices. Current source contracts do not establish deployment.
[Testing](testing.md) owns commands, fixture isolation, evidence reuse and release
gates. Version-bound protocol observations remain in [reference](reference/);
selected causal incidents remain in [post-mortems](post-mortems/).

## Design decisions and evidence

The current design deepens existing domain owners where real consumers already
share an operation: original-return admission, resource observation and settlement,
configuration capture, runtime I/O, Wiki protected operations, frontend reads and
installation sealing. Each keeps its distinct commit points and failure scope;
multiple collections or locks alone do not justify a unified task bus or new
session authority. Add a mechanism only for a demonstrated requirement that the
current owner cannot satisfy; this is not a permanent technology blacklist.

The [earlier decision comparison](https://github.com/Danuvius152/Octopus/blob/5899c51d8f3b8fd513084e768e089c4a53f6af1a/docs/implemented/architecture-contract.md#既有领域接口的取舍)
retains the original alternatives and costs. The
[Owner scope incident](post-mortems/session-owner-scope-creep.md) and
[account setup retirement](post-mortems/account-setup-retirement.md) retain causal
evidence. Budget coordination rationale belongs to [turn safety](implemented/turn-safety.md#预算协调的归属).
These records establish their recorded revision and scope, not current deployment.

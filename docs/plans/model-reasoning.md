# Model, reasoning and speed settings

> **Status:** shipped — Independent model, reasoning and Normal/Fast speed controls with runtime discovery and preserved conversation history.

The chat header opens the same dialog as `/model`. A model override and a
reasoning-effort override are independent: editing only effort sends only
`reasoning_effort`, so the model binding stays intact. The agent form sets
durable defaults. Clearing either session field restores inheritance for that
field alone.

Codex also offers a separate Speed selector: Normal and Fast. Agent speed
defaults to Normal; sessions inherit the agent's choice and can override it
independently. Fast increases usage. Octopus does not offer Ultrafast.

`POST /api/agents`, `PATCH /api/agents/{id}` and
`PATCH /api/sessions/{id}` accept `service_tier: "fast"` to opt in or
`service_tier: "default"` for Normal. Omitting the field from a PATCH keeps its
binding; passing `null` clears it. A cleared session inherits its agent,
and a cleared agent uses Normal. Speed never changes the model or effort.

## Persistence and precedence

`agents.reasoning_effort` and `sessions.reasoning_effort` are nullable TEXT
columns. Existing rows remain NULL. Each field resolves independently as
session override → agent default → CLI configuration. Session model, effort
and credential bindings can be cleared to NULL and survive a restart.
`service_tier` is a separate nullable TEXT column on agents and sessions;
its precedence is session override → agent default → Normal, regardless of
Fast preferences saved in the CLI. Existing rows therefore use Normal.
Archived detail, restore, archive replacement, rewind and full-copy forks
preserve the settings along with their existing ownership and resume state.

One logical turn snapshots its configuration. A change saved during a turn
applies to the next turn, including if the current turn automatically retries.
A held process's spawn signature includes effort so it is replaced when the
next turn needs different settings. The backend resume id is retained.
The spawn signature also includes speed.

## Runtime metadata and validation

`GET /api/backends/{backend}/models` requires authentication and account scope.
An optional `credential_id` must belong to the caller and match the backend.
The harness reads only model metadata from the effective credential's runtime
home, using the same fallback to the host login as an actual invocation.

Codex reads its own `models_cache.json` and the model/effort defaults from
`config.toml`; the response exposes no file paths, tokens or arbitrary config
values. Missing or damaged caches leave free-form model selection and basic
reasoning levels available. The advertised levels for a known model constrain
the picker and explicit effort updates. Model names remain free-form so a new
model does not require an Octopus release. Claude Code retains its existing
model shortlist and does not expose a reasoning-effort control.

The model catalog includes `service_tiers` and `default_service_tier`.
Codex maps its advertised `priority` tier to Fast and also understands the
older `additional_speed_tiers` catalog field. Known models only offer Fast
when the cached catalog advertises it. Missing caches retain Normal/Fast for
free-form models, and the runtime remains the authority on actual access.
Claude Code does not expose speed controls. Invalid tiers and unavailable
model/tier combinations are rejected before changing any bindings.
Hidden cached models also constrain validation without appearing in the picker.
Agent/session configuration updates serialize their read, validation and write
independently of running turns. An agent default change checks affected session
overrides, and invocation preflight catches availability changes after a save.

Codex renders explicit effort as `-c model_reasoning_effort="<effort>"` before
the `resume` subcommand. Model-only changes keep the effort binding; efforts
are not converted into another model. No global Codex config is rewritten.
Speed renders as `-c service_tier="fast"` or `-c service_tier="default"`
before `resume`, with the Fast feature enabled. Normal is always explicit so
resuming a thread that previously used Fast does not retain that tier.
One-shot schedule parsing and file-reference resolution use the same setting.

CLI configuration discovery describes the selected runtime's user config;
workspace-specific CLI overrides can still affect settings inherited from the
CLI. Explicit Octopus overrides take command-line precedence. A cached catalog
is the client's last advertised list, not a new availability check.

## Verification

Backend coverage checks independent inheritance, validation, metadata redaction,
credential ownership, persistence, archived restore and fork inheritance. A
recovery test changes effort mid-turn and checks that retries retain the old
setting. Frontend tests cover effort-only requests, model-specific choices,
rejection messages and concurrent store updates. Browser coverage changes
effort through the visible header and reloads an existing transcript. The live
Codex resume test changes low → high and recalls the original word from the
same backend thread.
Speed coverage verifies Normal defaults, metadata compatibility, independent
overrides, validation, persistence, archive replacement, forks and retry
snapshots. Browser coverage toggles Normal → Fast → Normal and reloads the
original history. The live Codex test follows the same three speed settings
while recalling the original word from the same backend thread.
The CLI's applied-thread-settings records verify Fast (`priority`) followed by
Normal (`default`). Concurrent PATCH, fork/reload and stale-form regressions
cover the independent settings bindings.

Local delivery on 2026-10-11 passed all eight ordinary check gates (1,360
backend tests and 306 frontend tests), 69 browser regressions, nine real Codex
tests and the Codex browser conversation. The running service was restarted
before the production frontend build. Deployment checks verified the live
model catalog and compiled controls, unchanged resume state, all 2,725 existing
message rows, and restored visibility of the previously unowned fork with its
48 messages. A database and frontend backup was taken before restarting.

Reference: [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Fast behavior: [OpenAI Docs: Speed](https://learn.chatgpt.com/docs/agent-configuration/speed).

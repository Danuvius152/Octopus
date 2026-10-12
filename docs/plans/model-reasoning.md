# Model and reasoning settings

> **Status:** shipped — Independent agent and session reasoning controls with runtime model discovery and preserved conversation history.

The chat header opens the same dialog as `/model`. A model override and a
reasoning-effort override are independent: editing only effort sends only
`reasoning_effort`, so the model binding stays intact. The agent form sets
durable defaults. Clearing either session field restores inheritance for that
field alone.

## Persistence and precedence

`agents.reasoning_effort` and `sessions.reasoning_effort` are nullable TEXT
columns. Existing rows remain NULL. Each field resolves independently as
session override → agent default → CLI configuration. Session model, effort
and credential bindings can be cleared to NULL and survive a restart.
Archived detail, restore, archive replacement, rewind and full-copy forks
preserve the settings along with their existing ownership and resume state.

One logical turn snapshots its configuration. A change saved during a turn
applies to the next turn, including if the current turn automatically retries.
A held process's spawn signature includes effort so it is replaced when the
next turn needs different settings. The backend resume id is retained.

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

Codex renders explicit effort as `-c model_reasoning_effort="<effort>"` before
the `resume` subcommand. Model-only changes keep the effort binding; efforts
are not converted into another model. No global Codex config is rewritten.

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

Local delivery on 2026-10-11 passed all eight ordinary check gates (1,360
backend tests and 306 frontend tests), 69 browser regressions, nine real Codex
tests and the Codex browser conversation. The running service was restarted
before the production frontend build. Deployment checks verified the live
model catalog and compiled controls, unchanged resume state, all 2,725 existing
message rows, and restored visibility of the previously unowned fork with its
48 messages. A database and frontend backup was taken before restarting.

Reference: [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

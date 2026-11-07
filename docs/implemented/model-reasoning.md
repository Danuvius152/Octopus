# 模型、推理强度与速度设置

> **Status:** implemented — 独立模型、推理强度和 Normal/Fast 控制，保留继承、运行时发现与会话历史。

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

One logical request captures its session overrides and Agent defaults together
through its original Owner when preparation acquires the configuration lock. Queued input and preparation
still waiting for that lock have not captured settings: changes saved before
capture apply to that request. Later changes apply to a subsequent request;
retries keep the original capture. A held process's spawn signature includes
effort and speed, so incompatible settings replace the process while retaining
the backend resume id.

## Saving defaults and repairing an existing session

Saving an Agent validates the complete candidate against its own backend,
credential and model controls. A legal default change can make an existing
session's inherited settings temporarily incompatible; that does not veto the
save. The session keeps its original backend, history and native resume identity.
Overrides are not automatically added or converted to preserve compatibility.

The chat header and `/model` show the selected session's effective settings and
each field's source. The dialog can change credential and model controls in one
atomic PATCH, including when both an inherited credential and effort are
incompatible. Clearing a credential restores inheritance. Its draft catalog
describes the draft credential; it does not replace the current effective-value
display. Rejections preserve existing bindings and explain what to correct.
Responses remain associated with their original session, so switching sessions
cannot move an error or close the new session's dialog.

| Boundary | Validation and timing |
| --- | --- |
| Agent save | Serialize candidate read, complete validation and write; validate the new defaults themselves. |
| Session save | Validate the complete proposed effective combination using the session's retained backend; reject atomically. |
| Logical request capture | Capture the effective configuration under the configuration lock; retries keep that capture. |
| Process launch | Recheck supported controls and current credential authorization before invoking the captured backend. |

`AgentManager` owns candidate validation and default saves. The original SessionOwner
owns override commits and configuration capture; the existing session module
coordinates committed credential-deletion propagation and
request capture in [configuration.py](../../server/sessions/configuration.py).
These operations share the database instance's short configuration lock;
routes translate requests and errors without composing locks or live-field
refreshes. Effective field precedence stays in `effective_settings`.
Credential refresh, connector loading and other slow preparation happen after
capture releases the lock. Revoking a credential can still prevent spawn or
any later frame write; capture grants no continuing authorization.

Research rechecks the original live Session identity and admission after capture
returns, before registering a job. A session deleted or archived while that
result was pending cannot admit research from its retained configuration copy.

Context-affecting override changes commit their invalidation with the original
configuration transaction. Agent default/credential changes advance their
comparison revision in the original transaction; committed notifications only
wake an Owner. Context snapshots and samples independently compare those facts,
including A→B→A changes. The complete binding and observation rules are owned by
[the context contract](architecture-contract.md#上下文样本与配置绑定).

`resolve_credential_by_id` in [credentials.py](../../server/sessions/credentials.py)
already serves ordinary sessions, Research, files and Agent operations. It owns
the account/credential refresh task, shields it from an individual waiter's
cancellation, conditionally commits refreshed secrets and rechecks authorization
after waiting. Manager/database shutdown drains those original tasks. An explicit
invalid binding never falls back to the host login.

Keep these two narrow boundaries: configuration capture owns request values;
credential resolution owns secret freshness and current authority. A combined
configuration/credential service would move initialization and drain obligations
without removing a rule from its existing consumers. Add new settings through
the existing precedence, validation and capture paths.

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
model does not require an Octopus release. Claude Code advertises the request
levels low, medium, high, xhigh and max for its aliases and free-form models.
These are CLI request levels, not a guarantee of distinct model capabilities.
Claude defaults remain unspecified so clearing both overrides follows the CLI.

The model catalog includes `service_tiers` and `default_service_tier`.
Codex maps its advertised `priority` tier to Fast and also understands the
older `additional_speed_tiers` catalog field. Known models only offer Fast
when the cached catalog advertises it. Missing caches retain Normal/Fast for
free-form models, and the runtime remains the authority on actual access.
Claude Code does not expose speed controls. Invalid tiers and unavailable
model/tier combinations are rejected before changing any bindings.
Hidden cached models also constrain validation without appearing in the picker.
Agent saves, session saves and execution share the same pure model-control
validation. Empty supported-control lists do not authorize explicit values.
Free-form model names preserve backend capability checks; invocation preflight
catches incompatible inherited settings and changes in availability after a save.

Ordinary Codex applies model, reasoning effort and explicit service tier during
native thread start/resume and verifies the returned effective settings before
business input. Fast maps to native `priority`; Normal is explicit `default`,
so a resumed thread cannot retain its previous Fast setting. Model-only changes
keep the effort binding; effort is not converted into another model. The
independent Research exec leaf renders `-m` and TOML effort/tier overrides;
one-shot schedule parsing and file-reference resolution pass the same selected
settings to the text worker. No global Codex config or saved agent row is rewritten.

Claude Code renders explicit effort as `--effort` and sets
`CLAUDE_CODE_EFFORT_LEVEL` in the child environment to the same value: that
environment variable takes precedence over the CLI flag. When both overrides
are empty, neither is changed. Turn and one-shot calls share this rule; host
configuration is never rewritten. The existing process replacement mechanism
applies changes on the next turn while preserving the resume id.
DeepSeek maps low to low, medium/high/xhigh to high, and max to max; Octopus
passes the selected request level through without provider-specific conversion.
That provider mapping inside the Claude runtime is not a registered DeepSeek
Harness backend. The separate backend and capability investigation remain in
[the DeepSeek plan](../plans/deepseek-harness-backend.md).
See [Claude effort precedence](https://code.claude.com/docs/en/model-config#adjust-effort-level)
and [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/).

CLI configuration discovery describes the selected runtime's user config;
workspace-specific CLI overrides can still affect settings inherited from the
CLI. Explicit Octopus overrides take command-line precedence. A cached catalog
is the client's last advertised list, not a new availability check.

## Verification

Backend coverage checks independent inheritance, validation, metadata redaction,
credential ownership, persistence, archived restore and fork inheritance. A
configuration regression orders saves at the actual lock-acquisition boundary;
queued, captured and retried requests use the expected complete settings.
Spawn and initial-frame regressions revoke the captured credential before its
actual effect. Frontend tests cover effort-only requests, model-specific choices,
rejection messages, combined credential/effort repair and concurrent store
updates. Browser coverage changes effort through the visible header and reloads
an existing transcript. This installation's routine Codex live checks use low
effort; backend-specific parameter and retry variations retain hermetic coverage.
Speed coverage verifies Normal defaults, metadata compatibility, independent
overrides, validation, persistence, archive replacement, forks and retry
snapshots. Browser coverage toggles Normal → Fast → Normal and reloads the
original history. The live Codex test follows the same three speed settings
while recalling the original word from the same backend thread.
The CLI's applied-thread-settings records verify Fast (`priority`) followed by
Normal (`default`). Concurrent PATCH, fork/reload and stale-form regressions
cover the independent settings bindings.

Claude coverage uses its production renderer/parser with a hermetic STREAM_JSON
subprocess. It verifies low → high → cleared session → cleared agent settings,
child environment precedence, process replacement, a stable resume id and
preserved history. Agent/session UI tests cover saving Claude effort independently
of model selection. This coverage does not establish live Claude or DeepSeek
model behavior; this installation's live release checks remain Codex-only.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/model-reasoning.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.

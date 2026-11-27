# Agent 配置与所属会话

> **Status:** implemented — 账户所属 Agent 配置、会话继承与归档删除生命周期。

## Ownership and configuration

An Agent is the durable definition: name, persona, backend, model, reasoning,
speed, credential binding, built-in MCP selection, tool policy and native
subagent definitions. Sessions are conversations owned by that agent; schedules,
connector enablement and the agent's Markdown memory also use that identity.
This keeps configuration and continuity independent of any one conversation.

Configuration is a live reference. A saved agent change affects requests that
have not yet captured their settings during preparation. A captured request
keeps that configuration through retries. Session model, effort, speed and credential
overrides resolve independently; clearing one restores that field's inheritance.
The complete precedence and validation rules belong to
[model settings](model-reasoning.md). Working directory is a session property,
not an Agent setting, and is captured as an absolute permitted path at creation.

A valid Agent default change is saved even when an older session's inherited
combination becomes incompatible. That session retains its backend and history;
the user repairs its effective settings before continuing. Session updates and
execution validate the whole effective combination, including current credential
authorization. Saving defaults does not silently migrate old conversations.

Defaults apply to new agents. Startup, upgrades and account transfer preserve
saved persona, model and tool choices. Path rebinding does not authorize rewriting
configuration. Existing settings are validated at their owning update and
invocation boundaries; a design preference about future compatibility is not a
claim that the current API accepts every combination.

## Creation, names and lifecycle

Agent names are unique among live agents of the same account. Creating a session
requires an existing agent and validates credential ownership and backend
compatibility. The session's account is derived from that agent; the caller
does not supply an unrelated owner.

Double-clicking an Agent opens its existing form. The form groups engine and
sign-in under Conversation defaults, with Model defaults available on expansion.
Connectors, Allowed tools, Denied tools and Sub-agents retain their Agent-level
editing and validation. Session overrides and alternate engines follow the
[model and conversation settings contract](model-reasoning.md).

Archiving preserves configuration, files and history while removing the agent
from ordinary use. SessionManager coordinates owned sessions and scheduler
operations with agent lifecycle changes. Restoring checks for a live-name
conflict and restores the sidebar pin. First-account creation and invitation
registration create the account's protected Default Agent in the same transaction.
A protected `is_system` agent, if present,
cannot be archived, deleted or unpinned; ordinary startup does not recreate an
installation-era default agent or backfill user configuration.

Archive and hard deletion check account ownership before mutation. For hard
deletion, SessionManager stops admitting operations that can create sessions
for the Agent and waits for existing reservations to settle. An active or
archived session blocks deletion. SQLite checks this when claiming deletion
and again in the final delete transaction. A refusal releases the temporary
admission hold. Existing history and its input and return paths remain
available under the Agent's current lifecycle state.

The claim commits the account and Agent's cleanup responsibility before any
private files are removed. The pending Agent stays closed to new work and
restoration. Repeated requests share outstanding work and can retry a
failed cleanup or a known uncommitted final delete under the original Agent
ID. The operation continues if the HTTP waiter disconnects. Success and its
event require both [private memory cleanup](memory.md#4-plumbing) and a
confirmed final delete. If the commit acknowledgment is lost, the current
database generation stops without publishing success. After restart the
stored row determines whether cleanup can resume or the Agent is already
absent.

Archiving keeps private memory. Pinning controls visibility independently
of archive. See [sidebar behavior](sidebar-pins.md) and
[account ownership](multi-tenancy.md#business-ownership).

## Related contracts and verification

- [Memory](memory.md) owns native file memory, content routing and role boundaries.
- [Delegation](agent-collaboration.md) owns named-agent rounds and caller delivery.
- [Scheduled runs](scheduled-runs.md) owns origin-session versus disposable runs.
- [Connectors](connectors.md) owns agent-scoped installation enablement.
- [Account isolation](multi-tenancy.md) owns account paths and authorization.

Implementation: [AgentManager](../../server/agent_manager.py),
[session lifecycle](../../server/sessions/lifecycle.py),
[agent routes](../../server/routers/agents.py).
Regression entry points: `tests/test_agents_api.py`, `tests/test_agent_delete_recovery.py`,
`tests/test_agent_configuration_startup.py`, `tests/test_agent_credential_bindings.py`
and `tests/test_model_settings.py`.

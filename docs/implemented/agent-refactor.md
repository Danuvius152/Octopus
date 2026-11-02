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

Archiving preserves configuration, files and history while removing the agent
from ordinary use. SessionManager coordinates owned sessions and scheduler
operations with agent lifecycle changes. Restoring checks for a live-name
conflict and restores the sidebar pin. First-account creation and invitation
registration create the account's protected Default Agent in the same transaction.
A protected `is_system` agent, if present,
cannot be archived, deleted or unpinned; ordinary startup does not recreate an
installation-era default agent or backfill user configuration.

Hard deletion requires no remaining sessions and removes the private memory
directory. Archive keeps it. Pinning controls visibility independently of
archive; see [sidebar behavior](sidebar-pins.md).

Archive and hard deletion do not currently offer identical account enforcement.
Archive rejects a failed scoped lookup before mutation. The DELETE route has a
known static gap: a failed scoped lookup can proceed to the manager's unscoped
delete before its final assertion. Under the existing non-system/no-session
conditions, that may affect a foreign Agent and its memory when its ID is known.
The repair and no-side-effect acceptance conditions belong to
[remaining account work](../plans/multi-tenancy-gaps.md); this documentation
correction does not implement that repair.

## Related contracts and verification

- [Memory](memory.md) owns native file memory, content routing and role boundaries.
- [Delegation](agent-collaboration.md) owns named-agent rounds and caller delivery.
- [Scheduled runs](scheduled-runs.md) owns origin-session versus disposable runs.
- [Connectors](connectors.md) owns agent-scoped installation enablement.
- [Account isolation](multi-tenancy.md) owns account paths and authorization.

Implementation: [AgentManager](../../server/agent_manager.py),
[session lifecycle](../../server/sessions/lifecycle.py),
[agent routes](../../server/routers/agents.py).
Regression entry points: `tests/test_agents_api.py`,
`tests/test_agent_configuration_startup.py`, `tests/test_agent_credential_bindings.py`
and `tests/test_model_settings.py`.

The [original refactor](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/agent-refactor.md)
records the introduction of agents. Its single-user baseline, migration SQL,
bridge rollout and then-future memory/connectors are historical, not current work.

# Connectors: installations, enablement and token ownership

> **Status:** implemented — Account-owned OAuth installations, agent-scoped enablement and typed MCP tools.

## Current contract and design decisions

A connector kind describes a provider and its tools. An installation is one
account's connected external identity. `agent_connectors` enables installations
for a durable agent; each turn loads that agent's enabled installations and
renders backend-neutral MCP entries through the selected Harness. Enablement
survives conversation replacement without copying secrets into a session.

Gmail and GitHub are built-in kinds. Browser-defined custom OAuth kinds use the
generic connector and MCP module. OAuth client configuration is DB-first with
environment fallback. The browser request determines the redirect origin unless
an explicit public-base override applies. Typed MCP tools avoid proxying arbitrary
CLI traffic. The server stores credentials; model prompts carry capabilities and
tool namespaces, not provider tokens.

`ConnectorManager` owns token refresh and per-installation locking. A process-level
connector MCP instance may serve many installations, so cached tokens never bypass
current installation/account checks. Per-installation coordination and database
transactions protect different responsibilities: external refresh/binding versus
atomic installation-and-secret writes. See the
[account and MCP contract](architecture-contract.md#公开接口与账户配置).

## OAuth and lifecycle

Pending OAuth login is bounded and in memory. The fifteen-minute TTL bounds
callback admission; after a callback is claimed, its network operations have
their own deadlines. Cancellation and local binding coordinate on the same login
owner. Local binding atomically associates the external identity, installation
and secret. An external grant alone does not prove local installation succeeded.

Reconnect retains the installation identity and enabled-agent relationships.
Archive, delete, ownership and refresh outcomes are checked at their current
boundaries. The old second-pass audit's proposed provider revocation, host-login
policy and other mechanisms must not be inferred from this contract; remaining
proposals are [separately scoped](../plans/multi-tenancy-gaps.md).

## Navigation and verification

[Connector setup](../connectors-setup.md) owns operator instructions. The
[architecture overview](../architecture.md#connector-system-serverconnectors--connector_managerpy)
explains component relationships. Exact route/schema inventories come from
FastAPI/OpenAPI and the code:
[manager](../../server/connector_manager.py), [providers](../../server/connectors/),
[routes](../../server/routers/connectors.py),
[MCP modules](../../server/mcp_servers/connectors/).

Regression entry points are `tests/test_connectors_oauth.py`,
`tests/test_router_connectors.py`, `tests/test_connectors_db.py`,
`tests/test_connectors_registry.py` and `tests/test_backend_connectors.py`.
They do not prove current external-provider access.

The [original design](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/connectors.md)
contains the superseded per-session model, Notion proposal and implementation
phases. Those are historical evidence, not outstanding work or current contracts.

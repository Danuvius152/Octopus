# Connector：安装、启用与令牌归属

> **Status:** implemented — 账户所属 OAuth 安装、Agent 启用范围与类型化 MCP 工具。

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

OAuth state, expiry and single callback claim do not currently bind the callback
to the initiating browser's cookie. Disconnect deletes the local installation,
without promising to revoke the provider grant. Browser binding and revocation policy remain in
[the account plan](../plans/multi-tenancy-gaps.md), rather than being claimed as
current guarantees. Complete long-result access, sensitive-write authorization,
health checks and per-tool policy have their own
[capability plan](../plans/connector-capabilities.md).

## Token failures and delayed rejection

The refresh producer preserves HTTP status and machine error code, including
non-JSON error pages. Transport failure, 429 and 5xx are temporary; 5xx remains
temporary even with an `invalid_grant` body. Only a 400/401 `invalid_grant`
establishes a permanent grant rejection. Configuration, permission, unavailable
installation/account and unknown failures remain distinct through manager,
internal route and MCP consumer. A temporary failure does not mark authorization
invalid; a later call may retry refresh. Successful refresh preserves an omitted refresh token
and clears prior error state.

One token resolution captures access token, expiry, API base and an opaque
`grant_stamp` under the installation lock. The stamp hashes the owned installation,
authentication type, encrypted material and endpoint; it is comparison evidence,
not a token or execution grant. Gmail, GitHub and custom consumers keep that
snapshot local to each call. A resource 401 submits its stamp in a JSON rejection
request. The original DB transaction compares current account, installation and
material: `applied` marks the current grant, `obsolete` preserves a replacement,
and `gone` reports its absence/unavailability. Unconfirmed marking has its own
message; none of those latter cases falsely asks the current login to reconnect.
Only the first applied state change emits the reconnect-needed metric; every
resource rejection remains a call-failure observation.

This does not change the shared HTTP retry policy or replay a resource operation
after authorization failure. Write idempotence and unknown external effects
remain independently scoped work.

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
`tests/test_connector_grant_failures.py` additionally joins the real internal
routes, Context, three consumers and SQLite across provider HTTP failures,
authorization replacement, deletion/disable and concurrent calls sharing Context.

The [original design](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/connectors.md)
contains the superseded per-session model, Notion proposal and implementation
phases. Those are historical evidence, not outstanding work or current contracts.

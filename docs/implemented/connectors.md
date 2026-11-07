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
[account and MCP contract](multi-tenancy.md).

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

A custom definition has an opaque process-local identity owned by the manager.
Pending login retains the original connector captured before the first client
configuration await. Authorization and callback validate that identity and read
its matching client configuration in one short definition lock before contacting
the provider. A deleted definition is refused before sending a replacement's
client secret to the old endpoint. Network I/O holds no definition lock; a
definition deleted while I/O is in flight is refused again at local binding.

Definition deletion atomically removes its OAuth client and all accounts'
installations, secrets and agent enablement links. Creation, deletion, binding
and independent client set/clear operations share the short local ordering;
deletion retires the in-memory identity only after confirmed DB deletion, even
when its waiter is cancelled. Delete/recreate under the same slug gets a different
identity, so old callbacks and client writers cannot attach to the replacement.
A binding that commits first is removed by deletion, while its historical
successful login receipt remains successful. Built-ins retain their registry
identity and need no custom-definition row.

The process-local representation is sufficient because this installation has one
manager, pending logins are not persisted and account transfer is offline. It
adds no storage field or migration; it does not coordinate independent writers
in multiple server processes.

OAuth state, expiry and single callback claim do not currently bind the callback
to the initiating browser's cookie. Disconnect deletes the local installation,
without promising to revoke the provider grant. Browser binding and revocation policy remain in
[the account plan](../plans/multi-tenancy-gaps.md), rather than being claimed as
current guarantees. Complete long-result access, sensitive-write authorization,
health checks and per-tool policy have their own
[capability plan](../plans/connector-capabilities.md).

## Refresh ownership and shutdown

After each caller's current account/installation check, the manager registers a
strongly held token task before awaiting it. Same-installation callers share that
task through shielded waits; cancellation detaches a waiter while the original
task continues provider I/O, save and exception observation. Other installations
can progress independently. Each waiter independently checks installation
ownership before joining and account availability again after the result;
joining shared work grants no authority.

The original installation lock spans refresh and save, serializing reauthorization
and installation deletion. Inside it, a short definition lock captures the
installation, encrypted grant, endpoint and matching client configuration
together, then releases before network I/O. Definition deletion can proceed
during that I/O. Save checks current account and installation authority, so a late
result neither recreates a deleted installation nor replaces a new authorization.
The lock order is identity deduplication, installation, definition, then DB where
those responsibilities are needed; deletion never waits back on an installation.

Task completion observes failures with credential-redacted diagnostics and retires
the original task from the manager's set. Shutdown closes admission and waits up
to 30 seconds without cancelling an owned rotation. A timeout leaves outstanding
tasks enumerable and fails close; an unexpected save failure observed during
shutdown also fails close. The DB pre-close hook joins this responsibility before
storage closes, and installation drain includes these operations. This preserves
responses received by the current process; a provider response never received or
a hard process kill still has unknown remote effects and triggers no automatic
rotation retry.

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
[architecture overview](../architecture.md#accounts-agent-configuration-and-providers)
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
`tests/test_connector_manager.py` covers owned refresh across cancellation before
response, at the save lock and during COMMIT, sharing, shutdown and real save
failures. The previous waiter-owned path lost a received rotation when its caller
left; regressions now reopen SQLite and require the saved original grant.
`tests/test_connector_custom_lifecycle.py` and the router tests cover deletion,
rollback, binding order, same-slug replacement and stale client writers.
`tests/test_connector_definition_http.py` uses two loopback providers with different
endpoints and secrets, through the real HTTP exchange/refresh implementation:
stale callbacks send nothing, in-flight old requests use only old credentials,
and their late results cannot bind or save a replacement. These are local protocol
and persistence checks, not live OAuth grants.

The [original design](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/connectors.md)
contains the superseded per-session model, Notion proposal and implementation
phases. Those are historical evidence, not outstanding work or current contracts.

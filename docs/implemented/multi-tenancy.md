# 账户、授权与资料归属

> **Status:** implemented — 账户认证、业务作用域、账户密钥、受管路径与现行隔离边界。

## Authentication and its limits

Current business execution requires an active account. An empty installation
supports first-account creation; later registration uses invitation codes.
The old pre-account business mode, installation adoption, global access-token
rotation and its recovery route have been retired. Current account transfer is
the explicit [offline operation](../backup-and-migrate.md#account-export-and-import).

Passwords authenticate; opaque server-side tokens authorize API/WebSocket access.
Session/PAT tokens are stored by digest with revocation and applicable expiry.
Changing a password commits the new hash and revocation of other bearers in one
short database transaction, preserving the existing current-token exception.
UserManager owns password policy and hashing; the database owns this complete
operation. Cancellation, a failed revocation or reopening the database exposes
either the old state or the complete new state. No recovery queue finishes half
a password change. Disabling an account also revokes its bearers.
Login throttling bounds guessing. Opaque tokens were chosen because
revocation can be checked against the already-open SQLite authority without
adding a JWT refresh/revocation subsystem.

`UserManager.login` verifies the password outside the write transaction, then
the database inserts the session token only if the account still exists, is
enabled and has the exact password hash observed by that verification. A password
change, disable or deletion that wins this race produces an authentication
failure without a bearer; throttling clears only after successful issuance.
Setup, registration and PAT issuance retain their separate business entries.
This checks current validity, without adding a historical disable/re-enable epoch.
The route-to-SQLite barriers in `tests/test_auth_routes.py` cover these races;
`tests/test_users.py` retains revocation and current-device exceptions.

HTTP ownership, MCP invocation authority and a running subprocess have distinct
lifetimes. Revoking login is not proof that an already-running child has retired.
The [current execution contract](architecture-contract.md) owns admission and
retirement; the [remaining tenancy proposal](../plans/multi-tenancy-gaps.md) must
be checked against code before treating any old audit finding as still present.

## Keys and secrets

The server-held master key wraps one random data-encryption key per account.
Account DEKs encrypt credentials and connector tokens. Passwords do not encrypt
those secrets: a scheduled request must be able to use its account's credentials
while the person is offline, and a password change must not rewrite ciphertexts.
The server/operator can decrypt secrets it must use; this is not a promise of
cryptographic isolation from the host administrator.

`credential_secrets` is the unique stored secret authority. App tokens and MCP
signatures use their scoped master-key mechanisms; neither is the retired global
API access token. User deletion/crypto-shred is not an implemented product route.
Secret rotation, current storage and release preparation follow
[storage handoff](current-storage-format.md), not old migration SQL.

## 5. Business ownership

Agents, sessions, schedules, installations and applications carry account identity.
Routes resolve account scope, and referenced ownership/current authority belong
at each operation's actual effect boundary. A session owner is derived
from its agent. Connector enablement belongs to the agent; cross-account
delegation is refused. Broadcasts use the appropriate account/session audience.
Unscoped internal access is not permission for account-facing routes to read all
rows. Shared registries and provider definitions are installation-level by design;
users' instances and secrets are not.

Agent DELETE requires an explicit account scope in `AgentManager.delete_agent`.
Absent scope, a foreign target and a missing target all fail before row deletion,
memory cleanup or notification. The manager returns the authorized original row
for the route's account-scoped event; no post-deletion assertion establishes
authority. Protected system agents and agents with sessions remain undeletable.
The existing memory-mutation owner keeps database and directory work ordered;
this is not a database/filesystem atomic transaction. `tests/test_tenant_isolation.py`
checks both the real HTTP and business entries, retained memory and event audience;
Agent API and memory regressions cover legitimate deletion and existing guards.

Known limits remain explicit.
`schedules_changed` is a global empty invalidation notification; account-scoped
refetch remains the data boundary, and the event contains no schedule rows.

Host-login fallback applies only when there is no effective explicit credential
binding. A deleted, foreign, backend-incompatible or otherwise invalid explicit
binding is refused; it does not silently select the host's login. Captured
settings still undergo current authorization at spawn/write boundaries.

Per-account turn limits and other admission limits are enforced by the owning
execution paths. Host-default credential behavior must be read from the current
resolver; this document does not promote the old audit's proposed host-login flag
or global quota redesign to an implemented guarantee.

## 6. Files and workspace

`workspace.paths_for(user_id)` gives the account's workspace, agents, applications,
attachments, large prompts and research roots. A new session without an explicit
directory uses the account workspace. Relative paths resolve there; absolute
paths are validated against the account root and granted extra roots after
symlink resolution. Applications legitimately work in their account-owned sibling
directory, not necessarily below `workspace/` itself.

Attachment storage owns filename sanitization and the final UTF-8 byte budget.
The on-disk basename is at most 214 bytes, including the 12-character attachment
ID and `__` separator. Truncation preserves whole characters and extensions of
at most 16 characters; longer extensions follow the ordinary name budget. This
keeps the previous ASCII limit and reserves space for fork-preservation temporary
names. Invalid UTF-8 names produce an attachment error. Existing account paths,
attachment identity and download authorization retain their original owners.

This validates session directory selection. The local deployment runs trusted
agents and applications under the host user; it is not an OS sandbox against
hostile tenants or root. Per-tenant execution isolation would require a separate
design. Application code, durable data and rebuildable dependencies have distinct
[directory ownership](application-backends.md#3-three-directories-three-owners).

## Verification and remaining scope

Code: [users](../../server/users.py), [keys](../../server/crypto.py),
[request scopes](../../server/deps.py), [paths](../../server/workspace.py),
[MCP identity](../../server/mcp_identity.py) and [database](../../server/db/).
`tests/test_route_scoping.py`, `tests/test_tenant_isolation.py` and
`tests/test_agent_credential_bindings.py` provide regression entry points.
Password regressions in `tests/test_users.py` reopen real SQLite after cancellation
or failure at the password, revocation and commit boundaries; `tests/test_auth_routes.py`
checks the current-token exception. `tests/test_attachments.py` covers upload and
download of ASCII, Chinese and emoji filenames at the final byte boundary.

SSO, self-service password recovery, billing and hostile-tenant sandboxing are
not implied by account support. The current storage model validates a complete
baseline; it does not replay the old first-account adoption/backfill workflow.
The [original design](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/multi-tenancy.md)
and [retired token rotation](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/token-rotation.md)
remain historical evidence of those choices.
